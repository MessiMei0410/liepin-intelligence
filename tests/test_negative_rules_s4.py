"""S4-3：排除规则引擎 —— 五类检查清单逐类留痕测试。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §4；
typology 事实源 knowledge_base/kb_seed_jiachi_equipment_v1.json 的 negative_rule_typology。
全部使用临时库 + 临时 KB fixture（运行时只读，绝不触碰真实知识库与生产 DB）。
restricted 边界 P0：禁挖名单只进策略约束，不进 LLM 输入；费率/手机号等永远不出库。
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
from a_system_agent import knowledge_base, negative_rules, strategy_v2


KB_SEED_FIXTURE = {
    "meta": {"version": "test", "usage": "S4-3 五类清单 fixture"},
    "negative_rule_typology": {
        "说明": "从职位工作簿归纳的五类负向规则",
        "types": [
            {"type": "竞业协议排除", "count_in_workbook": 15, "example": "有长鑫竞业协议建议不推", "note": "下限"},
            {"type": "在职保护名单", "count_in_workbook": 50, "example": "长电/通富在职不要推", "note": "下限"},
            {"type": "身份/背景限制", "count_in_workbook": 27, "example": "经理岗暂不看台湾人", "note": "下限"},
            {"type": "稳定性筛选", "count_in_workbook": 15, "example": "五年三跳不行", "note": "下限"},
            {"type": "学历门槛", "count_in_workbook": 35, "example": "统招本科、最好一本以上", "note": "下限"},
        ],
    },
}

KB_CASE_FIXTURE = {
    "meta": {"case_id": "case_pengxinxu_fab"},
    "client_profile": {"name": "深圳市鹏新旭技术有限公司"},
    "restricted": {
        "banned_companies": ["青岛芯恩", "福建晋华", "楚芯"],
        "banned_rule": "指目前在职；已离职可正常推荐",
        "non_compete_companies": ["某竞业对手甲"],
        "consultant_phone": "13912345678",
        "fee_rate": "费率23%",
        "scripts_redline": "话术红线MARKER_REDLINE_X7",
        "offer_amounts": ["offer金额 年薪120万"],
    },
}

FORBIDDEN_LITERALS = ["13912345678", "费率23%", "MARKER_REDLINE_X7", "120万", "话术红线"]


def _write_kb(base: Path, *, seed: dict | None = KB_SEED_FIXTURE, case: dict | None = KB_CASE_FIXTURE) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    if seed is not None:
        (base / "kb_seed_jiachi_equipment_v1.json").write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    if case is not None:
        (base / "cases").mkdir(parents=True, exist_ok=True)
        (base / "cases" / "case_pengxinxu_fab_v1.json").write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return base


def _job(**overrides) -> dict:
    job = {
        "client": "某某客户", "title": "设备工程师", "summary": "", "hard_requirements": "",
        "requirements": "", "responsibilities": "", "exclusions": "", "education": "",
        "experience": "", "ability_keywords": "", "profile": {},
    }
    job.update(overrides)
    return job


def _checklist_types(checklist: list[dict]) -> dict[str, dict]:
    return {entry["type"]: entry for entry in checklist}


class TypologyLoadTest(unittest.TestCase):
    """typology 以 KB kb_seed_*.json 为事实源；缺失/坏 JSON 按 PRD §4 默认五类降级留痕。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_load_five_types_from_kb_seed(self) -> None:
        kb_dir = _write_kb(Path(self.temp.name) / "kb")
        types, trace = negative_rules.load_negative_rule_typology(kb_dir)
        names = [entry["type"] for entry in types]
        assert names == list(negative_rules.NEGATIVE_RULE_TYPES), "五类顺序固定（PRD §4）"
        assert set(names) == {"在职保护名单", "学历门槛", "身份/背景限制", "竞业协议排除", "稳定性筛选"}
        assert all(entry["source_file"] == "kb_seed_jiachi_equipment_v1.json" for entry in types)
        assert any("已加载负向规则 typology" in line for line in trace)

    def test_missing_dir_degrades_to_default_five(self) -> None:
        missing = Path(self.temp.name) / "no_such_dir"
        types, trace = negative_rules.load_negative_rule_typology(missing)
        assert [entry["type"] for entry in types] == list(negative_rules.NEGATIVE_RULE_TYPES)
        assert any("降级" in line for line in trace)

    def test_broken_seed_degrades_to_default_five(self) -> None:
        kb_dir = Path(self.temp.name) / "kb"
        kb_dir.mkdir()
        (kb_dir / "kb_seed_jiachi_equipment_v1.json").write_text("{这不是合法JSON", encoding="utf-8")
        types, trace = negative_rules.load_negative_rule_typology(kb_dir)
        assert [entry["type"] for entry in types] == list(negative_rules.NEGATIVE_RULE_TYPES)
        assert any("解析失败" in line for line in trace)


