#!/usr/bin/env python3
"""Generate conservative candidate intelligence records from open follow-ups."""

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
MODEL_VERSION = "reply-followup-rules-v1"
BASELINE_MODEL_VERSION = "candidate-baseline-rules-v1"
POSITIVE_FEEDBACK = {"approved", "interviewing", "interview_passed", "offer", "hired"}
NEGATIVE_FEEDBACK = {"rejected", "interview_failed", "eliminated"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_intelligence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT NOT NULL,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    fit_score INTEGER DEFAULT 0,
    fit_level TEXT DEFAULT 'unrated',
    evidence_json TEXT DEFAULT '{}',
    risk_json TEXT DEFAULT '{}',
    strong_matches_json TEXT DEFAULT '[]',
    weak_matches_json TEXT DEFAULT '[]',
    verification_questions_json TEXT DEFAULT '[]',
    recommendation_decision TEXT,
    next_action TEXT,
    last_evaluated_at TEXT,
    model_version TEXT DEFAULT 'rules-v0',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(candidate_name, candidate_company, client, position)
)
"""


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


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidate_intelligence)")}
    for column, definition in CANDIDATE_INTELLIGENCE_EXTRA_COLUMNS.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE candidate_intelligence ADD COLUMN {column} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ci_client_position ON candidate_intelligence(client, position)"
    )
    conn.commit()


def load_followups(conn: sqlite3.Connection, include_closed: bool) -> list[sqlite3.Row]:
    where = "" if include_closed else "WHERE COALESCE(t.status, 'open') = 'open'"
    return conn.execute(
        f"""
        SELECT
            t.id AS task_id,
            t.candidate_id,
            ifnull(t.candidate_name, '') AS candidate_name,
            ifnull(t.candidate_company, '') AS candidate_company,
            ifnull(t.client, '') AS client,
            ifnull(t.position, '') AS position,
            ifnull(t.inferred_client, '') AS inferred_client,
            ifnull(t.inferred_position, '') AS inferred_position,
            ifnull(t.confirmed_client, '') AS confirmed_client,
            ifnull(t.confirmed_position, '') AS confirmed_position,
            ifnull(t.confirmation_status, '') AS confirmation_status,
            ifnull(t.match_confidence, 'unmatched') AS match_confidence,
            ifnull(t.match_reason, '') AS match_reason,
            ifnull(t.task_type, '') AS task_type,
            ifnull(t.priority, 2) AS priority,
            ifnull(t.draft_message, '') AS draft_message,
            ifnull(t.talk_strategy, '') AS talk_strategy,
            ifnull(t.talk_score, 0) AS talk_score,
            ifnull(t.talk_risk, '') AS talk_risk,
            ifnull(t.talk_missing, '') AS talk_missing,
            ifnull(r.id, 0) AS reply_id,
            ifnull(r.intent, '') AS intent,
            ifnull(r.sentiment, '') AS sentiment,
            ifnull(r.raw_text, '') AS raw_text,
            ifnull(r.candidate_title, '') AS candidate_title,
            ifnull(r.suggested_next_action, t.reason) AS suggested_next_action,
            ifnull(cp.function_tags_json, '[]') AS candidate_function_tags_json,
            ifnull(cp.risk_tags_json, '[]') AS candidate_profile_risks_json,
            ifnull(cp.profile_summary, '') AS candidate_profile_summary,
            ifnull(pp.hard_requirements_json, '[]') AS position_hard_requirements_json,
            ifnull(pp.ability_keywords_json, '[]') AS position_ability_keywords_json,
            ifnull(pp.exclusion_tags_json, '[]') AS position_exclusion_tags_json,
            ifnull(pp.soft_preferences_json, '[]') AS position_soft_preferences_json,
            ifnull(pp.risk_points_json, '[]') AS position_risk_points_json
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        LEFT JOIN candidate_profiles cp
            ON t.candidate_id = cp.candidate_id
        LEFT JOIN position_profiles pp
            ON (ifnull(t.confirmed_client, '') = pp.client OR ifnull(t.inferred_client, '') = pp.client OR ifnull(t.client, '') = pp.client)
           AND (ifnull(t.confirmed_position, '') = pp.position OR ifnull(t.inferred_position, '') = pp.position OR ifnull(t.position, '') = pp.position)
        {where}
        ORDER BY t.priority ASC, t.id ASC
        """
    ).fetchall()


def load_candidate_baselines(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            c.id AS candidate_id,
            ifnull(c.name, '') AS candidate_name,
            ifnull(c.company, '') AS candidate_company,
            ifnull(c.title, '') AS candidate_title,
            ifnull(c.education, '') AS education,
            ifnull(c.experience, '') AS experience,
            ifnull(c.skills, '') AS skills,
            ifnull(c.city, '') AS city,
            ifnull(c.client, '') AS client,
            ifnull(c.position, '') AS position,
            ifnull(c.status, 'new') AS status,
            ifnull(c.notes, '') AS notes,
            ifnull(c.source, '') AS source,
            ifnull(cp.education_level, '') AS education_level,
            ifnull(cp.seniority, '') AS seniority,
            ifnull(cp.industry_tags_json, '[]') AS candidate_industry_tags_json,
            ifnull(cp.function_tags_json, '[]') AS candidate_function_tags_json,
            ifnull(cp.risk_tags_json, '[]') AS candidate_profile_risks_json,
            ifnull(cp.profile_summary, '') AS candidate_profile_summary,
            ifnull(pp.education_requirement, '') AS position_education_requirement,
            ifnull(pp.experience_requirement, '') AS position_experience_requirement,
            ifnull(pp.hard_requirements_json, '[]') AS position_hard_requirements_json,
            ifnull(pp.ability_keywords_json, '[]') AS position_ability_keywords_json,
            ifnull(pp.exclusion_tags_json, '[]') AS position_exclusion_tags_json,
            ifnull(pp.soft_preferences_json, '[]') AS position_soft_preferences_json,
            ifnull(pp.risk_points_json, '[]') AS position_risk_points_json,
            ifnull(pp.jd_analysis_summary, '') AS jd_analysis_summary
        FROM candidates c
        LEFT JOIN candidate_profiles cp
            ON c.id = cp.candidate_id
        LEFT JOIN position_profiles pp
            ON c.client = pp.client AND c.position = pp.position
        WHERE ifnull(c.name, '') != ''
        ORDER BY c.id ASC
        """
    ).fetchall()


