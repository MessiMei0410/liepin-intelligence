#!/usr/bin/env python3
"""Add talk draft quality fields for follow-up tasks and replies."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


COLUMNS = {
    "talk_strategy": "TEXT",
    "talk_score": "INTEGER DEFAULT 0",
    "talk_reason": "TEXT",
    "talk_risk": "TEXT",
    "talk_missing": "TEXT",
    "talk_generated_at": "TEXT",
}


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    existing = table_columns(conn, table)
    added: list[str] = []
    for name, spec in COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
            added.append(f"{table}.{name}")
    return added


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    added: list[str] = []
    added.extend(ensure_columns(conn, "followup_tasks"))
    added.extend(ensure_columns(conn, "candidate_replies"))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS talk_draft_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            reply_id INTEGER,
            candidate_name TEXT,
            strategy TEXT,
            score INTEGER,
            reason TEXT,
            risk TEXT,
            missing TEXT,
            draft TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_talk_draft_audits_task ON talk_draft_audits(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_talk_draft_audits_strategy ON talk_draft_audits(strategy)")
    conn.commit()
    return added


def write_report(added: list[str], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘话术质量底座巡检_{stamp}.md"
    lines = [
        "# 猎聘话术质量底座巡检",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- 新增字段：{len(added)}",
        "- 审计记录表：talk_draft_audits",
        "",
        "## 新增明细",
        "",
    ]
    if added:
        lines.extend(f"- {item}" for item in added)
    else:
        lines.append("- 无，当前数据库已经具备话术质量字段。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure talk draft quality schema exists.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_db(db_path)
    try:
        added = ensure_schema(conn)
    finally:
        conn.close()

    report = write_report(added, output_dir)
    print(json.dumps({"ok": True, "added": added, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
