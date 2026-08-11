from __future__ import annotations

import json

from a_system_agent import AgentService, FakeLLM
from a_system_agent.conversation_state import (
    build_context_state,
    deterministic_context_summary,
    enrich_turn_understanding,
)
from a_system_agent.turn_decision import build_turn_decision
from test_a_system_agent_v1 import AgentDbCase, fake_assessment


def _understanding(**overrides):
    value = {
        "speech_act": "propose",
        "action": "candidate_sourcing",
        "topic": "sourcing",
        "objective": "为当前岗位补充候选人",
        "target": {"type": "job", "id": 10, "client": "长越科技", "label": "机械高级工程师"},
        "constraints": [],
        "fact_updates": [],
        "refers_to_previous": False,
        "confidence": 0.96,
        "needs_clarification": False,
    }
    value.update(overrides)
    return value


def test_salary_topic_is_not_a_salary_action_without_action_evidence() -> None:
    message = "士兰微这个岗位预算 120w"
    understanding = enrich_turn_understanding(
        _understanding(
            speech_act="propose",
            action="salary",
            topic="salary",
            objective="整理谈薪材料",
        ),
        message=message,
        pending_plan_ref={},
    )

    assert understanding["speech_act"] == "inform"
    assert understanding["action"] == "none"
    assert understanding["topic"] == "salary"
    assert understanding["action_evidence"] == []
    assert understanding["fact_updates"] == [{
        "kind": "job_budget",
        "quote": message,
        "value": "120w",
    }]

    decision = build_turn_decision(understanding, message=message)
    assert decision["effect"] == "answer"
    assert decision["safe_for_action"] is False
    assert decision["blocked_reason"] == ""


def test_explicit_salary_request_can_create_a_draft_plan() -> None:
    message = "帮我给这个人选整理谈薪方案"
    understanding = enrich_turn_understanding(
        _understanding(
            speech_act="execute",
            action="salary",
            topic="salary",
            objective=message,
            target={"type": "candidate", "id": 30},
        ),
        message=message,
        pending_plan_ref={},
    )

    assert understanding["action"] == "salary"
    assert understanding["action_evidence"] == [message]
    decision = build_turn_decision(understanding, message=message)
    assert decision["effect"] == "create_plan"
    assert decision["safe_for_action"] is True
    assert decision["authorization"]["evidence"] == [message]


def test_observation_with_candidate_words_does_not_become_sourcing() -> None:
    message = "这轮只找到两个人选"
    understanding = enrich_turn_understanding(
        _understanding(
            speech_act="propose",
            action="candidate_sourcing",
            objective="这轮只找到两个人选",
        ),
        message=message,
        pending_plan_ref={},
    )

    assert understanding["speech_act"] == "inform"
    assert understanding["action"] == "none"
    assert understanding["action_evidence"] == []
    assert build_turn_decision(understanding, message=message)["effect"] == "answer"


def test_model_inform_cannot_retain_a_sourcing_action() -> None:
    message = "这轮只找到两个人选"
    understanding = enrich_turn_understanding(
        _understanding(
            speech_act="inform",
            action="candidate_sourcing",
            topic="candidate_match",
            objective="为当前岗位补充候选人",
        ),
        message=message,
        pending_plan_ref={},
    )
    assert understanding["speech_act"] == "inform"
    assert understanding["action"] == "none"
    assert understanding["action_evidence"] == []
    assert understanding["safe_for_action"] is False


def test_candidate_match_opinion_does_not_become_review_or_recommendation() -> None:
    message = "这个人选完美匹配士兰微这个岗位"
    for action in ("candidate_review", "recommendation"):
        understanding = enrich_turn_understanding(
            _understanding(
                speech_act="propose",
                action=action,
                topic="candidate_match",
                objective=message,
                target={"type": "candidate", "id": 30},
            ),
            message=message,
            pending_plan_ref={},
        )
        assert understanding["speech_act"] == "inform"
        assert understanding["action"] == "none"
        assert build_turn_decision(understanding, message=message)["safe_for_action"] is False


