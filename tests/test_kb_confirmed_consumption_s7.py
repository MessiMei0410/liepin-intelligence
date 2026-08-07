"""二期知识飞轮闭环：知识成果运行时消费接入测试。

覆盖两个"只写不读"成果的消费侧接入：
1. 公司校准覆盖层（company_calibrations 表 → load/apply_calibration_overlay）：
   - run_search_strategy：有 calibrated 记录的岗位策略公司池带 source=consultant_calibrated
     标注（step2 + kb_graph_candidates + classification_trace）；无表/空表/无 calibrated
     记录时 strategy_v2 输出与现状逐字节一致；
   - candidate_assessment graph_hits：合并覆盖层后匹配，命中信息增强（校准 track/
     categories + source=consultant_calibrated），不改评分；无覆盖层时命中无 source 键。
2. 顾问确认规则（kb_agent_confirmed_rules_v1.json，knowledge_proposals accept 写入）：
   - negative_rule 追加进五类清单汇总（source=consultant_confirmed + proposal_id）；
   - skill_alias 并入 normalize_skill 归一（source=consultant_confirmed，优先内置别名）；
   - level_mapping 在 map_level 中优先于内置库；
   - 文件缺失/坏 JSON/结构异常一律降级跳过，不炸、不改变现状输出。

全部使用临时库 + 临时 KB fixture（ASA_KNOWLEDGE_BASE_DIR 覆盖），绝不写真实知识库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from test_a_system_agent_v1 import fake_assessment
from test_candidate_assessment_s6 import (
    GOOD_LLM,
    RESUME_TEXT,
    DbCase as AssessmentDbCase,
    _seed_person,
    _stub_fetcher,
)
from test_kb_consumption_s4 import KbConsumptionDbCase

from a_system_agent import FakeLLM, candidate_assessment, knowledge_base, negative_rules, strategy_v2

# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

SKILL_ONTOLOGY_FIXTURE = {
    "meta": {"version": "test"},
    "families": [
        {
            "family_id": "packaging_equipment",
            "label": "封测设备场景",
            "skills": [
                {
                    "name": "键合机",
                    "aliases": ["键合设备", "wire bonder"],
                    "related": ["固晶机"],
                    "evidence": ["键合设备整机研发/交付"],
                }
            ],
        }
    ],
}

LEVEL_MAPPING_FIXTURE = {
    "meta": {"version": "test"},
    "level_bands": [
        {
            "band": "senior",
            "label": "高级/资深工程师",
            "aliases": ["高级工程师", "资深工程师"],
            "accepted": ["高级工程师", "资深工程师"],
            "years_hint": "5-10年",
            "basis": "fixture：内置职级带",
        }
    ],
    "systems": [],
}


def _confirmed_rules_doc(rules: list[dict]) -> dict:
    return {"meta": {"version": "v1", "created": "2026-08-05", "updated": "2026-08-05"}, "rules": rules}


def _rule(rule_type: str, content: dict, proposal_id: str = "kprop_test1", **overrides) -> dict:
    entry = {
        "rule_id": f"krule_{proposal_id}",
        "rule_type": rule_type,
        "title": f"测试提案 {proposal_id}",
        "content": content,
        "evidence": [{"source_type": "stop_reason", "summary": "测试证据"}],
        "proposed_by": "consultant_confirmed",
        "source": "knowledge_proposal",
        "proposal_id": proposal_id,
        "version": "v1",
        "created_at": "2026-08-05 10:00:00",
    }
    entry.update(overrides)
    return entry


def _write_confirmed_rules(kb_dir: Path, rules: list[dict]) -> None:
    (kb_dir / "kb_agent_confirmed_rules_v1.json").write_text(
        json.dumps(_confirmed_rules_doc(rules), ensure_ascii=False), encoding="utf-8"
    )


CALIBRATION_TABLE_DDL = """
CREATE TABLE company_calibrations(
    calibration_id TEXT PRIMARY KEY,
    company_key TEXT,
    company_name TEXT,
    status TEXT,
    track TEXT,
    product_lines_json TEXT,
    skill_tags_json TEXT,
    level_system TEXT,
    no_poach INTEGER,
    non_compete INTEGER,
    note TEXT,
    calibrated_by TEXT,
    calibrated_at TEXT,
    version INTEGER
)
"""


def _insert_calibration(
    db_path: Path,
    company_name: str,
    *,
    status: str = "calibrated",
    track: str = "后道测试设备",
    skill_tags: list[str] | None = None,
    calibration_id: str = "ccal_test1",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO company_calibrations
               (calibration_id,company_key,company_name,status,track,product_lines_json,skill_tags_json,
                level_system,no_poach,non_compete,note,calibrated_by,calibrated_at,version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                calibration_id,
                knowledge_base.normalize_client_name(company_name),
                company_name,
                status,
                track,
                '["STS8200 测试机"]',
                json.dumps(skill_tags if skill_tags is not None else ["测试机", "分选机"], ensure_ascii=False),
                "P/M 双序列",
                1,
                0,
                "顾问确认赛道与产品线",
                "consultant",
                "2026-08-01 10:00:00",
                1,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class KbDirCase(unittest.TestCase):
    """临时 KB 目录 + ASA_KNOWLEDGE_BASE_DIR 隔离（纯函数消费测试）。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        self.kb_dir = Path(self.kb_temp.name) / "kb"
        self.kb_dir.mkdir(parents=True, exist_ok=True)
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()