def project_values(row: sqlite3.Row) -> tuple[str, str, str]:
    client = row["confirmed_client"] or row["inferred_client"] or row["client"]
    position = row["confirmed_position"] or row["inferred_position"] or row["position"]
    confidence = row["match_confidence"] or "unmatched"
    if row["confirmation_status"] == "confirmed" and client and position:
        confidence = "confirmed"
    return client, position, confidence


def fit_level(score: int) -> str:
    if score >= 85:
        return "A-优先推进"
    if score >= 70:
        return "B-可推进"
    if score >= 55:
        return "C-需确认"
    return "D-暂缓"


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys([item for item in items if item]))


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return dedupe([" ".join(str(item or "").split()) for item in value if str(item or "").strip()])


def recommendation_decision(score: int, risks: list[str], questions: list[str]) -> str:
    if any("明确表达不匹配" in item or "负向反馈" in item for item in risks):
        return "暂缓"
    if score >= 82 and not risks:
        return "建议推荐"
    if score >= 70:
        return "先确认后推荐"
    if questions:
        return "补问后再判断"
    return "暂缓"


def build_match_card(
    row: sqlite3.Row,
    score: int,
    confidence: str,
    intent: str,
    evidence: list[str],
    risks: list[str],
    questions: list[str],
) -> tuple[list[str], list[str], list[str], str]:
    candidate_tags = parse_json_list(row["candidate_function_tags_json"])
    position_tags = parse_json_list(row["position_ability_keywords_json"])
    hard_requirements = parse_json_list(row["position_hard_requirements_json"])
    candidate_profile_risks = parse_json_list(row["candidate_profile_risks_json"])
    position_risks = parse_json_list(row["position_risk_points_json"])

    strong: list[str] = []
    weak: list[str] = []
    shared = [tag for tag in candidate_tags if tag in set(position_tags)]
    if shared:
        strong.append(f"能力标签重合：{'、'.join(shared[:4])}")
    if confidence in {"confirmed", "high"}:
        strong.append(f"项目归属可信：{confidence}")
    if intent in {"interested", "need_contact", "need_more_info"}:
        strong.append(f"候选人回复可推进：{intent}")
    if row["candidate_profile_summary"]:
        strong.append(f"履历摘要：{row['candidate_profile_summary']}")

    if position_tags and not shared:
        weak.append(f"岗位要求{'、'.join(position_tags[:4])}，候选人标签未明显命中")
    if hard_requirements:
        weak.append(f"需核实硬性门槛：{'、'.join(hard_requirements[:3])}")
    if candidate_profile_risks:
        weak.extend(candidate_profile_risks[:2])
    if confidence in {"low", "unmatched", ""}:
        weak.append("项目归属置信度不足")
    if not row["candidate_title"]:
        weak.append("缺候选人当前职位")

    card_risks = dedupe(risks + candidate_profile_risks[:2] + position_risks[:2])
    card_questions = dedupe(questions)
    if hard_requirements and not card_questions:
        card_questions.append("请确认学历、年限、地点、薪资和核心项目经历是否满足岗位硬性要求。")
    if shared:
        card_questions.append(f"请补问与{'、'.join(shared[:2])}相关的项目深度和个人职责。")

    decision = recommendation_decision(score, card_risks, card_questions)
    if decision == "建议推荐":
        strong.append("当前分数和风险均支持优先推荐")
    elif decision == "先确认后推荐":
        weak.append("分数可推进，但推荐前仍需补一轮关键确认")
    return dedupe(strong)[:6], dedupe(weak)[:6], dedupe(card_questions)[:6], decision


