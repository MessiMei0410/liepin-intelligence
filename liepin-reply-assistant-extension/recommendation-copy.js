(function (root) {
  'use strict';

  function clean(value) {
    return String(value || '')
      .replace(/\u00a0/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function compact(value, maxLength) {
    const text = clean(value);
    if (!text) return '';
    return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
  }

  function uniqueList(items, maxLength) {
    if (!Array.isArray(items)) return [];
    return [...new Set(items.map(clean).filter(Boolean))].slice(0, maxLength);
  }

  function joinParts(parts, fallback) {
    const text = parts.map(clean).filter(Boolean).join('；');
    return text || fallback;
  }

  function numbered(items) {
    return items.map((item, index) => `${index + 1}. ${item}`).join('\n');
  }

  function firstSentence(text, fallback) {
    const value = clean(text);
    if (!value) return fallback;
    return value.replace(/[。；;]$/, '');
  }

  function splitLines(value) {
    return String(value || '')
      .split('\n')
      .map(clean)
      .filter(Boolean);
  }

  function pushUnique(list, value, maxLength) {
    const text = clean(value);
    if (!text || list.includes(text) || list.length >= maxLength) return;
    list.push(text);
  }

  const MECHANICAL_EVIDENCE_RE = /运动台|气浮|静压|直驱|直线电机|平面电机|微动平台|宏动平台|龙门|光栅|定位平台|纳米级|亚纳米|精密定位|光机|光刻|物镜|准分子激光|半导体前道|前道|量测|OCD|OVL|膜厚|晶圆|wafer|键合|bonding|封装|机械设计|结构设计|非标机械|详细设计|二维出图|BOM|装配调试|公差|加工|选型|方案设计|ANSYS|Ansys|Abaqus|COMSOL|有限元|热分析|模态|动力学|刚度|振动|隔振|减振|SolidWorks|solidworks|Creo|PROE|Pro\/E|UG|NX|CAD|AutoCAD|Catia|catia/i;
  const HARDWARE_PLATFORM_EVIDENCE_RE = /驱控|驱动器|控制器|伺服控制器|伺服驱动|高性能驱动|运动控制|工业控制器|硬件平台|硬件架构|总体架构|需求分解|平台化|标准化|复用|接口定义|关键器件选型|方案评审|原理方案|设计取舍|数字电路|模拟电路|电源完整性|电源设计|采样链路|采样电路|编码器接口|隔离保护|保护机制|通信接口|接口电路|EtherCAT|CAN|RS485|LVDS|原理图|PCB|板卡|bring-up|样机调试|板级调试|硬件调试|波形分析|示波器|逻辑分析仪|边界工况|根因|问题定位|整改闭环|系统联调|EMC|EMI|热设计|可靠性|DFM|DFT|安规|认证测试|环境测试|生产导入|量产导入|工程变更|量产问题|可制造性/i;
  const AUTOMATION_SOFTWARE_EVIDENCE_RE = /自动化软件|设备软件|控制软件|运动控制|Motion|伺服控制|多轴控制|轴控|插补|轨迹规划|C\+\+|C#|\.NET|WPF|Qt|上位机|HMI|SCADA|PLC|TwinCAT|Beckhoff|倍福|EtherCAT|CANopen|Modbus|OPC|SECS\/GEM|半导体设备|设备控制|装备控制|产线自动化|视觉定位|机器视觉|相机|标定|机器人控制|CNC|数控|调试|联调|软件架构|模块化|异常处理|日志|通信协议|状态机|工艺流程|Recipe|配方|MES|自动化/i;
  const FPGA_EVIDENCE_RE = /FPGA|SoC FPGA|RTL|Verilog|SystemVerilog|VHDL|Vivado|Quartus|Xilinx|Intel FPGA|Altera|逻辑架构|模块划分|资源规划|可复用IP|接口规范|平台化模块|逻辑设计|PWM|采样同步|编码器接口|总线通信|EtherCAT|CAN|高速接口|光纤总线|故障保护|保护逻辑|数据通路|控制时序|时钟规划|时序约束|CDC|综合实现|时序收敛|资源优化|低延迟|低抖动|仿真验证|板级验证|回归测试|在线调试|逻辑分析仪|示波器|bring-up|边界工况|问题定位/i;
  const POWER_MARKETING_EVIDENCE_RE = /三次电源|多相|Multiphase|VRM|CPU\s*VR|VRD|VR14|SVID|SVI3|Power Stage|SPS|Smart Power Stage|DrMOS|POL|Point of Load|eFuse|Buck|DC-DC|DCDC|服务器|AI服务器|PC|主板|显卡|GPU|ODM|OEM|技术市场|产品市场|Technical Marketing|Product Marketing|产品定义|产品规划|roadmap|GTM|竞品分析|市场分析|方案推广|客户推广|Design[- ]?in|design[- ]?in|design win|FAE|AE|应用工程|原理图|schematic|PCB review|Layout review|板级调试|EVB|demo board|参考设计|datasheet|应用笔记/i;
  const QUALITY_PQE_EVIDENCE_RE = /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|晶圆厂|Fab|wafer\s*fab|前道|晶圆制造|晶圆产线|wafer\s*line|半导体产线|loading|Loading|负载|装载|上料|载片|片盒|wafer\s*loading|loading\s*effect|微负载|负载效应|SPC|统计过程控制|控制图|过程能力|CPK|Cpk|PPK|Ppk|Minitab|JMP|MSA|测量系统分析|GRR|Gage\s*R&R|Gauge\s*R&R|量具重复性|再现性|量测系统|测量仪器认证|PQE|CQE|Product Quality|Customer Quality|产品质量|制程质量|过程质量|客户质量|品质工程|QE|QRA|QRE|客诉|客户投诉|客户审核|客户稽核|外审|audit|8D|FA|失效分析|异常处理|质量改善|质量闭环|RMA|MRB|PA改善|PCCB|CCR|CAR|5Why|鱼骨图|line\s*yield|良率|Yield|报废|质量成本|新产品上量|上量管控|可靠性|可靠性验证|量产质量|NPI质量|项目质量|FMEA|PFMEA|DOE|QMS|ISO9001|IATF16949|APQP|PPAP|QC七大手法|半导体|晶圆|芯片|集成电路|wafer|CP|FT|WAT|DRAM|NAND/i;
  const PROCUREMENT_EVIDENCE_RE = /采购|寻源|议价|比价|定点|开模|询价|供应商开发|供应商管理|交期|缺料|呆滞|降本|成本优化|合同|商务|机加工件|结构件|钣金|标准件|非标件|电子料|备件|BOM|图纸|规格书|替代料|样品|试产|导入|供应链计划|PMC|S&OP|库存|周转|安全库存|交付|排产|物流|仓储|采购订单|PO|供应商审核|稽核|质量异常|来料异常|8D|对账|发票|付款|谈判|成本拆解|框架协议/i;
  const ACTION_RE = /负责|担任|主导|参与|设计|开发|搭建|优化|验证|调试|交付|量产|选型|仿真|分析|制定|输出|解决|跟进|验收|采购|沟通|测试|实验/i;
  const TITLE_RE = /工程师|经理|专家|主管|负责人|架构师|设计师|研发|总监|部长|主任|产品市场|技术市场|FAE|AE|leader|manager|engineer|marketing/i;
  const COMPANY_RE = /公司|集团|科技|半导体|设备|电子|微电子|精科|研究所|研究院|中心|厂|Ltd|Inc|Corp|士兰微|英飞凌|Infineon|瑞萨|Renesas|芯朋微|德州仪器|Texas Instruments|\bTI\b|MPS|Monolithic|Power Integrations|安森美|onsemi|维谛|Vertiv|新华三|H3C|超聚变|浪潮|Inspur|联想|Lenovo|富士康|Foxconn|记忆科技/i;
  const BAD_WORK_LINE_RE = /职责|业绩|所在部门|工作地点|下属人数|汇报对象|项目职务|项目描述|项目职责|项目业绩|机械\/设备|电子\/半导体|人$|^\d+-\d+人$/;

  function profileDomain(profile) {
    return clean(profile && profile.domain);
  }

  function evidenceReForProfile(profile) {
    const domain = profileDomain(profile);
    if (domain === 'hardware_platform') return HARDWARE_PLATFORM_EVIDENCE_RE;
    if (domain === 'automation_software') return AUTOMATION_SOFTWARE_EVIDENCE_RE;
    if (domain === 'fpga') return FPGA_EVIDENCE_RE;
    if (domain === 'power_marketing') return POWER_MARKETING_EVIDENCE_RE;
    if (domain === 'quality_pqe') return QUALITY_PQE_EVIDENCE_RE;
    if (domain === 'procurement_semiconductor') return PROCUREMENT_EVIDENCE_RE;
    return MECHANICAL_EVIDENCE_RE;
  }

  function actionThemeReForProfile(profile) {
    const domain = profileDomain(profile);
    if (domain === 'hardware_platform') {
      return /驱控|驱动器|控制器|硬件平台|硬件架构|方案|原理|采样|编码器|隔离|保护|接口|电源|EMC|热设计|可靠性|认证|量产|生产导入|bring-up|波形|调试|验证|问题定位/;
    }
    if (domain === 'automation_software') {
      return /自动化软件|设备软件|控制软件|运动控制|伺服|多轴|轴控|插补|轨迹|C\+\+|C#|\.NET|WPF|Qt|上位机|HMI|PLC|TwinCAT|EtherCAT|CANopen|SECS\/GEM|半导体设备|设备控制|视觉|机器人控制|CNC|联调|软件架构|通信协议|状态机|Recipe|MES/;
    }
    if (domain === 'fpga') {
      return /FPGA|RTL|逻辑|模块|IP|PWM|采样同步|编码器|总线|保护|数据通路|控制时序|时钟|时序|CDC|仿真|板级验证|在线调试/;
    }
    if (domain === 'power_marketing') {
      return /三次电源|多相|VRM|DrMOS|POL|Power Stage|服务器|AI服务器|PC|主板|ODM|技术市场|产品市场|产品定义|roadmap|GTM|竞品|客户推广|Design[- ]?in|design[- ]?in|FAE|AE|原理图|PCB|调试|验证|参考设计/i;
    }
    if (domain === 'quality_pqe') {
      return /12吋|12寸|12英寸|300\s*mm|300mm|晶圆厂|Fab|wafer\s*fab|前道|晶圆制造|晶圆产线|wafer\s*line|半导体产线|loading|Loading|负载|装载|上料|载片|片盒|wafer\s*loading|loading\s*effect|微负载|负载效应|SPC|统计过程控制|控制图|过程能力|CPK|PPK|Minitab|JMP|MSA|测量系统分析|GRR|Gage\s*R&R|Gauge\s*R&R|量测系统|测量仪器认证|PQE|CQE|产品质量|制程质量|客户质量|品质|QE|QRA|客户审核|外审|异常|闭环|line\s*yield|良率|Yield|新产品上量|量产质量|NPI|半导体|晶圆/i;
    }
    if (domain === 'procurement_semiconductor') {
      return /采购|寻源|议价|比价|定点|询价|供应商|交期|缺料|呆滞|降本|成本|合同|商务|机加工件|结构件|钣金|标准件|非标件|BOM|图纸|规格书|替代料|试产|导入|供应链|PMC|库存|采购订单|PO|审核|稽核|来料异常|8D|对账|付款|谈判|框架协议/i;
    }
    return /结构|主轴|转台|仿真|二维|三维|图纸|刚度|承载|气浮|静压|直驱|设计|调试|验证/;
  }

  function isNoiseLine(line) {
    return /求职意向|工作经历|项目经历|教育经历|语言能力|我的技能|自我评价|附加信息|简历备注|查看联系方式|推荐职位|立即沟通/.test(line);
  }

  function usefulLines(value) {
    return splitLines(value)
      .filter(line => !isNoiseLine(line))
      .filter(line => line.length <= 180);
  }

  function findFirstLine(lines, re) {
    if (typeof re === 'function') return lines.find(line => re(line)) || '';
    return lines.find(line => re.test(line)) || '';
  }

  function isCompanyLine(line) {
    return COMPANY_RE.test(line) &&
      !BAD_WORK_LINE_RE.test(line) &&
      line.length <= 70;
  }

  function isTitleLine(line) {
    return TITLE_RE.test(line) &&
      !/职责|公司|集团|科技|项目/.test(line) &&
      line.length <= 56;
  }

  function parseDateRange(line) {
    const text = clean(line);
    const match = text.match(/((?:19|20)\d{2})(?:[./-]|年)(\d{1,2})月?\s*(?:-|–|—|至|~|～)\s*(至今|今|((?:19|20)\d{2})(?:[./-]|年)(\d{1,2})月?)/);
    if (!match) return null;
    const startYear = Number(match[1]);
    const startMonth = Number(match[2]);
    const now = new Date();
    const endYear = /至今|今/.test(match[3]) ? now.getFullYear() : Number(match[4]);
    const endMonth = /至今|今/.test(match[3]) ? now.getMonth() + 1 : Number(match[5]);
    const startIndex = startYear * 12 + startMonth;
    const endIndex = endYear * 12 + endMonth;
    return {
      text: match[0],
      months: Math.max(0, endIndex - startIndex + 1)
    };
  }

  function normalizeDateText(value) {
    return clean(value).replace(/[（）()]/g, '').replace(/,\s*.*$/, '');
  }

  function buildWorkSegments(workLines, profile) {
    const evidenceRe = evidenceReForProfile(profile);
    const segments = [];
    for (let i = 0; i < workLines.length; i += 1) {
      const company = workLines[i];
      const date = parseDateRange(workLines[i + 1] || '');
      if (!isCompanyLine(company) || !date) continue;
      const nextOffset = workLines.slice(i + 2).findIndex((line, index, rest) =>
        isCompanyLine(line) && parseDateRange(rest[index + 1] || '')
      );
      const end = nextOffset >= 0 ? i + 2 + nextOffset : workLines.length;
      const lines = workLines.slice(i, end).filter(Boolean);
      const body = lines.slice(2);
      const title = findFirstLine(body, line =>
        isTitleLine(line) &&
        !/所在部门|团队负责人/.test(line)
      );
      const coreLine = body.find(line => line !== title && !isTitleLine(line) && evidenceRe.test(line) && ACTION_RE.test(line)) ||
        body.find(line => line !== title && !isTitleLine(line) && evidenceRe.test(line)) ||
        '';
      segments.push({
        date,
        lines,
        company,
        title,
        coreLine
      });
      i = end - 1;
    }
    return segments;
  }

  function buildProjectSegments(projectLines, profile) {
    const evidenceRe = evidenceReForProfile(profile);
    const actionThemeRe = actionThemeReForProfile(profile);
    const segments = [];
    for (let i = 0; i < projectLines.length; i += 1) {
      const title = projectLines[i];
      const date = parseDateRange(projectLines[i + 1] || '');
      const titleLike = date &&
        title.length <= 70 &&
        !/项目职务|项目描述|项目职责|项目业绩|显示其他/.test(title);
      if (!titleLike) continue;
      const nextOffset = projectLines.slice(i + 2).findIndex((line, index, rest) => {
        const nextDate = parseDateRange(rest[index + 1] || '');
        return nextDate &&
          line.length <= 70 &&
          !/项目职务|项目描述|项目职责|项目业绩|显示其他/.test(line);
      });
      const end = nextOffset >= 0 ? i + 2 + nextOffset : projectLines.length;
      const lines = projectLines.slice(i, end).filter(Boolean);
      const body = lines.slice(2);
      const coreLines = body.filter(line =>
        evidenceRe.test(line) ||
        (ACTION_RE.test(line) && actionThemeRe.test(line))
      );
      segments.push({
        title,
        date,
        lines,
        coreLines
      });
      i = end - 1;
    }
    return segments;
  }

  function collectEducationFacts(educationLines, maxLength) {
    const facts = [];
    for (let i = 0; i < educationLines.length; i += 1) {
      if (!/大学|学院|学校|研究所|院校|University|Institute|College/i.test(educationLines[i])) continue;
      const nextSchoolOffset = educationLines.slice(i + 1).findIndex(line =>
        /大学|学院|学校|研究所|院校|University|Institute|College/i.test(line)
      );
      const end = nextSchoolOffset >= 0 ? i + 1 + nextSchoolOffset : Math.min(educationLines.length, i + 8);
      const block = educationLines.slice(i, end);
      const school = block[0];
      const major = block.find(line =>
        /机械|自动化|工程|光学|仪器|电子|物理|材料|设计|制造|控制|计算机|软件/.test(line) &&
        !/大学|学院|学校|研究所|院校/.test(line)
      ) || '';
      const degree = block.find(line => /博士后|博士|硕士|本科|大专|MBA/.test(line)) || '';
      const time = block.find(line => /(?:19|20)\d{2}\.\d{2}\s*-\s*(?:至今|(?:19|20)\d{2}\.\d{2})/.test(line)) || '';
      const tags = uniqueList(block.map(line => line.match(/统招|非统招|985|211/)?.[0]).filter(Boolean), 4);
      if (degree || major || time) {
        pushUnique(facts, [time, school, major, degree, ...tags].filter(Boolean).join('｜'), maxLength);
      }
      if (facts.length >= maxLength) return facts;
    }
    if (facts.length) return facts;

    educationLines.forEach((line, index) => {
      if (!/博士后|博士|硕士|本科|大专|MBA|统招|非统招/.test(line)) return;
      const near = educationLines
        .slice(Math.max(0, index - 2), index + 3)
        .filter(item => /博士后|博士|硕士|本科|大专|MBA|统招|非统招|大学|学院|机械|自动化|工程|电子|光学|材料|20\d{2}/.test(item))
        .slice(0, 4);
      pushUnique(facts, near.join('｜'), maxLength);
    });
    if (!facts.length) {
      const fallback = educationLines
        .filter(line => /大学|学院|机械|自动化|工程|电子|光学|材料|20\d{2}/.test(line))
        .slice(0, 4)
        .join('｜');
      if (fallback) pushUnique(facts, fallback, maxLength);
    }
    return facts;
  }

  function segmentLabel(segment) {
    return [segment.date && normalizeDateText(segment.date.text), segment.company, segment.title]
      .map(clean)
      .filter(Boolean)
      .join('｜');
  }

  function extractResumeEvidence(resume, profile) {
    const workLines = usefulLines(resume && resume.workRawText);
    const projectLines = usefulLines(resume && resume.projectRawText);
    const educationLines = usefulLines(resume && resume.educationRawText);
    const workSegments = buildWorkSegments(workLines, profile);
    const projectSegments = buildProjectSegments(projectLines, profile);
    const evidence = [];

    if (resume && resume.titleCompanyLine) {
      pushUnique(evidence, `简历顶部显示：${compact(resume.titleCompanyLine, 96)}`, 8);
    }

    workSegments
      .filter(segment => segment.coreLine || segment.company || segment.title)
      .slice(0, 4)
      .forEach(segment => {
        const label = segmentLabel(segment);
        const source = segment.coreLine ? `；原文：${compact(segment.coreLine, 110)}` : '';
        if (label || source) pushUnique(evidence, `工作经历显示：${label || '未识别公司/职位'}${source}`, 8);
      });

    projectSegments
      .filter(segment => segment.coreLines.length)
      .slice(0, 2)
      .forEach(segment => {
        const source = segment.coreLines.slice(0, 2).join('；');
        pushUnique(
          evidence,
          `项目经历显示：${normalizeDateText(segment.date.text)}｜${segment.title}；原文：${compact(source, 120)}`,
          8
        );
      });

    collectEducationFacts(educationLines, 3).forEach(fact => {
      pushUnique(evidence, `教育经历显示：${compact(fact, 110)}`, 8);
    });

    return evidence.length ? evidence.slice(0, 6) : ['简历暂未提取到足够具体项目证据，需要人工补看工作经历、项目经历和教育经历'];
  }

  function extractResumeRisks(resume, profile) {
    const intention = clean(resume && resume.intentionText);
    const statusText = clean(resume && resume.statusText);
    const workLines = usefulLines(resume && resume.workRawText);
    const projectLines = usefulLines(resume && resume.projectRawText);
    const educationLines = usefulLines(resume && resume.educationRawText);
    const fullLines = usefulLines(resume && resume.fullText);
    const educationText = clean((resume && resume.educationRawText) || educationLines.join('\n'));
    const combinedLines = [
      statusText,
      intention,
      resume && resume.titleCompanyLine,
      ...workLines,
      ...projectLines,
      ...educationLines,
      ...fullLines
    ].map(clean).filter(Boolean);
    const text = clean(combinedLines.join('\n'));
    const risks = [];
    const clientName = clean(profile && profile.client) || '客户';
    const locationName = clean(profile && profile.targetLocationName) || '苏州/长三角';
    const locationRe = profile && profile.targetRegionRe instanceof RegExp
      ? profile.targetRegionRe
      : /苏州|上海|无锡|杭州|嘉兴|南京|长三角/;

    const educationFacts = collectEducationFacts(educationLines, 4);
    const educationSource = educationFacts.length ? educationFacts.join('；') : compact(educationText, 120);
    const degreeMatches = uniqueList((educationText.match(/博士后|博士|硕士|本科|大专|MBA/g) || []), 6);
    if (!degreeMatches.length) {
      pushUnique(risks, '学历风险：教育经历未识别到博士/硕士/本科/大专/MBA，需人工复核学历字段', 8);
    } else if (degreeMatches.includes('大专') && !degreeMatches.some(item => /博士后|博士|硕士|本科|MBA/.test(item))) {
      pushUnique(risks, `学历风险：教育经历显示“${compact(educationSource, 88)}”，最高学历疑似大专，需确认是否满足客户学历门槛`, 8);
    } else {
      pushUnique(risks, `学历核实：教育经历显示“${compact(educationSource, 88)}”，需确认最高学历、统招和学信网`, 8);
    }
    if (/非统招|成人|自考|函授|网络教育/.test(educationText)) {
      pushUnique(risks, `统招风险：教育经历出现“${compact(findFirstLine(educationLines, /非统招|成人|自考|函授|网络教育/) || educationSource, 70)}”，需确认客户是否接受`, 8);
    } else if (degreeMatches.length && !/统招/.test(educationText)) {
      pushUnique(risks, '统招核实：教育经历未明确出现“统招”字样，如客户卡统招需单独确认', 8);
    }

    const statusSource = statusText ||
      findFirstLine(combinedLines, /已离职|离职|急寻新工作|在职|暂无跳槽|暂不考虑|不看机会|不感兴趣/);
    if (/已离职|离职|急寻新工作/.test(statusSource)) {
      pushUnique(risks, `离职状态：简历显示“${compact(statusSource, 70)}”，需确认离职原因、空窗期和到岗时间`, 8);
    } else if (/暂无跳槽|暂不考虑|不看机会|不感兴趣/.test(statusSource)) {
      pushUnique(risks, `意愿风险：简历显示“${compact(statusSource, 70)}”，需低压确认是否愿意看${clientName}机会`, 8);
    } else if (/在职/.test(statusSource)) {
      pushUnique(risks, `在职状态：简历显示“${compact(statusSource, 70)}”，需确认看机会意愿、沟通窗口和离职周期`, 8);
    } else {
      pushUnique(risks, '状态核实：简历未明确识别在职/离职状态，需沟通确认当前状态和到岗周期', 8);
    }

    const workSegments = buildWorkSegments(workLines, profile).filter(segment => segment.date);
    const shortSegments = workSegments.filter(segment => segment.date.months > 0 && segment.date.months < 18);
    if (shortSegments.length >= 2) {
      const detail = shortSegments.slice(0, 3).map(segment => `${segmentLabel(segment)}约${segment.date.months}个月`).join('；');
      pushUnique(risks, `跳槽频率风险：识别到${shortSegments.length}段任职不足18个月（${compact(detail, 100)}），需确认每次变动原因`, 8);
    } else if (shortSegments.length === 1) {
      const segment = shortSegments[0];
      pushUnique(risks, `稳定性核实：有1段任职不足18个月（${compact(`${segmentLabel(segment)}约${segment.date.months}个月`, 90)}），建议了解变动原因`, 8);
    } else if (workSegments.length) {
      pushUnique(risks, `跳槽频率核实：工作经历识别到${workSegments.length}段带时间的任职记录，未自动发现多段短任职，但仍需核实关键离职原因`, 8);
    } else {
      pushUnique(risks, '跳槽频率核实：工作经历时间段未完整识别，需人工查看任职起止时间和变动次数', 8);
    }

    if (intention) {
      const acceptsTargetLocation = locationRe.test(intention);
      if (/成都|北京|深圳|广州|武汉|西安|天津|重庆|长沙|郑州|合肥|厦门|东莞/.test(intention) && !acceptsTargetLocation) {
        pushUnique(risks, `地点风险：求职意向显示“${compact(intention, 78)}”，需确认是否接受${locationName}`, 8);
      } else if (!acceptsTargetLocation) {
        pushUnique(risks, `地点核实：求职意向显示“${compact(intention, 78)}”，未明确${locationName}接受度`, 8);
      }
    } else {
      pushUnique(risks, `地点核实：未识别到求职意向城市，需确认是否接受${locationName}`, 8);
    }

    const salaryLine = findFirstLine(combinedLines, /(\d+)\s*-\s*(\d+)\s*[kK]|(\d+)\s*[kK]|薪|总包|年薪|月薪|期望薪资|目前薪资/);
    if (salaryLine) {
      pushUnique(risks, `薪资核实：简历/意向中出现“${compact(salaryLine, 78)}”，需确认当前包、期望包和客户预算`, 8);
    } else {
      pushUnique(risks, '薪资风险：未识别到明确薪资预期，需沟通当前薪资结构和期望区间', 8);
    }

    const domain = profileDomain(profile);
    const isHardwareProfile = domain === 'hardware_platform' || /硬件|电控|电路/.test(clean(profile && profile.position));
    const isAutomationSoftwareProfile = domain === 'automation_software' || /自动化软件|控制软件|设备软件|运动控制|上位机|C\+\+/.test(clean(profile && profile.position));
    const isFpgaProfile = domain === 'fpga' || /FPGA/i.test(clean(profile && profile.position));
    const isPowerMarketingProfile = domain === 'power_marketing' || /技术市场|产品市场|三次电源|服务器.*电源|VRM|DrMOS|POL|Power Stage/i.test(clean(profile && profile.position));
    const isQualityPqeProfile = domain === 'quality_pqe' || /PQE|产品质量|制程质量|客户质量|品质专家/i.test(clean(profile && profile.position));
    const isProcurementProfile = domain === 'procurement_semiconductor' || /采购|供应链|供应商|寻源|物料/.test(clean(profile && profile.position));
    const directionLine = findFirstLine(
      combinedLines,
      isFpgaProfile
        ? /销售|质量|工艺\/制程|工艺工程师|设备维护|售后|软件开发|算法工程师|机械设计|结构设计|FAE|技术支持/
        : isAutomationSoftwareProfile
        ? /纯销售|质量|工艺\/制程|工艺工程师|设备维护|售后|机械设计|结构设计|硬件测试|硬件工程师|嵌入式硬件|电气接线|配电|厂务|气化|系统集成/
        : isPowerMarketingProfile
        ? /纯销售|渠道销售|质量|工艺\/制程|工艺工程师|封装|测试|热设计|设备维护|售后|软件开发|算法工程师|机械设计|结构设计|一次电源|二次电源|适配器|充电器|逆变器|光伏|储能|BMS/
        : isQualityPqeProfile
        ? /纯销售|渠道销售|生产操作员|质检员|检验员|IPQC|OQC|IQC|仓库|采购|软件开发|算法工程师|机械设计|结构设计|设备维护|售后/
        : isProcurementProfile
        ? /纯销售|软件开发|算法工程师|机械设计|结构设计|质检员|检验员|设备维护|售后/
        : isHardwareProfile
        ? /销售|质量|工艺\/制程|工艺工程师|设备维护|售后|软件开发|算法工程师|机械设计|结构设计|FAE|技术支持/
        : /销售|质量|工艺\/制程|工艺工程师|设备维护|售后|软件|算法|电气/
    );
    if (directionLine) {
      const mainline = isFpgaProfile
        ? 'FPGA逻辑架构、时序验证和板级调试主线'
        : isAutomationSoftwareProfile
        ? '自动化软件、设备控制软件和运动控制主线'
        : isPowerMarketingProfile
        ? '三次电源/服务器或PC电源技术市场、产品定义和客户推广主线'
        : isQualityPqeProfile
        ? '半导体PQE、客户质量、8D/FA闭环、良率和可靠性质量主线'
        : isProcurementProfile
        ? '半导体设备采购、寻源、供应商协同和物料闭环主线'
        : isHardwareProfile
        ? '驱控硬件平台、架构设计、调试验证和量产导入主线'
        : '机械结构/精密运动主线';
      pushUnique(risks, `方向核实：简历出现“${compact(directionLine, 78)}”，需确认是否偏离${mainline}`, 8);
    }
    if (isQualityPqeProfile) {
      const weakFabLine = findFirstLine(
        combinedLines,
        /宜兴中车时代半导体|中车时代半导体|株洲中车时代半导体|中车时代电气|SiC|碳化硅|IGBT|功率器件|功率半导体|车规|封装|封测|化合物半导体|设备供应商/
      );
      const strictFabLine = findFirstLine(
        combinedLines,
        /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|12吋fab|300mm\s*fab|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix/i
      );
      if (weakFabLine && !strictFabLine) {
        pushUnique(risks, `场景风险：简历出现“${compact(weakFabLine, 78)}”，可能是功率器件/化合物/封测/设备或泛半导体质量经历，不能直接按12吋fab loading PQE推荐`, 8);
      }
    }

    return risks.slice(0, 7);
  }

  function matchLine(context) {
    return `匹配点：${context.matchedText}`;
  }

  function riskLine(context) {
    return `风险点：${context.riskText}`;
  }

  function matchDetail(context) {
    return `核心匹配点：\n${numbered(context.evidence)}`;
  }

  function riskDetail(context) {
    return `风险点及需核实事项：\n${numbered(context.resumeRisks)}`;
  }

  function summaryEvidence(context) {
    const primary = context.evidence.find(item => /工作经历显示|项目经历显示|简历顶部显示/.test(item)) ||
      context.evidence[0] ||
      context.matchedText;
    return compact(primary, 128);
  }

  function summaryRisk(context) {
    return compact((context.resumeRisks && context.resumeRisks[0]) || context.riskText, 96);
  }

  function recommendationSummary(context, conclusion) {
    const current = context.current ? `当前背景：${context.current}` : '当前背景：待补充';
    return `推荐总结：${conclusion}。${current}；主要匹配依据：${summaryEvidence(context)}；优先核实：${summaryRisk(context)}`;
  }

  function shortOutreach(context, sentence, question) {
    return `您好，${context.name}，${sentence}${question}`;
  }

  function scoreLabel(result) {
    const score = Number(result && result.score);
    const grade = clean(result && result.grade);
    if (Number.isFinite(score) && grade) return `${score}分，${grade}`;
    if (Number.isFinite(score)) return `${score}分`;
    return grade || '待判断';
  }

  function profileLabel(profile) {
    const client = clean(profile && profile.client);
    const position = clean(profile && profile.position);
    const label = clean(profile && profile.label);
    if (client && position) return `${client}${position}`;
    return label || position || client || '目标岗位';
  }

  function candidateLabel(resume) {
    return clean(resume && resume.name) || '该人选';
  }

  function currentLine(resume) {
    return compact(resume && resume.titleCompanyLine, 56);
  }

  function intentionLine(resume) {
    return compact(resume && resume.intentionText, 70);
  }

  function scenarioOf(result) {
    const score = Number(result && result.score);
    const grade = clean(result && result.grade);
    if ((result && result.benchmark) || /标杆/.test(grade)) return 'benchmark';
    if (/A/.test(grade) || score >= 82) return 'a';
    if (/B/.test(grade) || score >= 68) return 'b';
    if (/C/.test(grade) || score >= 55) return 'c';
    return 'pause';
  }

  function makeContext(result, resume, profile) {
    const matched = uniqueList(result && result.matched, 4);
    const risks = uniqueList(result && result.risks, 4);
    const evidence = extractResumeEvidence(resume || {}, profile || {});
    const resumeRisks = extractResumeRisks(resume || {}, profile || {});
    return {
      name: candidateLabel(resume),
      role: profileLabel(profile),
      current: currentLine(resume),
      intention: intentionLine(resume),
      score: scoreLabel(result || {}),
      action: clean(result && result.action),
      matched,
      risks,
      evidence,
      resumeRisks,
      matchedText: joinParts(matched, '暂无明确命中点'),
      riskText: joinParts(risks, '地点、薪资、机会意愿待确认'),
      evidenceText: joinParts(evidence, '简历暂未提取到足够具体项目证据，需要人工补看项目段'),
      resumeRiskText: joinParts(resumeRisks.length ? resumeRisks : risks, '学历、状态、稳定性、地点和薪资均需进一步确认')
    };
  }

  function buildBenchmarkCopy(context) {
    return {
      clientSummary: numbered([
        `结论：${context.name}为标杆样本，建议只用于校准${context.role}画像，不触达、不推荐`,
        matchLine(context),
        riskLine(context)
      ]),
      outreachGreeting: '标杆样本，不建议触达；请勿外发候选人沟通话术。',
      internalNote: numbered([
        '动作：仅做履历学习和岗位画像对齐',
        matchLine(context),
        `风险点：疑似客户相关履历，不外发、不触达；${context.riskText}`
      ]),
      customerRecommendation: numbered([
        recommendationSummary(context, `${context.name}为${context.score}，但属于标杆样本/疑似客户相关履历，只用于校准${context.role}画像`),
        matchDetail(context),
        '风险点及处理建议：不外发、不触达，只作为岗位画像参考'
      ])
    };
  }

  function buildACopy(context) {
    const current = context.current ? `当前背景：${context.current}` : '当前背景：待补充';
    return {
      clientSummary: numbered([
        `推荐结论：${context.name}，${context.score}，建议优先推荐给客户`,
        current,
        matchLine(context),
        riskLine(context)
      ]),
      outreachGreeting: shortOutreach(context, `看您目前经历和${context.role}方向比较贴近。`, '近期方便约 10 分钟沟通吗？'),
      internalNote: numbered([
        '优先级：A 级，优先触达，沟通顺序为意愿、地点、薪资、项目深度',
        matchLine(context),
        riskLine(context),
        '下一步：先确认看机会意愿和地点薪资，再补项目深度'
      ]),
      customerRecommendation: numbered([
        recommendationSummary(context, `${context.name}为${context.score}，与${context.role}整体匹配度高，建议优先推进`),
        matchDetail(context),
        riskDetail(context),
        '推进建议：建议先电话确认机会意愿、地点接受度和薪资预期，再重点深挖项目职责、技术深度和个人贡献'
      ])
    };
  }

  function buildBCopy(context) {
    const current = context.current ? `当前背景：${context.current}` : '当前背景：待补充';
    const intention = context.intention ? `求职意向：${context.intention}` : '求职意向：待确认';
    return {
      clientSummary: numbered([
        `推荐结论：${context.name}，${context.score}，建议作为可沟通备选`,
        current,
        intention,
        matchLine(context),
        riskLine(context)
      ]),
      outreachGreeting: shortOutreach(context, `这边有一个${context.role}方向机会，和您部分经历有交集。`, '您近期有在看新的机会吗？'),
      internalNote: numbered([
        '优先级：B 级，可低压沟通，先确认意愿和硬性条件',
        matchLine(context),
        riskLine(context),
        '下一步：通过地点、薪资、核心项目真实性核实后再正式推荐'
      ]),
      customerRecommendation: numbered([
        recommendationSummary(context, `${context.name}为${context.score}，与${context.role}有明确交集，可作为备选人选推进`),
        matchDetail(context),
        riskDetail(context),
        '推进建议：建议先低压触达，确认看机会意愿、地点和核心项目真实性；确认通过后再进入正式推荐'
      ])
    };
  }

  function buildCCopy(context) {
    return {
      clientSummary: numbered([
        `推荐结论：${context.name}，${context.score}，建议先复核，不直接推荐`,
        matchLine(context),
        riskLine(context),
        '处理建议：补完整经历、项目职责、地点和意愿后再判断'
      ]),
      outreachGreeting: shortOutreach(context, `想先确认下您目前主要方向是否还在${context.role}相关领域。`, '方便的话我再补充岗位信息。'),
      internalNote: numbered([
        '优先级：C 级复核，先补信息再决定是否触达或推荐',
        matchLine(context),
        riskLine(context),
        '下一步：核实项目职责、技术深度、地点和意愿'
      ]),
      customerRecommendation: numbered([
        recommendationSummary(context, `${context.name}为${context.score}，与${context.role}存在部分线索，暂不建议直接推荐`),
        matchDetail(context),
        riskDetail(context),
        '推进建议：先补齐项目职责、技术深度、地点和意愿；如风险点可解释，再重新评估是否推荐'
      ])
    };
  }

  function buildPauseCopy(context) {
    return {
      clientSummary: numbered([
        `推荐结论：${context.name}，${context.score}，暂缓推荐`,
        matchLine(context),
        riskLine(context),
        '处理建议：除非客户放宽画像，否则不优先投入沟通'
      ]),
      outreachGreeting: '暂缓触达；建议先补充完整经历或等待客户放宽画像后再沟通。',
      internalNote: numbered([
        '优先级：暂缓，不进入优先触达池',
        matchLine(context),
        riskLine(context),
        '下一步：仅在客户放宽画像或补到关键匹配证据后再重评'
      ]),
      customerRecommendation: numbered([
        recommendationSummary(context, `${context.name}为${context.score}，当前与${context.role}匹配度不足，暂不推荐`),
        matchDetail(context),
        riskDetail(context),
        `观察条件：后续如补充到${firstSentence(context.matchedText, '关键匹配证据')}，再重新评估`
      ])
    };
  }

  function buildRecommendationCopy(result, resume, profile) {
    const context = makeContext(result || {}, resume || {}, profile || {});
    const scenario = scenarioOf(result || {});
    if (scenario === 'benchmark') return buildBenchmarkCopy(context);
    if (scenario === 'a') return buildACopy(context);
    if (scenario === 'b') return buildBCopy(context);
    if (scenario === 'c') return buildCCopy(context);
    return buildPauseCopy(context);
  }

  const api = {
    buildRecommendationCopy,
    extractResumeEvidence,
    extractResumeRisks
  };

  root.LIEPIN_RECOMMENDATION_COPY = api;
})(typeof window !== 'undefined' ? window : globalThis);