# ---------------------------------------------------------------------------
# 1. 顾问确认 negative_rule → 五类清单汇总
# ---------------------------------------------------------------------------

class ConfirmedNegativeRuleTest(KbDirCase):
    def test_confirmed_negative_rule_appended_with_source(self) -> None:
        _write_confirmed_rules(
            self.kb_dir,
            [
                _rule(
                    "negative_rule",
                    {
                        "scope_type": "client",
                        "scope": "长川科技",
                        "rule": "客户「长川科技」人选多次因「方向不符」停止推进，建议固化为排除规则",
                        "trigger": "stop_reason_cluster",
                    },
                    proposal_id="kprop_neg1",
                )
            ],
        )
        checklist, trace = negative_rules.build_negative_rule_checklist(
            {"title": "机械工程师"}, kb_dir=self.kb_dir
        )
        five = [entry for entry in checklist if entry["type"] in negative_rules.NEGATIVE_RULE_TYPES]
        assert len(five) == 5, "固定五类清单保持完整"
        confirmed = [entry for entry in checklist if entry["source"] == "consultant_confirmed"]
        assert len(confirmed) == 1
        entry = confirmed[0]
        assert entry["type"] == negative_rules.CONFIRMED_NEGATIVE_TYPE
        assert entry["applicable"] is True
        assert "方向不符" in entry["rule"]
        assert entry["proposal_id"] == "kprop_neg1"
        assert "kprop_neg1" in entry["basis"] and "长川科技" in entry["basis"]
        assert checklist[-1] is entry, "确认规则追加在固定五类之后"
        assert any("consultant_confirmed" in line for line in trace)

    def test_confirmed_negative_rule_skips_invalid_entries(self) -> None:
        _write_confirmed_rules(
            self.kb_dir,
            [
                _rule("negative_rule", {"scope": "甲", "rule": "有效规则"}, proposal_id="kprop_ok"),
                _rule("negative_rule", {"scope": "乙", "rule": ""}, proposal_id="kprop_empty"),
                _rule("negative_rule", {"scope": "丙", "rule": "已被取代"}, proposal_id="kprop_old", status="superseded"),
                _rule("skill_alias", {"alias": "示波器", "canonical": "测量仪器"}, proposal_id="kprop_other"),
                {"rule_type": "negative_rule", "content": "非对象"},  # content 非 dict
            ],
        )
        checklist, _trace = negative_rules.build_negative_rule_checklist({"title": "工程师"}, kb_dir=self.kb_dir)
        confirmed = [entry for entry in checklist if entry["source"] == "consultant_confirmed"]
        assert [entry["proposal_id"] for entry in confirmed] == ["kprop_ok"], (
            "空 rule / 非有效 status / 其他类型 / content 非对象的条目一律跳过"
        )

    def test_missing_or_broken_file_keeps_five_classes(self) -> None:
        # 缺文件
        checklist, _trace = negative_rules.build_negative_rule_checklist({"title": "工程师"}, kb_dir=self.kb_dir)
        assert len(checklist) == 5
        # 坏 JSON
        (self.kb_dir / "kb_agent_confirmed_rules_v1.json").write_text("{这不是合法JSON", encoding="utf-8")
        checklist, trace = negative_rules.build_negative_rule_checklist({"title": "工程师"}, kb_dir=self.kb_dir)
        assert len(checklist) == 5
        assert any("解析失败" in line for line in trace)
        # 结构异常（rules 非数组）
        (self.kb_dir / "kb_agent_confirmed_rules_v1.json").write_text(
            json.dumps({"meta": {}, "rules": {"not": "alist"}}), encoding="utf-8"
        )
        checklist, _trace = negative_rules.build_negative_rule_checklist({"title": "工程师"}, kb_dir=self.kb_dir)
        assert len(checklist) == 5


