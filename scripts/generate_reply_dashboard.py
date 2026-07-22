#!/usr/bin/env python3
"""Generate a local dashboard report for Liepin reply follow-ups."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            t.id,
            ifnull(t.candidate_name, '') AS candidate_name,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.priority, 2) AS priority,
            ifnull(t.due_at, '') AS due_at,
            ifnull(t.status, 'open') AS status,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.match_reason, '') AS match_reason,
            ifnull(t.lane_tag, '') AS lane_tag,
            ifnull(t.lane_reason, '') AS lane_reason,
            ifnull(t.draft_message, '') AS draft_message,
            ifnull(t.confirmed_client, r.confirmed_client) AS confirmed_client,
            ifnull(t.confirmed_position, r.confirmed_position) AS confirmed_position,
            ifnull(t.confirmation_status, r.confirmation_status) AS confirmation_status,
            ifnull(t.confirmed_at, r.confirmed_at) AS confirmed_at,
            ifnull(r.intent, '') AS intent,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.candidate_title, '') AS candidate_title,
            ifnull(r.suggested_next_action, t.reason) AS suggested_next_action
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        ORDER BY
            CASE WHEN ifnull(t.status, 'open') = 'open' THEN 0 ELSE 1 END,
            ifnull(t.priority, 2) ASC,
            CASE ifnull(t.match_confidence, 'unmatched')
                WHEN 'confirmed' THEN 0
                WHEN 'high' THEN 0
                WHEN 'medium' THEN 1
                WHEN 'low' THEN 2
                ELSE 3
            END,
            ifnull(t.due_at, '') ASC,
            t.id DESC
        """
    ).fetchall()


