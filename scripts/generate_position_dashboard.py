#!/usr/bin/env python3
"""Generate a position-level dashboard for the Liepin intelligence workflow."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from position_storage import (
    ensure_position_storage_schema,
    fetch_latest_position_snapshot,
    fetch_position_assets,
    fetch_position_snapshots,
    table_exists,
)


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

POSITIVE_INTENTS = {"interested", "need_contact", "need_more_info", "salary_concern"}
ACTIVE_STATUSES = {"new", "recommended", "contacted", "replied", "interviewing", "offered", "greeted"}
ADVANCED_STATUSES = {"recommended", "contacted", "replied", "interviewing", "offered", "hired", "greeted"}
HIGH_CONFIDENCE = {"confirmed", "high", "medium"}
POSITIVE_FEEDBACK = {"approved", "interviewing", "interview_passed", "offer", "hired"}
NEGATIVE_FEEDBACK = {"rejected", "interview_failed", "eliminated"}
FEEDBACK_LABELS = {
    "approved": "客户认可",
    "rejected": "客户否决",
    "interviewing": "安排面试",
    "interview_passed": "面试通过",
    "interview_failed": "面试未过",
    "offer": "进入 offer",
    "hired": "已入职",
    "hold": "客户暂缓",
    "eliminated": "淘汰",
}


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


def safe_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def display_project(client: str, position: str) -> str:
    client = clean(client)
    position = clean(position)
    if client and position:
        return f"{client}/{position}"
    if client:
        return f"{client}/未定岗位"
    if position:
        return f"未定客户/{position}"
    return "未定客户/未定岗位"


def project_key(client: str, position: str) -> tuple[str, str]:
    return (clean(client), clean(position))


def row_project(row: sqlite3.Row) -> tuple[str, str]:
    client = clean(row["confirmed_client"] if "confirmed_client" in row.keys() else "")
    position = clean(row["confirmed_position"] if "confirmed_position" in row.keys() else "")
    if client or position:
        return project_key(client, position)

    client = clean(row["inferred_client"] if "inferred_client" in row.keys() else "")
    position = clean(row["inferred_position"] if "inferred_position" in row.keys() else "")
    if client or position:
        return project_key(client, position)

    return project_key(
        row["client"] if "client" in row.keys() else "",
        row["position"] if "position" in row.keys() else "",
    )


def first_line(text: Any, limit: int = 58) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def empty_project(client: str, position: str) -> dict[str, Any]:
    return {
        "client": clean(client),
        "position": clean(position),
        "position_rows": 0,
        "open_position_rows": 0,
        "gap": 0,
        "candidates": 0,
        "active_candidates": 0,
        "advanced_candidates": 0,
        "recommended_candidates": 0,
        "interviewing_candidates": 0,
        "candidate_status": Counter(),
        "replies": 0,
        "positive_replies": 0,
        "reply_intents": Counter(),
        "feedback_events": 0,
        "positive_feedback": 0,
        "negative_feedback": 0,
        "hold_feedback": 0,
        "feedback_types": Counter(),
        "open_followups": 0,
        "high_confidence_followups": 0,
        "low_confidence_followups": 0,
        "task_types": Counter(),
        "intelligence_count": 0,
        "a_candidates": 0,
        "b_candidates": 0,
        "top_score": 0,
        "top_candidates": [],
        "search_experiments": 0,
        "search_viewed": 0,
        "search_extracted": 0,
        "outreach_events": 0,
        "latest_activity": "",
        "sources": set(),
    }


def touch_latest(item: dict[str, Any], value: str) -> None:
    value = clean(value)
    if value and value > item["latest_activity"]:
        item["latest_activity"] = value


def get_project(projects: dict[tuple[str, str], dict[str, Any]], client: str, position: str) -> dict[str, Any]:
    key = project_key(client, position)
    if key not in projects:
        projects[key] = empty_project(*key)
    return projects[key]


def load_positions(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "positions"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT
            client,
            title AS position,
            COUNT(*) AS position_rows,
            SUM(CASE WHEN COALESCE(status, 'open') = 'open' THEN 1 ELSE 0 END) AS open_position_rows,
            SUM(COALESCE(gap, 0)) AS gap,
            MAX(updated_at) AS latest_activity
        FROM positions
        GROUP BY client, title
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        item["position_rows"] += int(row["position_rows"] or 0)
        item["open_position_rows"] += int(row["open_position_rows"] or 0)
        item["gap"] += int(row["gap"] or 0)
        item["sources"].add("positions")
        touch_latest(item, row["latest_activity"])


def load_position_storage(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if table_exists(conn, "position_snapshots"):
        for row in safe_rows(
            conn,
            """
            SELECT client, position, COUNT(*) AS count, MAX(COALESCE(captured_at, created_at)) AS latest_activity
            FROM position_snapshots
            GROUP BY client, position
            """,
        ):
            item = get_project(projects, row["client"], row["position"])
            item["sources"].add("position_snapshots")
            touch_latest(item, row["latest_activity"])
    if table_exists(conn, "position_assets"):
        for row in safe_rows(
            conn,
            """
            SELECT client, position, COUNT(*) AS count, MAX(COALESCE(updated_at, created_at)) AS latest_activity
            FROM position_assets
            GROUP BY client, position
            """,
        ):
            item = get_project(projects, row["client"], row["position"])
            item["sources"].add("position_assets")
            touch_latest(item, row["latest_activity"])


def load_candidates(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "candidates"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT client, position, COALESCE(NULLIF(status, ''), 'new') AS status,
               COUNT(*) AS count, MAX(updated_at) AS latest_activity
        FROM candidates
        GROUP BY client, position, COALESCE(NULLIF(status, ''), 'new')
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        count = int(row["count"] or 0)
        status = clean(row["status"]) or "new"
        item["candidates"] += count
        item["candidate_status"][status] += count
        if status in ACTIVE_STATUSES:
            item["active_candidates"] += count
        if status in ADVANCED_STATUSES:
            item["advanced_candidates"] += count
        if status == "recommended":
            item["recommended_candidates"] += count
        if status == "interviewing":
            item["interviewing_candidates"] += count
        item["sources"].add("candidates")
        touch_latest(item, row["latest_activity"])


def load_replies(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "candidate_replies"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT
            client, position, inferred_client, inferred_position, confirmed_client, confirmed_position,
            COALESCE(NULLIF(intent, ''), 'unclear') AS intent,
            COUNT(*) AS count,
            MAX(COALESCE(message_time, created_at)) AS latest_activity
        FROM candidate_replies
        GROUP BY client, position, inferred_client, inferred_position, confirmed_client, confirmed_position,
                 COALESCE(NULLIF(intent, ''), 'unclear')
        """,
    ):
        client, position = row_project(row)
        item = get_project(projects, client, position)
        count = int(row["count"] or 0)
        intent = clean(row["intent"]) or "unclear"
        item["replies"] += count
        item["reply_intents"][intent] += count
        if intent in POSITIVE_INTENTS:
            item["positive_replies"] += count
        item["sources"].add("replies")
        touch_latest(item, row["latest_activity"])


