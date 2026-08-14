from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .capability_runtime_base import _loads, _row, _slug
from .context import build_candidate_context


class RunnerAssessmentMixin:
    """共享事实原语：岗位/人选上下文、评估读取、事件与跟进任务。"""

    def _job(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("type") != "job" or not context.get("id"):
            raise ValueError("该能力需要明确岗位上下文")
        conn = self.service._connect()
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
            position_join = ""
            position_fields = ""
            if columns:
                position_join = "LEFT JOIN positions p ON p.client=c.name AND p.title=j.title"
                position_fields = ",p.id AS position_id,p.location AS position_location,p.salary,p.education,p.experience,p.responsibilities,p.requirements,p.headcount,p.deadline,p.liepin_status"
            row = conn.execute(
                f"""
                SELECT j.*,c.name AS client{position_fields}
                FROM jobs j JOIN clients c ON c.id=j.client_id {position_join}
                WHERE j.id=? ORDER BY COALESCE(p.id,0) DESC LIMIT 1
                """ if columns else "SELECT j.*,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (int(context["id"]),),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到岗位：{context['id']}")
            result = _row(row)
            profile = conn.execute(
                "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
                (result.get("client"), result.get("title")),
            ).fetchone() if self._table(conn, "position_profiles") else None
            result["profile"] = _row(profile)
            return result
        finally:
            conn.close()

    @staticmethod
    def _table(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)).fetchone() is not None

    def _candidate(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("type") != "candidate" or not context.get("id"):
            raise ValueError("该能力需要明确人选上下文")
        return build_candidate_context(self.service.db_path, int(context["id"]))

    def _latest_assessment(self, job_candidate_id: int) -> dict[str, Any]:
        conn = self.service._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_candidate_assessments WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
                (int(job_candidate_id),),
            ).fetchone()
            if row is None:
                result = self.service.submit_assessment(int(job_candidate_id), wait=True)
                return result.get("assessment") or {}
            item = _row(row)
            for key, target, default in (
                ("criteria_json", "criteria", {}), ("strengths_json", "strengths", []),
                ("gaps_json", "gaps", []), ("risks_json", "risks", []),
                ("verification_questions_json", "verification_questions", []),
                ("citations_json", "citations", []),
            ):
                item[target] = _loads(item.get(key), default)
            return item
        finally:
            conn.close()

    def _path(self, category: str, title: str, suffix: str) -> Path:
        folder = self.output_dir / category
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{_slug(title)}-{datetime.now():%Y%m%d-%H%M%S}.{suffix.lstrip('.')}"

    @staticmethod
    def _artifact(kind: str, title: str, *, content: str = "", file_path: Path | None = None,
                  mime_type: str = "text/markdown", validation: str = "passed", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": kind, "title": title, "content": content,
            "file_path": str(file_path) if file_path else "", "mime_type": mime_type,
            "validation_status": validation, "metadata": metadata or {},
        }

    @staticmethod
    def _blocked(summary: str, missing: list[str], references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"summary": summary, "blocked": True, "missing_inputs": missing, "references": references or []}

    def _job_reference(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "job", "id": job.get("id"), "label": job.get("title"), "subtitle": job.get("client")}]

    def _candidate_reference(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        identity, position, relation = candidate["identity"], candidate["position"], candidate["relation"]
        return [{"type": "candidate", "id": relation["job_candidate_id"], "label": identity.get("name"), "subtitle": f"{position.get('client','')} / {position.get('job','')}"}]

    def _candidate_event(self, candidate: dict[str, Any], event_type: str, status: str, summary: str, raw: dict[str, Any]) -> int:
        relation = candidate["relation"]
        conn = self.service._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,?,?)
                """,
                (relation["job_candidate_id"], relation["person_id"], relation.get("job_id"), event_type, status,
                 summary[:1000], json.dumps(raw, ensure_ascii=False), "agent_workflow", str(raw.get("workflow_id") or "")),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def _followup(self, candidate: dict[str, Any], task_type: str, reason: str, inputs: dict[str, Any], days: int = 2) -> int | None:
        conn = self.service._connect()
        try:
            if not self._table(conn, "followup_tasks"):
                return None
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(followup_tasks)").fetchall()}
            next_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM followup_tasks").fetchone()[0])
            identity, position, relation = candidate["identity"], candidate["position"], candidate["relation"]
            values = {
                "id": next_id, "candidate_id": relation.get("source_candidate_id"), "candidate_name": identity.get("name"),
                "candidate_company": identity.get("company"), "client": position.get("client"), "position": position.get("job"),
                "task_type": task_type, "priority": int(inputs.get("priority") or 2),
                "due_at": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                "status": "open", "reason": reason[:1000], "source_table": "agent_workflow",
                "source_id": inputs.get("step_id"), "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "job_candidate_id": relation["job_candidate_id"],
            }
            keys = [key for key in values if key in columns]
            conn.execute(
                f"INSERT INTO followup_tasks ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [values[key] for key in keys],
            )
            conn.commit()
            return next_id
        finally:
            conn.close()
