"""Auditable cleanup of candidate relationships for one job.

The operation is deliberately a soft archive. It keeps people/candidates and
assessment history intact while removing selected job relationships from the
active pipeline through the existing H5 stopped state.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .batch_stop import apply_batch_stop, build_batch_stop_items
from .candidate_pool_filter import filter_job_candidates, job_filter_domain


SCOPE_ALL_ACTIVE = "all_active"
SCOPE_NONMATCHING = "nonmatching"
VALID_SCOPE_MODES = {SCOPE_ALL_ACTIVE, SCOPE_NONMATCHING}


class RelationshipCleanupScopeBlocked(ValueError):
    """The requested cleanup scope cannot be resolved without unsafe guessing."""


def _normalized_scope(scope_mode: str) -> str:
    mode = str(scope_mode or SCOPE_NONMATCHING).strip().lower()
    if mode not in VALID_SCOPE_MODES:
        raise ValueError(f"不支持的岗位关系清理范围：{scope_mode}")
    return mode


def _job_context(job: dict[str, Any]) -> str:
    return " ".join(
        str(job.get(key) or "")
        for key in ("summary", "hard_requirements", "ability_keywords", "search_words", "exclusions")
        if key in job
    )


def _nonmatching_domain(job: dict[str, Any]) -> str:
    domain = job_filter_domain(str(job.get("title") or ""), _job_context(job))
    if domain is None:
        raise RelationshipCleanupScopeBlocked(
            "无法可靠识别当前岗位职能域，无法安全自动判断“不匹配”关系。"
            "请明确选择归档全部在推进关系，或人工指定要归档的关系。"
        )
    return domain


def _is_active(stage: Any, raw_status: Any) -> bool:
    stage_text = str(stage or "")
    status_text = str(raw_status or "")
    return not (
        any(token in stage_text for token in ("初筛不通过", "停止", "淘汰", "关闭"))
        or status_text in {"screen_rejected", "rejected", "xsaas_review_stop"}
    )


def _job_rows(db_path: str, job_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT j.*, c.name AS client
            FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE j.id=?
            """,
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise ValueError(f"岗位不存在：{job_id}")
        rows = conn.execute(
            """
            SELECT jc.id, jc.clean_stage, jc.raw_status, p.display_name,
                   p.current_company, p.current_title
            FROM job_candidates jc
            JOIN people p ON p.id=jc.person_id
            WHERE jc.job_id=?
            ORDER BY jc.updated_at DESC, jc.id DESC
            """,
            (int(job_id),),
        ).fetchall()
        return dict(job), [dict(row) for row in rows]
    finally:
        conn.close()


def build_relationship_cleanup_preview(
    db_path: str,
    job_id: int,
    *,
    scope_mode: str = SCOPE_NONMATCHING,
    relationship_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Return the exact current relation set that an R2 approval would archive."""
    mode = _normalized_scope(scope_mode)

    job, rows = _job_rows(db_path, int(job_id))
    active_by_id = {
        int(row["id"]): row
        for row in rows
        if _is_active(row.get("clean_stage"), row.get("raw_status"))
    }
    if mode == SCOPE_ALL_ACTIVE:
        items = [
            {
                "jc_id": relation_id,
                "name": str(row.get("display_name") or ""),
                "company": str(row.get("current_company") or ""),
                "title": str(row.get("current_title") or ""),
                "grade": "范围内全部",
                "reason": "顾问明确要求清理当前岗位全部在推进关系",
                "stop_reason": "other",
                "stop_reason_label": "岗位关系归档",
                "note": "ASA 岗位关系清理：保留人选主档，仅归档当前岗位关系",
            }
            for relation_id, row in active_by_id.items()
        ]
    else:
        domain = _nonmatching_domain(job)
        filtered = filter_job_candidates(
            db_path,
            int(job_id),
            client=str(job.get("client") or ""),
            domain=domain,
            max_candidates=max(2000, len(rows) + 1),
        )
        items = [
            item
            for item in build_batch_stop_items(filtered)
            if int(item.get("jc_id") or 0) in active_by_id
        ]

    if relationship_ids is not None:
        approved_ids = {int(item) for item in relationship_ids if int(item) > 0}
        items = [item for item in items if int(item.get("jc_id") or 0) in approved_ids]
    items.sort(key=lambda item: int(item.get("jc_id") or 0))
    return {
        "version": "candidate_relationship_cleanup_preview_v1",
        "job_id": int(job_id),
        "client": str(job.get("client") or ""),
        "job": str(job.get("title") or ""),
        "scope_mode": mode,
        "active_relationships": len(active_by_id),
        "relationship_count": len(items),
        "candidate_records_preserved": True,
        "items": items,
    }


def validate_relationship_cleanup_scope(
    db_path: str,
    job_id: int,
    *,
    scope_mode: str = SCOPE_NONMATCHING,
) -> None:
    """Fail closed before a workflow is created when nonmatching is unsafe."""
    mode = _normalized_scope(scope_mode)
    if mode == SCOPE_ALL_ACTIVE:
        return
    job, _ = _job_rows(db_path, int(job_id))
    _nonmatching_domain(job)


def apply_relationship_cleanup(
    db_path: str,
    job_id: int,
    *,
    scope_mode: str = SCOPE_NONMATCHING,
    actor: str = "copilot",
    approved_relationship_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Soft-archive the approved relation set without changing candidate masters."""
    preview = build_relationship_cleanup_preview(
        db_path,
        int(job_id),
        scope_mode=scope_mode,
        relationship_ids=approved_relationship_ids,
    )
    result = apply_batch_stop(
        db_path,
        int(job_id),
        list(preview["items"]),
        actor=actor,
        source="candidate_relationship_cleanup",
        update_candidate_status=False,
    )
    return {
        **result,
        "version": "candidate_relationship_cleanup_receipt_v1",
        "job_id": int(job_id),
        "client": preview["client"],
        "job": preview["job"],
        "scope_mode": preview["scope_mode"],
        "approved_relationships": (
            len({int(item) for item in approved_relationship_ids if int(item) > 0})
            if approved_relationship_ids is not None
            else preview["relationship_count"]
        ),
        "candidate_records_preserved": True,
    }
