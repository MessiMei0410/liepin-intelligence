#!/usr/bin/env python3
"""Generate pre-outreach and recommendation readiness checks."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


NEGATIVE_STATUSES = {"client_rejected", "eliminated", "duplicate"}
ADVANCED_STATUSES = {"recommended", "client_approved", "interviewing", "offered", "hired"}

CANDIDATE_INTELLIGENCE_EXTRA_COLUMNS = {
    "strong_matches_json": "TEXT DEFAULT '[]'",
    "weak_matches_json": "TEXT DEFAULT '[]'",
    "verification_questions_json": "TEXT DEFAULT '[]'",
    "recommendation_decision": "TEXT",
}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def ensure_candidate_intelligence_columns(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "candidate_intelligence"):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidate_intelligence)")}
    for column, definition in CANDIDATE_INTELLIGENCE_EXTRA_COLUMNS.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE candidate_intelligence ADD COLUMN {column} {definition}")
    conn.commit()


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [clean(item) for item in value if clean(item)] if isinstance(value, list) else []


def first_line(text: Any, limit: int = 70) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def load_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "candidate_intelligence"):
        return []
    return conn.execute(
        """
        SELECT
            ci.candidate_id, ci.candidate_name, ci.candidate_company, ci.client, ci.position,
            ci.fit_score, ci.fit_level, ci.evidence_json, ci.risk_json, ci.next_action,
            COALESCE(ci.strong_matches_json, '[]') AS strong_matches_json,
            COALESCE(ci.weak_matches_json, '[]') AS weak_matches_json,
            COALESCE(ci.verification_questions_json, '[]') AS verification_questions_json,
            COALESCE(ci.recommendation_decision, '') AS recommendation_decision,
            COALESCE(c.status, '') AS candidate_status,
            COALESCE(c.title, '') AS candidate_title,
            COALESCE(c.education, '') AS candidate_education,
            COALESCE(c.experience, '') AS candidate_experience,
            COALESCE(c.city, '') AS candidate_city,
            COALESCE(c.skills, '') AS candidate_skills,
            COALESCE(c.notes, '') AS candidate_notes,
            COALESCE(cp.seniority, '') AS seniority,
            COALESCE(cp.function_tags_json, '[]') AS function_tags_json,
            COALESCE(cp.risk_tags_json, '[]') AS candidate_risk_json,
            COALESCE(pp.ability_keywords_json, '[]') AS ability_keywords_json,
            COALESCE(pp.hard_requirements_json, '[]') AS hard_requirements_json,
            COALESCE(o.outreach_count, 0) AS outreach_count,
            COALESCE(o.recommend_count, 0) AS recommend_count
        FROM candidate_intelligence ci
        LEFT JOIN candidates c
          ON ci.candidate_id = c.id
        LEFT JOIN candidate_profiles cp
          ON ci.candidate_id = cp.candidate_id
        LEFT JOIN position_profiles pp
          ON ci.client = pp.client AND ci.position = pp.position
        LEFT JOIN (
          SELECT candidate_id, client, position, COUNT(*) AS outreach_count,
                 SUM(CASE WHEN event_type='recommend_position' THEN 1 ELSE 0 END) AS recommend_count
          FROM outreach_events
          GROUP BY candidate_id, client, position
        ) o
          ON ci.candidate_id = o.candidate_id
         AND ci.client = o.client
         AND ci.position = o.position
        ORDER BY ci.fit_score DESC, ci.updated_at DESC
        """
    ).fetchall()


def surname(name: str) -> str:
    value = clean(name)
    if not value or value.startswith("匿名"):
        return ""
    return value[0]


def salutation(row: sqlite3.Row) -> str:
    name = clean(row["candidate_name"])
    prefix = surname(name)
    title_text = f"{row['candidate_title']} {row['seniority']} {row['position']}"
    if prefix and any(key in title_text for key in ["总监", "经理", "负责人", "副总", "总裁", "总经理"]):
        return f"{prefix}总"
    if prefix and any(key in title_text for key in ["工程师", "专家", "研发", "技术", "工艺", "设备", "硬件", "机械"]):
        return f"{prefix}工"
    if prefix:
        return f"{prefix}老师"
    return "您好"


def shared_tags(row: sqlite3.Row) -> list[str]:
    candidate_tags = set(parse_json_list(row["function_tags_json"]))
    position_tags = set(parse_json_list(row["ability_keywords_json"]))
    return [tag for tag in candidate_tags if tag in position_tags][:3]


def education_rank(text: str) -> int:
    value = clean(text)
    if "博士" in value:
        return 4
    if "硕士" in value or "研究生" in value:
        return 3
    if "本科" in value:
        return 2
    if "大专" in value or "专科" in value:
        return 1
    return 0


def extract_years(text: str) -> int:
    import re

    value = clean(text)
    matches = re.findall(r"(\d+)\s*年以上|(\d+)\s*年", value)
    years = [int(item) for pair in matches for item in pair if item]
    return max(years) if years else 0


def hard_check_items(row: sqlite3.Row) -> list[str]:
    hard_requirements = parse_json_list(row["hard_requirements_json"])
    candidate_text = " ".join(
        clean(row[key])
        for key in [
            "candidate_title",
            "candidate_education",
            "candidate_experience",
            "candidate_city",
            "candidate_skills",
            "candidate_notes",
        ]
    )
    items: list[str] = []

    required_edu = max([education_rank(item) for item in hard_requirements] or [0])
    candidate_edu = education_rank(row["candidate_education"])
    if required_edu and candidate_edu:
        items.append("学历通过" if candidate_edu >= required_edu else "学历不足")
    elif required_edu:
        items.append("学历待确认")
    else:
        items.append("学历未设硬门槛")

    required_years = max([extract_years(item) for item in hard_requirements] or [0])
    candidate_years = extract_years(row["candidate_experience"] or row["candidate_notes"])
    if required_years and candidate_years:
        items.append("年限通过" if candidate_years >= required_years else f"年限待核实：岗位{required_years}+年")
    elif required_years:
        items.append(f"年限待确认：岗位{required_years}+年")
    else:
        items.append("年限未设硬门槛")

    tags = shared_tags(row)
    items.append(f"岗位经验命中：{'、'.join(tags)}" if tags else "岗位经验待确认")

    city = clean(row["candidate_city"])
    items.append(f"地点已知：{city}" if city else "地点待确认")

    salary_text = candidate_text
    items.append("薪资有线索" if any(key in salary_text for key in ["薪", "k", "K", "万", "期望"]) else "薪资待确认")

    stability_text = candidate_text
    items.append(
        "稳定性需复核"
        if any(key in stability_text for key in ["频繁", "短期", "离职", "空窗"])
        else "稳定性未见明显风险"
    )
    return items


def draft_message(row: sqlite3.Row) -> str:
    hello = salutation(row)
    project = clean(row["position"]) or "这个机会"
    tags = shared_tags(row)
    tag_text = f"您过往{tags[0]}相关经历" if tags else "您目前的经历方向"
    client = clean(row["client"])
    client_text = f"{client}的" if client else ""
    return f"{hello}您好，我这边有个{client_text}{project}机会，和{tag_text}比较接近。您现在方便先了解一下岗位方向吗？"


def classify(row: sqlite3.Row) -> tuple[str, list[str], list[str]]:
    score = int(row["fit_score"] or 0)
    status = clean(row["candidate_status"])
    risks = []
    checks = []
    if status in NEGATIVE_STATUSES:
        risks.append(f"候选人状态为 {status}")
    if int(row["recommend_count"] or 0):
        risks.append("已推荐过该项目，避免重复推荐")
    if not clean(row["client"]) or not clean(row["position"]):
        checks.append("先补客户/岗位归属")
    if not parse_json_list(row["ability_keywords_json"]):
        checks.append("岗位画像缺能力标签")
    if parse_json_list(row["candidate_risk_json"]):
        checks.extend(parse_json_list(row["candidate_risk_json"])[:2])
    if score < 70:
        checks.append("匹配分低于 70，先补验证问题")
    if status in ADVANCED_STATUSES:
        checks.append(f"候选人已处于 {status}，按阶段推进")

    if risks:
        return "暂缓", risks, checks
    if score >= 80 and not checks:
        return "可推荐", [], ["推荐前确认简历版本和当前意愿"]
    if score >= 70:
        return "先确认", [], checks or ["先电话确认核心经历"]
    return "待复核", [], checks


def card_list(row: sqlite3.Row, key: str, fallback_json: str = "") -> list[str]:
    items = parse_json_list(row[key])
    if items:
        return items
    if fallback_json:
        try:
            value = json.loads(row[fallback_json] or "{}")
        except json.JSONDecodeError:
            return []
        if isinstance(value, dict):
            nested = value.get(key.replace("_json", "")) or value.get(key.replace("verification_questions", "questions").replace("_json", ""))
            if isinstance(nested, list):
                return [clean(item) for item in nested if clean(item)]
    return []


def dict_json_list(row: sqlite3.Row, column: str, key: str) -> list[str]:
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


def build_items(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    items = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (clean(row["candidate_name"]), clean(row["client"]), clean(row["position"]))
        if key in seen:
            continue
        seen.add(key)
        status, risks, checks = classify(row)
        strong_matches = card_list(row, "strong_matches_json", "evidence_json")
        weak_matches = card_list(row, "weak_matches_json", "evidence_json")
        questions = card_list(row, "verification_questions_json", "evidence_json")
        if not strong_matches:
            strong_matches = dict_json_list(row, "evidence_json", "evidence")[:4]
        if not weak_matches:
            weak_matches = (dict_json_list(row, "risk_json", "risks") or checks)[:4]
        if not questions:
            questions = dict_json_list(row, "evidence_json", "questions")[:4]
        if not questions:
            questions = ["确认当前看机会意愿、地点接受度、薪资区间和最能支撑岗位的项目经历。"]
        decision = clean(row["recommendation_decision"]) or status
        items.append(
            {
                "candidate_id": row["candidate_id"],
                "candidate_name": clean(row["candidate_name"]),
                "candidate_company": clean(row["candidate_company"]),
                "client": clean(row["client"]),
                "position": clean(row["position"]),
                "fit_score": int(row["fit_score"] or 0),
                "fit_level": clean(row["fit_level"]),
                "recommendation_decision": decision,
                "strong_matches": strong_matches,
                "weak_matches": weak_matches,
                "questions": questions,
                "hard_checks": hard_check_items(row),
                "readiness": status,
                "risks": risks,
                "checks": checks,
                "draft_message": draft_message(row),
                "next_action": clean(row["next_action"]),
                "outreach_count": int(row["outreach_count"] or 0),
            }
        )
    return items


def write_report(output_dir: Path, items: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘推荐前校验_{stamp}.md"
    counts = Counter(item["readiness"] for item in items)
    lines = [
        "# 猎聘推荐前校验",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 概览",
        "",
        f"- 候选人：{len(items)}",
        f"- 状态分布：{'、'.join(f'{k} {v}' for k, v in counts.items()) or '暂无'}",
        "",
        "## 可推进清单",
        "",
        "| 状态 | 分 | 人选 | 项目 | 校验/风险 | 建议话术 |",
        "|---|---:|---|---|---|---|",
    ]
    if not items:
        lines.append("| 暂无 | - | - | - | - | - |")
    for item in sorted(items, key=lambda row: (row["readiness"] == "可推荐", row["fit_score"]), reverse=True)[:40]:
        project = f"{item['client'] or '未定客户'}/{item['position'] or '未定岗位'}"
        notes = "；".join(item["risks"] or item["checks"] or ["可推进"])
        hard_notes = "；".join(item["hard_checks"][:4])
        name = item["candidate_name"]
        if item["candidate_company"]:
            name = f"{name}（{item['candidate_company']}）"
        lines.append(
            f"| {item['readiness']} | {item['fit_score']} | {name.replace('|', '｜')} | "
            f"{project.replace('|', '｜')} | {first_line(notes + '；' + hard_notes, 46).replace('|', '｜')} | "
            f"{first_line(item['draft_message'], 58).replace('|', '｜')} |"
        )

    lines.extend(["", "## 人岗匹配评分卡", ""])
    for item in sorted(items, key=lambda row: row["fit_score"], reverse=True)[:30]:
        project = f"{item['client'] or '未定客户'}/{item['position'] or '未定岗位'}"
        strong = "；".join(item["strong_matches"][:4]) or "暂无"
        weak = "；".join(item["weak_matches"][:4]) or "暂无"
        questions = "；".join(item["questions"][:4]) or "暂无"
        hard_checks = "；".join(item["hard_checks"]) or "暂无"
        risks = "；".join(item["risks"] or item["checks"] or []) or "无明显风险"
        lines.extend(
            [
                f"### {item['candidate_name']}｜{project}",
                "",
                f"- 评分：{item['fit_score']}（{item['fit_level']}）",
                f"- 推荐判断：{item['recommendation_decision']}",
                f"- 强匹配点：{strong}",
                f"- 弱匹配/待确认：{weak}",
                f"- 硬性校验：{hard_checks}",
                f"- 风险：{risks}",
                f"- 建议追问：{questions}",
                f"- 下一步：{item['next_action'] or '待人工判断'}",
                "",
            ]
        )

    lines.extend(["", "## 使用规则", ""])
    lines.append("1. “可推荐”仍需先确认当前意愿和简历版本。")
    lines.append("2. “先确认”只问一个关键问题，避免显得人机。")
    lines.append("3. “暂缓”不要直接推荐，先处理负向状态、重复推荐或项目归属问题。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate outreach readiness checks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_candidate_intelligence_columns(conn)
        items = build_items(load_records(conn))
    finally:
        conn.close()
    report = write_report(Path(args.output_dir).expanduser(), items)
    print(
        json.dumps(
            {
                "ok": True,
                "items": len(items),
                "ready": sum(1 for item in items if item["readiness"] == "可推荐"),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
