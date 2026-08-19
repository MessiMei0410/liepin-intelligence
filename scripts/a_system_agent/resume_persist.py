"""简历档案落库（共用写入口径）。

从 assessment_handler.capture_liepin_resume 抽取的落库段，供两条捕获链路复用：
- CDP 只读抓取（capture_liepin_resume，浏览器已打开的猎聘详情页）；
- 扩展桥接快照回填（asa_core 简历回填 commit，扩展在详情页读到全文后经桥接上报）。

写入口径（同一事务由调用方保证）：
- source_profiles 按 (person_id, 'liepin', resume_id) upsert，raw_json 保留全文与分段；
- people 只回填空字段（公司/职位/城市/学历/经验），绝不覆盖已有值；
- candidate_profiles.profile_summary 按 (candidate_id, client, position) upsert；
- candidate_events 记 resume_profile_captured（source_table='source_profiles'，
  source_id=source_profile_id），重复捕获更新原事件而不是重复插行。

红线（护栏第 12 条）由调用方在写入前执行：partial/failed 抓取不得落库；
非 complete 快照不得覆盖已有完整档案。本函数不做完整性判断。
"""

from __future__ import annotations

from typing import Any

from ._shared import _dumps, _table_columns, _table_exists


def resume_profile_summary(resume: dict[str, Any]) -> str:
    """candidate_profiles.profile_summary 口径：全文 + 全文中缺失的分段补齐。"""
    full_text = str(resume.get("full_text") or "").strip()
    profile_parts = [full_text] if full_text else []
    for heading, field in (
        ("工作经历", "work_text"),
        ("项目经历", "project_text"),
        ("教育经历", "education_text"),
    ):
        section_text = str(resume.get(field) or "").strip()
        if section_text and heading not in full_text:
            profile_parts.extend([heading, section_text])
    return "\n".join(profile_parts).strip()[:60000]


