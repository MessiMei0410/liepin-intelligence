from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.conversation_state import (  # noqa: E402
    STATE_VERSION,
    TURN_VERSION,
    build_context_state,
    enrich_turn_understanding,
)
from copilot_business_replay import DEFAULT_CORPUS, _database_observation, evaluate_service_scenario_matrix, run_replay  # noqa: E402
from copilot_business_replay import replay_service_turn, summarize_service_traces  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.llm import UnavailableLLM  # noqa: E402
from asa_core.service import CoreService  # noqa: E402
from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402


def test_corpus_has_real_and_adversarial_business_turns() -> None:
    corpus = json.loads(DEFAULT_CORPUS.read_text(encoding="utf-8"))
    cases = corpus["cases"]
    assert len(cases) == 160
    assert sum(case["source"] == "local_asa_session_anonymized" for case in cases) == 120
    assert sum(case["source"] == "adversarial_v1" for case in cases) == 40
    required = {
        "context", "history", "speech_act", "primary_action", "target", "objective",
        "fact_updates", "constraint_changes", "operations", "expected_operation_order",
        "expected_route", "expected_business_result", "should_execute", "needs_clarification",
        "source_quotes",
    }
    assert all(required.issubset(case) for case in cases)
    assert all(
        all(quote in case["message"] for quote in case.get("source_quotes") or [])
        for case in cases
    )
    serialized = json.dumps(corpus, ensure_ascii=False)
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", serialized)
    assert "/Users/" not in serialized


def test_compound_filter_stop_list_has_ordered_operations() -> None:
    message = "把所有候选人过滤一下，不匹配的停止推进，再给我名单"
    turn = enrich_turn_understanding(
        {"speech_act": "other", "action": "none", "confidence": 1.0},
        message=message,
        pending_plan_ref={},
    )
    assert turn["version"] == TURN_VERSION == "copilot_turn_understanding_v3"
    assert turn["primary_action"] == "candidate_review"
    assert [item["action"] for item in turn["operations"]] == [
        "filter_candidates", "batch_stop", "list_candidates",
    ]
    assert turn["source_quotes"] == [message]


def test_replay_question_and_discussion_never_execute() -> None:
    report = run_replay()
    assert report["total"] == 160
    assert report["methodology"]["evaluation_scope"] == "deterministic_understanding_postprocessor"
    assert report["methodology"]["end_to_end"] is False
    assert report["metrics"]["question_or_discussion_misexecution"] == 0
    assert report["metrics"]["explicit_object_accuracy"] is None
    assert "explicit_object_accuracy" in report["methodology"]["not_authoritative"]
    assert report["metrics"]["high_frequency_action_accuracy"] >= 0.95
    assert report["metrics"]["five_turn_goal_and_condition_retention"] >= 0.95
    assert report["failures_by_layer"] == {}


def test_observation_does_not_replace_five_turn_goal() -> None:
    focus = {
        "context": {"type": "job", "id": 137},
        "client": "客户甲",
        "job": {"id": 137, "title": "机械高级工程师"},
        "candidate": {},
        "confidence": 1.0,
    }
    state = build_context_state(
        None,
        message="再找 10 人",
        context={"type": "job", "id": 137},
        business_focus=focus,
        understanding={
            "turn_kind": "command", "topic": "sourcing", "action": "candidate_sourcing",
            "objective": "再找 10 人", "target": {"type": "job", "id": 137}, "fact_updates": [],
        },
        decision={"effect": "create_plan", "effective_constraints": []},
        workflow_intent=None,
        now="2026-08-15 10:00:00",
    )
    for index, message in enumerate(["这轮只找到 2 人", "客户说可以放宽行业", "地点还是江浙沪", "先不要触达"]):
        turn = enrich_turn_understanding(
            {"speech_act": "inform", "action": "none", "confidence": 1.0},
            message=message,
            pending_plan_ref={},
        )
        state = build_context_state(
            state,
            message=message,
            context={"type": "job", "id": 137},
            business_focus=focus,
            understanding=turn,
            decision={"effect": "answer"},
            workflow_intent=None,
            now=f"2026-08-15 10:0{index + 1}:00",
        )
    assert state["version"] == STATE_VERSION == "copilot_context_state_v3"
    assert state["active_goal"]["objective"] == "再找 10 人"
    assert any(item["quote"] == "这轮只找到 2 人" for item in state["observations"])


