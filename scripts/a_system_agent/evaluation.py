from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def compute_evaluation(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    assessment_total = int(
        conn.execute("SELECT COUNT(*) FROM agent_candidate_assessments").fetchone()[0]
    )
    latest_feedback = conn.execute(
        """
        SELECT f.*,a.client,a.job,a.fit_score,a.fit_level,a.recommendation,a.created_at AS assessed_at,
               p.display_name AS candidate
        FROM agent_feedback f
        JOIN agent_candidate_assessments a ON a.id=f.assessment_id
        JOIN job_candidates jc ON jc.id=f.job_candidate_id
        JOIN people p ON p.id=jc.person_id
        WHERE f.id=(SELECT MAX(f2.id) FROM agent_feedback f2 WHERE f2.assessment_id=f.assessment_id)
        ORDER BY f.id DESC
        """
    ).fetchall()
    counts = Counter(str(row["feedback_type"] or "") for row in latest_feedback)
    reviewed = len(latest_feedback)
    approved = counts.get("approve", 0)
    corrected = counts.get("correct", 0)
    rejected = counts.get("reject", 0)

    recommendation_stats: dict[str, dict[str, int]] = {}
    correction_themes: Counter[str] = Counter()
    recent: list[dict[str, Any]] = []
    for row in latest_feedback:
        recommendation = str(row["recommendation"] or "unknown")
        bucket = recommendation_stats.setdefault(
            recommendation, {"reviewed": 0, "approved": 0, "corrected": 0, "rejected": 0}
        )
        bucket["reviewed"] += 1
        feedback_type = str(row["feedback_type"] or "")
        if feedback_type in bucket:
            bucket[feedback_type] += 1
        corrected_payload = _loads(row["corrected_json"], {})
        if isinstance(corrected_payload, dict):
            for key, value in corrected_payload.items():
                text = " ".join(str(value or "").split())
                correction_themes[f"{key}: {text}" if text else str(key)] += 1
        recent.append(
            {
                "feedback_id": row["id"],
                "assessment_id": row["assessment_id"],
                "job_candidate_id": row["job_candidate_id"],
                "candidate": row["candidate"] or "",
                "client": row["client"] or "",
                "job": row["job"] or "",
                "feedback_type": feedback_type,
                "fit_score": row["fit_score"],
                "recommendation": recommendation,
                "note": row["note"] or "",
                "created_at": row["created_at"],
            }
        )

    panel_total = int(conn.execute("SELECT COUNT(*) FROM agent_review_panels").fetchone()[0])
    panel_completed = int(
        conn.execute("SELECT COUNT(*) FROM agent_review_panels WHERE status='completed'").fetchone()[0]
    )
    role_total = int(conn.execute("SELECT COUNT(*) FROM agent_role_reviews").fetchone()[0])
    model_role_total = int(
        conn.execute("SELECT COUNT(*) FROM agent_role_reviews WHERE source='model'").fetchone()[0]
    )
    fallback_role_total = role_total - model_role_total
    memory_counts = {
        str(row["status"]): int(row["total"])
        for row in conn.execute(
            "SELECT status,COUNT(*) AS total FROM agent_learning_rules GROUP BY status"
        ).fetchall()
    }
    generated_at = str(
        conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
    )
    return {
        "generated_at": generated_at,
        "assessment_total": assessment_total,
        "reviewed_total": reviewed,
        "unreviewed_total": max(0, assessment_total - reviewed),
        "approved_total": approved,
        "corrected_total": corrected,
        "rejected_total": rejected,
        "feedback_coverage": _ratio(reviewed, assessment_total),
        "agreement_rate": _ratio(approved, reviewed),
        "correction_rate": _ratio(corrected, reviewed),
        "rejection_rate": _ratio(rejected, reviewed),
        "panel_total": panel_total,
        "panel_completed": panel_completed,
        "role_review_total": role_total,
        "model_role_total": model_role_total,
        "fallback_role_total": fallback_role_total,
        "model_role_rate": _ratio(model_role_total, role_total),
        "memory": {
            "pending": memory_counts.get("pending", 0),
            "active": memory_counts.get("active", 0),
            "revoked": memory_counts.get("revoked", 0),
        },
        "recommendations": recommendation_stats,
        "correction_themes": [
            {"theme": theme, "count": count}
            for theme, count in correction_themes.most_common(8)
        ],
        "recent_feedback": recent[:12],
    }
