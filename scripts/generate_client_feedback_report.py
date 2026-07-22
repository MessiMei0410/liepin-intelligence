#!/usr/bin/env python3
"""Generate a client-feedback loop report."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from record_client_feedback import DEFAULT_DB, DEFAULT_OUTPUT_DIR, LABEL_BY_FEEDBACK, connect, ensure_schema


POSITIVE_FEEDBACK = {"approved", "interviewing", "interview_passed", "offer", "hired"}
NEGATIVE_FEEDBACK = {"rejected", "interview_failed", "eliminated"}


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def first_line(text: Any, limit: int = 80) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def parse_tags(text: str | None) -> list[str]:
    try:
        value = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    return [clean(item) for item in value if clean(item)] if isinstance(value, list) else []


def project_label(client: str, position: str) -> str:
    client = clean(client)
    position = clean(position)
    if client and position:
        return f"{client}/{position}"
    if client:
        return f"{client}/未定岗位"
    if position:
        return f"未定客户/{position}"
    return "未定客户/未定岗位"


def load_feedback(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM client_feedback_events
        ORDER BY datetime(feedback_time) DESC, id DESC
        """
    ).fetchall()


def load_candidate_status(conn: sqlite3.Connection) -> Counter:
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(status, ''), 'new') AS status, COUNT(*) AS count
        FROM candidates
        GROUP BY COALESCE(NULLIF(status, ''), 'new')
        ORDER BY count DESC
        """
    ).fetchall()
    return Counter({row["status"]: int(row["count"] or 0) for row in rows})


def aggregate_by_project(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[project_label(row["client"], row["position"])].append(row)
    result = []
    for project, items in groups.items():
        result.append(
            {
                "project": project,
                "total": len(items),
                "positive": sum(1 for item in items if item["feedback_type"] in POSITIVE_FEEDBACK),
                "negative": sum(1 for item in items if item["feedback_type"] in NEGATIVE_FEEDBACK),
                "latest": max(clean(item["feedback_time"]) for item in items),
            }
        )
    return sorted(result, key=lambda item: (item["positive"], item["total"], item["latest"]), reverse=True)


def write_report(rows: list[sqlite3.Row], candidate_status: Counter, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘客户反馈闭环_{stamp}.md"

    type_counts = Counter(row["feedback_type"] for row in rows)
    status_after = Counter(row["status_after"] or "未更新" for row in rows)
    tag_counts = Counter(tag for row in rows for tag in parse_tags(row["reason_tags_json"]))
    projects = aggregate_by_project(rows)
    positive = sum(1 for row in rows if row["feedback_type"] in POSITIVE_FEEDBACK)
    negative = sum(1 for row in rows if row["feedback_type"] in NEGATIVE_FEEDBACK)

    lines = [
        "# 猎聘客户反馈闭环",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前状态",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "- 暂无客户反馈事件。",
                "- 客户认可、否决、面试、offer、入职等结果后，双击 `记录客户反馈.command` 录入。",
                "- 有反馈后，这里会开始统计哪些岗位、候选人特征和搜索方向更有效。",
            ]
        )
    else:
        lines.extend(
            [
                f"- 已记录客户反馈：{len(rows)} 条",
                f"- 正向反馈：{positive}",
                f"- 负向反馈：{negative}",
                f"- 反馈类型：{'、'.join(f'{LABEL_BY_FEEDBACK.get(k, k)} {v}' for k, v in type_counts.items())}",
                f"- 更新后状态：{'、'.join(f'{k} {v}' for k, v in status_after.items())}",
                f"- 常见原因：{'、'.join(f'{k} {v}' for k, v in tag_counts.most_common(8)) or '暂无'}",
            ]
        )

    lines.extend(
        [
            "",
            "## 候选人状态",
            "",
            f"- {'、'.join(f'{key} {value}' for key, value in candidate_status.most_common(10)) or '暂无'}",
            "",
            "## 项目反馈",
            "",
        ]
    )
    if projects:
        lines.extend(["| 项目 | 反馈 | 正向 | 负向 | 最新时间 |", "|---|---:|---:|---:|---|"])
        for item in projects[:16]:
            lines.append(
                f"| {item['project'].replace('|', '｜')} | {item['total']} | {item['positive']} | "
                f"{item['negative']} | {item['latest']} |"
            )
    else:
        lines.append("- 暂无项目反馈。")

    lines.extend(["", "## 最新反馈", ""])
    if rows:
        lines.extend(["| 时间 | 候选人 | 项目 | 类型 | 原因 | 下一步 |", "|---|---|---|---|---|---|"])
        for row in rows[:12]:
            tags = "、".join(parse_tags(row["reason_tags_json"])) or first_line(row["feedback_detail"], 28) or "无"
            lines.append(
                f"| {row['feedback_time']} | {row['candidate_name']} | "
                f"{project_label(row['client'], row['position']).replace('|', '｜')} | "
                f"{LABEL_BY_FEEDBACK.get(row['feedback_type'], row['feedback_type'])} | "
                f"{tags.replace('|', '｜')} | {first_line(row['next_action'], 30).replace('|', '｜') or '未填'} |"
            )
    else:
        lines.append("- 暂无。")

    lines.extend(
        [
            "",
            "## 下一步建议",
            "",
        ]
    )
    if not rows:
        lines.append("1. 下一次客户给出认可/否决/面试反馈时，先录入一条反馈事件。")
    else:
        lines.append("1. 把正向反馈沉淀成下一轮搜索的正样本。")
        lines.append("2. 把负向反馈的原因标签用于降权关键词、公司或候选人类型。")
        lines.append("3. 对进入面试/offer 的人选，及时更新后续状态。")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a client feedback loop report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        rows = load_feedback(conn)
        candidate_status = load_candidate_status(conn)
    finally:
        conn.close()

    report = write_report(rows, candidate_status, Path(args.output_dir).expanduser())
    print(
        json.dumps(
            {
                "ok": True,
                "feedback_events": len(rows),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
