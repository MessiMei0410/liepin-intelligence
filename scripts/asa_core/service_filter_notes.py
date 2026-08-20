"""岗位级筛选口径便签（dogfood R2-3）。

"以后筛选用六自由度作为大加分项"这类跨会话口径偏好此前没有任何持久化通道：
job_list_filters 只有严格/宽松 mode 记忆，关键词口径写死在 candidate_pool_filter.py，
模型只能空口承诺"已记录"。本域提供每岗位一条口径便签（job_filter_notes 表，
migration 15）：

- 便签是给人和模型看的**口径声明**——名单卡（candidate_list_card）出卡时把便签
  带进 answer/card 的口径声明，模型与用户都能看到"生效了什么"；
- 便签**不参与**确定性筛选逻辑（关键词变更仍走代码 PR），避免自由文本改筛选；
  唯一例外是性别限制口径桥：便签含性别排除词（不看女/限男/仅男 等）时，commit
  在同一确认链同事务把 jobs.gender_requirement 置为 male_only（migration 16 的
  结构化开关），确定性分级引擎据此排除铁证女性人选（unknown 一律保留待核验）；
- 写入走既有写确认链：preflight 铸一次性未激活 token → UI 激活 → commit 消费
  （consume_write_confirmation），模型工具面拿不到激活能力，"人确认是机制"。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .database import connect

FILTER_NOTE_MAX_CHARS = 500


def _detect_male_only(note_text: str) -> str:
    """便签文本性别排除词检测（桥）：命中返回 'male_only'，否则 ''。"""
    from a_system_agent.gender_inference import detect_male_only_note

    return detect_male_only_note(note_text)


def _read_gender_requirement(conn: sqlite3.Connection, job_id: int) -> str:
    """防御式读取 jobs.gender_requirement（合成库/旧库无该列时按 '' 处理）。"""
    try:
        cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
        if "gender_requirement" not in cols:
            return ""
        row = conn.execute(
            "SELECT gender_requirement FROM jobs WHERE id=?", (int(job_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    value = str(row["gender_requirement"] or "").strip() if row else ""
    return value if value == "male_only" else ""


class FilterNotesMixin:
    """岗位筛选口径便签：读取 / preflight / commit（写确认链三段）。"""

    def _job_brief(self, conn, job_id: int):
        return conn.execute(
            "SELECT j.id, j.title, c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()

    def get_job_filter_note(self, job_id: int) -> dict[str, Any]:
        """读取岗位口径便签（只读）：无便签时 note 为 null。job 不存在 → LookupError。
        响应始终携带 gender_requirement 结构化开关（'' 不限 / 'male_only'）。"""
        conn = connect(self.db_path)
        try:
            job = self._job_brief(conn, job_id)
            if not job:
                raise LookupError("job not found")
            row = conn.execute(
                "SELECT note, updated_by, updated_at FROM job_filter_notes WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
            return {
                "ok": True,
                "job_id": int(job_id),
                "job": {"id": int(job["id"]), "title": str(job["title"] or ""), "client": str(job["client"] or "")},
                "gender_requirement": _read_gender_requirement(conn, job_id),
                "note": (
                    {
                        "note": str(row["note"]),
                        "updated_by": str(row["updated_by"] or ""),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                    if row
                    else None
                ),
            }
        finally:
            conn.close()

    def filter_note_preflight(self, job_id: int, note: str) -> dict[str, Any]:
        """口径便签写申请（不写库）：校验岗位存在 + 便签非空，铸一次性未激活 token
        （5 分钟有效，需 UI 激活后才可 commit）。回显当前便签供确认卡对照。"""
        text = " ".join(str(note or "").split())
        if not text:
            raise ValueError("口径便签内容不能为空")
        if len(text) > FILTER_NOTE_MAX_CHARS:
            raise ValueError(f"口径便签最长 {FILTER_NOTE_MAX_CHARS} 字")
        current = self.get_job_filter_note(job_id)  # job 不存在 → LookupError（404）
        token, expires = self._mint_write_token(int(job_id), "job_filter_note", activated=False)
        previous = current.get("note")
        male_only = _detect_male_only(text) == "male_only"
        impact = (
            "确认后保存为该岗位的筛选口径便签：之后出名单卡时随口径声明显示；"
            "便签不改变确定性筛选逻辑本身（关键词变更需走代码变更）。"
        )
        if male_only:
            impact = (
                "检测到性别限制口径：确认后将同时把该岗位的性别要求开关置为 male_only，"
                "确定性分级会排除铁证为女性的候选人（性别不明的保留并标注待核验）；"
                + impact
            )
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "job_filter_note",
            "job": current["job"],
            "note": text,
            "previous_note": previous["note"] if previous else "",
            "gender_requirement": current.get("gender_requirement") or "",
            "gender_requirement_detected": male_only,
            "impact": impact,
        }

    def filter_note_commit(
        self,
        job_id: int,
        note: str,
        preflight_token: str,
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """口径便签写入：token 绑定 job_id + 动作（预检哪个岗位就只能写哪个岗位），
        upsert 每岗位一条；同 request_id 重放返回已保存内容（幂等）。"""
        text = " ".join(str(note or "").split())
        if not text:
            raise ValueError("口径便签内容不能为空")
        if len(text) > FILTER_NOTE_MAX_CHARS:
            raise ValueError(f"口径便签最长 {FILTER_NOTE_MAX_CHARS} 字")
        self.consume_write_confirmation(preflight_token, int(job_id), "job_filter_note")
        male_only = _detect_male_only(text) == "male_only"
        conn = connect(self.db_path)
        try:
            if not self._job_brief(conn, job_id):
                raise LookupError("job not found")
            existing = conn.execute(
                "SELECT note, request_id FROM job_filter_notes WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
            replay = bool(
                existing
                and str(existing["request_id"] or "")
                and str(existing["request_id"]) == str(request_id or "")
            )
            if not replay:
                conn.execute(
                    """INSERT INTO job_filter_notes (job_id, note, updated_by, request_id)
                       VALUES (?,?,?,?)
                       ON CONFLICT(job_id) DO UPDATE SET
                           note=excluded.note, updated_by=excluded.updated_by,
                           request_id=excluded.request_id,
                           updated_at=datetime('now','localtime')""",
                    (int(job_id), text, "consultant", str(request_id or "")),
                )
                # 便签→结构化开关的桥：同一确认链同事务落 gender_requirement=male_only。
                # 列不存在（合成库/未迁移旧库）时跳过置位，response 如实回报未生效。
                if male_only:
                    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(jobs)")}
                    if "gender_requirement" in cols:
                        conn.execute(
                            "UPDATE jobs SET gender_requirement='male_only' WHERE id=?",
                            (int(job_id),),
                        )
                conn.commit()
            gender_requirement = _read_gender_requirement(conn, job_id)
            result = {
                "ok": True,
                "job_id": int(job_id),
                "note": text,
                "already_saved": replay,
                "gender_requirement": gender_requirement,
                "gender_requirement_detected": male_only,
            }
            if male_only and gender_requirement == "male_only":
                result["notice"] = (
                    "检测到性别限制口径：已将岗位性别要求置为 male_only，"
                    "确定性分级将排除铁证为女性的候选人（性别不明的保留并标注待核验）。"
                )
            return result
        finally:
            conn.close()