def title_tokens(title: str) -> list[str]:
    tokens: list[str] = []
    mapping = {
        "机械": ["机械方向"],
        "电源": ["电源方向"],
        "电力电子": ["电力电子方向"],
        "硬件": ["硬件方向"],
        "fpga": ["FPGA方向"],
        "device": ["Device方向"],
        "总监": ["管理级别"],
        "经理": ["管理级别"],
        "leader": ["管理级别"],
        "主管": ["管理级别"],
        "专家": ["专家级别"],
        "工程师": ["工程师序列"],
    }
    lower = (title or "").lower()
    for key, values in mapping.items():
        if key in lower:
            tokens.extend(values)
    return dedupe(tokens)


def load_feedback_index(conn: sqlite3.Connection) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT candidate_id, candidate_name, candidate_company, client, position,
                   feedback_type, status_after, reason_tags_json, feedback_detail, feedback_time
            FROM client_feedback_events
            ORDER BY datetime(feedback_time) DESC, id DESC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    feedback: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        keys = [
            (
                str(row["candidate_id"] or ""),
                row["candidate_name"] or "",
                row["candidate_company"] or "",
                row["client"] or "",
                row["position"] or "",
            ),
            (
                "",
                row["candidate_name"] or "",
                row["candidate_company"] or "",
                row["client"] or "",
                row["position"] or "",
            ),
        ]
        for key in keys:
            feedback.setdefault(key, dict(row))
    return feedback


def feedback_for(row: sqlite3.Row, client: str, position: str, feedback_index: dict[tuple[str, str, str, str, str], dict[str, Any]]) -> dict[str, Any] | None:
    keys = [
        (
            str(row["candidate_id"] or ""),
            row["candidate_name"] or "",
            row["candidate_company"] or "",
            client or "",
            position or "",
        ),
        (
            "",
            row["candidate_name"] or "",
            row["candidate_company"] or "",
            client or "",
            position or "",
        ),
    ]
    for key in keys:
        if key in feedback_index:
            return feedback_index[key]
    return None


