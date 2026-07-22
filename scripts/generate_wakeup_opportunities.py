#!/usr/bin/env python3
"""Generate candidate and client wake-up opportunities from the local talent pool."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

NEGATIVE_STATUSES = {"client_rejected", "eliminated", "passed", "duplicate"}
ACTIVE_STATUSES = {"recommended", "client_approved", "contacted", "interviewing", "offered", "hired", "replied"}
POSITIVE_INTENTS = {"interested", "need_more_info", "salary_concern", "location_concern", "need_contact"}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [clean(item) for item in value if clean(item)]


def first_line(text: Any, limit: int = 72) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def pipe_safe(text: Any) -> str:
    return clean(text).replace("|", "｜")


def date_expr(column: str) -> str:
    return f"""
        CASE
          WHEN {column} GLOB '????-??-??*' THEN substr({column}, 1, 10)
          WHEN {column} GLOB '????/??/??*' THEN replace(substr({column}, 1, 10), '/', '-')
          ELSE NULL
        END
    """


def load_candidate_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "candidates"):
        return []
    return conn.execute(
        f"""
        WITH latest_outreach AS (
          SELECT candidate_name, candidate_company, client, position, MAX({date_expr('event_time')}) AS last_outreach_at
          FROM outreach_events
          GROUP BY candidate_name, candidate_company, client, position
        ),
        latest_reply AS (
          SELECT candidate_name, candidate_company, client, position,
                 MAX({date_expr('COALESCE(message_time, created_at)')}) AS last_reply_at,
                 MAX(CASE WHEN intent IN ('interested','need_more_info','salary_concern','location_concern','need_contact') THEN 1 ELSE 0 END) AS has_positive_reply,
                 GROUP_CONCAT(DISTINCT intent) AS reply_intents
          FROM candidate_replies
          GROUP BY candidate_name, candidate_company, client, position
        ),
        feedback AS (
          SELECT candidate_name, candidate_company, client, position,
                 MAX({date_expr('feedback_time')}) AS last_feedback_at,
                 SUM(CASE WHEN feedback_type IN ('approved','interview','offer','hired') THEN 1 ELSE 0 END) AS positive_feedback_count,
                 SUM(CASE WHEN feedback_type IN ('rejected','eliminated') THEN 1 ELSE 0 END) AS negative_feedback_count
          FROM client_feedback_events
          GROUP BY candidate_name, candidate_company, client, position
        )
        SELECT
            c.id,
            c.name,
            COALESCE(c.company, '') AS company,
            COALESCE(c.title, '') AS title,
            COALESCE(c.client, '') AS client,
            COALESCE(c.position, '') AS position,
            COALESCE(c.status, 'new') AS status,
            COALESCE(c.city, '') AS city,
            COALESCE(c.education, '') AS education,
            COALESCE(c.experience, '') AS experience,
            COALESCE(c.skills, '') AS skills,
            COALESCE(c.notes, '') AS notes,
            {date_expr('c.search_date')} AS search_at,
            {date_expr('c.created_at')} AS created_at,
            {date_expr('c.updated_at')} AS updated_at,
            COALESCE(ci.fit_score, 0) AS fit_score,
            COALESCE(ci.fit_level, '') AS fit_level,
            COALESCE(ci.strong_matches_json, '[]') AS strong_matches_json,
            COALESCE(ci.weak_matches_json, '[]') AS weak_matches_json,
            COALESCE(ci.verification_questions_json, '[]') AS verification_questions_json,
            COALESCE(ci.next_action, '') AS next_action,
            COALESCE(ci.recommendation_decision, '') AS recommendation_decision,
            COALESCE(pp.pitch_points_json, '[]') AS pitch_points_json,
            lo.last_outreach_at,
            lr.last_reply_at,
            COALESCE(lr.has_positive_reply, 0) AS has_positive_reply,
            COALESCE(lr.reply_intents, '') AS reply_intents,
            fb.last_feedback_at,
            COALESCE(fb.positive_feedback_count, 0) AS positive_feedback_count,
            COALESCE(fb.negative_feedback_count, 0) AS negative_feedback_count
        FROM candidates c
        LEFT JOIN candidate_intelligence ci
          ON c.id = ci.candidate_id
        LEFT JOIN position_profiles pp
          ON c.client = pp.client
         AND c.position = pp.position
        LEFT JOIN latest_outreach lo
          ON c.name = lo.candidate_name
         AND COALESCE(c.company, '') = COALESCE(lo.candidate_company, '')
         AND COALESCE(c.client, '') = COALESCE(lo.client, '')
         AND COALESCE(c.position, '') = COALESCE(lo.position, '')
        LEFT JOIN latest_reply lr
          ON c.name = lr.candidate_name
         AND COALESCE(c.company, '') = COALESCE(lr.candidate_company, '')
         AND COALESCE(c.client, '') = COALESCE(lr.client, '')
         AND COALESCE(c.position, '') = COALESCE(lr.position, '')
        LEFT JOIN feedback fb
          ON c.name = fb.candidate_name
         AND COALESCE(c.company, '') = COALESCE(fb.candidate_company, '')
         AND COALESCE(c.client, '') = COALESCE(fb.client, '')
         AND COALESCE(c.position, '') = COALESCE(fb.position, '')
        WHERE COALESCE(c.name, '') <> ''
        ORDER BY fit_score DESC, c.id DESC
        """
    ).fetchall()


def load_project_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "positions"):
        return []
    return conn.execute(
        f"""
        WITH candidate_stats AS (
          SELECT client, position,
                 COUNT(*) AS candidate_count,
                 MAX(COALESCE({date_expr('updated_at')}, {date_expr('created_at')}, {date_expr('search_date')})) AS last_candidate_at
          FROM candidates
          GROUP BY client, position
        ),
        outreach_stats AS (
          SELECT client, position, COUNT(*) AS outreach_count, MAX({date_expr('event_time')}) AS last_outreach_at
          FROM outreach_events
          GROUP BY client, position
        ),
        feedback_stats AS (
          SELECT client, position, COUNT(*) AS feedback_count, MAX({date_expr('feedback_time')}) AS last_feedback_at
          FROM client_feedback_events
          GROUP BY client, position
        )
        SELECT
          p.client,
          p.title AS position,
          COALESCE(p.status, 'open') AS status,
          COALESCE(p.department, '') AS department,
          COALESCE(p.team, '') AS team,
          COALESCE(p.gap, 0) AS gap,
          COALESCE(p.headcount, 0) AS headcount,
          {date_expr('p.created_at')} AS created_at,
          {date_expr('p.updated_at')} AS updated_at,
          COALESCE(cs.candidate_count, 0) AS candidate_count,
          cs.last_candidate_at,
          COALESCE(os.outreach_count, 0) AS outreach_count,
          os.last_outreach_at,
          COALESCE(fs.feedback_count, 0) AS feedback_count,
          fs.last_feedback_at,
          COALESCE(pp.pitch_points_json, '[]') AS pitch_points_json,
          COALESCE(pp.search_keywords_json, '[]') AS search_keywords_json,
          COALESCE(pp.jd_analysis_summary, '') AS jd_analysis_summary
        FROM positions p
        LEFT JOIN candidate_stats cs
          ON p.client = cs.client
         AND p.title = cs.position
        LEFT JOIN outreach_stats os
          ON p.client = os.client
         AND p.title = os.position
        LEFT JOIN feedback_stats fs
          ON p.client = fs.client
         AND p.title = fs.position
        LEFT JOIN position_profiles pp
          ON p.client = pp.client
         AND p.title = pp.position
        WHERE COALESCE(p.status, 'open') = 'open'
        ORDER BY p.client, p.title
        """
    ).fetchall()


def load_reply_wakeup_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "candidate_replies"):
        return []
    return conn.execute(
        f"""
        SELECT
          candidate_name AS name,
          COALESCE(candidate_company, '') AS company,
          COALESCE(candidate_title, '') AS title,
          COALESCE(confirmed_client, inferred_client, client, '') AS client,
          COALESCE(confirmed_position, inferred_position, position, '') AS position,
          MAX({date_expr('COALESCE(message_time, created_at)')}) AS last_reply_at,
          GROUP_CONCAT(DISTINCT intent) AS reply_intents,
          MAX(raw_text) AS raw_text,
          MAX(suggested_next_action) AS suggested_next_action,
          MAX(draft_message) AS draft_message
        FROM candidate_replies
        WHERE intent IN ('interested','need_more_info','salary_concern','location_concern','need_contact')
          AND COALESCE(candidate_name, '') <> ''
        GROUP BY candidate_name, candidate_company, client, position
        ORDER BY last_reply_at ASC
        """
    ).fetchall()


def parse_date(value: Any) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None


def days_since(value: Any, now: datetime) -> int | None:
    parsed = parse_date(value)
    if parsed is None:
        return None
    return max(0, (now.date() - parsed.date()).days)


def latest_date(*values: Any) -> str:
    parsed = [(parse_date(value), clean(value)) for value in values if clean(value)]
    parsed = [(dt, text) for dt, text in parsed if dt is not None]
    if not parsed:
        return ""
    return max(parsed, key=lambda item: item[0])[1][:10]


def salutation(name: str, title: str) -> str:
    value = clean(name)
    if not value or value.startswith("匿名"):
        return "您好"
    prefix = value[0]
    title_text = clean(title)
    if any(key in title_text for key in ["总监", "经理", "负责人", "副总", "总裁", "总经理"]):
        return f"{prefix}总"
    if any(key in title_text for key in ["工程师", "专家", "研发", "技术", "工艺", "设备", "硬件", "机械"]):
        return f"{prefix}工"
    return f"{prefix}老师"


def candidate_reason(row: sqlite3.Row, strong: list[str], idle_days: int | None) -> str:
    reasons = []
    score = int(row["fit_score"] or 0)
    if score >= 75:
        reasons.append(f"匹配分 {score}")
    if int(row["has_positive_reply"] or 0):
        reasons.append("历史回复偏正向")
    if strong:
        reasons.append(strong[0])
    if idle_days is not None and idle_days >= 14:
        reasons.append(f"已 {idle_days} 天未互动")
    return "；".join(reasons) or "资料可复用，适合轻量问候确认近况"


def candidate_bucket(row: sqlite3.Row, idle_days: int | None, score: int) -> str:
    status = clean(row["status"])
    if status in NEGATIVE_STATUSES or int(row["negative_feedback_count"] or 0):
        return "暂不唤醒"
    if status in ACTIVE_STATUSES and idle_days is not None and idle_days <= 7:
        return "正在推进"
    if score >= 78 and idle_days is not None and idle_days >= 7:
        return "优先唤醒"
    if int(row["has_positive_reply"] or 0) and idle_days is not None and idle_days >= 5:
        return "优先唤醒"
    if score >= 65 and (idle_days is None or idle_days >= 10):
        return "可轻触达"
    return "观察"


def candidate_message(row: sqlite3.Row, strong: list[str], pitch: list[str]) -> str:
    hello = salutation(row["name"], row["title"])
    project = clean(row["position"]) or "之前聊到的机会"
    anchor = strong[0] if strong else clean(row["title"]) or "您的经历方向"
    pitch_text = f"，这边岗位现在重点看{pitch[0]}" if pitch else ""
    return f"{hello}您好，之前留意到您{anchor}，和{project}方向比较接近{pitch_text}。想简单问下，您最近还有看新机会的计划吗？"


def build_candidate_items(rows: list[sqlite3.Row], now: datetime) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (clean(row["name"]), clean(row["company"]), clean(row["client"]), clean(row["position"]))
        if key in seen:
            continue
        seen.add(key)
        last_activity = latest_date(
            row["last_reply_at"],
            row["last_outreach_at"],
            row["last_feedback_at"],
            row["updated_at"],
            row["created_at"],
            row["search_at"],
        )
        idle_days = days_since(last_activity, now)
        score = int(row["fit_score"] or 0)
        strong = parse_json_list(row["strong_matches_json"])
        pitch = parse_json_list(row["pitch_points_json"])
        bucket = candidate_bucket(row, idle_days, score)
        if bucket in {"观察", "正在推进"} and score < 70:
            continue
        items.append(
            {
                "bucket": bucket,
                "name": clean(row["name"]),
                "company": clean(row["company"]),
                "title": clean(row["title"]),
                "client": clean(row["client"]),
                "position": clean(row["position"]),
                "status": clean(row["status"]),
                "fit_score": score,
                "idle_days": idle_days,
                "last_activity": last_activity or "未知",
                "reason": candidate_reason(row, strong, idle_days),
                "risks": parse_json_list(row["weak_matches_json"])[:3],
                "questions": parse_json_list(row["verification_questions_json"])[:3],
                "message": candidate_message(row, strong, pitch),
            }
        )
    return sorted(
        items,
        key=lambda item: (
            {"优先唤醒": 4, "可轻触达": 3, "观察": 2, "正在推进": 1, "暂不唤醒": 0}.get(item["bucket"], 0),
            item["fit_score"],
            item["idle_days"] if item["idle_days"] is not None else -1,
        ),
        reverse=True,
    )


def reply_bucket(row: sqlite3.Row, idle_days: int | None) -> str:
    intents = set(clean(row["reply_intents"]).split(","))
    if idle_days is not None and idle_days <= 3:
        return "正在推进"
    if intents & {"interested", "need_more_info", "salary_concern", "location_concern", "need_contact"}:
        return "优先唤醒"
    return "可轻触达"


def reply_message(row: sqlite3.Row) -> str:
    draft = clean(row["draft_message"])
    if draft:
        return draft
    hello = salutation(row["name"], row["title"])
    position = clean(row["position"]) or "之前聊过的机会"
    return f"{hello}您好，之前您对{position}有过一些兴趣/沟通，我这边想跟您同步一下近况。您最近还方便了解新的岗位机会吗？"


def build_reply_wakeup_items(rows: list[sqlite3.Row], now: datetime, existing_keys: set[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        key = (clean(row["name"]), clean(row["company"]), clean(row["client"]), clean(row["position"]))
        if key in existing_keys:
            continue
        idle_days = days_since(row["last_reply_at"], now)
        bucket = reply_bucket(row, idle_days)
        if bucket == "正在推进":
            continue
        intents = clean(row["reply_intents"]) or "正向回复"
        raw_text = clean(row["raw_text"])
        items.append(
            {
                "bucket": bucket,
                "name": clean(row["name"]),
                "company": clean(row["company"]),
                "title": clean(row["title"]),
                "client": clean(row["client"]),
                "position": clean(row["position"]),
                "status": "reply",
                "fit_score": 0,
                "idle_days": idle_days,
                "last_activity": clean(row["last_reply_at"]) or "未知",
                "reason": f"历史回复：{intents}" + (f"；原话：{first_line(raw_text, 34)}" if raw_text else ""),
                "risks": [],
                "questions": [clean(row["suggested_next_action"]) or "确认当前意愿、岗位方向和联系方式。"],
                "message": reply_message(row),
            }
        )
    return items


def project_bucket(row: sqlite3.Row, idle_days: int | None) -> str:
    candidate_count = int(row["candidate_count"] or 0)
    outreach_count = int(row["outreach_count"] or 0)
    feedback_count = int(row["feedback_count"] or 0)
    if candidate_count == 0:
        return "需补池"
    if idle_days is not None and idle_days >= 14 and outreach_count == 0:
        return "客户唤醒"
    if idle_days is not None and idle_days >= 10 and feedback_count == 0:
        return "催反馈"
    if candidate_count >= 5 and outreach_count == 0:
        return "可批量触达"
    return "观察"


def project_message(row: sqlite3.Row, keywords: list[str], pitch: list[str]) -> str:
    client = clean(row["client"])
    position = clean(row["position"])
    key_text = f"，我们这边近期重点可围绕{keywords[0]}再补一轮" if keywords else ""
    pitch_text = f"；前期判断这个岗位的卖点是{pitch[0]}" if pitch else ""
    return f"{client}这边{position}岗位我想同步一下进展{key_text}{pitch_text}。如果需求还在，我这边可以按最新口径再筛一版人选给您。"


def build_project_items(rows: list[sqlite3.Row], now: datetime) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        last_activity = latest_date(
            row["last_feedback_at"],
            row["last_outreach_at"],
            row["last_candidate_at"],
            row["updated_at"],
            row["created_at"],
        )
        idle_days = days_since(last_activity, now)
        keywords = parse_json_list(row["search_keywords_json"])
        pitch = parse_json_list(row["pitch_points_json"])
        bucket = project_bucket(row, idle_days)
        if bucket == "观察":
            continue
        items.append(
            {
                "bucket": bucket,
                "client": clean(row["client"]),
                "position": clean(row["position"]),
                "candidate_count": int(row["candidate_count"] or 0),
                "outreach_count": int(row["outreach_count"] or 0),
                "feedback_count": int(row["feedback_count"] or 0),
                "idle_days": idle_days,
                "last_activity": last_activity or "未知",
                "keywords": keywords[:5],
                "pitch": pitch[:3],
                "message": project_message(row, keywords, pitch),
                "jd_summary": clean(row["jd_analysis_summary"]),
            }
        )
    return sorted(
        items,
        key=lambda item: (
            {"需补池": 4, "客户唤醒": 3, "催反馈": 2, "可批量触达": 1}.get(item["bucket"], 0),
            item["idle_days"] if item["idle_days"] is not None else -1,
            item["candidate_count"],
        ),
        reverse=True,
    )


def write_candidate_table(lines: list[str], items: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "| 分组 | 分 | 沉睡 | 人选 | 项目 | 唤醒理由 | 话术草稿 |",
            "|---|---:|---:|---|---|---|---|",
        ]
    )
    if not items:
        lines.append("| 暂无 | - | - | - | - | - | - |")
        return
    for item in items:
        person = item["name"]
        company_title = " / ".join(part for part in [item["company"], item["title"]] if part)
        if company_title:
            person = f"{person}（{company_title}）"
        project = f"{item['client'] or '未定客户'}/{item['position'] or '未定岗位'}"
        idle = item["idle_days"] if item["idle_days"] is not None else "未知"
        lines.append(
            f"| {pipe_safe(item['bucket'])} | {item['fit_score']} | {idle} | {pipe_safe(person)} | "
            f"{pipe_safe(project)} | {pipe_safe(first_line(item['reason'], 54))} | "
            f"{pipe_safe(first_line(item['message'], 64))} |"
        )


def write_project_table(lines: list[str], items: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "| 分组 | 沉睡 | 客户/岗位 | 人才池 | 触达/反馈 | 建议动作 |",
            "|---|---:|---|---:|---|---|",
        ]
    )
    if not items:
        lines.append("| 暂无 | - | - | - | - | - |")
        return
    for item in items:
        project = f"{item['client']}/{item['position']}"
        idle = item["idle_days"] if item["idle_days"] is not None else "未知"
        action = item["message"]
        lines.append(
            f"| {pipe_safe(item['bucket'])} | {idle} | {pipe_safe(project)} | {item['candidate_count']} | "
            f"{item['outreach_count']}/{item['feedback_count']} | {pipe_safe(first_line(action, 78))} |"
        )


def write_report(output_dir: Path, candidate_items: list[dict[str, Any]], project_items: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘唤醒机会清单_{stamp}.md"
    candidate_counts = Counter(item["bucket"] for item in candidate_items)
    project_counts = Counter(item["bucket"] for item in project_items)
    lines = [
        "# 猎聘唤醒机会清单",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 定位",
        "",
        "用于筛出值得重新触达的沉睡候选人、久未反馈的客户/岗位，并生成轻量问候话术。这里只生成建议，不自动发送。",
        "",
        "## 概览",
        "",
        f"- 候选人机会：{len(candidate_items)} 条",
        f"- 候选人分布：{'、'.join(f'{k} {v}' for k, v in candidate_counts.items()) or '暂无'}",
        f"- 客户/岗位机会：{len(project_items)} 条",
        f"- 客户/岗位分布：{'、'.join(f'{k} {v}' for k, v in project_counts.items()) or '暂无'}",
        "",
        "## 候选人唤醒",
        "",
    ]
    write_candidate_table(lines, candidate_items[:40])
    lines.extend(["", "## 客户/岗位唤醒", ""])
    write_project_table(lines, project_items[:30])
    lines.extend(["", "## 候选人详情", ""])
    for item in candidate_items[:25]:
        risks = "；".join(item["risks"]) or "暂无明显风险"
        questions = "；".join(item["questions"]) or "确认近况、意愿、地点和薪资范围。"
        lines.extend(
            [
                f"### {item['name']}｜{item['client'] or '未定客户'}/{item['position'] or '未定岗位'}",
                "",
                f"- 分组：{item['bucket']}｜匹配分：{item['fit_score']}｜最后活动：{item['last_activity']}｜沉睡：{item['idle_days'] if item['idle_days'] is not None else '未知'} 天",
                f"- 唤醒理由：{item['reason']}",
                f"- 风险/待确认：{risks}",
                f"- 建议追问：{questions}",
                f"- 话术草稿：{item['message']}",
                "",
            ]
        )
    lines.extend(
        [
            "## 使用规则",
            "",
            "1. 优先处理“优先唤醒”：先问近况和看机会意愿，不直接推长 JD。",
            "2. “可轻触达”适合低压问候，若对方无回应，不连续追问。",
            "3. 客户/岗位唤醒先确认需求是否还在，再决定补搜索或出汇总。",
            "4. 负向反馈、明确拒绝、重复和淘汰状态默认不建议唤醒。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate wake-up opportunities.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    now = datetime.now()
    conn = connect(Path(args.db).expanduser())
    try:
        candidate_items = build_candidate_items(load_candidate_rows(conn), now)
        existing_keys = {
            (item["name"], item["company"], item["client"], item["position"])
            for item in candidate_items
        }
        candidate_items.extend(build_reply_wakeup_items(load_reply_wakeup_rows(conn), now, existing_keys))
        candidate_items = sorted(
            candidate_items,
            key=lambda item: (
                {"优先唤醒": 4, "可轻触达": 3, "观察": 2, "正在推进": 1, "暂不唤醒": 0}.get(item["bucket"], 0),
                item["fit_score"],
                item["idle_days"] if item["idle_days"] is not None else -1,
            ),
            reverse=True,
        )
        project_items = build_project_items(load_project_rows(conn), now)
    finally:
        conn.close()
    report = write_report(Path(args.output_dir).expanduser(), candidate_items, project_items)
    print(
        json.dumps(
            {
                "ok": True,
                "candidate_items": len(candidate_items),
                "project_items": len(project_items),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
