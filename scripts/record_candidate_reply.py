#!/usr/bin/env python3
"""Record one Liepin candidate reply and create the follow-up task."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


CANDIDATE_REPLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    channel TEXT DEFAULT 'liepin',
    conversation_id TEXT,
    message_time TEXT,
    direction TEXT DEFAULT 'candidate',
    raw_text TEXT NOT NULL,
    intent TEXT DEFAULT 'unclear',
    sentiment TEXT DEFAULT 'neutral',
    blockers_json TEXT DEFAULT '[]',
    suggested_next_action TEXT,
    processed_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(channel, conversation_id, message_time, raw_text)
)
"""


FOLLOWUP_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS followup_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    task_type TEXT NOT NULL,
    priority INTEGER DEFAULT 2,
    due_at TEXT,
    status TEXT DEFAULT 'open',
    reason TEXT,
    source_table TEXT,
    source_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


CANDIDATE_REPLY_EXTRA_COLUMNS = {
    "candidate_title": "TEXT",
    "inferred_client": "TEXT",
    "inferred_position": "TEXT",
    "match_confidence": "TEXT DEFAULT 'unmatched'",
    "match_reason": "TEXT",
    "draft_message": "TEXT",
    "confirmed_client": "TEXT",
    "confirmed_position": "TEXT",
    "confirmation_status": "TEXT DEFAULT 'unconfirmed'",
    "confirmation_note": "TEXT",
    "confirmed_at": "TEXT",
    "talk_strategy": "TEXT",
    "talk_score": "INTEGER DEFAULT 0",
    "talk_reason": "TEXT",
    "talk_risk": "TEXT",
    "talk_missing": "TEXT",
    "talk_generated_at": "TEXT",
    "reply_tags_json": "TEXT DEFAULT '[]'",
    "classification_reason": "TEXT",
    "classifier_version": "TEXT",
    "task_type": "TEXT",
    "priority": "INTEGER DEFAULT 2",
    "message_id": "TEXT",
    "message_evidence": "TEXT",
    "conversation_identity_confidence": "TEXT",
    "correction_status": "TEXT DEFAULT 'active'",
    "correction_reason": "TEXT",
    "corrected_at": "TEXT",
}


