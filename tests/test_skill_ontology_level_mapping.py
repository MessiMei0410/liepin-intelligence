"""知识飞轮二期：技能本体 + 职级映射知识库消费测试。

覆盖：
- kb_skill_ontology_semiconductor_v1.json / kb_level_mapping_v1.json 加载（真实库结构校验 +
  缺失/坏 JSON 优雅降级）；
- normalize_skill 别名归一（中英文同义词、未命中原样）、related_skills 相关词提示；
- map_level 职级带命中（最长别名优先）/未命中降级；
- 策略生成消费（确定性模式，沿用 test_kb_consumption_s4 的临时 KB + FakeLLM 玩法）：
  step4 关键词别名归一 + 相关词提示（source=kb_skill）、step3 职级映射优先 kb_level；
- 简历评估消费：build_llm_payload 注入 skill_terms_normalized（source=kb_skill），保守接入。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_a_system_agent_v1 import AgentDbCase, fake_assessment
from a_system_agent import AgentService, FakeLLM
from a_system_agent import candidate_assessment, knowledge_base, strategy_v2


KB_SKILL_ONTOLOGY_FIXTURE = {
    "meta": {"version": "test", "usage": "知识飞轮二期测试 fixture"},
    "families": [
        {
            "family_id": "packaging_equipment",
            "label": "封测设备场景",
            "skills": [
                {
                    "name": "键合机",
                    "aliases": ["键合设备", "die bonder", "wire bonder"],
                    "related": ["固晶机", "贴片机"],
                    "evidence": ["键合设备整机研发/交付"],
                },
                {"name": "固晶机", "aliases": ["固晶", "die attach"], "related": ["键合机"], "evidence": ["固晶设备研发"]},
            ],
        },
        {
            "family_id": "power_supply",
            "label": "计算电源与多相供电",
            "skills": [
                {
                    "name": "多相控制器",
                    "aliases": ["多相Buck", "multiphase"],
                    "related": ["DrMOS", "VRM"],
                    "evidence": ["多相/DrMOS 产品线立项或量产经历"],
                }
            ],
        },
    ],
}

KB_LEVEL_MAPPING_FIXTURE = {
    "meta": {"version": "test", "usage": "知识飞轮二期测试 fixture"},
    "level_bands": [
        {
            "band": "senior",
            "label": "高级/资深工程师",
            "aliases": ["高级工程师", "资深工程师"],
            "accepted": ["高级工程师", "资深工程师", "技术经理"],
            "years_hint": "5-10年",
            "basis": "fixture：按整机复杂度定档",
        },
        {
            "band": "director",
            "label": "总监",
            "aliases": ["总监"],
            "accepted": ["经理", "总监", "副总"],
            "years_hint": "10年+",
            "basis": "fixture：总监档",
        },
    ],
    "systems": [
        {
            "system_id": "internet_p",
            "label": "互联网大厂 P 序列",
            "levels": [{"level": "P6", "band": "senior", "title": "高级/资深工程师", "basis": "fixture 对标"}],
        }
    ],
}

KB_PROFILES_FIXTURE = {"meta": {"version": "test"}, "profiles": []}
KB_GRAPH_FIXTURE = {"meta": {"version": "test"}, "companies": {}}


def _write_kb(
    base: Path,
    *,
    ontology: dict | None = KB_SKILL_ONTOLOGY_FIXTURE,
    level_mapping: dict | None = KB_LEVEL_MAPPING_FIXTURE,
    broken_ontology: bool = False,
    broken_level_mapping: bool = False,
) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "kb_client_profiles_v1.json").write_text(
        json.dumps(KB_PROFILES_FIXTURE, ensure_ascii=False), encoding="utf-8"
    )
    (base / "kb_company_graph_jsj_v1.json").write_text(
        json.dumps(KB_GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8"
    )
    if ontology is not None:
        target = base / knowledge_base.SKILL_ONTOLOGY_FILE
        target.write_text(
            "{这不是合法JSON" if broken_ontology else json.dumps(ontology, ensure_ascii=False), encoding="utf-8"
        )
    if level_mapping is not None:
        target = base / knowledge_base.LEVEL_MAPPING_FILE
        target.write_text(
            "{这不是合法JSON" if broken_level_mapping else json.dumps(level_mapping, ensure_ascii=False),
            encoding="utf-8",
        )
    return base


class SkillOntologyLoadTest(unittest.TestCase):
    """本体加载：临时库结构解析、缺失/坏 JSON 降级；真实库结构校验。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_load_fixture_ontology(self) -> None:
        ontology, trace = knowledge_base.load_skill_ontology(self.kb_dir)
        assert len(ontology["skills"]) == 3
        assert ontology["families"] == {"packaging_equipment": "封测设备场景", "power_supply": "计算电源与多相供电"}
        assert ontology["skills"]["键合机"]["aliases"] == ["键合设备", "die bonder", "wire bonder"]
        assert any("已加载技能本体 3 技能 / 2 族" in line for line in trace)

    def test_missing_or_broken_ontology_degrades(self) -> None:
        missing_dir = Path(tempfile.mkdtemp()) / "no_such_dir"
        ontology, trace = knowledge_base.load_skill_ontology(missing_dir)
        assert ontology == {"skills": {}, "aliases": {}, "families": {}}
        assert any("降级为空本体" in line for line in trace)

        broken_base = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(broken_base, level_mapping=None, broken_ontology=True)
        ontology, trace = knowledge_base.load_skill_ontology(broken_base)
        assert ontology["skills"] == {}
        assert any("解析失败" in line and "降级为空本体" in line for line in trace)

    def test_real_ontology_structure(self) -> None:
        """真实 kb_skill_ontology_semiconductor_v1.json：五族齐备，每条技能带别名/相关/证据。"""
        ontology, trace = knowledge_base.load_skill_ontology()
        assert ontology["skills"], trace
        assert {"power_supply", "motion_control", "packaging_equipment", "precision_mechanical", "technical_marketing"} <= set(
            ontology["families"]
        )
        for name, skill in ontology["skills"].items():
            assert skill["aliases"], f"{name} 缺别名"
            assert skill["related"], f"{name} 缺相关技能"
            assert skill["evidence"], f"{name} 缺证据形式"
        # 业务关键技能词必须在本体内（从 seed/cases 归纳的核心词）
        for expected in ("多相控制器", "DrMOS", "VRM", "TLVR", "VPD", "磁件", "运动控制", "EtherCAT", "键合机", "有限元"):
            assert expected in ontology["skills"], f"{expected} 应在本体内"


