#!/usr/bin/env python3
"""寻访入池人选履历补抓。

自动寻访入库（intake）之后调用：找出该客户+岗位下已入池、但 source_profiles
里还没有完整猎聘履历的人选，逐个打开简历详情页抓取（复用
run_published_position_search.capture_resume_details 的限速与风控检测），
回写 source_profiles.raw_json / people / candidate_events。

stdout 只输出一个 JSON 对象，供 capability_runtime._run_external_json 解析。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from run_published_position_search import capture_resume_details

DEFAULT_DB = Path(
    os.environ.get("A_SYSTEM_DB")
    or "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
)
MIN_FULL_TEXT_CHARS = 100
CAPTURE_METHOD = "asa_liepin_cdp_backfill"

_TARGET_SQL = """
SELECT jc.id AS job_candidate_id, jc.job_id, jc.person_id, jc.source_candidate_id,
       p.display_name AS name, p.current_company AS company, p.current_title AS title,
       sp.id AS source_profile_id, sp.raw_json AS profile_raw_json,
       ev.raw_json AS event_raw_json
FROM job_candidates jc
JOIN people p ON p.id = jc.person_id
JOIN jobs j ON j.id = jc.job_id
JOIN clients c ON c.id = j.client_id
LEFT JOIN source_profiles sp ON sp.id = (
    SELECT sp2.id FROM source_profiles sp2
    WHERE sp2.person_id = jc.person_id AND lower(sp2.source_type) = 'liepin'
    ORDER BY sp2.source_date DESC, sp2.id DESC LIMIT 1)
LEFT JOIN candidate_events ev ON ev.id = (
    SELECT ev2.id FROM candidate_events ev2
    WHERE ev2.job_candidate_id = jc.id AND ev2.event_type = 'search_shortlisted'
    ORDER BY ev2.id DESC LIMIT 1)
WHERE c.name = ? AND j.title = ?
ORDER BY jc.id
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _resume_complete(raw: dict[str, Any]) -> bool:
    full_text = str(raw.get("full_text") or "").strip()
    status = str(raw.get("resume_capture_status") or "").strip()
    return len(full_text) >= MIN_FULL_TEXT_CHARS and status in ("", "complete")


