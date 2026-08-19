"""简历回填写动作域（护栏第 9/12 条机制）。

场景：用户在猎聘打开某候选人详情页，要求把当前页简历更新进 v3 档案。
数据链：扩展在详情页读到全文 → 桥接直推快照（liepin_workbench_server
update_resume_snapshot）→ 本模块 preflight（定位本地候选人 + 完整性守卫 +
新旧 diff）→ 用户界面确认卡激活 token → commit 落库（复用
a_system_agent.resume_persist.persist_captured_resume，与 CDP 捕获同一写入口径）。

红线：
- 外部 ID（猎聘 resume_id）是证据不是主键：匹配不到本地 job_candidates → 409，
  绝不新建记录；
- partial/failed 抓取不得落库；非 complete 快照不得覆盖已有完整档案
  （完整性守卫在 preflight 与 commit 各执行一次，commit 时快照 hash 必须
  与 preflight 绑定的一致，页面漂移即拒绝）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from a_system_agent.liepin_capture import resume_matches_identity
from a_system_agent.resume_persist import persist_captured_resume

from .database import connect, json_value, transaction

# 完整性守卫口径：全文下限 + 姓名 + 工作经历段（全文含「工作经历」亦可）。
RESUME_BACKFILL_MIN_FULL_TEXT_CHARS = 800

RESUME_BACKFILL_SECTIONS = (
    ("full_text", "简历全文"),
    ("work_text", "工作经历"),
    ("project_text", "项目经历"),
    ("education_text", "教育经历"),
)

RESUME_BACKFILL_IDENTITY_FIELDS = (
    ("current_company", "company", "当前公司"),
    ("current_title", "title", "当前职位"),
    ("city", "city", "城市"),
    ("education", "education", "学历"),
    ("experience", "experience", "经验"),
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _snapshot_resume(snapshot: dict[str, Any]) -> dict[str, Any]:
    resume = snapshot.get("resume") if isinstance(snapshot, dict) else None
    return resume if isinstance(resume, dict) else {}


def snapshot_completeness(snapshot: dict[str, Any]) -> dict[str, Any]:
    """完整性守卫（护栏第 12 条）：partial 抓取不得回填。"""
    resume = _snapshot_resume(snapshot)
    full_text = _text(resume.get("full_text"))
    issues: list[str] = []
    if len(full_text) < RESUME_BACKFILL_MIN_FULL_TEXT_CHARS:
        issues.append(f"全文仅 {len(full_text)} 字（低于 {RESUME_BACKFILL_MIN_FULL_TEXT_CHARS} 字下限）")
    if not _clean(resume.get("name")):
        issues.append("缺少姓名")
    if not _text(resume.get("work_text")) and "工作经历" not in full_text:
        issues.append("缺少工作经历段")
    return {
        "complete": not issues,
        "issues": issues,
        "full_text_chars": len(full_text),
    }


def snapshot_content_hash(snapshot: dict[str, Any]) -> str:
    """快照内容指纹：preflight 绑定 token、commit 防漂移、落库幂等共用同一口径。"""
    resume = _snapshot_resume(snapshot)
    payload = {
        key: _text(resume.get(key))
        for key in ("resume_id", "name", "company", "title", "work_text", "project_text", "education_text", "full_text")
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _relation_row(conn: Any, candidate_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT jc.id,jc.job_id,jc.person_id,jc.clean_stage,jc.flow_bucket,jc.raw_status,
               jc.raw_client,jc.raw_position,jc.source_candidate_id,
               p.display_name,p.current_company,p.current_title,p.city,p.education,p.experience,
               j.title AS job,c.name AS client
          FROM job_candidates jc
          JOIN people p ON p.id=jc.person_id
          LEFT JOIN jobs j ON j.id=jc.job_id
          LEFT JOIN clients c ON c.id=j.client_id
         WHERE jc.id=?
        """,
        (int(candidate_id),),
    ).fetchone()
    return dict(row) if row else None


def _liepin_source_ids(conn: Any, person_id: int) -> set[str]:
    """该人选已登记的猎聘外部档案 ID（证据集合）：source_profiles + entity_source_links。"""
    found: set[str] = set()
    for row in conn.execute(
        "SELECT source_candidate_id FROM source_profiles WHERE person_id=? AND lower(COALESCE(source_type,''))='liepin'",
        (int(person_id),),
    ).fetchall():
        value = _clean(row["source_candidate_id"] if not isinstance(row, dict) else row.get("source_candidate_id"))
        if value:
            found.add(value)
    try:
        links = conn.execute(
            """SELECT source_entity_id FROM entity_source_links
                WHERE canonical_type='person' AND canonical_id=? AND source_system='liepin'""",
            (str(int(person_id)),),
        ).fetchall()
    except Exception:
        links = []
    for row in links:
        value = _clean(row["source_entity_id"] if not isinstance(row, dict) else row.get("source_entity_id"))
        if value:
            found.add(value)
    return found