FOLLOWUP_TASK_EXTRA_COLUMNS = {
    "inferred_client": "TEXT",
    "inferred_position": "TEXT",
    "match_confidence": "TEXT DEFAULT 'unmatched'",
    "match_reason": "TEXT",
    "draft_message": "TEXT",
    "confirmed_client": "TEXT",
    "confirmed_position": "TEXT",
    "confirmation_status": "TEXT DEFAULT 'unconfirmed'",
    "confirmation_note": "TEXT",
    "confirmed_at": "TEXT",
    "talk_strategy": "TEXT",
    "talk_score": "INTEGER DEFAULT 0",
    "talk_reason": "TEXT",
    "talk_risk": "TEXT",
    "talk_missing": "TEXT",
    "talk_generated_at": "TEXT",
    "resolution_note": "TEXT",
    "closed_at": "TEXT",
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = table_columns(conn, table)
    for column, definition in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(CANDIDATE_REPLY_SCHEMA)
    conn.execute(FOLLOWUP_TASK_SCHEMA)
    add_missing_columns(conn, "candidate_replies", CANDIDATE_REPLY_EXTRA_COLUMNS)
    add_missing_columns(conn, "followup_tasks", FOLLOWUP_TASK_EXTRA_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_client_position ON candidate_replies(client, position)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_intent ON candidate_replies(intent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON followup_tasks(status, due_at)")
    conn.commit()


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def load_candidate(conn: sqlite3.Connection, candidate_id: int | None) -> sqlite3.Row | None:
    if not candidate_id:
        return None
    return conn.execute("SELECT * FROM candidates WHERE id = ? LIMIT 1", (candidate_id,)).fetchone()


def infer_candidate(conn: sqlite3.Connection, name: str, company: str = "", client: str = "", position: str = "") -> sqlite3.Row | None:
    name = clean(name)
    if not name:
        return None
    params: list[Any] = [name]
    filters = ["name = ?"]
    if company:
        filters.append("company = ?")
        params.append(company)
    if client:
        filters.append("client = ?")
        params.append(client)
    if position:
        filters.append("position = ?")
        params.append(position)
    return conn.execute(
        f"""
        SELECT *
        FROM candidates
        WHERE {' AND '.join(filters)}
        ORDER BY
            CASE WHEN status IN ('recommended','contacted','replied','client_approved','interviewing','offered') THEN 0 ELSE 1 END,
            datetime(updated_at) DESC,
            id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def stable_conversation_id(channel: str, candidate_name: str, candidate_title: str, raw_text: str, client: str, position: str) -> str:
    basis = "|".join([channel, candidate_name, candidate_title, raw_text, client, position])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def build_record(args: argparse.Namespace, candidate: sqlite3.Row | None) -> dict[str, Any]:
    raw_text = normalize_message(args.raw_text)
    if not raw_text:
        raise ValueError("候选人回复不能为空。")

    channel = clean(args.channel) or "liepin"
    candidate_name = clean(args.candidate_name) or (clean(candidate["name"]) if candidate else "")
    if not candidate_name:
        raise ValueError("需要选择或填写候选人。")
    candidate_company = clean(args.candidate_company) or (clean(candidate["company"]) if candidate else "")
    client = clean(args.client) or (clean(candidate["client"]) if candidate else "")
    position = clean(args.position) or (clean(candidate["position"]) if candidate else "")
    candidate_title = clean(args.candidate_title) or (clean(candidate["title"]) if candidate and "title" in candidate.keys() else "")
    message_time = clean(args.message_time) or datetime.now().isoformat(timespec="seconds")
    classification = classify_reply(raw_text)
    conversation_id = clean(args.conversation_id) or stable_conversation_id(
        channel,
        candidate_name,
        candidate_title,
        raw_text,
        client,
        position,
    )

    return {
        "candidate_id": int(args.candidate_id or (candidate["id"] if candidate else 0)) or None,
        "candidate_name": candidate_name,
        "candidate_company": candidate_company,
        "candidate_title": candidate_title,
        "client": client,
        "position": position,
        "channel": channel,
        "conversation_id": conversation_id,
        "message_time": message_time,
        "direction": "candidate",
        "raw_text": raw_text,
        "intent": classification["intent"],
        "sentiment": classification["sentiment"],
        "blockers_json": json.dumps(classification["blockers"], ensure_ascii=False),
        "reply_tags_json": json.dumps(classification.get("reply_tags") or [], ensure_ascii=False),
        "classification_reason": classification.get("classification_reason") or "",
        "classifier_version": classification.get("classifier_version") or "",
        "suggested_next_action": classification["suggested_next_action"],
        "task_type": classification["task_type"],
        "priority": int(classification["priority"]),
    }


def find_duplicate(conn: sqlite3.Connection, record: dict[str, Any]) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id
        FROM candidate_replies
        WHERE channel = ?
          AND COALESCE(candidate_name, '') = ?
          AND raw_text = ?
          AND COALESCE(correction_status, 'active') != 'undone'
        ORDER BY id DESC
        LIMIT 1
        """,
        (record["channel"], record["candidate_name"], record["raw_text"]),
    ).fetchone()


def insert_reply_and_task(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    create_task: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    record = {
        "message_id": "",
        "message_evidence": "",
        "conversation_identity_confidence": "",
        **record,
    }
    duplicate = find_duplicate(conn, record)
    if duplicate is not None:
        return {"reply_id": int(duplicate["id"]), "task_id": None, "inserted": 0, "duplicate": 1, "candidate_updates": 0}

    cursor = conn.execute(
        """
        INSERT INTO candidate_replies (
            candidate_id, candidate_name, candidate_company, candidate_title,
            client, position, channel, conversation_id, message_time, direction,
            raw_text, intent, sentiment, blockers_json, reply_tags_json,
            classification_reason, classifier_version, suggested_next_action,
            message_id, message_evidence, conversation_identity_confidence,
            task_type, priority, correction_status, processed_at
        ) VALUES (
            :candidate_id, :candidate_name, :candidate_company, :candidate_title,
            :client, :position, :channel, :conversation_id, :message_time, :direction,
            :raw_text, :intent, :sentiment, :blockers_json, :reply_tags_json,
            :classification_reason, :classifier_version, :suggested_next_action,
            :message_id, :message_evidence, :conversation_identity_confidence,
            :task_type, :priority, 'active', datetime('now','localtime')
        )
        """,
        record,
    )
    reply_id = int(cursor.lastrowid)

    task_id: int | None = None
    if create_task and record["task_type"] != "none":
        due = datetime.now() + timedelta(hours=4 if record["priority"] == 1 else 24)
        task_cursor = conn.execute(
        """
            INSERT INTO followup_tasks (
                candidate_id, candidate_name, candidate_company, client, position,
                task_type, priority, due_at, reason, source_table, source_id,
                inferred_client, inferred_position, match_confidence, match_reason, status
            ) VALUES (
                :candidate_id, :candidate_name, :candidate_company, :client, :position,
                :task_type, :priority, :due_at, :suggested_next_action, 'candidate_replies', :reply_id,
                :client, :position,
                CASE WHEN :client != '' AND :position != '' THEN 'confirmed' ELSE 'unmatched' END,
                :classification_reason, 'open'
            )
            """,
            {**record, "reply_id": reply_id, "due_at": due.isoformat(timespec="seconds")},
        )
        task_id = int(task_cursor.lastrowid)

    candidate_updates = 0
    if record["candidate_id"]:
        before = conn.total_changes
        conn.execute(
            """
            UPDATE candidates
            SET status = CASE
                    WHEN status IN ('new', 'contacted', 'recommended', '') OR status IS NULL THEN 'replied'
                    ELSE status
                END,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (record["candidate_id"],),
        )
        candidate_updates = conn.total_changes - before

    if commit:
        conn.commit()
    return {
        "reply_id": reply_id,
        "task_id": task_id,
        "inserted": 1,
        "duplicate": 0,
        "candidate_updates": candidate_updates,
    }


def write_receipt(output_dir: Path, record: dict[str, Any], stats: dict[str, Any], dry_run: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else f"id{stats.get('reply_id')}"
    path = output_dir / f"猎聘候选人回复记录_{suffix}_{stamp}.md"
    tags = json.loads(record["reply_tags_json"] or "[]")
    blockers = json.loads(record["blockers_json"] or "[]")
    lines = [
        "# 猎聘候选人回复记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "写入状态："
        + (
            "未写入（干跑验证）"
            if dry_run
            else ("重复，未新增" if stats.get("duplicate") else f"已写入 #{stats.get('reply_id')}")
        ),
        "",
        "## 回复判断",
        "",
        f"- 候选人：{record['candidate_name']}",
        f"- 项目：{record['client'] or '未定客户'}/{record['position'] or '未定岗位'}",
        f"- 意图：{record['intent']}",
        f"- 优先级：P{record['priority']}",
        f"- 标签：{'、'.join(tags) if tags else '无'}",
        f"- 阻力：{'、'.join(blockers) if blockers else '无'}",
        f"- 判断原因：{record['classification_reason'] or '无'}",
        f"- 建议动作：{record['suggested_next_action']}",
        "",
        "## 原文",
        "",
        record["raw_text"],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Record one Liepin candidate reply.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--candidate-id", type=int)
    parser.add_argument("--candidate-name", default="")
    parser.add_argument("--candidate-company", default="")
    parser.add_argument("--candidate-title", default="")
    parser.add_argument("--client", default="")
    parser.add_argument("--position", default="")
    parser.add_argument("--channel", default="liepin")
    parser.add_argument("--conversation-id", default="")
    parser.add_argument("--message-time", default="")
    parser.add_argument("--raw-text", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-task", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        candidate = load_candidate(conn, args.candidate_id) or infer_candidate(
            conn,
            args.candidate_name,
            args.candidate_company,
            args.client,
            args.position,
        )
        record = build_record(args, candidate)
        if args.dry_run:
            stats = {"reply_id": None, "task_id": None, "inserted": 0, "duplicate": 0, "candidate_updates": 0}
        else:
            stats = insert_reply_and_task(conn, record, create_task=not args.no_task)
    finally:
        conn.close()

    receipt = write_receipt(output_dir, record, stats, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "record": record,
                "stats": stats,
                "receipt": str(receipt),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
