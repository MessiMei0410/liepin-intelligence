from __future__ import annotations

from typing import Any


ROLE_DEFINITIONS = {
    "evidence_auditor": {
        "label": "证据审校",
        "mission": "只检查结论是否被履历、岗位门槛和引用证据支持，不讨论业务推进。",
    },
    "risk_challenger": {
        "label": "风险挑战",
        "mission": "主动寻找硬门槛失败、证据矛盾、人工停止和高代价误判风险。",
    },
    "process_advisor": {
        "label": "流程顾问",
        "mission": "只提出人工下一步核验建议，不执行触达、推进、停止或状态修改。",
    },
}

VERDICTS = {"support", "verify", "block"}


def role_payload(
    context: dict[str, Any], assessment: dict[str, Any], role: str
) -> dict[str, Any]:
    model_context = context.get("model_context") or {}
    base = {
        "role": role,
        "mission": ROLE_DEFINITIONS[role]["mission"],
        "position": model_context.get("position") or {},
    }
    if role == "evidence_auditor":
        base.update(
            {
                "identity": model_context.get("identity") or {},
                "candidate_profile": model_context.get("candidate_profile") or {},
                "criteria": assessment.get("criteria") or {},
                "citations": assessment.get("citations") or [],
                "gaps": assessment.get("gaps") or [],
                "evidence_coverage": assessment.get("evidence_coverage") or 0,
            }
        )
    elif role == "risk_challenger":
        base.update(
            {
                "criteria": assessment.get("criteria") or {},
                "risks": assessment.get("risks") or [],
                "gaps": assessment.get("gaps") or [],
                "events": (model_context.get("events") or [])[:16],
                "current_recommendation": assessment.get("recommendation") or "",
            }
        )
    else:
        base.update(
            {
                "events": (model_context.get("events") or [])[:12],
                "current_recommendation": assessment.get("recommendation") or "",
                "next_action": assessment.get("next_action") or "",
                "verification_questions": assessment.get("verification_questions") or [],
                "policy": assessment.get("policy") or {},
            }
        )
    return base


def normalize_role_review(raw: dict[str, Any], role: str) -> dict[str, Any]:
    verdict = str(raw.get("verdict") or "verify").strip().lower()
    if verdict not in VERDICTS:
        verdict = "verify"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    findings = [
        " ".join(str(item or "").split())
        for item in raw.get("findings") or []
        if " ".join(str(item or "").split())
    ][:8]
    questions = [
        " ".join(str(item or "").split())
        for item in raw.get("questions") or []
        if " ".join(str(item or "").split())
    ][:8]
    return {
        "role": role,
        "role_label": ROLE_DEFINITIONS[role]["label"],
        "verdict": verdict,
        "confidence": confidence,
        "findings": list(dict.fromkeys(findings)),
        "questions": list(dict.fromkeys(questions)),
        "recommendation": " ".join(str(raw.get("recommendation") or "").split()),
    }


def fallback_role_review(
    context: dict[str, Any], assessment: dict[str, Any], role: str
) -> dict[str, Any]:
    criteria = assessment.get("criteria") or {}
    hard = criteria.get("hard_requirements") or []
    hard_not_met = [item for item in hard if item.get("status") == "not_met"]
    hard_unknown = [item for item in hard if item.get("status") in {"unknown", "partial"}]
    coverage = float(assessment.get("evidence_coverage") or 0)
    risks = list(assessment.get("risks") or [])
    gaps = list(assessment.get("gaps") or [])
    questions = list(assessment.get("verification_questions") or [])
    stopped = bool((assessment.get("policy") or {}).get("stopped"))

    if role == "evidence_auditor":
        verdict = "block" if hard_not_met else "verify" if hard_unknown or coverage < 0.72 else "support"
        findings = []
        if hard_not_met:
            findings.append(f"{len(hard_not_met)} 项硬门槛有明确不满足证据")
        if hard_unknown:
            findings.append(f"{len(hard_unknown)} 项硬门槛仍需核验")
        findings.extend(gaps[:4])
        if not findings:
            findings.append("当前引用证据能够覆盖主要判断")
        confidence = max(0.35, min(0.95, coverage))
        recommendation = "补齐硬门槛证据后再确认" if verdict == "verify" else assessment.get("next_action") or "人工确认判断"
    elif role == "risk_challenger":
        verdict = "block" if stopped or hard_not_met else "verify" if risks or hard_unknown else "support"
        findings = (["当前关系存在人工停止，不得重新推进"] if stopped else []) + risks[:6]
        if not findings:
            findings.append("未发现足以阻断当前判断的结构化风险")
        confidence = 0.9 if stopped or hard_not_met else 0.72 if risks else 0.65
        recommendation = "保持人工停止" if stopped else "逐项核验高代价风险"
    else:
        recommendation_code = str(assessment.get("recommendation") or "verify_first")
        verdict = "block" if stopped or recommendation_code == "not_recommended" else "verify" if recommendation_code in {"verify_first", "hold"} else "support"
        findings = [assessment.get("next_action") or "人工复核后决定"]
        confidence = float(assessment.get("confidence") or 0.5)
        recommendation = assessment.get("next_action") or "完成人工核验后决定"
    return normalize_role_review(
        {
            "verdict": verdict,
            "confidence": confidence,
            "findings": findings,
            "questions": questions[:6],
            "recommendation": recommendation,
        },
        role,
    )


def synthesize_panel(
    reviews: list[dict[str, Any]], assessment: dict[str, Any], *, stopped: bool
) -> dict[str, Any]:
    verdicts = [str(item.get("verdict") or "verify") for item in reviews]
    support_count = verdicts.count("support")
    verify_count = verdicts.count("verify")
    block_count = verdicts.count("block")
    if stopped:
        recommendation = "hold"
        consensus = "保持人工停止"
    elif block_count >= 2:
        recommendation = "not_recommended"
        consensus = "多数角色建议不推进"
    elif block_count or verify_count or len(set(verdicts)) > 1:
        recommendation = "verify_first"
        consensus = "存在分歧，先核验"
    else:
        current = str(assessment.get("recommendation") or "priority_review")
        recommendation = current if current in {"priority_review", "verify_first"} else "priority_review"
        consensus = "角色共识支持当前判断"
    confidences = [float(item.get("confidence") or 0) for item in reviews]
    findings: list[str] = []
    questions: list[str] = []
    for item in reviews:
        findings.extend(item.get("findings") or [])
        questions.extend(item.get("questions") or [])
    process_review = next((item for item in reviews if item.get("role") == "process_advisor"), {})
    return {
        "consensus": consensus,
        "recommendation": recommendation,
        "confidence": round(sum(confidences) / max(1, len(confidences)), 4),
        "disagreement": len(set(verdicts)) > 1,
        "votes": {"support": support_count, "verify": verify_count, "block": block_count},
        "findings": list(dict.fromkeys(findings))[:12],
        "questions": list(dict.fromkeys(questions))[:12],
        "next_action": process_review.get("recommendation") or assessment.get("next_action") or "人工复核后决定",
    }
