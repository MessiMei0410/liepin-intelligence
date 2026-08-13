"""S4-4：回放评测进回归 —— 两定稿 case 七指标「不许倒退」门槛（CI 形态）。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §6；
指标口径与公司名归一规则见 scripts/strategy_replay_eval.py 模块 docstring；
基线数值与未命中明细见 docs/ASA_strategy_replay_baseline_s4-4_2026-07-23.md。
指标方向见 scripts/strategy_replay_eval.py METRIC_DIRECTIONS（noise_rate 越低越好，其余越高越好）。

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

# 基线：2026-07-23 实跑生成（确定性模式 deterministic_fallback；口径见脚本 docstring §指标口径）。
# case_silan_tme：L1，参考池 15 家（T1 4 + T2 11），Agent 池 23 家（kb_profile 15 + kb_graph 8）；
# case_changyue_equipment：L3 JD + 2026-08-12 顾问确认的长川系禁挖约束，参考池 20 家、
# Agent 池 31 家；原型补齐客户/友商锚点，禁挖约束作为策略负向规则保留，不进入渠道执行面。
# 二期扩展（2026-08-05）：新增 evidence_coverage / noise_rate / recommendation_rate_proxy 三指标。
# 2026-08-12 基线按实际回放更新；方向按 replay.METRIC_DIRECTIONS：noise_rate 越低越好（≤ 基线），
# 其余越高越好（≥ 基线）；recommendation_rate_proxy 为 proxy 口径，非顾问确认真实推荐率。
REPLAY_BASELINE = {
    "case_silan_tme": {
        "pool_recall": 1.0,
        "pool_precision": 0.6522,
        "keyword_coverage": 1.0,
        "anchor_completeness": 1.0,
        "evidence_coverage": 1.0,
        "noise_rate": 0.3478,
        "recommendation_rate_proxy": 0.6522,
    },
    "case_changyue_equipment": {
        "pool_recall": 0.95,
        "pool_precision": 0.6129,
        "keyword_coverage": 1.0,
        "anchor_completeness": 0.875,
        "evidence_coverage": 1.0,
        "noise_rate": 0.3871,
        "recommendation_rate_proxy": 0.6129,
    },
}


class ReplayBaselineTest(unittest.TestCase):
    """任务 1/3：两 case 各跑回放，指标按方向比对基线（不许倒退）；定级与结构口径锁定。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 真实 KB 缺失时 run_replay 抛 ReplayCaseError，测试直接炸出声（不静默全过）。
        cls.results = {case_id: replay.run_replay(case_id, REAL_KB_DIR) for case_id in REPLAY_BASELINE}

    def test_metrics_not_below_baseline(self) -> None:
        for case_id, baseline in REPLAY_BASELINE.items():
            metrics = self.results[case_id]["metrics"]
            for key, floor in baseline.items():
                direction = replay.METRIC_DIRECTIONS.get(key, "higher")
                if direction == "lower":  # noise_rate：越低越好，超过基线即倒退
                    self.assertLessEqual(
                        metrics[key], floor,
                        f"{case_id}.{key}={metrics[key]} 高于基线 {floor}（越低越好）——策略生成逻辑疑似倒退",
                    )
                else:
                    self.assertGreaterEqual(
                        metrics[key], floor,
                        f"{case_id}.{key}={metrics[key]} 低于基线 {floor}（2026-07-23/2026-08-05 口径）——策略生成逻辑疑似倒退",
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
        # 岗位原型补齐了客户/友商锚点；case 的 restricted 约束仅进入负向规则。
        assert result["missing_anchors"] == []
        pool = result["details"]["pool"]
        assert pool["reference_size"] == 20
        assert pool["agent_size"] == 31
        # 锚点明细：客户/友商/产品线均来自受控知识来源，场景仍有 0.5 偏差。
        anchors = result["details"]["anchors"]["anchors"]
        assert anchors["product_tech_line"]["score"] == 1.0
        assert anchors["customer_of_customer"]["score"] == 1.0
        assert anchors["competitive_landscape"]["score"] == 1.0
        assert anchors["scenario_track"]["score"] == 0.5


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
        # 拷贝目录与真实库内容一致 → 指标等于基线（按指标方向比对）
        for key, floor in REPLAY_BASELINE["case_silan_tme"].items():
            if replay.METRIC_DIRECTIONS.get(key, "higher") == "lower":
                self.assertLessEqual(result["metrics"][key], floor)
            else:
                self.assertGreaterEqual(result["metrics"][key], floor)

    def test_env_var_overrides(self) -> None:
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_copy)
        assert replay.resolve_kb_dir() == self.kb_copy
        result = replay.run_replay("case_changyue_equipment")  # 不传目录 → 走环境变量
        assert str(self.kb_copy) in result["case_file"]
        for key, floor in REPLAY_BASELINE["case_changyue_equipment"].items():
            if replay.METRIC_DIRECTIONS.get(key, "higher") == "lower":
                self.assertLessEqual(result["metrics"][key], floor)
            else:
                self.assertGreaterEqual(result["metrics"][key], floor)
        # run_replay 结束后恢复原环境
        assert os.environ.get("ASA_KNOWLEDGE_BASE_DIR") == str(self.kb_copy)


