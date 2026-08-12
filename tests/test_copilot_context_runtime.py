from __future__ import annotations

import json

from a_system_agent import AgentService, FakeLLM
from a_system_agent.copilot_handler import _copilot_pending_plan
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
    assert updated["pending_plan"]["stale_reason"] == "job_budget_updated"
    assert updated["pending_plan"]["last_referenced_at"] == "2026-08-11 10:00:00"
    assert updated["pending_plan"]["state_revision"] == updated["revision"]
    assert updated["facts"][0]["kind"] == "job_budget"
    assert updated["facts"][0]["value"] == "120w"
    assert updated["last_turn"]["kind"] == "fact_update"
    assert updated["last_turn"]["requested_action"] == "none"
    assert updated["constraints"][0]["quote"] == "10位候选人"

    summary = deterministic_context_summary(updated)
    assert summary["stage"] == "candidate_sourcing:planned"
    assert "岗位预算：这个岗位预算 120w" in summary["key_facts"]
    assert summary["pending"] == [
        "待确认计划 workflow_1（已过期：岗位预算更新，计划基于 2026-08-11 10:00:00 的信息）"
    ]


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


class CountingIntentLLM(AggressiveIntentLLM):
    def __init__(self) -> None:
        super().__init__()
        self.intent_calls = 0

    def interpret_copilot_intent(self, payload: dict) -> dict | None:
        self.intent_calls += 1
        raise AssertionError("deterministic factual turns should not call the intent model")


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
        # 谈薪高风险动作：先出复述确认卡，不直接创建计划。
        assert command["workflow_id"] is None
        assert "确认后我将创建谈薪计划" in command["answer"]

        confirmed = self.service.copilot(
            "确认创建",
            session_id="salary_command_creates_plan",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert confirmed["workflow_id"]
        assert confirmed["turn_decision"]["effect"] == "create_plan"
        assert confirmed["turn_decision"]["authorization"]["evidence"] == ["确认创建"]
        assert confirmed["workflow"]["status"] == "planned"

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

    def test_job_budget_in_candidate_context_is_scoped_to_the_linked_job(self) -> None:
        initial_count = self._workflow_count()

        result = self.service.copilot(
            "长越科技这个岗位预算 120w",
            session_id="candidate_context_job_budget",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert result["turn_decision"]["effect"] == "answer"
        assert result["context"] == {"type": "candidate", "id": 30}
        assert "岗位预算补充" in result["answer"]
        assert "不创建谈薪任务" in result["answer"]
        assert self._workflow_count() == initial_count

        state = self.service.get_copilot_context_state("candidate_context_job_budget")
        budget_facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(budget_facts) == 1
        assert budget_facts[0]["value"] == "120w"
        assert budget_facts[0]["scope"] == {"type": "job", "id": 10}

    def test_short_budget_is_answered_and_scoped_as_a_job_fact(self) -> None:
        result = self.service.copilot(
            "预算 120w",
            session_id="short_budget_fact",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert "岗位预算补充" in result["answer"]
        assert "不创建谈薪任务" in result["answer"]
        state = self.service.get_copilot_context_state("short_budget_fact")
        facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert facts == [{
            "id": facts[0]["id"],
            "kind": "job_budget",
            "quote": "预算 120w",
            "value": "120w",
            "scope": {"type": "job", "id": 10},
            "source": "user",
            "at": facts[0]["at"],
        }]

    def test_candidate_budget_fact_is_not_misfiled_as_the_linked_job_budget(self) -> None:
        initial_count = self._workflow_count()

        result = self.service.copilot(
            "这个人选的预算是 100w，期望 110w",
            session_id="candidate_budget_fact",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert result["turn_decision"]["effect"] == "answer"
        assert "张航的薪资事实" in result["answer"]
        assert "岗位预算补充" not in result["answer"]
        assert self._workflow_count() == initial_count

        state = self.service.get_copilot_context_state("candidate_budget_fact")
        compensation = [item for item in state["facts"] if item["kind"] == "candidate_compensation"]
        assert len(compensation) == 1
        assert compensation[0]["scope"] == {"type": "candidate", "id": 30}
        assert not any(item["kind"] == "job_budget" for item in state["facts"])

    def test_candidate_total_package_is_compensation_not_job_budget(self) -> None:
        result = self.service.copilot(
            "候选人当前总包 90w，预期 105w",
            session_id="candidate_total_package_fact",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert "薪资事实" in result["answer"]
        state = self.service.get_copilot_context_state("candidate_total_package_fact")
        facts = [item for item in state["facts"] if item["kind"] == "candidate_compensation"]
        assert len(facts) == 1
        assert facts[0]["scope"] == {"type": "candidate", "id": 30}
        assert not any(item["kind"] == "job_budget" for item in state["facts"])

    def test_mixed_job_budget_and_candidate_amount_uses_candidate_scope(self) -> None:
        result = self.service.copilot(
            "岗位预算给候选人按 100w",
            session_id="mixed_job_candidate_budget_fact",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert "薪资事实" in result["answer"]
        assert "岗位预算补充" not in result["answer"]
        state = self.service.get_copilot_context_state("mixed_job_candidate_budget_fact")
        assert [item["kind"] for item in state["facts"]] == ["candidate_compensation"]
        assert state["facts"][0]["scope"] == {"type": "candidate", "id": 30}

    def test_budget_range_without_unit_on_the_first_number_is_preserved(self) -> None:
        result = self.service.copilot(
            "预算 80-120w",
            session_id="budget_range_fact",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        state = self.service.get_copilot_context_state("budget_range_fact")
        facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(facts) == 1
        assert facts[0]["value"] == "80-120w"

    def test_compact_job_details_in_bound_context_are_recorded(self) -> None:
        result = self.service.copilot(
            "杭州、汇报 CTO、5 年经验",
            session_id="compact_job_details",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert "岗位细节补充" in result["answer"]
        state = self.service.get_copilot_context_state("compact_job_details")
        facts = [item for item in state["facts"] if item["kind"] == "job_requirement"]
        assert len(facts) == 1
        assert facts[0]["scope"] == {"type": "job", "id": 10}
        assert facts[0]["quote"] == "杭州、汇报 CTO、5 年经验"

    def test_candidate_match_opinion_gets_a_meaningful_non_action_answer(self) -> None:
        result = self.service.copilot(
            "这个人选完美匹配长越科技这个岗位",
            session_id="candidate_match_opinion_answer",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert "匹配度的判断" in result["answer"]
        assert "不自动复核或生成推荐材料" in result["answer"]

    def test_two_explicit_jobs_are_ambiguous_and_do_not_receive_a_fact(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES (11,1,'软件高级工程师','杭州','已发布','','','','','控制软件核心岗','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "长越科技的机械岗位和软件岗位预算 120w",
            session_id="two_explicit_jobs",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert "不能唯一确定" in result["answer"]
        state = self.service.get_copilot_context_state("two_explicit_jobs")
        assert not any(item["kind"] == "job_budget" for item in state["facts"])

    def test_explicit_cross_client_job_overrides_stale_page_focus(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute("INSERT INTO clients VALUES (2,'士兰微')")
            conn.execute(
                "INSERT INTO jobs VALUES (20,2,'电源专家','杭州','已发布','','','','','服务器电源专家','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "士兰微的电源专家岗位预算 120w",
            session_id="cross_client_job_reference",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert result["context"] == {"type": "job", "id": 20}
        state = self.service.get_copilot_context_state("cross_client_job_reference")
        facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(facts) == 1
        assert facts[0]["scope"] == {"type": "job", "id": 20}

    def test_fact_between_plan_and_short_ack_does_not_start_the_old_plan(self) -> None:
        session_id = "fact_breaks_confirmation_chain"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        workflow_id = created["workflow_id"]
        fact = self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert fact["workflow_id"] is None

        followup = self.service.copilot(
            "可以",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert followup["workflow_id"] is None
        assert "没有启动任何任务" in followup["answer"]
        state = self.service.get_workflow(workflow_id)
        assert state["workflow"]["status"] == "planned"

    def test_immediate_plan_confirmation_starts_only_the_presented_target(self) -> None:
        session_id = "immediate_plan_confirmation"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        followup = self.service.copilot(
            "继续",
            session_id=session_id,
            context={"type": "global"},
        )

        assert followup["workflow_id"] == created["workflow_id"]
        assert followup["workflow"]["status"] in {"queued", "running", "blocked", "completed"}

    def test_short_confirmation_from_another_job_cannot_start_old_plan(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES (11,1,'软件高级工程师','杭州','已发布','','','','','控制软件核心岗','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()
        session_id = "cross_job_short_confirmation"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        followup = self.service.copilot(
            "可以",
            session_id=session_id,
            context={"type": "job", "id": 11, "page": "positions"},
        )

        assert followup["workflow_id"] is None
        assert "没有启动任何任务" in followup["answer"]
        assert self.service.get_workflow(created["workflow_id"])["workflow"]["status"] == "planned"

    def test_selected_workflow_takes_precedence_over_stale_focus_pending_plan(self) -> None:
        first = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id="stale_focus_plan",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        second = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id="selected_workflow_plan",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        focus = self.service.get_copilot_focus("stale_focus_plan")
        ref, state = _copilot_pending_plan(
            self.service,
            {"type": "workflow", "id": second["workflow_id"]},
            focus,
        )

        assert ref["workflow_id"] == second["workflow_id"]
        assert state["workflow"]["workflow_id"] == second["workflow_id"]
        assert ref["workflow_id"] != first["workflow_id"]

    def test_selected_job_filters_same_client_reference_candidates(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES (11,1,'软件高级工程师','杭州','已发布','','','','','控制软件核心岗','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "长越科技这个岗位预算 120w",
            session_id="same_client_selected_job",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        job_references = [item for item in result["references"] if item.get("type") == "job"]
        assert [item["id"] for item in job_references] == [10]
        assert result["context"] == {"type": "job", "id": 10}

    def test_explicit_job_reference_overrides_stale_page_focus(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES (11,1,'软件高级工程师','杭州','已发布','','','','','控制软件核心岗','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "岗位 11 的候选名单给我",
            session_id="explicit_job_overrides_focus",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["context"] == {"type": "job", "id": 11}
        assert "软件高级工程师" in result["answer"]
        assert all(item.get("id") != 10 for item in result["references"] if item.get("type") == "job")

    def test_ambiguous_client_only_list_request_does_not_pick_first_job(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute(
                "INSERT INTO jobs VALUES (11,1,'软件高级工程师','杭州','已发布','','','','','控制软件核心岗','2026-08-11')"
            )
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "长越科技的候选名单给我",
            session_id="ambiguous_client_only_list",
            context={"type": "page", "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert "不能唯一确定" in result["answer"]
        assert "岗位 10" in result["answer"]
        assert "岗位 11" in result["answer"]

    def test_factual_turns_skip_intent_model_when_context_is_unique(self) -> None:
        llm = CountingIntentLLM()
        service = AgentService(self.db_path, llm)
        try:
            result = service.copilot(
                "长越科技这个岗位预算 120w",
                session_id="deterministic_fact_shortcut",
                context={"type": "job", "id": 10, "page": "positions"},
            )
            assert result["workflow_id"] is None
            assert result["model_participation"]["mode"] == "rules"
            assert llm.intent_calls == 0
        finally:
            service.close()

    def test_stopped_candidate_can_receive_compensation_facts_without_resume_block(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute("UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过' WHERE id=30")
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "这个人选目前 80w，期望 100w",
            session_id="stopped_candidate_compensation_fact",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert result["intent_understanding"]["action"] == "none"
        assert "薪资事实" in result["answer"]
        assert "不能继续推进" not in result["answer"]
        state = self.service.get_copilot_context_state("stopped_candidate_compensation_fact")
        assert any(item["kind"] == "candidate_compensation" for item in state["facts"])

    def test_stopped_candidate_explicit_resume_request_is_still_blocked(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute("UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过' WHERE id=30")
            conn.commit()
        finally:
            conn.close()

        result = self.service.copilot(
            "继续推进这个人选",
            session_id="stopped_candidate_resume_block",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result.get("workflow_id") is None
        assert "不能继续推进" in result["answer"]

    def test_stopped_candidate_salary_command_does_not_create_workflow(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute("UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过' WHERE id=30")
            conn.commit()
        finally:
            conn.close()
        initial_count = self._workflow_count()

        result = self.service.copilot(
            "帮我给这个人选整理谈薪方案",
            session_id="stopped_candidate_salary_block",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result.get("workflow_id") is None
        assert self._workflow_count() == initial_count
        assert "不能继续推进" in result["answer"]

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
        recap = self.service.copilot(
            "帮我给这个人选整理谈薪方案",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert recap["workflow_id"] is None
        created = self.service.copilot(
            "确认创建",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert created["workflow_id"]
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

    def test_fact_turn_marks_pending_plan_stale_and_short_confirm_is_blocked(self) -> None:
        session_id = "stale_plan_short_confirm_blocked"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        workflow_id = created["workflow_id"]

        conn = self.service._connect()
        try:
            row = conn.execute(
                """SELECT structured_json FROM agent_copilot_messages
                   WHERE session_id=? AND role='assistant' ORDER BY id ASC LIMIT 1""",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        presented_ref = json.loads(row[0])["presented_plan_ref"]
        assert presented_ref["workflow_id"] == workflow_id
        assert presented_ref["state_revision"] >= 1

        fact = self.service.copilot(
            "长越科技这个岗位预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert fact["workflow_id"] is None
        fact_receipt = fact["fact_receipt"]
        assert fact_receipt["kind"] == "job_budget"
        assert fact_receipt["value"] == "120w"
        assert fact_receipt["scope"] == "job:10"
        assert "待确认计划" in fact_receipt["impact"]
        assert "重新确认" in fact_receipt["impact"]

        state = self.service.get_copilot_context_state(session_id)
        pending_plan = state["pending_plan"]
        assert pending_plan["workflow_id"] == workflow_id
        assert pending_plan["stale_reason"] == "job_budget_updated"
        assert pending_plan["last_referenced_at"]
        assert pending_plan["state_revision"] == state["revision"]

        followup = self.service.copilot(
            "继续",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert followup["workflow_id"] is None
        assert "旧信息" in followup["answer"]
        assert "岗位预算更新" in followup["answer"]
        assert "没有启动任何任务" in followup["answer"]
        assert self.service.get_workflow(workflow_id)["workflow"]["status"] == "planned"

        state_after = self.service.get_copilot_context_state(session_id)
        assert state_after["pending_plan"]["stale_reason"] == "job_budget_updated"

    def test_fact_receipt_is_structured_and_persisted(self) -> None:
        session_id = "fact_receipt_structured"
        result = self.service.copilot(
            "长越科技这个岗位预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        receipt = result["fact_receipt"]
        assert receipt == {
            "object": receipt["object"],
            "kind": "job_budget",
            "quote": "长越科技这个岗位预算 120w",
            "value": "120w",
            "scope": "job:10",
            "impact": "仅更新上下文，不启动工作流",
        }
        assert "岗位预算补充" in result["answer"]
        assert "不创建谈薪任务" in result["answer"]

        conn = self.service._connect()
        try:
            row = conn.execute(
                """SELECT structured_json FROM agent_copilot_messages
                   WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        structured = json.loads(row[0])
        assert structured["fact_receipt"] == receipt

    def test_candidate_compensation_fact_receipt_uses_candidate_scope(self) -> None:
        result = self.service.copilot(
            "这个人选目前 80w，期望 100w",
            session_id="fact_receipt_candidate_compensation",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        receipt = result["fact_receipt"]
        assert receipt["kind"] == "candidate_compensation"
        assert receipt["object"] == "张航"
        assert receipt["scope"] == "candidate:30"
        assert receipt["impact"] == "仅更新上下文，不启动工作流"
        assert "张航的薪资事实" in result["answer"]

    def test_non_fact_turn_has_no_fact_receipt(self) -> None:
        result = self.service.copilot(
            "这个人选要不要谈薪？",
            session_id="no_fact_receipt_on_question",
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert result["workflow_id"] is None
        assert result["fact_receipt"] is None

    def _turn_decision_events(self, session_id: str) -> list[dict]:
        conn = self.service._connect()
        try:
            rows = conn.execute(
                "SELECT payload_json FROM agent_copilot_events WHERE session_id=? AND event='turn_decision' ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) for row in rows]

    def test_fact_retract_marks_fact_and_leaves_correction_trail(self) -> None:
        session_id = "fact_retract_receipt"
        initial_count = self._workflow_count()
        fact = self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert fact["workflow_id"] is None

        retract = self.service.copilot(
            "刚才那条不要记",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert retract["workflow_id"] is None
        assert retract["turn_decision"]["effect"] == "answer"
        assert "已撤销刚才记录的岗位预算事实" in retract["answer"]
        assert "「预算 120w」" in retract["answer"]
        # 纠错/撤销输入绝不能触发新任务创建。
        assert self._workflow_count() == initial_count
        state = self.service.get_copilot_context_state(session_id)
        budget_facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(budget_facts) == 1
        assert budget_facts[0]["retracted"] is True
        retract_corrections = [item for item in state["corrections"] if item["kind"] == "fact_retract"]
        assert len(retract_corrections) == 1
        assert retract_corrections[0]["previous_quote"] == "预算 120w"
        summary = deterministic_context_summary(state)
        assert not any("120w" in item for item in summary["key_facts"])

    def test_fact_retract_without_facts_is_refused(self) -> None:
        initial_count = self._workflow_count()
        result = self.service.copilot(
            "刚才那条不要记",
            session_id="fact_retract_empty",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result["workflow_id"] is None
        assert "最近没有已记录的事实可撤销" in result["answer"]
        assert self._workflow_count() == initial_count

    def test_scope_correction_migrates_candidate_fact_to_the_linked_job(self) -> None:
        session_id = "scope_correction_candidate_to_job"
        initial_count = self._workflow_count()
        fact = self.service.copilot(
            "这个人选目前 80w，期望 100w",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert fact["workflow_id"] is None
        state = self.service.get_copilot_context_state(session_id)
        comp = [item for item in state["facts"] if item["kind"] == "candidate_compensation"]
        assert len(comp) == 1
        assert comp[0]["scope"] == {"type": "candidate", "id": 30}

        corrected = self.service.copilot(
            "不是这个人，是给岗位记的",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert corrected["workflow_id"] is None
        assert corrected["turn_decision"]["effect"] == "answer"
        assert "迁移到岗位" in corrected["answer"]
        assert self._workflow_count() == initial_count
        state = self.service.get_copilot_context_state(session_id)
        comp = [item for item in state["facts"] if item["kind"] == "candidate_compensation"]
        assert len(comp) == 1
        assert comp[0]["scope"] == {"type": "job", "id": 10}
        scope_corrections = [item for item in state["corrections"] if item["kind"] == "fact_scope"]
        assert len(scope_corrections) == 1
        assert scope_corrections[0]["previous_scope"] == {"type": "candidate", "id": 30}
        assert scope_corrections[0]["new_scope"] == {"type": "job", "id": 10}

    def test_scope_correction_to_a_named_job(self) -> None:
        conn = self.service._connect()
        try:
            conn.execute("INSERT INTO clients VALUES (2,'士兰微')")
            conn.execute(
                "INSERT INTO jobs VALUES (11,2,'模拟IC设计工程师','杭州','已发布','','','','','模拟芯片核心岗','2026-08-12')"
            )
            conn.commit()
        finally:
            conn.close()
        session_id = "scope_correction_named_job"
        self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        corrected = self.service.copilot(
            "不是这个岗位，是士兰微那个",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert corrected["workflow_id"] is None
        assert "士兰微" in corrected["answer"]
        state = self.service.get_copilot_context_state(session_id)
        budget_facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(budget_facts) == 1
        assert budget_facts[0]["scope"] == {"type": "job", "id": 11}
        scope_corrections = [item for item in state["corrections"] if item["kind"] == "fact_scope"]
        assert len(scope_corrections) == 1
        assert scope_corrections[0]["previous_scope"] == {"type": "job", "id": 10}
        assert scope_corrections[0]["new_scope"] == {"type": "job", "id": 11}

    def test_scope_correction_without_resolvable_target_asks_for_clarification(self) -> None:
        session_id = "scope_correction_unresolved"
        initial_count = self._workflow_count()
        self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        corrected = self.service.copilot(
            "不是这个岗位，是给人选记的",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert corrected["workflow_id"] is None
        assert corrected["turn_decision"]["effect"] == "clarify"
        assert "暂未迁移" in corrected["answer"]
        assert self._workflow_count() == initial_count
        state = self.service.get_copilot_context_state(session_id)
        budget_facts = [item for item in state["facts"] if item["kind"] == "job_budget"]
        assert len(budget_facts) == 1
        assert budget_facts[0]["scope"] == {"type": "job", "id": 10}
        assert not any(item["kind"] == "fact_scope" for item in state["corrections"])
        assert any("岗位或候选人" in str(item.get("question") or "") for item in state["open_questions"])

    def test_undo_task_cancels_fresh_planned_workflow_and_keeps_facts(self) -> None:
        session_id = "undo_planned_task"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        workflow_id = created["workflow_id"]
        fact = self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert fact["workflow_id"] is None
        initial_count = self._workflow_count()

        undo = self.service.copilot(
            "撤销刚才创建的任务",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert undo["workflow_id"] == workflow_id
        assert "已取消刚才创建的" in undo["answer"]
        assert "已记录的岗位/候选人事实保留" in undo["answer"]
        assert self.service.get_workflow(workflow_id)["workflow"]["status"] == "cancelled"
        # 撤销不新建任务，已记录事实保留。
        assert self._workflow_count() == initial_count
        state = self.service.get_copilot_context_state(session_id)
        assert state["pending_plan"] == {}
        assert any(
            item["kind"] == "job_budget" and not item.get("retracted")
            for item in state["facts"]
        )

    def test_undo_task_refuses_when_workflow_already_started(self) -> None:
        session_id = "undo_started_task"
        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        started = self.service.copilot(
            "继续",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert started["workflow_id"] == created["workflow_id"]
        assert started["workflow"]["status"] in {"queued", "running", "blocked", "completed"}

        undo = self.service.copilot(
            "撤销刚才创建的任务",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert undo["workflow_id"] is None
        assert "已开始执行，不能直接撤销" in undo["answer"]
        assert "停止流程" in undo["answer"]
        assert self.service.get_workflow(created["workflow_id"])["workflow"]["status"] != "cancelled"

    def test_salary_precreate_recap_then_confirm_creates_planned_plan(self) -> None:
        session_id = "salary_precreate_flow"
        initial_count = self._workflow_count()
        fact = self.service.copilot(
            "这个人选目前 80w，期望 100w",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )
        assert fact["workflow_id"] is None

        recap = self.service.copilot(
            "帮我给这个人选整理谈薪方案",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        # 复述卡阶段：不创建计划，workflow_created=false。
        assert recap["workflow_id"] is None
        assert "我理解你要对「张航」发起谈薪" in recap["answer"]
        assert "当前薪资 80w" in recap["answer"]
        assert "期望 100w" in recap["answer"]
        assert "确认后我将创建谈薪计划" in recap["answer"]
        assert self._workflow_count() == initial_count
        recap_events = self._turn_decision_events(session_id)
        assert recap_events[-1]["workflow_created"] is False
        assert recap_events[-1]["confirmation_mode"] == "salary_precreate"

        confirmed = self.service.copilot(
            "确认创建",
            session_id=session_id,
            context={"type": "candidate", "id": 30, "page": "candidates"},
        )

        assert confirmed["workflow_id"]
        assert confirmed["workflow"]["status"] == "planned"
        assert confirmed["turn_decision"]["effect"] == "create_plan"
        confirm_events = self._turn_decision_events(session_id)
        assert confirm_events[-1]["workflow_created"] is True
        assert confirm_events[-1]["confirmation_mode"] == "salary_confirmed"
        assert confirm_events[-1]["action"] == "salary"

    def test_turn_decision_audit_event_is_recorded_for_fact_and_plan_turns(self) -> None:
        session_id = "turn_decision_audit"
        fact = self.service.copilot(
            "预算 120w",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert fact["workflow_id"] is None

        created = self.service.copilot(
            "给长越科技机械高级工程师补充10位候选人",
            session_id=session_id,
            context={"type": "job", "id": 10, "page": "positions"},
        )
        assert created["workflow_id"]

        events = self._turn_decision_events(session_id)
        assert len(events) == 2
        fact_event, plan_event = events
        assert fact_event["intent"] == "inform"
        assert fact_event["action"] == "none"
        assert fact_event["effect"] == "answer"
        assert fact_event["workflow_created"] is False
        assert fact_event["fact_receipt"]["kind"] == "job_budget"
        assert plan_event["intent"] in {"propose", "execute"}
        assert plan_event["action"] == "candidate_sourcing"
        assert plan_event["effect"] == "create_plan"
        assert plan_event["workflow_created"] is True
        assert isinstance(plan_event["target"], dict)
        assert isinstance(plan_event["evidence"], list)