def select_targets(
    conn: sqlite3.Connection, client: str, job: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """返回 (待补抓卡片列表, 因无详情链接而跳过的人数)。"""
    targets: list[dict[str, Any]] = []
    skipped_no_url = 0
    for row in conn.execute(_TARGET_SQL, (client, job)).fetchall():
        raw = _loads(row["profile_raw_json"])
        if row["source_profile_id"] and _resume_complete(raw):
            continue
        event_raw = _loads(row["event_raw_json"])
        url = str(
            raw.get("source_url") or raw.get("resume_url")
            or event_raw.get("source_url") or event_raw.get("resume_url") or ""
        ).strip()
        if not url:
            skipped_no_url += 1
            continue
        targets.append({
            "name": row["name"],
            "current_company": row["company"],
            "current_title": row["title"],
            "resume_url": url,
            "raw_text": str(raw.get("raw_text") or event_raw.get("raw_text") or "").strip(),
            "_job_candidate_id": int(row["job_candidate_id"]),
            "_job_id": row["job_id"],
            "_person_id": int(row["person_id"]),
            "_source_profile_id": row["source_profile_id"],
            "_source_candidate_id": row["source_candidate_id"],
            "_base_raw": raw or event_raw,
        })
        if len(targets) >= limit:
            break
    return targets, skipped_no_url


def write_back(conn: sqlite3.Connection, card: dict[str, Any]) -> int:
    """把抓到的履历合并进 source_profiles.raw_json，返回 source_profile_id。"""
    raw = dict(card.get("_base_raw") or {})
    raw.update({
        "full_text": card.get("full_text") or "",
        "work_text": card.get("work_text") or "",
        "project_text": card.get("project_text") or "",
        "education_text": card.get("education_text") or "",
        "profile_text": card.get("profile_text") or "",
        "resume_capture_status": card.get("resume_capture_status") or "",
        "resume_capture_missing": card.get("resume_capture_missing") or [],
        "resume_capture_error": card.get("resume_capture_error") or "",
        "captured_at": card.get("resume_captured_at") or "",
        "source_url": card.get("resume_url") or raw.get("source_url") or "",
        "capture_method": CAPTURE_METHOD,
    })
    person_id = int(card["_person_id"])
    source_profile_id = card.get("_source_profile_id")
    if source_profile_id:
        source_profile_id = int(source_profile_id)
        conn.execute(
            "UPDATE source_profiles SET source_date=date('now','localtime'),raw_json=? WHERE id=?",
            (_dumps(raw), source_profile_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO source_profiles
            (person_id,source_type,source_candidate_id,source_date,raw_json)
            VALUES (?,'liepin',?,date('now','localtime'),?)
            """,
            (person_id, str(card.get("resume_id") or card.get("_source_candidate_id") or ""), _dumps(raw)),
        )
        source_profile_id = int(cursor.lastrowid)
    conn.execute(
        """
        UPDATE people SET
            current_company=CASE WHEN COALESCE(current_company,'')='' THEN ? ELSE current_company END,
            current_title=CASE WHEN COALESCE(current_title,'')='' THEN ? ELSE current_title END,
            city=CASE WHEN COALESCE(city,'')='' THEN ? ELSE city END,
            education=CASE WHEN COALESCE(education,'')='' THEN ? ELSE education END,
            experience=CASE WHEN COALESCE(experience,'')='' THEN ? ELSE experience END
        WHERE id=?
        """,
        (
            card.get("current_company"), card.get("current_title"), card.get("city"),
            card.get("education"), card.get("experience"), person_id,
        ),
    )
    job_candidate_id = int(card["_job_candidate_id"])
    summary = (
        f"ASA 寻访入库后自动补抓猎聘履历：{card.get('name') or '未识别'}；"
        f"工作经历 {len(str(card.get('work_text') or ''))} 字，"
        f"项目经历 {len(str(card.get('project_text') or ''))} 字，"
        f"教育经历 {len(str(card.get('education_text') or ''))} 字。"
    )
    event_payload = {
        "source_profile_id": source_profile_id,
        "resume_id": card.get("resume_id") or "",
        "source_url": card.get("resume_url") or "",
        "capture_method": CAPTURE_METHOD,
    }
    event = conn.execute(
        """
        SELECT id FROM candidate_events
        WHERE job_candidate_id=? AND event_type='resume_profile_captured'
          AND source_table='source_profiles' AND source_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (job_candidate_id, str(source_profile_id)),
    ).fetchone()
    if event:
        conn.execute(
            """
            UPDATE candidate_events SET event_status='completed',event_time=datetime('now','localtime'),
                summary=?,raw_json=? WHERE id=?
            """,
            (summary, _dumps(event_payload), int(event["id"])),
        )
    else:
        conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (?,?,?,'resume_profile_captured','completed',datetime('now','localtime'),?,?,'source_profiles',?)
            """,
            (job_candidate_id, person_id, card.get("_job_id"), summary, _dumps(event_payload), str(source_profile_id)),
        )
    return source_profile_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="寻访入池人选履历补抓")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--client", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--detail-min-delay", type=float, default=2.5)
    parser.add_argument("--detail-max-delay", type=float, default=5.5)
    parser.add_argument("--detail-burst-size", type=int, default=6)
    parser.add_argument("--detail-burst-cooldown", type=float, default=15.0)
    parser.add_argument("--stop-on-risk-page", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dry-run", action="store_true", help="只列出待补抓人选，不抓取")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = _connect(Path(args.db))
    try:
        targets, skipped_no_url = select_targets(conn, args.client, args.job, max(1, args.limit))
        result: dict[str, Any] = {
            "client": args.client,
            "job": args.job,
            "selected": len(targets),
            "skipped_no_url": skipped_no_url,
            "written": 0,
            "status": "completed",
        }
        if args.dry_run:
            result["status"] = "dry_run"
            result["targets"] = [
                {"job_candidate_id": card["_job_candidate_id"], "name": card["name"], "resume_url": card["resume_url"]}
                for card in targets
            ]
            print(_dumps(result))
            return 0
        if targets:
            try:
                stats = capture_resume_details(
                    args.port,
                    targets,
                    len(targets),
                    min_delay=args.detail_min_delay,
                    max_delay=args.detail_max_delay,
                    burst_size=args.detail_burst_size,
                    burst_cooldown=args.detail_burst_cooldown,
                    stop_on_risk_page=args.stop_on_risk_page,
                )
            except Exception as exc:
                result.update({"status": "failed", "error": str(exc)[:300]})
                print(_dumps(result))
                return 0
            result.update(stats)
            for card in targets:
                if str(card.get("resume_capture_status") or "") in ("complete", "partial"):
                    write_back(conn, card)
                    result["written"] = int(result["written"]) + 1
            conn.commit()
        print(_dumps(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
