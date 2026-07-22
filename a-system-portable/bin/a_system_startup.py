#!/usr/bin/env python3
from __future__ import annotations

import json
import platform
import sqlite3
from pathlib import Path


HOME = Path(__file__).resolve().parents[1]
DB = HOME / "app" / "outputs" / "talent_system_v3.db"
HTML = HOME / "app" / "outputs" / "A系统.html"


def scalar(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


stats = {"jobs": 0, "candidates": 0, "job_candidates": 0, "candidate_events": 0}
if DB.exists():
    conn = sqlite3.connect(DB)
    try:
        stats = {name: scalar(conn, name) for name in stats}
    finally:
        conn.close()

print("A 系统便携版启动上下文：")
print(json.dumps({
    "home": str(HOME),
    "architecture": platform.machine(),
    "db": str(DB),
    "html": str(HTML),
    "service": "http://127.0.0.1:8765",
    "cdp": "http://127.0.0.1:9223",
    "visible_tabs": ["总览", "岗位看板", "人选进度", "人选列表"],
    "stats": stats,
    "rules": [
        "v3 database is the source of truth",
        "manual stop/H5/rejected states remain eliminated",
        "candidate progress names open exact talent detail",
        "rebuild, strict audit, regression, desktop/mobile check before handoff",
    ],
}, ensure_ascii=False, indent=2))

