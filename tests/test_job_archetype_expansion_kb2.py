"""知识飞轮二期：岗位原型库扩充 + 评估直接消费原型测试。

覆盖：
1. 真实知识库全部 seed_*.json 可加载且结构合法（schema 校验：meta/job_archetype/
   keyword_groups/negative_rules/level_mapping/target_company_pool）；
2. 原型与技能本体/职级映射的引用一致性：skills_ontology_nodes 必须是
   kb_skill_ontology_semiconductor_v1 的 canonical 词，level_mapping.level_band 必须是
   kb_level_mapping_v1 的职级带；
3. 新原型可解释匹配（真实业务 P0 岗位标题 → 对应原型）+ 既有原型命中不回归；
4. 评估直接消费原型：build_llm_payload 命中注入 archetype_reference（source=kb_archetype，
   硬门槛 + 本体典型证据形式），无命中完全不注入；run_assessment 端到端留痕；
5. 无可用原型显式说明：build_strategy_v2 archetype_matched=false + archetype_note。

真实知识库只读；端到端评估用临时 KB/临时 DB fixture，绝不触碰生产库与外网 LLM。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import candidate_assessment, knowledge_base, strategy_v2  # noqa: E402
from a_system_agent.llm import FakeLLM  # noqa: E402
from test_candidate_assessment_s6 import (  # noqa: E402
    GOOD_LLM,
    GRAPH_FIXTURE,
    _create_db,
    _seed_person,
    _stub_fetcher,
)

# 本次扩充新增的 8 个原型（既有 3 个：tme_computing_power / changyue_bonding_motion_control /
# changyue_precision_equipment_mechanical，合计 11 个）
NEW_ARCHETYPE_IDS = {
    "power_rd_expert_computing",
    "fab_td_process_expert",
    "fab_equipment_expert",
    "fab_yield_expert",
    "fab_quality_reliability",
    "changyue_bonding_electrical",
    "changyue_failure_analysis",
    "fpga_embedded_hardware",
}

# 真实业务 P0 岗位标题 → 期望命中的原型（来源：outputs 缺口清单 / case_pengxinxu / case_changyue）
MATCH_CASES = (
    ("士兰微", "电源专家", "power_rd_expert_computing"),
    ("某客户", "ACDC电源硬件工程师", "power_rd_expert_computing"),
    ("鹏新旭", "PVD工艺专家", "fab_td_process_expert"),
    ("鹏新旭", "TD PIE技术专家", "fab_td_process_expert"),
    ("鹏新旭", "PVD设备专家", "fab_equipment_expert"),
    ("鹏新旭", "量测设备专家", "fab_equipment_expert"),
    ("鹏新旭", "YE技术专家", "fab_yield_expert"),
    ("鹏新旭", "PQE主任工程师", "fab_quality_reliability"),
    ("苏科思", "FPGA技术主管", "fpga_embedded_hardware"),
    ("长越科技", "电气高级工程师", "changyue_bonding_electrical"),
    ("长越科技", "失效分析高级工程师", "changyue_failure_analysis"),
)

# 端到端评估 fixture：tme 原型（含硬门槛与本体引用），写入临时 KB
E2E_SEED_FIXTURE = {
    "meta": {"version": "test", "source": "知识飞轮二期评估消费测试 fixture"},
    "job_archetype": {
        "archetype_id": "tme_computing_power",
        "title": "技术市场经理/总监（TME，计算电源管理方向）",
        "client": "士兰微",
        "essence": "面向整机/车企客户的技术型市场岗",
        "directions": [],
        "target_functions": ["TME", "FAE"],
        "location_policy": "杭州优先",
    },
    "target_company_pool": {
        "T1_competitor_device": {
            "rationale": "同赛道功率半导体原厂",
            "companies": [{"name": "MPS（芯源系统）"}, {"name": "矽力杰"}],
        },
    },
    "keyword_groups": [
        {"group": "product_tech", "targets": "跨公司产品技术词兜底", "terms": ["多相控制器", "DrMOS", "POL"]},
    ],
    "negative_rules": ["方向词不用“PC电源”字面（客户语义是多相/DrMOS/POL）"],
    "skills_ontology_nodes": ["多相控制器", "TME"],
    "level_mapping": {"accepted_candidate_levels": ["主管", "经理", "总监"], "note": "按独立负责产品线定档而非 title"},
}

E2E_ONTOLOGY_FIXTURE = {
    "meta": {"version": "test"},
    "families": [
        {
            "family_id": "power_supply",
            "label": "计算电源与多相供电",
            "skills": [
                {
                    "name": "多相控制器",
                    "aliases": ["多相Buck"],
                    "related": ["DrMOS"],
                    "evidence": ["多相/DrMOS 产品线立项或量产经历", "参考设计"],
                },
            ],
        },
        {
            "family_id": "technical_marketing",
            "label": "技术市场与应用",
            "skills": [
                {"name": "TME", "aliases": ["技术市场"], "related": ["FAE"], "evidence": ["产品线立项", "技术宣讲与培训"]},
            ],
        },
    ],
}


class RealKbSeedSchemaTest(unittest.TestCase):
    """要求 4.1：全部 seed_*.json 可加载且结构合法（真实知识库，只读）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kb_dir = strategy_v2.knowledge_base_dir()

    def test_seed_files_load_without_parse_failure(self) -> None:
        paths = sorted(self.kb_dir.glob("seed_*.json"))
        assert len(paths) >= 8, f"岗位原型不足 8 个：{[p.name for p in paths]}"
        archetypes, trace = strategy_v2.load_job_archetypes(self.kb_dir)
        assert not any("解析失败" in line for line in trace), trace
        assert len(archetypes) == len(paths), "每个 seed_*.json 必须贡献一个可加载原型"
        ids = {item["archetype_id"] for item in archetypes}
        assert len(ids) == len(archetypes), "archetype_id 不得重复"
        assert NEW_ARCHETYPE_IDS <= ids, f"新原型缺失：{NEW_ARCHETYPE_IDS - ids}"

    def test_every_seed_file_schema(self) -> None:
        for path in sorted(self.kb_dir.glob("seed_*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            meta = doc.get("meta") or {}
            assert str(meta.get("version") or "").strip(), f"{path.name} 缺 meta.version"
            assert str(meta.get("source") or "").strip(), f"{path.name} 缺 meta.source（来源依据）"
            archetype = doc.get("job_archetype") or {}
            for key in ("archetype_id", "title", "essence"):
                assert str(archetype.get(key) or "").strip(), f"{path.name} job_archetype 缺 {key}"
            groups = doc.get("keyword_groups") or []
            assert groups, f"{path.name} keyword_groups 为空"
            for group in groups:
                assert group.get("group") and group.get("terms"), f"{path.name} 存在缺 group/terms 的关键词组"
            rules = doc.get("negative_rules") or []
            assert rules and all(isinstance(rule, str) and rule.strip() for rule in rules), (
                f"{path.name} negative_rules 必须是非空字符串数组"
            )
            level_mapping = doc.get("level_mapping") or {}
            assert level_mapping.get("accepted_candidate_levels"), f"{path.name} level_mapping 缺 accepted_candidate_levels"
            pool = doc.get("target_company_pool") or {}
            assert pool, f"{path.name} target_company_pool 为空"
            for key, block in pool.items():
                companies = block.get("companies") or []
                assert companies and all(str(company.get("name") or "").strip() for company in companies), (
                    f"{path.name} 公司池 {key} 必须含带 name 的公司"
                )


class ReferenceConsistencyTest(unittest.TestCase):
    """要求 4.2：原型与技能本体/职级映射的引用一致性（canonical 词存在性校验）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kb_dir = strategy_v2.knowledge_base_dir()
        cls.ontology, _ = knowledge_base.load_skill_ontology(cls.kb_dir)
        cls.mapping, _ = knowledge_base.load_level_mapping(cls.kb_dir)

    def test_skills_ontology_nodes_are_canonical(self) -> None:
        canonical_names = set(self.ontology["skills"])
        assert canonical_names, "技能本体为空，无法做一致性校验"
        for path in sorted(self.kb_dir.glob("seed_*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            for node in doc.get("skills_ontology_nodes") or []:
                assert node in canonical_names, (
                    f"{path.name} skills_ontology_nodes 引用了非 canonical 词：{node}"
                    "（应改用 kb_skill_ontology 的技能本名，或记入 skills_ontology_note 待扩族）"
                )

    def test_level_band_references_exist(self) -> None:
        bands = {band["band"] for band in self.mapping["bands"]}
        assert bands, "职级映射库为空，无法做一致性校验"
        for path in sorted(self.kb_dir.glob("seed_*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            band = (doc.get("level_mapping") or {}).get("level_band")
            if band:
                assert band in bands, f"{path.name} level_mapping.level_band 非法：{band}"

    def test_new_archetypes_carry_explicit_level_band(self) -> None:
        """新原型必须显式引用职级带（三库互相引用、口径一致）。"""
        seen: dict[str, str] = {}
        for path in sorted(self.kb_dir.glob("seed_*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            archetype_id = (doc.get("job_archetype") or {}).get("archetype_id")
            if archetype_id in NEW_ARCHETYPE_IDS:
                seen[archetype_id] = str((doc.get("level_mapping") or {}).get("level_band") or "")
        assert set(seen) == NEW_ARCHETYPE_IDS
        bands = {band["band"] for band in self.mapping["bands"]}
        for archetype_id, band in seen.items():
            assert band in bands, f"{archetype_id} 缺有效 level_band（当前：{band or '缺失'}）"


class ArchetypeMatchRulesTest(unittest.TestCase):
    """要求 1 验收：真实业务 P0 岗位标题命中对应原型；既有原型命中不回归。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.archetypes, cls.trace = strategy_v2.load_job_archetypes()

    def test_new_p0_titles_match_expected_archetypes(self) -> None:
        for client, title, expected in MATCH_CASES:
            hit, trace = strategy_v2.match_job_archetype(client, title, self.archetypes)
            assert hit is not None, f"{client}/{title} 未命中任何原型：{trace}"
            assert hit["archetype_id"] == expected, f"{client}/{title} 命中 {hit['archetype_id']}，期望 {expected}"
            assert hit["matched_by"] == "title_token"

    def test_existing_archetype_matches_do_not_regress(self) -> None:
        # 士兰微技术市场岗（标题含“电源”字样）仍必须命中 TME 原型，不被电源研发原型抢占
        hit, _ = strategy_v2.match_job_archetype("士兰微", "技术市场经理（三次电源/服务器或PC市场）", self.archetypes)
        assert hit is not None and hit["archetype_id"] == "tme_computing_power"
        software, _ = strategy_v2.match_job_archetype("长越科技", "自动化软件高级工程师", self.archetypes)
        assert software is not None and software["archetype_id"] == "changyue_bonding_motion_control"
        mechanical, _ = strategy_v2.match_job_archetype("长越科技", "机械高级工程师", self.archetypes)
        assert mechanical is not None and mechanical["archetype_id"] == "changyue_precision_equipment_mechanical"

    def test_unrelated_title_still_misses(self) -> None:
        miss, trace = strategy_v2.match_job_archetype("微导纳米", "半导体CIP紧急采购", self.archetypes)
        assert miss is None
        assert any("未命中" in line for line in trace)


class AssessmentArchetypeConsumptionTest(unittest.TestCase):
    """要求 4.3：评估消费注入 —— build_llm_payload 有/无命中两种。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.kb_dir = strategy_v2.knowledge_base_dir()
        cls.ontology, _ = knowledge_base.load_skill_ontology(cls.kb_dir)
        cls.archetypes, _ = strategy_v2.load_job_archetypes(cls.kb_dir)

    def _payload(self, archetype):
        return candidate_assessment.build_llm_payload(
            candidate={"current_company": "杰华特", "current_title": "电源工程师", "full_text": "x" * 60},
            job={"client": "士兰微", "title": "电源专家", "summary": "", "hard_requirements": ""},
            strategy_doc=None,
            graph_hits=[],
            skill_ontology=self.ontology,
            archetype=archetype,
        )

    def test_hit_injects_archetype_reference(self) -> None:
        archetype, _ = strategy_v2.match_job_archetype("士兰微", "电源专家", self.archetypes)
        assert archetype is not None
        payload = self._payload(archetype)
        reference = payload.get("archetype_reference")
        assert reference is not None, "命中原型必须注入 archetype_reference"
        assert reference["source"] == "kb_archetype"
        assert reference["archetype_id"] == "power_rd_expert_computing"
        assert reference["matched_by"] == "title_token"
        assert reference["hard_gates"], "硬门槛（原型 negative_rules）必须注入"
        assert reference["accepted_levels"], "接受职级必须注入"
        assert reference["typical_evidence_forms"], "本体典型证据形式必须注入"
        assert "不改变评分门槛" in reference["usage_note"]

    def test_miss_injects_nothing(self) -> None:
        payload = self._payload(None)
        assert "archetype_reference" not in payload, "无命中时完全不注入，保持现状"
        # 空 dict / 无 id 原型同样不注入
        assert "archetype_reference" not in self._payload({})
        assert "archetype_reference" not in self._payload({"title": "无 id 原型"})


class _AssessmentKbDbCase(unittest.TestCase):
    """临时 KB（图谱 + tme 种子 + 技能本体 fixture）+ 临时 DB；ASA_KNOWLEDGE_BASE_DIR 隔离。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = Path(self.kb_temp.name) / "kb"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "kb_company_graph_jsj_v1.json").write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
        (kb_dir / "seed_silan_tme_v1.json").write_text(json.dumps(E2E_SEED_FIXTURE, ensure_ascii=False), encoding="utf-8")
        (kb_dir / "kb_skill_ontology_semiconductor_v1.json").write_text(
            json.dumps(E2E_ONTOLOGY_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        self.kb_dir = kb_dir
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        _create_db(self.db_path)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        self.db_temp.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class AssessmentArchetypeEndToEndTest(_AssessmentKbDbCase):
    """要求 4.3 端到端：run_assessment 命中注入 payload + signal_stats 留痕；无命中保持现状。"""

    def _run(self, *, candidate_id: int, job_id: int, person_id: int):
        _seed_person(self.db_path, candidate_id=candidate_id, job_id=job_id, person_id=person_id)
        captured: dict[str, Any] = {}

        def trajectory(payload: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            return json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))

        llm = FakeLLM({}, trajectory=trajectory)
        conn = self._connect()
        try:
            doc = candidate_assessment.run_assessment(
                conn, candidate_id=candidate_id, job_id=job_id, llm=llm,
                kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher,
            )
        finally:
            conn.close()
        return doc, captured.get("payload") or {}

    def test_hit_injects_reference_and_trace(self) -> None:
        # 岗位 154：士兰微「技术市场经理/总监（PC电源）」→ 命中 tme 原型
        doc, payload = self._run(candidate_id=1, job_id=154, person_id=1)
        reference = payload.get("archetype_reference")
        assert reference is not None, "命中原型的评估 payload 必须含 archetype_reference"
        assert reference["source"] == "kb_archetype"
        assert reference["archetype_id"] == "tme_computing_power"
        assert reference["hard_gates"] == ["方向词不用“PC电源”字面（客户语义是多相/DrMOS/POL）"]
        assert "多相/DrMOS 产品线立项或量产经历" in reference["typical_evidence_forms"]
        stats = (doc.get("signal_stats") or {}).get("archetype_reference")
        assert stats is not None, "doc signal_stats 必须留痕 archetype_reference"
        assert stats["source"] == "kb_archetype" and stats["matched"] is True
        assert stats["archetype_id"] == "tme_computing_power"

    def test_miss_keeps_current_behavior(self) -> None:
        # 岗位 137：长越「机械高级工程师」—— 临时 KB 只有 tme 种子，无命中
        doc, payload = self._run(candidate_id=2, job_id=137, person_id=2)
        assert "archetype_reference" not in payload, "无命中时 payload 完全不注入"
        assert "archetype_reference" not in (doc.get("signal_stats") or {}), "无命中时 signal_stats 不留痕"


class NoArchetypeNoteTest(unittest.TestCase):
    """要求 4.4：无可用原型显式说明（archetype_matched=false + 原因）。"""

    def _classification(self, archetype_id: str = "") -> dict:
        return {
            "input_level": "L3",
            "anchors": {},
            "missing_anchors": list(strategy_v2.ANCHOR_KEYS),
            "trace": ["岗位“某客户/采购专员”未命中任何岗位原型"],
            "archetype_id": archetype_id,
        }

    def test_no_archetype_marks_matched_false_with_reason(self) -> None:
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, self._classification(), archetype=None
        )
        assert v2["archetype_matched"] is False
        note = str(v2.get("archetype_note") or "")
        assert "无可用岗位原型" in note and "classification_trace" in note
        assert any("archetype_matched=false" in line for line in v2["classification_trace"])
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors

    def test_matched_archetype_marks_true_without_note(self) -> None:
        archetype = {
            "archetype_id": "a1",
            "title": "测试原型",
            "essence": "测试",
            "matched_by": "title_token",
            "level_mapping": {},
            "keyword_groups": [],
            "negative_rules": [],
            "target_company_pool": {},
        }
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": [], "channels": {}}, self._classification("a1"), archetype=archetype
        )
        assert v2["archetype_matched"] is True
        assert "archetype_note" not in v2
        assert any("source=kb_archetype" in line for line in v2["classification_trace"])
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors


if __name__ == "__main__":
    unittest.main()