def load_followups(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "followup_tasks"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT
            client, position, inferred_client, inferred_position, confirmed_client, confirmed_position,
            COALESCE(NULLIF(status, ''), 'open') AS status,
            COALESCE(NULLIF(match_confidence, ''), 'unmatched') AS match_confidence,
            COALESCE(NULLIF(task_type, ''), 'review_reply') AS task_type,
            COUNT(*) AS count,
            MAX(updated_at) AS latest_activity
        FROM followup_tasks
        GROUP BY client, position, inferred_client, inferred_position, confirmed_client, confirmed_position,
                 COALESCE(NULLIF(status, ''), 'open'),
                 COALESCE(NULLIF(match_confidence, ''), 'unmatched'),
                 COALESCE(NULLIF(task_type, ''), 'review_reply')
        """,
    ):
        client, position = row_project(row)
        item = get_project(projects, client, position)
        count = int(row["count"] or 0)
        if clean(row["status"]) == "open":
            item["open_followups"] += count
            if clean(row["match_confidence"]) in HIGH_CONFIDENCE:
                item["high_confidence_followups"] += count
            else:
                item["low_confidence_followups"] += count
        item["task_types"][clean(row["task_type"]) or "review_reply"] += count
        item["sources"].add("followups")
        touch_latest(item, row["latest_activity"])


def load_intelligence(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "candidate_intelligence"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT client, position, candidate_name, candidate_company, fit_score, fit_level,
               next_action, updated_at
        FROM candidate_intelligence
        ORDER BY fit_score DESC, updated_at DESC
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        score = int(row["fit_score"] or 0)
        level = clean(row["fit_level"])
        item["intelligence_count"] += 1
        item["top_score"] = max(item["top_score"], score)
        if level.startswith("A") or score >= 85:
            item["a_candidates"] += 1
        elif level.startswith("B") or score >= 75:
            item["b_candidates"] += 1
        if score >= 55 and len(item["top_candidates"]) < 5:
            item["top_candidates"].append(
                {
                    "name": clean(row["candidate_name"]) or "未识别",
                    "company": clean(row["candidate_company"]),
                    "score": score,
                    "level": level or "未评级",
                    "next_action": first_line(row["next_action"], 46),
                }
            )
        item["sources"].add("intelligence")
        touch_latest(item, row["updated_at"])


def load_search_experiments(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "search_experiments"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT client, position, COUNT(*) AS count,
               SUM(COALESCE(viewed_count, 0)) AS viewed_count,
               SUM(COALESCE(extracted_count, 0)) AS extracted_count,
               MAX(COALESCE(updated_at, run_time)) AS latest_activity
        FROM search_experiments
        GROUP BY client, position
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        item["search_experiments"] += int(row["count"] or 0)
        item["search_viewed"] += int(row["viewed_count"] or 0)
        item["search_extracted"] += int(row["extracted_count"] or 0)
        item["sources"].add("search")
        touch_latest(item, row["latest_activity"])


def load_outreach(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "outreach_events"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT client, position, COUNT(*) AS count, MAX(event_time) AS latest_activity
        FROM outreach_events
        GROUP BY client, position
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        item["outreach_events"] += int(row["count"] or 0)
        item["sources"].add("outreach")
        touch_latest(item, row["latest_activity"])


def load_client_feedback(conn: sqlite3.Connection, projects: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not table_exists(conn, "client_feedback_events"):
        return
    for row in safe_rows(
        conn,
        """
        SELECT client, position,
               COALESCE(NULLIF(feedback_type, ''), 'unlabeled') AS feedback_type,
               COUNT(*) AS count,
               MAX(COALESCE(feedback_time, created_at)) AS latest_activity
        FROM client_feedback_events
        GROUP BY client, position, COALESCE(NULLIF(feedback_type, ''), 'unlabeled')
        """,
    ):
        item = get_project(projects, row["client"], row["position"])
        count = int(row["count"] or 0)
        feedback_type = clean(row["feedback_type"]) or "unlabeled"
        item["feedback_events"] += count
        item["feedback_types"][feedback_type] += count
        if feedback_type in POSITIVE_FEEDBACK:
            item["positive_feedback"] += count
        elif feedback_type in NEGATIVE_FEEDBACK:
            item["negative_feedback"] += count
        elif feedback_type == "hold":
            item["hold_feedback"] += count
        item["sources"].add("client_feedback")
        touch_latest(item, row["latest_activity"])


def load_recent_activity(conn: sqlite3.Connection) -> list[dict[str, str]]:
    activity: list[dict[str, str]] = []
    if table_exists(conn, "candidate_replies"):
        for row in safe_rows(
            conn,
            """
            SELECT candidate_name, raw_text,
                   COALESCE(NULLIF(confirmed_client,''), NULLIF(inferred_client,''), NULLIF(client,''), '') AS client,
                   COALESCE(NULLIF(confirmed_position,''), NULLIF(inferred_position,''), NULLIF(position,''), '') AS position,
                   COALESCE(message_time, created_at) AS event_time,
                   '候选人回复' AS event_type
            FROM candidate_replies
            ORDER BY datetime(COALESCE(message_time, created_at)) DESC, id DESC
            LIMIT 8
            """,
        ):
            activity.append(
                {
                    "time": clean(row["event_time"]),
                    "type": clean(row["event_type"]),
                    "project": display_project(row["client"], row["position"]),
                    "name": clean(row["candidate_name"]) or "未识别",
                    "summary": first_line(row["raw_text"], 54),
                }
            )
    if table_exists(conn, "outreach_events"):
        for row in safe_rows(
            conn,
            """
            SELECT candidate_name, client, position, event_time, event_type, message_summary
            FROM outreach_events
            ORDER BY datetime(event_time) DESC, id DESC
            LIMIT 8
            """,
        ):
            activity.append(
                {
                    "time": clean(row["event_time"]),
                    "type": clean(row["event_type"]),
                    "project": display_project(row["client"], row["position"]),
                    "name": clean(row["candidate_name"]) or "未识别",
                    "summary": first_line(row["message_summary"], 54),
                }
            )
    if table_exists(conn, "client_feedback_events"):
        for row in safe_rows(
            conn,
            """
            SELECT candidate_name, client, position, feedback_type, feedback_detail,
                   next_action, COALESCE(feedback_time, created_at) AS event_time
            FROM client_feedback_events
            ORDER BY datetime(COALESCE(feedback_time, created_at)) DESC, id DESC
            LIMIT 8
            """,
        ):
            feedback_type = clean(row["feedback_type"]) or "unlabeled"
            detail = first_line(row["feedback_detail"] or row["next_action"], 42)
            activity.append(
                {
                    "time": clean(row["event_time"]),
                    "type": FEEDBACK_LABELS.get(feedback_type, "客户反馈"),
                    "project": display_project(row["client"], row["position"]),
                    "name": clean(row["candidate_name"]) or "未识别",
                    "summary": detail or FEEDBACK_LABELS.get(feedback_type, feedback_type),
                }
            )
    return sorted(activity, key=lambda item: item["time"], reverse=True)[:10]


def collect_projects(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    projects: dict[tuple[str, str], dict[str, Any]] = {}
    load_positions(conn, projects)
    load_position_storage(conn, projects)
    load_candidates(conn, projects)
    load_replies(conn, projects)
    load_followups(conn, projects)
    load_intelligence(conn, projects)
    load_search_experiments(conn, projects)
    load_outreach(conn, projects)
    load_client_feedback(conn, projects)
    recent_activity = load_recent_activity(conn)
    return list(projects.values()), recent_activity


def priority_score(item: dict[str, Any]) -> float:
    score = 0.0
    score += item["open_followups"] * 18
    score += item["positive_replies"] * 15
    score += item["positive_feedback"] * 16
    score += item["negative_feedback"] * 5
    score += item["hold_feedback"] * 3
    score += item["a_candidates"] * 14
    score += item["b_candidates"] * 8
    score += item["advanced_candidates"] * 5
    score += item["interviewing_candidates"] * 8
    score += min(item["gap"], 5) * 3
    score += min(item["active_candidates"], 20) * 0.7
    if item["gap"] and item["active_candidates"] < 3:
        score += 8
    if not item["client"] or not item["position"]:
        score += item["open_followups"] * 4
    return round(score, 1)


def next_action(item: dict[str, Any]) -> str:
    if not item["client"] or not item["position"]:
        return "先补客户/岗位归属"
    if item["positive_feedback"] and not item["open_followups"]:
        return "推进客户认可人选"
    if item["negative_feedback"] and not item["positive_feedback"]:
        return "复盘客户否决原因"
    if item["open_followups"]:
        return f"处理 {item['open_followups']} 个待办"
    if item["a_candidates"] or item["b_candidates"]:
        return "推进 A/B 人选"
    if item["gap"] and item["active_candidates"] < 3:
        return "补一轮定向搜索"
    if item["active_candidates"] and not item["advanced_candidates"]:
        return "从池子里筛可推荐人选"
    if item["search_experiments"] == 0:
        return "记录下一轮搜索实验"
    return "继续观察转化"


def risk_flags(item: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if not item["client"] or not item["position"]:
        flags.append("归属不清")
    if item["gap"] and item["active_candidates"] < 3:
        flags.append("池子薄")
    if item["low_confidence_followups"]:
        flags.append("低置信待办")
    if item["candidates"] and not item["intelligence_count"]:
        flags.append("未评分")
    if item["search_experiments"] == 0 and item["open_position_rows"]:
        flags.append("无搜索实验")
    if item["outreach_events"] == 0 and item["open_followups"]:
        flags.append("无触达记录")
    if item["negative_feedback"]:
        flags.append("有负向客户反馈")
    if item["hold_feedback"]:
        flags.append("客户暂缓")
    return flags


def label_status(counter: Counter, limit: int = 3) -> str:
    if not counter:
        return "暂无"
    return "、".join(f"{key} {value}" for key, value in counter.most_common(limit))


def sort_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        projects,
        key=lambda item: (
            priority_score(item),
            item["positive_replies"],
            item["a_candidates"] + item["b_candidates"],
            item["active_candidates"],
            item["gap"],
        ),
        reverse=True,
    )


def write_project_table(lines: list[str], projects: list[dict[str, Any]], limit: int = 40) -> None:
    lines.extend(
        [
            "| 分 | 岗位 | 缺口 | 人才池 | 推荐/面试 | A/B | 回复 | 反馈 | 待办 | 风险 | 下一步 |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    if not projects:
        lines.append("| - | 暂无 | - | - | - | - | - | - | - | - | - |")
        return
    for item in projects[:limit]:
        lines.append(
            "| {score} | {project} | {gap} | {pool} | {advanced}/{interviewing} | {ab} | {replies}/{positive} | {feedback} | {tasks} | {risk} | {next} |".format(
                score=priority_score(item),
                project=display_project(item["client"], item["position"]).replace("|", "｜"),
                gap=item["gap"],
                pool=item["active_candidates"],
                advanced=item["advanced_candidates"],
                interviewing=item["interviewing_candidates"],
                ab=item["a_candidates"] + item["b_candidates"],
                replies=item["replies"],
                positive=item["positive_replies"],
                feedback=f"{item['positive_feedback']}/{item['negative_feedback']}",
                tasks=item["open_followups"],
                risk="、".join(risk_flags(item)) or "无",
                next=next_action(item),
            )
        )


def write_top_candidates(lines: list[str], projects: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "| 岗位 | 人选 | 分数 | 等级 | 下一步 |",
            "|---|---|---:|---|---|",
        ]
    )
    wrote = False
    for item in projects:
        for candidate in item["top_candidates"][:3]:
            wrote = True
            name = candidate["name"]
            if candidate["company"]:
                name = f"{name}（{candidate['company']}）"
            lines.append(
                f"| {display_project(item['client'], item['position']).replace('|', '｜')} | "
                f"{name.replace('|', '｜')} | {candidate['score']} | {candidate['level']} | "
                f"{candidate['next_action'].replace('|', '｜') or '待处理'} |"
            )
    if not wrote:
        lines.append("| 暂无 | 暂无 | - | - | - |")


def write_report(projects: list[dict[str, Any]], recent_activity: list[dict[str, str]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘岗位驾驶舱_{stamp}.md"

    sorted_projects = sort_projects(projects)
    priority_projects = [
        item for item in sorted_projects
        if item["open_followups"] or item["positive_replies"] or item["a_candidates"] or item["b_candidates"]
    ]
    thin_pool = [
        item for item in sorted_projects
        if item["open_position_rows"] and item["gap"] and item["active_candidates"] < 3
    ]
    needs_project = [
        item for item in sorted_projects
        if (not item["client"] or not item["position"]) and (
            item["open_followups"] or item["replies"] or item["intelligence_count"] or item["candidates"]
        )
    ]

    open_positions = sum(1 for item in projects if item["open_position_rows"])
    positions_with_pool = sum(1 for item in projects if item["active_candidates"])
    total_candidates = sum(item["active_candidates"] for item in projects)
    total_followups = sum(item["open_followups"] for item in projects)
    total_positive = sum(item["positive_replies"] for item in projects)
    total_positive_feedback = sum(item["positive_feedback"] for item in projects)
    total_negative_feedback = sum(item["negative_feedback"] for item in projects)

    lines = [
        "# 猎聘岗位驾驶舱",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前状态",
        "",
        f"- 在招岗位方向：{open_positions}",
        f"- 已有人才池的岗位方向：{positions_with_pool}",
        f"- 活跃候选人池：{total_candidates}",
        f"- 打开待办：{total_followups}",
        f"- 正向或可继续回复：{total_positive}",
        f"- 客户反馈：正向 {total_positive_feedback} / 负向 {total_negative_feedback}",
        f"- 需要补客户/岗位归属：{len(needs_project)} 个方向",
        "",
        "## 先推进",
        "",
    ]
    write_project_table(lines, priority_projects[:12], limit=12)

    lines.extend(["", "## 需要补池", ""])
    write_project_table(lines, thin_pool[:12], limit=12)

    lines.extend(["", "## 需要补客户/岗位归属", ""])
    if needs_project:
        write_project_table(lines, needs_project[:12], limit=12)
    else:
        lines.append("- 暂无明显未归属项目。")

    lines.extend(["", "## 重点人选与需确认人选", ""])
    write_top_candidates(lines, priority_projects)

    lines.extend(["", "## 岗位总览", ""])
    write_project_table(lines, sorted_projects, limit=60)

    lines.extend(["", "## 最近动作", ""])
    if recent_activity:
        for item in recent_activity:
            lines.append(
                f"- {item['time'] or '未标时间'}｜{item['type']}｜{item['project']}｜"
                f"{item['name']}｜{item['summary'] or '无摘要'}"
            )
    else:
        lines.append("- 暂无近期动作。")

    lines.extend(
        [
            "",
            "## 使用建议",
            "",
            "1. 先处理“先推进”：这些岗位已经有人选、回复或待办，离成交最近。",
            "2. 再看“需要补池”：缺口还在但候选人池偏薄，适合安排下一轮搜索。",
            "3. 最后处理“需要补客户/岗位归属”：补准项目后，回复和候选人评分才会进入正确岗位。",
        ]
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a position-level Liepin dashboard.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        projects, recent_activity = collect_projects(conn)
    finally:
        conn.close()

    report = write_report(projects, recent_activity, Path(args.output_dir).expanduser())
    sorted_projects = sort_projects(projects)
    print(
        json.dumps(
            {
                "ok": True,
                "projects": len(projects),
                "priority_projects": sum(
                    1 for item in sorted_projects
                    if item["open_followups"] or item["positive_replies"] or item["a_candidates"] or item["b_candidates"]
                ),
                "open_followups": sum(item["open_followups"] for item in projects),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
