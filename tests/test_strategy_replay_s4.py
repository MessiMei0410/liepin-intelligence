"""S4-4：回放评测进回归 —— 两定稿 case 三指标「不许倒退」门槛（CI 形态）。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §6；
指标口径与公司名归一规则见 scripts/strategy_replay_eval.py 模块 docstring；
基线数值与未命中明细见 docs/ASA_strategy_replay_baseline_s4-4_2026-07-23.md。

回放为确定性模式（FakeLLM deterministic_fallback + 临时库 + 真实 KB 只读），
指标可复现，适合进回归门槛；真实 LLM 对照不进本测试。

基线更新手册（策略生成逻辑改动后）：
1. 跑 `PYTHONPATH=scripts /usr/local/bin/python3 scripts/strategy_replay_eval.py --json` 拿新指标；
2. 逐项核对未命中明细（脚本人类可读输出 / 基线文档），确认是能力提升而非口径漂移；
3. 手动更新本文件 REPLAY_BASELINE 常量（并改注释里的日期）+ 同步基线文档；
4. 指标下降一律视为回归：修策略生成逻辑，不得改基线放行。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import strategy_replay_eval as replay  # noqa: E402

# 回放事实源（只读）：真实知识库目录；可用 ASA_KNOWLEDGE_BASE_DIR 覆盖（本测试显式传目录）。
REAL_KB_DIR = Path("/Users/messi/Documents/ASA/knowledge_base")

# 基线：2026-07-23 实跑生成（确定性模式 deterministic_fallback；口径见脚本 docstring §三指标）。
# case_silan_tme：L1，参考池 15 家（T1 4 + T2 11），Agent 池 23 家（kb_profile 15 + kb_graph 8）；
# case_changyue_equipment：L3 裸跑（case meta 声明锚点全缺），参考池 20 家，Agent 池 8 家全偏，
# 三指标为 0 是真实基线而非评测 bug——门槛锚定「不许倒退」，改进落地后按上面流程抬升基线。
REPLAY_BASELINE = {
    "case_silan_tme": {
        "pool_recall": 1.0,
        "pool_precision": 0.6522,
        "keyword_coverage": 1.0,
        "anchor_completeness": 1.0,
    },
    "case_changyue_equipment": {
        "pool_recall": 0.0,
        "pool_precision": 0.0,
        "keyword_coverage": 0.0,
        "anchor_completeness": 0.375,
    },
}


class ReplayBaselineTest(unittest.TestCase):
    """任务 1/3：两 case 各跑回放，三指标 ≥ 基线（不许倒退）；定级与结构口径锁定。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 真实 KB 缺失时 run_replay 抛 ReplayCaseError，测试直接炸出声（不静默全过）。
        cls.results = {case_id: replay.run_replay(case_id, REAL_KB_DIR) for case_id in REPLAY_BASELINE}

    def test_metrics_not_below_baseline(self) -> None:
        for case_id, baseline in REPLAY_BASELINE.items():
            metrics = self.results[case_id]["metrics"]
            for key, floor in baseline.items():
                self.assertGreaterEqual(
                    metrics[key], floor,
                    f"{case_id}.{key}={metrics[key]} 低于基线 {floor}（2026-07-23 口径）——策略生成逻辑疑似倒退",
                )

    def test_silan_structure_and_reference(self) -> None:
        result = self.results["case_silan_tme"]
        assert result["input_level"] == "L1"
        assert result["generation_mode"] == "deterministic_fallback"
        assert result["missing_anchors"] == []
        pool = result["details"]["pool"]
        # 参考池规模锁定（防 case 文件被改坏后分母漂移导致指标失真）
        assert pool["reference_size"] == 15
        assert pool["missed_reference"] == [], "士兰微 case 池必须全覆盖"
        assert result["details"]["keywords"]["covered_groups"] == 3

    def test_changyue_structure_and_reference(self) -> None:
        result = self.results["case_changyue_equipment"]
        assert result["input_level"] == "L3"
        assert result["generation_mode"] == "deterministic_fallback"
        # case meta 声明：友商/客户锚点全部缺失（L3 裸跑口径）
        assert result["missing_anchors"] == ["customer_of_customer", "competitive_landscape"]
        pool = result["details"]["pool"]
        assert pool["reference_size"] == 20
        assert pool["agent_size"] == 8
        # 锚点明细：产品/技术线锚定正确、场景/赛道锚定偏差（0.5）、两锚点缺失
        anchors = result["details"]["anchors"]["anchors"]
        assert anchors["product_tech_line"]["score"] == 1.0
        assert anchors["scenario_track"]["score"] == 0.5
        assert anchors["customer_of_customer"]["score"] == 0.0
        assert anchors["competitive_landscape"]["score"] == 0.0