class TestCopilotServiceReplay(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="已理解。"))

    def tearDown(self) -> None:
        self.service.close()
        super().tearDown()

    def _run(self, message: str, session_id: str, context: dict) -> dict:
        return self.service.copilot(message, session_id=session_id, context=context)

    def test_real_service_matrix_meets_object_action_and_question_gates(self) -> None:
        job_actions = [
            ("给当前岗位补池10人", "candidate_sourcing"),
            ("为这个岗位再找8个人", "candidate_sourcing"),
            ("继续给这个岗位找人", "candidate_sourcing"),
            ("重新开启这个岗位的寻访", "candidate_sourcing"),
            ("给这个岗位补充12位候选人", "candidate_sourcing"),
            ("筛选一下这个岗位的人选", "candidate_review"),
            ("重新过滤再给名单", "candidate_review"),
            ("把这个岗位候选池分级", "candidate_review"),
            ("把这个岗位的候选名单给我", "candidate_review"),
            ("把所有候选人过滤一下，不匹配的停止推进，再给我名单", "candidate_review"),
        ]
        candidate_actions = [
            ("给这个人选做推荐报告", "recommendation"),
            ("帮我整理这个人选的推荐材料", "recommendation"),
            ("推荐这个人选", "recommendation"),
            ("联系这个人选", "candidate_outreach"),
            ("触达这个人选", "candidate_outreach"),
            ("开聊这个人选", "candidate_outreach"),
            ("停止推进这个人选", "candidate_review"),
            ("推进这个人选", "candidate_review"),
            ("这个人选复核通过", "candidate_review"),
            ("给这个人选整理谈薪方案", "salary"),
        ]
        questions = [
            ("这个岗位现在有多少人？", {"type": "job", "id": 10}),
            ("这个岗位要不要继续寻访？", {"type": "job", "id": 10}),
            ("这个岗位的名单出来了吗？", {"type": "job", "id": 10}),
            ("这个岗位为什么人数不足？", {"type": "job", "id": 10}),
            ("这个岗位能不能放宽条件？", {"type": "job", "id": 10}),
            ("这个人选现在什么阶段？", {"type": "candidate", "id": 30}),
            ("这个人选要不要停止推进？", {"type": "candidate", "id": 30}),
            ("这个人选是否适合推荐？", {"type": "candidate", "id": 30}),
            ("这个人选为什么没推进？", {"type": "candidate", "id": 30}),
            ("这个人选能不能联系？", {"type": "candidate", "id": 30}),
        ]
        scenarios = [
            {
                "id": f"job-action-{index}", "message": message,
                "context": {"type": "job", "id": 10},
                "expected_target": {"type": "job", "id": 10}, "expected_action": action,
            }
            for index, (message, action) in enumerate(job_actions, 1)
        ] + [
            {
                "id": f"candidate-action-{index}", "message": message,
                "context": {"type": "candidate", "id": 30},
                "expected_target": {"type": "candidate", "id": 30}, "expected_action": action,
            }
            for index, (message, action) in enumerate(candidate_actions, 1)
        ] + [
            {
                "id": f"question-{index}", "message": message, "context": context,
                "expected_target": context, "expected_action": "none", "expect_zero_execution": True,
            }
            for index, (message, context) in enumerate(questions, 1)
        ]

        report = evaluate_service_scenario_matrix(
            db_path=self.db_path,
            scenarios=scenarios,
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )

        assert report["expected_values_injected"] is False
        assert report["metrics"]["explicit_object_accuracy"] >= 0.98
        assert report["metrics"]["high_frequency_action_accuracy"] >= 0.95
        assert report["metrics"]["question_or_discussion_misexecution"] == 0
        assert report["failures"] == []

    def test_real_service_trace_proves_question_has_zero_execution(self) -> None:
        trace = replay_service_turn(
            db_path=self.db_path,
            message="这个人选要不要停止推进？",
            session_id="trace-question",
            context={"type": "candidate", "id": 30, "page": "candidates"},
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )

        assert trace["understanding"]["speech_act"] == "ask"
        assert trace["understanding"]["safe_for_action"] is False
        assert trace["writes"] == {"candidate_events": 0, "workflows": 0, "commands": 0}
        assert trace["tool"]["workflow_id"] is None
        assert "已启动" not in trace["answer"]
        service_metrics = summarize_service_traces([trace])
        assert service_metrics["evaluation_scope"] == "real_service_boundary"
        assert service_metrics["routes"] == {"read": 1, "clarify": 0, "confirm": 0, "workflow": 0}
        assert service_metrics["write_totals"] == {"candidate_events": 0, "workflows": 0, "commands": 0}
        persisted = self.service.get_copilot_session("trace-question")["messages"][-1]["turn_trace"]
        assert persisted["user_message"] == "这个人选要不要停止推进？"
        assert persisted["route"] == "read"
        assert persisted["receipt"]["verified"] is True

    def test_question_does_not_mutate_condition_ledger(self) -> None:
        session_id = "trace-question-condition-ledger"
        before = self.service.get_copilot_context_state(session_id)

        result = self.service.copilot(
            "这个岗位要不要继续寻访？",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        after = self.service.get_copilot_context_state(session_id)
        assert result["intent_understanding"]["speech_act"] == "ask"
        assert result["intent_understanding"]["constraints"] == []
        assert result["turn_decision"]["constraint_changes"] == []
        assert after.get("constraints", []) == before.get("constraints", []) == []
        assert after.get("corrections", []) == before.get("corrections", []) == []

    def test_real_service_trace_captures_command_and_verified_receipt(self) -> None:
        conn = sqlite3.connect(self.db_path)
        for column in ("clean_reason", "stop_reason"):
            try:
                conn.execute(f"ALTER TABLE job_candidates ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT INTO people VALUES (21,'李工','某电气公司','电气工程师','上海','本科','8年')"
        )
        conn.execute(
            "INSERT INTO candidates VALUES (41,'李工','某电气公司','电气工程师','本科','8年','',"
            "'上海','长越科技','机械高级工程师','new','','2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id,job_id,person_id,raw_client,raw_position,raw_status,raw_stage,clean_stage,flow_bucket,updated_at,source_candidate_id) "
            "VALUES (31,10,21,'长越科技','机械高级工程师','new','','S1 新增寻访/待复核','待复核','2026-07-14','41')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles VALUES (51,41,'李工','某电气公司','长越科技','机械高级工程师',"
            "'本科','8年','[]','[]','[]','电气控制柜设计','2026-07-14')"
        )
        conn.commit()
        conn.close()

        trace = replay_service_turn(
            db_path=self.db_path,
            message="把所有候选人过滤一下，不匹配的停止推进，再给我名单",
            session_id="trace-command",
            context={"type": "job", "id": 10, "page": "positions"},
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )

        assert trace["route"] == "confirm"
        assert trace["writes"]["candidate_events"] == 0
        assert trace["writes"]["workflows"] == 0
        assert trace["writes"]["commands"] == 1
        command = trace["tool"]["pending_command"]
        assert [item["action"] for item in command["operations"]] == [
            "filter_candidates", "batch_stop", "list_candidates",
        ]
        preflight = self.service.preflight_copilot_command(command["command_id"])
        decision = self.service.decide_copilot_command(
            command["command_id"],
            decision="approve",
            confirmation_token=preflight["confirmation_token"],
        )
        receipt = decision["receipt"]
        assert receipt["verified"] is True
        assert receipt["succeeded"] == 1

    def test_candidate_list_source_is_scoped_to_mapping_relation(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE candidates ADD COLUMN source TEXT")
        conn.execute("ALTER TABLE candidates ADD COLUMN xsaas_id TEXT")
        conn.execute(
            "CREATE TABLE entity_source_links (id INTEGER PRIMARY KEY,canonical_type TEXT,canonical_id TEXT,"
            "source_system TEXT,source_entity_type TEXT,source_entity_id TEXT,source_url TEXT,metadata_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE recommendation_packages (id INTEGER PRIMARY KEY,package_id TEXT,"
            "job_candidate_id INTEGER,version INTEGER)"
        )
        conn.execute("CREATE TABLE recommendation_package_feedback (id INTEGER PRIMARY KEY,package_id TEXT)")
        conn.execute(
            "INSERT INTO candidate_events "
            "(id,job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id) "
            "VALUES (91,30,20,10,'mapping_intake','pending_review','2026-08-15 10:00:00','Mapping 入库','{}','mapping_task','mapping_task_1')"
        )
        conn.execute(
            "INSERT INTO source_profiles VALUES (99,20,'liepin','later-profile','2026-08-15','','长越科技','机械高级工程师','{}')"
        )
        conn.commit()
        conn.close()

        core = CoreService(self.db_path)
        item = core.candidates()["items"][0]
        assert item["id"] == 30
        assert item["source_type"] == "mapping"
        assert core.candidates(query="30")["items"][0]["id"] == 30
        detail = core.candidate(30)["candidate"]
        assert detail["source_type"] == "mapping"
        assert detail["source_lineage"][0]["source_type"] == "mapping"

    def test_recent_workflow_progress_query_resolves_from_session(self) -> None:
        session_id = "trace-recent-workflow"
        proposed = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert proposed["workflow_id"]
        assert not proposed.get("pending_command")
        # 问询进展只读：不新增工作流，且能从会话解析出最近工作流。
        assert replay_service_turn(
            db_path=self.db_path,
            message="刚才那个任务进展如何？",
            session_id=session_id,
            context={"type": "job", "id": 10},
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )["writes"]["workflows"] == 0
        trace = replay_service_turn(
            db_path=self.db_path,
            message="刚才那个任务进展如何？",
            session_id=session_id,
            context={"type": "global"},
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )
        assert trace["tool"]["workflow_id"] == proposed["workflow_id"]
        assert trace["writes"]["workflows"] == 0
        assert trace["understanding"]["speech_act"] == "ask"

    def test_adjacent_natural_language_cancel_rejects_pending_batch_stop_command(self) -> None:
        conn = sqlite3.connect(self.db_path)
        for column in ("clean_reason", "stop_reason"):
            try:
                conn.execute(f"ALTER TABLE job_candidates ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT INTO people VALUES (21,'李工','某电气公司','电气工程师','上海','本科','8年')"
        )
        conn.execute(
            "INSERT INTO candidates VALUES (41,'李工','某电气公司','电气工程师','本科','8年','',"
            "'上海','长越科技','机械高级工程师','new','','2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id,job_id,person_id,raw_client,raw_position,raw_status,raw_stage,clean_stage,flow_bucket,updated_at,source_candidate_id) "
            "VALUES (31,10,21,'长越科技','机械高级工程师','new','','S1 新增寻访/待复核','待复核','2026-07-14','41')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles VALUES (51,41,'李工','某电气公司','长越科技','机械高级工程师',"
            "'本科','8年','[]','[]','[]','电气控制柜设计','2026-07-14')"
        )
        conn.commit()
        conn.close()

        session_id = "trace-batch-stop-command-cancel"
        before = _database_observation(self.db_path)
        proposed = self.service.copilot(
            "把所有候选人过滤一下，不匹配的停止推进，再给我名单",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        command_id = proposed["pending_command"]["command_id"]

        cancelled = self.service.copilot(
            "算了",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert cancelled["workflow_id"] is None
        assert cancelled["turn_decision"]["effect"] == "cancel_command"
        assert cancelled["execution_receipt"]["state"] == "已取消"
        assert self.service.get_copilot_command(command_id)["command"]["status"] == "rejected"
        assert self.service.get_copilot_context_state(session_id)["pending_command"] == {}
        assert _database_observation(self.db_path)["candidate_events"] == before["candidate_events"]

    def test_natural_language_cannot_approve_r3(self) -> None:
        created = self.service.create_goal(
            "给长越科技机械高级工程师补充10位候选人", {"type": "job", "id": 10}
        )
        workflow_id = created["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        deadline = time.time() + 5
        state = self.service.get_workflow(workflow_id)
        while time.time() < deadline and state["workflow"]["status"] not in {"waiting_approval", "failed"}:
            time.sleep(0.03)
            state = self.service.get_workflow(workflow_id)
        approval = next(item for item in state["approvals"] if item["risk_level"] == "R3")
        assert approval["status"] == "pending"

        trace = replay_service_turn(
            db_path=self.db_path,
            message="可以，批准这次外部寻访",
            session_id="trace-r3-text",
            context={"type": "workflow", "id": workflow_id},
            run_turn=self._run,
            read_state=self.service.get_copilot_context_state,
        )
        after = self.service.get_workflow(workflow_id)
        after_approval = next(item for item in after["approvals"] if item["approval_id"] == approval["approval_id"])
        assert after_approval["status"] == "pending"
        assert after["workflow"]["status"] == "waiting_approval"
        assert trace["writes"]["candidate_events"] == 0

    def test_model_unavailable_keeps_reads_and_fails_closed_for_uncertain_writes(self) -> None:
        unavailable = AgentService(self.db_path, UnavailableLLM())
        try:
            query = replay_service_turn(
                db_path=self.db_path,
                message="这个岗位现在有多少人？",
                session_id="trace-unavailable-read",
                context={"type": "job", "id": 10},
                run_turn=lambda message, session_id, context: unavailable.copilot(
                    message, session_id=session_id, context=context
                ),
                read_state=unavailable.get_copilot_context_state,
            )
            assert "共有 1 位候选人" in query["answer"]
            assert query["writes"] == {"candidate_events": 0, "workflows": 0, "commands": 0}

            command = replay_service_turn(
                db_path=self.db_path,
                message="按前面讨论的方案处理一下这批人",
                session_id="trace-unavailable-write",
                context={"type": "job", "id": 10},
                run_turn=lambda message, session_id, context: unavailable.copilot(
                    message, session_id=session_id, context=context
                ),
                read_state=unavailable.get_copilot_context_state,
            )
            assert command["understanding"]["needs_clarification"] is True
            assert command["understanding"]["safe_for_action"] is False
            assert "未执行、未写入业务表" in command["answer"]
            assert command["writes"] == {"candidate_events": 0, "workflows": 0, "commands": 0}
        finally:
            unavailable.close()
