#!/usr/bin/env python3
"""Smoke-test reply assistant profile mapping with offline sample resumes."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "liepin-reply-assistant-extension"


NODE_SCRIPT = r"""
const fs = require('fs');
const vm = require('vm');

const root = process.argv[2];
const sandbox = {
  window: {},
  console,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const file of ['match-profiles.js']) {
  vm.runInContext(fs.readFileSync(`${root}/${file}`, 'utf8'), sandbox, { filename: file });
}

const profiles = sandbox.window.LIEPIN_MATCH_PROFILES || {};

function scoreResumeAgainstProfile(resume, profile) {
  const text = resume.fullText;
  let score = profile.base || 50;
  const matched = [];
  const risks = [];
  const categories = {
    company: false,
    fabLine: false,
    coreSkill: false,
    engineering: false,
    seniority: false,
    education: false,
    city: false
  };

  for (const rule of profile.targetCompanyRules || []) {
    if (rule.re.test(text)) {
      score += rule.points;
      matched.push(rule.text);
      categories.company = true;
      break;
    }
  }

  let skillHits = 0;
  for (const rule of profile.skillRules || []) {
    if (rule.re.test(text)) {
      score += rule.points;
      matched.push(rule.text);
      skillHits += 1;
      if (profile.domain === 'fpga') {
        if (/FPGA|逻辑架构|RTL|关键模块|时序|CDC|PWM|采样同步|编码器|数据通路|保护逻辑/.test(rule.text)) categories.coreSkill = true;
        if (/仿真|验证|调试|时序|CDC|收敛|问题定位|bring-up|板级/.test(rule.text)) categories.engineering = true;
      } else if (profile.domain === 'hardware_platform') {
        if (/驱控|控制器|驱动器|硬件平台|硬件架构|数字|模拟|电源|采样|编码器|隔离保护|关键硬件方案/.test(rule.text)) categories.coreSkill = true;
        if (/bring-up|波形|边界|EMC|热设计|可靠性|DFM|DFT|认证|生产导入|量产|调试/.test(rule.text)) categories.engineering = true;
      } else if (profile.domain === 'quality_pqe') {
        if (/12吋|12寸|12英寸|300mm|300 mm|晶圆厂|Fab|fab|晶圆制造|晶圆产线|半导体产线|前道/.test(rule.text)) categories.fabLine = true;
        if (/loading|负载|装载|上料|载片|SPC|统计过程控制|控制图|过程能力|Minitab|JMP/i.test(rule.text)) categories.coreSkill = true;
        if (/loading|负载|装载|上料|载片|SPC|MSA|GRR|控制图|过程能力|line yield|良率|制程|可靠性|量产质量|NPI|新产品上量|客户审核|报废|质量成本|FMEA|CPK|DOE|质量工具|体系|半导体|晶圆|封装|12吋|12寸|12英寸|300mm|300 mm|Fab|fab|晶圆产线|前道/i.test(rule.text)) categories.engineering = true;
      } else if (profile.domain === 'procurement_semiconductor') {
        if (/采购|寻源|供应商|物料|交期|议价|降本|采购主线/.test(rule.text)) categories.coreSkill = true;
        if (/机加工|结构件|BOM|导入|半导体设备|产线|库存|交付|订单|异常|谈判/.test(rule.text)) categories.engineering = true;
      } else {
        if (/运动台|气浮|直驱|纳米|定位|硬件|电控|电路|FPGA|高速信号|运动控制|伺服|电机驱动/.test(rule.text)) categories.coreSkill = true;
        if (/机械|工程|仿真|工具|硬件|测试|仪器|调试|量产|EMC|可靠性/.test(rule.text)) categories.engineering = true;
      }
      if (/技术负责|团队带教|评审规范|规范沉淀|代码评审|模块负责|质量专项|主管|专家|主任|负责人|经理|产品线|产品负责人|跨部门|客户推进/.test(rule.text)) categories.seniority = true;
    }
  }

  for (const rule of profile.educationRules || []) {
    if (rule.re.test(text)) {
      score += rule.points;
      matched.push(rule.text);
      categories.education = true;
      break;
    }
  }

  for (const rule of profile.cityRules || []) {
    if (rule.re.test(text)) {
      score += rule.points;
      matched.push(rule.text);
      categories.city = true;
      break;
    }
  }

  for (const rule of profile.riskRules || []) {
    if (rule.re.test(text)) risks.push(rule.text);
  }

  const seniorityEvidenceRe = profile.domain === 'fpga'
    ? /FPGA主管|FPGA经理|FPGA负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|代码评审|设计规范|仿真规范|规范沉淀|问题复盘|团队带教|带教/
    : profile.domain === 'hardware_platform'
      ? /硬件主管|硬件经理|硬件负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|设计规范|技术评审|评审机制|规范沉淀|方案复盘|团队带教|带教/
      : profile.domain === 'quality_pqe'
        ? /PQE主管|PQE经理|CQE主管|CQE经理|质量主管|质量经理|品质主管|品质经理|主任工程师|资深|高级|专家|质量负责人|客户质量负责人|MRB主导|MRB会议|质量专项|8D负责人|FA负责人|QRA负责人|Leader|Lead|Staff/i
        : profile.domain === 'procurement_semiconductor'
          ? /采购主管|采购经理|供应链经理|主采|双采购|负责人|专家/
          : null;
  if (seniorityEvidenceRe && seniorityEvidenceRe.test(text)) categories.seniority = true;

  if (profile.domain === 'quality_pqe') {
    const fabLineEvidenceRe = /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|晶圆厂|Fab|fab|wafer\s*fab|前道|晶圆制造|晶圆产线|wafer\s*line|半导体产线|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix/i;
    if (fabLineEvidenceRe.test(text)) categories.fabLine = true;
  }

  if (profile.domain === 'quality_pqe' && !categories.fabLine) {
    score = Math.min(score - 16, 72);
    risks.push(profile.fabLineGapText || '12吋fab产线背景不明确');
  }
  if (!categories.coreSkill) {
    score -= 10;
    risks.push(profile.coreSkillGapText || '未明显看到运动台/气浮/精密定位主线');
  }
  if (!categories.company) {
    score -= 6;
    risks.push(profile.targetCompanyGapText || '目标公司相似度不够明确');
  }
  if (!categories.engineering) {
    score -= 5;
    risks.push(profile.engineeringGapText || '机械设计/仿真/工程落地信息不足');
  }
  if (!categories.seniority && ['hardware_platform', 'fpga', 'quality_pqe'].includes(profile.domain)) {
    score = Math.min(score, 78);
    risks.push(profile.seniorityGapText || '主管/资深专家层级待核实');
  }
  if (!categories.city) {
    score -= 3;
    risks.push(profile.cityGapText || '苏州或长三角接受度待确认');
  }
  if (skillHits >= 4) score += 5;

  const bounded = Math.max(20, Math.min(96, score));
  return { score: bounded, matched: [...new Set(matched)].slice(0, 8), risks: [...new Set(risks)].slice(0, 6) };
}

