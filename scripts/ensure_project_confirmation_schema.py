#!/usr/bin/env python3
"""Add project confirmation fields for follow-up tasks and replies."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path(os.environ.get("A_SYSTEM_DB", "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")).expanduser()
DEFAULT_OUTPUT_DIR = Path(os.environ.get("A_SYSTEM_LIEPIN_OUTPUTS", Path(__file__).resolve().parents[1] / "outputs")).expanduser()


CONFIRMATION_COLUMNS = {
    "confirmed_client": "TEXT",
    "confirmed_position": "TEXT",
    "confirmation_status": "TEXT DEFAULT 'unconfirmed'",
    "confirmation_note": "TEXT",
    "confirmed_at": "TEXT",
}


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    added: list[str] = []
    existing = table_columns(conn, table)
    for name, spec in CONFIRMATION_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
            added.append(f"{table}.{name}")
    return added


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    added: list[str] = []
    added.extend(ensure_columns(conn, "candidate_replies"))
    added.extend(ensure_columns(conn, "followup_tasks"))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            reply_id INTEGER,
            candidate_name TEXT,
            old_client TEXT,
            old_position TEXT,
            old_confidence TEXT,
            confirmed_client TEXT NOT NULL,
            confirmed_position TEXT NOT NULL,
            source TEXT DEFAULT 'manual',
            note TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_confirmations_task ON project_confirmations(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_confirmations_project ON project_confirmations(confirmed_client, confirmed_position)")
    conn.commit()
    return added


def write_report(added: list[str], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘项目确认底座巡检_{stamp}.md"
    lines = [
        "# 猎聘项目确认底座巡检",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- 新增字段：{len(added)}",
        "- 确认记录表：project_confirmations",
        "",
        "## 新增明细",
        "",
    ]
    if added:
        lines.extend(f"- {item}" for item in added)
    else:
        lines.append("- 无，当前数据库已经具备项目确认字段。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure project confirmation schema exists.")
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
    print(
        json.dumps(
            {
                "ok": True,
                "db": str(db_path),
                "added": added,
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
