from __future__ import annotations

from typing import Any

from .policy import is_stopped


STATUS_VALUE = {"met": 1.0, "partial": 0.5, "not_met": 0.0, "unknown": None}
GROUP_WEIGHTS = {"hard_requirements": 60.0, "core_abilities": 25.0, "soft_preferences": 15.0}


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())
        if text and text not in result:
            result.append(text)
    return result[:12]


def _criterion_text(item: Any) -> str:
    return " ".join(str(item.get("criterion") if isinstance(item, dict) else item or "").split())


def _normalize_group(raw_items: Any, expected: list[str], *, critical: bool) -> list[dict[str, Any]]:
    supplied = raw_items if isinstance(raw_items, list) else []
    normalized: list[dict[str, Any]] = []
    used: set[int] = set()
    for criterion in expected:
        match_index = None
        for index, item in enumerate(supplied):
            text = _criterion_text(item)
            if index in used or not text:
                continue
            if text == criterion or text in criterion or criterion in text:
                match_index = index
                break
        item = supplied[match_index] if match_index is not None else {}
        if match_index is not None:
            used.add(match_index)
        status = str(item.get("status") or "unknown").strip().lower() if isinstance(item, dict) else "unknown"
        if status not in STATUS_VALUE:
            status = "unknown"
        evidence = _clean_list(item.get("evidence") if isinstance(item, dict) else [])
        if status != "unknown" and not evidence:
            status = "unknown"
        normalized.append(
            {
                "criterion": criterion,
                "status": status,
                "critical": bool(item.get("critical", critical)) if isinstance(item, dict) else critical,
                "evidence": evidence,
                "reason": " ".join(str(item.get("reason") or "").split()) if isinstance(item, dict) else "",
            }
        )
    for index, item in enumerate(supplied):
        if index in used or not isinstance(item, dict):
            continue
        criterion = _criterion_text(item)
        if not criterion:
            continue
        status = str(item.get("status") or "unknown").strip().lower()
        evidence = _clean_list(item.get("evidence"))
        if status not in STATUS_VALUE or (status != "unknown" and not evidence):
            status = "unknown"
        normalized.append(
            {
                "criterion": criterion,
                "status": status,
                "critical": bool(item.get("critical", critical)),
                "evidence": evidence,
                "reason": " ".join(str(item.get("reason") or "").split()),
            }
        )
    return normalized[:30]


def normalize_assessment(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    position = context.get("position", {})
    criteria_raw = raw.get("criteria") if isinstance(raw.get("criteria"), dict) else {}
    groups = {
        "hard_requirements": _normalize_group(
            criteria_raw.get("hard_requirements"),
            _clean_list(position.get("hard_requirements")),
            critical=True,
        ),
        "core_abilities": _normalize_group(
            criteria_raw.get("core_abilities"),
            _clean_list(position.get("ability_keywords")),
            critical=False,
        ),
        "soft_preferences": _normalize_group(
            criteria_raw.get("soft_preferences"),
            _clean_list(position.get("soft_preferences")),
            critical=False,
        ),
    }
    active_groups = {name: items for name, items in groups.items() if items}
    active_weight = sum(GROUP_WEIGHTS[name] for name in active_groups) or 1.0
    known_weight = 0.0
    earned_weight = 0.0
    total_weight = 0.0
    hard_unmet = False
    hard_unknown = False
    for name, items in active_groups.items():
        group_weight = GROUP_WEIGHTS[name] / active_weight * 100.0
        item_weight = group_weight / len(items)
        for item in items:
            total_weight += item_weight
            value = STATUS_VALUE[item["status"]]
            if value is not None:
                known_weight += item_weight
                earned_weight += item_weight * value
            if name == "hard_requirements" and item["critical"]:
                hard_unmet = hard_unmet or item["status"] == "not_met"
                hard_unknown = hard_unknown or item["status"] == "unknown"
    coverage = round(known_weight / total_weight, 4) if total_weight else 0.0
    score = round(earned_weight / known_weight * 100) if known_weight else 0
    if hard_unmet:
        score = min(score, 49)
    elif hard_unknown or coverage < 0.6:
        score = min(score, 69)
    raw_confidence = raw.get("confidence", 0.0)
    try:
        raw_confidence = max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        raw_confidence = 0.0
    confidence = round(min(raw_confidence, coverage), 4)
    if score >= 85:
        fit_level = "A-优先推进"
    elif score >= 70:
        fit_level = "B-可推进"
    elif score >= 55:
        fit_level = "C-需确认"
    else:
        fit_level = "D-暂缓"
    if hard_unmet:
        recommendation = "not_recommended"
    elif hard_unknown or coverage < 0.6:
        recommendation = "verify_first"
    elif score >= 85 and confidence >= 0.75:
        recommendation = "priority_review"
    elif score >= 70:
        recommendation = "verify_first"
    elif score >= 55:
        recommendation = "hold"
    else:
        recommendation = "not_recommended"
    stopped = is_stopped(context)
    next_action = " ".join(str(raw.get("next_action") or "").split())
    if stopped:
        recommendation = "hold"
        next_action = "当前关系已人工停止，仅保留历史判断，不自动重新推进。"
    risks = _clean_list(raw.get("risks"))
    if stopped and "当前关系已人工停止" not in risks:
        risks.insert(0, "当前关系已人工停止")
    result = {
        "fit_score": int(score),
        "fit_level": fit_level,
        "recommendation": recommendation,
        "confidence": confidence,
        "evidence_coverage": coverage,
        "criteria": groups,
        "strengths": _clean_list(raw.get("strengths")),
        "gaps": _clean_list(raw.get("gaps")),
        "risks": risks,
        "verification_questions": _clean_list(raw.get("verification_questions")),
        "next_action": next_action or "先补齐关键证据，再由人工决定是否推进。",
        "outreach_angle": " ".join(str(raw.get("outreach_angle") or "").split()),
        "citations": raw.get("citations") if isinstance(raw.get("citations"), list) else [],
        "needs_review": bool(
            hard_unmet
            or hard_unknown
            or coverage < 0.6
            or confidence < 0.7
            or raw.get("contradiction")
        ),
    }
    return result