# ---------------------------------------------------------------------------
# 2. 顾问确认 skill_alias → normalize_skill 归一
# ---------------------------------------------------------------------------

class ConfirmedSkillAliasTest(KbDirCase):
    def setUp(self) -> None:
        super().setUp()
        (self.kb_dir / "kb_skill_ontology_semiconductor_v1.json").write_text(
            json.dumps(SKILL_ONTOLOGY_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )

    def test_confirmed_alias_normalizes_with_source(self) -> None:
        _write_confirmed_rules(
            self.kb_dir,
            [_rule("skill_alias", {"alias": "邦定机", "canonical": "键合机"}, proposal_id="kprop_alias1")],
        )
        hit = knowledge_base.normalize_skill("邦定机", kb_dir=self.kb_dir)
        assert hit["matched"] is True
        assert hit["canonical"] == "键合机"
        assert hit["family"] == "packaging_equipment", "canonical 在本体中时 family 照常带出"
        assert hit["source"] == "consultant_confirmed"
        # 内置别名不受影响
        builtin = knowledge_base.normalize_skill("wire bonder", kb_dir=self.kb_dir)
        assert builtin["matched"] is True and builtin["source"] == "kb_skill"
        # 确认别名优先于内置别名（同一归一键以确认规则为准）
        _write_confirmed_rules(
            self.kb_dir,
            [_rule("skill_alias", {"alias": "键合设备", "canonical": "自定义键合"}, proposal_id="kprop_alias2")],
        )
        overridden = knowledge_base.normalize_skill("键合设备", kb_dir=self.kb_dir)
        assert overridden["canonical"] == "自定义键合"
        assert overridden["source"] == "consultant_confirmed"
        assert overridden["family"] == "", "canonical 不在本体时 family 为空，不编造"

    def test_missing_or_broken_file_keeps_builtin_behavior(self) -> None:
        hit = knowledge_base.normalize_skill("wire bonder", kb_dir=self.kb_dir)
        assert hit["matched"] is True and hit["source"] == "kb_skill"
        miss = knowledge_base.normalize_skill("不存在的技能词", kb_dir=self.kb_dir)
        assert miss["matched"] is False and miss["source"] == "none"

        (self.kb_dir / "kb_agent_confirmed_rules_v1.json").write_text("{坏JSON", encoding="utf-8")
        hit = knowledge_base.normalize_skill("wire bonder", kb_dir=self.kb_dir)
        assert hit["matched"] is True and hit["source"] == "kb_skill", "坏文件不得影响内置归一"


# ---------------------------------------------------------------------------
# 3. 顾问确认 level_mapping → map_level 优先/补充内置库
# ---------------------------------------------------------------------------

class ConfirmedLevelMappingTest(KbDirCase):
    def setUp(self) -> None:
        super().setUp()
        (self.kb_dir / "kb_level_mapping_v1.json").write_text(
            json.dumps(LEVEL_MAPPING_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )

    def test_confirmed_level_mapping_hit(self) -> None:
        _write_confirmed_rules(
            self.kb_dir,
            [
                _rule(
                    "level_mapping",
                    {
                        "alias": "首席工程师",
                        "band": "principal",
                        "label": "首席/Principal",
                        "accepted": ["首席工程师", "Principal Engineer"],
                        "basis": "顾问确认：原厂首席对标 P9",
                    },
                    proposal_id="kprop_lvl1",
                )
            ],
        )
        hit, trace = knowledge_base.map_level("首席工程师（硬件方向）", kb_dir=self.kb_dir)
        assert hit is not None
        assert hit["band"] == "principal"
        assert hit["matched_alias"] == "首席工程师"
        assert hit["accepted_levels"] == ["首席工程师", "Principal Engineer"]
        assert hit["source"] == "consultant_confirmed"
        assert hit["proposal_id"] == "kprop_lvl1"
        assert any("consultant_confirmed" in line for line in trace)
        # 内置库命中不受影响
        builtin, _ = knowledge_base.map_level("资深工程师", kb_dir=self.kb_dir)
        assert builtin is not None and builtin["source"] == "kb_level" and builtin["band"] == "senior"

    def test_confirmed_level_mapping_takes_priority_over_builtin(self) -> None:
        # 确认规则别名“工程师”比内置“高级工程师”短，但确认规则优先判定。
        _write_confirmed_rules(
            self.kb_dir,
            [_rule("level_mapping", {"alias": "工程师", "band": "confirmed_band"}, proposal_id="kprop_lvl2")],
        )
        hit, _ = knowledge_base.map_level("高级工程师", kb_dir=self.kb_dir)
        assert hit is not None
        assert hit["band"] == "confirmed_band"
        assert hit["source"] == "consultant_confirmed"

    def test_missing_or_broken_file_keeps_builtin_mapping(self) -> None:
        hit, _ = knowledge_base.map_level("高级工程师", kb_dir=self.kb_dir)
        assert hit is not None and hit["source"] == "kb_level"

        (self.kb_dir / "kb_agent_confirmed_rules_v1.json").write_text("{坏JSON", encoding="utf-8")
        hit, _ = knowledge_base.map_level("高级工程师", kb_dir=self.kb_dir)
        assert hit is not None and hit["source"] == "kb_level", "坏文件不得影响内置职级映射"
        miss, _ = knowledge_base.map_level("扫地僧", kb_dir=self.kb_dir)
        assert miss is None, "确认规则与内置库都未命中时仍走 LLM/原型路径"


# ---------------------------------------------------------------------------
# 4. 校准覆盖层 → run_search_strategy 公司池
# ---------------------------------------------------------------------------

class StrategyCalibrationOverlayTest(KbConsumptionDbCase):
    """策略生成消费校准覆盖层：命中公司带 consultant_calibrated；无覆盖层逐字节一致。"""

    def _create_calibration_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(CALIBRATION_TABLE_DDL)
            conn.commit()
        finally:
            conn.close()

    def test_calibrated_company_marked_in_strategy(self) -> None:
        self._create_calibration_table()
        _insert_calibration(self.db_path, "杭州鲁滨逊测试技术有限公司", track="后道测试设备")
        result = self._run_strategy(50)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors

        calibrated = [
            company
            for entry in v2["step2_target_pool"]
            for company in entry["companies"]
            if company["source"] == "consultant_calibrated"
        ]
        assert [company["name"] for company in calibrated] == ["杭州鲁滨逊测试技术有限公司"], (
            "校准命中公司在策略输出必须带 source=consultant_calibrated"
        )
        assert v2["step2_source_distribution"].get("consultant_calibrated") == 1
        # 未校准的图谱命中保持 kb_graph
        assert any(
            company["source"] == "kb_graph"
            for entry in v2["step2_target_pool"]
            for company in entry["companies"]
        )
        # LLM 输入的图谱候选同样带校准标注
        payload_hit = next(
            company
            for company in self.captured["payload"]["kb_graph_candidates"]
            if company["name"] == "杭州鲁滨逊测试技术有限公司"
        )
        assert payload_hit["source"] == "consultant_calibrated"
        # trace 留痕
        assert any("应用顾问校准覆盖" in line for line in v2["classification_trace"])
        assert any("consultant_calibrated" in line for line in v2["classification_trace"])

    def test_no_overlay_output_byte_identical(self) -> None:
        """无表 / 空表 / 仅非 calibrated 记录：strategy_v2 输出与现状逐字节一致。"""
        baseline = self._run_strategy(50)["strategy_v2"]
        assert not any(
            company["source"] == "consultant_calibrated"
            for entry in baseline["step2_target_pool"]
            for company in entry["companies"]
        )
        assert not any("校准覆盖" in line for line in baseline["classification_trace"])

        self._create_calibration_table()
        with_empty_table = self._run_strategy(50)["strategy_v2"]
        assert json.dumps(with_empty_table, ensure_ascii=False, sort_keys=True) == json.dumps(
            baseline, ensure_ascii=False, sort_keys=True
        ), "空校准表不得改变策略输出"

        _insert_calibration(
            self.db_path, "杭州鲁滨逊测试技术有限公司", status="needs_review", calibration_id="ccal_review"
        )
        with_review_only = self._run_strategy(50)["strategy_v2"]
        assert json.dumps(with_review_only, ensure_ascii=False, sort_keys=True) == json.dumps(
            baseline, ensure_ascii=False, sort_keys=True
        ), "needs_review 记录不得进入覆盖层"


# ---------------------------------------------------------------------------
# 5. 校准覆盖层 → candidate_assessment graph_hits
# ---------------------------------------------------------------------------

class AssessmentCalibrationOverlayTest(AssessmentDbCase):
    """评估 graph_hits 合并覆盖层后匹配：只增强命中信息，不改评分。"""

    def _create_calibration_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(CALIBRATION_TABLE_DDL)
            conn.commit()
        finally:
            conn.close()

    def _run_with_capture(self) -> dict:
        captured: dict = {}

        def _trajectory(payload: dict) -> dict:
            captured["payload"] = payload
            return GOOD_LLM

        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        conn = self.connect()
        try:
            candidate_assessment.run_assessment(
                conn,
                candidate_id=1,
                job_id=154,
                llm=FakeLLM({}, trajectory=_trajectory),
                kb_dir=str(self.kb_dir),
                signal_fetcher=_stub_fetcher,
            )
        finally:
            conn.close()
        return captured["payload"]

    def test_graph_hits_merge_calibration_overlay(self) -> None:
        self._create_calibration_table()
        _insert_calibration(
            self.db_path,
            "杰华特微电子股份有限公司",
            track="校准后赛道",
            skill_tags=["电源管理", "多相控制器"],
        )
        payload = self._run_with_capture()
        hit = next(item for item in payload["graph_hits"] if item["graph_name"] == "杰华特微电子股份有限公司")
        assert hit["source"] == "consultant_calibrated"
        assert hit["track"] == "校准后赛道", "校准 track 覆盖原始图谱值"
        assert hit["categories"] == ["电源管理", "多相控制器"], "校准 skill_tags 替换四分类标签"
        # 未校准的图谱命中不带 source 键（保持现状）
        other = next(item for item in payload["graph_hits"] if item["graph_name"] == "晶丰明源半导体（上海）股份有限公司")
        assert "source" not in other

    def test_graph_hits_without_overlay_unchanged(self) -> None:
        payload = self._run_with_capture()
        assert payload["graph_hits"], "图谱命中本身必须存在"
        assert all("source" not in hit for hit in payload["graph_hits"]), (
            "无校准覆盖层时命中信息与现状一致（无 source 键）"
        )
        hit = next(item for item in payload["graph_hits"] if item["graph_name"] == "杰华特微电子股份有限公司")
        assert hit["track"] == "模拟芯片"


if __name__ == "__main__":
    unittest.main()