class SkillNormalizeTest(unittest.TestCase):
    """别名归一：中英文同义词命中、大小写/空白变体、未命中原样、相关词提示。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")
        self.ontology, _ = knowledge_base.load_skill_ontology(self.kb_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_alias_normalization(self) -> None:
        info = knowledge_base.normalize_skill("die bonder", self.ontology)
        assert info["matched"] is True
        assert info["canonical"] == "键合机"
        assert info["family"] == "packaging_equipment"
        assert info["source"] == "kb_skill"
        # 大小写与空白变体同样命中
        assert knowledge_base.normalize_skill("Die  Bonder", self.ontology)["canonical"] == "键合机"
        assert knowledge_base.normalize_skill("多相 buck", self.ontology)["canonical"] == "多相控制器"
        # 本名自身命中
        assert knowledge_base.normalize_skill("键合机", self.ontology)["canonical"] == "键合机"

    def test_unmatched_term_passes_through(self) -> None:
        info = knowledge_base.normalize_skill("组织架构搭建", self.ontology)
        assert info["matched"] is False
        assert info["source"] == "none"
        assert info["canonical"] == ""
        assert knowledge_base.normalize_skill("", self.ontology)["matched"] is False

    def test_normalize_with_empty_ontology_degrades(self) -> None:
        empty = {"skills": {}, "aliases": {}, "families": {}}
        info = knowledge_base.normalize_skill("die bonder", empty)
        assert info["matched"] is False and info["source"] == "none"

    def test_related_skills(self) -> None:
        assert knowledge_base.related_skills("die bonder", self.ontology) == ["固晶机", "贴片机"]
        assert knowledge_base.related_skills("组织架构搭建", self.ontology) == []


class LevelMappingTest(unittest.TestCase):
    """职级映射：最长别名命中、未命中/空库降级、真实库结构校验。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")
        self.mapping, _ = knowledge_base.load_level_mapping(self.kb_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_load_fixture_mapping(self) -> None:
        mapping, trace = knowledge_base.load_level_mapping(self.kb_dir)
        assert len(mapping["bands"]) == 2
        assert len(mapping["systems"]) == 1
        assert mapping["systems"][0]["levels"][0]["basis"], "对照体系必须带对标依据"
        assert any("已加载职级映射 2 职级带 / 1 体系" in line for line in trace)

    def test_map_level_hit(self) -> None:
        hit, trace = knowledge_base.map_level("精密设备机械高级工程师", self.mapping)
        assert hit is not None
        assert hit["band"] == "senior"
        assert hit["matched_alias"] == "高级工程师"
        assert hit["accepted_levels"] == ["高级工程师", "资深工程师", "技术经理"]
        assert hit["basis"]
        assert hit["source"] == "kb_level"
        assert any("source=kb_level" in line and "对标依据" in line for line in trace)

    def test_map_level_miss_and_empty_degrade(self) -> None:
        hit, trace = knowledge_base.map_level("前台文员", self.mapping)
        assert hit is None
        assert any("未命中" in line and "LLM/原型路径" in line for line in trace)
        hit, trace = knowledge_base.map_level("机械高级工程师", {"bands": [], "systems": []})
        assert hit is None
        assert any("职级映射为空" in line for line in trace)

    def test_missing_or_broken_level_mapping_degrades(self) -> None:
        missing_dir = Path(tempfile.mkdtemp()) / "no_such_dir"
        mapping, trace = knowledge_base.load_level_mapping(missing_dir)
        assert mapping == {"bands": [], "systems": []}
        assert any("降级为空映射" in line for line in trace)

        broken_base = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(broken_base, ontology=None, broken_level_mapping=True)
        mapping, trace = knowledge_base.load_level_mapping(broken_base)
        assert mapping["bands"] == []
        assert any("解析失败" in line and "降级为空映射" in line for line in trace)

    def test_real_level_mapping_structure(self) -> None:
        """真实 kb_level_mapping_v1.json：职级带 + 体系对照齐备，全部带对标依据。"""
        mapping, trace = knowledge_base.load_level_mapping()
        assert len(mapping["bands"]) >= 5, trace
        assert len(mapping["systems"]) >= 3
        for band in mapping["bands"]:
            assert band["aliases"] and band["accepted"] and band["basis"], f"{band['band']} 缺别名/接受职级/对标依据"
        for system in mapping["systems"]:
            for level in system.get("levels") or []:
                assert str(level.get("basis") or "").strip(), f"{system.get('system_id')} 存在无对标依据的级别"
        # seed/cases 中真实出现的职级词必须可命中
        for title in ("机械高级工程师", "技术市场总监", "软件架构师", "机械主管"):
            hit, _ = knowledge_base.map_level(title, mapping)
            assert hit is not None, f"{title} 应命中职级带"


class StrategyKbConsumptionDbCase(AgentDbCase):
    """公共 fixture：临时 KB（本体+职级映射）+ 临时库客户/岗位。"""

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (5,'某设备厂')")
        conn.execute(
            "INSERT INTO jobs VALUES (50,5,'键合设备机械高级工程师','杭州','已发布','','','','','键合设备机械设计','2026-08-05')"
        )
        conn.execute(
            "INSERT INTO jobs VALUES (60,5,'运营专员','杭州','已发布','','','','','运营支持','2026-08-05')"
        )
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))
        self.captured: dict = {}

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def _run_strategy(self, job_id: int) -> dict:
        def _strategy(payload: dict) -> dict:
            self.captured["payload"] = payload
            return {
                "strategy_summary": "模型策略",
                "channels": {
                    "liepin": [{"round": "core", "query": "键合机 机械设计", "purpose": "核心", "evidence": "岗位要求"}],
                    "xsaas": [],
                },
                "strategy_v2": {
                    "step3_level_mapping": {"accepted_levels": ["工程师"], "calibration_rule": "LLM 推断口径"},
                    "step4_keyword_groups": [
                        {"group": "equipment_scene", "targets": "键合设备", "terms": ["die bonder", "键合机", "机械设计"]}
                    ],
                },
            }

        self.service.llm = FakeLLM(fake_assessment(), search_strategy=_strategy)
        return self.service.capability_runtime.run_search_strategy(
            {"type": "job", "id": job_id}, {"objective": "补充候选人"}
        )


