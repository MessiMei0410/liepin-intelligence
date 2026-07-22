#!/usr/bin/env python3
"""Sync Liepin reply assistant outreach events into outreach_events."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from export_reply_assistant_samples import (
    DEFAULT_EXTENSION_ID,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PROFILE_DIR,
    extension_storage_dir,
)


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
EVENT_KEY = b"liepinReplyAssistantOutreachEvents"


OUTREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT NOT NULL,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    channel TEXT DEFAULT 'liepin',
    event_type TEXT NOT NULL,
    event_status TEXT DEFAULT 'done',
    message_summary TEXT,
    source_url TEXT,
    event_time TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_assistant_outreach_sync (
    event_id TEXT PRIMARY KEY,
    outreach_event_id INTEGER,
    event_at TEXT,
    event_type TEXT,
    candidate_name TEXT,
    project_client TEXT,
    project_position TEXT,
    raw_json TEXT NOT NULL,
    source TEXT,
    synced_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(OUTREACH_SCHEMA)
    conn.execute(SYNC_SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outreach_client_position ON outreach_events(client, position)"
    )
    conn.commit()


def decode_arrays_from_bytes(data: bytes) -> list[list[dict[str, Any]]]:
    decoder = json.JSONDecoder()
    arrays: list[list[dict[str, Any]]] = []
    offset = 0
    while True:
        key_pos = data.find(EVENT_KEY, offset)
        if key_pos < 0:
            break
        json_start = data.find(b"[", key_pos + len(EVENT_KEY), key_pos + len(EVENT_KEY) + 200)
        offset = key_pos + len(EVENT_KEY)
        if json_start < 0:
            continue
        text = data[json_start:].decode("utf-8", errors="ignore")
        try:
            value, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            arrays.append(value)
    return arrays


def newest_event_time(events: list[dict[str, Any]]) -> str:
    times = [str(item.get("eventAt", "")) for item in events if item.get("eventAt")]
    return max(times) if times else ""


def read_events_from_leveldb(storage_dir: Path) -> list[dict[str, Any]]:
    if not storage_dir.exists():
        return []

    candidates: list[list[dict[str, Any]]] = []
    for path in storage_dir.iterdir():
        if path.suffix not in {".log", ".ldb"}:
            continue
        try:
            candidates.extend(decode_arrays_from_bytes(path.read_bytes()))
        except OSError:
            continue
    if not candidates:
        return []
    return max(candidates, key=lambda arr: (len(arr), newest_event_time(arr)))


def stable_event_id(event: dict[str, Any]) -> str:
    explicit = str(event.get("id") or "").strip()
    if explicit:
        return explicit
    basis = {
        "eventAt": event.get("eventAt"),
        "eventType": event.get("eventType"),
        "candidateName": event.get("candidateName"),
        "draft": event.get("draft"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_event(event: dict[str, Any], source: str) -> dict[str, Any]:
    project = event.get("project") if isinstance(event.get("project"), dict) else {}
    candidate_name = event.get("candidateName") or "未识别"
    event_status = event.get("eventStatus") or "done"
    if candidate_name == "未识别" and event_status == "done":
        event_status = "needs_candidate_confirmation"
    return {
        "event_id": stable_event_id(event),
        "candidate_name": candidate_name,
        "candidate_company": "",
        "client": project.get("client") or "",
        "position": project.get("position") or "",
        "channel": event.get("channel") or "liepin",
        "event_type": event.get("eventType") or "reply_assistant_event",
        "event_status": event_status,
        "message_summary": event.get("messageSummary") or event.get("draft") or "",
        "source_url": event.get("sourceUrl") or event.get("url") or "",
        "event_time": event.get("eventAt") or datetime.now().isoformat(timespec="seconds"),
        "raw_json": json.dumps(event, ensure_ascii=False, sort_keys=True),
        "source": source,
    }


def existing_event_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["event_id"])
        for row in conn.execute("SELECT event_id FROM reply_assistant_outreach_sync")
    }


def find_candidate_id(conn: sqlite3.Connection, event: dict[str, Any]) -> int | None:
    name = event["candidate_name"]
    if not name or name == "未识别":
        return None
    row = conn.execute(
        """
        SELECT id
        FROM candidates
        WHERE name = ?
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (name,),
    ).fetchone()
    return int(row["id"]) if row else None


