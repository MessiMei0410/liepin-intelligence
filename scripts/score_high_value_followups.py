#!/usr/bin/env python3
"""Score self-recommendation and targeted-interest tasks into fast-lane buckets."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


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
            ifnull(t.candidate_name, '') AS candidate_name,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.priority, 2) AS priority,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.confirmation_status, '') AS confirmation_status,
            ifnull(t.talk_score, 0) AS talk_score,
            ifnull(t.talk_strategy, '') AS talk_strategy,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.confirmed_client, '') AS confirmed_client,
            ifnull(t.confirmed_position, '') AS confirmed_position,
            ifnull(r.intent, '') AS intent,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.candidate_title, '') AS candidate_title
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
          AND ifnull(t.task_type, '') IN ('self_recommendation_followup', 'targeted_interest_followup')
        ORDER BY ifnull(t.talk_score, 0) DESC, t.priority ASC, t.id DESC
        """
    ).fetchall()


def has_anchor(row: sqlite3.Row) -> bool:
    return any(row[key] for key in ("inferred_client", "inferred_position", "confirmed_client", "confirmed_position"))


def has_strong_anchor(row: sqlite3.Row) -> bool:
    return bool(row["confirmed_position"] or row["inferred_position"])


def effective_confidence(row: sqlite3.Row) -> str:
    if row["confirmation_status"] == "confirmed" and (row["confirmed_client"] or row["confirmed_position"]):
        return "confirmed"
    return row["match_confidence"] or "unmatched"


def classify_lane(row: sqlite3.Row) -> tuple[str, str]:
    confidence = effective_confidence(row)
    score = int(row["talk_score"] or 0)
    task_type = row["task_type"] or ""
    raw = row["raw_text"] or ""
    anchor = has_anchor(row)

    if (
        confidence in ("confirmed", "high")
        or (confidence == "medium" and bool(row["confirmed_client"] or row["inferred_client"]))
    ) and has_strong_anchor(row) and score >= 72:
        return "fast_lane", "项目已明确或置信度足够，且候选人表达积极，可优先往前推。"
    if confidence == "medium" and has_strong_anchor(row) and score >= 72:
        return "regular_lane", "岗位锚点较明确，但客户名仍缺失；发前看一眼，不直接进快推。"
    if confidence in ("low", "unmatched", "created", ""):
        if task_type == "targeted_interest_followup" and ("职位" in raw or "岗位" in raw):
            return "anchor_first", "候选人有岗位兴趣，但项目置信度不足；先补客户/岗位锚点，不直接约电话。"
        if task_type == "self_recommendation_followup":
            return "anchor_first", "候选人主动投递但项目未确认；先补客户或岗位锚点，再决定是否升级。"
        return "anchor_first", "项目置信度不足，不进入快推；先确认客户、岗位或候选人主页。"
    if task_type == "self_recommendation_followup" and anchor and score >= 55:
        return "regular_lane", "主动投递且已有岗位锚点，但未达到快推线，适合常规推进。"
    if score >= 45:
        return "regular_lane", "候选人有一定热度，但还不适合直接约电话。"
    return "anchor_first", "先补客户或岗位锚点，再决定是否升级优先级。"


def save(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, object]:
    try:
        conn.execute("ALTER TABLE followup_tasks ADD COLUMN lane_tag TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE followup_tasks ADD COLUMN lane_reason TEXT")
    except sqlite3.OperationalError:
        pass

    lane_counts = Counter()
    tagged: list[dict[str, object]] = []
    for row in rows:
        lane, reason = classify_lane(row)
        lane_counts[lane] += 1
        conn.execute(
            """
            UPDATE followup_tasks
            SET lane_tag = ?,
                lane_reason = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (lane, reason, row["task_id"]),
        )
        tagged.append(
            {
                "task_id": row["task_id"],
                "candidate_name": row["candidate_name"],
                "task_type": row["task_type"],
                "match_confidence": effective_confidence(row),
                "talk_score": int(row["talk_score"] or 0),
                "lane_tag": lane,
                "lane_reason": reason,
                "raw_text": row["raw_text"],
            }
        )
    conn.commit()
    return {"lane_counts": dict(lane_counts), "tagged": tagged}


def write_report(output_dir: Path, result: dict[str, object]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘高价值跟进分层_{stamp}.md"
    tagged = result["tagged"]
    lines = [
        "# 猎聘高价值跟进分层",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 分层统计：{'、'.join(f'{k} {v}' for k, v in result['lane_counts'].items())}",
        "",
        "## 快推",
        "",
    ]
    for item in [x for x in tagged if x["lane_tag"] == "fast_lane"][:20]:
        lines.append(
            f"- #{item['task_id']}｜{item['candidate_name']}｜{item['task_type']}｜分数{item['talk_score']}｜{item['raw_text']}"
        )
    lines.extend(["", "## 常规推进", ""])
    for item in [x for x in tagged if x["lane_tag"] == "regular_lane"][:20]:
        lines.append(
            f"- #{item['task_id']}｜{item['candidate_name']}｜{item['task_type']}｜分数{item['talk_score']}｜{item['raw_text']}"
        )
    lines.extend(["", "## 先补锚点", ""])
    for item in [x for x in tagged if x["lane_tag"] == "anchor_first"][:20]:
        lines.append(
            f"- #{item['task_id']}｜{item['candidate_name']}｜{item['task_type']}｜分数{item['talk_score']}｜{item['raw_text']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Score high-value follow-up tasks into fast lanes.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db).expanduser())
    try:
        rows = load_rows(conn)
        result = save(conn, rows)
    finally:
        conn.close()
    report = write_report(output_dir, result)
    print(json.dumps({"ok": True, "total": len(rows), **result, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