class StrategySkillOntologyTest(StrategyKbConsumptionDbCase):
    """策略生成消费本体：step4 别名归一 + 相关词提示（source=kb_skill）。"""

    def test_step4_alias_normalization_and_hints(self) -> None:
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        group = next(item for item in v2["step4_keyword_groups"] if item["group"] == "equipment_scene")
        # die bonder 与 键合机 归一到同一 canonical，只保留首个原词（去重归一，不破坏召回口径）
        assert group["terms"] == ["die bonder", "机械设计"]
        ontology_block = group["skill_ontology"]
        assert ontology_block["source"] == "kb_skill"
        pairs = {item["raw"]: item["canonical"] for item in ontology_block["normalized"]}
        assert pairs == {"die bonder": "键合机", "键合机": "键合机"}
        assert "固晶机" in ontology_block["related_terms_hint"]
        assert any("技能别名归一" in line and "kb_skill" in line for line in v2["classification_trace"])
        assert any("相关技能提示" in line and "kb_skill" in line for line in v2["classification_trace"])

    def test_missing_ontology_keeps_current_behavior(self) -> None:
        (self.kb_dir / knowledge_base.SKILL_ONTOLOGY_FILE).unlink()
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        group = next(item for item in v2["step4_keyword_groups"] if item["group"] == "equipment_scene")
        assert group["terms"] == ["die bonder", "键合机", "机械设计"], "无本体时维持现状（不归一不去重）"
        assert "skill_ontology" not in group
        assert any("降级为空本体" in line for line in v2["classification_trace"])


