#!/usr/bin/env python3
"""Refresh open candidate reply tasks using the latest classifier."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
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
            ifnull(t.task_type, '') AS old_task_type,
            ifnull(t.priority, 2) AS old_priority,
            ifnull(t.reason, '') AS old_reason,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            r.id AS reply_id,
            ifnull(r.candidate_name, '') AS candidate_name,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.intent, '') AS old_intent
        FROM followup_tasks t
        JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
        ORDER BY t.id DESC
        """
    ).fetchall()


def refresh(conn: sqlite3.Connection) -> dict[str, object]:
    rows = load_rows(conn)
    changed: list[dict[str, object]] = []
    unchanged = 0
    intents = Counter()
    task_types = Counter()
    for row in rows:
        classified = classify_reply(row["raw_text"] or "")
        intents[classified["intent"]] += 1
        task_types[classified["task_type"]] += 1
        changed_flag = (
            classified["intent"] != (row["old_intent"] or "")
            or classified["task_type"] != (row["old_task_type"] or "")
            or int(classified["priority"]) != int(row["old_priority"] or 0)
            or classified["suggested_next_action"] != (row["old_reason"] or "")
        )
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
            (
                classified["task_type"],
                int(classified["priority"]),
                classified["suggested_next_action"],
                row["task_id"],
            ),
        )
        if changed_flag:
            changed.append(
                {
                    "task_id": row["task_id"],
                    "candidate_name": row["candidate_name"],
                    "old_intent": row["old_intent"],
                    "new_intent": classified["intent"],
                    "old_task_type": row["old_task_type"],
                    "new_task_type": classified["task_type"],
                    "raw_text": row["raw_text"],
                }
            )
        else:
            unchanged += 1
    conn.commit()
    return {
        "total": len(rows),
        "changed": changed,
        "unchanged": unchanged,
        "intent_counts": dict(intents),
        "task_type_counts": dict(task_types),
    }


def write_report(output_dir: Path, result: dict[str, object]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘打开待办分类刷新_{stamp}.md"
    lines = [
        "# 猎聘打开待办分类刷新",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 打开待办总数：{result['total']}",
        f"- 有变化：{len(result['changed'])}",
        f"- 无变化：{result['unchanged']}",
        f"- 新意图分布：{'、'.join(f'{k} {v}' for k, v in result['intent_counts'].items())}",
        f"- 新任务分布：{'、'.join(f'{k} {v}' for k, v in result['task_type_counts'].items())}",
        "",
        "## 变化样例",
        "",
    ]
    for item in result["changed"][:40]:
        lines.append(
            f"- #{item['task_id']}｜{item['candidate_name']}｜{item['old_intent']}->{item['new_intent']}｜"
            f"{item['old_task_type']}->{item['new_task_type']}｜{item['raw_text']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh open reply tasks with the latest classifier.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db).expanduser())
    try:
        result = refresh(conn)
    finally:
        conn.close()
    report = write_report(output_dir, result)
    print(json.dumps({"ok": True, **result, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
