#!/usr/bin/env python3
"""Downgrade low-signal short-confirmation follow-up tasks into a light-touch pool."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
SHORT_CONFIRMATIONS = ("可以的", "好的", "嗯好", "ok", "OK", "收到", "行", "好呀")
LIGHT_REASON = "候选人仅做简短确认，先放入轻跟进池，补岗位锚点后再推进。"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in SHORT_CONFIRMATIONS)
    return conn.execute(
        f"""
        SELECT
            t.id,
            t.candidate_name,
            ifnull(t.client, '') AS client,
            ifnull(t.position, '') AS position,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.confirmed_client, '') AS confirmed_client,
            ifnull(t.confirmed_position, '') AS confirmed_position,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.task_type, '') AS task_type,
            ifnull(r.id, 0) AS reply_id,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.intent, '') AS intent
        FROM followup_tasks t
        JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
          AND ifnull(r.raw_text, '') IN ({marks})
        ORDER BY t.id DESC
        """,
        SHORT_CONFIRMATIONS,
    ).fetchall()


def has_anchor(row: sqlite3.Row) -> bool:
    return any(
        row[key]
        for key in (
            "client",
            "position",
            "inferred_client",
            "inferred_position",
            "confirmed_client",
            "confirmed_position",
        )
    )


def rebalance(conn: sqlite3.Connection) -> dict[str, object]:
    rows = load_rows(conn)
    downgraded: list[dict[str, object]] = []
    kept: list[dict[str, object]] = []
    for row in rows:
        if has_anchor(row) or row["match_confidence"] in ("confirmed", "high", "medium"):
            kept.append({"task_id": row["id"], "candidate_name": row["candidate_name"], "raw_text": row["raw_text"]})
            continue
        conn.execute(
            """
            UPDATE followup_tasks
            SET task_type = 'light_touch_followup',
                priority = 3,
                reason = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (LIGHT_REASON, row["id"]),
        )
        conn.execute(
            """
            UPDATE candidate_replies
            SET intent = 'short_confirmation',
                suggested_next_action = ?,
                classification_reason = '候选人仅简短确认，先轻跟进，不直接视为强意向。',
                classifier_version = 'reply-rules-v4'
            WHERE id = ?
            """,
            (LIGHT_REASON, row["reply_id"]),
        )
        downgraded.append({"task_id": row["id"], "candidate_name": row["candidate_name"], "raw_text": row["raw_text"]})
    conn.commit()
    return {"total": len(rows), "downgraded": downgraded, "kept": kept}


def write_report(output_dir: Path, result: dict[str, object]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘短确认降噪_{stamp}.md"
    downgraded = result["downgraded"]
    kept = result["kept"]
    lines = [
        "# 猎聘短确认降噪",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 短确认任务总数：{result['total']}",
        f"- 降为轻跟进：{len(downgraded)}",
        f"- 保留原优先级：{len(kept)}",
        "",
        "## 已降噪样例",
        "",
    ]
    for item in downgraded[:20]:
        lines.append(f"- #{item['task_id']}｜{item['candidate_name']}｜{item['raw_text']}")
    lines.extend(["", "## 保留原优先级样例", ""])
    for item in kept[:10]:
        lines.append(f"- #{item['task_id']}｜{item['candidate_name']}｜{item['raw_text']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebalance short-confirmation follow-up tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db).expanduser())
    try:
        result = rebalance(conn)
    finally:
        conn.close()
    report = write_report(output_dir, result)
    print(json.dumps({"ok": True, **result, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
