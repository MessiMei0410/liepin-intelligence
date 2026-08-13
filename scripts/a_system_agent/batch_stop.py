"""批量停止推进：把 candidate_pool_filter 的分级结果中不匹配/禁挖/无证据的候选人
一次性落库为 H5 最近寻访/初筛不通过，并写审计事件。

与单人选 candidate_commit("stop") 保持同一落库口径：
- clean_stage / raw_stage = H5 最近寻访/初筛不通过
- flow_bucket = 最近寻访
- raw_status = screen_rejected（X-SaaS 用 xsaas_review_stop）
- candidates.status = screen_rejected
- candidate_events: resume_review_completed / stop
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any


STOP_GRADES = ("X-排除", "禁挖", "D-无证据", "D-无画像", "D-期望超限", "D-城市不符")
_MGMT_TOKENS = ("副总", "总经理", "总监", "部长", "主任", "经理", "manager", "ceo", "director")
_BATCH_STOP_LABELS = {"too_senior": "资历过高", "direction_mismatch": "方向不符", "other": "其他"}


def _stop_reason_for(item: dict[str, Any]) -> tuple[str, str]:
    grade = str(item.get("grade") or "")
    title = str(item.get("title") or "")
    company = str(item.get("company") or "")
    if grade == "禁挖":
        return "other", f"ASA 批量停止：禁挖名单（{company}）"
    if grade.startswith("D-"):
        return "other", "ASA 批量停止：简历无岗位硬证据"
    if any(token in title for token in _MGMT_TOKENS):
        return "too_senior", f"ASA 批量停止：资历过高（{title}）"
    return "direction_mismatch", f"ASA 批量停止：方向不符（{title}）"


def build_batch_stop_items(filter_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从分级结果中挑出需要停止推进的人，并附上统一停止原因。"""
    items: list[dict[str, Any]] = []
    for candidate in filter_result.get("candidates") or []:
        if str(candidate.get("grade") or "") not in STOP_GRADES:
            continue
        code, note = _stop_reason_for(candidate)
        items.append(
            {
                "jc_id": int(candidate.get("id") or 0),
                "name": str(candidate.get("name") or ""),
                "company": str(candidate.get("company") or ""),
                "title": str(candidate.get("title") or ""),
                "grade": str(candidate.get("grade") or ""),
                "reason": str(candidate.get("reason") or ""),
                "stop_reason": code,
                "stop_reason_label": _BATCH_STOP_LABELS.get(code, ""),
                "note": note,
            }
        )
    return items


def batch_stop_summary(items: list[dict[str, Any]]) -> str:
    counts = Counter(str(item.get("stop_reason") or "other") for item in items)
    parts = [
        f"{_BATCH_STOP_LABELS.get(code, code)} {count} 人"
        for code, count in sorted(counts.items(), key=lambda pair: -pair[1])
    ]
    return "、".join(parts) or "0 人"


def apply_batch_stop(
    db_path: str,
    job_id: int,
    items: list[dict[str, Any]],
    *,
    actor: str = "copilot",
    source: str = "copilot_batch_stop",
) -> dict[str, Any]:
    """幂等地批量落库停止；已停止的人选会被跳过。"""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    applied = skipped = events = candidates_updated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in items:
            jc_id = int(item.get("jc_id") or 0)
            if jc_id <= 0:
                skipped += 1
                continue
            row = conn.execute(
                "SELECT person_id, job_id, clean_stage, source_candidate_id FROM job_candidates WHERE id=?",
                (jc_id,),
            ).fetchone()
            if row is None:
                skipped += 1
                continue
            current_stage = str(row["clean_stage"] or "")
            if any(token in current_stage for token in ("初筛不通过", "停止", "淘汰", "关闭")):
                skipped += 1
                continue

            is_xsaas = current_stage.startswith("X")
            raw_status = "xsaas_review_stop" if is_xsaas else "screen_rejected"
            stage = "H5 最近寻访/初筛不通过"
            code = str(item.get("stop_reason") or "other")
            note = str(item.get("note") or "ASA 批量停止推进")
            conn.execute(
                """
                UPDATE job_candidates
                   SET clean_stage=?, flow_bucket=?, raw_status=?, raw_stage=?,
                       clean_reason=?, stop_reason=?, updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (stage, "最近寻访", raw_status, stage, note, code, jc_id),
            )
            applied += 1

            source_candidate_id = str(row["source_candidate_id"] or "").strip()
            if source_candidate_id.isdigit():
                candidate = conn.execute(
                    "SELECT id FROM candidates WHERE id=?", (int(source_candidate_id),)
                ).fetchone()
                if candidate:
                    conn.execute(
                        """
                        UPDATE candidates
                           SET status=?,
                               notes=CASE
                                 WHEN trim(COALESCE(notes,''))='' THEN ?
                                 ELSE trim(notes) || '｜' || ?
                               END,
                               updated_at=datetime('now','localtime')
                         WHERE id=?
                        """,
                        ("screen_rejected", note, note, int(source_candidate_id)),
                    )
                    candidates_updated += 1

            raw = {
                "action": "stop",
                "actor": actor,
                "note": note,
                "stop_reason": code,
                "stop_reason_label": item.get("stop_reason_label") or _BATCH_STOP_LABELS.get(code, ""),
                "grade": item.get("grade"),
                "grade_reason": item.get("reason"),
                "source": source,
            }
            conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id, person_id, job_id, event_type, event_status, event_time, summary, raw_json, source_table)
                VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'copilot_batch_stop')
                """,
                (jc_id, row["person_id"], row["job_id"], "resume_review_completed", "stop", note, json.dumps(raw, ensure_ascii=False)),
            )
            events += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "applied": applied,
        "skipped": skipped,
        "events": events,
        "candidates_updated": candidates_updated,
    }
