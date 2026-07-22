#!/usr/bin/env python3
"""Record learning notes from Liepin reply intelligence."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_replies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id,
            candidate_name,
            intent,
            raw_text,
            candidate_title,
            inferred_client,
            inferred_position,
            match_confidence,
            match_reason
        FROM candidate_replies
        ORDER BY id
        """
    ).fetchall()


def note_exists(conn: sqlite3.Connection, topic: str, source: str, today: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM learning_notes
        WHERE topic = ?
          AND source = ?
          AND date(created_at) = date(?)
        LIMIT 1
        """,
        (topic, source, today),
    ).fetchone()
    return row is not None


def insert_note(
    conn: sqlite3.Connection,
    *,
    client: str,
    position: str,
    topic: str,
    note: str,
    source: str,
    confidence: int,
    today: str,
) -> bool:
    if note_exists(conn, topic, source, today):
        return False
    conn.execute(
        """
        INSERT INTO learning_notes (client, position, topic, note, source, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
        """,
        (client, position, topic, note, source, confidence),
    )
    return True


def build_notes(rows: list[sqlite3.Row]) -> list[dict]:
    total = len(rows)
    confidence_counts = Counter((row["match_confidence"] or "unmatched") for row in rows)
    intent_counts = Counter((row["intent"] or "unclear") for row in rows)

    high_rows = [row for row in rows if row["match_confidence"] in ("high", "medium")]
    low_rows = [row for row in rows if row["match_confidence"] == "low"]
    unmatched_rows = [row for row in rows if row["match_confidence"] in ("", "unmatched")]

    notes: list[dict] = []
    notes.append(
        {
            "client": "",
            "position": "",
            "topic": "reply_signal_mix",
            "confidence": 4,
            "note": (
                f"本轮猎聘 IM 可见回复 {total} 条：高置信 {confidence_counts.get('high', 0)} 条，"
                f"中置信 {confidence_counts.get('medium', 0)} 条，低置信 {confidence_counts.get('low', 0)} 条，"
                f"待确认 {confidence_counts.get('unmatched', 0)} 条。意图分布："
                + "、".join(f"{key} {value}" for key, value in sorted(intent_counts.items()))
                + "。优先先处理高/中置信正向回复，再批量补低置信岗位信息。"
            ),
        }
    )

    if high_rows:
        examples = "；".join(
            f"{row['candidate_name']}->{(row['inferred_client'] or '').strip()}/{(row['inferred_position'] or '').strip()}"
            for row in high_rows[:5]
        )
        notes.append(
            {
                "client": "",
                "position": "",
                "topic": "direct_position_keyword_mapping",
                "confidence": 5,
                "note": (
                    "候选人原话直接包含岗位名时，可以高优先级映射到项目；"
                    f"本轮样例：{examples}。后续搜索和话术里应保留岗位原词，减少泛化。"
                ),
            }
        )

    if low_rows:
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in low_rows:
            grouped[row["inferred_position"] or "未命名方向"].append(row["candidate_name"] or "未识别")
        low_text = "；".join(f"{position}: {', '.join(names[:4])}" for position, names in grouped.items())
        notes.append(
            {
                "client": "",
                "position": "",
                "topic": "low_confidence_bucket_review",
                "confidence": 3,
                "note": (
                    "低置信回复主要缺客户名或岗位名，需要人工确认后再挂库。"
                    f"当前低置信方向：{low_text}。建议在跟进话术里先补客户、工作年限、地点、薪资四个确认点。"
                ),
            }
        )

    if unmatched_rows:
        examples = "；".join(
            f"{row['candidate_name']}: {str(row['raw_text'])[:28]}"
            for row in unmatched_rows[:5]
        )
        notes.append(
            {
                "client": "",
                "position": "",
                "topic": "unmatched_reply_triage",
                "confidence": 3,
                "note": (
                    "待确认回复多为拒绝、泛称姓名、薪资可谈或只发简历，不能安全自动匹配项目。"
                    f"本轮样例：{examples}。下一步应优先从 IM 详情或候选人主页补当前沟通岗位。"
                ),
            }
        )

    return notes


def write_report(notes: list[dict], inserted: int, output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复学习记录_{stamp}.md"
    lines = [
        "# 猎聘回复学习记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- 本次新增学习笔记：{inserted}",
        f"- 本次识别主题：{len(notes)}",
        "",
        "## 笔记",
        "",
    ]
    for item in notes:
        lines.extend(
            [
                f"### {item['topic']}",
                "",
                f"- 置信度：{item['confidence']}/5",
                f"- 内容：{item['note']}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Record learning notes from enhanced Liepin replies.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    source = f"liepin_reply_learning:{today}"

    conn = connect_db(db_path)
    try:
        rows = load_replies(conn)
        notes = build_notes(rows)
        inserted = 0
        for item in notes:
            inserted += int(
                insert_note(
                    conn,
                    client=item["client"],
                    position=item["position"],
                    topic=item["topic"],
                    note=item["note"],
                    source=source,
                    confidence=item["confidence"],
                    today=today,
                )
            )
        conn.commit()
    finally:
        conn.close()

    report = write_report(notes, inserted, output_dir)
    print(
        json.dumps(
            {
                "ok": True,
                "db": str(db_path),
                "notes": len(notes),
                "inserted": inserted,
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