class ChecklistTraceTest(unittest.TestCase):
    """五类逐类留痕：适用/不适用 + 依据；无依据标 applicable=false + 理由。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_all_five_classes_traced_even_when_inapplicable(self) -> None:
        checklist, trace = negative_rules.build_negative_rule_checklist(_job(), kb_dir=self.kb_dir)
        assert [entry["type"] for entry in checklist] == list(negative_rules.NEGATIVE_RULE_TYPES)
        for entry in checklist:
            assert entry["applicable"] is False
            assert entry["rule"] == ""
            assert entry["basis"], "不适用的类必须给出理由"
            assert entry["source"] == "none"
        assert any("无该客户禁挖名单" in entry["basis"] for entry in checklist)
        assert any("学历门槛" in line and "不适用" in line for line in trace), "逐类留痕进 trace"

    def test_education_threshold_from_job_hard_requirements(self) -> None:
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(hard_requirements="统招本科及以上，8 年设备经验"), kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["学历门槛"]
        assert entry["applicable"] is True
        assert entry["source"] == "jd"
        assert "统招本科" in entry["rule"]
        assert "岗位硬性要求" in entry["basis"]

    def test_education_threshold_from_position_profile(self) -> None:
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(profile={"education_requirement": "硕士及以上"}), kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["学历门槛"]
        assert entry["applicable"] is True and entry["source"] == "jd"
        assert "硕士" in entry["rule"]

    def test_identity_restriction_from_exclusions(self) -> None:
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(exclusions="经理岗暂不看台湾人"), kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["身份/背景限制"]
        assert entry["applicable"] is True and entry["source"] == "jd"
        assert "台湾人" in entry["rule"]

    def test_stability_filter_from_kb_archetype_and_jd(self) -> None:
        archetype = {"archetype_id": "a1", "negative_rules": ["五年三跳不行", "只要大厂背景"]}
        checklist, _ = negative_rules.build_negative_rule_checklist(_job(), archetype=archetype, kb_dir=self.kb_dir)
        entry = _checklist_types(checklist)["稳定性筛选"]
        assert entry["applicable"] is True and entry["source"] == "kb_profile"
        assert "五年三跳" in entry["rule"]

        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(requirements="频繁跳槽者慎推"), kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["稳定性筛选"]
        assert entry["applicable"] is True and entry["source"] == "jd"

    def test_non_compete_from_restricted_and_jd(self) -> None:
        restricted_info = {
            "client": "深圳市鹏新旭技术有限公司", "matched_by": "exact",
            "constraints": {"non_compete_companies": ["某竞业对手甲"]},
            "skipped_keys": [], "source_file": "case_pengxinxu_fab_v1.json",
        }
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(), restricted_info=restricted_info, kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["竞业协议排除"]
        assert entry["applicable"] is True and entry["source"] == "restricted_client"
        assert "某竞业对手甲" in entry["rule"]
        assert "自动继承" in entry["basis"]

        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(exclusions="有长鑫竞业协议建议不推"), kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["竞业协议排除"]
        assert entry["applicable"] is True and entry["source"] == "jd"

    def test_banned_list_from_restricted_inherits_and_consultant_fallback(self) -> None:
        restricted_info = {
            "client": "深圳市鹏新旭技术有限公司", "matched_by": "alias",
            "constraints": {"banned_companies": ["青岛芯恩", "福建晋华"], "banned_rule": "指目前在职"},
            "skipped_keys": [], "source_file": "case_pengxinxu_fab_v1.json",
        }
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(), restricted_info=restricted_info, kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["在职保护名单"]
        assert entry["applicable"] is True and entry["source"] == "restricted_client"
        assert "青岛芯恩" in entry["rule"] and "在职保护" in entry["rule"]
        assert "自动继承" in entry["basis"]

        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(), consultant_answers="禁挖名单：甲公司、乙公司", kb_dir=self.kb_dir
        )
        entry = _checklist_types(checklist)["在职保护名单"]
        assert entry["applicable"] is True and entry["source"] == "consultant"

    def test_checklist_output_never_contains_restricted_forbidden_literals(self) -> None:
        info, _ = knowledge_base.load_restricted_constraints("深圳市鹏新旭技术有限公司", kb_dir=self.kb_dir)
        checklist, trace = negative_rules.build_negative_rule_checklist(
            _job(), restricted_info=info, kb_dir=self.kb_dir
        )
        encoded = json.dumps({"checklist": checklist, "trace": trace}, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in encoded, f"restricted 受限字面量不得出现在五类清单：{literal}"


class BannedListInheritanceTest(unittest.TestCase):
    """禁挖继承：客户级禁挖名单从 restricted 层按客户读取，同客户新岗位自动继承。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.temp.name) / "kb")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _on_duty_entry(self, client: str) -> dict:
        info, _ = knowledge_base.load_restricted_constraints(client, kb_dir=self.kb_dir)
        checklist, _ = negative_rules.build_negative_rule_checklist(
            _job(client=client), restricted_info=info, kb_dir=self.kb_dir
        )
        return _checklist_types(checklist)["在职保护名单"]

    def test_same_client_new_jobs_inherit_banned_list(self) -> None:
        # 同客户两个新岗位（岗位文本无任何禁挖信息）→ 均自动继承 restricted 禁挖名单
        for client in ("深圳市鹏新旭技术有限公司", "鹏新旭"):
            entry = self._on_duty_entry(client)
            assert entry["applicable"] is True, f"{client} 新岗位必须继承禁挖名单"
            assert entry["source"] == "restricted_client"
            assert "青岛芯恩" in entry["rule"] and "楚芯" in entry["rule"]
            assert "按客户持久化" in entry["basis"] and "自动继承" in entry["basis"]

    def test_other_client_does_not_inherit(self) -> None:
        entry = self._on_duty_entry("长川科技")
        assert entry["applicable"] is False
        assert "无该客户禁挖名单" in entry["basis"]


