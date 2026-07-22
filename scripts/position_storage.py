#!/usr/bin/env python3
"""Shared storage helpers for the Liepin position book."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


POSITION_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    position TEXT NOT NULL,
    position_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT,
    source_url TEXT,
    source_title TEXT,
    raw_text TEXT,
    raw_json TEXT DEFAULT '{}',
    content_hash TEXT NOT NULL,
    captured_at TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(content_hash)
)
"""

POSITION_ASSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    position TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    asset_title TEXT,
    asset_summary TEXT,
    file_path TEXT NOT NULL,
    source_snapshot_id INTEGER,
    asset_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(client, position, asset_type, file_path)
)
"""

POSITION_STORAGE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_project_time ON position_snapshots(client, position, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_source ON position_snapshots(source_type, source_ref)",
    "CREATE INDEX IF NOT EXISTS idx_position_assets_project_type ON position_assets(client, position, asset_type)",
]


def clean(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("\u3000", " ").split())


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def ensure_position_storage_schema(conn: sqlite3.Connection) -> None:
    conn.execute(POSITION_SNAPSHOT_SCHEMA)
    conn.execute(POSITION_ASSET_SCHEMA)
    for stmt in POSITION_STORAGE_INDEXES:
        conn.execute(stmt)
    conn.commit()


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, Path)):
        return str(value)
    return str(value)


def stable_hash(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _position_raw_text(data: dict[str, Any]) -> str:
    parts = [
        data.get("title", ""),
        data.get("department", ""),
        data.get("team", ""),
        data.get("level", ""),
        data.get("education", ""),
        data.get("experience", ""),
        data.get("requirements", ""),
        data.get("responsibilities", ""),
        f"headcount={data.get('headcount', '')}",
        f"gap={data.get('gap', '')}",
        data.get("deadline", ""),
        data.get("status", ""),
    ]
    return "\n".join(clean(part) for part in parts if clean(part))


def build_position_snapshot_record(
    row: sqlite3.Row | dict[str, Any],
    *,
    source_type: str = "positions_row",
    source_ref: str | None = None,
    source_url: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    data = _row_to_dict(row)
    client = clean(data.get("client"))
    position = clean(data.get("title") or data.get("position"))
    raw_json = {
        key: data.get(key)
        for key in data.keys()
    }
    raw_text = _position_raw_text(data)
    payload = {
        "client": client,
        "position": position,
        "source_type": source_type,
        "source_ref": clean(source_ref) or clean(data.get("id")),
        "source_url": clean(source_url),
        "raw_text": raw_text,
        "raw_json": raw_json,
    }
    return {
        "client": client,
        "position": position,
        "position_id": data.get("id"),
        "source_type": source_type,
        "source_ref": clean(source_ref) or clean(data.get("id")),
        "source_url": clean(source_url),
        "source_title": clean(data.get("title") or data.get("position")),
        "raw_text": raw_text,
        "raw_json": json.dumps(raw_json, ensure_ascii=False),
        "content_hash": stable_hash(payload),
        "captured_at": clean(captured_at) or clean(data.get("updated_at")) or clean(data.get("created_at")) or datetime.now().isoformat(timespec="seconds"),
    }


def seed_position_snapshots_from_positions(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "positions"):
        return 0
    ensure_position_storage_schema(conn)
    rows = conn.execute("SELECT * FROM positions").fetchall()
    before = conn.total_changes
    for row in rows:
        record = build_position_snapshot_record(
            row,
            source_type="positions_row",
            source_ref=str(row["id"]),
            captured_at=row["updated_at"] or row["created_at"],
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO position_snapshots (
                client, position, position_id, source_type, source_ref, source_url,
                source_title, raw_text, raw_json, content_hash, captured_at
            ) VALUES (
                :client, :position, :position_id, :source_type, :source_ref, :source_url,
                :source_title, :raw_text, :raw_json, :content_hash, :captured_at
            )
            """,
            record,
        )
    conn.commit()
    return conn.total_changes - before


def fetch_latest_position_snapshot(
    conn: sqlite3.Connection,
    client: str,
    position: str,
) -> sqlite3.Row | None:
    if not table_exists(conn, "position_snapshots"):
        return None
    return conn.execute(
        """
        SELECT *
        FROM position_snapshots
        WHERE client = ? AND position = ?
        ORDER BY datetime(COALESCE(captured_at, created_at)) DESC, id DESC
        LIMIT 1
        """,
        (clean(client), clean(position)),
    ).fetchone()


def fetch_position_snapshots(
    conn: sqlite3.Connection,
    client: str,
    position: str,
    limit: int = 5,
) -> list[sqlite3.Row]:
    if not table_exists(conn, "position_snapshots"):
        return []
    return conn.execute(
        """
        SELECT *
        FROM position_snapshots
        WHERE client = ? AND position = ?
        ORDER BY datetime(COALESCE(captured_at, created_at)) DESC, id DESC
        LIMIT ?
        """,
        (clean(client), clean(position), limit),
    ).fetchall()


def fetch_position_assets(
    conn: sqlite3.Connection,
    client: str,
    position: str,
    limit: int = 12,
) -> list[sqlite3.Row]:
    if not table_exists(conn, "position_assets"):
        return []
    return conn.execute(
        """
        SELECT *
        FROM position_assets
        WHERE client = ? AND position = ?
        ORDER BY datetime(COALESCE(updated_at, created_at)) DESC, id DESC
        LIMIT ?
        """,
        (clean(client), clean(position), limit),
    ).fetchall()


def upsert_position_asset(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    ensure_position_storage_schema(conn)
    cursor = conn.execute(
        """
        INSERT INTO position_assets (
            client, position, asset_type, asset_title, asset_summary,
            file_path, source_snapshot_id, asset_json, created_at, updated_at
        ) VALUES (
            :client, :position, :asset_type, :asset_title, :asset_summary,
            :file_path, :source_snapshot_id, :asset_json, datetime('now','localtime'), datetime('now','localtime')
        )
        ON CONFLICT(client, position, asset_type, file_path) DO UPDATE SET
            asset_title=excluded.asset_title,
            asset_summary=excluded.asset_summary,
            source_snapshot_id=excluded.source_snapshot_id,
            asset_json=excluded.asset_json,
            updated_at=datetime('now','localtime')
        """,
        {
            "client": clean(record.get("client")),
            "position": clean(record.get("position")),
            "asset_type": clean(record.get("asset_type")),
            "asset_title": clean(record.get("asset_title")),
            "asset_summary": clean(record.get("asset_summary")),
            "file_path": clean(record.get("file_path")),
            "source_snapshot_id": record.get("source_snapshot_id"),
            "asset_json": record.get("asset_json") or "{}",
        },
    )
    conn.commit()
    return int(cursor.lastrowid)
