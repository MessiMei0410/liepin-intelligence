"""S4-3c-4（N6）：策略全要素消费检查契约测试。

口径：docs/ASA_寻访链路完整优化方案_2026-07-23.md N6 节、docs/ASA_KIMI_TASK_S4-3c_S4-5_2026-07-23.md S4-3c-4 节。
种子的 T1/T2/T3 各层公司池、地点策略、排除规则、有效关键词组必须全部进入 strategy_v2，
缺项显式列入 unused；种子未命中原型 coverage_report=None 留痕不算缺失；
restricted 层要素永远不进 unused 对外面。
知识库种子一律用临时目录 fixture / 真实种子的临时副本，绝不触碰生产库与真实知识库。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_a_system_agent_v1 import AgentDbCase, fake_assessment
from a_system_agent import AgentService, FakeLLM
from a_system_agent import strategy_v2


KB_SEED_FIXTURE = {
    "meta": {"version": "test", "usage": "S4-3c-4 N6 测试 fixture"},
    "job_archetype": {
        "archetype_id": "tme_computing_power",
        "title": "技术市场经理/总监（TME，计算电源管理方向）",
        "client": "士兰微",
        "essence": "面向整机/车企客户的技术型市场岗",
        "directions": [
            {"name": "PC", "customers": ["联想", "荣耀"], "products": ["多相控制器", "DrMOS"], "competitors": ["MPS", "矽力杰"]},
        ],
        "target_functions": ["TME", "FAE"],
        "location_policy": "杭州优先",
    },
    "target_company_pool": {
        "T1_competitor_device": {
            "rationale": "同赛道功率半导体原厂",
            "companies": [{"name": "MPS（芯源系统）"}, {"name": "矽力杰"}],
        },
        "T2_customer_OEM": {"rationale": "客户整机厂电源工程师", "companies": [{"name": "联想"}, {"name": "浪潮"}]},
        "T3_adjacent_unconfirmed": {"rationale": "相邻产品线原厂", "companies": [{"name": "南芯科技"}]},
    },
    "keyword_groups": [
        {"group": "competitor_tme", "targets": "T1 友商技术市场/应用序列", "terms": ["MPS", "矽力杰", "TME", "DrMOS"]},
        {"group": "product_tech", "targets": "跨公司产品技术词兜底", "terms": ["多相控制器", "DrMOS", "POL"]},
    ],
    "negative_rules": ["方向词不用“PC电源”字面（客户语义是多相/DrMOS/POL）"],
    "level_mapping": {"accepted_candidate_levels": ["主管", "经理", "总监"], "note": "按独立负责产品线定档而非 title"},
}

# 士兰微真实种子（只读来源；测试用其临时副本，绝不直接读写知识库）
SILAN_SEED_SOURCE = Path("/Users/messi/Documents/ASA/knowledge_base/seed_silan_tme_v1.json")


def _write_kb(directory: Path, doc: dict | None = KB_SEED_FIXTURE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "seed_silan_tme_v1.json"
    target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


def _classification(level: str = "L3") -> dict:
    return {
        "input_level": level,
        "anchors": {key: {"present": False, "values": [], "source": "missing", "inferred": False, "confidence": ""} for key in strategy_v2.ANCHOR_KEYS},
        "missing_anchors": list(strategy_v2.ANCHOR_KEYS),
        "trace": ["测试留痕"],
        "archetype_id": "tme_computing_power",
    }


def _fixture_archetype() -> dict:
    archetypes, _ = strategy_v2.load_job_archetypes(_KB_DIR_CACHE)
    return archetypes[0]


_KB_DIR_CACHE: Path


class CoverageReportUnitTest(unittest.TestCase):
    """build_coverage_report 纯函数口径：全消费 rate=1.0、缺层进 unused、无原型 None、restricted 不外泄。"""

    @classmethod
    def setUpClass(cls) -> None:
        global _KB_DIR_CACHE
        cls._tmp = tempfile.TemporaryDirectory()
        _KB_DIR_CACHE = _write_kb(Path(cls._tmp.name) / "kb").parent

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_full_consumption_rate_is_1(self) -> None:
        archetype = _fixture_archetype()
        # 地点策略经 LLM fragment 落进 step5（模拟消费地点策略的策略对象），其余要素走 kb 兜底全消费
        fragment = {"step5_expectation": {"fallback_plan": "若 T1 召回不足，按杭州优先扩 T2/T3 池"}}
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, _classification(),
            archetype=archetype, llm_fragment=fragment,
        )
        report = strategy_v2.build_coverage_report(archetype, v2)
        assert report is not None
        assert report["coverage_rate"] == 1.0, report
        assert report["unused"] == []
        assert report["element_count"] == report["consumed_count"] == 7  # T1/T2/T3 + 地点 + 1 排除规则 + 2 关键词组
        for label in ("T1 竞对原厂", "T2 客户整机厂", "T3 相邻池（未确认）", "杭州优先", "关键词组 competitor_tme", "关键词组 product_tech"):
            assert label in report["consumed"], label
        assert any(item.startswith("排除规则：") for item in report["consumed"])

    def test_dropped_t2_layer_is_listed_unused(self) -> None:
        archetype = _fixture_archetype()
        # 策略对象只保留 T1 层（模拟 LLM 自造目标池，T2/T3 层未消费）
        fragment = {
            "step2_target_pool": [
                {
                    "path": "same_layer", "tier": "T1",
                    "companies": [
                        {"name": "MPS", "source": "llm_inferred", "confidence": "medium"},
                        {"name": "矽力杰", "source": "llm_inferred", "confidence": "medium"},
                    ],
                    "rationale": "只看同赛道原厂",
                }
            ]
        }
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, _classification(),
            archetype=archetype, llm_fragment=fragment,
        )
        report = strategy_v2.build_coverage_report(archetype, v2)
        assert report is not None
        unused = {item["element"]: item["reason"] for item in report["unused"]}
        assert "T2 客户整机厂" in unused, unused
        assert "联想" in unused["T2 客户整机厂"] and "浪潮" in unused["T2 客户整机厂"]
        assert "T3 相邻池（未确认）" in unused
        assert "杭州优先" in unused, "地点策略未进 schema 必须如实记为未使用"
        assert "T1 竞对原厂" in report["consumed"], "公司名双向包含匹配（MPS ≡ MPS（芯源系统））"
        assert 0 < report["coverage_rate"] < 1.0

    def test_partial_t2_consumption_reports_missing_companies(self) -> None:
        archetype = _fixture_archetype()
        fragment = {
            "step2_target_pool": [
                {"path": "same_layer", "tier": "T1", "companies": [{"name": "MPS（芯源系统）", "source": "kb_profile", "confidence": "high"}, {"name": "矽力杰", "source": "kb_profile", "confidence": "high"}], "rationale": "T1"},
                {"path": "reverse", "tier": "T2", "companies": [{"name": "联想", "source": "kb_profile", "confidence": "high"}], "rationale": "只取一家整机厂"},
                {"path": "adjacent", "tier": "T3", "companies": [{"name": "南芯科技", "source": "kb_profile", "confidence": "medium"}], "rationale": "T3"},
            ],
            "step5_expectation": {"fallback_plan": "杭州优先放宽"},
        }
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, _classification(),
            archetype=archetype, llm_fragment=fragment,
        )
        report = strategy_v2.build_coverage_report(archetype, v2)
        assert report is not None
        unused = {item["element"]: item["reason"] for item in report["unused"]}
        assert list(unused) == ["T2 客户整机厂"], unused
        assert "仅部分消费" in unused["T2 客户整机厂"] and "浪潮" in unused["T2 客户整机厂"]

    def test_no_archetype_returns_none(self) -> None:
        v2 = strategy_v2.build_strategy_v2({"target_companies": ["ABC公司"], "channels": {}}, _classification())
        assert strategy_v2.build_coverage_report(None, v2) is None
        assert strategy_v2.build_coverage_report({}, v2) is None
        assert strategy_v2.build_coverage_report({"title": "无 id 原型"}, v2) is None

    def test_restricted_elements_never_enter_unused(self) -> None:
        archetype = _fixture_archetype()
        # restricted 白名单约束并入 negative_rules（禁挖名单/竞业），但要素清单只来自种子
        restricted_rules = [{"type": "禁挖名单", "rule": "禁挖：某某竞品公司全体在职员工"}, {"type": "竞业限制", "rule": "某友商签竞业 12 个月"}]
        fragment = {
            "step2_target_pool": [
                {"path": "same_layer", "tier": "T1", "companies": [{"name": "MPS", "source": "llm_inferred", "confidence": "medium"}, {"name": "矽力杰", "source": "llm_inferred", "confidence": "medium"}], "rationale": "T1"}
            ],
            "step5_expectation": {"fallback_plan": "杭州优先放宽"},
        }
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, _classification(),
            archetype=archetype, llm_fragment=fragment, restricted_rules=restricted_rules,
        )
        assert any(rule.get("source") == "restricted_client" for rule in v2["negative_rules"]), "restricted 约束本身仍并入策略"
        report = strategy_v2.build_coverage_report(archetype, v2)
        assert report is not None
        report_text = json.dumps(report, ensure_ascii=False)
        assert "某某竞品公司" not in report_text
        assert "竞业 12 个月" not in report_text
        assert "禁挖" not in report_text, "restricted 要素不得出现在 coverage_report 对外面"
        assert "restricted_client" not in report_text


class CoverageReportRuntimeTest(AgentDbCase):
    """run_search_strategy 集成：报告写入策略对象与 artifact metadata；无原型显式 None。"""

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(Path(self.kb_temp.name) / "kb")
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (3,'士兰微')")
        conn.execute(
            "INSERT INTO jobs VALUES (30,3,'技术市场经理（PC电源）','杭州','已发布','','','','','PC方向','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (4,'某某公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (40,4,'销售总监','上海','已发布','','','','','快消行业销售总监','2026-07-23')"
        )
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def test_report_written_into_strategy_and_artifact_metadata(self) -> None:
        result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 30}, {"objective": "补充候选人"})
        v2 = result["strategy_v2"]
        report = v2["coverage_report"]
        assert report is not None
        assert report["archetype_id"] == "tme_computing_power"
        assert report["element_count"] == 7
        # kb 兜底消费 T1/T2/T3/排除规则/关键词组；地点策略未进 schema → 未使用
        assert "T2 客户整机厂" in report["consumed"]
        unused_elements = [item["element"] for item in report["unused"]]
        assert unused_elements == ["杭州优先"], report
        assert report["coverage_rate"] == round(6 / 7, 4)
        assert any("N6 要素消费检查" in line for line in v2["classification_trace"])
        artifact = result["artifacts"][0]
        assert artifact["metadata"]["coverage_report"] == report
        assert artifact["metadata"]["strategy_v2"]["coverage_report"] == report
        assert "coverage_report" in artifact["content"]

    def test_no_archetype_job_gets_explicit_null_report(self) -> None:
        result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 40}, {"objective": "补充候选人"})
        v2 = result["strategy_v2"]
        assert "coverage_report" in v2, "无原型岗位也必须显式留痕 coverage_report=None"
        assert v2["coverage_report"] is None
        assert result["artifacts"][0]["metadata"]["coverage_report"] is None


@unittest.skipUnless(SILAN_SEED_SOURCE.is_file(), "士兰微真实种子不存在时跳过（验收锚点重放）")
class SilanSeedReplayTest(AgentDbCase):
    """验收锚点：士兰微真实种子（临时副本）重放 run_search_strategy。

    T2 客户整机厂与“杭州优先”必须出现在 strategy_v2 或 unused 清单。
    """

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = Path(self.kb_temp.name) / "kb"
        kb_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SILAN_SEED_SOURCE, kb_dir / SILAN_SEED_SOURCE.name)
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (3,'士兰微')")
        conn.execute(
            "INSERT INTO jobs VALUES (30,3,'技术市场经理（三次电源/服务器或PC市场）','杭州','已发布','','','','','计算电源管理','2026-07-23')"
        )
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def test_silan_seed_replay_coverage(self) -> None:
        result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 30}, {"objective": "补充5位合适人选"})
        v2 = result["strategy_v2"]
        assert v2["archetype_id"] == "tme_computing_power"
        report = v2["coverage_report"]
        assert report is not None, "命中士兰微原型必须产出 coverage_report"
        strategy_text = json.dumps(v2, ensure_ascii=False)
        consumed_text = "、".join(report["consumed"])
        unused_elements = [item["element"] for item in report["unused"]]
        unused_text = "、".join(unused_elements)
        # 验收锚点：T2 客户整机厂出现在策略对象或清单；T2 公司（联想等）真实进 step2
        assert "T2 客户整机厂" in consumed_text + unused_text or "T2" in strategy_text
        assert any(entry["tier"] == "T2" and entry["path"] == "reverse" for entry in v2["step2_target_pool"])
        t2_names = [company["name"] for entry in v2["step2_target_pool"] if entry["tier"] == "T2" for company in entry["companies"]]
        assert "联想" in t2_names and "蔚来" in t2_names, t2_names
        # 验收锚点：“杭州优先”出现在策略对象或 unused 清单
        assert "杭州优先" in strategy_text or "杭州优先" in unused_text
        # 本期实测：地点策略未进 schema → 必须在 unused 显式列出（N6 产出，非生成缺陷修复）
        assert "杭州优先" in unused_elements, report
        # T1/T2/T3 与排除规则/关键词组走 kb 兜底全消费，唯一缺口是地点策略
        assert report["coverage_rate"] > 0.5, report
        assert "T1 竞对原厂" in report["consumed"] and "T2 客户整机厂" in report["consumed"] and "T3 相邻池（未确认）" in report["consumed"]


if __name__ == "__main__":
    unittest.main()