class StrategyV2ChecklistIntegrationTest(AgentDbCase):
    """run_search_strategy 集成：五类清单写入 strategy_v2.negative_rules 且校验通过；
    restricted 只进策略约束不进 LLM 输入。"""

    def setUp(self) -> None:
        super().setUp()
        self.kb_temp = tempfile.TemporaryDirectory()
        self.kb_dir = _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (8,'深圳市鹏新旭技术有限公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (80,8,'分析设备专家','深圳','已发布','统招本科及以上','','','经理岗暂不看台湾人','fab 分析设备','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (9,'某某公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (90,9,'销售总监','上海','已发布','','','','','快消行业销售总监','2026-07-23')"
        )
        conn.commit()
        conn.close()
        self.captured: dict = {}

        def _strategy(payload: dict) -> dict:
            self.captured["payload"] = payload
            return {
                "strategy_summary": "模型策略",
                "channels": {"liepin": [{"round": "core", "query": "分析设备", "purpose": "核心", "evidence": "岗位"}], "xsaas": []},
            }

        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), search_strategy=_strategy))

    def tearDown(self) -> None:
        self.service.close()
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        super().tearDown()

    def _run(self, job_id: int) -> dict:
        return self.service.capability_runtime.run_search_strategy({"type": "job", "id": job_id}, {"objective": "补充候选人"})

    def test_five_classes_traced_in_strategy_v2(self) -> None:
        result = self._run(80)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        checklist = {
            entry["type"]: entry
            for entry in v2["negative_rules"]
            if entry["type"] in negative_rules.NEGATIVE_RULE_TYPES
        }
        assert list(checklist) == list(negative_rules.NEGATIVE_RULE_TYPES), "五类必须逐类留痕"
        for entry in checklist.values():
            assert "applicable" in entry and "basis" in entry and "source" in entry
        assert checklist["在职保护名单"]["applicable"] is True
        assert checklist["在职保护名单"]["source"] == "restricted_client"
        assert "青岛芯恩" in checklist["在职保护名单"]["rule"]
        assert checklist["学历门槛"]["applicable"] is True, "岗位硬性要求含统招本科"
        assert checklist["身份/背景限制"]["applicable"] is True, "岗位排除项含台湾人"
        assert checklist["竞业协议排除"]["applicable"] is True, "restricted 竞业约束并入"
        assert checklist["稳定性筛选"]["applicable"] is False
        assert checklist["稳定性筛选"]["basis"], "不适用的类必须给出理由"
        assert any("五类清单" in line for line in v2["classification_trace"])

    def test_five_classes_all_false_when_no_basis(self) -> None:
        result = self._run(90)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        checklist = [
            entry for entry in v2["negative_rules"] if entry["type"] in negative_rules.NEGATIVE_RULE_TYPES
        ]
        assert len(checklist) == 5
        assert all(entry["applicable"] is False for entry in checklist)
        assert all(entry["basis"] for entry in checklist)

    def test_five_classes_degrade_when_kb_seed_missing(self) -> None:
        (self.kb_dir / "kb_seed_jiachi_equipment_v1.json").unlink()
        result = self._run(90)
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        checklist = [
            entry for entry in v2["negative_rules"] if entry["type"] in negative_rules.NEGATIVE_RULE_TYPES
        ]
        assert len(checklist) == 5, "typology 缺失按 PRD §4 默认五类降级，清单仍逐类留痕"

    def test_restricted_never_enters_llm_payload(self) -> None:
        result = self._run(80)
        encoded = json.dumps(self.captured["payload"], ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS + ["青岛芯恩", "福建晋华"]:
            assert literal not in encoded, f"restricted 字面量不得进入 LLM 输入：{literal}"
        # 禁挖名单只进策略约束（negative_rules source=restricted_client），不进 LLM
        v2_encoded = json.dumps(result["strategy_v2"], ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in v2_encoded, f"restricted 受限字面量不得出现在策略对象：{literal}"


if __name__ == "__main__":
    unittest.main()
