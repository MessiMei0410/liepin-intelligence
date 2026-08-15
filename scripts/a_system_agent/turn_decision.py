from __future__ import annotations

import hashlib
import re
from typing import Any

from .conversation_state import action_evidence_for_turn


ACTION_SPEECH_ACTS = {"propose", "confirm", "execute", "correct", "cancel"}
PLAN_EFFECTS = {"create_plan", "revise_plan", "start_plan", "cancel_plan"}
CONSTRAINT_OPERATIONS = {"add", "replace", "remove"}


def _clean(value: Any, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit]


def _constraint_id(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12]


def _constraint_item(value: Any, *, default_kind: str = "other") -> dict[str, str] | None:
    if isinstance(value, dict):
        quote = _clean(value.get("quote"))
        kind = _clean(value.get("kind") or default_kind, 32) or "other"
    else:
        quote = _clean(value)
        kind = default_kind
    if not quote:
        return None
    return {"id": _constraint_id(quote), "quote": quote, "kind": kind}


def normalize_constraints(values: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        item = _constraint_item(value)
        if item and item["quote"] not in seen:
            seen.add(item["quote"])
            rows.append(item)
    return rows[-24:]


def _best_existing_match(needle: str, existing: list[dict[str, str]]) -> dict[str, str] | None:
    normalized = re.sub(r"[\s，。；;、]", "", needle)
    if not normalized:
        return None
    direct = [
        item for item in existing
        if normalized in re.sub(r"[\s，。；;、]", "", item["quote"])
        or re.sub(r"[\s，。；;、]", "", item["quote"]) in normalized
    ]
    if len(direct) == 1:
        return direct[0]
    tokens = set(re.findall(r"[A-Za-z0-9.+-]+|[\u4e00-\u9fff]{2,}", needle))
    scored: list[tuple[int, dict[str, str]]] = []
    for item in existing:
        item_tokens = set(re.findall(r"[A-Za-z0-9.+-]+|[\u4e00-\u9fff]{2,}", item["quote"]))
        score = len(tokens & item_tokens)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def _explicit_constraint_changes(
    raw_changes: Any,
    *,
    message: str,
    existing: list[dict[str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for value in raw_changes if isinstance(raw_changes, list) else []:
        if not isinstance(value, dict):
            continue
        operation = _clean(value.get("operation") or value.get("op"), 16).lower()
        if operation not in CONSTRAINT_OPERATIONS:
            continue
        quote = _clean(value.get("quote") or value.get("value"))
        previous_quote = _clean(value.get("previous_quote") or value.get("from"))
        kind = _clean(value.get("kind"), 32) or "other"
        if operation in {"add", "replace"} and (not quote or quote not in message):
            continue
        if operation in {"remove", "replace"}:
            matched = _best_existing_match(previous_quote or quote, existing)
            if matched is None:
                continue
            previous_quote = matched["quote"]
            if kind == "other":
                kind = matched["kind"]
        row = {"operation": operation, "quote": quote, "kind": kind}
        if previous_quote:
            row["previous_quote"] = previous_quote
        rows.append(row)
    return rows


def derive_constraint_changes(
    message: str,
    extracted: Any,
    previous: Any,
    raw_changes: Any = None,
) -> list[dict[str, str]]:
    existing = normalize_constraints(previous)
    explicit = _explicit_constraint_changes(raw_changes, message=message, existing=existing)
    if explicit:
        return explicit

    replacement = re.search(
        r"(?:把)?\s*(?P<old>[^，。；;]{1,80}?)\s*(?:改成|改为|调整为|换成)\s*(?P<new>[^，。；;]{1,80})",
        message,
    )
    if replacement:
        matched = _best_existing_match(replacement.group("old"), existing)
        quote = _clean(replacement.group("new"))
        if matched and quote:
            return [{
                "operation": "replace",
                "previous_quote": matched["quote"],
                "quote": quote,
                "kind": matched["kind"],
            }]

    correction = re.search(
        r"(?:不是|不要)\s*(?P<old>[^，。；;]{1,80})[，,；;]\s*(?:是|要|改为)\s*(?P<new>[^，。；;]{1,80})",
        message,
    )
    if correction:
        matched = _best_existing_match(correction.group("old"), existing)
        quote = _clean(correction.group("new"))
        if matched and quote:
            return [{
                "operation": "replace",
                "previous_quote": matched["quote"],
                "quote": quote,
                "kind": matched["kind"],
            }]

    removal = re.search(
        r"(?:去掉|删除|移除|取消|不再要求|不再限制|不限制|不用卡)\s*(?P<old>[^，。；;]{1,100})",
        message,
    )
    if removal:
        matched = _best_existing_match(removal.group("old"), existing)
        if matched:
            return [{
                "operation": "remove",
                "previous_quote": matched["quote"],
                "quote": "",
                "kind": matched["kind"],
            }]

    current = [item for item in normalize_constraints(extracted) if item["quote"] in message]
    existing_quotes = {item["quote"] for item in existing}
    changes: list[dict[str, str]] = []
    for item in current:
        if item["quote"] in existing_quotes:
            continue
        if item["kind"] == "target_count":
            previous_target = next((old for old in existing if old["kind"] == "target_count"), None)
            if previous_target:
                changes.append({
                    "operation": "replace",
                    "previous_quote": previous_target["quote"],
                    "quote": item["quote"],
                    "kind": item["kind"],
                })
                continue
        changes.append({"operation": "add", "quote": item["quote"], "kind": item["kind"]})
    return changes


def apply_constraint_changes(previous: Any, changes: Any) -> list[dict[str, str]]:
    effective = normalize_constraints(previous)
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            continue
        operation = str(change.get("operation") or "")
        previous_quote = _clean(change.get("previous_quote"))
        quote = _clean(change.get("quote"))
        if operation in {"remove", "replace"} and previous_quote:
            effective = [item for item in effective if item["quote"] != previous_quote]
        if operation in {"add", "replace"} and quote and all(item["quote"] != quote for item in effective):
            item = _constraint_item({"quote": quote, "kind": change.get("kind") or "other"})
            if item:
                effective.append(item)
    return effective[-24:]


def build_turn_decision(
    understanding: dict[str, Any],
    *,
    message: str,
    previous_constraints: Any = None,
    pending_plan_ref: dict[str, Any] | None = None,
    raw_constraint_changes: Any = None,
) -> dict[str, Any]:
    speech_act = str(understanding.get("speech_act") or "other").lower()
    action = str(understanding.get("action") or "none").lower()
    needs_clarification = bool(understanding.get("needs_clarification"))
    confidence = float(understanding.get("confidence") or 0.0)
    pending_ref = dict(pending_plan_ref or {})
    changes = [] if speech_act in {"ask", "discuss", "other"} else derive_constraint_changes(
        message,
        understanding.get("constraints"),
        previous_constraints,
        raw_constraint_changes,
    )
    effective = apply_constraint_changes(previous_constraints, changes)
    action_evidence = [
        str(item).strip()
        for item in (understanding.get("action_evidence") or [])
        if str(item).strip() and str(item).strip() in message
    ]
    if not action_evidence:
        evidence_understanding = dict(understanding)
        evidence_understanding["constraint_changes"] = changes
        action_evidence = action_evidence_for_turn(
            evidence_understanding,
            message=message,
            pending_plan_ref=pending_ref,
        )

    effect = "answer"
    authorization = "none"
    if needs_clarification:
        effect = "clarify"
    elif speech_act in {"ask", "inform", "discuss", "other"}:
        effect = "answer"
    elif speech_act == "cancel":
        effect = "cancel_plan" if pending_ref.get("workflow_id") else "answer"
    elif action == "strategy_revision" and speech_act in {"propose", "execute", "correct"}:
        effect = "revise_plan" if pending_ref.get("workflow_id") and action_evidence else "clarify"
    elif speech_act == "correct":
        effect = "revise_plan" if pending_ref.get("workflow_id") and changes and action_evidence else "answer"
    elif speech_act == "confirm":
        if pending_ref.get("workflow_id"):
            effect = "start_plan"
            authorization = "confirm_exact_plan"
        else:
            effect = "clarify"
    elif speech_act == "execute":
        effect = "start_plan" if pending_ref.get("workflow_id") and understanding.get("refers_to_previous") else "create_plan"
        explicit_execute = bool(re.search(r"(?:立即|马上|现在|直接)?(?:开始|执行|启动|马上搜|直接搜)", message))
        authorization = "confirm_exact_plan" if effect == "start_plan" else "explicit_execute" if explicit_execute else "none"
    elif speech_act == "propose":
        effect = "create_plan" if action_evidence else "answer"

    safe_for_action = bool(
        effect in PLAN_EFFECTS
        and speech_act in ACTION_SPEECH_ACTS
        and action != "none"
        and confidence >= 0.72
        and not needs_clarification
        and bool(action_evidence)
        and (effect not in {"start_plan", "cancel_plan", "revise_plan"} or pending_ref.get("workflow_id"))
    )
    if not safe_for_action and effect in PLAN_EFFECTS:
        effect = "clarify" if speech_act in {"confirm", "execute"} else "answer"
        authorization = "none"

    observations = [
        {"type": "speech_act", "value": speech_act},
        {"type": "confidence", "value": round(confidence, 3)},
    ]
    if understanding.get("refers_to_previous"):
        observations.append({"type": "reference", "value": "previous_turn"})
    if action_evidence:
        observations.append({"type": "action_evidence", "value": action_evidence})
    blocked_reason = ""
    if (
        action != "none"
        and speech_act in ACTION_SPEECH_ACTS
        and not action_evidence
        and not needs_clarification
    ):
        blocked_reason = "missing_explicit_action_evidence"
    return {
        "version": "turn_decision_v2",
        "observations": observations,
        "goal": {
            "action": action,
            "objective": _clean(understanding.get("objective"), 500),
            "target": dict(understanding.get("target") or {}),
        },
        "constraint_changes": changes,
        "effective_constraints": effective,
        "effect": effect,
        "pending_plan_ref": pending_ref,
        "authorization": {
            "mode": authorization,
            "workflow_id": pending_ref.get("workflow_id"),
            "version": pending_ref.get("version"),
            "plan_hash": pending_ref.get("plan_hash"),
            "evidence": action_evidence,
        },
        "blocked_reason": blocked_reason,
        "safe_for_action": safe_for_action,
    }
