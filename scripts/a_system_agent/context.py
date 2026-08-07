from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .privacy import sanitize_payload


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row else {}


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _json_list(value: Any) -> list[str]:
    parsed = _json_value(value, [])
    if not isinstance(parsed, list):
        return []
    result: list[str] = []
    for item in parsed:
        text = " ".join(str(item or "").split()).strip(" -；;，,")
        if not text or re.fullmatch(r"\d+[.、]?", text):
            continue
        if text not in result:
            result.append(text)
    return result


def _requirement_list(value: Any) -> list[str]:
    parsed = _json_value(value, [])
    if not isinstance(parsed, list):
        return _json_list(value)
    groups: list[str] = []
    current: list[str] = []
    for raw_item in parsed:
        text = " ".join(str(raw_item or "").split()).strip(" -；;，,")
        if not text:
            continue
        if re.fullmatch(r"\d+[.、]?", text):
            if current:
                groups.append("；".join(current))
                current = []
            continue
        trailing_marker = bool(re.search(r"\s+\d+[.、]?$", text))
        text = re.sub(r"\s+\d+[.、]?$", "", text).strip()
        if text.startswith(("年龄", "学历", "地点", "薪资")) and current:
            groups.append("；".join(current))
            current = []
        if text:
            current.append(text)
        if trailing_marker and current:
            groups.append("；".join(current))
            current = []
    if current:
        groups.append("；".join(current))
    return list(dict.fromkeys(groups))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None


