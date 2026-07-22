#!/usr/bin/env python3
"""Generate customer-facing batch recommendation summaries by project."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

NEGATIVE_STATUSES = {"client_rejected", "eliminated", "duplicate"}
ADVANCED_STATUSES = {"recommended", "client_approved", "interviewing", "offered", "hired"}


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


def first_line(text: Any, limit: int = 74) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def pipe_safe(text: Any) -> str:
    return clean(text).replace("|", "｜")


def load_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "candidate_intelligence"):
        return []
    return conn.execute(
        """
        SELECT
            ci.candidate_id,
            ci.candidate_name,
            ci.candidate_company,
            ci.client,
            ci.position,
            ci.fit_score,
            ci.fit_level,
            COALESCE(ci.evidence_json, '{}') AS evidence_json,
            COALESCE(ci.risk_json, '{}') AS risk_json,
            COALESCE(ci.next_action, '') AS next_action,
            COALESCE(ci.strong_matches_json, '[]') AS strong_matches_json,
            COALESCE(ci.weak_matches_json, '[]') AS weak_matches_json,
            COALESCE(ci.verification_questions_json, '[]') AS verification_questions_json,
            COALESCE(ci.recommendation_decision, '') AS recommendation_decision,
            COALESCE(c.status, '') AS candidate_status,
            COALESCE(c.title, '') AS candidate_title,
            COALESCE(c.education, '') AS candidate_education,
            COALESCE(c.experience, '') AS candidate_experience,
            COALESCE(c.city, '') AS candidate_city,
            COALESCE(c.notes, '') AS candidate_notes,
            COALESCE(pp.pitch_points_json, '[]') AS pitch_points_json,
            COALESCE(pp.risk_points_json, '[]') AS position_risk_points_json,
            COALESCE(pp.jd_analysis_summary, '') AS jd_analysis_summary,
            COALESCE(o.recommend_count, 0) AS recommend_count,
            COALESCE(f.positive_feedback_count, 0) AS positive_feedback_count,
            COALESCE(f.negative_feedback_count, 0) AS negative_feedback_count
        FROM candidate_intelligence ci
        LEFT JOIN candidates c
          ON ci.candidate_id = c.id
        LEFT JOIN position_profiles pp
          ON ci.client = pp.client
         AND ci.position = pp.position
        LEFT JOIN (
          SELECT candidate_id, client, position,
                 SUM(CASE WHEN event_type='recommend_position' THEN 1 ELSE 0 END) AS recommend_count
          FROM outreach_events
          GROUP BY candidate_id, client, position
        ) o
          ON ci.candidate_id = o.candidate_id
         AND ci.client = o.client
         AND ci.position = o.position
        LEFT JOIN (
          SELECT candidate_id, client, position,
                 SUM(CASE WHEN feedback_type IN ('approved','interview','offer','hired') THEN 1 ELSE 0 END) AS positive_feedback_count,
                 SUM(CASE WHEN feedback_type IN ('rejected','eliminated') THEN 1 ELSE 0 END) AS negative_feedback_count
          FROM client_feedback_events
          GROUP BY candidate_id, client, position
        ) f
          ON ci.candidate_id = f.candidate_id
         AND ci.client = f.client
         AND ci.position = f.position
        WHERE COALESCE(ci.client, '') <> ''
          AND COALESCE(ci.position, '') <> ''
        ORDER BY ci.client, ci.position, ci.fit_score DESC, ci.updated_at DESC
        """
    ).fetchall()


def nested_json_list(row: sqlite3.Row, column: str, key: str) -> list[str]:
    try:
        value = json.loads(row[column] or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, dict):
        return []
    items = value.get(key)
    if not isinstance(items, list):
        return []
    return [clean(item) for item in items if clean(item)]


def card_list(row: sqlite3.Row, column: str, fallback_column: str = "") -> list[str]:
    items = parse_json_list(row[column])
    if items:
        return items
    if fallback_column:
        fallback_key = column.replace("_json", "").replace("verification_questions", "questions")
        return nested_json_list(row, fallback_column, fallback_key)
    return []


def risk_list(row: sqlite3.Row) -> list[str]:
    risks = card_list(row, "weak_matches_json", "risk_json")
    if not risks:
        risks = nested_json_list(row, "risk_json", "risks")
    if not risks:
        risks = parse_json_list(row["position_risk_points_json"])
    return risks[:4]


def classify(row: sqlite3.Row, risks: list[str]) -> str:
    score = int(row["fit_score"] or 0)
    status = clean(row["candidate_status"])
    decision = clean(row["recommendation_decision"])
    negative_feedback = int(row["negative_feedback_count"] or 0)
    if status in NEGATIVE_STATUSES or negative_feedback:
        return "暂缓"
    if score >= 82 and "暂缓" not in decision and "不推荐" not in decision and "待复核" not in decision:
        return "最推荐"
    if score >= 70 or "先确认" in decision or "可推荐" in decision:
        return "备选"
    if risks or score < 70:
        return "风险高"
    return "待补资料"


def recommendation_reason(row: sqlite3.Row, strong_matches: list[str], pitch_points: list[str]) -> str:
    name = clean(row["candidate_name"]) or "该人选"
    title = clean(row["candidate_title"])
    company = clean(row["candidate_company"])
    evidence = strong_matches[0] if strong_matches else ""
    pitch = pitch_points[0] if pitch_points else ""
    base = f"{name}"
    if company or title:
        base += f"来自{company or '现公司'}，{title or '岗位背景'}"
    if evidence:
        return f"{base}，核心匹配点是{evidence}"
    if pitch:
        return f"{base}，与岗位卖点“{pitch}”可形成沟通锚点"
    return f"{base}，建议先补齐关键经历后再判断推荐价值"


def next_action(row: sqlite3.Row, bucket: str, questions: list[str]) -> str:
    existing = clean(row["next_action"])
    if bucket == "最推荐":
        return existing or "优先电话确认意愿、薪资和简历版本，确认后进入客户推荐。"
    if bucket == "备选":
        question = questions[0] if questions else "确认当前看机会意愿和关键项目经历。"
        return f"先补问：{question}"
    if bucket == "暂缓":
        return "先处理负向状态、客户反馈或重复推荐问题，暂不进入客户汇总。"
    return existing or "补齐项目归属、薪资、地点或核心经历后再判断。"


def build_items(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    items = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = (
            clean(row["candidate_name"]),
            clean(row["candidate_company"]),
            clean(row["client"]),
            clean(row["position"]),
        )
        if key in seen:
            continue
        seen.add(key)
        strong_matches = card_list(row, "strong_matches_json", "evidence_json")
        if not strong_matches:
            strong_matches = nested_json_list(row, "evidence_json", "evidence")
        risks = risk_list(row)
        questions = card_list(row, "verification_questions_json", "evidence_json")
        pitch_points = parse_json_list(row["pitch_points_json"])
        bucket = classify(row, risks)
        items.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_name": clean(row["candidate_name"]),
                "candidate_company": clean(row["candidate_company"]),
                "candidate_title": clean(row["candidate_title"]),
                "client": clean(row["client"]),
                "position": clean(row["position"]),
                "fit_score": int(row["fit_score"] or 0),
                "fit_level": clean(row["fit_level"]),
                "bucket": bucket,
                "reason": recommendation_reason(row, strong_matches, pitch_points),
                "strong_matches": strong_matches[:4],
                "risks": risks[:4],
                "questions": questions[:4],
                "next_action": next_action(row, bucket, questions),
                "status": clean(row["candidate_status"]),
                "recommend_count": int(row["recommend_count"] or 0),
                "positive_feedback_count": int(row["positive_feedback_count"] or 0),
                "negative_feedback_count": int(row["negative_feedback_count"] or 0),
                "jd_summary": clean(row["jd_analysis_summary"]),
                "pitch_points": pitch_points[:3],
            }
        )
    return items


def project_sort_key(items: list[dict[str, Any]]) -> tuple[int, int, int]:
    best = max((item["fit_score"] for item in items), default=0)
    top_count = sum(1 for item in items if item["bucket"] == "最推荐")
    return (top_count, best, len(items))


def project_decision(items: list[dict[str, Any]]) -> list[str]:
    top = [item for item in items if item["bucket"] == "最推荐"]
    backups = [item for item in items if item["bucket"] == "备选"]
    lines = []
    if top:
        names = "、".join(item["candidate_name"] for item in sorted(top, key=lambda x: x["fit_score"], reverse=True)[:2])
        lines.append(f"优先推荐：{names}。")
    if backups:
        names = "、".join(item["candidate_name"] for item in sorted(backups, key=lambda x: x["fit_score"], reverse=True)[:2])
        lines.append(f"备选保留：{names}，先补关键问题再决定是否进入正式推荐。")
    if not top and not backups:
        lines.append("本项目暂不建议直接批量推给客户，先补资料或重新寻访。")
    return lines


def write_report(output_dir: Path, items: list[dict[str, Any]], per_project: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘客户推荐汇总_{stamp}.md"
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[(item["client"], item["position"])].append(item)

    bucket_counts = Counter(item["bucket"] for item in items)
    lines = [
        "# 猎聘客户推荐汇总",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 定位",
        "",
        "这是客户决策汇总版，用来快速看同一岗位下哪些人优先推荐、哪些人先补问、哪些人暂缓。正式单人推荐报告仍沿用嘉驰格式。",
        "",
        "## 概览",
        "",
        f"- 覆盖项目：{len(groups)} 个客户/岗位",
        f"- 候选人：{len(items)} 位",
        f"- 分布：{'、'.join(f'{k} {v}' for k, v in bucket_counts.items()) or '暂无'}",
        "",
        "## 项目推荐清单",
        "",
    ]

    sorted_groups = sorted(groups.items(), key=lambda pair: project_sort_key(pair[1]), reverse=True)
    if not sorted_groups:
        lines.append("暂无可生成客户推荐汇总的候选人。")

    for (client, position), project_items in sorted_groups:
        project_items = sorted(
            project_items,
            key=lambda item: (
                {"最推荐": 4, "备选": 3, "风险高": 2, "待补资料": 1, "暂缓": 0}.get(item["bucket"], 0),
                item["fit_score"],
            ),
            reverse=True,
        )[:per_project]
        jd_summary = next((item["jd_summary"] for item in project_items if item["jd_summary"]), "")
        pitch_points = next((item["pitch_points"] for item in project_items if item["pitch_points"]), [])
        lines.extend(
            [
                f"### {client}｜{position}",
                "",
            ]
        )
        if jd_summary:
            lines.append(f"- 岗位判断：{first_line(jd_summary, 110)}")
        if pitch_points:
            lines.append(f"- 客户沟通卖点：{'；'.join(pitch_points)}")
        lines.extend(project_decision(project_items))
        lines.extend(
            [
                "",
                "| 组别 | 分 | 人选 | 当前公司/职位 | 一句话推荐理由 | 风险/待确认 | 建议下一步 |",
                "|---|---:|---|---|---|---|---|",
            ]
        )
        for item in project_items:
            person = item["candidate_name"]
            if item["status"] in ADVANCED_STATUSES:
                person += f"（{item['status']}）"
            company_title = " / ".join(part for part in [item["candidate_company"], item["candidate_title"]] if part) or "待补"
            risk_text = "；".join(item["risks"] or item["questions"] or ["暂无明显风险"])
            lines.append(
                f"| {pipe_safe(item['bucket'])} | {item['fit_score']} | {pipe_safe(person)} | "
                f"{pipe_safe(company_title)} | {pipe_safe(first_line(item['reason'], 58))} | "
                f"{pipe_safe(first_line(risk_text, 48))} | {pipe_safe(first_line(item['next_action'], 52))} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 使用规则",
            "",
            "1. “最推荐”可以准备客户侧材料，但推荐前仍要确认意愿、薪资、地点和简历版本。",
            "2. “备选”先补一个关键问题，确认后再升级为推荐。",
            "3. “风险高/暂缓”不要直接放进客户推荐包，避免消耗客户信任。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate batch recommendation summary.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--per-project", type=int, default=5)
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        items = build_items(load_records(conn))
    finally:
        conn.close()
    report = write_report(Path(args.output_dir).expanduser(), items, max(1, args.per_project))
    print(
        json.dumps(
            {
                "ok": True,
                "items": len(items),
                "projects": len({(item["client"], item["position"]) for item in items}),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