def persist_captured_resume(
    conn: Any,
    *,
    relation: dict[str, Any],
    position: dict[str, Any],
    identity: dict[str, Any],
    candidate_id: int | None,
    resume: dict[str, Any],
    job_candidate_id: int,
    capture_method: str,
) -> dict[str, Any]:
    """把一份完整简历快照写入 v3 档案。调用方持有事务（commit/rollback）。

    relation 需含 person_id、job_id；position 含 client/job；
    identity 为姓名/公司/职位兜底（candidate_profiles 行名）。
    """
    profile_payload = {
        **resume,
        "profile_text": "\n".join(
            str(resume.get(key) or "")
            for key in ("work_text", "project_text", "education_text", "full_text")
            if resume.get(key)
        )[:60000],
        "capture_method": capture_method,
        "job_candidate_id": int(job_candidate_id),
    }
    profile_summary = resume_profile_summary(resume)
    existing = conn.execute(
        """
        SELECT id FROM source_profiles
        WHERE person_id=? AND source_type='liepin' AND source_candidate_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(relation["person_id"]), str(resume["resume_id"])),
    ).fetchone()
    if existing:
        source_profile_id = int(existing["id"])
        conn.execute(
            """
            UPDATE source_profiles SET source_date=date('now','localtime'),raw_status=?,
                raw_client=?,raw_position=?,raw_json=? WHERE id=?
            """,
            (
                str(resume.get("status") or ""), position.get("client"), position.get("job"),
                _dumps(profile_payload), source_profile_id,
            ),
        )
        updated = True
    else:
        cursor = conn.execute(
            """
            INSERT INTO source_profiles
            (person_id,source_type,source_candidate_id,source_date,raw_status,raw_client,raw_position,raw_json)
            VALUES (?,'liepin',?,date('now','localtime'),?,?,?,?)
            """,
            (
                int(relation["person_id"]), str(resume["resume_id"]), str(resume.get("status") or ""),
                position.get("client"), position.get("job"), _dumps(profile_payload),
            ),
        )
        source_profile_id = int(cursor.lastrowid)
        updated = False
    conn.execute(
        """
        UPDATE people SET
            current_company=CASE WHEN COALESCE(current_company,'')='' THEN ? ELSE current_company END,
            current_title=CASE WHEN COALESCE(current_title,'')='' THEN ? ELSE current_title END,
            city=CASE WHEN COALESCE(city,'')='' THEN ? ELSE city END,
            education=CASE WHEN COALESCE(education,'')='' THEN ? ELSE education END,
            experience=CASE WHEN COALESCE(experience,'')='' THEN ? ELSE experience END
        WHERE id=?
        """,
        (
            resume.get("company"), resume.get("title"), resume.get("city"),
            resume.get("education"), resume.get("experience"), int(relation["person_id"]),
        ),
    )
    if candidate_id and _table_exists(conn, "candidate_profiles"):
        columns = _table_columns(conn, "candidate_profiles")
        if "profile_summary" in columns:
            existing_profile = conn.execute(
                """
                SELECT id FROM candidate_profiles
                WHERE candidate_id=? AND client=? AND position=?
                ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1
                """,
                (int(candidate_id), position.get("client"), position.get("job")),
            ).fetchone()
            if existing_profile:
                conn.execute(
                    """
                    UPDATE candidate_profiles SET profile_summary=?,
                        education_level=COALESCE(NULLIF(?,''),education_level),
                        seniority=COALESCE(NULLIF(?,''),seniority),
                        updated_at=datetime('now','localtime')
                    WHERE id=?
                    """,
                    (
                        profile_summary, str(resume.get("education") or ""),
                        str(resume.get("experience") or ""), int(existing_profile["id"]),
                    ),
                )
            else:
                next_profile_id = int(
                    conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM candidate_profiles").fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO candidate_profiles
                    (id,candidate_id,candidate_name,candidate_company,client,position,
                     education_level,seniority,industry_tags_json,function_tags_json,
                     risk_tags_json,profile_summary,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                    """,
                    (
                        next_profile_id, int(candidate_id),
                        resume.get("name") or identity.get("name"),
                        resume.get("company") or identity.get("company"),
                        position.get("client"), position.get("job"),
                        resume.get("education"), resume.get("experience"),
                        "[]", "[]", "[]", profile_summary,
                    ),
                )
    summary = (
        f"ASA 从已打开的猎聘详情页补全简历：{resume.get('name') or identity.get('name')}；"
        f"工作经历 {len(str(resume.get('work_text') or ''))} 字，"
        f"项目经历 {len(str(resume.get('project_text') or ''))} 字，"
        f"教育经历 {len(str(resume.get('education_text') or ''))} 字。"
    )
    event = conn.execute(
        """
        SELECT id FROM candidate_events
        WHERE job_candidate_id=? AND event_type='resume_profile_captured'
          AND source_table='source_profiles' AND source_id=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(job_candidate_id), str(source_profile_id)),
    ).fetchone()
    event_payload = {
        "source_profile_id": source_profile_id,
        "resume_id": resume["resume_id"],
        "source_url": resume.get("source_url"),
        "capture_method": capture_method,
    }
    if event:
        event_id = int(event["id"])
        conn.execute(
            """
            UPDATE candidate_events SET event_status='completed',event_time=datetime('now','localtime'),
                summary=?,raw_json=? WHERE id=?
            """,
            (summary, _dumps(event_payload), event_id),
        )
    else:
        event_cursor = conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (?,?,?,'resume_profile_captured','completed',datetime('now','localtime'),?,?,'source_profiles',?)
            """,
            (
                int(job_candidate_id), int(relation["person_id"]), relation.get("job_id"),
                summary, _dumps(event_payload), str(source_profile_id),
            ),
        )
        event_id = int(event_cursor.lastrowid)
    return {
        "source_profile_id": source_profile_id,
        "profile_updated": updated,
        "event_id": event_id,
        "summary": summary,
    }
