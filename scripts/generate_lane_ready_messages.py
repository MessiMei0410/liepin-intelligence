#!/usr/bin/env python3
"""Generate copy-ready short messages for fast-lane and anchor-first tasks."""

from __future__ import annotations

import argparse
import json
import sqlite3
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
            t.id,
            ifnull(t.candidate_name, '') AS candidate_name,
            ifnull(t.lane_tag, '') AS lane_tag,
            ifnull(t.lane_reason, '') AS lane_reason,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.confirmation_status, '') AS confirmation_status,
            ifnull(t.talk_score, 0) AS talk_score,
            ifnull(t.talk_risk, '') AS talk_risk,
            ifnull(t.talk_missing, '') AS talk_missing,
            ifnull(t.confirmed_client, ifnull(t.inferred_client, ifnull(t.client, ''))) AS client,
            ifnull(t.confirmed_position, ifnull(t.inferred_position, ifnull(t.position, ''))) AS position,
            ifnull(t.draft_message, '') AS draft_message,
            ifnull(r.candidate_title, '') AS candidate_title,
            ifnull(r.raw_text, '') AS raw_text
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
          AND ifnull(t.lane_tag, '') IN ('fast_lane', 'regular_lane', 'anchor_first')
        ORDER BY
            CASE ifnull(t.lane_tag, '') WHEN 'fast_lane' THEN 0 WHEN 'regular_lane' THEN 1 ELSE 2 END,
            t.id DESC
        """
    ).fetchall()


def effective_confidence(row: sqlite3.Row) -> str:
    if row["confirmation_status"] == "confirmed" and (row["client"] or row["position"]):
        return "confirmed"
    return row["match_confidence"] or "unmatched"


def can_direct_send(row: sqlite3.Row) -> bool:
    return (
        row["lane_tag"] == "fast_lane"
        and (
            effective_confidence(row) in {"confirmed", "high"}
            or (effective_confidence(row) == "medium" and bool(row["client"]))
        )
        and bool(row["client"] or row["position"])
        and int(row["talk_score"] or 0) >= 72
    )


def short_name(name: str) -> str:
    return name if name.endswith(("先生", "女士", "老师")) else f"{name}"


def ready_message(row: sqlite3.Row) -> str:
    name = short_name(row["candidate_name"])
    client = row["client"]
    position = row["position"]
    lane = row["lane_tag"]

    if lane == "fast_lane" and can_direct_send(row):
        if client and position:
            return f"{name}，您好，您这边和{client}的{position}挺匹配的，方便今天找个 10 分钟电话快速沟通下吗？"
        if position:
            return f"{name}，您好，您这边和{position}方向挺贴的，方便今天找个 10 分钟电话快速沟通下吗？"

    if client and position:
        return f"{name}，您好，我先把{client}的{position}核心方向和您说下，您更关心岗位方向、客户背景还是地点？"
    if position:
        return f"{name}，您好，我先把{position}的核心方向和您说下，您更关心岗位方向、客户背景还是地点？"
    return f"{name}，您好，我先把这个机会的核心方向和您说清，您更关心岗位方向、客户背景还是地点？"


def write_report(output_dir: Path, rows: list[sqlite3.Row]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘分层可直接发送话术_{stamp}.md"
    lines = [
        "# 猎聘分层可直接发送话术",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 可直接发",
        "",
    ]
    direct_rows = [r for r in rows if can_direct_send(r)]
    if direct_rows:
        for row in direct_rows:
            project = f"{row['client'] or '待补客户'}/{row['position'] or '待补岗位'}"
            lines.append(
                f"- #{row['id']}｜{row['candidate_name']}｜{project}｜置信 {effective_confidence(row)}"
                f"｜分数 {row['talk_score']}｜{ready_message(row)}"
            )
    else:
        lines.append("- 暂无可直接发送的快推话术。")

    lines.extend(["", "## 发前看一眼", ""])
    review_rows = [r for r in rows if r["lane_tag"] == "regular_lane" or (r["lane_tag"] == "fast_lane" and not can_direct_send(r))]
    if review_rows:
        for row in review_rows:
            lines.append(
                f"- #{row['id']}｜{row['candidate_name']}｜置信 {effective_confidence(row)}"
                f"｜分数 {row['talk_score']}｜风险：{row['talk_risk'] or row['lane_reason'] or '需人工确认'}"
                f"｜建议：{ready_message(row)}"
            )
    else:
        lines.append("- 暂无需要人工复核的快推话术。")

    lines.extend(["", "## 先补锚点", ""])
    for row in [r for r in rows if r["lane_tag"] == "anchor_first"]:
        lines.append(f"- #{row['id']}｜{row['candidate_name']}｜缺：{row['talk_missing'] or '客户/岗位锚点'}｜{ready_message(row)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate copy-ready lane messages.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(Path(args.db).expanduser())
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    report = write_report(output_dir, rows)
    print(json.dumps({"ok": True, "rows": len(rows), "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