class ReplayNormalizationTest(unittest.TestCase):
    """任务 3：公司名归一规则单测（别名/括号/后缀/斜杠拆分/泛化条目/图谱别名）。"""

    def test_company_keys_brackets_alias_suffix_slash(self) -> None:
        keys = replay.company_keys("MPS（芯源系统）")
        assert {"mps", "芯源系统"} <= keys
        keys = replay.company_keys("K&S（库力索法）")
        assert {"k&s", "库力索法"} <= keys
        # 尾部公司后缀循环剥离
        assert replay.company_keys("浙江达仕科技有限公司") == {"浙江达仕科技"}
        # 斜杠复合名拆分
        assert {"世禹", "景焱"} <= replay.company_keys("世禹/景焱")
        # 括号别名为地名时丢弃（防「芯钛科（上海）」误配「上海光键」）
        keys = replay.company_keys("芯钛科（上海）")
        assert "芯钛科" in keys and "上海" not in keys

    def test_companies_match_rules(self) -> None:
        # 后缀归一
        assert replay.companies_match("浙江达仕科技", "浙江达仕科技有限公司")
        # 括号别名互通
        assert replay.companies_match("MPS（芯源系统）", "芯源系统")
        # 大小写/空白不敏感
        assert replay.companies_match("MPS", " mps ")
        # 包含匹配要求短键 ≥3：景焱（2 字）不得吞并 嘉兴景焱…
        assert not replay.companies_match("景焱", "嘉兴景焱智能装备技术有限公司")
        # 同字头不同公司不得误配
        assert not replay.companies_match("大族封测", "上海大族富创得科技股份有限公司")

    def test_graph_alias_resolution(self) -> None:
        graph_keys = replay.build_graph_keys(REAL_KB_DIR)
        assert len(graph_keys) == 589, "图谱 589 家全量加载"
        # 短名归一到图谱法定全名后视为同一家（图谱别名机制）
        assert replay.graph_canonical("浙江达仕科技", graph_keys) == "浙江达仕科技有限公司"
        assert replay.graph_canonical("嘉兴景焱", graph_keys) == "嘉兴景焱智能装备技术有限公司"
        # 图谱别名不得过度归一：名称残缺的自由文本不能误挂到图谱公司
        assert replay.companies_match("浙江达仕科技", "达仕科技 杭州分部", graph_keys) is False
        assert replay.graph_canonical("不存在的公司某某", graph_keys) == ""

    def test_generic_company_filter(self) -> None:
        assert replay.is_generic_company("其他 die bonder/wire bonder 设备商")
        assert replay.is_generic_company("拓荆/中微/北方华创等设备商运动控制岗")
        assert replay.is_generic_company("机器人/直线电机平台公司")
        assert replay.is_generic_company("精密测量设备公司")
        assert replay.is_generic_company("大型方案商/代理商技术市场")
        assert not replay.is_generic_company("浙江达仕科技有限公司")
        assert not replay.is_generic_company("ASMPT")

    def test_keyword_term_coverage(self) -> None:
        assert replay._term_covered("运动控制", ["实时运动控制算法"])
        assert replay._term_covered("POL", ["pol 电源"])
        assert replay._term_covered("DrMOS", ["DrMOS"])
        assert not replay._term_covered("键合机", ["运动控制"])

    def test_anchor_scoring_scale(self) -> None:
        reference = {key: [] for key in ("customer_of_customer", "product_tech_line", "competitive_landscape", "scenario_track")}
        reference["customer_of_customer"] = ["封测厂"]
        agent = {
            "customer_of_customer": {"present": False, "values": []},
            "product_tech_line": {"present": True, "values": ["运动控制"]},
            "competitive_landscape": {"present": True, "values": ["机器人"]},
            "scenario_track": {"present": True, "values": ["半导体"]},
        }
        reference["product_tech_line"] = ["运动控制"]
        reference["competitive_landscape"] = ["ASMPT"]
        reference["scenario_track"] = ["半导体封装设备"]
        result = replay.evaluate_anchors(agent, reference)
        scores = result["anchors"]
        assert scores["customer_of_customer"]["score"] == 0.0  # 缺失
        assert scores["product_tech_line"]["score"] == 1.0  # 与参考重合
        assert scores["competitive_landscape"]["score"] == 0.5  # 锚定偏差
        assert scores["scenario_track"]["score"] == 1.0  # 包含即重合
        assert result["score"] == 0.625