def _persons_for_resume_id(conn: Any, resume_id: str) -> list[int]:
    persons: set[int] = set()
    for row in conn.execute(
        "SELECT DISTINCT person_id FROM source_profiles WHERE lower(COALESCE(source_type,''))='liepin' AND source_candidate_id=?",
        (resume_id,),
    ).fetchall():
        persons.add(int(row["person_id"] if not isinstance(row, dict) else row.get("person_id")))
    try:
        links = conn.execute(
            """SELECT canonical_id FROM entity_source_links
                WHERE canonical_type='person' AND source_system='liepin' AND source_entity_id=?""",
            (resume_id,),
        ).fetchall()
        for row in links:
            value = row["canonical_id"] if not isinstance(row, dict) else row.get("canonical_id")
            if str(value or "").isdigit():
                persons.add(int(value))
    except Exception:
        pass
    return sorted(persons)


def _latest_relation_for_person(conn: Any, person_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id FROM job_candidates WHERE person_id=? ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1",
        (int(person_id),),
    ).fetchone()
    if not row:
        return None
    relation_id = int(row["id"] if not isinstance(row, dict) else row.get("id"))
    return _relation_row(conn, relation_id)


def _current_resume_sections(conn: Any, relation: dict[str, Any]) -> dict[str, str]:
    """现有档案正文（与 service.candidate 读口径一致：source_profiles 最长全文优先，
    事件载荷与 legacy candidates.skills 兜底），供 diff 与「非 complete 不覆盖」判断。"""
    person_id = int(relation["person_id"])
    current: dict[str, Any] = {}
    for row in conn.execute(
        "SELECT raw_json FROM source_profiles WHERE person_id=? ORDER BY id DESC",
        (person_id,),
    ).fetchall():
        raw = json_value(row["raw_json"] if not isinstance(row, dict) else row.get("raw_json"), {})
        if not isinstance(raw, dict):
            continue
        if len(_text(raw.get("full_text") or raw.get("profile_text"))) > len(_text(current.get("full_text"))):
            current = raw
    for row in conn.execute(
        """SELECT raw_json FROM candidate_events
            WHERE job_candidate_id=? OR person_id=? ORDER BY id DESC LIMIT 60""",
        (int(relation["id"]), person_id),
    ).fetchall():
        raw = json_value(row["raw_json"] if not isinstance(row, dict) else row.get("raw_json"), {})
        if not isinstance(raw, dict):
            continue
        text = _text(
            raw.get("full_text") or raw.get("profile_text") or raw.get("candidate_profile_text") or raw.get("content")
        )
        if len(text) > len(_text(current.get("full_text"))):
            current = {**raw, "full_text": text}
    source_candidate_id = _clean(relation.get("source_candidate_id"))
    if source_candidate_id.isdigit():
        try:
            legacy = conn.execute(
                "SELECT skills FROM candidates WHERE CAST(id AS TEXT)=?",
                (source_candidate_id,),
            ).fetchone()
        except Exception:
            legacy = None
        legacy_text = _text((legacy["skills"] if not isinstance(legacy, dict) else legacy.get("skills")) if legacy else "")
        if len(legacy_text) > len(_text(current.get("full_text"))):
            current = {"full_text": legacy_text}
    return {
        "full_text": _text(current.get("full_text") or current.get("profile_text")),
        "work_text": _text(current.get("work_text")),
        "project_text": _text(current.get("project_text")),
        "education_text": _text(current.get("education_text")),
    }


