"""P3-d（2026-08-14）：回放推荐率 —— 真实顾问确认口径优先、proxy 显式回落。

口径见 scripts/strategy_replay_eval.py 模块 docstring ⑥：
- case 可选字段 advisor_confirmed_recommendable_companies（士兰微顶层 / 长越岗位级）
  存在且归一后非空 → 真实口径（recommendation_basis="advisor_confirmed"）；
- 无字段 / 空名单 / 归一后全为泛化条目 → 显式回落 proxy（basis="proxy"），
  指标值与接入前完全一致（既有 case 快照兼容）。

覆盖：proxy 回落快照兼容、真实口径命中与标注、混合（一 case 有确认一 case 无）、
边界（空名单/全泛化/确认名单零命中）、--compare 方向语义不变。
集成测试依赖真实知识库（只读），CI（ubuntu 无本机 KB）自动 skip；
纯单测不依赖 KB，CI 必跑。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import strategy_replay_eval as replay  # noqa: E402

REAL_KB_DIR = Path("/Users/messi/Documents/ASA/knowledge_base")
HAS_REAL_KB = (REAL_KB_DIR / "cases" / "seed_silan_tme_v1.json").is_file()

# 与 tests/test_strategy_replay_s4.py 的 REPLAY_BASELINE 对齐（proxy 快照兼容断言用）
PROXY_BASELINE = {
    "case_silan_tme": 0.6522,
    "case_changyue_equipment": 0.6129,
}


def _silan_doc(advisor_field=None) -> dict:
    doc = {
        "meta": {"version": "v-test"},
        "job_archetype": {
            "client": "士兰微", "title": "TME",
            "directions": [{"name": "计算电源", "customers": ["客户甲"], "products": ["DrMOS"], "competitors": ["MPS"]}],
        },
        "target_company_pool": {
            "T1_competitor_device": {"companies": [{"name": "MPS（芯源系统）"}]},
            "T2_customer_OEM": {"companies": [{"name": "客户甲有限公司"}]},
        },
        "keyword_groups": [{"group": "g1", "terms": ["DrMOS"]}],
    }
    if advisor_field is not None:
        doc[replay.ADVISOR_CONFIRMED_FIELD] = advisor_field
    return doc


def _changyue_doc(advisor_field=None) -> dict:
    position = {
        "priority_v1.1": 1,
        "title": "自动化软件高级工程师",
        "anchor_analysis": {
            "customer_of_client": "封测厂",
            "product_tech": ["运动控制"],
            "competitors": "缺失，见 target_company_pool.T1",
            "scene": "半导体封装",
        },
        "target_company_pool": {
            "T1_same_layer": {
                "companies_international": ["ASMPT"],
                "companies_domestic_added_v1.1": [{"name": "浙江达仕科技有限公司"}],
                "companies_domestic_databacked_v1.2": [],
            },
            "T2_adjacent": {"companies": ["嘉兴景焱智能装备技术有限公司"]},
        },
        "keyword_groups": [{"group": "g1", "terms": ["运动控制"]}],
    }
    if advisor_field is not None:
        position[replay.ADVISOR_CONFIRMED_FIELD] = advisor_field
    return {"client_profile": {"client": "长越"}, "positions": [position]}


class AdvisorConfirmedExtractionTest(unittest.TestCase):
    """extract_reference：可选确认字段透传与归一（缺字段/空名单 → 空列表 → proxy 回落）。"""

    def test_silan_field_absent_gives_empty(self) -> None:
        reference = replay.extract_reference(replay.CASE_SILAN, _silan_doc())
        assert reference["advisor_confirmed"] == []

    def test_silan_field_extracted_and_normalized(self) -> None:
        # 字符串与 {"name": ...} 条目混合；泛化条目剔除；归一去重
        reference = replay.extract_reference(replay.CASE_SILAN, _silan_doc([
            "MPS（芯源系统）", {"name": "芯源系统"}, "其他 die bonder 设备商", "  ",
        ]))
        assert reference["advisor_confirmed"] == ["MPS（芯源系统）"]

    def test_changyue_field_at_position_level(self) -> None:
        reference = replay.extract_reference(replay.CASE_CHANGYUE, _changyue_doc(["ASMPT", "ASMPT"]))
        assert reference["advisor_confirmed"] == ["ASMPT"]
        # 缺字段 → 空列表
        reference = replay.extract_reference(replay.CASE_CHANGYUE, _changyue_doc())
        assert reference["advisor_confirmed"] == []

    def test_non_list_field_treated_as_absent(self) -> None:
        reference = replay.extract_reference(replay.CASE_SILAN, _silan_doc("MPS（芯源系统）"))
        assert reference["advisor_confirmed"] == []


class RecommendationDualBasisTest(unittest.TestCase):
    """evaluate_pool：真实口径优先 + proxy 显式回落的核心判定逻辑（纯单测，不依赖 KB）。"""

    def setUp(self) -> None:
        self.agent_pool = [
            {"name": "ASMPT", "source": "kb_graph", "tier": "T1"},
            {"name": "K&S（库力索法）", "source": "llm_inferred", "tier": "T1"},
            {"name": "无关公司甲", "source": "kb_graph", "tier": "T2"},
        ]
        self.reference_pool = ["ASMPT", "K&S"]

    def test_no_confirmed_data_falls_back_to_proxy(self) -> None:
        metrics = replay.evaluate_pool(self.agent_pool, self.reference_pool, [])
        assert metrics["recommendation_basis"] == "proxy"
        # 快照兼容：指标值/proxy 字段与接入前完全一致
        assert metrics["recommendation_proxy_hits"] == 1
        assert metrics["recommendation_proxy"] == round(1 / 3, 4)
        assert metrics["recommendation_rate"] == metrics["recommendation_proxy"]
        assert metrics["recommendation_hits"] == metrics["recommendation_proxy_hits"]
        assert metrics["advisor_confirmed_size"] == 0
        assert metrics["advisor_confirmed_missing"] == []

    def test_empty_confirmed_list_falls_back_to_proxy(self) -> None:
        for empty in ([], [""], None):
            metrics = replay.evaluate_pool(self.agent_pool, self.reference_pool, [], empty)
            assert metrics["recommendation_basis"] == "proxy", f"advisor_confirmed={empty!r} 应回落 proxy"
            assert metrics["recommendation_rate"] == metrics["recommendation_proxy"]

    def test_confirmed_data_uses_real_basis(self) -> None:
        # 确认名单 2 家：ASMPT 命中 Agent 池；「未覆盖公司乙」不命中 → 真实口径 = 1/3
        metrics = replay.evaluate_pool(
            self.agent_pool, self.reference_pool, [], ["ASMPT", "未覆盖公司乙"],
        )
        assert metrics["recommendation_basis"] == "advisor_confirmed"
        assert metrics["recommendation_hits"] == 1
        assert metrics["recommendation_rate"] == round(1 / 3, 4)
        assert metrics["advisor_confirmed_size"] == 2
        assert metrics["advisor_confirmed_missing"] == ["未覆盖公司乙"]
        # proxy 字段仍按原口径计算保留（留痕对比）
        assert metrics["recommendation_proxy"] == round(1 / 3, 4)

    def test_real_basis_independent_of_proxy_rules(self) -> None:
        # 真实口径只看确认名单匹配：llm_inferred 来源 / 未命中参考池的公司被确认也算推荐
        metrics = replay.evaluate_pool(
            self.agent_pool, self.reference_pool, [], ["K&S（库力索法）", "无关公司甲"],
        )
        assert metrics["recommendation_basis"] == "advisor_confirmed"
        assert metrics["recommendation_hits"] == 2
        assert metrics["recommendation_rate"] == round(2 / 3, 4)
        # proxy 口径下这两家都不算（1 家 llm_inferred、1 家未命中参考池）→ 两口径可不同
        assert metrics["recommendation_proxy"] == round(1 / 3, 4)

    def test_confirmed_zero_match_gives_zero_rate(self) -> None:
        metrics = replay.evaluate_pool(
            self.agent_pool, self.reference_pool, [], ["确认但完全没命中的公司"],
        )
        assert metrics["recommendation_basis"] == "advisor_confirmed"
        assert metrics["recommendation_rate"] == 0.0
        assert metrics["recommendation_hits"] == 0

    def test_empty_agent_pool_with_confirmed_data(self) -> None:
        metrics = replay.evaluate_pool([], ["ASMPT"], [], ["ASMPT"])
        assert metrics["recommendation_basis"] == "advisor_confirmed"
        assert metrics["recommendation_rate"] == 0.0
        assert metrics["advisor_confirmed_missing"] == ["ASMPT"]


class RecommendationCompareSemanticsTest(unittest.TestCase):
    """--compare / METRIC_DIRECTIONS 语义：指标键不变、方向不变、倒退判定不变。"""

    def test_metric_key_and_direction_unchanged(self) -> None:
        assert "recommendation_rate_proxy" in replay.METRIC_KEYS
        assert replay.METRIC_DIRECTIONS["recommendation_rate_proxy"] == "higher"

    def test_compare_direction_still_higher_better(self) -> None:
        current = {"cases": [], "overall": {"recommendation_rate_proxy": 0.5}}
        baseline = {"cases": [], "overall": {"recommendation_rate_proxy": 0.6}}
        diff = replay.compare_reports(current, baseline)
        assert diff["overall"]["recommendation_rate_proxy"]["direction"] == "higher"
        assert diff["overall"]["recommendation_rate_proxy"]["regressed"] is True
        assert "overall.recommendation_rate_proxy" in diff["regressions"]


@unittest.skipUnless(HAS_REAL_KB, "真实知识库不存在（CI ubuntu），跳过集成测试")
class RecommendationReplayIntegrationTest(unittest.TestCase):
    """集成：真实 case 无确认字段 → proxy 快照兼容；注入确认字段 → 真实口径生效。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.kb_copy = Path(cls.temp.name) / "kb_copy"
        shutil.copytree(
            REAL_KB_DIR, cls.kb_copy,
            ignore=shutil.ignore_patterns("*.xlsx", "*.md", "__pycache__"),
        )

    def _inject_advisor_field(self, companies: list[str]) -> None:
        case_path = self.kb_copy / "cases" / "seed_silan_tme_v1.json"
        doc = json.loads(case_path.read_text(encoding="utf-8"))
        doc[replay.ADVISOR_CONFIRMED_FIELD] = companies
        case_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_existing_cases_stay_on_proxy(self) -> None:
        for case_id, baseline_value in PROXY_BASELINE.items():
            result = replay.run_replay(case_id, self.kb_copy)
            assert result["recommendation_basis"] == "proxy"
            # 快照兼容：无确认字段时指标值与接入前基线一致
            assert result["metrics"]["recommendation_rate_proxy"] == baseline_value
        text = replay.render_text(replay.evaluate_all(self.kb_copy))
        assert "proxy 非真实口径" in text
        assert "真实顾问确认口径" not in text

    def test_injected_confirmed_data_switches_to_real_basis(self) -> None:
        # 注入：T1 两家真实公司（参考池全覆盖 → 必命中 Agent 池）+ 一家不存在公司
        self._inject_advisor_field(["MPS（芯源系统）", "矽力杰", "不存在的确认公司某某"])
        self.addCleanup(self._restore_silan_case)
        result = replay.run_replay(replay.CASE_SILAN, self.kb_copy)
        assert result["recommendation_basis"] == "advisor_confirmed"
        pool = result["details"]["pool"]
        assert pool["advisor_confirmed_size"] == 3
        assert pool["recommendation_hits"] == 2
        assert pool["advisor_confirmed_missing"] == ["不存在的确认公司某某"]
        assert result["metrics"]["recommendation_rate_proxy"] == round(2 / pool["agent_size"], 4)
        # proxy 留痕仍在且数值等于旧口径基线
        assert pool["recommendation_proxy"] == PROXY_BASELINE[replay.CASE_SILAN]
        text = replay.render_text({"kb_dir": str(self.kb_copy), "cases": [result],
                                   "overall": result["metrics"]})
        assert "真实顾问确认口径" in text
        assert "不存在的确认公司某某" in text

    def test_mixed_cases_basis_per_case(self) -> None:
        # 混合：士兰微注入确认字段、长越不动 → 各 case 独立判定口径
        self._inject_advisor_field(["MPS（芯源系统）"])
        self.addCleanup(self._restore_silan_case)
        report = replay.evaluate_all(self.kb_copy)
        basis = {case["case_id"]: case["recommendation_basis"] for case in report["cases"]}
        assert basis == {
            replay.CASE_SILAN: "advisor_confirmed",
            replay.CASE_CHANGYUE: "proxy",
        }

    def _restore_silan_case(self) -> None:
        shutil.copy2(
            REAL_KB_DIR / "cases" / "seed_silan_tme_v1.json",
            self.kb_copy / "cases" / "seed_silan_tme_v1.json",
        )


if __name__ == "__main__":
    unittest.main()