class ReplayFailurePathTest(unittest.TestCase):
    """任务 3：case 文件缺失/坏 JSON/结构异常 → 明确报错（ReplayCaseError），不静默全过。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.kb_dir = Path(self.temp.name) / "kb"
        (self.kb_dir / "cases").mkdir(parents=True)

    def _write_case(self, filename: str, content: str) -> None:
        (self.kb_dir / "cases" / filename).write_text(content, encoding="utf-8")

    def test_missing_case_file_raises(self) -> None:
        with self.assertRaises(replay.ReplayCaseError) as ctx:
            replay.run_replay("case_silan_tme", self.kb_dir)
        assert "缺失" in str(ctx.exception)

    def test_broken_json_raises(self) -> None:
        self._write_case("seed_silan_tme_v1.json", "{这不是合法JSON")
        with self.assertRaises(replay.ReplayCaseError) as ctx:
            replay.run_replay("case_silan_tme", self.kb_dir)
        assert "解析失败" in str(ctx.exception)

    def test_structurally_invalid_case_raises(self) -> None:
        self._write_case("seed_silan_tme_v1.json", json.dumps({"meta": {"version": "v9"}}, ensure_ascii=False))
        with self.assertRaises(replay.ReplayCaseError) as ctx:
            replay.run_replay("case_silan_tme", self.kb_dir)
        assert "job_archetype" in str(ctx.exception)

    def test_unknown_case_raises(self) -> None:
        with self.assertRaises(replay.ReplayCaseError):
            replay.run_replay("case_not_exists", self.kb_dir)

    def test_cli_returns_2_on_case_error(self) -> None:
        assert replay.main(["--kb-dir", str(self.kb_dir), "--case", "case_silan_tme"]) == 2


class ReplayKbDirOverrideTest(unittest.TestCase):
    """任务 3：KB 目录覆盖 —— 参数与 ASA_KNOWLEDGE_BASE_DIR 环境变量都生效。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.kb_copy = Path(self.temp.name) / "kb_copy"
        shutil.copytree(
            REAL_KB_DIR, self.kb_copy,
            ignore=shutil.ignore_patterns("*.xlsx", "*.md", "__pycache__"),
        )
        self._old_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_env

    def test_kb_dir_argument_overrides(self) -> None:
        result = replay.run_replay("case_silan_tme", self.kb_copy)
        assert str(self.kb_copy) in result["case_file"], "回放必须读取覆盖目录下的 case 文件"
        # 拷贝目录与真实库内容一致 → 指标等于基线
        for key, floor in REPLAY_BASELINE["case_silan_tme"].items():
            self.assertGreaterEqual(result["metrics"][key], floor)

    def test_env_var_overrides(self) -> None:
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_copy)
        assert replay.resolve_kb_dir() == self.kb_copy
        result = replay.run_replay("case_changyue_equipment")  # 不传目录 → 走环境变量
        assert str(self.kb_copy) in result["case_file"]
        for key, floor in REPLAY_BASELINE["case_changyue_equipment"].items():
            self.assertGreaterEqual(result["metrics"][key], floor)
        # run_replay 结束后恢复原环境
        assert os.environ.get("ASA_KNOWLEDGE_BASE_DIR") == str(self.kb_copy)


if __name__ == "__main__":
    unittest.main()
