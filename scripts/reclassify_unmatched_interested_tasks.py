#!/usr/bin/env python3
"""Reclassify unmatched interested call tasks into more specific follow-up buckets."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from reply_intelligence_rules import classify_reply


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            t.id AS task_id,
            t.priority AS task_priority,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.client, '') AS client,
            ifnull(t.position, '') AS position,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.confirmed_client, '') AS confirmed_client,
            ifnull(t.confirmed_position, '') AS confirmed_position,
            r.id AS reply_id,
            ifnull(r.candidate_name, '') AS candidate_name,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.intent, '') AS old_intent
        FROM followup_tasks t
        JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
          AND ifnull(t.task_type, '') = 'call_candidate'
          AND ifnull(r.intent, '') = 'interested'
          AND ifnull(t.match_confidence, 'unmatched') = 'unmatched'
        ORDER BY t.id DESC
        """
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


def reclassify(conn: sqlite3.Connection) -> dict[str, object]:
    rows = load_rows(conn)
    changed: list[dict[str, object]] = []
    unchanged: list[dict[str, object]] = []
    for row in rows:
        classified = classify_reply(row["raw_text"] or "")
        new_intent = classified["intent"]
        new_task_type = classified["task_type"]
        new_priority = int(classified["priority"])

        if has_anchor(row) and new_intent == "targeted_interest":
            unchanged.append({"task_id": row["task_id"], "candidate_name": row["candidate_name"], "raw_text": row["raw_text"], "intent": new_intent})
            continue

        if new_intent == "interested" and new_task_type == "call_candidate":
            unchanged.append({"task_id": row["task_id"], "candidate_name": row["candidate_name"], "raw_text": row["raw_text"], "intent": new_intent})
            continue

        conn.execute(
            """
            UPDATE candidate_replies
            SET intent = ?,
                sentiment = ?,
                blockers_json = ?,
                reply_tags_json = ?,
                classification_reason = ?,
                classifier_version = ?,
                suggested_next_action = ?
            WHERE id = ?
            """,
            (
                classified["intent"],
                classified["sentiment"],
                json.dumps(classified["blockers"], ensure_ascii=False),
                json.dumps(classified.get("reply_tags") or [], ensure_ascii=False),
                classified.get("classification_reason") or "",
                classified.get("classifier_version") or "",
                classified["suggested_next_action"],
                row["reply_id"],
            ),
        )
        conn.execute(
            """
            UPDATE followup_tasks
            SET task_type = ?,
                priority = ?,
                reason = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (new_task_type, new_priority, classified["suggested_next_action"], row["task_id"]),
        )
        changed.append(
            {
                "task_id": row["task_id"],
                "candidate_name": row["candidate_name"],
                "raw_text": row["raw_text"],
                "old_intent": row["old_intent"],
                "new_intent": new_intent,
                "new_task_type": new_task_type,
            }
        )
    conn.commit()
    return {"total": len(rows), "changed": changed, "unchanged": unchanged}


def write_report(output_dir: Path, result: dict[str, object]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘强跟进二次分流_{stamp}.md"
    changed = result["changed"]
    unchanged = result["unchanged"]
    lines = [
        "# 猎聘强跟进二次分流",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 待重分流总数：{result['total']}",
        f"- 已重新分流：{len(changed)}",
        f"- 保持原样：{len(unchanged)}",
        "",
        "## 已重新分流",
        "",
    ]
    for item in changed[:30]:
        lines.append(
            f"- #{item['task_id']}｜{item['candidate_name']}｜{item['new_intent']}｜{item['new_task_type']}｜{item['raw_text']}"
        )
    lines.extend(["", "## 保持原样", ""])
    for item in unchanged[:20]:
        lines.append(f"- #{item['task_id']}｜{item['candidate_name']}｜{item['intent']}｜{item['raw_text']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Reclassify unmatched interested call tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db).expanduser())
    try:
        result = reclassify(conn)
    finally:
        conn.close()
    report = write_report(output_dir, result)
    print(json.dumps({"ok": True, **result, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
