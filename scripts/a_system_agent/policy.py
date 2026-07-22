from __future__ import annotations

from typing import Any


RISK_LEVELS = {
    "read": "R0",
    "assess": "R0",
    "explain": "R0",
    "save_assessment": "R1",
    "save_draft": "R1",
    "create_task": "R1",
    "complete_task": "R1",
    "capture_resume": "R1",
    "resume_review": "R2",
    "candidate_state_correction": "R2",
    "project_assignment": "R2",
    "learning_rule_activate": "R2",
    "candidate_merge": "R3",
    "outreach": "R3",
    "sourcing_run": "R3",
    "delete_history": "R4",
    "raw_sql": "R4",
}

AUTO_ALLOWED = {"R0", "R1"}


def latest_review_status(context: dict[str, Any]) -> str:
    for event in context.get("events", []):
        if event.get("event_type") == "resume_review_completed":
            return str(event.get("event_status") or "").strip().lower()
    return ""


def is_stopped(context: dict[str, Any]) -> bool:
    relation = context.get("relation", {})
    candidate = context.get("candidate", {})
    stage = str(relation.get("clean_stage") or "")
    status = str(candidate.get("status") or "").strip().lower()
    return (
        stage.startswith("H5 ")
        or latest_review_status(context) == "stop"
        or status in {"screen_rejected", "rejected", "client_rejected"}
    )


def action_decision(action_type: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    risk = RISK_LEVELS.get(action_type, "R4")
    decision = "allow" if risk in AUTO_ALLOWED else "confirm" if risk in {"R2", "R3"} else "deny"
    reason = "低风险内部动作"
    if risk == "R2":
        reason = "业务状态变更必须人工确认"
    elif risk == "R3":
        reason = "身份或对外动作必须强确认"
    elif risk == "R4":
        reason = "Agent 永久禁止该动作"
    if context and is_stopped(context) and action_type in {"resume_review", "outreach", "sourcing_run"}:
        decision = "deny"
        reason = "当前关系已人工停止，Agent 不得重新推进"
    return {"action_type": action_type, "risk_level": risk, "decision": decision, "reason": reason}
