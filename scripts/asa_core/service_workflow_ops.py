from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .database import connect, json_value
from .service_candidate_actions import _row


def _funnel_detail(complete: int, partial: int, failed: int) -> dict[str, Any]:
    total = complete + partial + failed
    return {
        "complete": complete,
        "partial": partial,
        "failed": failed,
        "complete_rate": round(complete / total, 4) if total else None,
    }


class WorkflowOpsMixin:
    """工作流操作域：工作流/产物读取与压缩载荷、策略评审编辑、
    Mapping 任务、候选人评估、岗位画像与寻访漏斗。

    方法体自 service.py 逐字节迁移（P2-1），语义不变。
    """

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self._compact_workflow_payload(self.agent_service.get_workflow(workflow_id))
        raise RuntimeError("workflow service unavailable")

    def _compact_workflow_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a first-paint workflow payload; full step output remains on /steps/{id}."""
        def size(value: Any) -> int:
            try:
                return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
            except (TypeError, ValueError):
                return 0

        def query_plan_index(value: Any) -> dict[str, Any]:
            plan = value if isinstance(value, dict) else {}
            cells = plan.get("cells") if isinstance(plan.get("cells"), list) else []
            return {
                "schema_version": plan.get("schema_version"),
                "plan_hash": plan.get("plan_hash"),
                "cell_count": int(plan.get("cell_count") or len(cells)),
                "dimensions": plan.get("dimensions") if isinstance(plan.get("dimensions"), dict) else {},
                "execution_semantics": plan.get("execution_semantics") if isinstance(plan.get("execution_semantics"), dict) else {},
            }

        def preflight_index(value: Any) -> dict[str, Any]:
            preflight = value if isinstance(value, dict) else {}
            compact = {
                key: item
                for key, item in preflight.items()
                if not isinstance(item, (dict, list))
            }
            if isinstance(preflight.get("query_plan_v1"), dict):
                compact["query_plan_v1"] = query_plan_index(preflight["query_plan_v1"])
            snapshot = preflight.get("strategy_snapshot")
            if isinstance(snapshot, dict):
                compact_snapshot = {
                    key: item
                    for key, item in snapshot.items()
                    if not isinstance(item, (dict, list))
                }
                if isinstance(snapshot.get("channels"), dict) and size(snapshot["channels"]) <= 48_000:
                    compact_snapshot["channels"] = snapshot["channels"]
                if isinstance(snapshot.get("query_plan_v1"), dict):
                    compact_snapshot["query_plan_v1"] = query_plan_index(snapshot["query_plan_v1"])
                compact["strategy_snapshot"] = compact_snapshot
            compact["_summary_only"] = True
            return compact

        for step in payload.get("steps") or []:
            output = step.get("output") if isinstance(step, dict) else None
            if isinstance(output, dict) and size(output) > 160_000:
                step["output"] = self.agent_service.workflow_engine._compact_step_output(output)
        for approval in payload.get("approvals") or []:
            preflight = approval.get("preflight") if isinstance(approval, dict) else None
            if isinstance(preflight, dict) and size(preflight) > 160_000:
                approval["preflight"] = preflight_index(preflight)
        for artifact in payload.get("artifacts") or []:
            metadata = artifact.get("metadata") if isinstance(artifact, dict) else None
            if isinstance(metadata, dict) and size(metadata) > 64_000:
                artifact["metadata"] = {
                    "schema_version": metadata.get("schema_version"),
                    "summary_only": True,
                    "field_count": len(metadata),
                }
        for event in payload.get("events") or []:
            if isinstance(event, dict):
                event.pop("detail_json", None)
                event.pop("detail", None)
        return payload

    def workflow_artifact(self, artifact_id: str, *, preview_limit: int = 200_000) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("workflow service unavailable")
        try:
            payload = self.agent_service.get_workflow_artifact(artifact_id)
        except ValueError as exc:
            raise LookupError(str(exc)) from exc
        artifact = dict(payload.get("artifact") or {})
        content = str(artifact.get("content") or "")
        safe_path = self._safe_artifact_file(artifact.get("file_path"))
        mime_type = str(artifact.get("mime_type") or "text/markdown")
        file_name = safe_path.name if safe_path else self._artifact_download_name(artifact, mime_type)
        preview = content[:max(1, int(preview_limit))]
        return {
            "ok": True,
            "artifact": {
                "artifact_id": str(artifact.get("artifact_id") or artifact_id),
                "workflow_id": str(artifact.get("workflow_id") or ""),
                "step_id": artifact.get("step_id"),
                "artifact_type": str(artifact.get("artifact_type") or ""),
                "title": str(artifact.get("title") or "执行产物"),
                "mime_type": mime_type,
                "content": preview,
                "content_size": len(content.encode("utf-8")),
                "content_truncated": len(preview) < len(content),
                "metadata": artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {},
                "validation_status": str(artifact.get("validation_status") or "pending"),
                "created_at": str(artifact.get("created_at") or ""),
                "downloadable": bool(safe_path or content),
                "download_kind": "file" if safe_path else "content" if content else "none",
                "file_name": file_name,
                "download_url": f"/api/v1/artifacts/{artifact_id}/file" if safe_path or content else "",
            },
        }

    def workflow_artifact_download(self, artifact_id: str) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("workflow service unavailable")
        try:
            artifact = dict((self.agent_service.get_workflow_artifact(artifact_id).get("artifact") or {}))
        except ValueError as exc:
            raise LookupError(str(exc)) from exc
        safe_path = self._safe_artifact_file(artifact.get("file_path"))
        if safe_path:
            return {
                "kind": "file",
                "path": safe_path,
                "mime_type": str(artifact.get("mime_type") or "application/octet-stream"),
                "file_name": safe_path.name,
            }
        content = str(artifact.get("content") or "")
        if content:
            mime_type = str(artifact.get("mime_type") or "text/markdown")
            return {
                "kind": "content",
                "content": content,
                "mime_type": mime_type,
                "file_name": self._artifact_download_name(artifact, mime_type),
            }
        raise LookupError("该产物没有可下载文件或正文")

    def _safe_artifact_file(self, value: Any) -> Path | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser().resolve()
        allowed_root = (self.db_path.parent / "asa_artifacts").resolve()
        if allowed_root not in path.parents or not path.is_file():
            return None
        return path

    @staticmethod
    def _artifact_download_name(artifact: dict[str, Any], mime_type: str) -> str:
        artifact_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(artifact.get("artifact_id") or "artifact")).strip("-") or "artifact"
        extension = {
            "text/markdown": ".md",
            "application/json": ".json",
            "text/plain": ".txt",
        }.get(mime_type, ".txt")
        return f"{artifact_id}{extension}"

    def workflow_summary(self, workflow_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_workflow_summary(workflow_id)
        raise RuntimeError("workflow service unavailable")

    def workflow_step(self, workflow_id: str, step_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_workflow_step(workflow_id, step_id)
        raise RuntimeError("workflow service unavailable")

    def workflow_candidates(self, workflow_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_workflow_candidates(workflow_id, limit, offset)
        raise RuntimeError("workflow service unavailable")

    def workflow_strategy_review(self, workflow_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_strategy_review(workflow_id)
        raise RuntimeError("workflow service unavailable")

    def rebuild_strategy_review(self, workflow_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.rebuild_strategy_review(workflow_id)
        raise RuntimeError("workflow service unavailable")

    def apply_strategy_item_edits(
        self,
        workflow_id: str,
        edits: list[dict[str, Any]],
        *,
        note: str = "",
        expected_strategy_hash: str = "",
        preflight_token: str = "",
    ) -> dict[str, Any]:
        # 寻访策略结构化按项编辑：新 revision artifact + 质量门重校验；404=工作流/策略不存在，
        # 409=状态闸门/目标项缺失/质量校验不过（ValueError/LookupError 由路由层映射）。
        edits_hash = hashlib.sha256(json.dumps(edits, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._preflight_lock:
            grant = self._strategy_edit_tokens.pop(str(preflight_token or ""), None)
        if (
            not grant
            or grant.get("workflow_id") != workflow_id
            or grant.get("edits_hash") != edits_hash
            or grant.get("strategy_hash") != str(expected_strategy_hash or "")
            or grant.get("expires_at") <= datetime.now()
        ):
            raise ValueError("策略写入预检已失效，请重新检查写入内容")
        if self.agent_service:
            return self.agent_service.apply_strategy_item_edits(
                workflow_id, edits, note=note, expected_strategy_hash=expected_strategy_hash,
            )
        raise RuntimeError("workflow service unavailable")

    def strategy_item_edits_preflight(
        self,
        workflow_id: str,
        edits: list[dict[str, Any]],
        *,
        expected_strategy_hash: str = "",
    ) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("workflow service unavailable")
        preview = self.agent_service.preflight_strategy_item_edits(
            workflow_id, edits, expected_strategy_hash=expected_strategy_hash,
        )
        token = secrets.token_urlsafe(24)
        expires = datetime.now() + timedelta(minutes=5)
        edits_hash = hashlib.sha256(json.dumps(edits, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._preflight_lock:
            now = datetime.now()
            self._strategy_edit_tokens = {
                key: value for key, value in self._strategy_edit_tokens.items()
                if value.get("expires_at") > now
            }
            self._strategy_edit_tokens[token] = {
                "workflow_id": workflow_id,
                "edits_hash": edits_hash,
                "strategy_hash": str(preview.get("strategy_hash") or ""),
                "expires_at": expires,
            }
        return {
            **preview,
            "preflight_token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "impact": "确认后只写入所列策略项并生成新 revision；不会启动寻访，R3 审批仍需单独确认。",
        }

    def create_mapping_task(self, job_id: int, *, trigger: str = "manual") -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_mapping_task(job_id, trigger=trigger)
        raise RuntimeError("workflow service unavailable")

    def get_mapping_task(self, job_id: int, artifact_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_mapping_task(job_id, artifact_id)
        raise RuntimeError("workflow service unavailable")

    def generate_candidate_assessment(self, candidate_id: int, job_id: int, *, force: bool = False) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.generate_candidate_assessment(candidate_id, job_id, force=force)
        raise RuntimeError("workflow service unavailable")

    def get_candidate_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_candidate_assessment(candidate_id, job_id)
        raise RuntimeError("workflow service unavailable")

    def refresh_candidate_fit_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.refresh_candidate_fit_assessment(candidate_id, job_id)
        raise RuntimeError("workflow service unavailable")

    def update_candidate_assessment_advisor_action(
        self, candidate_id: int, job_id: int, *, action: str, note: str = ""
    ) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.update_candidate_assessment_advisor_action(
                candidate_id, job_id, action=action, note=note
            )
        raise RuntimeError("workflow service unavailable")

    def assessment_calibration_metrics(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.assessment_calibration_metrics()
        raise RuntimeError("workflow service unavailable")

    def get_job_profile_insights(self, job_id: int) -> dict[str, Any]:
        """S8 岗位画像读取（这个岗位实际在干什么）：无画像 → status=not_generated（空结构）；
        岗位不存在 → LookupError（404）。只读，绝不触碰业务表。"""
        from a_system_agent import job_profile_insights
        from a_system_agent.schema import ensure_schema

        conn = connect(self.db_path)
        try:
            ensure_schema(conn)
            job = conn.execute("SELECT id FROM jobs WHERE id=?", (int(job_id),)).fetchone()
            if job is None:
                raise LookupError(f"岗位不存在：{job_id}")
            row = conn.execute(
                "SELECT status,source_count,insight_json,version,as_of,updated_at FROM job_profile_insights WHERE job_id=?",
                (int(job_id),),
            ).fetchone()
            if row is None:
                return {
                    "ok": True, "job_id": int(job_id), "status": "not_generated", "source_count": 0,
                    "min_source_count": job_profile_insights.MIN_SOURCE_COUNT, "as_of": "", "version": 0,
                    "duties": [], "tools": [], "deliverables": [], "customers": [], "disputed": [], "stats": {},
                }
            doc = json_value(row["insight_json"], {})
            if not isinstance(doc, dict):
                doc = {}
            return {
                "ok": True,
                "job_id": int(job_id),
                "status": str(row["status"] or "insufficient"),
                "source_count": int(row["source_count"] or 0),
                "min_source_count": job_profile_insights.MIN_SOURCE_COUNT,
                "as_of": str(row["as_of"] or ""),
                "version": int(row["version"] or 1),
                "duties": doc.get("duties") or [],
                "tools": doc.get("tools") or [],
                "deliverables": doc.get("deliverables") or [],
                "customers": doc.get("customers") or [],
                "disputed": doc.get("disputed") or [],
                "stats": doc.get("stats") or {},
            }
        finally:
            conn.close()

    def submit_job_profile_feedback(self, job_id: int, *, item_type: str, item_key: str, item_label: str = "", note: str = "") -> dict[str, Any]:
        """S8 顾问纠正通道：画像某条目标记 disputed（不删除，聚合排除 + 留痕统计）。
        ValueError（409）：item_type 非法 / item_key 为空；LookupError（404）：岗位不存在。"""
        from a_system_agent import job_profile_insights
        from a_system_agent.schema import ensure_schema

        conn = connect(self.db_path)
        try:
            ensure_schema(conn)
            result = job_profile_insights.submit_feedback(
                conn, job_id=int(job_id), item_type=str(item_type or "").strip(),
                item_key=str(item_key or ""), item_label=str(item_label or ""), note=str(note or ""),
            )
            conn.commit()
            insight = result.pop("insight", {})
            return {
                **result,
                "duties": insight.get("duties") or [],
                "tools": insight.get("tools") or [],
                "deliverables": insight.get("deliverables") or [],
                "customers": insight.get("customers") or [],
                "disputed": insight.get("disputed") or [],
                "stats": insight.get("stats") or {},
                "source_count": insight.get("source_count") or 0,
                "as_of": insight.get("as_of") or "",
            }
        finally:
            conn.close()

    def generate_assessment_calibration_report(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.generate_assessment_calibration_report()
        raise RuntimeError("workflow service unavailable")

    def update_mapping_candidate(
        self,
        artifact_id: str,
        index: int,
        *,
        status: str | None = None,
        consultant_note: str | None = None,
    ) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.update_mapping_candidate(
                artifact_id, index, status=status, consultant_note=consultant_note
            )
        raise RuntimeError("workflow service unavailable")

    def regenerate_mapping_icebreaker(self, artifact_id: str, index: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.regenerate_mapping_icebreaker(artifact_id, index)
        raise RuntimeError("workflow service unavailable")

    def intake_mapping_candidate(self, artifact_id: str, index: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.intake_mapping_candidate(artifact_id, index)
        raise RuntimeError("workflow service unavailable")

    def backflow_mapping_task(self, artifact_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.backflow_mapping_task(artifact_id)
        raise RuntimeError("workflow service unavailable")

    def mapping_metrics(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.mapping_metrics()
        raise RuntimeError("workflow service unavailable")

    def apply_strategy_review_diff_decisions(self, workflow_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.apply_strategy_review_diff_decisions(workflow_id, decisions)
        raise RuntimeError("workflow service unavailable")

    def workflow_sourcing_funnel(self, workflow_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            try:
                rows = [_row(row) for row in conn.execute(
                    """
                    SELECT * FROM agent_sourcing_funnel
                    WHERE workflow_id=? ORDER BY created_at DESC, id DESC
                    """,
                    (workflow_id,),
                )]
            except sqlite3.OperationalError:
                rows = []
            try:
                certificate_row = conn.execute(
                    """
                    SELECT certificate_json FROM agent_sourcing_coverage_certificates
                    WHERE workflow_id=? ORDER BY issued_at DESC,id DESC LIMIT 1
                    """,
                    (workflow_id,),
                ).fetchone()
                coverage_certificate = (
                    json_value(certificate_row["certificate_json"], {}) if certificate_row else None
                )
            except sqlite3.OperationalError:
                coverage_certificate = None
        finally:
            conn.close()
        sum_keys = (
            "recall_count", "extracted_count", "dedupe_count", "unique_count",
            "intake_duplicate_count", "intake_new_count", "assessed_count", "high_score_count",
        )
        channels: dict[str, dict[str, Any]] = {}
        runs: list[dict[str, Any]] = []
        for row in rows:
            detail = _funnel_detail(
                int(row.get("detail_complete") or 0),
                int(row.get("detail_partial") or 0),
                int(row.get("detail_failed") or 0),
            )
            queries = json_value(row.get("queries_json"), [])
            runs.append({
                "run_id": row.get("run_id"),
                "channel": row.get("channel"),
                "status": row.get("status"),
                "query_count": int(row.get("query_count") or 0),
                "queries": queries if isinstance(queries, list) else [],
                **{key: int(row.get(key) or 0) for key in sum_keys},
                "detail": detail,
                "zero_attribution": row.get("zero_attribution") or None,
                "error": row.get("error") or None,
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            })
            channel = str(row.get("channel") or "unknown")
            agg = channels.get(channel)
            if agg is None:
                agg = channels[channel] = {
                    "channel": channel,
                    "runs": 0,
                    "status": row.get("status"),
                    **{key: 0 for key in sum_keys},
                    "detail": {"complete": 0, "partial": 0, "failed": 0},
                    "zero_attribution": None,
                }
            agg["runs"] += 1
            for key in sum_keys:
                agg[key] += int(row.get(key) or 0)
            for key in ("complete", "partial", "failed"):
                agg["detail"][key] += detail[key]
            # rows 按时间倒序，第一个非空归因即最新归因
            if agg["zero_attribution"] is None and row.get("zero_attribution"):
                agg["zero_attribution"] = row.get("zero_attribution")
        channel_items = []
        for agg in channels.values():
            detail = agg.pop("detail")
            totals = _funnel_detail(detail["complete"], detail["partial"], detail["failed"])
            channel_items.append({**agg, "detail": totals})
        channel_items.sort(key=lambda item: str(item.get("channel") or ""))
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "channels": channel_items,
            "runs": runs,
            "coverage_certificate": coverage_certificate,
        }
