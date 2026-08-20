"""岗位建档（用户实证卡点：「士兰微新增市场总监岗位」此前无处可建）。

此前岗位只能由寻访 intake / 岗位库同步等管线写入：顾问或 Agent 想手工建档
没有任何入口（界面无新增按钮，Agent 工具面只能回答"去界面手动建"）。本域
把岗位建档做成走写确认链的一等能力：

- preflight（不写库）：客户名解析（clients 精确匹配 → 去公司后缀别名/互相
  包含的模糊匹配；都不中则标记"将新建客户"并在确认文案明示）+ 重复检测
  （同客户同名岗位已存在 → 409 冲突说明，含既有岗位 ID）+ 铸一次性未激活
  token（5 分钟有效，需 UI 激活后才可 commit）；
- commit：consume_write_confirmation（token 绑定客户名+岗位名，预检什么就
  只能建什么）→ 必要时建 client → 建 job。初始字段按既有手工建档行口径：
  status='待启动' / lifecycle_stage='intake' / source_layer='workbench'；
- 建档只是登记岗位：绝不自动启动任何寻访/抓取/工作流（红线）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .database import connect

# 客户名模糊匹配时剥掉的公司后缀（与 a_system_agent.copilot_evidence._client_aliases 同口径）。
_CLIENT_SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司", "科技", "电子", "集团", "股份")

JOB_CREATE_MAX_JD_CHARS = 8000


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").split())


def _client_core(name: str) -> str:
    """客户名核心：循环剥公司后缀（「杭州士兰微电子有限公司」→「杭州士兰微」）。"""
    core = name
    changed = True
    while changed:
        changed = False
        for suffix in _CLIENT_SUFFIXES:
            if core.endswith(suffix) and len(core) - len(suffix) >= 2:
                core = core[: -len(suffix)]
                changed = True
    return core


class JobCreateMixin:
    """岗位建档：preflight / commit（写确认链三段）。"""

    @staticmethod
    def _job_create_target(client_name: str, title: str) -> str:
        return f"{client_name}::{title}"

    def _resolve_client(self, conn, client_name: str) -> dict[str, Any]:
        """客户名解析：精确 → 模糊（别名核心相等或互相包含）。多义 → ValueError（409）。

        返回 {id, name, match}；无匹配时 id=None、match='new'（name 为输入名）。"""
        row = conn.execute(
            "SELECT id,name FROM clients WHERE trim(name)=?", (client_name,)
        ).fetchone()
        if row:
            return {"id": int(row["id"]), "name": str(row["name"]), "match": "exact"}
        input_core = _client_core(client_name)
        candidates: list[dict[str, Any]] = []
        for row in conn.execute("SELECT id,name FROM clients").fetchall():
            existing = _normalize(row["name"])
            if not existing:
                continue
            existing_core = _client_core(existing)
            if (
                existing_core == input_core
                or (len(input_core) >= 2 and input_core in existing)
                or (len(existing_core) >= 2 and existing_core in client_name)
            ):
                candidates.append({"id": int(row["id"]), "name": existing})
        if len(candidates) > 1:
            names = "、".join(item["name"] for item in candidates[:5])
            raise ValueError(
                f"客户名「{client_name}」匹配到多个既有客户（{names}），请改用客户全称重新发起"
            )
        if candidates:
            return {"id": candidates[0]["id"], "name": candidates[0]["name"], "match": "fuzzy"}
        return {"id": None, "name": client_name, "match": "new"}

    @staticmethod
    def _duplicate_job(conn, client_id: int, title: str):
        if client_id is None:
            return None
        return conn.execute(
            """SELECT id,title,status,lifecycle_stage FROM jobs
               WHERE client_id=? AND trim(title)=?""",
            (int(client_id), title),
        ).fetchone()

    def job_create_preflight(
        self,
        client_name: str,
        title: str,
        *,
        direction: str = "",
        base: str = "",
        jd_text: str = "",
        priority: str = "",
    ) -> dict[str, Any]:
        """岗位建档申请（不写库）：客户名解析 + 重复检测 + 铸一次性未激活 token。

        409（ValueError）：客户名/岗位名为空、客户名多义、同客户同名岗位已存在。"""
        client_name = _normalize(client_name)
        title = _normalize(title)
        direction = _normalize(direction)
        base = _normalize(base)
        priority = _normalize(priority)
        jd_text = str(jd_text or "").strip()
        if not client_name:
            raise ValueError("客户名不能为空")
        if not title:
            raise ValueError("岗位名称不能为空")
        if len(jd_text) > JOB_CREATE_MAX_JD_CHARS:
            raise ValueError(f"JD 文本最长 {JOB_CREATE_MAX_JD_CHARS} 字")
        conn = connect(self.db_path)
        try:
            client = self._resolve_client(conn, client_name)
            duplicate = self._duplicate_job(conn, client["id"], title)
        finally:
            conn.close()
        if duplicate:
            status = str(duplicate["status"] or duplicate["lifecycle_stage"] or "").strip() or "状态未知"
            raise ValueError(
                f"客户「{client['name']}」下已存在同名岗位（#{int(duplicate['id'])}「{duplicate['title']}」，{status}）；"
                f"请直接基于岗位 #{int(duplicate['id'])} 操作，或换一个岗位名称"
            )
        warnings: list[str] = []
        if client["match"] == "fuzzy":
            warnings.append(f"客户名「{client_name}」按既有客户「{client['name']}」匹配建档")
        if not jd_text:
            warnings.append("未提供 JD 文本：建档后岗位职责/要求为空，可后续补充")
        token, expires = self._mint_write_token(
            self._job_create_target(client_name, title), "job_create", activated=False
        )
        client_line = (
            f"新建客户「{client['name']}」并" if client["match"] == "new" else f"在既有客户「{client['name']}」下"
        )
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "job_create",
            "job": {
                "client": client["name"],
                "client_id": client["id"],
                "client_is_new": client["match"] == "new",
                "client_match": client["match"],
                "title": title,
                "direction": direction,
                "base": base,
                "priority": priority,
                "jd_text": jd_text,
            },
            "warnings": warnings,
            "impact": f"确认后将{client_line}建档岗位「{title}」（初始状态：待启动）；"
                      "建档只登记岗位，不会自动启动任何寻访/抓取。",
        }

    def job_create_commit(
        self,
        client_name: str,
        title: str,
        preflight_token: str,
        *,
        direction: str = "",
        base: str = "",
        jd_text: str = "",
        priority: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """岗位建档写入：token 绑定客户名+岗位名（预检什么就只能建什么）。

        幂等：commit 时同客户同名岗位已存在（首次 commit 已落库的重放/竞态）→
        返回 already_created=True 与既有岗位 ID，不重复建行。"""
        client_name = _normalize(client_name)
        title = _normalize(title)
        direction = _normalize(direction)
        base = _normalize(base)
        priority = _normalize(priority)
        jd_text = str(jd_text or "").strip()
        if not client_name:
            raise ValueError("客户名不能为空")
        if not title:
            raise ValueError("岗位名称不能为空")
        if len(jd_text) > JOB_CREATE_MAX_JD_CHARS:
            raise ValueError(f"JD 文本最长 {JOB_CREATE_MAX_JD_CHARS} 字")
        self.consume_write_confirmation(
            preflight_token, self._job_create_target(client_name, title), "job_create"
        )
        conn = connect(self.db_path)
        try:
            client = self._resolve_client(conn, client_name)
            duplicate = self._duplicate_job(conn, client["id"], title)
            if duplicate:
                return {
                    "ok": True,
                    "already_created": True,
                    "job_id": int(duplicate["id"]),
                    "client_id": client["id"],
                    "client_name": client["name"],
                    "client_created": False,
                    "title": title,
                }
            client_created = client["match"] == "new"
            if client_created:
                cursor = conn.execute("INSERT INTO clients(name) VALUES (?)", (client["name"],))
                client_id = int(cursor.lastrowid)
            else:
                client_id = int(client["id"])
            summary_parts = []
            if direction:
                summary_parts.append(f"方向：{direction}")
            summary_parts.append(jd_text if jd_text else "JD 待补充（建档时未提供，可后续补充）")
            cursor = conn.execute(
                """
                INSERT INTO jobs
                (client_id,title,location,status,lifecycle_stage,source_layer,summary,created_at,updated_at)
                VALUES (?,?,?,'待启动','intake','workbench',?,datetime('now','localtime'),datetime('now','localtime'))
                """,
                (client_id, title, base, "\n".join(summary_parts)),
            )
            job_id = int(cursor.lastrowid)
            if priority:
                conn.execute(
                    """
                    INSERT INTO job_pipeline_metrics
                    (job_id,metric_date,a_count,b_count,p0_count,p1_count,published_count,under_review_count,
                     contacted_count,pending_followup_count,priority,risk,next_keywords_json,target_companies_json,
                     exclude_terms_json,data_gap)
                    VALUES (?,?,0,0,0,0,0,0,0,0,?,'','[]','[]','[]',0)
                    """,
                    (job_id, datetime.now().strftime("%Y-%m-%d"), priority),
                )
            conn.commit()
            return {
                "ok": True,
                "already_created": False,
                "job_id": job_id,
                "client_id": client_id,
                "client_name": client["name"],
                "client_created": client_created,
                "title": title,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
