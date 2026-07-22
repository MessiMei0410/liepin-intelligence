#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


VISIBLE_TABS = ("总览", "岗位看板", "人选进度", "人选列表")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
    ).fetchone() is not None


def scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main() -> None:
    parser = argparse.ArgumentParser(description="Tenant-neutral A System regression guard.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--skip-audit", action="store_true")
    args = parser.parse_args()

    checks: list[dict[str, object]] = []
    conn = sqlite3.connect(args.db)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        checks.append({"name": "sqlite_integrity", "ok": integrity == "ok", "detail": integrity})
        for table in ("clients", "jobs", "candidates", "job_candidates", "candidate_events"):
            checks.append({"name": f"schema_{table}", "ok": table_exists(conn, table)})
        if table_exists(conn, "job_candidates"):
            duplicates = scalar(
                conn,
                """SELECT COUNT(*) FROM (
                    SELECT job_id, person_id, COALESCE(raw_position, ''), COUNT(*) n
                    FROM job_candidates
                    GROUP BY job_id, person_id, COALESCE(raw_position, '') HAVING n > 1
                )""",
            )
            checks.append({"name": "job_candidate_duplicates", "ok": duplicates == 0, "detail": duplicates})
    finally:
        conn.close()

    html = args.html.read_text(encoding="utf-8") if args.html.exists() else ""
    checks.append({"name": "html_exists", "ok": bool(html)})
    for tab in VISIBLE_TABS:
        checks.append({"name": f"tab_{tab}", "ok": tab in html})
    ok = all(bool(item["ok"]) for item in checks)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