def insert_events(conn: sqlite3.Connection, events: list[dict[str, Any]]) -> dict[str, int]:
    known = existing_event_ids(conn)
    inserted = 0
    skipped = 0
    for event in events:
        if event["event_id"] in known:
            skipped += 1
            continue
        candidate_id = find_candidate_id(conn, event)
        cursor = conn.execute(
            """
            INSERT INTO outreach_events (
                candidate_id, candidate_name, candidate_company, client, position,
                channel, event_type, event_status, message_summary, source_url, event_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                event["candidate_name"],
                event["candidate_company"],
                event["client"],
                event["position"],
                event["channel"],
                event["event_type"],
                event["event_status"],
                event["message_summary"],
                event["source_url"],
                event["event_time"],
            ),
        )
        outreach_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO reply_assistant_outreach_sync (
                event_id, outreach_event_id, event_at, event_type, candidate_name,
                project_client, project_position, raw_json, source, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
            """,
            (
                event["event_id"],
                outreach_id,
                event["event_time"],
                event["event_type"],
                event["candidate_name"],
                event["client"],
                event["position"],
                event["raw_json"],
                event["source"],
            ),
        )
        inserted += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def mark_unidentified_events(conn: sqlite3.Connection) -> int:
    before = conn.total_changes
    conn.execute(
        """
        UPDATE outreach_events
        SET event_status = 'needs_candidate_confirmation'
        WHERE candidate_name = '未识别'
          AND COALESCE(event_status, 'done') = 'done'
        """
    )
    conn.commit()
    return conn.total_changes - before


def count_unidentified_events(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM outreach_events
        WHERE event_status = 'needs_candidate_confirmation'
        """
    ).fetchone()
    return int(row[0] or 0) if row else 0


def write_report(output_dir: Path, source: str, events: list[dict[str, Any]], stats: dict[str, int]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复助手触达事件同步报告_{stamp}.md"
    types = Counter(event["event_type"] for event in events)
    projects = Counter(
        f"{event['client'] or '未定客户'}/{event['position'] or '未定岗位'}"
        for event in events
    )
    lines = [
        "# 猎聘回复助手触达事件同步报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"数据来源：{source}",
        "",
        "## 同步结果",
        "",
        f"- 本次读取：{len(events)} 条",
        f"- 新增入库：{stats.get('inserted', 0)} 条",
        f"- 已同步跳过：{stats.get('skipped', 0)} 条",
        f"- 当前需补候选人：{stats.get('needs_candidate_confirmation', 0)} 条",
        "",
        "## 分布",
        "",
        f"- 事件类型：{'、'.join(f'{k} {v}' for k, v in types.items()) or '暂无'}",
        f"- 项目方向：{'、'.join(f'{k} {v}' for k, v in projects.most_common(8)) or '暂无'}",
        "",
        "## 最近事件",
        "",
    ]
    if events:
        for event in events[:10]:
            status = "｜待补人名" if event["event_status"] == "needs_candidate_confirmation" else ""
            lines.append(
                f"- {event['event_time']}｜{event['event_type']}｜{event['candidate_name']}｜"
                f"{event['client'] or '未定客户'}/{event['position'] or '未定岗位'}{status}"
            )
    else:
        lines.append("- 暂无插件触达事件。升级到 0.1.5 后，点击“采纳修改”或“填入输入框”会开始记录。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Liepin reply assistant outreach events.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--extension-id", default=DEFAULT_EXTENSION_ID)
    parser.add_argument("--json-file")
    args = parser.parse_args()

    if args.json_file:
        source = str(Path(args.json_file).expanduser())
        payload = json.loads(Path(args.json_file).expanduser().read_text(encoding="utf-8"))
        raw_events = payload.get("events") or payload.get("samples") or []
    else:
        storage_dir = extension_storage_dir(Path(args.profile_dir).expanduser(), args.extension_id)
        source = str(storage_dir)
        raw_events = read_events_from_leveldb(storage_dir)

    events = [normalize_event(event, source) for event in raw_events]
    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        stats = insert_events(conn, events)
        mark_unidentified_events(conn)
        stats["needs_candidate_confirmation"] = count_unidentified_events(conn)
    finally:
        conn.close()

    report = write_report(Path(args.output_dir).expanduser(), source, events, stats)
    print(
        json.dumps(
            {
                "ok": True,
                "source": source,
                "events": len(events),
                "inserted": stats.get("inserted", 0),
                "skipped": stats.get("skipped", 0),
                "needs_candidate_confirmation": stats.get("needs_candidate_confirmation", 0),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