const cases = [
  {
    name: '微导纳米双采购岗',
    profileKey: 'weida_procurement',
    expectedDomain: 'procurement_semiconductor',
    minScore: 82,
    text: '微导纳米 双采购岗 候选人来自半导体设备供应链，本科，苏州。担任采购经理/主采，负责机加工件、结构件、钣金、标准件和电子料采购，主导寻源、询价、比价、议价、供应商开发、供应商审核、交期缺料异常、BOM替代料导入、降本和框架协议。'
  },
  {
    name: '鹏新旭PQE',
    profileKey: 'pengxinxu_pqe',
    expectedDomain: 'quality_pqe',
    minScore: 82,
    text: '鹏新旭 PQE专家 候选人本科，深圳。曾在中芯12吋/300mm晶圆厂前道产线做PQE主任工程师，主导SPC控制图、CPK、Minitab、loading effect装载负载异常、line yield良率、MRB、8D、FA和客户审核闭环。'
  },
  {
    name: '苏科思硬件技术主管',
    profileKey: 'sukesi_hardware_manager',
    expectedDomain: 'hardware_platform',
    minScore: 82,
    text: '苏科思 硬件技术主管 候选人硕士，苏州。来自汇川，硬件主管，负责伺服驱动器、运动控制器和驱控硬件平台架构，数字电路、模拟电路、电源设计、采样链路、编码器接口、隔离保护、EtherCAT、原理图PCB、bring-up、EMC、可靠性和量产导入。'
  },
  {
    name: '苏科思FPGA技术主管',
    profileKey: 'sukesi_fpga',
    expectedDomain: 'fpga',
    minScore: 82,
    text: '苏科思 FPGA技术主管 候选人硕士，苏州。来自雷赛，FPGA负责人，负责SoC FPGA、RTL、Verilog、逻辑架构、模块划分、PWM、采样同步、编码器接口、EtherCAT、保护逻辑、数据通路、时钟规划、时序约束、CDC、仿真验证、板级调试和团队代码评审。'
  }
];

const results = cases.map(item => {
  const profile = profiles[item.profileKey];
  const result = profile ? scoreResumeAgainstProfile({ fullText: item.text }, profile) : null;
  return {
    name: item.name,
    profileKey: item.profileKey,
    expectedDomain: item.expectedDomain,
    actualDomain: profile && profile.domain,
    score: result && result.score,
    matched: result && result.matched,
    risks: result && result.risks,
    pass: Boolean(profile && profile.domain === item.expectedDomain && result.score >= item.minScore)
  };
});

console.log(JSON.stringify(results, null, 2));
if (results.some(item => !item.pass)) process.exit(1);
"""


def main() -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(NODE_SCRIPT)
        script_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["node", str(script_path), str(EXTENSION)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        script_path.unlink(missing_ok=True)

    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    print(proc.stdout, end="")
    if proc.returncode != 0:
        print("回复助手岗位映射 smoke 未通过", file=sys.stderr)
        return proc.returncode
    print("回复助手岗位映射 smoke 通过: 采购、PQE、硬件、FPGA 均命中预期 profile/domain")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