class StrategyLevelMappingTest(StrategyKbConsumptionDbCase):
    """策略生成消费职级映射：step3 优先 kb_level，未命中走 LLM/原型路径。"""

    def test_step3_kb_level_takes_precedence_over_llm(self) -> None:
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        step3 = v2["step3_level_mapping"]
        assert step3["level_source"] == "kb_level"
        assert step3["kb_level_band"] == "senior"
        assert step3["accepted_levels"] == ["高级工程师", "资深工程师", "技术经理"]
        assert step3["kb_level_basis"]
        # kb_level 命中优先于 LLM fragment 的 ["工程师"]
        assert step3["accepted_levels"] != ["工程师"]
        assert v2["evaluation_constraints"]["levels"] == step3["accepted_levels"]
        assert any("source=kb_level" in line for line in v2["classification_trace"])
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors

    def test_step3_falls_back_to_llm_when_unmapped(self) -> None:
        result = self._run_strategy(60)  # 运营专员：职级映射未命中
        v2 = result["strategy_v2"]
        step3 = v2["step3_level_mapping"]
        assert "level_source" not in step3
        assert step3["accepted_levels"] == ["工程师"], "未命中 kb_level 时走现有 LLM 路径"
        assert any("未命中" in line and "LLM/原型路径" in line for line in v2["classification_trace"])

    def test_missing_level_mapping_degrades(self) -> None:
        (self.kb_dir / knowledge_base.LEVEL_MAPPING_FILE).unlink()
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        assert v2["step3_level_mapping"]["accepted_levels"] == ["工程师"]
        assert any("降级为空映射" in line for line in v2["classification_trace"])
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors


class AssessmentSkillNormalizationTest(unittest.TestCase):
    """简历评估消费：build_llm_payload 注入归一技能词（source=kb_skill），保守接入。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")
        self.ontology, _ = knowledge_base.load_skill_ontology(self.kb_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _strategy_doc(self) -> dict:
        return {
            "step1_job_essence": {"statement": "键合设备整机研发"},
            "step3_level_mapping": {"accepted_levels": ["高级工程师"]},
            "step4_keyword_groups": [
                {"group": "equipment_scene", "targets": "键合设备", "terms": ["die bonder", "键合机", "组织架构搭建"]}
            ],
        }

    def test_payload_injects_normalized_skill_terms(self) -> None:
        payload = candidate_assessment.build_llm_payload(
            candidate={"current_company": "某设备厂", "current_title": "机械工程师"},
            job={"client": "某客户", "title": "键合设备机械高级工程师"},
            strategy_doc=self._strategy_doc(),
            graph_hits=[],
            skill_ontology=self.ontology,
        )
        normalized = payload["strategy_v2"]["skill_terms_normalized"]
        assert normalized, "本体命中时必须注入归一技能词"
        assert all(item["source"] == "kb_skill" for item in normalized)
        # 同一 canonical 只出现一次（die bonder 先出现，键合机 归并）；未命中词不进入该块
        assert len(normalized) == 1
        assert normalized[0]["raw"] == "die bonder"
        assert normalized[0]["canonical"] == "键合机"
        assert normalized[0]["family"] == "packaging_equipment"
        # 评分相关字段不被改动（保守接入）
        assert payload["strategy_v2"]["accepted_levels"] == ["高级工程师"]

    def test_payload_without_ontology_unchanged(self) -> None:
        payload = candidate_assessment.build_llm_payload(
            candidate={"current_company": "某设备厂", "current_title": "机械工程师"},
            job={"client": "某客户", "title": "键合设备机械高级工程师"},
            strategy_doc=self._strategy_doc(),
            graph_hits=[],
        )
        assert "skill_terms_normalized" not in payload["strategy_v2"]
        empty = {"skills": {}, "aliases": {}, "families": {}}
        payload = candidate_assessment.build_llm_payload(
            candidate={"current_company": "某设备厂", "current_title": "机械工程师"},
            job={"client": "某客户", "title": "键合设备机械高级工程师"},
            strategy_doc=self._strategy_doc(),
            graph_hits=[],
            skill_ontology=empty,
        )
        assert "skill_terms_normalized" not in payload["strategy_v2"]


if __name__ == "__main__":
    unittest.main()