def _candidate_row(
    conn: sqlite3.Connection,
    source_candidate_id: Any,
    name: str,
    company: str,
    title: str,
    client: str,
    job: str,
) -> dict[str, Any]:
    source_text = str(source_candidate_id or "").strip()
    row = None
    if source_text.isdigit():
        row = conn.execute("SELECT * FROM candidates WHERE id=? LIMIT 1", (int(source_text),)).fetchone()
    if row is None:
        rows = conn.execute(
            """
            SELECT * FROM candidates
            WHERE name=? AND client=? AND position=?
              AND (?='' OR COALESCE(company,'')=?)
              AND (?='' OR COALESCE(title,'')=?)
            ORDER BY id DESC
            LIMIT 2
            """,
            (name, client, job, company, company, title, title),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
    return _row(row)


def _extract_resume_text(
    conn: sqlite3.Connection,
    candidate_id: int | None,
    person_id: int,
    job_candidate_id: int,
) -> dict[str, str]:
    """从 source_profiles、candidate_events 和 candidates 表中召回最完整的简历原文。"""
    texts: list[str] = []

    def add_text(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, dict):
            for key in ("full_text", "profile_text", "candidate_profile_text", "content"):
                add_text(value.get(key))

    for row in conn.execute(
        "SELECT raw_json FROM source_profiles WHERE person_id=? ORDER BY id DESC",
        (int(person_id),),
    ).fetchall():
        add_text(_json_value(row["raw_json"], {}))

    event_source = "v_effective_candidate_events" if _table_exists(conn, "v_effective_candidate_events") else "candidate_events"
    for row in conn.execute(
        f"SELECT raw_json FROM {event_source} WHERE job_candidate_id=? OR person_id=? ORDER BY COALESCE(event_time,'') DESC, id DESC LIMIT 100",
        (int(job_candidate_id), int(person_id)),
    ).fetchall():
        add_text(_json_value(row["raw_json"], {}))

    if candidate_id is not None:
        try:
            row = conn.execute(
                "SELECT legacy_profile_text FROM candidates WHERE id=? LIMIT 1", (int(candidate_id),)
            ).fetchone()
            if row:
                add_text(row["legacy_profile_text"])
        except sqlite3.OperationalError:
            pass

    best = max(texts, key=len) if texts else ""
    return {
        "full_text": best,
        "summary": best[:800] if best else "",
    }


def build_candidate_context(db_path: str | Path, job_candidate_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        relation_row = conn.execute(
            """
            SELECT jc.*, p.display_name, p.current_company, p.current_title,
                   p.city AS person_city, p.education AS person_education,
                   p.experience AS person_experience,
                   c.name AS client, j.title AS job, j.location AS job_location,
                   j.status AS job_status, j.hard_requirements AS job_hard_requirements,
                   j.ability_keywords AS job_ability_keywords,
                   j.target_companies AS job_target_companies,
                   j.exclusions AS job_exclusions, j.summary AS job_summary,
                   j.updated_at AS job_updated_at
            FROM job_candidates jc
            JOIN people p ON p.id=jc.person_id
            LEFT JOIN jobs j ON j.id=jc.job_id
            LEFT JOIN clients c ON c.id=j.client_id
            WHERE jc.id=?
            """,
            (int(job_candidate_id),),
        ).fetchone()
        if relation_row is None:
            raise ValueError(f"找不到人岗关系：{job_candidate_id}")
        relation = _row(relation_row)
        client = str(relation.get("client") or relation.get("raw_client") or "")
        job = str(relation.get("job") or relation.get("raw_position") or "")
        candidate = _candidate_row(
            conn,
            relation.get("source_candidate_id"),
            str(relation.get("display_name") or ""),
            str(relation.get("current_company") or ""),
            str(relation.get("current_title") or ""),
            client,
            job,
        )
        candidate_id = candidate.get("id")
        resume = _extract_resume_text(conn, candidate_id, relation["person_id"], int(job_candidate_id))
        candidate_profile = {}
        if candidate_id is not None and _table_exists(conn, "candidate_profiles"):
            candidate_profile = _row(
                conn.execute(
                    """
                    SELECT * FROM candidate_profiles
                    WHERE candidate_id=? AND client=? AND position=?
                    ORDER BY COALESCE(updated_at,'') DESC, id DESC LIMIT 1
                    """,
                    (candidate_id, client, job),
                ).fetchone()
            )
        position_profile = {}
        if _table_exists(conn, "position_profiles"):
            position_profile = _row(
                conn.execute(
                    """
                    SELECT * FROM position_profiles
                    WHERE client=? AND position=?
                    ORDER BY COALESCE(updated_at,'') DESC, id DESC LIMIT 1
                    """,
                    (client, job),
                ).fetchone()
            )
        source_profiles: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT * FROM source_profiles WHERE person_id=? ORDER BY id DESC",
            (relation["person_id"],),
        ).fetchall():
            item = _row(row)
            item["raw_json"] = _json_value(item.get("raw_json"), {})
            source_profiles.append(item)
        event_source = "v_effective_candidate_events" if _table_exists(conn, "v_effective_candidate_events") else "candidate_events"
        events = [
            _row(row)
            for row in conn.execute(
                f"""
                SELECT id,event_type,event_status,event_time,summary,source_table,source_id
                FROM {event_source}
                WHERE job_candidate_id=?
                ORDER BY COALESCE(event_time,'') DESC,id DESC LIMIT 40
                """,
                (int(job_candidate_id),),
            ).fetchall()
        ]
        evaluation_events = [
            event for event in events if event.get("event_type") != "followup_task"
        ]
        learning_rules: list[dict[str, Any]] = []
        if _table_exists(conn, "agent_learning_rules"):
            for row in conn.execute(
                """
                SELECT id,rule_key,rule_type,rule_json,version,support_count,
                       contradiction_count,approved_at
                FROM agent_learning_rules
                WHERE status='active' AND (client=? OR client IS NULL OR client='')
                  AND (job=? OR job IS NULL OR job='')
                ORDER BY version DESC,id DESC
                """,
                (client, job),
            ).fetchall():
                item = _row(row)
                item["rule"] = _json_value(item.pop("rule_json", "{}"), {})
                learning_rules.append(item)
        context = {
            "relation": {
                "job_candidate_id": relation["id"],
                "job_id": relation.get("job_id"),
                "person_id": relation["person_id"],
                "source_candidate_id": relation.get("source_candidate_id"),
                "raw_status": relation.get("raw_status"),
                "raw_stage": relation.get("raw_stage"),
                "clean_stage": relation.get("clean_stage"),
                "flow_bucket": relation.get("flow_bucket"),
                "updated_at": relation.get("updated_at"),
            },
            "identity": {
                "name": relation.get("display_name"),
                "company": relation.get("current_company"),
                "title": relation.get("current_title"),
                "city": relation.get("person_city"),
                "education": relation.get("person_education"),
                "experience": relation.get("person_experience"),
            },
            "candidate": candidate,
            "candidate_profile": {
                **candidate_profile,
                "industry_tags": _json_list(candidate_profile.get("industry_tags_json")),
                "function_tags": _json_list(candidate_profile.get("function_tags_json")),
                "risk_tags": _json_list(candidate_profile.get("risk_tags_json")),
            },
            "position": {
                "client": client,
                "job": job,
                "location": relation.get("job_location"),
                "status": relation.get("job_status"),
                "summary": position_profile.get("jd_analysis_summary") or relation.get("job_summary"),
                "education_requirement": position_profile.get("education_requirement"),
                "experience_requirement": position_profile.get("experience_requirement"),
                "hard_requirements": _requirement_list(position_profile.get("hard_requirements_json"))
                or _requirement_list(relation.get("job_hard_requirements")),
                "ability_keywords": _json_list(position_profile.get("ability_keywords_json"))
                or _json_list(relation.get("job_ability_keywords")),
                "soft_preferences": _json_list(position_profile.get("soft_preferences_json")),
                "target_companies": _json_list(position_profile.get("target_companies_json"))
                or _json_list(relation.get("job_target_companies")),
                "exclusions": _json_list(position_profile.get("exclusion_tags_json"))
                or _json_list(relation.get("job_exclusions")),
                "risk_points": _json_list(position_profile.get("risk_points_json")),
                "pitch_points": _json_list(position_profile.get("pitch_points_json")),
                "updated_at": position_profile.get("updated_at") or relation.get("job_updated_at"),
            },
            "source_profiles": source_profiles,
            "events": events,
            "learning_rules": learning_rules,
            "resume": resume,
        }
        model_context = sanitize_payload(
            {
                "identity": context["identity"],
                "candidate": candidate,
                "candidate_profile": context["candidate_profile"],
                "position": context["position"],
                "source_profiles": source_profiles,
                "events": evaluation_events,
                "learning_rules": learning_rules,
                "resume": resume,
            }
        )
        context["model_context"] = model_context
        canonical = json.dumps(model_context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        context["snapshot_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return context
    finally:
        conn.close()
