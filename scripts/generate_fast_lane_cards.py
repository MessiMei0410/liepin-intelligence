#!/usr/bin/env python3
"""Generate today's fast-lane reply cards for Liepin follow-up."""

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
            ifnull(t.lane_reason, '') AS lane_reason,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.match_confidence, '') AS match_confidence,
            ifnull(t.confirmation_status, '') AS confirmation_status,
            ifnull(t.talk_score, 0) AS talk_score,
            ifnull(t.talk_risk, '') AS talk_risk,
            ifnull(t.talk_missing, '') AS talk_missing,
            ifnull(t.draft_message, '') AS draft_message,
            ifnull(t.confirmed_client, ifnull(t.inferred_client, ifnull(t.client, ''))) AS client,
            ifnull(t.confirmed_position, ifnull(t.inferred_position, ifnull(t.position, ''))) AS position,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.candidate_title, '') AS candidate_title
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
          ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open'
          AND ifnull(t.lane_tag, '') = 'fast_lane'
          AND (
            (ifnull(t.confirmation_status, '') = 'confirmed'
              AND (ifnull(t.confirmed_client, '') <> '' OR ifnull(t.confirmed_position, '') <> ''))
            OR ifnull(t.match_confidence, '') IN ('confirmed', 'high')
            OR (
              ifnull(t.match_confidence, '') = 'medium'
              AND ifnull(t.confirmed_client, ifnull(t.inferred_client, ifnull(t.client, ''))) <> ''
            )
          )
          AND (
            ifnull(t.confirmed_client, ifnull(t.inferred_client, ifnull(t.client, ''))) <> ''
            OR ifnull(t.confirmed_position, ifnull(t.inferred_position, ifnull(t.position, ''))) <> ''
          )
          AND ifnull(t.talk_score, 0) >= 72
        ORDER BY ifnull(t.talk_score, 0) DESC, t.id DESC
        LIMIT 7
        """
    ).fetchall()


def confidence(row: sqlite3.Row) -> str:
    if row["confirmation_status"] == "confirmed" and (row["client"] or row["position"]):
        return "confirmed"
    return row["match_confidence"] or "待确认"


def ready_message(row: sqlite3.Row) -> str:
    name = row["candidate_name"]
    client = row["client"]
    position = row["position"]
    if client and position:
        return f"{name}，您好，您这边和{client}的{position}挺匹配的，方便今天找个 10 分钟电话快速沟通下吗？"
    if position:
        return f"{name}，您好，您这边和{position}方向挺贴的，方便今天找个 10 分钟电话快速沟通下吗？"
    return f"{name}，您好，您这段背景我这边想继续往下推进，方便今天找个 10 分钟电话快速沟通下吗？"


def project_text(row: sqlite3.Row) -> str:
    if row["client"] and row["position"]:
        return f"{row['client']}/{row['position']}"
    if row["position"]:
        return row["position"]
    if row["client"]:
        return row["client"]
    return "待补项目"


def write_report(output_dir: Path, rows: list[sqlite3.Row]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘今日7条快推卡片_{stamp}.md"
    lines = [
        "# 猎聘今日 7 条快推卡片",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "- 进入本清单的硬条件：项目置信度为已确认/高/中，且有客户或岗位锚点，且话术分数不低于 72。",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "当前暂无满足硬条件的快推卡片。",
                "",
                "建议先看“先补锚点”清单，把客户名、岗位名或候选人主页补齐后再重跑。",
                "",
            ]
        )
    for idx, row in enumerate(rows, start=1):
        lines.extend(
            [
                f"## {idx}. {row['candidate_name']}",
                "",
                f"- 项目：{project_text(row)}",
                f"- 置信：{confidence(row)}",
                f"- 分数：{row['talk_score']}",
                f"- 风险：{row['talk_risk'] or '无明显风险'}",
                f"- 缺失：{row['talk_missing'] or '无'}",
                f"- 为什么先回：{row['lane_reason'] or '热度高，适合优先承接。'}",
                f"- 对方原话：{row['raw_text']}",
                f"- 可直接发：{ready_message(row)}",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate today's 7 fast-lane cards.")
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
