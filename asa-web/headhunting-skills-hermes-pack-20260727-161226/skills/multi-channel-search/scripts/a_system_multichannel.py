#!/usr/bin/env python3
"""A System job-driven multi-channel search orchestration."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote


DEFAULT_DB = Path(
    "/Users/messi/Documents/Codex/2026-06-26/re/outputs/"
    "talent_system_v3_20260629.db"
)


def _json_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError("岗位画像字段必须是 JSON 数组")
    return parsed


def load_position_context(
    db_path: str | Path, client: str, job: str
) -> dict[str, Any]:
    """Load one canonical open position and its A System job/profile records."""
    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        position_rows = conn.execute(
            """
            SELECT * FROM positions
            WHERE client=? AND title=?
            ORDER BY id
            """,
            (client, job),
        ).fetchall()
        closed_markers = ("关闭", "停止", "完成", "下架", "取消", "冻结", "closed")
        position = next(
            (
                row
                for row in position_rows
                if not any(
                    marker in str(row["status"] or "").lower()
                    for marker in closed_markers
                )
            ),
            None,
        )
        if position is None:
            raise ValueError(f"未找到在招岗位：{client}/{job}")

        job_row = conn.execute(
            """
            SELECT j.*
            FROM jobs j
            JOIN clients c ON c.id=j.client_id
            WHERE c.name=? AND j.title=?
            ORDER BY j.id
            LIMIT 1
            """,
            (client, job),
        ).fetchone()
        if job_row is None:
            raise ValueError(f"未找到 A 系统岗位关系：{client}/{job}")

        profile = conn.execute(
            """
            SELECT * FROM position_profiles
            WHERE client=? AND position=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (client, job),
        ).fetchone()
        if profile is None:
            raise ValueError(f"未找到岗位画像：{client}/{job}")

        source_position_ids = [
            int(value) for value in _json_list(profile["source_position_ids_json"])
        ]
        if int(position["id"]) in source_position_ids:
            source_binding = "position_id"
        elif int(job_row["id"]) in source_position_ids:
            source_binding = "job_id_legacy"
        else:
            raise ValueError(
                "画像来源岗位不匹配："
                f"position_id={position['id']}, job_id={job_row['id']}, "
                f"sources={source_position_ids}"
            )

        return {
            "client": client,
            "job": job,
            "job_id": int(job_row["id"]),
            "job_status": job_row["status"],
            "position_id": int(position["id"]),
            "position_status": position["status"] or "",
            "source_binding": source_binding,
            "location": position["location"] or "",
            "experience": position["experience"] or "",
            "summary": position["summary"] or profile["jd_analysis_summary"] or "",
            "hard_requirements": _json_list(profile["hard_requirements_json"]),
            "ability_keywords": _json_list(profile["ability_keywords_json"]),
            "target_companies": _json_list(profile["target_companies_json"]),
            "exclusions": _json_list(profile["exclusion_tags_json"]),
            "search_keywords": _json_list(profile["search_keywords_json"]),
            "source_position_ids": source_position_ids,
            "risk_points": _json_list(profile["risk_points_json"]),
            "pitch_points": _json_list(profile["pitch_points_json"]),
        }
    finally:
        conn.close()


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", text)


def _identity_key(name: Any, company: Any, title: Any) -> str:
    values = [_normalize_text(name), _normalize_text(company), _normalize_text(title)]
    if not all(values):
        return ""
    return "|".join(values)