class ReplayExtendedMetricsTest(unittest.TestCase):
    """二期（2026-08-05）：证据覆盖率/噪音率/推荐率 proxy 的口径单测。"""

    def test_noise_rate_is_one_minus_precision(self) -> None:
        result = replay.run_replay("case_silan_tme", REAL_KB_DIR)
        metrics = result["metrics"]
        assert metrics["noise_rate"] == round(1.0 - metrics["pool_precision"], 4)
        # precision 语义不变（与基线一致），噪音率只是显式输出
        assert metrics["pool_precision"] == REPLAY_BASELINE["case_silan_tme"]["pool_precision"]

    def test_evidence_coverage_source_attribution(self) -> None:
        v2 = {
            "anchors": {
                "customer_of_customer": {"present": True, "source": "client_doc"},
                "product_tech_line": {"present": True, "source": "jd"},
                "competitive_landscape": {"present": False, "source": "missing"},  # missing 不计分母
                "scenario_track": {"present": True, "source": "kb_archetype"},
            },
            "negative_rules": [
                {"rule": "排除非半导体背景", "source": "kb_profile"},
                {"rule": "禁挖名单", "source": "restricted_client"},
                {"rule": "LLM 猜测的约束", "source": "llm_inferred"},  # 无依据
                {"rule": "", "source": "none"},  # 空 rule 留痕条目不计入
            ],
        }
        pool = [
            {"name": "A", "source": "kb_profile"},
            {"name": "B", "source": "kb_graph"},
            {"name": "C", "source": "llm_inferred"},  # 无依据
        ]
        result = replay.evaluate_evidence(pool, v2)
        # 要素：池 3（2 有依据）+ present 锚点 3（3 有依据）+ 约束 3（2 有依据）= 9，有依据 7
        assert result["total"] == 9
        assert result["backed"] == 7
        assert result["coverage"] == round(7 / 9, 4)
        assert result["breakdown"]["target_pool"] == {"backed": 2, "total": 3}
        assert result["breakdown"]["anchors_present"] == {"backed": 3, "total": 3}
        assert result["breakdown"]["constraints"] == {"backed": 2, "total": 3}

    def test_recommendation_proxy_counts_backed_matched_only(self) -> None:
        # proxy = 命中参考池且 source 非 llm_inferred 的 Agent 公司占比（非真实推荐率）
        agent_pool = [
            {"name": "ASMPT", "source": "kb_graph", "tier": "T1"},      # 命中 + 有依据 → 计入
            {"name": "K&S（库力索法）", "source": "llm_inferred", "tier": "T1"},  # 命中但 llm_inferred → 不计
            {"name": "无关公司甲", "source": "kb_graph", "tier": "T2"},   # 未命中 → 不计
        ]
        metrics = replay.evaluate_pool(agent_pool, ["ASMPT", "K&S"], [])
        assert metrics["recommendation_proxy_hits"] == 1
        assert metrics["recommendation_proxy"] == round(1 / 3, 4)

    def test_new_metrics_present_in_report(self) -> None:
        for case_id in REPLAY_BASELINE:
            metrics = replay.run_replay(case_id, REAL_KB_DIR)["metrics"]
            for key in ("evidence_coverage", "noise_rate", "recommendation_rate_proxy"):
                assert key in metrics, f"{case_id} 缺新指标 {key}"
                assert 0.0 <= metrics[key] <= 1.0