def build_intelligence(row: sqlite3.Row, feedback_index: dict[tuple[str, str, str, str, str], dict[str, Any]]) -> dict[str, Any]:
    client, position, confidence = project_values(row)
    title = row["candidate_title"] or ""
    intent = row["intent"] or "unclear"
    talk_score = int(row["talk_score"] or 0)

    score = 45
    evidence: list[str] = []
    risks: list[str] = []
    questions: list[str] = []

    confidence_bonus = {
        "confirmed": 25,
        "high": 22,
        "medium": 14,
        "low": 4,
        "unmatched": -8,
        "": -8,
    }.get(confidence, -4)
    score += confidence_bonus
    evidence.append(f"项目匹配置信度：{confidence}")

    intent_bonus = {
        "interested": 16,
        "need_contact": 14,
        "need_more_info": 10,
        "salary_concern": 8,
        "not_interested": -18,
        "unclear": 0,
        "": 0,
    }.get(intent, 0)
    score += intent_bonus
    evidence.append(f"回复意图：{intent}")

    feedback = feedback_for(row, client, position, feedback_index)
    if feedback:
        feedback_type = feedback.get("feedback_type") or ""
        status_after = feedback.get("status_after") or ""
        evidence.append(f"最新客户反馈：{feedback_type} -> {status_after}")
        if feedback_type in POSITIVE_FEEDBACK:
            score += 18
            evidence.append("客户反馈为正向，作为优先推进正样本")
        elif feedback_type in NEGATIVE_FEEDBACK:
            score -= 24
            risks.append(f"客户负向反馈：{feedback.get('feedback_detail') or status_after}")
        elif feedback_type == "hold":
            score -= 6
            risks.append("客户反馈暂缓，需要等待新条件或补充材料")

    if talk_score >= 85:
        score += 8
        evidence.append(f"话术质量分较高：{talk_score}")
    elif talk_score >= 70:
        score += 4
        evidence.append(f"话术质量分可用：{talk_score}")
    elif talk_score:
        score -= 2
        risks.append(f"话术分偏低：{talk_score}")

    if title:
        tokens = title_tokens(title)
        evidence.append(f"候选人头衔：{title}")
        if tokens:
            evidence.append(f"头衔信号：{'、'.join(tokens)}")
        if position and any(token.lower() in (position + title).lower() for token in ["机械", "电源", "硬件", "fpga", "device"]):
            score += 6
            evidence.append("头衔与岗位方向有直接关键词重合")
    else:
        score -= 5
        risks.append("缺候选人头衔")

    if not client:
        score -= 6
        risks.append("客户未确认")
        questions.append("当前可透露的客户名称或客户类型是什么？")
    if not position:
        score -= 6
        risks.append("岗位未确认")
        questions.append("这位候选人实际对应哪个岗位？")
    if confidence in ("low", "unmatched", ""):
        risks.append("项目置信度不足，推进前需要人工确认")
    if intent == "not_interested":
        risks.append("候选人明确表达不匹配或不感兴趣")
        questions.append("是否保留为其他岗位潜在人选，还是归档为拒绝？")
    if intent == "salary_concern":
        risks.append("薪资需要先对齐")
        questions.append("目前总包、固定、奖金结构和期望区间是多少？")
    if intent == "need_more_info":
        questions.append("候选人最关心公司、年限、地点还是薪资？")
    if intent == "need_contact":
        questions.append("加微信后优先确认求职意愿、地点和薪资区间。")
    if intent == "interested":
        questions.append("当前职责、项目经历、薪资期望、地点接受度是否匹配？")

    score = max(20, min(96, score))
    strong_matches, weak_matches, verification_questions, decision = build_match_card(
        row, score, confidence, intent, evidence, risks, questions
    )
    next_action = decide_next_action(row, score, confidence, intent, client, position)

    return {
        "candidate_id": row["candidate_id"],
        "candidate_name": row["candidate_name"] or "未识别",
        "candidate_company": row["candidate_company"] or "",
        "client": client,
        "position": position,
        "fit_score": score,
        "fit_level": fit_level(score),
        "evidence_json": json.dumps(
            {
                "task_id": row["task_id"],
                "reply_id": row["reply_id"],
                "evidence": dedupe(evidence),
                "questions": dedupe(questions),
                "strong_matches": strong_matches,
                "weak_matches": weak_matches,
                "recommendation_decision": decision,
                "raw_reply": row["raw_text"],
                "match_reason": row["match_reason"],
                "talk_strategy": row["talk_strategy"],
                "client_feedback": feedback or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "risk_json": json.dumps(
            {
                "risks": dedupe(risks),
                "talk_risk": row["talk_risk"],
                "talk_missing": row["talk_missing"],
                "card_risks": dedupe(risks),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "strong_matches_json": json.dumps(strong_matches, ensure_ascii=False),
        "weak_matches_json": json.dumps(weak_matches, ensure_ascii=False),
        "verification_questions_json": json.dumps(verification_questions, ensure_ascii=False),
        "recommendation_decision": decision,
        "next_action": next_action,
        "last_evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "model_version": MODEL_VERSION,
    }


def text_blob(*values: Any) -> str:
    return " ".join(str(value or "").lower() for value in values)


def shared_terms(left: list[str], right: list[str], blob: str = "") -> list[str]:
    right_set = {item.lower() for item in right}
    matches: list[str] = []
    for item in left:
        lowered = item.lower()
        if lowered in right_set or (lowered and lowered in blob):
            matches.append(item)
    return dedupe(matches)


def build_candidate_baseline_intelligence(row: sqlite3.Row, feedback_index: dict[tuple[str, str, str, str, str], dict[str, Any]]) -> dict[str, Any]:
    client = row["client"] or ""
    position = row["position"] or ""
    status = row["status"] or "new"
    title = row["candidate_title"] or ""
    company = row["candidate_company"] or ""
    city = row["city"] or ""

    candidate_tags = parse_json_list(row["candidate_function_tags_json"])
    industry_tags = parse_json_list(row["candidate_industry_tags_json"])
    candidate_risks = parse_json_list(row["candidate_profile_risks_json"])
    position_tags = parse_json_list(row["position_ability_keywords_json"])
    hard_requirements = parse_json_list(row["position_hard_requirements_json"])
    position_risks = parse_json_list(row["position_risk_points_json"])
    exclusions = parse_json_list(row["position_exclusion_tags_json"])
    title_signals = title_tokens(title)
    blob = text_blob(title, company, row["skills"], row["experience"], row["candidate_profile_summary"], position)
    shared = shared_terms(candidate_tags + industry_tags + title_signals, position_tags + hard_requirements, blob)

    score = 50
    evidence: list[str] = []
    risks: list[str] = []
    questions: list[str] = []
    strong: list[str] = []
    weak: list[str] = []

    evidence.append("来源：候选人主表与岗位画像的保守规则匹配")
    if client and position:
        score += 12
        evidence.append("候选人已有客户和岗位归属")
        strong.append(f"项目归属：{client}/{position}")
    elif client or position:
        score += 4
        risks.append("客户或岗位归属不完整")
    else:
        score -= 15
        risks.append("缺客户和岗位归属")

    if title:
        evidence.append(f"当前职位：{title}")
        if title_signals:
            evidence.append(f"头衔信号：{'、'.join(title_signals)}")
    else:
        score -= 5
        risks.append("缺当前职位")

    if row["candidate_profile_summary"]:
        strong.append(f"履历摘要：{row['candidate_profile_summary']}")
    if candidate_tags:
        evidence.append(f"候选人能力标签：{'、'.join(candidate_tags[:5])}")
    if position_tags:
        evidence.append(f"岗位能力关键词：{'、'.join(position_tags[:5])}")

    if shared:
        score += min(18, 6 + 3 * len(shared))
        strong.append(f"能力/方向命中：{'、'.join(shared[:5])}")
    elif position_tags:
        score -= 4
        weak.append(f"岗位关键词未明显命中：{'、'.join(position_tags[:4])}")

    if hard_requirements:
        weak.append(f"需核实硬性门槛：{'、'.join(hard_requirements[:4])}")
        questions.append("请确认学历、年限、地点、薪资和核心项目经历是否满足岗位硬性要求。")
    if row["position_education_requirement"]:
        questions.append(f"请确认学历是否满足：{row['position_education_requirement']}。")
    if row["position_experience_requirement"]:
        questions.append(f"请确认年限/经验是否满足：{row['position_experience_requirement']}。")
    if city:
        evidence.append(f"当前地点：{city}")
    else:
        risks.append("缺地点信息")
        questions.append("请确认当前所在地和目标工作地点接受度。")

    if status in {"recommended", "client_approved", "interviewing", "offered", "hired"}:
        score += 15
        strong.append(f"历史状态支持推进：{status}")
    elif status in {"contacted", "greeted", "replied"}:
        score += 8
        strong.append(f"已有触达/回复状态：{status}")
    elif status in {"eliminated", "passed", "client_rejected", "duplicate"}:
        score -= 20
        risks.append(f"历史状态不宜直接推进：{status}")

    feedback = feedback_for(row, client, position, feedback_index)
    if feedback:
        feedback_type = feedback.get("feedback_type") or ""
        status_after = feedback.get("status_after") or ""
        evidence.append(f"最新客户反馈：{feedback_type} -> {status_after}")
        if feedback_type in POSITIVE_FEEDBACK:
            score += 16
            strong.append("客户反馈为正向")
        elif feedback_type in NEGATIVE_FEEDBACK:
            score -= 24
            risks.append(f"客户负向反馈：{feedback.get('feedback_detail') or status_after}")

    if candidate_risks:
        risks.extend(candidate_risks[:3])
    if position_risks:
        risks.extend(position_risks[:3])
    if exclusions:
        risks.append(f"需排除项核验：{'、'.join(exclusions[:3])}")
    if not row["education"] and not row["education_level"]:
        weak.append("缺学历信息")
    if not company:
        weak.append("缺当前公司")

    score = max(20, min(92, score))
    strong = dedupe(strong)[:6]
    weak = dedupe(weak)[:6]
    risks = dedupe(risks)[:8]
    questions = dedupe(questions)[:6]
    decision = recommendation_decision(score, risks, questions)
    if score >= 75 and not any(item.startswith("历史状态不宜") for item in risks):
        next_action = "补一轮硬性门槛确认；通过后可进入推荐前校验。"
    elif client and position:
        next_action = "保守保留在岗位池，优先补职位、地点、薪资和项目经历。"
    else:
        next_action = "先补客户/岗位归属，再重算匹配。"

    return {
        "candidate_id": row["candidate_id"],
        "candidate_name": row["candidate_name"] or "未识别",
        "candidate_company": row["candidate_company"] or "",
        "client": client,
        "position": position,
        "fit_score": score,
        "fit_level": fit_level(score),
        "evidence_json": json.dumps(
            {
                "source": "candidate_baseline",
                "candidate_status": status,
                "evidence": dedupe(evidence),
                "questions": questions,
                "strong_matches": strong,
                "weak_matches": weak,
                "recommendation_decision": decision,
                "client_feedback": feedback or {},
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "risk_json": json.dumps(
            {
                "risks": risks,
                "card_risks": risks,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "strong_matches_json": json.dumps(strong, ensure_ascii=False),
        "weak_matches_json": json.dumps(weak, ensure_ascii=False),
        "verification_questions_json": json.dumps(questions, ensure_ascii=False),
        "recommendation_decision": decision,
        "next_action": next_action,
        "last_evaluated_at": datetime.now().isoformat(timespec="seconds"),
        "model_version": BASELINE_MODEL_VERSION,
    }


def decide_next_action(
    row: sqlite3.Row,
    score: int,
    confidence: str,
    intent: str,
    client: str,
    position: str,
) -> str:
    if intent == "not_interested":
        return "记录拒绝原因；如方向确实不匹配，暂缓推进。"
    if confidence in ("low", "unmatched", "") or not position:
        return "先补客户/岗位确认，再决定是否继续沟通。"
    if intent == "need_contact":
        return "可转微信；微信里先确认求职意愿、地点和薪资。"
    if intent == "need_more_info":
        return "补公司/岗位关键信息，但一次只问一个关键问题。"
    if intent == "salary_concern":
        return "先对齐薪资结构和期望，再判断是否推荐。"
    if score >= 85:
        return "优先约 10 分钟电话，确认核心经历和意愿。"
    if score >= 70:
        return "可继续沟通，先确认硬性门槛。"
    return "人工复核后再推进。"


def existing_keys(conn: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    return {
        (
            row["candidate_name"] or "",
            row["candidate_company"] or "",
            row["client"] or "",
            row["position"] or "",
        )
        for row in conn.execute(
            "SELECT candidate_name, candidate_company, client, position FROM candidate_intelligence"
        )
    }


def existing_candidate_keys(conn: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    return {
        (
            row["candidate_name"] or "",
            row["candidate_company"] or "",
            row["client"] or "",
            row["position"] or "",
        )
        for row in conn.execute(
            "SELECT candidate_name, candidate_company, client, position FROM candidate_intelligence"
        )
    }


def upsert_records(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> dict[str, int]:
    before = existing_keys(conn)
    sql = """
    INSERT INTO candidate_intelligence (
        candidate_id, candidate_name, candidate_company, client, position,
        fit_score, fit_level, evidence_json, risk_json,
        strong_matches_json, weak_matches_json, verification_questions_json,
        recommendation_decision, next_action,
        last_evaluated_at, model_version, updated_at
    ) VALUES (
        :candidate_id, :candidate_name, :candidate_company, :client, :position,
        :fit_score, :fit_level, :evidence_json, :risk_json,
        :strong_matches_json, :weak_matches_json, :verification_questions_json,
        :recommendation_decision, :next_action,
        :last_evaluated_at, :model_version, datetime('now','localtime')
    )
    ON CONFLICT(candidate_name, candidate_company, client, position) DO UPDATE SET
        candidate_id=excluded.candidate_id,
        fit_score=excluded.fit_score,
        fit_level=excluded.fit_level,
        evidence_json=excluded.evidence_json,
        risk_json=excluded.risk_json,
        strong_matches_json=excluded.strong_matches_json,
        weak_matches_json=excluded.weak_matches_json,
        verification_questions_json=excluded.verification_questions_json,
        recommendation_decision=excluded.recommendation_decision,
        next_action=excluded.next_action,
        last_evaluated_at=excluded.last_evaluated_at,
        model_version=excluded.model_version,
        updated_at=datetime('now','localtime')
    """
    for record in records:
        conn.execute(sql, record)
    conn.commit()
    inserted = sum(
        1
        for record in records
        if (
            record["candidate_name"],
            record["candidate_company"],
            record["client"],
            record["position"],
        )
        not in before
    )
    return {"inserted": inserted, "updated": len(records) - inserted}


def record_key(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(record.get("candidate_id") or ""),
        record["candidate_name"] or "",
        record["candidate_company"] or "",
        record["client"] or "",
        record["position"] or "",
    )


def prefer_record(new: dict[str, Any], old: dict[str, Any] | None) -> bool:
    if old is None:
        return True
    if bool(new.get("candidate_id")) != bool(old.get("candidate_id")):
        return bool(new.get("candidate_id"))
    if int(new["fit_score"]) != int(old["fit_score"]):
        return int(new["fit_score"]) > int(old["fit_score"])
    return bool(new.get("candidate_company")) and not bool(old.get("candidate_company"))


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for record in records:
        key = record_key(record)
        if prefer_record(record, best.get(key)):
            best[key] = record
    return list(best.values())


def cleanup_duplicate_intelligence(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT id, candidate_id, candidate_name, candidate_company, client, position, fit_score
        FROM candidate_intelligence
        ORDER BY
          CASE WHEN candidate_id IS NOT NULL THEN 0 ELSE 1 END,
          fit_score DESC,
          updated_at DESC,
          id DESC
        """
    ).fetchall()
    seen: set[tuple[str, str, str, str, str]] = set()
    delete_ids: list[int] = []
    for row in rows:
        key = (
            str(row["candidate_id"] or ""),
            row["candidate_name"] or "",
            row["candidate_company"] or "",
            row["client"] or "",
            row["position"] or "",
        )
        if key in seen:
            delete_ids.append(int(row["id"]))
        else:
            seen.add(key)
    if delete_ids:
        conn.executemany("DELETE FROM candidate_intelligence WHERE id = ?", [(item,) for item in delete_ids])
        conn.commit()
    return len(delete_ids)


def first_line(text: str, limit: int = 84) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("..." if len(clean) > limit else "")


def write_report(output_dir: Path, records: list[dict[str, Any]], stats: dict[str, int], dry_run: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else "applied"
    path = output_dir / f"猎聘候选人智能画像_{suffix}_{stamp}.md"

    levels = Counter(record["fit_level"] for record in records)
    by_project = Counter(
        f"{record['client'] or '未定客户'}/{record['position'] or '未定岗位'}"
        for record in records
    )
    top_records = sorted(records, key=lambda item: item["fit_score"], reverse=True)

    lines = [
        "# 猎聘候选人智能画像",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"入库状态：{'未入库（干跑验证）' if dry_run else '已写入 candidate_intelligence'}",
        "",
        "## 概览",
        "",
        f"- 本次生成：{len(records)} 条",
        f"- 新增入库：{stats.get('inserted', 0)} 条",
        f"- 更新覆盖：{stats.get('updated', 0)} 条",
        f"- 本次去重：{stats.get('deduped', 0)} 条",
        f"- 清理旧重复：{stats.get('cleaned', 0)} 条",
        f"- 等级分布：{'、'.join(f'{k} {v}' for k, v in levels.items()) or '暂无'}",
        f"- 项目分布：{'、'.join(f'{k} {v}' for k, v in by_project.most_common(8)) or '暂无'}",
        "",
        "## 优先处理",
        "",
        "| 分数 | 等级 | 推荐判断 | 候选人 | 项目 | 强匹配点 | 弱匹配/待确认 | 下一步 |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for record in top_records[:10]:
        project = f"{record['client'] or '未定客户'}/{record['position'] or '未定岗位'}"
        strong = "；".join(json.loads(record["strong_matches_json"])[:2]) or "暂无"
        weak = "；".join(json.loads(record["weak_matches_json"])[:2]) or "暂无"
        lines.append(
            f"| {record['fit_score']} | {record['fit_level']} | {record['recommendation_decision']} | "
            f"{record['candidate_name']} | {project} | {first_line(strong, 52)} | "
            f"{first_line(weak, 52)} | {first_line(record['next_action'], 48)} |"
        )

    lines.extend(["", "## 人岗匹配评分卡", ""])
    for record in top_records:
        risks = "；".join(json.loads(record["risk_json"]).get("risks") or []) or "无明显风险"
        questions = "；".join(json.loads(record["verification_questions_json"]) or []) or "无"
        strong = "；".join(json.loads(record["strong_matches_json"]) or []) or "暂无"
        weak = "；".join(json.loads(record["weak_matches_json"]) or []) or "暂无"
        project = f"{record['client'] or '未定客户'}/{record['position'] or '未定岗位'}"
        lines.extend(
            [
                f"### {record['candidate_name']}｜{project}",
                "",
                f"- 评分：{record['fit_score']}（{record['fit_level']}）",
                f"- 推荐判断：{record['recommendation_decision']}",
                f"- 强匹配点：{strong}",
                f"- 弱匹配/待确认：{weak}",
                f"- 风险：{risks}",
                f"- 建议追问：{questions}",
                f"- 下一步：{record['next_action']}",
                "",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate candidate intelligence from follow-up tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        followups = load_followups(conn, args.include_closed)
        feedback_index = load_feedback_index(conn)
        raw_records = [build_intelligence(row, feedback_index) for row in followups if row["candidate_name"]]
        existing = existing_candidate_keys(conn)
        if not args.skip_baseline:
            baseline_rows = load_candidate_baselines(conn)
            for row in baseline_rows:
                key = (
                    row["candidate_name"] or "",
                    row["candidate_company"] or "",
                    row["client"] or "",
                    row["position"] or "",
                )
                if key not in existing:
                    raw_records.append(build_candidate_baseline_intelligence(row, feedback_index))
        records = dedupe_records(raw_records)
        stats = {"inserted": 0, "updated": 0}
        if not args.dry_run:
            stats = upsert_records(conn, records)
            stats["deduped"] = len(raw_records) - len(records)
            stats["cleaned"] = cleanup_duplicate_intelligence(conn)
    finally:
        conn.close()

    report = write_report(Path(args.output_dir).expanduser(), records, stats, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "records": len(records),
                "inserted": stats.get("inserted", 0),
                "updated": stats.get("updated", 0),
                "deduped": stats.get("deduped", 0),
                "cleaned": stats.get("cleaned", 0),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