def load_exclusion_set(
    db_path: str | Path, client: str, job: str
) -> dict[str, Any]:
    """Load current job identities and preserve the latest manual review result."""
    context = load_position_context(db_path, client, job)
    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                c.id AS candidate_id,
                c.name,
                c.company,
                c.title,
                c.status,
                c.source,
                c.xsaas_id,
                c.elimination_reason,
                jc.id AS job_candidate_id,
                jc.clean_stage,
                (
                    SELECT ce.event_status
                    FROM candidate_events ce
                    WHERE ce.job_candidate_id=jc.id
                      AND ce.event_type='resume_review_completed'
                    ORDER BY ce.event_time DESC, ce.id DESC
                    LIMIT 1
                ) AS latest_review
            FROM candidates c
            LEFT JOIN job_candidates jc
              ON jc.job_id=?
             AND jc.raw_position=?
             AND jc.source_candidate_id=CAST(c.id AS TEXT)
            WHERE c.client=? AND c.position=?
            ORDER BY c.id
            """,
            (context["job_id"], job, client, job),
        ).fetchall()
    finally:
        conn.close()

    records: list[dict[str, Any]] = []
    summary = {"stopped": 0, "contacted": 0, "existing": 0}
    by_candidate_id: dict[str, dict[str, Any]] = {}
    by_xsaas_id: dict[str, dict[str, Any]] = {}
    by_identity: dict[str, dict[str, Any]] = {}
    for row in rows:
        status = _normalize_text(row["status"])
        stage = str(row["clean_stage"] or "")
        latest_review = _normalize_text(row["latest_review"])
        if (
            latest_review == "stop"
            or status in {"screen_rejected", "rejected", "client_rejected"}
            or stage.startswith("H5 ")
        ):
            disposition = "stopped"
        elif status == "contacted" or stage == "已触达":
            disposition = "contacted"
        else:
            disposition = "existing"
        candidate_id = (
            int(row["candidate_id"])
            if row["candidate_id"] is not None
            else None
        )
        record = {
            "candidate_id": candidate_id,
            "job_candidate_id": (
                int(row["job_candidate_id"])
                if row["job_candidate_id"] is not None
                else None
            ),
            "name": row["name"] or "",
            "company": row["company"] or "",
            "title": row["title"] or "",
            "source": row["source"] or "",
            "xsaas_id": row["xsaas_id"] or "",
            "latest_review": row["latest_review"] or "",
            "clean_stage": stage,
            "elimination_reason": row["elimination_reason"] or "",
            "disposition": disposition,
        }
        records.append(record)
        summary[disposition] += 1
        if record["candidate_id"] is not None:
            by_candidate_id[str(record["candidate_id"])] = record
        if record["xsaas_id"]:
            by_xsaas_id[_normalize_text(record["xsaas_id"])] = record
        identity = _identity_key(record["name"], record["company"], record["title"])
        if identity:
            by_identity[identity] = record
    return {
        "client": client,
        "job": job,
        "summary": summary,
        "records": records,
        "by_candidate_id": by_candidate_id,
        "by_xsaas_id": by_xsaas_id,
        "by_identity": by_identity,
    }


def classify_duplicate(
    candidate: dict[str, Any], exclusion_set: dict[str, Any]
) -> dict[str, Any]:
    """Return duplicate evidence without treating a masked name alone as identity."""
    candidate_id = candidate.get("candidate_id")
    if candidate_id not in (None, ""):
        record = exclusion_set["by_candidate_id"].get(str(candidate_id))
        if record:
            return {
                "duplicate": True,
                "reason": "local_candidate_id",
                "disposition": record["disposition"],
                "record": record,
            }

    xsaas_id = _normalize_text(candidate.get("xsaas_id"))
    if xsaas_id:
        record = exclusion_set["by_xsaas_id"].get(xsaas_id)
        if record:
            return {
                "duplicate": True,
                "reason": "xsaas_id",
                "disposition": record["disposition"],
                "record": record,
            }

    identity = _identity_key(
        candidate.get("name"), candidate.get("company"), candidate.get("title")
    )
    if identity:
        record = exclusion_set["by_identity"].get(identity)
        if record:
            return {
                "duplicate": True,
                "reason": "identity_evidence",
                "disposition": record["disposition"],
                "record": record,
            }

    return {"duplicate": False, "reason": "no_unique_match", "disposition": "new"}


def _unique_queries(
    rows: list[dict[str, Any]], exhausted: set[str], limit: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        query = " ".join(str(row.get("query") or "").split())
        key = _normalize_text(query)
        if not query or key in seen or key in exhausted:
            continue
        seen.add(key)
        result.append({**row, "query": query})
        if len(result) >= limit:
            break
    return result


def build_search_plan(
    context: dict[str, Any],
    exclusion_set: dict[str, Any],
    search_history: list[dict[str, Any]] | None = None,
    *,
    max_queries_per_channel: int = 6,
) -> dict[str, Any]:
    """Build deterministic job-profile queries for Liepin and X-SaaS."""
    history = search_history or []
    skipped_queries = [
        str(row.get("query") or "").strip()
        for row in history
        if str(row.get("status") or "").lower() == "learned"
        and int(row.get("recommended_count") or 0) == 0
        and str(row.get("query") or "").strip()
    ]
    exhausted = {_normalize_text(query) for query in skipped_queries}

    abilities = [str(value).strip() for value in context["ability_keywords"] if str(value).strip()]
    ability_anchor = " ".join(abilities[:2])
    profile_rounds = [
        {
            "round": "profile-core",
            "query": query,
            "purpose": "岗位画像核心能力",
        }
        for query in context["search_keywords"]
    ]
    company_rounds = [
        {
            "round": "target-company",
            "query": f"{company} {ability_anchor}".strip(),
            "purpose": "目标公司加能力证据",
        }
        for company in context["target_companies"]
    ]
    liepin = _unique_queries(
        profile_rounds + company_rounds, exhausted, max_queries_per_channel
    )

    xsaas_core = [
        {
            "round": "internal-skill",
            "query": query,
            "purpose": "内库能力全文检索",
        }
        for query in context["search_keywords"]
    ]
    xsaas_company = [
        {
            "round": "internal-company-skill",
            "query": f"{company} {ability_anchor}".strip(),
            "purpose": "内库目标公司经历加能力证据",
        }
        for company in context["target_companies"]
    ]
    xsaas = _unique_queries(
        xsaas_core + xsaas_company, exhausted, max_queries_per_channel
    )

    stop_reasons = sorted(
        {
            str(row.get("elimination_reason") or "").strip()
            for row in exclusion_set["records"]
            if row.get("disposition") == "stopped"
            and str(row.get("elimination_reason") or "").strip()
        }
    )
    return {
        "mode": "a-system-job",
        "client": context["client"],
        "job": context["job"],
        "job_id": context["job_id"],
        "position_id": context["position_id"],
        "channels": {"liepin": liepin, "xsaas": xsaas},
        "review_gates": {
            "hard_requirements": context["hard_requirements"],
            "ability_keywords": abilities,
            "negative_rules": context["exclusions"],
            "risk_points": context["risk_points"],
            "historical_stop_reasons": stop_reasons,
            "detail_review_required": True,
        },
        "exclusion_summary": exclusion_set["summary"],
        "skipped_queries": skipped_queries,
    }


def classify_channel_snapshot(
    channel: str,
    snapshot: dict[str, Any],
    *,
    expected_query: str,
) -> dict[str, Any]:
    """Classify a captured channel state before recording search results."""
    href = str(snapshot.get("href") or "")
    title = str(snapshot.get("title") or "")
    body = str(snapshot.get("body") or "")
    combined = f"{href} {title} {body}".lower()
    if "login" in href.lower() or "登录" in body:
        return {
            "ready": False,
            "status": "login_required",
            "channel": channel,
            "reason": "渠道登录态失效",
        }

    expected_key = _normalize_text(expected_query)
    if channel == "liepin":
        actual_key = _normalize_text(snapshot.get("input_value"))
        if not actual_key or actual_key != expected_key:
            return {
                "ready": False,
                "status": "invalid_search",
                "channel": channel,
                "reason": "猎聘关键词未真实提交",
            }
        card_count = int(snapshot.get("card_count") or 0)
        relevant_count = int(snapshot.get("relevant_card_count") or 0)
        total = str(snapshot.get("total") or "").strip()
        if total == "3000+" and card_count and relevant_count == 0:
            return {
                "ready": False,
                "status": "generic_feed",
                "channel": channel,
                "reason": "猎聘返回泛化推荐流",
            }
        return {
            "ready": True,
            "status": "ready" if card_count else "zero_results",
            "channel": channel,
            "result_count": total,
            "card_count": card_count,
            "relevant_card_count": relevant_count,
        }

    if channel == "xsaas":
        decoded_href = _normalize_text(unquote(href))
        if expected_key not in decoded_href:
            return {
                "ready": False,
                "status": "stale_query",
                "channel": channel,
                "reason": "X-SaaS 页面仍是旧关键词或缓存结果",
            }
        candidate_count = int(snapshot.get("candidate_count") or 0)
        return {
            "ready": True,
            "status": "ready" if candidate_count else "zero_results",
            "channel": channel,
            "candidate_count": candidate_count,
        }

    raise ValueError(f"不支持的渠道：{channel}")


def _first_value(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return ""


def normalize_candidate(
    channel: str, raw: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Normalize one channel record without assigning a local candidate ID."""
    if channel not in {"liepin", "xsaas"}:
        raise ValueError(f"不支持的渠道：{channel}")
    name = str(_first_value(raw, "name", "candidate_name")).strip()
    company = str(
        _first_value(raw, "company", "current_company", "candidate_company")
    ).strip()
    title = str(
        _first_value(raw, "title", "current_position", "candidate_title")
    ).strip()
    missing = [
        label
        for label, value in (("姓名", name), ("公司", company), ("职位", title))
        if not value
    ]
    if missing:
        raise ValueError(f"候选人关键字段缺失：{','.join(missing)}")
    if re.search(r"大学|学院|学校", company):
        raise ValueError(f"公司字段疑似学校：{company}")

    source_url = str(
        _first_value(raw, "source_url", "resume_url", "url")
    ).strip()
    profile_text = str(
        _first_value(raw, "profile_text", "candidate_profile_text", "content", "full")
    ).strip()
    xsaas_id = str(_first_value(raw, "xsaas_id", "candidate_id")).strip() if channel == "xsaas" else ""
    source_candidate_id = (
        xsaas_id
        if channel == "xsaas"
        else str(_first_value(raw, "res_id_encode", "resume_id", "source_candidate_id")).strip()
    )
    work_history = raw.get("work") if isinstance(raw.get("work"), list) else []
    education_history = raw.get("education_history") if isinstance(raw.get("education_history"), list) else []
    work_text = "\n".join(
        " · ".join(
            value for value in (
                str(item.get("company") or "").strip(),
                str(item.get("title") or "").strip(),
                str(item.get("dates") or "").strip(),
            ) if value
        )
        for item in work_history if isinstance(item, dict)
    )
    education_text = "\n".join(
        " · ".join(
            value for value in (
                str(item.get("school") or "").strip(),
                str(item.get("major") or "").strip(),
                str(item.get("degree") or "").strip(),
                str(item.get("dates") or "").strip(),
            ) if value
        )
        for item in education_history if isinstance(item, dict)
    )
    full_text = str(_first_value(raw, "full_text", "profile_text") or profile_text).strip()
    work_text = str(_first_value(raw, "work_text") or work_text).strip()
    project_text = str(_first_value(raw, "project_text")).strip()
    education_text = str(_first_value(raw, "education_text") or education_text).strip()
    capture_status = str(_first_value(raw, "resume_capture_status")).strip()
    if capture_status:
        missing_sections = []
        if not source_url:
            missing_sections.append("来源链接")
        if len(full_text) < 100:
            missing_sections.append("完整履历")
        if len(work_text) < 20:
            missing_sections.append("工作经历")
        if len(education_text) < 10:
            missing_sections.append("教育经历")
        if capture_status != "complete" or missing_sections:
            detail = "、".join(dict.fromkeys(missing_sections or raw.get("resume_capture_missing") or ["详情抓取失败"]))
            error = str(raw.get("resume_capture_error") or "").strip()
            raise ValueError(f"完整简历未通过入库校验：{detail}{f'；{error}' if error else ''}")
    return {
        "channel": channel,
        "source": channel,
        "client": context["client"],
        "job": context["job"],
        "job_id": context["job_id"],
        "name": name,
        "company": company,
        "title": title,
        "education": str(_first_value(raw, "education", "education_level")).strip(),
        "experience": str(_first_value(raw, "experience", "workYears", "years")).strip(),
        "city": str(_first_value(raw, "city", "location", "expected_city")).strip(),
        "profile_text": profile_text,
        "full_text": full_text,
        "work_text": work_text,
        "project_text": project_text,
        "education_text": education_text,
        "source_url": source_url,
        "source_candidate_id": source_candidate_id,
        "source_query": str(_first_value(raw, "query", "source_query")).strip(),
        "xsaas_id": xsaas_id,
        "stage": (
            "S1 新增寻访/待复核"
            if channel == "liepin"
            else "X1 X-SaaS新增/待复核"
        ),
        "raw_status": (
            "search_shortlisted"
            if channel == "liepin"
            else "xsaas_search_shortlisted"
        ),
        "event_status": "pending_review",
        "flow_bucket": "待复核",
        "raw": raw,
    }


