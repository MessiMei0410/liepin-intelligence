"""岗位级筛选口径便签（dogfood R2-3）。

"以后筛选用六自由度作为大加分项"这类跨会话口径偏好此前没有任何持久化通道：
job_list_filters 只有严格/宽松 mode 记忆，关键词口径写死在 candidate_pool_filter.py，
模型只能空口承诺"已记录"。本域提供每岗位一条口径便签（job_filter_notes 表，
migration 15）：

- 便签是给人和模型看的**口径声明**——名单卡（candidate_list_card）出卡时把便签
  带进 answer/card 的口径声明，模型与用户都能看到"生效了什么"；
- 便签**不参与**确定性筛选逻辑（关键词变更仍走代码 PR），避免自由文本改筛选；
- 写入走既有写确认链：preflight 铸一次性未激活 token → UI 激活 → commit 消费
  （consume_write_confirmation），模型工具面拿不到激活能力，"人确认是机制"。
"""

from __future__ import annotations

from typing import Any

from .database import connect

FILTER_NOTE_MAX_CHARS = 500


class FilterNotesMixin:
    """岗位筛选口径便签：读取 / preflight / commit（写确认链三段）。"""

    def _job_brief(self, conn, job_id: int):
        return conn.execute(
            "SELECT j.id, j.title, c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()

    def get_job_filter_note(self, job_id: int) -> dict[str, Any]:
        """读取岗位口径便签（只读）：无便签时 note 为 null。job 不存在 → LookupError。"""
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
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "job_filter_note",
            "job": current["job"],
            "note": text,
            "previous_note": previous["note"] if previous else "",
            "impact": "确认后保存为该岗位的筛选口径便签：之后出名单卡时随口径声明显示；"
                      "便签不改变确定性筛选逻辑本身（关键词变更需走代码变更）。",
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
                conn.commit()
            return {
                "ok": True,
                "job_id": int(job_id),
                "note": text,
                "already_saved": replay,
            }
        finally:
            conn.close()
