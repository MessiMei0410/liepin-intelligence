"""S4-1：输入分级 + L3 提问清单 + strategy_v2 schema 落库测试。

口径：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §1/§2；
知识库种子用临时目录 fixture（运行时只读，绝不触碰真实库与生产 DB）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from pathlib import Path

from test_a_system_agent_v1 import AgentDbCase, fake_assessment
from a_system_agent import AgentService, FakeLLM
from a_system_agent import query_builders, strategy_v2


KB_SEED_FIXTURE = {
    "meta": {"version": "test", "usage": "S4-1 测试 fixture"},
    "job_archetype": {
        "archetype_id": "tme_computing_power",
        "title": "技术市场经理/总监（TME，计算电源管理方向）",
        "client": "士兰微",
        "essence": "面向整机/车企客户的技术型市场岗",
        "directions": [
            {"name": "PC", "customers": ["联想", "荣耀"], "products": ["多相控制器", "DrMOS"], "competitors": ["MPS", "矽力杰"]},
            {"name": "服务器三次电源（板级电源）", "customers": ["浪潮", "新华三"], "products": ["多相控制器", "eFuse"], "competitors": ["MPS", "晶丰明源"]},
        ],
        "target_functions": ["TME", "FAE", "AE", "电源工程师"],
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


def _write_kb(directory: Path, doc: dict | None = KB_SEED_FIXTURE, *, broken: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "seed_silan_tme_v1.json"
    target.write_text("{这不是合法JSON" if broken else json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


class StrategyV2ClassificationTest(unittest.TestCase):
    """任务 1/3：三级定级、定级留痕、原型匹配规则。"""

    def test_l1_with_client_workbook(self) -> None:
        job = {
            "title": "技术市场经理（三次电源/服务器或PC市场）",
            "client": "士兰微",
            "source_layer": "position_workbook+cross_thread_sync",
            "hard_requirements": "本科；8年以上；必须做过三次电源",
            "ability_keywords": "三次电源、Multiphase、VRM、POL、DrMOS",
            "target_companies": "英飞凌、MPS、矽力杰",
            "summary": "主要客户：联想、浪潮；服务器与PC市场",
        }
        result = strategy_v2.classify_strategy_input(job)
        assert result["input_level"] == "L1"
        assert result["anchors"]["customer_of_customer"]["present"] is True
        assert result["anchors"]["customer_of_customer"]["source"] == "client_doc"
        assert result["anchors"]["competitive_landscape"]["present"] is True
        assert result["anchors"]["product_tech_line"]["present"] is True
        assert result["anchors"]["scenario_track"]["present"] is True
        assert result["missing_anchors"] == []
        assert any("定级 L1" in line for line in result["trace"])
        assert all("锚点" in line for line in result["trace"][-5:])

    def test_l2_with_consultant_profile(self) -> None:
        job = {
            "title": "机械高级工程师",
            "client": "长越科技",
            "summary": "精密设备机械核心岗",
            "profile": {
                "hard_requirements_json": '["7年以上精密设备机械设计经验"]',
                "ability_keywords_json": '["有限元"]',
                "target_companies_json": '["ASM","应用材料"]',
                "soft_preferences_json": '["半导体设备经验"]',
            },
        }
        result = strategy_v2.classify_strategy_input(job)
        assert result["input_level"] == "L2"
        assert result["missing_anchors"] == ["customer_of_customer"]
        assert result["anchors"]["product_tech_line"]["source"] == "consultant"
        assert any("定级 L2" in line for line in result["trace"])

    def test_l3_jd_only(self) -> None:
        job = {"title": "销售总监", "client": "某某公司", "summary": "快消行业销售总监，5年以上销售管理经验"}
        result = strategy_v2.classify_strategy_input(job)
        assert result["input_level"] == "L3"
        assert set(result["missing_anchors"]) == set(strategy_v2.ANCHOR_KEYS)
        assert len(result["missing_anchors"]) >= 2
        assert any("定级 L3" in line for line in result["trace"])
        assert any("缺失" in line for line in result["trace"])

    def test_l3_archetype_infers_missing_anchors_with_confidence(self) -> None:
        import tempfile

        kb_dir = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(kb_dir)
        archetypes, _ = strategy_v2.load_job_archetypes(kb_dir)
        archetype, _ = strategy_v2.match_job_archetype("士兰微", "技术市场经理", archetypes)
        job = {"title": "技术市场经理", "client": "士兰微", "summary": "负责产品市场推广"}
        result = strategy_v2.classify_strategy_input(job, archetype=archetype)
        assert result["input_level"] == "L3"
        assert result["missing_anchors"] == []
        for key in ("customer_of_customer", "product_tech_line", "competitive_landscape", "scenario_track"):
            anchor = result["anchors"][key]
            assert anchor["present"] is True
            assert anchor["source"] == "kb_archetype", key
            assert anchor["inferred"] is True
            assert anchor["confidence"] == "medium"

    def test_archetype_match_rules_and_trace(self) -> None:
        import tempfile

        kb_dir = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(kb_dir)
        archetypes, trace = strategy_v2.load_job_archetypes(kb_dir)
        assert [item["archetype_id"] for item in archetypes] == ["tme_computing_power"]
        assert any("已加载岗位原型" in line for line in trace)

        hit, trace = strategy_v2.match_job_archetype("士兰微", "技术市场经理（PC电源）", archetypes)
        assert hit is not None and hit["archetype_id"] == "tme_computing_power"
        assert hit["matched_by"] == "title_token"
        assert any("命中原型" in line for line in trace)

        hit_en, _ = strategy_v2.match_job_archetype("其他客户", "Senior TME Manager", archetypes)
        assert hit_en is not None, "TME 英文缩写同样命中原型"

        miss, trace = strategy_v2.match_job_archetype("某某公司", "销售总监", archetypes)
        assert miss is None
        assert any("未命中" in line for line in trace)

    def test_runtime_kb_matches_changyue_software_and_mechanical_archetypes(self) -> None:
        archetypes, trace = strategy_v2.load_job_archetypes()
        archetype_ids = {item["archetype_id"] for item in archetypes}
        assert "changyue_bonding_motion_control" in archetype_ids, trace
        assert "changyue_precision_equipment_mechanical" in archetype_ids, trace

        software, _ = strategy_v2.match_job_archetype("长越科技", "自动化软件高级工程师", archetypes)
        mechanical, _ = strategy_v2.match_job_archetype("长越科技", "机械高级工程师", archetypes)
        assert software is not None and software["archetype_id"] == "changyue_bonding_motion_control"
        assert mechanical is not None and mechanical["archetype_id"] == "changyue_precision_equipment_mechanical"
        assert software["golden_candidates"]
        assert mechanical["golden_candidates"]
        other_client, _ = strategy_v2.match_job_archetype("其他客户", "机械设计工程师", archetypes)
        assert other_client is None, "长越岗位原型不得因标题相似而污染其他客户策略"

    def test_changyue_golden_candidates_are_covered_by_compiled_query_grid(self) -> None:
        archetypes, _ = strategy_v2.load_job_archetypes()
        for client, title in (
            ("长越科技", "自动化软件高级工程师"),
            ("长越科技", "机械高级工程师"),
        ):
            archetype, _ = strategy_v2.match_job_archetype(client, title, archetypes)
            assert archetype is not None
            classification = strategy_v2.classify_strategy_input(
                {"client": client, "title": title, "summary": archetype["essence"]},
                archetype=archetype,
            )
            v2 = strategy_v2.build_strategy_v2(
                {"channels": {}, "target_companies": [], "strategy_summary": archetype["essence"]},
                classification,
                archetype=archetype,
            )
            query_plan = query_builders.compile_query_plan_v1(v2)
            replay = strategy_v2.build_golden_candidate_replay(archetype, query_plan)

            assert replay is not None
            assert replay["candidate_count"] >= 3
            assert replay["covered_count"] == replay["candidate_count"]
            assert replay["recall_rate"] == 1.0
            assert replay["passed"] is True
            assert replay["uncovered_profile_ids"] == []

    def test_archetype_missing_or_broken_kb_falls_back_with_trace(self) -> None:
        import tempfile

        missing_dir = Path(tempfile.mkdtemp()) / "no_such_dir"
        archetypes, trace = strategy_v2.load_job_archetypes(missing_dir)
        assert archetypes == []
        assert any("按无岗位原型处理" in line for line in trace)

        broken_dir = Path(tempfile.mkdtemp()) / "kb"
        _write_kb(broken_dir, broken=True)
        archetypes, trace = strategy_v2.load_job_archetypes(broken_dir)
        assert archetypes == []
        assert any("解析失败" in line and "按无岗位原型处理" in line for line in trace)

    def test_override_and_answer_detection(self) -> None:
        assert strategy_v2.is_direct_search_override("直接搜")
        assert strategy_v2.is_direct_search_override("先搜吧")
        assert strategy_v2.is_direct_search_override("直接搜索")
        assert strategy_v2.is_direct_search_override("可以搜索")
        assert not strategy_v2.is_direct_search_override("搜索一下结果")
        assert not strategy_v2.is_direct_search_override("今天天气怎么样")
        assert strategy_v2.looks_like_anchor_answer("客户的客户：新能源车企；目标友商：ABC公司")
        assert not strategy_v2.looks_like_anchor_answer("嗯")


class StrategyV2SchemaTest(unittest.TestCase):
    """任务 5：strategy_v2 组装、校验、v1 读取兼容。"""

    def _classification(self, level: str = "L3") -> dict:
        return {
            "input_level": level,
            "anchors": {key: {"present": False, "values": [], "source": "missing", "inferred": False, "confidence": ""} for key in strategy_v2.ANCHOR_KEYS},
            "missing_anchors": list(strategy_v2.ANCHOR_KEYS),
            "trace": ["测试留痕"],
            "archetype_id": "",
        }

    def test_validate_required_keys_and_version(self) -> None:
        v2 = strategy_v2.build_strategy_v2({"target_companies": ["ABC公司"], "channels": {}}, self._classification())
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        assert v2["schema_version"] == "strategy_v2"
        assert v2["consultant_edits"] == []
        assert v2["negative_rules"] == []
        for key in strategy_v2.STRATEGY_V2_REQUIRED_KEYS:
            assert key in v2

        broken = {key: value for key, value in v2.items() if key != "step2_target_pool"}
        ok, errors = strategy_v2.validate_strategy_v2(broken)
        assert not ok
        assert any("step2_target_pool" in error for error in errors)

        wrong_version = dict(v2, schema_version="strategy_v1")
        ok, errors = strategy_v2.validate_strategy_v2(wrong_version)
        assert not ok and any("schema_version" in error for error in errors)

        bad_pool = dict(v2, step2_target_pool=[{"path": "unknown", "tier": "T9", "companies": [{"name": "X", "source": "web", "confidence": "sure"}], "rationale": ""}])
        ok, errors = strategy_v2.validate_strategy_v2(bad_pool)
        assert not ok
        assert any("path" in error for error in errors)
        assert any("source" in error for error in errors)
        assert any("confidence" in error for error in errors)

    def test_build_marks_consultant_override_and_inferred(self) -> None:
        classification = self._classification()
        classification["anchors"]["product_tech_line"] = {
            "present": True, "values": ["DrMOS"], "source": "kb_archetype", "inferred": True, "confidence": "medium",
        }
        v2 = strategy_v2.build_strategy_v2(
            {"target_companies": ["ABC公司"], "channels": {}},
            classification,
            consultant={"consultant_override": True},
        )
        assert v2["consultant_override"] is True
        assert v2["anchors"]["product_tech_line"]["inferred"] is True
        assert v2["anchors"]["product_tech_line"]["confidence"] == "medium"
        assert "待确认" in v2["step2_target_pool"][0]["rationale"]
        assert v2["step2_target_pool"][0]["companies"][0]["source"] == "llm_inferred"

    def test_explicit_negative_rule_keeps_graph_companies_out_of_target_pool(self) -> None:
        v2 = strategy_v2.build_strategy_v2(
            {"channels": {}},
            self._classification(),
            llm_fragment={
                "step2_target_pool": [
                    {
                        "path": "same_layer",
                        "tier": "T1",
                        "companies": [{"name": "台达", "source": "client_doc", "confidence": "high"}],
                        "rationale": "VPD/VRM 模块电源原厂",
                    }
                ],
                "negative_rules": [
                    {
                        "type": "行业排除",
                        "rule": "不得搜索或推荐2家kb_graph公司：上海微电子装备、芯钛科半导体设备",
                        "source": "consultant_confirmed_exclusion",
                    }
                ],
            },
            graph_pool=[
                {"name": "上海微电子装备", "confidence": "medium"},
                {"name": "芯钛科半导体设备", "confidence": "high"},
            ],
        )

        companies = [
            company["name"]
            for entry in v2["step2_target_pool"]
            for company in entry["companies"]
        ]
        assert companies == ["台达"]
        assert v2["step2_source_distribution"] == {"client_doc": 1}
        assert any("图谱公司命中显式排除规则" in line for line in v2["classification_trace"])

    def test_extract_strategy_v2_from_v1_metadata_returns_none(self) -> None:
        assert strategy_v2.extract_strategy_v2({"plan": {"channels": {}}}) is None
        assert strategy_v2.extract_strategy_v2('{"plan": {}}') is None
        assert strategy_v2.extract_strategy_v2(None) is None
        v2 = strategy_v2.build_strategy_v2({"channels": {}}, self._classification())
        assert strategy_v2.extract_strategy_v2({"plan": {}, "strategy_v2": v2}) == v2


class StrategyV2CopilotGateTest(AgentDbCase):
    """任务 2/4：L3 提问清单门控、原型放行、顾问 override/锚点回复。"""

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        self.kb_temp = tempfile.TemporaryDirectory()
        _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(Path(self.kb_temp.name) / "kb")
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (2,'某某公司')")
        conn.execute(
            "INSERT INTO jobs VALUES (20,2,'销售总监','上海','已发布','','','','','快消行业销售总监，5年以上销售管理经验','2026-07-23')"
        )
        conn.execute("INSERT INTO clients VALUES (3,'士兰微')")
        conn.execute(
            "INSERT INTO jobs VALUES (30,3,'技术市场经理（PC电源）','杭州','已发布','','','','','PC方向','2026-07-23')"
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

    def _workflow_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM agent_workflows").fetchone()[0])
        finally:
            conn.close()

    def test_l3_gate_asks_four_anchor_questions_without_workflow(self) -> None:
        session_id = "s4-gate-ask"
        result = self.service.copilot(
            "给某某公司销售总监再找些候选人",
            session_id=session_id,
            context={"type": "job", "id": 20},
        )
        assert result["workflow_id"] is None
        assert result["goal"] is None
        assert self._workflow_count() == 0, "提问清单场景不得创建任何工作流"
        answer = result["answer"]
        assert "客户的客户" in answer
        assert "对标友商" in answer
        assert "禁挖名单" in answer
        assert "直接搜" in answer
        assert "已启动" not in answer and "已建立目标" not in answer
        assert not any(action.get("type") == "start_workflow" for action in result["suggested_actions"])
        # 无外部执行：没有审批、没有产物
        conn = sqlite3.connect(self.db_path)
        try:
            assert int(conn.execute("SELECT COUNT(*) FROM agent_approvals").fetchone()[0]) == 0
            assert int(conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0]) == 0
        finally:
            conn.close()
        pending = self.service._pending_strategy_clarification(session_id)
        assert pending["job_id"] == 20
        assert pending["input_level"] == "L3"
        assert set(pending["missing_anchors"]) == set(strategy_v2.ANCHOR_KEYS)
        assert len(pending["questions"]) == 4

    def test_archetype_hit_bypasses_gate_and_creates_workflow(self) -> None:
        result = self.service.copilot(
            "给士兰微技术市场经理再找些候选人",
            session_id="s4-gate-archetype",
            context={"type": "job", "id": 30},
        )
        assert result["workflow_id"], result["answer"]
        assert any(step["capability_id"] == "multi_channel_sourcing" for step in result["plan_summary"])
        assert self._workflow_count() == 1
        assert self.service._pending_strategy_clarification("s4-gate-archetype") == {}

    def test_direct_search_override_creates_workflow_with_consultant_override(self) -> None:
        session_id = "s4-gate-override"
        blocked = self.service.copilot(
            "给某某公司销售总监再找些候选人",
            session_id=session_id,
            context={"type": "job", "id": 20},
        )
        assert blocked["workflow_id"] is None

        override = self.service.copilot("直接搜", session_id=session_id, context={"type": "global"})
        assert override["workflow_id"], override["answer"]
        assert self._workflow_count() == 1
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT context_json FROM agent_workflow_context WHERE workflow_id=?",
                (override["workflow_id"],),
            ).fetchone()
        finally:
            conn.close()
        context = json.loads(row[0])
        clarification = context["strategy_clarification"]
        assert clarification["consultant_override"] is True
        assert clarification["asked_questions"] is True
        assert clarification["input_level"] == "L3"
        # pending 已消化：后续不再误判悬挂
        assert self.service._pending_strategy_clarification(session_id) == {}

        # 策略对象记录 consultant_override，推断项保持 inferred+confidence
        result = self.service.capability_runtime.run_search_strategy(context, {"objective": override["goal"]["objective"]})
        v2 = result["strategy_v2"]
        assert v2["consultant_override"] is True
        inferred_anchors = [a for a in v2["anchors"].values() if a.get("inferred")]
        assert all(anchor.get("confidence") for anchor in inferred_anchors)

    def test_consultant_anchor_answers_merge_into_strategy_context(self) -> None:
        session_id = "s4-gate-answers"
        blocked = self.service.copilot(
            "给某某公司销售总监再找些候选人",
            session_id=session_id,
            context={"type": "job", "id": 20},
        )
        assert blocked["workflow_id"] is None

        answers = "客户的客户：新能源车企；目标友商：ABC公司；有禁挖名单"
        followup = self.service.copilot(answers, session_id=session_id, context={"type": "global"})
        assert followup["workflow_id"], followup["answer"]
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT context_json FROM agent_workflow_context WHERE workflow_id=?",
                (followup["workflow_id"],),
            ).fetchone()
        finally:
            conn.close()
        context = json.loads(row[0])
        assert context["strategy_clarification"]["consultant_override"] is False
        assert context["strategy_clarification"]["consultant_answers"] == answers

        result = self.service.capability_runtime.run_search_strategy(context, {"objective": "补充候选人"})
        v2 = result["strategy_v2"]
        assert v2["consultant_override"] is False
        assert "新能源车企" in v2["anchors"]["customer_of_customer"]["values"]
        assert v2["anchors"]["customer_of_customer"]["source"] == "consultant"
        assert "ABC公司" in v2["anchors"]["competitive_landscape"]["values"]
        assert v2["consultant_answers"] == answers

    def test_unrelated_reply_does_not_consume_pending(self) -> None:
        session_id = "s4-gate-unrelated"
        self.service.copilot(
            "给某某公司销售总监再找些候选人",
            session_id=session_id,
            context={"type": "job", "id": 20},
        )
        chatter = self.service.copilot("今天天气怎么样", session_id=session_id, context={"type": "global"})
        assert chatter["workflow_id"] is None
        assert self._workflow_count() == 0
        assert self.service._pending_strategy_clarification(session_id)["job_id"] == 20


class StrategyV2StorageTest(AgentDbCase):
    """任务 5/6：run_search_strategy 落库 v2、校验失败不写库、v1 旧数据兼容。"""

    def setUp(self) -> None:
        super().setUp()
        import tempfile

        self.kb_temp = tempfile.TemporaryDirectory()
        _write_kb(Path(self.kb_temp.name) / "kb")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(Path(self.kb_temp.name) / "kb")
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (3,'士兰微')")
        conn.execute(
            "INSERT INTO jobs VALUES (30,3,'技术市场经理（PC电源）','杭州','已发布','','','','','PC方向','2026-07-23')"
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

    def wait_for(self, workflow_id: str, statuses: set[str], timeout: float = 8) -> dict:
        deadline = time.time() + timeout
        state = self.service.get_workflow(workflow_id)
        while time.time() < deadline and state["workflow"]["status"] not in statuses:
            time.sleep(0.03)
            state = self.service.get_workflow(workflow_id)
        return state

    def test_run_search_strategy_builds_valid_v2_with_kb_pool(self) -> None:
        self.service.llm = FakeLLM(
            fake_assessment(),
            search_strategy={
                "strategy_summary": "模型策略",
                "channels": {"liepin": [{"round": "core", "query": "多相控制器 DrMOS TME", "purpose": "核心", "evidence": "岗位"}], "xsaas": []},
                "strategy_v2": {
                    "step1_job_essence": {"statement": "面向整机客户的技术型市场岗", "value_chain_role": "器件原厂 TME", "confirmed_by": "consultant"},
                    "step4_keyword_groups": [{"group": "competitor_tme", "targets": "T1 友商", "terms": ["MPS", "TME"]}],
                    "step5_expectation": {"expected_recall_per_tier": {"T1": 15}, "fallback_plan": "T1不足放宽T3"},
                    "negative_rules": [{"type": "方向词", "rule": "不用PC电源字面", "source": "kb_profile"}],
                },
            },
        )
        result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 30}, {"objective": "补充候选人"})
        v2 = result["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        assert v2["schema_version"] == "strategy_v2"
        assert v2["input_level"] == "L3"
        assert v2["consultant_edits"] == []
        assert v2["consultant_override"] is False
        assert v2["archetype_id"] == "tme_computing_power"
        # LLM 未给 step2 → 命中原型的知识库公司池（kb_profile）
        assert v2["step2_target_pool"], errors
        sources = {company["source"] for entry in v2["step2_target_pool"] for company in entry["companies"]}
        assert sources == {"kb_profile"}
        paths = {entry["path"] for entry in v2["step2_target_pool"]}
        assert {"same_layer", "reverse", "adjacent"}.issuperset(paths)
        # LLM 填充的步骤透传
        assert v2["step1_job_essence"]["statement"] == "面向整机客户的技术型市场岗"
        assert v2["step4_keyword_groups"][0]["group"] == "competitor_tme"
        assert v2["step3_level_mapping"]["accepted_levels"] == ["主管", "经理", "总监"]
        artifact = result["artifacts"][0]
        assert artifact["type"] == "search_strategy"
        assert artifact["metadata"]["schema_version"] == "strategy_v2"
        assert artifact["metadata"]["strategy_v2"]["step5_expectation"]["fallback_plan"] == "T1不足放宽T3"
        assert "plan" in artifact["metadata"], "v1 plan 读取侧兼容保留"
        query_plan = result["query_plan_v1"]
        assert query_plan["cells"]
        assert query_plan["plan_hash"] == query_builders.query_plan_hash(query_plan)
        assert artifact["metadata"]["query_plan_v1"] == query_plan
        assert result["strategy"]["generation"]["input_level"] == "L3"

    def test_workflow_persists_strategy_v2_artifact(self) -> None:
        goal = self.service.create_goal("给士兰微技术市场经理补充5位合适人选", {"type": "job", "id": 30})
        workflow_id = goal["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        state = self.wait_for(workflow_id, {"waiting_approval", "failed", "blocked", "completed"})
        assert state["workflow"]["status"] == "waiting_approval", state["workflow"]
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT metadata_json,content FROM agent_artifacts WHERE workflow_id=? AND artifact_type='search_strategy' ORDER BY id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row, "search_strategy artifact 必须落库"
        metadata = json.loads(row[0])
        v2 = metadata["strategy_v2"]
        ok, errors = strategy_v2.validate_strategy_v2(v2)
        assert ok, errors
        assert v2["schema_version"] == "strategy_v2"
        assert v2["consultant_edits"] == []
        assert metadata["schema_version"] == "strategy_v2"
        assert "strategy_v2" in row[1]

    def test_invalid_v2_is_not_stored_and_records_error(self) -> None:
        original = strategy_v2.build_strategy_v2
        try:
            strategy_v2.build_strategy_v2 = lambda *args, **kwargs: {"schema_version": "strategy_v2"}  # type: ignore[assignment]
            result = self.service.capability_runtime.run_search_strategy({"type": "job", "id": 30}, {"objective": "补充候选人"})
        finally:
            strategy_v2.build_strategy_v2 = original  # type: ignore[assignment]
        assert "strategy_v2" not in result
        assert "artifacts" not in result, "校验失败的 strategy_v2 不得写库"
        assert any("缺少必备键" in error for error in result["strategy_v2_error"]["errors"])
        assert result["strategy_v2_error"]["trace"]

    def test_v1_step_output_read_compat(self) -> None:
        goal = self.service.create_goal("给长越科技机械高级工程师补充10位合适人选", {"type": "job", "id": 10})
        workflow_id = goal["workflow"]["workflow_id"]
        legacy_plan = {"channels": {"liepin": [{"round": "core", "query": "精密机械 运动台", "purpose": "核心"}]}, "generation": {"model": "old"}}
        conn = sqlite3.connect(self.db_path)
        try:
            step_id = conn.execute(
                "SELECT id FROM agent_workflow_steps WHERE workflow_id=? AND capability_id='search_strategy'",
                (workflow_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE agent_workflow_steps SET status='completed',output_json=? WHERE id=?",
                (json.dumps({"strategy": legacy_plan}, ensure_ascii=False), step_id),
            )
            conn.execute(
                """
                INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
                VALUES ('artifact_v1','goal_x',?,?,'search_strategy','多渠道寻访策略','text/markdown','v1 内容',?, 'passed')
                """,
                (workflow_id, step_id, json.dumps({"plan": legacy_plan}, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()
        # v1 旧 step 输出：执行侧读取 plan 不崩
        strategy = self.service.capability_runtime._workflow_strategy({"workflow_id": workflow_id})
        assert strategy["channels"]["liepin"][0]["query"] == "精密机械 运动台"
        # v1 旧 artifact：读取侧 extract 返回 None 不崩；工作流详情正常
        detail = self.service.get_workflow(workflow_id)
        artifact = next(item for item in detail["artifacts"] if item["artifact_id"] == "artifact_v1")
        assert strategy_v2.extract_strategy_v2(artifact.get("metadata") or artifact.get("metadata_json")) is None


if __name__ == "__main__":
    unittest.main()