def _excerpt(text: str, limit: int = 120) -> str:
    text = _text(text)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _resume_diff(relation: dict[str, Any], current: dict[str, str], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    resume = _snapshot_resume(snapshot)
    diff: list[dict[str, Any]] = []
    for field, label in RESUME_BACKFILL_SECTIONS:
        before = _text(current.get(field))
        after = _text(resume.get(field))
        change = "unchanged" if before == after else ("added" if not before else "updated")
        diff.append({
            "field": field,
            "label": label,
            "change": change,
            "before_chars": len(before),
            "after_chars": len(after),
            "before_excerpt": _excerpt(before),
            "after_excerpt": _excerpt(after),
        })
    for relation_field, resume_field, label in RESUME_BACKFILL_IDENTITY_FIELDS:
        before = _clean(relation.get(relation_field))
        after = _clean(resume.get(resume_field))
        # people 行只回填空字段：已有值不会被覆盖（与落库口径一致）。
        change = "unchanged" if before == after else ("added" if not before and after else "kept")
        diff.append({
            "field": relation_field,
            "label": label,
            "change": change,
            "before_chars": len(before),
            "after_chars": len(after),
            "before_excerpt": _excerpt(before, 60),
            "after_excerpt": _excerpt(after, 60),
        })
    return diff


class ResumeBackfillMixin:
    """简历回填 preflight/commit：挂到 CoreService MRO（token 原语复用 CandidateActionsMixin）。"""

    def _resume_backfill_relation(self, conn: Any, candidate_id: int, resume_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """定位本地候选人 + 身份一致性校验（preflight/commit 共用）。

        - candidate_id 直给：关系必须存在；人选已登记猎聘档案 ID 时快照 ID 必须在
          证据集合内；未登记时用「姓氏/姓名+公司+职位」互证（resume_matches_identity）。
        - 仅 resume_id：按证据反查本地人选；查不到/多人都 409，绝不新建记录。
        """
        resume = _snapshot_resume(snapshot)
        snap_resume_id = _clean(resume.get("resume_id") or snapshot.get("resume_id"))
        if resume_id and snap_resume_id and _clean(resume_id) != snap_resume_id:
            raise ValueError(
                f"当前页快照（档案 {snap_resume_id}）与指定的猎聘档案（{_clean(resume_id)}）不一致，请切换到对应详情页后重试"
            )
        if not snap_resume_id:
            raise ValueError("当前页快照缺少猎聘档案 ID（resume_id），无法作为回填证据")
        if candidate_id:
            relation = _relation_row(conn, int(candidate_id))
            if not relation:
                raise LookupError("candidate not found")
            known_ids = _liepin_source_ids(conn, int(relation["person_id"]))
            if known_ids and snap_resume_id not in known_ids:
                raise ValueError(
                    f"当前页猎聘档案（{snap_resume_id}）与本地人选已登记的猎聘档案（{'、'.join(sorted(known_ids))}）不一致，禁止跨人回填"
                )
            identity = {
                "name": relation.get("display_name"),
                "company": relation.get("current_company"),
                "title": relation.get("current_title"),
            }
            evidence = {
                "name": resume.get("name"),
                "company": resume.get("company"),
                "title": resume.get("title"),
                "full_text": resume.get("full_text"),
            }
            if not known_ids and not resume_matches_identity(identity, evidence):
                raise ValueError(
                    f"页面简历（{ _clean(resume.get('name')) or '未识别姓名'}）与本地人选（{_clean(relation.get('display_name'))}）"
                    "身份证据不匹配（姓名/公司/职位），禁止回填"
                )
            return relation
        persons = _persons_for_resume_id(conn, snap_resume_id)
        if not persons:
            raise ValueError(
                "该人选不在 ASA 库中：未找到该猎聘档案对应的本地记录。"
                "外部 ID 是证据不是主键，回填不会新建档案；如需入库请先在 ASA 中完成入库流程"
            )
        if len(persons) > 1:
            raise ValueError(
                f"该猎聘档案对应 {len(persons)} 个本地人选，无法唯一定位；请在 ASA 界面打开目标人选后，按关系 ID 重新发起"
            )
        relation = _latest_relation_for_person(conn, persons[0])
        if not relation:
            raise ValueError("该人选不在 ASA 库中：已登记的猎聘档案没有人岗关系，无法回填")
        return relation

    def resume_backfill_preflight(
        self,
        candidate_id: int = 0,
        resume_id: str = "",
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, dict) or not _snapshot_resume(snapshot):
            raise ValueError(
                "未读到当前页简历快照：请先在猎聘打开该人选的详情页（浏览器扩展会自动上报快照），再发起简历回填"
            )
        completeness = snapshot_completeness(snapshot)
        if not completeness["complete"]:
            raise ValueError(
                f"页面简历抓取不完整（{'、'.join(completeness['issues'])}），按护栏 partial 抓取不得回填；"
                "请确认详情页完整加载后刷新页面识别"
            )
        conn = connect(self.db_path)
        try:
            relation = self._resume_backfill_relation(conn, int(candidate_id or 0), resume_id, snapshot)
            current = _current_resume_sections(conn, relation)
        finally:
            conn.close()
        resume = _snapshot_resume(snapshot)
        diff = _resume_diff(relation, current, snapshot)
        candidate = {
            "id": int(relation["id"]),
            "name": _clean(relation.get("display_name")),
            "stage": _clean(relation.get("clean_stage")),
            "client": _clean(relation.get("client") or relation.get("raw_client")),
            "job": _clean(relation.get("job") or relation.get("raw_position")),
        }
        resume_brief = {
            "resume_id": _clean(resume.get("resume_id") or snapshot.get("resume_id")),
            "source_url": _text(snapshot.get("source_url") or snapshot.get("url")),
            "captured_at": _clean(snapshot.get("captured_at")),
            "full_text_chars": completeness["full_text_chars"],
        }
        if all(entry["change"] in {"unchanged", "kept"} for entry in diff):
            return {
                "ok": True,
                "unchanged": True,
                "action": "resume_backfill",
                "candidate": candidate,
                "resume": resume_brief,
                "diff": diff,
                "message": "页面简历与本地档案一致，无需回填。",
            }
        token, expires = self._mint_write_token(
            (int(relation["id"]), snapshot_content_hash(snapshot)), "resume_backfill", activated=False
        )
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "resume_backfill",
            "candidate": candidate,
            "resume": resume_brief,
            "diff": diff,
            "impact": "简历档案将按当前页快照更新（档案库 upsert + 业务时间线留痕），并记入统一审计；已有的公司/职位等字段不会被清空。",
        }

    def resume_backfill_commit(
        self,
        candidate_id: int,
        preflight_token: str,
        snapshot: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        with self._preflight_lock:
            grant = self._preflight_tokens.pop(preflight_token, None)
        if not grant or grant[1] != "resume_backfill" or grant[2] <= datetime.now():
            raise ValueError("preflight token is invalid, expired, or already used")
        target_candidate_id, target_hash = grant[0]
        if int(candidate_id) != int(target_candidate_id):
            raise ValueError("预检令牌与目标人选不符，请重新发起预检")
        if not isinstance(snapshot, dict) or not _snapshot_resume(snapshot):
            raise ValueError("未读到当前页简历快照：页面可能已关闭，请重新打开详情页并发起预检")
        if snapshot_content_hash(snapshot) != target_hash:
            raise ValueError("当前页简历快照已变化，预检 diff 已失效，请重新发起预检")
        completeness = snapshot_completeness(snapshot)
        if not completeness["complete"]:
            raise ValueError("页面简历抓取不完整，按护栏 partial 抓取不得回填")
        resume = _snapshot_resume(snapshot)
        snap_resume_id = _clean(resume.get("resume_id") or snapshot.get("resume_id"))
        with transaction(self.db_path) as conn:
            relation = self._resume_backfill_relation(conn, int(candidate_id), snap_resume_id, snapshot)
            # 落库幂等：同人 + 同猎聘档案 + 同内容已是最新 → already_applied，不重复写事件。
            existing = conn.execute(
                """SELECT raw_json FROM source_profiles
                    WHERE person_id=? AND lower(COALESCE(source_type,''))='liepin' AND source_candidate_id=?
                    ORDER BY id DESC LIMIT 1""",
                (int(relation["person_id"]), snap_resume_id),
            ).fetchone()
            if existing:
                stored = json_value(existing["raw_json"] if not isinstance(existing, dict) else existing.get("raw_json"), {})
                if isinstance(stored, dict) and snapshot_content_hash({"resume": stored}) == target_hash:
                    return {
                        "ok": True,
                        "action": "resume_backfill",
                        "candidate_id": int(candidate_id),
                        "person_id": int(relation["person_id"]),
                        "already_applied": True,
                    }
            position = {
                "client": _clean(relation.get("client") or relation.get("raw_client")),
                "job": _clean(relation.get("job") or relation.get("raw_position")),
            }
            identity = {
                "name": relation.get("display_name"),
                "company": relation.get("current_company"),
                "title": relation.get("current_title"),
            }
            source_candidate_id = _clean(relation.get("source_candidate_id"))
            persisted = persist_captured_resume(
                conn,
                relation=relation,
                position=position,
                identity=identity,
                candidate_id=int(source_candidate_id) if source_candidate_id.isdigit() else None,
                resume={
                    **resume,
                    "resume_id": snap_resume_id,
                    "source_url": _text(snapshot.get("source_url") or snapshot.get("url")),
                },
                job_candidate_id=int(candidate_id),
                capture_method="asa_bridge_extension",
            )
        note_text = " ".join(str(note or "").split())
        return {
            "ok": True,
            "action": "resume_backfill",
            "candidate_id": int(candidate_id),
            "person_id": int(relation["person_id"]),
            "source_profile_id": persisted["source_profile_id"],
            "profile_updated": persisted["profile_updated"],
            "business_event_id": persisted["event_id"],
            "summary": persisted["summary"] + (f"｜{note_text}" if note_text else ""),
            "already_applied": False,
        }
