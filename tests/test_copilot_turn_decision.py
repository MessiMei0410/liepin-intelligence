from __future__ import annotations

from a_system_agent.turn_decision import build_turn_decision


def _understanding(**overrides):
    value = {
        "speech_act": "propose",
        "action": "candidate_sourcing",
        "objective": "为当前岗位补充候选人",
        "target": {"type": "job", "id": 10},
        "constraints": [],
        "refers_to_previous": False,
        "confidence": 0.96,
        "needs_clarification": False,
    }
    value.update(overrides)
    return value


def test_question_with_action_words_never_creates_a_plan() -> None:
    decision = build_turn_decision(
        _understanding(speech_act="ask"),
        message="这个岗位现在要不要继续寻访？",
    )
    assert decision["effect"] == "answer"
    assert decision["safe_for_action"] is False


def test_target_count_replaces_the_previous_count() -> None:
    decision = build_turn_decision(
        _understanding(
            speech_act="correct",
            constraints=[{"quote": "先要20人", "kind": "target_count"}],
        ),
        message="人数改一下，先要20人",
        previous_constraints=[{"quote": "先要10人", "kind": "target_count"}],
        pending_plan_ref={"workflow_id": "workflow_1", "version": 1, "plan_hash": "hash_1"},
    )
    assert decision["effect"] == "revise_plan"
    assert decision["constraint_changes"] == [{
        "operation": "replace",
        "previous_quote": "先要10人",
        "quote": "先要20人",
        "kind": "target_count",
    }]
    assert [item["quote"] for item in decision["effective_constraints"]] == ["先要20人"]


def test_constraint_can_be_removed() -> None:
    decision = build_turn_decision(
        _understanding(speech_act="correct"),
        message="去掉必须本科这个条件",
        previous_constraints=[{"quote": "必须本科", "kind": "must"}],
        pending_plan_ref={"workflow_id": "workflow_1", "version": 1, "plan_hash": "hash_1"},
    )
    assert decision["effect"] == "revise_plan"
    assert decision["constraint_changes"][0]["operation"] == "remove"
    assert decision["effective_constraints"] == []


def test_confirmation_is_bound_to_the_pending_plan_identity() -> None:
    decision = build_turn_decision(
        _understanding(speech_act="confirm", refers_to_previous=True),
        message="可以",
        previous_constraints=[],
        pending_plan_ref={"workflow_id": "workflow_1", "version": 3, "plan_hash": "hash_3"},
    )
    assert decision["effect"] == "start_plan"
    assert decision["authorization"] == {
        "mode": "confirm_exact_plan",
        "workflow_id": "workflow_1",
        "version": 3,
        "plan_hash": "hash_3",
    }


def test_cancel_without_a_pending_plan_has_no_business_effect() -> None:
    decision = build_turn_decision(
        _understanding(speech_act="cancel"),
        message="取消",
    )
    assert decision["effect"] == "answer"
    assert decision["safe_for_action"] is False