def test_context_state_keeps_fact_goal_correction_and_pending_plan_separate() -> None:
    initial = build_context_state(
        {},
        message="给长越机械岗位找10位候选人",
        context={"type": "job", "id": 10},
        business_focus={
            "context": {"type": "job", "id": 10},
            "client": "长越科技",
            "job": {"id": 10, "title": "机械高级工程师"},
            "candidate": {},
            "confidence": 1.0,
        },
        understanding=enrich_turn_understanding(
            _understanding(
                objective="给长越机械岗位找10位候选人",
                constraints=[{"quote": "10位候选人", "kind": "target_count"}],
            ),
            message="给长越机械岗位找10位候选人",
            pending_plan_ref={},
        ),
        decision={
            "effect": "create_plan",
            "safe_for_action": True,
            "constraint_changes": [{"operation": "add", "quote": "10位候选人", "kind": "target_count"}],
            "effective_constraints": [{"id": "target", "quote": "10位候选人", "kind": "target_count"}],
        },
        workflow_intent={
            "workflow_id": "workflow_1",
            "status": "planned",
            "version": 1,
            "plan_hash": "hash_1",
            "action": "candidate_sourcing",
            "objective": "给长越机械岗位找10位候选人",
        },
        now="2026-08-11 10:00:00",
    )
    updated = build_context_state(
        initial,
        message="这个岗位预算 120w",
        context={"type": "job", "id": 10},
        business_focus=initial["active_context"],
        understanding=enrich_turn_understanding(
            _understanding(
                speech_act="propose",
                action="salary",
                topic="salary",
                objective="整理谈薪材料",
            ),
            message="这个岗位预算 120w",
            pending_plan_ref={"workflow_id": "workflow_1", "version": 1, "plan_hash": "hash_1"},
        ),
        decision={
            "effect": "answer",
            "safe_for_action": False,
            "constraint_changes": [],
            "effective_constraints": initial["constraints"],
        },
        workflow_intent=None,
        now="2026-08-11 10:01:00",
    )

    assert updated["active_goal"]["action"] == "candidate_sourcing"
    assert updated["pending_plan"]["workflow_id"] == "workflow_1"
    assert updated["facts"][0]["kind"] == "job_budget"
    assert updated["facts"][0]["value"] == "120w"
    assert updated["last_turn"]["kind"] == "fact_update"
    assert updated["last_turn"]["requested_action"] == "none"
    assert updated["constraints"][0]["quote"] == "10位候选人"

    summary = deterministic_context_summary(updated)
    assert summary["stage"] == "candidate_sourcing:planned"
    assert "岗位预算：这个岗位预算 120w" in summary["key_facts"]
    assert summary["pending"] == ["待确认计划 workflow_1"]


def test_new_fact_of_same_kind_and_scope_supersedes_old_value() -> None:
    base_kwargs = {
        "context": {"type": "job", "id": 10},
        "business_focus": {
            "context": {"type": "job", "id": 10},
            "client": "长越科技",
            "job": {"id": 10, "title": "机械高级工程师"},
            "candidate": {},
            "confidence": 1.0,
        },
        "decision": {"effect": "answer", "safe_for_action": False, "constraint_changes": [], "effective_constraints": []},
        "workflow_intent": None,
    }
    first_message = "这个岗位预算 100w"
    first = build_context_state(
        {},
        message=first_message,
        understanding=enrich_turn_understanding(
            _understanding(speech_act="other", action="none", topic="salary"),
            message=first_message,
            pending_plan_ref={},
        ),
        now="2026-08-11 10:00:00",
        **base_kwargs,
    )
    second_message = "更正一下，这个岗位预算 120w"
    second = build_context_state(
        first,
        message=second_message,
        understanding=enrich_turn_understanding(
            _understanding(speech_act="correct", action="none", topic="salary"),
            message=second_message,
            pending_plan_ref={},
        ),
        now="2026-08-11 10:02:00",
        **base_kwargs,
    )

    assert len(second["facts"]) == 1
    assert second["facts"][0]["value"] == "120w"
    assert second["corrections"][-1]["previous_quote"] == first_message
    assert second["corrections"][-1]["quote"] == second_message


class AggressiveIntentLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__(fake_assessment(), chat_text="已理解。", intent_understanding=self._intent)

    @staticmethod
    def _intent(payload: dict) -> dict | None:
        message = str(payload.get("current_message") or "")
        action = "none"
        if any(token in message for token in ("预算", "期望薪资", "谈薪")):
            action = "salary"
        elif "完美匹配" in message:
            action = "recommendation"
        elif any(token in message for token in ("找到两个人选", "补充10位", "找10位")):
            action = "candidate_sourcing"
        if action == "none":
            return None
        return {
            "speech_act": "propose",
            "action": action,
            "topic": "salary" if action == "salary" else "candidate_match" if action == "recommendation" else "sourcing",
            "objective": message,
            "target": {"type": "job", "id": 10, "client": "长越科技", "label": "机械高级工程师"},
            "constraints": [],
            "fact_updates": [],
            "action_evidence": [message],
            "refers_to_previous": False,
            "confidence": 0.95,
            "needs_clarification": False,
            "missing_fields": [],
            "clarification_question": "",
        }


class CopilotContextReplayTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, AggressiveIntentLLM())

    def tearDown(self) -> None:
        self.service.close()
        super().tearDown()

    def _workflow_count(self) -> int:
        conn = self.service._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM agent_workflows").fetchone()[0])
        finally:
            conn.close()

    def test_realistic_followup_facts_do_not_replace_the_active_sourcing_goal(self) -> None:
        session_id = "replay_context_goal_separation"
        first = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert first["workflow_id"]
        initial_count = self._workflow_count()

        turns = [
            ("长越科技这个岗位预算 120w", "salary"),
            ("这轮只找到两个人选", "sourcing"),
            ("这个人选完美匹配长越科技这个岗位", "candidate_match"),
        ]
        for message, topic in turns:
            result = self.service.copilot(
                message,
                session_id=session_id,
                context={"type": "job", "id": 10, "page": "positions"},
            )
            assert result["workflow_id"] is None
            assert result["intent_understanding"]["action"] == "none"
            assert result["intent_understanding"]["topic"] == topic
            assert result["turn_decision"]["effect"] == "answer"

        assert self._workflow_count() == initial_count
        state = self.service.get_copilot_context_state(session_id)
        assert state["active_goal"]["action"] == "candidate_sourcing"
        assert state["pending_plan"]["workflow_id"] == first["workflow_id"]
        assert {item["kind"] for item in state["facts"]} >= {"job_budget", "workflow_observation"}

    def test_pure_candidate_result_observation_is_short_and_does_not_query_or_create(self) -> None:
        result = self.service.copilot(
            "这轮只找到两个人选",
            session_id="pure_candidate_result_observation",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert result["turn_decision"]["effect"] == "answer"
        assert result["model_participation"]["mode"] == "rules"
        assert "不会自动新建寻访任务" in result["answer"]
        assert "是哪两个人" not in result["answer"]

    def test_salary_question_is_read_only_but_explicit_salary_command_creates_plan(self) -> None:
        question = self.service.copilot(
            "这个人选要不要谈薪？",
            session_id="salary_question_read_only",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert question["workflow_id"] is None
        assert question["intent_understanding"]["speech_act"] == "ask"
        assert question["intent_understanding"]["action"] == "none"

        command = self.service.copilot(
            "帮我给这个人选整理谈薪方案",
            session_id="salary_command_creates_plan",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert command["workflow_id"]
        assert command["turn_decision"]["effect"] == "create_plan"
        assert command["turn_decision"]["authorization"]["evidence"] == ["帮我给这个人选整理谈薪方案"]

    def test_position_details_and_candidate_compensation_are_facts_not_salary_or_sourcing_plans(self) -> None:
        session_id = "detail_facts_only"
        first = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert first["workflow_id"]
        initial_count = self._workflow_count()

        details = self.service.copilot(
            "我只是补充岗位细节：杭州、汇报 CTO、5 年经验",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert details["workflow_id"] is None
        assert details["intent_understanding"]["action"] == "none"
        assert details["turn_decision"]["effect"] == "answer"
        assert self._workflow_count() == initial_count

        compensation = self.service.copilot(
            "这个人选目前 80w，期望 100w",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert compensation["workflow_id"] is None
        assert compensation["intent_understanding"]["action"] == "none"
        assert compensation["turn_decision"]["effect"] == "answer"
        assert self._workflow_count() == initial_count

        state = self.service.get_copilot_context_state(session_id)
        assert any(item["kind"] == "candidate_compensation" for item in state["facts"])

    def test_observation_plus_explicit_continuation_can_start_sourcing(self) -> None:
        result = self.service.copilot(
            "这轮只找到两个人选，继续寻访",
            session_id="observation_then_command",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert result["workflow_id"], result["answer"]
        assert result["intent_understanding"]["action"] == "candidate_sourcing"
        assert result["intent_understanding"]["action_evidence"]
        assert result["turn_decision"]["effect"] == "create_plan"

    def test_short_ack_without_pending_plan_does_not_start_anything(self) -> None:
        for message in ("可以", "继续"):
            result = self.service.copilot(
                message,
                session_id=f"no_pending_{message}",
                context={"type": "job", "id": 10, "page": "positions"},
            )
            assert result["workflow_id"] is None
            assert result["turn_decision"]["effect"] in {"answer", "clarify"}
            assert self._workflow_count() == 0

    def test_failed_workflow_is_removed_from_focus_before_short_ack(self) -> None:
        session_id = "failed_workflow_focus_recovery"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        workflow_id = created["workflow_id"]
        conn = self.service._connect()
        try:
            conn.execute("UPDATE agent_workflows SET status='failed' WHERE workflow_id=?", (workflow_id,))
            conn.execute(
                "UPDATE agent_goals SET status='failed' WHERE goal_id=(SELECT goal_id FROM agent_workflows WHERE workflow_id=?)",
                (workflow_id,),
            )
            conn.commit()
        finally:
            conn.close()

        focus = self.service.get_copilot_focus(session_id)
        assert focus["pending_workflow"] == {}
        assert focus["current_workflow"] == {}
        assert focus["action"] == ""

        followup = self.service.copilot(
            "继续",
            session_id=session_id,
            context={"type": "global"},
        )
        assert followup["workflow_id"] is None
        assert self._workflow_count() == 1

    def test_cancelled_workflow_cannot_keep_steering_the_restored_session(self) -> None:
        session_id = "cancelled_workflow_focus_recovery"
        created = self.service.copilot(
            "帮我给这个人选整理谈薪方案",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        self.service.cancel_workflow(created["workflow_id"], "测试取消")

        focus = self.service.get_copilot_focus(session_id)
        assert focus is not None
        assert focus["pending_workflow"] == {}
        assert focus["current_workflow"] == {}
        assert focus["action"] == ""
        assert focus["conversation_state"]["pending_plan"] == {}
        assert focus["conversation_state"]["active_goal"]["status"] == "cancelled"

    def test_summary_failure_falls_back_to_structured_context_state(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="已记录。"))
        session_id = "deterministic_summary_fallback"
        try:
            for budget in ("80w", "90w", "100w", "110w", "115w", "120w"):
                service.copilot(
                    f"这个岗位预算 {budget}",
                    session_id=session_id,
                    context={"type": "job", "id": 10, "page": "positions"},
                )
            conn = service._connect()
            try:
                row = conn.execute(
                    """SELECT summary_json FROM agent_copilot_summaries
                       WHERE session_id=? ORDER BY id DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
            finally:
                conn.close()
            summary = json.loads(row[0])
            assert summary["stage"] == "对话中"
            assert summary["entities"][-1]["id"] == 10
            assert summary["key_facts"] == ["岗位预算：这个岗位预算 120w"]
            context = service._copilot_conversation_context(
                session_id,
                service._copilot_conversation_history(session_id),
            )
            assert context["state"]["facts"][0]["value"] == "120w"
        finally:
            service.close()
