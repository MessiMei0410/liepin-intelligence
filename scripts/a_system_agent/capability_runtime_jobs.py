from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from typing import Any

from .capability_runtime_base import _list_text, _loads, _row


class RunnerJobsMixin:
    """岗位库能力：JD intake / calibration / library update / legacy 归档。"""

    def run_job_intake(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        content = "\n".join([
            f"# 岗位接入 · {job['client']} / {job['title']}", "",
            f"- 状态：{job.get('status') or '未设置'}", f"- 地点：{job.get('position_location') or job.get('location') or '待补充'}",
            f"- 薪资：{job.get('salary') or '待补充'}", f"- 编制：{job.get('headcount') or '待补充'}",
            "", "## 岗位摘要", str(job.get("summary") or job.get("profile", {}).get("jd_analysis_summary") or "待补充"),
        ])
        return {"summary": "岗位接入信息已从 v3 事实源整理。", "references": self._job_reference(job),
                "artifacts": [self._artifact("job_brief", "岗位接入简报", content=content)]}

    def run_jd_calibration(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        profile = job.get("profile") or {}
        facts = {
            "客户": job.get("client"), "岗位": job.get("title"),
            "地点": job.get("position_location") or job.get("location"), "薪资": job.get("salary"),
            "学历": job.get("education") or profile.get("education_requirement"),
            "经验": job.get("experience") or profile.get("experience_requirement"),
            "硬门槛": _loads(profile.get("hard_requirements_json"), []) or _loads(job.get("hard_requirements"), []),
            "核心能力": _loads(profile.get("ability_keywords_json"), []) or _loads(job.get("ability_keywords"), []),
        }
        missing = [key for key in ("岗位", "地点", "硬门槛", "核心能力") if not facts.get(key)]
        content = "# JD 校准\n\n" + "\n".join(f"- {key}：{json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value or '待补充'}" for key, value in facts.items())
        return {"summary": f"JD 校准完成；{len(missing)} 项待补充。", "missing_inputs": missing,
                "references": self._job_reference(job), "artifacts": [self._artifact("jd_calibration", "JD 校准结果", content=content, validation="needs_input" if missing else "passed")]}

    def run_job_library_update(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        objective = str(inputs.get("objective") or "")
        directions = self._job_update_directions(objective, inputs)
        conn = self.service._connect()
        try:
            client = self._job_update_client(conn, context, objective, inputs)
            if not client:
                return self._blocked("岗位库更新缺少客户名，未写入数据库。", ["client"])
            profiles = self._job_update_profiles(conn, client, objective, directions)
            if not profiles:
                return self._blocked("没有找到可用于更新岗位库的岗位画像，未写入数据库。", ["position_profiles"])
            client_row = conn.execute("SELECT id,name FROM clients WHERE name=?", (client,)).fetchone()
            if client_row is None:
                cursor = conn.execute("INSERT INTO clients(name) VALUES (?)", (client,))
                client_id = int(cursor.lastrowid)
            else:
                client_id = int(client_row["id"])

            changes: list[dict[str, Any]] = []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for profile in profiles:
                change = self._upsert_profile_job(conn, client_id, client, _row(profile), now)
                changes.append(change)
            archived = []
            if self._should_archive_legacy_update(objective, directions):
                archived = self._archive_legacy_split_jobs(conn, client, [item["title"] for item in changes], now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        sync_result: dict[str, Any] = {"skipped": True}
        if not inputs.get("skip_sync"):
            sync_result = self._sync_job_library(client)
        receipt = {
            "client": client,
            "directions": directions,
            "changes": changes,
            "archived_legacy": archived,
            "sync": sync_result,
        }
        content = "# 岗位库更新回执\n\n```json\n" + json.dumps(receipt, ensure_ascii=False, indent=2) + "\n```"
        return {
            "summary": f"岗位库已更新：{client} 新增/更新 {len(changes)} 个岗位方向。",
            "references": [{"type": "job", "id": item["job_id"], "label": item["title"], "subtitle": client} for item in changes],
            "artifacts": [self._artifact("job_library_update_receipt", "岗位库更新回执", content=content, metadata=receipt)],
            "job_library_update": receipt,
        }

    def _job_update_client(self, conn: sqlite3.Connection, context: dict[str, Any], objective: str, inputs: dict[str, Any]) -> str:
        explicit = str(inputs.get("client") or "").strip()
        if explicit:
            return explicit
        if context.get("type") == "job" and context.get("id"):
            row = conn.execute(
                "SELECT c.name FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (int(context["id"]),),
            ).fetchone()
            if row:
                return str(row["name"] or "")
        rows = conn.execute("SELECT name FROM clients ORDER BY LENGTH(name) DESC").fetchall()
        for row in rows:
            name = str(row["name"] or "")
            if name and name in objective:
                return name
        return ""

    @staticmethod
    def _job_update_directions(objective: str, inputs: dict[str, Any]) -> list[str]:
        raw = inputs.get("directions")
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            if values:
                return values
        mapping = (("PC", ("PC", "pc", "电脑")), ("服务器", ("服务器", "server", "Server")), ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")))
        directions = [label for label, tokens in mapping if any(token in objective for token in tokens)]
        return directions

    def _job_update_profiles(self, conn: sqlite3.Connection, client: str, objective: str, directions: list[str]) -> list[sqlite3.Row]:
        if not self._table(conn, "position_profiles"):
            return []
        clauses = ["client=?"]
        params: list[Any] = [client]
        if "技术市场" in objective:
            clauses.append("position LIKE '%技术市场%'")
        if directions:
            direction_clauses = []
            for direction in directions:
                if direction == "PC":
                    direction_clauses.append("(position LIKE '%PC%' OR position LIKE '%电脑%')")
                elif direction == "服务器":
                    direction_clauses.append("position LIKE '%服务器%'")
                elif direction == "ADAS":
                    direction_clauses.append("position LIKE '%ADAS%'")
            if direction_clauses:
                clauses.append("(" + " OR ".join(direction_clauses) + ")")
        rows = conn.execute(
            f"SELECT * FROM position_profiles WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        ).fetchall()
        if directions:
            rows = [row for row in rows if "服务器或PC市场" not in str(row["position"] or "")]
        return rows

    def _upsert_profile_job(self, conn: sqlite3.Connection, client_id: int, client: str, profile: dict[str, Any], now: str) -> dict[str, Any]:
        title = str(profile.get("position") or "").strip()
        if not title:
            raise ValueError("岗位画像缺少 position")
        position = conn.execute(
            "SELECT * FROM positions WHERE client=? AND title=? ORDER BY id DESC LIMIT 1",
            (client, title),
        ).fetchone() if self._table(conn, "positions") else None
        position_data = _row(position)
        hard = _list_text(profile.get("hard_requirements_json")) or str(position_data.get("hard_requirements") or "")
        ability = _list_text(profile.get("ability_keywords_json")) or str(position_data.get("ability_keywords") or "")
        targets = _list_text(profile.get("target_companies_json")) or str(position_data.get("target_companies") or "")
        exclusions = _list_text(profile.get("exclusion_tags_json")) or str(position_data.get("exclusions") or "")
        search_words = _list_text(profile.get("search_keywords_json")) or str(position_data.get("search_words") or title)
        summary = str(profile.get("jd_analysis_summary") or position_data.get("summary") or f"{client}/{title} 岗位画像已按方向拆分。")
        status = str(position_data.get("status") or "待启动")
        existing = conn.execute("SELECT * FROM jobs WHERE client_id=? AND title=?", (client_id, title)).fetchone()
        if existing:
            job_id = int(existing["id"])
            lifecycle = str(existing["lifecycle_stage"] or "sourcing")
            if lifecycle in {"archived", "closed", "cancelled"}:
                lifecycle = "sourcing"
            conn.execute(
                """
                UPDATE jobs
                   SET status=?,lifecycle_stage=?,hard_requirements=?,ability_keywords=?,
                       target_companies=?,exclusions=?,search_words=?,summary=?,
                       source_layer='v3_position_profile',updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (status, lifecycle, hard, ability, targets, exclusions, search_words, summary, job_id),
            )
            action = "updated"
        else:
            cursor = conn.execute(
                """
                INSERT INTO jobs
                (client_id,title,status,lifecycle_stage,source_layer,hard_requirements,ability_keywords,
                 target_companies,exclusions,search_words,summary,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))
                """,
                (client_id, title, status, "sourcing", "v3_position_profile", hard, ability, targets, exclusions, search_words, summary),
            )
            job_id = int(cursor.lastrowid)
            action = "created"
        if position_data:
            conn.execute(
                """
                UPDATE positions
                   SET hard_requirements=?,ability_keywords=?,target_companies=?,exclusions=?,
                       search_words=?,summary=?,updated_at=datetime('now','localtime')
                 WHERE client=? AND title=?
                """,
                (hard, ability, targets, exclusions, search_words, summary, client, title),
            )
        self._upsert_job_metrics(conn, job_id, profile, now)
        return {"action": action, "job_id": job_id, "title": title, "position_id": position_data.get("id")}

    def _upsert_job_metrics(self, conn: sqlite3.Connection, job_id: int, profile: dict[str, Any], now: str) -> None:
        payload = {
            "metric_date": now[:10],
            "priority": "P0-最急｜用户指定最高优先级",
            "risk": _list_text(profile.get("risk_points_json")),
            "next_keywords_json": profile.get("search_keywords_json") or "[]",
            "target_companies_json": profile.get("target_companies_json") or "[]",
            "exclude_terms_json": profile.get("exclusion_tags_json") or "[]",
        }
        row = conn.execute("SELECT id FROM job_pipeline_metrics WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if row:
            conn.execute(
                """
                UPDATE job_pipeline_metrics
                   SET metric_date=?,priority=?,risk=?,next_keywords_json=?,
                       target_companies_json=?,exclude_terms_json=?,data_gap=0
                 WHERE id=?
                """,
                (payload["metric_date"], payload["priority"], payload["risk"], payload["next_keywords_json"],
                 payload["target_companies_json"], payload["exclude_terms_json"], int(row["id"])),
            )
        else:
            conn.execute(
                """
                INSERT INTO job_pipeline_metrics
                (job_id,metric_date,a_count,b_count,p0_count,p1_count,published_count,under_review_count,
                 contacted_count,pending_followup_count,priority,risk,next_keywords_json,target_companies_json,
                 exclude_terms_json,data_gap)
                VALUES (?,?,0,0,0,0,0,0,0,0,?,?,?,?,?,0)
                """,
                (job_id, payload["metric_date"], payload["priority"], payload["risk"], payload["next_keywords_json"],
                 payload["target_companies_json"], payload["exclude_terms_json"]),
            )

    @staticmethod
    def _should_archive_legacy_update(objective: str, directions: list[str]) -> bool:
        return bool(directions) and any(token in objective for token in ("拆", "分成", "三个", "分别", "独立"))

    def _archive_legacy_split_jobs(self, conn: sqlite3.Connection, client: str, active_titles: list[str], now: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT j.id,j.title
            FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE c.name=? AND j.title LIKE '%技术市场%' AND j.title LIKE '%服务器或PC市场%'
            """,
            (client,),
        ).fetchall()
        archived = []
        for row in rows:
            if row["title"] in active_titles:
                continue
            conn.execute(
                """
                UPDATE jobs
                   SET status='已拆分为PC/服务器/ADAS-保留历史',
                       lifecycle_stage='archived',
                       closed_reason='岗位画像已拆分为独立方向，历史人选关系保留',
                       closed_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (int(row["id"]),),
            )
            archived.append({"job_id": int(row["id"]), "title": row["title"]})
        if self._table(conn, "positions"):
            conn.execute(
                """
                UPDATE positions
                   SET status='已拆分为PC/服务器/ADAS-保留历史',
                       updated_at=datetime('now','localtime')
                 WHERE client=? AND title LIKE '%技术市场%' AND title LIKE '%服务器或PC市场%'
                """,
                (client,),
            )
        return archived

    def _sync_job_library(self, client: str) -> dict[str, Any]:
        command = [
            self.python,
            "/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py",
            "--client",
            client,
            "--db",
            str(self.service.db_path),
            "--no-open",
            "--no-backup",
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:],
        }