class ReplayCompareModeTest(unittest.TestCase):
    """二期（2026-08-05）：--compare 基线 diff 模式 —— 逐项 diff、方向判定、退出码。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = replay.evaluate_all(REAL_KB_DIR, ["case_silan_tme"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.baseline_path = Path(self.temp.name) / "baseline.json"

    def _write_baseline(self, mutate=None) -> None:
        baseline = json.loads(json.dumps(self.report))  # 深拷贝当前报告作基线
        if mutate:
            mutate(baseline)
        self.baseline_path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")

    def test_compare_self_no_regression(self) -> None:
        self._write_baseline()
        diff = replay.compare_reports(self.report, json.loads(self.baseline_path.read_text(encoding="utf-8")))
        assert diff["regressions"] == []
        entry = diff["cases"]["case_silan_tme"]["metrics"]["pool_recall"]
        assert entry["delta"] == 0.0 and entry["regressed"] is False
        assert replay.main(["--kb-dir", str(REAL_KB_DIR), "--case", "case_silan_tme",
                            "--compare", str(self.baseline_path)]) == 0

    def test_compare_detects_regression_both_directions(self) -> None:
        def mutate(baseline: dict) -> None:
            metrics = baseline["cases"][0]["metrics"]
            metrics["pool_recall"] = 1.0  # silan 当前已是 1.0 → 不动
            metrics["pool_precision"] = 1.0  # 抬高 → 当前 0.6522 判倒退（越高越好）
            metrics["noise_rate"] = 0.0  # 压低 → 当前 0.3478 判倒退（越低越好）
        self._write_baseline(mutate)
        diff = replay.compare_reports(self.report, json.loads(self.baseline_path.read_text(encoding="utf-8")))
        regressed = set(diff["regressions"])
        assert "case_silan_tme.pool_precision" in regressed
        assert "case_silan_tme.noise_rate" in regressed
        assert "case_silan_tme.pool_recall" not in regressed
        metrics_diff = diff["cases"]["case_silan_tme"]["metrics"]
        assert metrics_diff["noise_rate"]["direction"] == "lower"
        assert replay.main(["--kb-dir", str(REAL_KB_DIR), "--case", "case_silan_tme",
                            "--compare", str(self.baseline_path)]) == 1

    def test_compare_marks_new_metrics_and_missing_current(self) -> None:
        def mutate(baseline: dict) -> None:
            # 基线删掉新指标 → 对比时应标 "new"；基线加一个当前没有的指标 → 按倒退处理
            metrics = baseline["cases"][0]["metrics"]
            for key in ("evidence_coverage", "noise_rate", "recommendation_rate_proxy"):
                metrics.pop(key)
            metrics["future_metric"] = 0.5
        self._write_baseline(mutate)
        diff = replay.compare_reports(self.report, json.loads(self.baseline_path.read_text(encoding="utf-8")))
        metrics_diff = diff["cases"]["case_silan_tme"]["metrics"]
        assert metrics_diff["evidence_coverage"]["note"] == "new"
        assert metrics_diff["future_metric"]["regressed"] is True
        assert "case_silan_tme.future_metric" in diff["regressions"]

    def test_compare_bad_baseline_file_returns_2(self) -> None:
        self.baseline_path.write_text("{坏JSON", encoding="utf-8")
        assert replay.main(["--kb-dir", str(REAL_KB_DIR), "--case", "case_silan_tme",
                            "--compare", str(self.baseline_path)]) == 2


if __name__ == "__main__":
    unittest.main()