def stage_candidates(
    records: list[dict[str, Any]],
    context: dict[str, Any],
    exclusion_set: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize candidates and separate historical and same-batch duplicates."""
    result: dict[str, list[dict[str, Any]]] = {
        "accepted": [],
        "existing": [],
        "batch_duplicates": [],
        "errors": [],
    }
    seen: set[str] = set()
    for raw in records:
        try:
            channel = str(raw.get("channel") or raw.get("source") or "").lower()
            candidate = normalize_candidate(channel, raw, context)
        except ValueError as exc:
            result["errors"].append({"raw": raw, "error": str(exc)})
            continue

        duplicate_probe = {
            "name": candidate["name"],
            "company": candidate["company"],
            "title": candidate["title"],
            "xsaas_id": candidate["xsaas_id"],
        }
        duplicate = classify_duplicate(duplicate_probe, exclusion_set)
        if duplicate["duplicate"]:
            result["existing"].append({**candidate, "duplicate": duplicate})
            continue

        identity = _identity_key(
            candidate["name"], candidate["company"], candidate["title"]
        )
        channel_id = (
            f"xsaas:{_normalize_text(candidate['xsaas_id'])}"
            if candidate["xsaas_id"]
            else ""
        )
        keys = {key for key in (identity, channel_id) if key}
        if keys & seen:
            result["batch_duplicates"].append(candidate)
            continue
        seen.update(keys)
        result["accepted"].append(candidate)
    return result


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _next_id(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()
    return int(row[0])


def _insert_dynamic(
    conn: sqlite3.Connection, table: str, values: dict[str, Any]
) -> sqlite3.Cursor:
    allowed = _table_columns(conn, table)
    payload = {key: value for key, value in values.items() if key in allowed}
    if not payload:
        raise ValueError(f"表 {table} 没有可写字段")
    columns = list(payload)
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    return conn.execute(sql, [payload[column] for column in columns])


def _existing_candidate_id(
    conn: sqlite3.Connection, context: dict[str, Any], candidate: dict[str, Any]
) -> int | None:
    rows = conn.execute(
        """
        SELECT id,name,company,title,xsaas_id
        FROM candidates WHERE client=? AND position=?
        """,
        (context["client"], context["job"]),
    ).fetchall()
    target_identity = _identity_key(
        candidate["name"], candidate["company"], candidate["title"]
    )
    target_xsaas = _normalize_text(candidate.get("xsaas_id"))
    for row in rows:
        if target_xsaas and target_xsaas == _normalize_text(row[4]):
            return int(row[0])
        if target_identity == _identity_key(row[1], row[2], row[3]):
            return int(row[0])
    return None


def apply_intake(
    db_path: str | Path,
    context: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Write staged candidates to every A System surface in one transaction."""
    if not apply:
        return {
            "applied": False,
            "planned": len(candidates),
            "inserted": 0,
            "skipped_existing": 0,
        }

    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    inserted = 0
    skipped = 0
    receipts: list[dict[str, Any]] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = now[:10]
    try:
        with conn:
            iteration_row = conn.execute(
                """
                SELECT COALESCE(MAX(iteration),0)+1 FROM candidates
                WHERE client=? AND position=?
                """,
                (context["client"], context["job"]),
            ).fetchone()
            iteration = int(iteration_row[0])
            for candidate in candidates:
                existing_id = _existing_candidate_id(conn, context, candidate)
                if existing_id is not None:
                    skipped += 1
                    receipts.append(
                        {"name": candidate["name"], "status": "existing", "candidate_id": existing_id}
                    )
                    continue

                candidate_id = _next_id(conn, "candidates")
                _insert_dynamic(
                    conn,
                    "candidates",
                    {
                        "id": candidate_id,
                        "name": candidate["name"],
                        "company": candidate["company"],
                        "title": candidate["title"],
                        "education": candidate["education"],
                        "experience": candidate["experience"],
                        "skills": candidate["profile_text"][:1200],
                        "level": "未分层",
                        "city": candidate["city"],
                        "client": context["client"],
                        "position": context["job"],
                        "search_date": today,
                        "status": "new",
                        "notes": f"{candidate['stage']}｜query={candidate['source_query']}",
                        "iteration": iteration,
                        "created_at": now,
                        "updated_at": now,
                        "source": candidate["source"],
                        "xsaas_id": candidate["xsaas_id"],
                    },
                )

                _insert_dynamic(
                    conn,
                    "candidate_clients",
                    {
                        "id": _next_id(conn, "candidate_clients"),
                        "candidate_name": candidate["name"],
                        "candidate_company": candidate["company"],
                        "client": context["client"],
                        "source": candidate["source"],
                        "position_tag": context["job"],
                        "created_at": now,
                    },
                )
                _insert_dynamic(
                    conn,
                    "candidate_profiles",
                    {
                        "id": _next_id(conn, "candidate_profiles"),
                        "candidate_id": candidate_id,
                        "candidate_name": candidate["name"],
                        "candidate_company": candidate["company"],
                        "client": context["client"],
                        "position": context["job"],
                        "education_level": candidate["education"],
                        "seniority": candidate["experience"],
                        "industry_tags_json": "[]",
                        "function_tags_json": json.dumps(
                            context["ability_keywords"], ensure_ascii=False
                        ),
                        "risk_tags_json": "[]",
                        "profile_summary": candidate["profile_text"][:1200],
                        "updated_at": now,
                    },
                )
                _insert_dynamic(
                    conn,
                    "candidate_intelligence",
                    {
                        "id": _next_id(conn, "candidate_intelligence"),
                        "candidate_id": candidate_id,
                        "candidate_name": candidate["name"],
                        "candidate_company": candidate["company"],
                        "client": context["client"],
                        "position": context["job"],
                        "fit_score": 0,
                        "fit_level": "unrated",
                        "evidence_json": "{}",
                        "risk_json": "{}",
                        "next_action": "打开完整简历，按岗位硬门槛人工复核",
                        "last_evaluated_at": now,
                        "model_version": "a-system-multichannel-v2",
                        "created_at": now,
                        "updated_at": now,
                        "strong_matches_json": "[]",
                        "weak_matches_json": "[]",
                        "verification_questions_json": "[]",
                        "recommendation_decision": "pending_review",
                    },
                )

                fingerprint = (
                    f"{candidate['name']}|{candidate['company']}|"
                    f"{_normalize_text(candidate['title'])}"
                )
                person = conn.execute(
                    "SELECT id FROM people WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if person is None:
                    cursor = _insert_dynamic(
                        conn,
                        "people",
                        {
                            "display_name": candidate["name"],
                            "current_company": candidate["company"],
                            "current_title": candidate["title"],
                            "city": candidate["city"],
                            "education": candidate["education"],
                            "experience": candidate["experience"],
                            "fingerprint": fingerprint,
                            "created_at": now,
                        },
                    )
                    person_id = int(cursor.lastrowid)
                else:
                    person_id = int(person[0])

                source_url = candidate["source_url"]
                if not source_url and candidate["channel"] == "xsaas" and candidate["xsaas_id"]:
                    source_url = (
                        "https://headhunt.x-saas.com.cn/#/app/candidate/info/"
                        f"{candidate['xsaas_id']}"
                    )
                if _table_columns(conn, "source_profiles"):
                    existing_profile = conn.execute(
                        """
                        SELECT id FROM source_profiles
                        WHERE person_id=? AND lower(COALESCE(source_type,''))=?
                          AND (
                            COALESCE(source_candidate_id,'')=COALESCE(?,'')
                            OR COALESCE(?,'')=''
                          )
                        ORDER BY id LIMIT 1
                        """,
                        (
                            person_id,
                            candidate["channel"],
                            candidate["source_candidate_id"],
                            candidate["source_candidate_id"],
                        ),
                    ).fetchone()
                    raw_profile = dict(candidate["raw"])
                    raw_profile.update(
                        {
                            "name": candidate["name"],
                            "company": candidate["company"],
                            "title": candidate["title"],
                            "education": candidate["education"],
                            "experience": candidate["experience"],
                            "city": candidate["city"],
                            "profile_text": candidate["profile_text"],
                            "full_text": candidate["full_text"] or candidate["profile_text"],
                            "work_text": candidate["work_text"],
                            "project_text": candidate["project_text"],
                            "education_text": candidate["education_text"],
                            "source_url": source_url,
                            "source_query": candidate["source_query"],
                        }
                    )
                    if existing_profile is None:
                        _insert_dynamic(
                            conn,
                            "source_profiles",
                            {
                                "person_id": person_id,
                                "source_type": candidate["channel"],
                                "source_candidate_id": candidate["source_candidate_id"] or None,
                                "source_date": today,
                                "raw_status": candidate["raw_status"],
                                "raw_client": context["client"],
                                "raw_position": context["job"],
                                "raw_json": json.dumps(raw_profile, ensure_ascii=False),
                            },
                        )
                if source_url and _table_columns(conn, "entity_source_links"):
                    conn.execute(
                        """
                        INSERT INTO entity_source_links
                            (canonical_type,canonical_id,source_system,source_entity_type,
                             source_entity_id,source_url,metadata_json,updated_at)
                        VALUES ('person',?,?,?,?,?,?,?)
                        ON CONFLICT(source_system,source_entity_type,source_entity_id,
                                    canonical_type,canonical_id)
                        DO UPDATE SET source_url=excluded.source_url,
                                      metadata_json=excluded.metadata_json,
                                      updated_at=excluded.updated_at
                        """,
                        (
                            str(person_id),
                            candidate["channel"],
                            "external_profile",
                            source_url,
                            source_url,
                            json.dumps(
                                {
                                    "backfilled_from": "multi_channel_intake",
                                    "source_query": candidate["source_query"],
                                },
                                ensure_ascii=False,
                            ),
                            now,
                        ),
                    )

                existing_relation = conn.execute(
                    """
                    SELECT id FROM job_candidates
                    WHERE job_id=? AND person_id=? AND raw_position=?
                    ORDER BY id LIMIT 1
                    """,
                    (context["job_id"], person_id, context["job"]),
                ).fetchone()
                if existing_relation is None:
                    relation_cursor = _insert_dynamic(
                        conn,
                        "job_candidates",
                        {
                            "job_id": context["job_id"],
                            "person_id": person_id,
                            "raw_client": context["client"],
                            "raw_position": context["job"],
                            "raw_status": candidate["raw_status"],
                            "raw_stage": candidate["stage"],
                            "clean_stage": candidate["stage"],
                            "flow_bucket": candidate["flow_bucket"],
                            "clean_reason": "多渠道寻访新增，待完整简历复核",
                            "recent_hunting": 1,
                            "search_date": today,
                            "updated_at": now,
                            "source_candidate_id": str(candidate_id),
                        },
                    )
                    job_candidate_id = int(relation_cursor.lastrowid)
                else:
                    job_candidate_id = int(existing_relation[0])

                _insert_dynamic(
                    conn,
                    "candidate_events",
                    {
                        "job_candidate_id": job_candidate_id,
                        "person_id": person_id,
                        "job_id": context["job_id"],
                        "event_type": candidate["raw_status"],
                        "event_status": candidate["event_status"],
                        "event_time": now,
                        "summary": f"多渠道寻访新增：{candidate['name']}｜{candidate['company']}｜{candidate['title']}",
                        "raw_json": json.dumps(candidate["raw"], ensure_ascii=False),
                        "source_table": f"{candidate['channel']}_search",
                        "source_id": candidate["source_url"] or candidate["xsaas_id"],
                    },
                )
                inserted += 1
                receipts.append(
                    {
                        "name": candidate["name"],
                        "status": "inserted",
                        "candidate_id": candidate_id,
                        "job_candidate_id": job_candidate_id,
                    }
                )
    finally:
        conn.close()
    return {
        "applied": True,
        "planned": len(candidates),
        "inserted": inserted,
        "skipped_existing": skipped,
        "receipts": receipts,
    }


def load_search_history(
    db_path: str | Path, client: str, job: str
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "search_experiments" not in tables:
            return []
        rows = conn.execute(
            """
            SELECT channel,query,recommended_count,status,run_time
            FROM search_experiments
            WHERE client=? AND position=?
            ORDER BY run_time,id
            """,
            (client, job),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def collect_live_preflight(port: int = 9223) -> dict[str, Any]:
    """Check CDP and channel login availability without mutating either site."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=5
        ) as response:
            tabs = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "ready": False,
            "port": port,
            "browser": "unavailable",
            "error": str(exc),
            "channels": {},
        }

    def channel_state(channel: str) -> dict[str, Any]:
        if channel == "liepin":
            matches = [
                tab
                for tab in tabs
                if tab.get("type") == "page" and "h.liepin.com" in tab.get("url", "")
            ]
            matches.sort(
                key=lambda tab: "search/getConditionItem" not in tab.get("url", "")
            )
        else:
            matches = [
                tab
                for tab in tabs
                if tab.get("type") == "page" and "x-saas" in tab.get("url", "").lower()
            ]
        if not matches:
            return {"ready": False, "status": "tab_missing"}
        tab = matches[0]
        url = str(tab.get("url") or "")
        if "login" in url.lower():
            return {
                "ready": False,
                "status": "login_required",
                "url": url,
                "title": tab.get("title") or "",
            }
        return {
            "ready": True,
            "status": "available",
            "url": url,
            "title": tab.get("title") or "",
        }

    channels = {
        "liepin": channel_state("liepin"),
        "xsaas": channel_state("xsaas"),
    }
    return {
        "ready": all(value["ready"] for value in channels.values()),
        "port": port,
        "browser": "available",
        "channels": channels,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A System job-driven multi-channel search orchestration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_job_args(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--db", default=str(DEFAULT_DB))
        subparser.add_argument("--client", required=True)
        subparser.add_argument("--job", required=True)

    context_parser = subparsers.add_parser("context")
    add_job_args(context_parser)
    plan_parser = subparsers.add_parser("plan")
    add_job_args(plan_parser)
    plan_parser.add_argument("--max-queries", type=int, default=6)
    preflight_parser = subparsers.add_parser("preflight")
    add_job_args(preflight_parser)
    preflight_parser.add_argument("--port", type=int, default=9223)
    intake_parser = subparsers.add_parser("intake")
    add_job_args(intake_parser)
    intake_parser.add_argument("--input", required=True)
    intake_parser.add_argument("--apply", action="store_true")
    return parser


def run_cli(argv: list[str] | None = None) -> dict[str, Any]:
    args = _parser().parse_args(argv)
    context = load_position_context(args.db, args.client, args.job)
    if args.command == "context":
        exclusion = load_exclusion_set(args.db, args.client, args.job)
        return {**context, "exclusion_summary": exclusion["summary"]}
    if args.command == "plan":
        exclusion = load_exclusion_set(args.db, args.client, args.job)
        history = load_search_history(args.db, args.client, args.job)
        return build_search_plan(
            context,
            exclusion,
            history,
            max_queries_per_channel=args.max_queries,
        )
    if args.command == "preflight":
        return {
            "client": args.client,
            "job": args.job,
            "job_id": context["job_id"],
            "preflight": collect_live_preflight(args.port),
        }
    if args.command == "intake":
        payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
        records = payload.get("candidates", []) if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("intake 输入必须是候选人数组或包含 candidates 数组的对象")
        exclusion = load_exclusion_set(args.db, args.client, args.job)
        staged = stage_candidates(records, context, exclusion)
        intake = apply_intake(
            args.db, context, staged["accepted"], apply=bool(args.apply)
        )
        return {
            "client": args.client,
            "job": args.job,
            "staged": {
                "accepted_count": len(staged["accepted"]),
                "existing_count": len(staged["existing"]),
                "batch_duplicate_count": len(staged["batch_duplicates"]),
                "error_count": len(staged["errors"]),
                "accepted": staged["accepted"],
                "existing": staged["existing"],
                "batch_duplicates": staged["batch_duplicates"],
                "errors": staged["errors"],
            },
            "intake": intake,
        }
    raise ValueError(f"不支持的命令：{args.command}")


def main() -> int:
    try:
        result = run_cli()
    except (ValueError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
