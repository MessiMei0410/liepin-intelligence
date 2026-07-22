#!/usr/bin/env python3
"""Record one outreach event into the local talent pool DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


SCHEMA = """
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


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outreach_client_position ON outreach_events(client, position)"
    )
    conn.commit()


def normalize_summary(text: str, limit: int = 220) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("..." if len(clean) > limit else "")


def infer_candidate(conn: sqlite3.Connection, name: str, company: str) -> sqlite3.Row | None:
    if not name:
        return None
    params: list[str] = [name]
    where = ["name = ?"]
    if company:
        where.append("company = ?")
        params.append(company)
    return conn.execute(
        f"""
        SELECT id, name, company, client, position
        FROM candidates
        WHERE {' OR '.join(where)}
        ORDER BY
            CASE WHEN status IN ('recommended','contacted','interviewing','offered','greeted') THEN 0 ELSE 1 END,
            updated_at DESC,
            created_at DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def build_record(args: argparse.Namespace, candidate: sqlite3.Row | None) -> dict:
    candidate_id = args.candidate_id
    candidate_company = args.candidate_company or ""
    client = args.client or ""
    position = args.position or ""

    if candidate:
        candidate_id = candidate_id or candidate["id"]
        candidate_company = candidate_company or candidate["company"] or ""
        client = client or candidate["client"] or ""
        position = position or candidate["position"] or ""

    return {
        "candidate_id": candidate_id,
        "candidate_name": args.candidate_name,
        "candidate_company": candidate_company,
        "client": client,
        "position": position,
        "channel": args.channel,
        "event_type": args.event_type,
        "event_status": args.event_status,
        "message_summary": normalize_summary(args.message_summary or args.message or ""),
        "source_url": args.source_url or "",
        "event_time": args.event_time or datetime.now().isoformat(timespec="seconds"),
    }


def insert_record(conn: sqlite3.Connection, record: dict) -> int:
    cursor = conn.execute(
        """
        INSERT INTO outreach_events (
            candidate_id, candidate_name, candidate_company, client, position,
            channel, event_type, event_status, message_summary, source_url, event_time
        ) VALUES (
            :candidate_id, :candidate_name, :candidate_company, :client, :position,
            :channel, :event_type, :event_status, :message_summary, :source_url, :event_time
        )
        """,
        record,
    )
    conn.commit()
    return int(cursor.lastrowid)


def write_receipt(output_dir: Path, record: dict, record_id: int | None, dry_run: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else f"id{record_id}"
    path = output_dir / f"猎聘触达事件记录_{suffix}_{stamp}.md"
    lines = [
        "# 猎聘触达事件记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"入库状态：{'未入库（干跑验证）' if dry_run else f'已入库 #{record_id}'}",
        "",
        "## 事件",
        "",
        f"- 候选人：{record['candidate_name']}",
        f"- 候选人公司：{record['candidate_company'] or '未填'}",
        f"- 客户：{record['client'] or '未填'}",
        f"- 岗位：{record['position'] or '未填'}",
        f"- 渠道：{record['channel']}",
        f"- 类型：{record['event_type']}",
        f"- 状态：{record['event_status']}",
        f"- 时间：{record['event_time']}",
        f"- 摘要：{record['message_summary'] or '无'}",
        f"- 来源：{record['source_url'] or '无'}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a Liepin outreach event.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--candidate-id", type=int)
    parser.add_argument("--candidate-name", required=True)
    parser.add_argument("--candidate-company")
    parser.add_argument("--client")
    parser.add_argument("--position")
    parser.add_argument("--channel", default="liepin")
    parser.add_argument("--event-type", default="reply_assistant_fill")
    parser.add_argument("--event-status", default="done")
    parser.add_argument("--message")
    parser.add_argument("--message-summary")
    parser.add_argument("--source-url")
    parser.add_argument("--event-time")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    record_id: int | None = None
    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        candidate = infer_candidate(conn, args.candidate_name, args.candidate_company or "")
        record = build_record(args, candidate)
        if not args.dry_run:
            record_id = insert_record(conn, record)
    finally:
        conn.close()

    receipt = write_receipt(Path(args.output_dir).expanduser(), record, record_id, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "record_id": record_id,
                "record": record,
                "receipt": str(receipt),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