def load_learning_notes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT topic, note, confidence, created_at
        FROM learning_notes
        ORDER BY id DESC
        LIMIT 6
        """
    ).fetchall()


def label_intent(intent: str) -> str:
    return {
        "interested": "高意向",
        "short_confirmation": "短确认",
        "self_recommendation": "主动投递",
        "targeted_interest": "定向兴趣",
        "need_more_info": "要信息",
        "need_contact": "要联系",
        "salary_concern": "薪资",
        "location_concern": "地点",
        "not_interested": "拒绝",
    }.get(intent, intent or "未分类")


def label_task(task_type: str) -> str:
    return {
        "call_candidate": "约电话",
        "light_touch_followup": "轻跟进",
        "self_recommendation_followup": "主动投递跟进",
        "targeted_interest_followup": "定向岗位跟进",
        "send_job_info": "补岗位信息",
        "exchange_contact": "转联系方式",
        "salary_followup": "薪资确认",
        "location_followup": "地点确认",
        "record_rejection": "记录拒绝",
        "review_reply": "人工复核",
    }.get(task_type, task_type or "待处理")


def project_text(row: sqlite3.Row) -> str:
    client = row["confirmed_client"] or row["inferred_client"]
    position = row["confirmed_position"] or row["inferred_position"]
    if client and position:
        return f"{client}/{position}"
    if position:
        return position
    if client:
        return client
    return "待确认"


def confidence_value(row: sqlite3.Row) -> str:
    if row["confirmation_status"] == "confirmed" and row["confirmed_client"] and row["confirmed_position"]:
        return "confirmed"
    return row["match_confidence"] or "unmatched"


def confidence_label(confidence: str) -> str:
    return {
        "confirmed": "已确认",
        "high": "高",
        "medium": "中",
        "low": "低",
        "unmatched": "待确认",
    }.get(confidence, confidence or "待确认")


def first_line(text: str, limit: int = 64) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("..." if len(clean) > limit else "")


def write_report(tasks: list[sqlite3.Row], notes: list[sqlite3.Row], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘回复智能驾驶舱_{stamp}.md"
    open_tasks = [row for row in tasks if row["status"] == "open"]
    confidence_counts = Counter(confidence_value(row) for row in open_tasks)
    intent_counts = Counter(row["intent"] or "unclear" for row in open_tasks)
    confirmed_rows = [row for row in open_tasks if confidence_value(row) == "confirmed"]
    high_medium = [row for row in open_tasks if confidence_value(row) in ("high", "medium")]
    low_rows = [row for row in open_tasks if confidence_value(row) == "low"]
    unmatched_rows = [row for row in open_tasks if confidence_value(row) in ("", "unmatched")]
    light_touch_rows = [row for row in open_tasks if row["task_type"] == "light_touch_followup"]
    fast_lane_rows = [row for row in open_tasks if row["lane_tag"] == "fast_lane"]
    positive_rows = [
        row for row in open_tasks
        if row["intent"] in ("interested", "self_recommendation", "targeted_interest", "need_contact", "need_more_info", "salary_concern")
    ]

    lines = [
        "# 猎聘回复智能驾驶舱",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前状态",
        "",
        f"- 打开待办：{len(open_tasks)}",
        f"- 已确认可直接推进：{len(confirmed_rows)}",
        f"- 高/中置信可优先推进：{len(high_medium)}",
        f"- 低置信需要补项目：{len(low_rows)}",
        f"- 仍需人工确认：{len(unmatched_rows)}",
        f"- 快推池：{len(fast_lane_rows)}",
        f"- 轻跟进池：{len(light_touch_rows)}",
        f"- 正向或可继续沟通：{len(positive_rows)}",
        f"- 意图分布：{'、'.join(f'{label_intent(k)} {v}' for k, v in sorted(intent_counts.items()))}",
        "",
        "## 先处理这几条",
        "",
        "| 优先 | 候选人 | 意图 | 项目判断 | 置信 | 建议动作 | 原话摘要 |",
        "|---:|---|---|---|---|---|---|",
    ]
    for row in open_tasks[:8]:
        lines.append(
            "| P{priority} | {name} | {intent} | {project} | {confidence} | {action} | {raw} |".format(
                priority=row["priority"],
                name=(row["candidate_name"] or "未识别").replace("|", "｜"),
                intent=label_intent(row["intent"]).replace("|", "｜"),
                project=project_text(row).replace("|", "｜"),
                confidence=confidence_label(confidence_value(row)),
                action=first_line(row["suggested_next_action"], 42).replace("|", "｜"),
                raw=first_line(row["raw_text"], 44).replace("|", "｜"),
            )
        )

    lines.extend(["", "## 已确认与高/中置信推进", ""])
    if confirmed_rows or high_medium:
        for row in confirmed_rows + high_medium:
            lines.append(
                f"- {row['candidate_name'] or '未识别'}：{project_text(row)}，"
                f"{confidence_label(confidence_value(row))}；建议先{label_task(row['task_type'])}。"
            )
    else:
        lines.append("- 暂无已确认或高/中置信待办。")

    lines.extend(["", "## 快推池", ""])
    if fast_lane_rows:
        for row in fast_lane_rows[:12]:
            lines.append(
                f"- {row['candidate_name'] or '未识别'}：{project_text(row)}；"
                f"{row['lane_reason'] or '可优先推进。'}"
            )
    else:
        lines.append("- 暂无快推待办。")

    lines.extend(["", "## 待补确认", ""])
    if low_rows or unmatched_rows:
        for row in (low_rows + unmatched_rows)[:10]:
            lines.append(
                f"- {row['candidate_name'] or '未识别'}：{project_text(row)}，"
                f"{confidence_label(confidence_value(row))}；要补：客户名/岗位名/地点/薪资或候选人主页。"
            )
    else:
        lines.append("- 暂无需要补确认的待办。")

    lines.extend(["", "## 轻跟进池", ""])
    if light_touch_rows:
        for row in light_touch_rows[:12]:
            lines.append(
                f"- {row['candidate_name'] or '未识别'}：{first_line(row['raw_text'], 24)}；"
                f"先补岗位信息，再追一个小问题。"
            )
    else:
        lines.append("- 暂无轻跟进待办。")

    lines.extend(["", "## 最新学习", ""])
    if notes:
        for note in notes:
            lines.append(f"- {note['topic']}（{note['confidence']}/5）：{first_line(note['note'], 110)}")
    else:
        lines.append("- 暂无学习记录。")

    lines.extend(
        [
            "",
            "## 下一步建议",
            "",
            "1. 先处理“已确认”和“高/中”项目，优先推进明确回复。",
            "2. 再筛选“低”，逐条补客户名和岗位名，确认后再推进话术。",
            "3. 轻跟进池不抢主优先级，先发岗位锚点，再看是否升温。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a dashboard for Liepin reply follow-ups.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_db(db_path)
    try:
        tasks = load_tasks(conn)
        notes = load_learning_notes(conn)
    finally:
        conn.close()

    report = write_report(tasks, notes, output_dir)
    open_tasks = [row for row in tasks if row["status"] == "open"]
    counts = Counter(confidence_value(row) for row in open_tasks)
    print(
        json.dumps(
            {
                "ok": True,
                "db": str(db_path),
                "open_tasks": len(open_tasks),
                "confidence_counts": dict(counts),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
