from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config, public_config
from .capability_runtime import RecruitingCapabilityRuntime, ZERO_RESULT_ATTRIBUTION_LABELS
from .context import build_candidate_context
from .evaluation import compute_evaluation
from .job_status import job_status_intake_allowed
from .llm import BaseLLM, PROMPT_VERSION, create_default_llm
from .liepin_capture import capture_open_liepin_resumes, resume_matches_identity
from .native_attachments import attachment_read_requested, image_analysis_requested, resolve_wechat_attachments
from .panel import (
    ROLE_DEFINITIONS,
    fallback_role_review,
    normalize_role_review,
    role_payload,
    synthesize_panel,
)
from .policy import action_decision, is_stopped
from .privacy import sanitize_payload
from .schema import ensure_schema
from .scoring import normalize_assessment
from .skills import SkillRegistry, SkillSpec
from . import strategy_v2
from .workflow import BUSINESS_OUTCOME_LABELS, WorkflowEngine, classify_business_outcome, sourcing_target_stats


ASSESSMENT_VERSION = "candidate-assessment-v1"
PANEL_VERSION = "candidate-panel-v2"
OPENCLI_BIN = Path(os.environ.get("A_SYSTEM_OPENCLI_BIN", "/Users/messi/.hermes/node/bin/opencli")).expanduser()
OPENCLI_BROWSER_READ_COMMANDS = {
    "analyze",
    "bind",
    "console",
    "extract",
    "find",
    "frames",
    "get",
    "network",
    "screenshot",
    "state",
    "wait",
    "verify",
}
OPENCLI_BROWSER_TAB_READ_COMMANDS = {"current", "list"}
DECISION_LABELS = {
    "priority_review": "建议优先复核",
    "verify_first": "先核验后判断",
    "hold": "暂缓",
    "not_recommended": "建议不推进",
}
SOURCING_SIGNAL_WEIGHTS = {
    "review_pass": 1.0,
    "contacted": 2.0,
    "recommended": 3.0,
    "stopped": -2.0,
    "client_approved": 4.0,
    "client_interview": 4.5,
    "client_offer": 6.0,
    "client_hired": 8.0,
    "client_rejected": -3.0,
    "client_hold": 0.0,
}
SOURCING_SIGNAL_LABELS = {
    "review_pass": "用户复核通过",
    "contacted": "用户已联系",
    "recommended": "用户已推荐客户",
    "stopped": "用户停止推进",
    "client_approved": "客户认可",
    "client_interview": "客户进入面试",
    "client_offer": "客户进入 Offer",
    "client_hired": "客户确认入职",
    "client_rejected": "客户否决",
    "client_hold": "客户暂缓",
}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row else {}


def _is_short_ack(message: str) -> bool:
    cleaned = re.sub(r"[\s。.!！?？,，、]+", "", str(message or ""))
    return cleaned in {"好", "好的", "好了", "可以", "行", "嗯", "收到", "明白", "ok", "OK"}


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _latest_event(context: dict[str, Any], event_type: str) -> dict[str, Any]:
    for event in context.get("events", []) or []:
        if event.get("event_type") == event_type:
            return event
    return {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _channel_key(source: Any) -> str:
    value = str(source or "").strip().lower()
    if "xsaas" in value or "x-saas" in value:
        return "xsaas"
    if "liepin" in value or "猎聘" in value:
        return "liepin"
    if "legacy" in value or "talent_pool" in value or "历史" in value:
        return "talent_pool"
    if value:
        return "other"
    return "unknown"


class AgentService:
    def __init__(self, db_path: str | Path, llm: BaseLLM | None = None, max_workers: int | None = None) -> None:
        self.db_path = Path(db_path).expanduser()
        self.config = load_config()
        self.llm = llm or create_default_llm(self.config)
        worker_count = max_workers or int(self.config["runtime"]["max_workers"])
        self.executor = ThreadPoolExecutor(max_workers=max(1, worker_count), thread_name_prefix="a-system-agent")
        self._lock = threading.Lock()
        self._copilot_locks_guard = threading.Lock()
        self._copilot_session_locks: dict[str, threading.RLock] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._active_by_snapshot: dict[tuple[int, str], str] = {}
        self._active_panel_by_snapshot: dict[tuple[int, str], str] = {}
        self._learning_confirmations: dict[str, dict[str, Any]] = {}
        self._proposal_confirmations: dict[str, dict[str, Any]] = {}
        conn = self._connect()
        try:
            ensure_schema(conn)
            conn.execute(
                """
                UPDATE agent_runs SET status='interrupted',
                    error='服务重启时任务仍未结束',
                    finished_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime')
                WHERE status IN ('queued','running')
                """
            )
            if _table_exists(conn, "candidates") and _table_exists(conn, "job_candidates"):
                candidate_columns = _table_columns(conn, "candidates")
                if "notes" in candidate_columns:
                    legacy_sourcing = conn.execute(
                        """
                        SELECT jc.id
                        FROM job_candidates jc
                        JOIN candidates c ON CAST(c.id AS TEXT)=jc.source_candidate_id
                        WHERE c.notes LIKE '%query=%'
                          AND NOT EXISTS (
                            SELECT 1 FROM agent_sourcing_attributions sa WHERE sa.job_candidate_id=jc.id
                          )
                        ORDER BY jc.id LIMIT 1000
                        """
                    ).fetchall()
                    for legacy_row in legacy_sourcing:
                        self._ensure_sourcing_attribution(conn, int(legacy_row["id"]))
            conn.commit()
        finally:
            conn.close()
        self.skills = SkillRegistry(list(self.config["skills"].get("enabled") or []))
        self.capability_runtime = RecruitingCapabilityRuntime(self)
        self._register_builtin_skills()
        self.capabilities = self.skills
        self.workflow_engine = WorkflowEngine(self)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def get_public_config(self) -> dict[str, Any]:
        return {
            "ok": True,
            "config": public_config(self.config, model_available=self.llm.model != "unavailable"),
        }

    def _register_builtin_skills(self) -> None:
        self.skills.register(
            SkillSpec(
                id="job_diagnosis", version="1.0", risk_level="R0",
                supported_contexts=("global", "page", "job"),
                input_schema={}, output_schema={"diagnosis": "object"},
                handler=self._skill_job_diagnosis,
            )
        )
        self.skills.register(
            SkillSpec(
                id="candidate_assessment", version="1.0", risk_level="R0",
                supported_contexts=("candidate",),
                input_schema={}, output_schema={"assessment": "object"},
                handler=self._skill_candidate_assessment,
            )
        )
        self.skills.register(
            SkillSpec(
                id="verification_plan", version="1.0", risk_level="R1",
                supported_contexts=("candidate",),
                input_schema={}, output_schema={"questions": "array"},
                handler=self._skill_verification_plan,
            )
        )
        self.skills.register(
            SkillSpec(
                id="communication_draft", version="1.0", risk_level="R1",
                supported_contexts=("candidate",),
                input_schema={"instructions?": "string"}, output_schema={"draft": "string"},
                handler=self._skill_communication_draft,
            )
        )
        self.skills.register(
            SkillSpec(
                id="liepin_resume_capture", version="1.0", risk_level="R1",
                supported_contexts=("candidate",),
                input_schema={"cdp_port?": "integer"}, output_schema={"resume": "object", "assessment": "object"},
                handler=self._skill_liepin_resume_capture,
            )
        )
        self.skills.register(
            SkillSpec(
                id="opencli_usage", version="1.0", risk_level="R0",
                supported_contexts=("global", "page", "job", "candidate", "queue"),
                input_schema={"command?": "string"}, output_schema={"opencli": "object"},
                handler=self._skill_opencli_usage,
                label="OpenCLI 状态与命令查询",
            )
        )
        self.skills.register(
            SkillSpec(
                id="opencli_browser_read", version="1.0", risk_level="R1",
                supported_contexts=("global", "page", "job", "candidate", "queue"),
                input_schema={"args": "string", "timeout_seconds?": "integer"}, output_schema={"opencli": "object"},
                handler=self._skill_opencli_browser_read,
                business_stage="browser",
                adapter_type="script",
                timeout_seconds=30,
                label="OpenCLI 浏览器只读操作",
            )
        )
        self.skills.register(
            SkillSpec(
                id="document_understanding", version="1.0", risk_level="R1",
                supported_contexts=("global", "page", "job", "candidate", "queue"),
                input_schema={"request": "string", "bridge": "object"}, output_schema={"attachment_evidence": "object"},
                handler=self._skill_document_understanding,
                business_stage="document",
                adapter_type="native",
                label="本机文档理解",
            )
        )
        workflow_capabilities = [
            ("job_intake", "岗位接入", "job_intake", "R1", "native", ("job_brief",)),
            ("jd_calibration", "校准岗位要求", "jd_calibration", "R0", "native", ("jd_calibration",)),
            ("job_library_update", "更新岗位库", "job_intake", "R2", "native", ("job_library_update_receipt",)),
            ("talent_pool_search", "检索历史人才库", "sourcing", "R0", "native", ()),
            ("search_strategy", "生成寻访策略", "search_strategy", "R1", "script", ("search_strategy",)),
            ("multi_channel_sourcing", "执行多渠道寻访", "sourcing", "R3", "browser", ("sourcing_ticket",)),
            ("job_publish_prepare", "准备岗位发布", "job_intake", "R1", "browser", ("job_publish_draft", "job_publish_prepare_readback")),
            ("job_publish_execute", "发布猎聘岗位", "job_intake", "R3", "browser", ("external_action_receipt",)),
            ("resume_export", "导出结构化简历", "resume_capture", "R1", "script", ("resume_document",)),
            ("candidate_batch_assessment", "批量评估候选人", "assessment", "R1", "native", ()),
            ("matching_report", "生成人岗匹配报告", "recommendation", "R1", "script", ("matching_report",)),
            ("recommendation_report", "生成嘉驰推荐报告", "recommendation", "R1", "script", ("recommendation_report",)),
            ("client_recommendation", "提交客户推荐", "recommendation", "R3", "browser", ("external_action_receipt",)),
            ("reply_triage", "识别回复与待办", "reply", "R0", "native", ()),
            ("communication_draft_batch", "批量生成沟通草稿", "outreach", "R1", "native", ("communication_drafts",)),
            ("outreach_prepare", "锁定触达草稿", "outreach", "R1", "native", ("outreach_draft_batch",)),
            ("outreach_execute", "执行候选人触达", "outreach", "R3", "browser", ("external_action_receipt",)),
            ("identity_merge_preflight", "身份合并预检", "verification", "R2", "native", ("identity_comparison",)),
            ("interview_followup", "整理面试与客户反馈", "interview", "R1", "native", ("interview_note",)),
            ("salary_verification", "核验薪资证据", "salary", "R1", "script", ("salary_report",)),
            ("salary_negotiation", "整理谈薪风险", "salary", "R1", "native", ("salary_negotiation_note",)),
            ("decision_coaching", "生成决策辅导方案", "decision", "R1", "native", ("decision_coaching",)),
            ("offer_confirmation", "确认 Offer 条件", "offer", "R3", "native", ("offer_confirmation",)),
            ("onboarding_followup", "创建入职跟进", "onboarding", "R1", "native", ("onboarding_note",)),
            ("project_retrospective", "生成项目复盘", "retrospective", "R1", "native", ("project_retrospective",)),
            ("memory_capture", "沉淀已确认经验", "retrospective", "R2", "native", ()),
        ]
        context_contracts = {
            "job_intake": ("job",), "jd_calibration": ("job",), "job_library_update": ("global", "page", "job"),
            "talent_pool_search": ("global", "page", "job"),
            "search_strategy": ("job",), "multi_channel_sourcing": ("job",), "job_publish_prepare": ("job",),
            "job_publish_execute": ("job",), "resume_export": ("candidate",), "candidate_batch_assessment": ("job", "candidate", "queue"),
            "matching_report": ("candidate",), "recommendation_report": ("candidate",), "client_recommendation": ("candidate",),
            "reply_triage": ("global", "page", "job", "candidate", "queue"), "communication_draft_batch": ("global", "page", "job", "candidate", "queue"),
            "outreach_prepare": ("global", "page", "job", "candidate", "queue"),
            "outreach_execute": ("global", "page", "job", "candidate", "queue"), "identity_merge_preflight": ("candidate",), "interview_followup": ("candidate",),
            "salary_verification": ("candidate",), "salary_negotiation": ("candidate",), "decision_coaching": ("candidate",),
            "offer_confirmation": ("candidate",), "onboarding_followup": ("candidate",), "project_retrospective": ("job", "candidate"),
            "memory_capture": ("global", "job", "candidate"),
        }
        input_contracts = {
            "job_library_update": {"client?": "string", "directions?": "array", "archive_legacy?": "boolean", "skip_sync?": "boolean"},
            "multi_channel_sourcing": {"target_count?": "integer", "cdp_port?": "integer"},
            "job_publish_prepare": {"publish_fields?": "object"}, "job_publish_execute": {"cdp_port?": "integer"},
            "salary_verification": {"salary_data?": "object"}, "outreach_prepare": {"message?": "string", "queue?": "string", "limit?": "integer"},
            "outreach_execute": {"message?": "string", "cdp_port?": "integer"},
            "client_recommendation": {"channel?": "string"}, "offer_confirmation": {"offer_terms?": "object"},
            "identity_merge_preflight": {"other_job_candidate_id?": "integer"}, "memory_capture": {"confirmed_memory?": "string"},
        }
        for capability_id, label, stage, risk, adapter, artifacts in workflow_capabilities:
            self.skills.register(
                SkillSpec(
                    id=capability_id, version="2.1", risk_level=risk,
                    supported_contexts=context_contracts[capability_id],
                    input_schema=input_contracts.get(capability_id, {}), output_schema={"summary": "string"},
                    handler=lambda context, inputs, current=capability_id: self._execute_workflow_capability(current, context, inputs),
                    business_stage=stage, adapter_type=adapter, timeout_seconds=300 if adapter != "native" else 60,
                    retry_limit=1, idempotent=capability_id not in {"outreach_execute", "client_recommendation", "job_publish_execute", "offer_confirmation"},
                    required_permissions=("single_action_confirmation",) if risk in {"R2", "R3"} else (),
                    artifact_types=artifacts, rollback_policy="audit_only" if risk == "R3" else "reversible_internal",
                    label=label,
                )
            )

    def _current_assessed_candidates(self, job_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT a.job_candidate_id,
                       p.display_name AS name,
                       p.current_company AS company,
                       p.current_title AS title,
                       p.city,p.education,p.experience,
                       a.fit_score,a.fit_level,a.recommendation,a.confidence,
                       a.next_action,a.created_at AS assessed_at,
                       COALESCE((
                           SELECT sp.source_type
                           FROM source_profiles sp
                           WHERE sp.person_id=p.id
                           ORDER BY sp.source_date DESC,sp.id DESC
                           LIMIT 1
                       ),'talent_pool') AS channel
                FROM agent_candidate_assessments a
                JOIN job_candidates jc ON jc.id=a.job_candidate_id
                JOIN people p ON p.id=jc.person_id
                JOIN agent_runs r ON r.run_id=a.run_id
                WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
                ORDER BY a.fit_score DESC,a.job_candidate_id DESC
                """,
                (int(job_id),),
            ).fetchall()
            return [_row(row) for row in rows]
        finally:
            conn.close()

    def _execute_workflow_capability(self, capability_id: str, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        locally_specialized = {"talent_pool_search", "candidate_batch_assessment", "reply_triage", "communication_draft_batch"}
        if capability_id not in locally_specialized:
            return self.capability_runtime.execute(capability_id, context, inputs)
        context_type = str(context.get("type") or "global")
        context_id = int(context.get("id") or 0) if context_type in {"job", "candidate"} else 0
        references: list[dict[str, Any]] = []
        facts: dict[str, Any] = {}
        conn = self._connect()
        try:
            if context_type == "job" and context_id:
                row = conn.execute(
                    """
                    SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,j.hard_requirements,
                           j.ability_keywords,j.target_companies,j.exclusions,
                           COUNT(jc.id) AS candidate_total
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    LEFT JOIN job_candidates jc ON jc.job_id=j.id
                    WHERE j.id=? GROUP BY j.id
                    """,
                    (context_id,),
                ).fetchone()
                facts = _row(row)
                if row:
                    references.append({"type": "job", "id": context_id, "label": row["job"], "subtitle": row["client"]})
            elif context_type == "candidate" and context_id:
                candidate_context = build_candidate_context(self.db_path, context_id)
                facts = {
                    "identity": candidate_context.get("identity") or {},
                    "position": candidate_context.get("position") or {},
                    "relation": candidate_context.get("relation") or {},
                }
                references.append({
                    "type": "candidate", "id": context_id,
                    "label": facts["identity"].get("name") or f"关系 #{context_id}",
                    "subtitle": f"{facts['position'].get('client','')} / {facts['position'].get('job','')}",
                })
        finally:
            conn.close()

        if capability_id == "talent_pool_search":
            conn = self._connect()
            try:
                if context_type == "job" and context_id:
                    rows = conn.execute(
                        """
                        SELECT jc.id,p.display_name,p.current_company,p.current_title,jc.clean_stage,jc.flow_bucket
                        FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                        WHERE jc.job_id=? ORDER BY jc.updated_at DESC,jc.id DESC LIMIT 50
                        """,
                        (context_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT jc.id,p.display_name,p.current_company,p.current_title,jc.clean_stage,jc.flow_bucket FROM job_candidates jc JOIN people p ON p.id=jc.person_id ORDER BY jc.updated_at DESC,jc.id DESC LIMIT 20"
                    ).fetchall()
            finally:
                conn.close()
            candidates = [_row(row) for row in rows]
            return {
                "summary": f"历史人才库检索完成，共读取 {len(candidates)} 条相关人岗关系。",
                "candidates": candidates,
                "references": [
                    {"type": "candidate", "id": item["id"], "label": item["display_name"], "subtitle": f"{item['current_company']} / {item['current_title']}"}
                    for item in candidates[:8]
                ],
            }
        if capability_id == "candidate_batch_assessment":
            def assessment_stats() -> tuple[dict[str, int], list[dict[str, Any]]]:
                if context_type != "job" or not context_id:
                    return ({"completed": len(completed), "score_75_plus": 0, "verify_first": 0, "low_score": 0}, [])
                assessed_items = self._current_assessed_candidates(context_id)
                return ({
                    "completed": len(assessed_items),
                    "score_75_plus": len([item for item in assessed_items if int(item.get("fit_score") or 0) >= 75]),
                    "verify_first": len([item for item in assessed_items if item.get("recommendation") == "verify_first"]),
                    "low_score": len([item for item in assessed_items if int(item.get("fit_score") or 0) < 55]),
                }, assessed_items)

            conn = self._connect()
            try:
                ids = [
                    int(row[0]) for row in conn.execute(
                        """
                        SELECT jc.id
                        FROM job_candidates jc
                        WHERE jc.job_id=?
                          AND COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'
                          AND COALESCE(jc.raw_status,'') NOT IN ('screen_rejected','rejected')
                          AND NOT EXISTS (
                            SELECT 1 FROM agent_candidate_assessments a
                            JOIN agent_runs r ON r.run_id=a.run_id
                            WHERE a.job_candidate_id=jc.id AND a.is_current=1 AND r.status='completed'
                          )
                        ORDER BY jc.id DESC LIMIT 50
                        """,
                        (context_id,),
                    ).fetchall()
                ] if context_type == "job" and context_id else ([context_id] if context_type == "candidate" and context_id else [])
            finally:
                conn.close()
            completed: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            for job_candidate_id in ids:
                try:
                    result = self.submit_assessment(
                        job_candidate_id,
                        trigger="workflow_candidate_batch_assessment",
                        wait=True,
                        timeout=180,
                    )
                    if result.get("status") == "completed":
                        assessment = result.get("assessment") or {}
                        verification_task_id = None
                        if assessment.get("recommendation") == "verify_first":
                            verification_task_id = self.ensure_verification_task(job_candidate_id, assessment)
                        completed.append({
                            "job_candidate_id": job_candidate_id,
                            "fit_score": assessment.get("fit_score"),
                            "recommendation": assessment.get("recommendation"),
                            "verification_task_id": verification_task_id,
                        })
                    else:
                        failed.append({"job_candidate_id": job_candidate_id, "error": result.get("error") or "评估未完成"})
                except Exception as exc:
                    failed.append({"job_candidate_id": job_candidate_id, "error": str(exc)[:300]})
            summary = f"候选人评估完成 {len(completed)} 人"
            if failed:
                summary += f"，失败 {len(failed)} 人"
            stats, assessed_items = assessment_stats()
            assessed_by_id = {int(item["job_candidate_id"]): item for item in assessed_items}
            completed = [{**assessed_by_id.get(int(item["job_candidate_id"]), {}), **item} for item in completed]
            if context_type == "job" and context_id:
                summary = (
                    f"本轮完成评估 {len(completed)} 位；岗位当前已有 {stats['completed']} 位评估结果。"
                    if completed else f"本轮没有新增待评估人选；岗位当前已有 {stats['completed']} 位评估结果。"
                )
            return {
                "summary": summary,
                "assessment_queue": {
                    **stats,
                    "completed_items": completed,
                    "assessed_items": assessed_items,
                    "failed": failed,
                    "started": len(ids),
                    "total": len(ids),
                },
                "references": references,
                "blocked": bool(failed),
                "missing_inputs": ["检查模型连接后重试失败评估"] if failed else [],
            }
        if capability_id == "reply_triage":
            inbox = self.get_flow_inbox(queue="已回复", limit=50)
            return {"summary": f"回复分流完成，当前识别 {len(inbox.get('items') or [])} 条回复行动。", "reply_items": inbox.get("items") or [], "references": references}
        if capability_id == "communication_draft_batch":
            inbox = self.get_flow_inbox(queue="已回复", limit=20)
            items = inbox.get("items") or []
            lines = ["# 候选人沟通草稿队列", ""]
            for item in items[:12]:
                lines.extend([
                    f"## {item.get('candidate') or '人选'} · {item.get('client') or ''} / {item.get('job') or ''}",
                    f"当前信号：{item.get('signal') or item.get('next_action') or '待人工判断'}",
                    "草稿：感谢你的回复。关于当前机会，我想结合你的关注点补充岗位信息，并确认下一步沟通时间。",
                    "",
                ])
            return {
                "summary": f"已为 {len(items[:12])} 条回复生成未发送草稿，未执行任何触达。",
                "references": [{"type": "candidate", "id": item.get("job_candidate_id"), "label": item.get("candidate"), "subtitle": f"{item.get('client','')} / {item.get('job','')}"} for item in items[:8]],
                "artifacts": [{"type": "communication_drafts", "title": "正向回复沟通草稿", "mime_type": "text/markdown", "content": "\n".join(lines), "validation_status": "passed"}],
            }

        external = capability_id in {"multi_channel_sourcing", "job_publish_execute", "client_recommendation", "outreach_execute", "offer_confirmation"}
        artifact_type = {
            "search_strategy": "search_strategy", "matching_report": "matching_report",
            "recommendation_report": "recommendation_report", "salary_verification": "salary_report",
            "salary_negotiation": "salary_negotiation_note", "decision_coaching": "decision_coaching",
            "interview_followup": "interview_note", "onboarding_followup": "onboarding_note",
            "project_retrospective": "project_retrospective", "job_publish_prepare": "job_publish_draft",
            "resume_export": "resume_document", "identity_merge_preflight": "identity_comparison",
        }.get(capability_id, "workflow_note")
        label = (self.skills.get(capability_id).label if self.skills.get(capability_id) else capability_id)
        evidence_lines = []
        if facts:
            evidence_lines.append(json.dumps(facts, ensure_ascii=False, indent=2))
        content = (
            f"# {label}\n\n"
            f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"- 上下文：{context_type} #{context_id or '-'}\n"
            f"- 执行模式：{'单次批准后的外部动作票据' if external else 'ASA 内部可审计产物'}\n\n"
            f"## 目标与依据\n\n{chr(10).join(evidence_lines) or '使用当前 v3 驾驶舱上下文。'}\n\n"
            "## 安全边界\n\n"
            + ("本记录表示该动作已获单次批准；实际渠道执行必须返回结果读回，未返回前不得声称已触达、发布或推荐。" if external else "本产物未执行对外发送、推荐、淘汰或身份合并。")
        )
        return {
            "summary": f"{label}已完成。" if not external else f"{label}已生成单次执行票据，等待渠道结果读回。",
            "references": references,
            "external_action_executed": False if external else None,
            "artifacts": [{
                "type": "external_action_ticket" if external else artifact_type,
                "title": label, "mime_type": "text/markdown", "content": content,
                "metadata": {"capability_id": capability_id, "context": context, "external_action_executed": False if external else None},
                "validation_status": "pending_execution" if external else "passed",
            }],
        }

    def _run_opencli(self, args: list[str], timeout_seconds: int = 20) -> dict[str, Any]:
        if not OPENCLI_BIN.exists():
            return {"ok": False, "blocked": True, "reason": f"找不到 OpenCLI：{OPENCLI_BIN}"}
        timeout = max(1, min(int(timeout_seconds), 60))
        child_env = os.environ.copy()
        child_env["PATH"] = str(OPENCLI_BIN.parent) + os.pathsep + child_env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        proc = subprocess.run(
            [str(OPENCLI_BIN), *args],
            cwd=str(self.db_path.parent),
            capture_output=True,
            env=child_env,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout[-12000:]
        stderr = proc.stderr[-4000:]
        parsed: Any = None
        if stdout.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None
        return {
            "ok": proc.returncode == 0,
            "command": [str(OPENCLI_BIN), *args],
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "json": parsed,
        }

    def _skill_opencli_usage(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        command = str(inputs.get("command") or "list").strip().lower()
        if command == "browser":
            result = self._run_opencli(["browser", "--help"], timeout_seconds=10)
        elif command == "skills":
            result = self._run_opencli(["skills", "list"], timeout_seconds=10)
        else:
            result = self._run_opencli(["list", "-f", "json"], timeout_seconds=20)
        return {"opencli": result}

    def _skill_opencli_browser_read(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        raw_args = str(inputs.get("args") or "").strip()
        if not raw_args:
            raise ValueError("opencli_browser_read 需要 args，例如：'asa state'")
        parts = shlex.split(raw_args)
        if parts[:2] == ["opencli", "browser"]:
            parts = parts[2:]
        elif parts[:1] == ["browser"]:
            parts = parts[1:]
        if len(parts) < 2:
            raise ValueError("args 必须包含 session 与只读命令，例如：'asa state'")
        action = parts[1]
        allowed = action in OPENCLI_BROWSER_READ_COMMANDS
        if action == "tab":
            allowed = len(parts) >= 3 and parts[2] in OPENCLI_BROWSER_TAB_READ_COMMANDS
        if not allowed:
            raise ValueError("opencli_browser_read 只允许读取型 browser 命令；写动作请走工作流审批。")
        timeout = int(inputs.get("timeout_seconds") or 30)
        return {"opencli": self._run_opencli(["browser", *parts], timeout_seconds=timeout)}

    def _skill_document_understanding(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        evidence = resolve_wechat_attachments(
            {"bridge": inputs.get("bridge") if isinstance(inputs.get("bridge"), dict) else {}},
            str(inputs.get("request") or ""),
        )
        items = evidence.get("items") if isinstance(evidence, dict) else []
        return {
            "attachment_evidence": evidence,
            "summary": f"已检查 {len(items or [])} 个当前窗口可见附件。",
            "references": [
                {
                    "type": "native_attachment",
                    "id": "",
                    "label": item.get("file_name") or "微信附件",
                    "subtitle": item.get("status") or "本机文档理解",
                }
                for item in (items or [])
            ],
        }

    def list_skills(self) -> dict[str, Any]:
        return {"ok": True, "skills": self.skills.list()}

    def record_context_snapshot(self, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(source or "unknown").strip().lower()[:80] or "unknown"
        sanitized = sanitize_payload(payload if isinstance(payload, dict) else {})
        context = sanitized.get("context") if isinstance(sanitized.get("context"), dict) else {}
        context_type = str(context.get("type") or sanitized.get("page_type") or source or "global").strip().lower()[:60]
        context_id = str(context.get("id") or sanitized.get("job_candidate_id") or sanitized.get("id") or "").strip()[:120] or None
        candidate = sanitized.get("candidate") if isinstance(sanitized.get("candidate"), dict) else {}
        title = (
            str(context.get("label") or candidate.get("name") or sanitized.get("candidate_name") or sanitized.get("window_title") or sanitized.get("title") or "")
            .strip()
            .replace("\n", " ")[:180]
        )
        summary_parts = [
            str(sanitized.get("surface") or source),
            str(context.get("subtitle") or ""),
            str(sanitized.get("status") or ""),
        ]
        summary = " · ".join(part.strip() for part in summary_parts if part and part.strip())[:300]
        encoded = _dumps(sanitized)
        snapshot_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        snapshot_id = f"ctx_{snapshot_hash[:16]}"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_context_snapshots
                (snapshot_id,source,context_type,context_id,snapshot_hash,title,summary,payload_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (snapshot_id, source, context_type, context_id, snapshot_hash, title, summary, encoded),
            )
            conn.commit()
            return {"ok": True, "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash}
        finally:
            conn.close()

    def record_tool_call(
        self,
        *,
        tool_name: str,
        permission_level: str = "read",
        request: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        status: str = "completed",
        snapshot_id: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        tool_name = str(tool_name or "unknown").strip()[:120] or "unknown"
        permission_level = str(permission_level or "read").strip().lower()
        if permission_level not in {"read", "write", "system_control", "external_send"}:
            permission_level = "write"
        status = str(status or "completed").strip().lower()[:40] or "completed"
        call_id = f"tool_{secrets.token_hex(8)}"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_tool_calls
                (call_id,snapshot_id,tool_name,permission_level,request_json,result_json,status,error,finished_at)
                VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))
                """,
                (
                    call_id,
                    str(snapshot_id or "").strip() or None,
                    tool_name,
                    permission_level,
                    _dumps(sanitize_payload(request or {})),
                    _dumps(sanitize_payload(result or {})),
                    status,
                    str(error or "")[:1000] or None,
                ),
            )
            conn.commit()
            return {"ok": True, "call_id": call_id}
        finally:
            conn.close()

    def record_permission_request(
        self,
        *,
        tool_name: str,
        permission_level: str,
        risk_level: str,
        reason: str,
        preview: dict[str, Any] | None = None,
        status: str = "pending",
        scope: str = "",
    ) -> dict[str, Any]:
        permission_id = f"perm_{secrets.token_hex(8)}"
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_permissions
                (permission_id,tool_name,permission_level,risk_level,status,reason,preview_json,scope,decided_at)
                VALUES (?,?,?,?,?,?,?,?,CASE WHEN ?!='pending' THEN datetime('now','localtime') ELSE NULL END)
                """,
                (
                    permission_id,
                    str(tool_name or "unknown")[:120],
                    str(permission_level or "write")[:60],
                    str(risk_level or "low")[:40],
                    str(status or "pending")[:40],
                    str(reason or "")[:500],
                    _dumps(sanitize_payload(preview or {})),
                    str(scope or "")[:160] or None,
                    str(status or "pending")[:40],
                ),
            )
            conn.commit()
            return {"ok": True, "permission_id": permission_id}
        finally:
            conn.close()

    def get_runtime_timeline(self, limit: int = 8) -> dict[str, Any]:
        limit = max(1, min(int(limit or 8), 30))
        conn = self._connect()
        try:
            snapshots = [
                {
                    **_row(row),
                    "payload": _loads(row["payload_json"], {}),
                }
                for row in conn.execute(
                    """
                    SELECT snapshot_id,source,context_type,context_id,title,summary,payload_json,created_at
                    FROM agent_context_snapshots ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            for item in snapshots:
                item.pop("payload_json", None)
            tool_calls = [
                {
                    **_row(row),
                    "request": _loads(row["request_json"], {}),
                    "result": _loads(row["result_json"], {}),
                }
                for row in conn.execute(
                    """
                    SELECT call_id,snapshot_id,tool_name,permission_level,status,error,request_json,result_json,created_at,finished_at
                    FROM agent_tool_calls ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            for item in tool_calls:
                item.pop("request_json", None)
                item.pop("result_json", None)
            permissions = [
                {
                    **_row(row),
                    "preview": _loads(row["preview_json"], {}),
                }
                for row in conn.execute(
                    """
                    SELECT permission_id,tool_name,permission_level,risk_level,status,reason,preview_json,scope,created_at,decided_at
                    FROM agent_permissions ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            for item in permissions:
                item.pop("preview_json", None)
            return {
                "ok": True,
                "context_snapshots": snapshots,
                "tool_calls": tool_calls,
                "permission_audit": permissions,
            }
        finally:
            conn.close()

    def create_goal(self, objective: str, context: dict[str, Any] | None = None, priority: int = 2) -> dict[str, Any]:
        return self.workflow_engine.create_goal(objective, context, priority)

    def list_goals(self, status: str = "", limit: int = 30) -> dict[str, Any]:
        return self.workflow_engine.list_goals(status, limit)

    def list_goal_templates(self) -> dict[str, Any]:
        return {
            "ok": True,
            "templates": [
                {
                    "id": "today_reply_triage",
                    "title": "今日回复处理",
                    "objective": "处理今日正向回复，生成沟通草稿并锁定需要审批的下一步",
                    "context": {"type": "queue", "page": "overview", "filters": {"queue": "已回复"}},
                    "priority": 1,
                    "summary": "分流正向/拒绝/薪资/地点信号，输出草稿、证据和后续审批项。",
                    "expected_outputs": ["回复分流", "沟通草稿", "待审批触达预检", "下一步队列"],
                    "automation": "R0/R1 自动整理；发送触达必须审批。",
                },
                {
                    "id": "job_sourcing_refill",
                    "title": "岗位补池/寻访",
                    "objective": "给当前 P0 岗位补充10位合适人选",
                    "context": {"type": "job", "page": "overview", "filters": {}},
                    "priority": 1,
                    "summary": "先查历史人才库和策略，再对多渠道寻访执行票据走审批。",
                    "expected_outputs": ["岗位缺口诊断", "历史库候选", "寻访策略", "多渠道执行票据", "新增人选评估"],
                    "automation": "内部诊断自动；猎聘/X-SaaS 执行需审批和结果回读。",
                },
                {
                    "id": "candidate_report_salary_materials",
                    "title": "推荐报告/谈薪材料",
                    "objective": "为当前人选生成推荐报告和谈薪材料",
                    "context": {"type": "candidate", "page": "overview", "filters": {}},
                    "priority": 2,
                    "summary": "复核人岗证据，生成匹配报告、推荐报告，并整理谈薪材料缺口。",
                    "expected_outputs": ["人岗复核", "匹配分析", "嘉驰推荐报告", "谈薪证据清单", "下一步建议"],
                    "automation": "报告草稿可自动生成；对客户提交推荐仍需审批。",
                },
            ],
        }

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.workflow_engine.get_workflow(workflow_id)

    def get_workflow_summary(self, workflow_id: str) -> dict[str, Any]:
        return self.workflow_engine.get_workflow_summary(workflow_id)

    def get_workflow_step(self, workflow_id: str, step_id: int) -> dict[str, Any]:
        return self.workflow_engine.get_workflow_step(workflow_id, step_id)

    def get_workflow_candidates(self, workflow_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.workflow_engine.get_workflow_candidates(workflow_id, limit, offset)

    def get_strategy_review(self, workflow_id: str) -> dict[str, Any]:
        """S4-3：读取工作流最新策略复盘；工作流不存在/无复盘均抛 LookupError（API 404）。"""
        from . import strategy_review

        conn = self._connect()
        try:
            exists = conn.execute("SELECT 1 FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if exists is None:
                raise LookupError(f"工作流不存在：{workflow_id}")
            payload = strategy_review.get_strategy_review(conn, workflow_id)
            if payload is None:
                raise LookupError(f"该工作流还没生成原因分析：{workflow_id}")
            return {"ok": True, **payload}
        finally:
            conn.close()

    def rebuild_strategy_review(self, workflow_id: str) -> dict[str, Any]:
        """S4-3：按需重算复盘（存量终局工作流补生成；幂等覆盖，version 自增 + history）。

        工作流不存在抛 LookupError（404）；非终局（completed/blocked/failed）抛 ValueError（409）。
        """
        from . import strategy_review

        conn = self._connect()
        try:
            artifact_id, review = strategy_review.rebuild_for_workflow(conn, workflow_id)
            conn.commit()
            return {"ok": True, "workflow_id": workflow_id, "artifact_id": artifact_id, "review": review}
        finally:
            conn.close()

    def apply_strategy_review_diff_decisions(self, workflow_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """S4-3c：顾问逐项采纳/拒绝复盘 diff——status 落 artifact（upsert 可重复覆盖），
        同一写动作追加 strategy_v2.consultant_edits 并写 explicit_corrections 学习信号。

        工作流不存在/无复盘抛 LookupError（404）；diff_id 未知或 status 非法抛 ValueError（409）。
        """
        from . import strategy_review

        conn = self._connect()
        try:
            payload = strategy_review.apply_diff_decisions(conn, workflow_id, decisions)
            conn.commit()
            return payload
        finally:
            conn.close()

    def create_mapping_task(self, job_id: int, *, trigger: str = "manual", collector: Any = None) -> dict[str, Any]:
        """S5-1：发起 Mapping 直挖——目标团队定位 + 名单生成，落 mapping_task artifact 并写 job 时间线。

        岗位不存在抛 LookupError（404）；岗位无 strategy_v2 策略或 trigger 非法抛 ValueError（409）。
        红线：不自动触达；无来源人名拒写；禁挖名单照常过滤；restricted 仅白名单出库。
        collector 可注入（测试用本地 fixture，绝不打外网；缺省为标准库只读采集器）。
        """
        from . import knowledge_base, mapping_task, strategy_v2

        conn = self._connect()
        try:
            job = conn.execute(
                "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (int(job_id),),
            ).fetchone()
            if job is None:
                raise LookupError(f"岗位不存在：{job_id}")
            if trigger not in mapping_task.TRIGGERS:
                raise ValueError(f"trigger 必须是 {'/'.join(mapping_task.TRIGGERS)}")
            strategy = conn.execute(
                """
                SELECT w.workflow_id,g.goal_id,a.artifact_id,a.metadata_json
                FROM agent_workflows w
                JOIN agent_goals g ON g.goal_id=w.goal_id
                JOIN agent_artifacts a ON a.workflow_id=w.workflow_id AND a.artifact_type='search_strategy'
                WHERE g.context_type='job' AND g.context_id=?
                ORDER BY a.id DESC LIMIT 1
                """,
                (int(job_id),),
            ).fetchone()
            if strategy is None:
                raise ValueError(f"岗位 {job_id} 还没有寻访策略（strategy_v2），无法发起 Mapping 直挖")
            strategy_doc = strategy_v2.extract_strategy_v2(strategy["metadata_json"])
            if strategy_doc is None:
                raise ValueError(f"岗位 {job_id} 的策略 artifact 不是 strategy_v2 格式，无法发起 Mapping 直挖")

            archetype = None
            archetypes, _load_trace = strategy_v2.load_job_archetypes()
            archetype_id = str(strategy_doc.get("archetype_id") or "")
            for item in archetypes:
                if str(item.get("archetype_id") or "") == archetype_id:
                    archetype = item
                    break
            graph, _graph_trace = knowledge_base.load_company_graph()

            doc = mapping_task.build_mapping_task(
                job_id=int(job_id),
                trigger=trigger,
                strategy_ref=str(strategy["artifact_id"]),
                strategy_doc=strategy_doc,
                client=str(job["client"] or ""),
                job_title=str(job["title"] or ""),
                graph=graph,
                archetype=archetype,
                collector=collector,
            )
            doc["workflow_id"] = str(strategy["workflow_id"])
            doc["goal_id"] = str(strategy["goal_id"])
            artifact_id = mapping_task.upsert_mapping_task(conn, doc)
            stats = doc.get("stats") or {}
            summary = (
                f"发起 Mapping 直挖：目标团队 {stats.get('teams', 0)} 个、候选目标人 {stats.get('candidates', 0)} 位"
                f"（禁挖过滤 {stats.get('banned_filtered', 0)}、无来源拒收 {stats.get('rejected_no_source', 0)}、"
                f"采集失败 {stats.get('failures_count', 0)} 次已留痕）。名单仅供顾问本人决策，系统不自动触达。"
            )
            conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                VALUES (NULL,NULL,?,'mapping_task_created','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
                """,
                (
                    int(job_id),
                    summary,
                    json.dumps(
                        {
                            "artifact_id": artifact_id,
                            "workflow_id": doc["workflow_id"],
                            "trigger": trigger,
                            "teams": stats.get("teams", 0),
                            "candidates": stats.get("candidates", 0),
                        },
                        ensure_ascii=False,
                    ),
                    artifact_id,
                ),
            )
            conn.commit()
            return {
                "ok": True,
                "job_id": int(job_id),
                "workflow_id": doc["workflow_id"],
                "artifact_id": artifact_id,
                "mapping_task": doc,
            }
        finally:
            conn.close()

    def get_mapping_task(self, job_id: int, artifact_id: str) -> dict[str, Any]:
        """S5-1：读取岗位的 mapping_task；岗位/artifact 不存在或不属于该岗位抛 LookupError（404）。"""
        from . import mapping_task

        conn = self._connect()
        try:
            job = conn.execute("SELECT id FROM jobs WHERE id=?", (int(job_id),)).fetchone()
            if job is None:
                raise LookupError(f"岗位不存在：{job_id}")
            payload = mapping_task.get_mapping_task(conn, artifact_id)
            if payload is None:
                raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
            owner = conn.execute(
                """
                SELECT 1 FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                WHERE w.workflow_id=? AND g.context_type='job' AND g.context_id=?
                """,
                (payload["workflow_id"], int(job_id)),
            ).fetchone()
            if owner is None:
                raise LookupError(f"Mapping 任务卡不属于岗位 {job_id}：{artifact_id}")
            return {"ok": True, "job_id": int(job_id), **payload}
        finally:
            conn.close()

    def update_mapping_candidate(
        self,
        artifact_id: str,
        index: int,
        *,
        status: str | None = None,
        consultant_note: str | None = None,
    ) -> dict[str, Any]:
        """S5-2：任务卡候选状态机 PATCH（状态/备注）。confirmed 迁移自动生成破冰素材。

        artifact/index 不存在抛 LookupError（404）；未知态/非法迁移/终态变更/直接置 intaken
        抛 ValueError（409）。artifact version 不 bump，content 同步重生成。
        """
        from . import mapping_task

        conn = self._connect()
        try:
            result = mapping_task.apply_candidate_update(
                conn, artifact_id, int(index), status=status, consultant_note=consultant_note
            )
            conn.commit()
            return result
        finally:
            conn.close()

    def regenerate_mapping_icebreaker(self, artifact_id: str, index: int) -> dict[str, Any]:
        """S5-2：重新生成破冰素材（仅已确认及之后状态）。质量不合格抛 ValueError（409）不写入。"""
        from . import mapping_task

        conn = self._connect()
        try:
            result = mapping_task.regenerate_icebreaker(conn, artifact_id, int(index))
            conn.commit()
            return result
        finally:
            conn.close()

    def intake_mapping_candidate(self, artifact_id: str, index: int) -> dict[str, Any]:
        """S5-2：Mapping 候选入库（仅 confirmed）。复用现有 intake 写入口径，同一事务；
        不写第二条 job_candidates；禁挖/无来源/已停止关系抛 ValueError（409）。
        """
        from . import mapping_task

        conn = self._connect()
        try:
            result = mapping_task.intake_candidate(conn, artifact_id, int(index))
            conn.commit()
            return result
        finally:
            conn.close()

    def backflow_mapping_task(self, artifact_id: str, *, kb_dir: str | None = None, as_of: str = "") -> dict[str, Any]:
        """S5-3：知识回流——把任务卡已确认团队数据写入公司图谱 teams 扩展层（知识库维护流程）。

        只在显式触发时执行（运行时 Core 不自动写图谱）；图谱文件原子重写，
        除 teams/teams_external 相关键外原文件逐字节保留；同 artifact 幂等（更新 as_of 不重复条目）。
        artifact 不存在抛 LookupError（404）；无已确认团队/全部禁挖/图谱缺失或结构异常抛 ValueError（409）。
        """
        from . import graph_teams_backflow, knowledge_base, mapping_task
        from .strategy_v2 import knowledge_base_dir

        conn = self._connect()
        try:
            payload = mapping_task.get_mapping_task(conn, artifact_id)
            if payload is None:
                raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
            doc = payload["mapping_task"]
            client = str(doc.get("client") or "")
            restricted, _trace = knowledge_base.load_restricted_constraints(client, kb_dir=kb_dir)
            constraints = (restricted or {}).get("constraints") if isinstance(restricted, dict) else {}
            banned = [
                str(item) for item in (constraints or {}).get("banned_companies") or [] if str(item or "").strip()
            ]
            graph_path = (Path(kb_dir) if kb_dir else knowledge_base_dir()) / knowledge_base.COMPANY_GRAPH_FILE
            summary = graph_teams_backflow.backflow_teams(
                graph_path, doc, artifact_id=str(artifact_id), as_of=as_of, banned=banned
            )
            job_id = int(doc.get("job_id") or 0)
            if job_id:
                conn.execute(
                    """
                    INSERT INTO candidate_events
                    (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                    VALUES (NULL,NULL,?,'mapping_task_backflow','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
                    """,
                    (
                        job_id,
                        f"Mapping 团队数据回流图谱：写入公司 {summary['companies_written']} 家、"
                        f"团队 {summary['teams_written']} 个（as_of {summary['as_of']}；"
                        f"禁挖跳过 {summary['skipped_banned']}，teams_external {summary['external_companies_written']} 家）。",
                        json.dumps(
                            {
                                "artifact_id": str(artifact_id),
                                "as_of": summary["as_of"],
                                "companies_written": summary["companies_written"],
                                "teams_written": summary["teams_written"],
                                "skipped_banned": summary["skipped_banned"],
                                "changed": summary["changed"],
                            },
                            ensure_ascii=False,
                        ),
                        str(artifact_id),
                    ),
                )
                conn.commit()
            return {"ok": True, **summary}
        finally:
            conn.close()

    def mapping_metrics(self) -> dict[str, Any]:
        """S5-3：Mapping 评测指标聚合（PRD §8 四项口径，只读；数据不足的分组如实 null）。"""
        from . import mapping_metrics

        conn = self._connect()
        try:
            metrics = mapping_metrics.compute_mapping_metrics(conn)
            return {
                "ok": True,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "metrics": metrics,
            }
        finally:
            conn.close()

    def start_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.workflow_engine.start_workflow(workflow_id)

    def revise_workflow(self, workflow_id: str, instruction: str) -> dict[str, Any]:
        return self.workflow_engine.revise_workflow(workflow_id, instruction)

    def cancel_workflow(self, workflow_id: str, note: str = "") -> dict[str, Any]:
        return self.workflow_engine.cancel_workflow(workflow_id, note)

    def archive_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.workflow_engine.archive_workflow(workflow_id)

    def retry_workflow_step(self, step_id: int) -> dict[str, Any]:
        return self.workflow_engine.retry_step(step_id)

    def complete_external_workflow_step(self, step_id: int, result: dict[str, Any]) -> dict[str, Any]:
        return self.workflow_engine.complete_external_step(step_id, result)

    def schedule_external_workflow_step(self, step_id: int, capability_id: str, request: dict[str, Any]) -> None:
        self.executor.submit(self._execute_external_workflow_step, int(step_id), capability_id, dict(request))

    def _execute_external_workflow_step(self, step_id: int, capability_id: str, request: dict[str, Any]) -> None:
        try:
            result = self.capability_runtime.execute_external(capability_id, request)
            self.workflow_engine.complete_external_step(step_id, result)
        except Exception as exc:
            self.workflow_engine.fail_external_step(step_id, str(exc))

    @staticmethod
    def validate_external_result(capability_id: str, result: dict[str, Any]) -> None:
        if result.get("verified") is not True:
            raise ValueError("渠道结果必须包含 verified=true")
        if capability_id == "multi_channel_sourcing":
            if not isinstance(result.get("channel_runs"), list) or not result["channel_runs"]:
                raise ValueError("寻访结果缺少 channel_runs")
            if not isinstance(result.get("intake"), dict) or not result["intake"]:
                raise ValueError("寻访结果缺少排重入库统计")
            if not isinstance(result.get("audit"), dict) or result["audit"].get("ok") is not True:
                raise ValueError("寻访结果未通过 A 系统同步审计")
            # additive 质量标记：ok 但 0 候选的渠道结果区分“已归因/未归因”，不改变 ok/status 流转。
            # 通过就地修改 result 让 complete_external_step 落库的 output_json 带上标记。
            for run in result["channel_runs"]:
                if not isinstance(run, dict):
                    continue
                channel_result = run.get("result") if isinstance(run.get("result"), dict) else {}
                if channel_result.get("ok") is not True:
                    continue
                try:
                    produced = int(channel_result.get("candidates") or 0)
                except (TypeError, ValueError):
                    produced = 0
                if produced > 0:
                    continue
                attribution = str(run.get("zero_attribution") or "").strip()
                run["quality"] = "zero_attributed" if attribution and attribution != "unknown" else "zero_unknown"
        if capability_id == "client_recommendation":
            required = ("channel", "status", "document_hash", "sent_at")
            if any(not result.get(key) for key in required):
                raise ValueError("客户推荐回执缺少渠道、状态、文档哈希或发送时间")
        if capability_id == "job_publish_execute":
            if result.get("status") not in {"published", "submitted", "auditing"} and result.get("published") is not True:
                raise ValueError("岗位发布回执缺少已发布、已提交或审核中状态")
        if capability_id == "outreach_execute":
            if not isinstance(result.get("items"), list) or not result["items"]:
                raise ValueError("触达回执缺少逐项结果")
            if not any(item.get("status") in {"sent_verified", "skipped"} for item in result["items"]):
                raise ValueError("触达回执没有任何已验证或幂等跳过项")

    def apply_external_result(self, capability_id: str, context: dict[str, Any], result: dict[str, Any], workflow_id: str) -> None:
        if capability_id != "client_recommendation" or context.get("type") != "candidate":
            return
        candidate = build_candidate_context(self.db_path, int(context["id"]))
        if is_stopped(candidate):
            raise ValueError("已停止关系不能写入客户推荐结果")
        event_id = self.capability_runtime._candidate_event(
            candidate, "candidate_recommended", "sent_verified", "客户推荐已通过渠道回执验证",
            {"workflow_id": workflow_id, "channel_result": result},
        )
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE job_candidates SET raw_status='recommended',clean_stage='已推荐客户',flow_bucket='客户推荐',updated_at=datetime('now','localtime') WHERE id=?",
                (int(context["id"]),),
            )
            conn.commit()
        finally:
            conn.close()
        self.record_sourcing_business_signal(
            int(context["id"]), "recommended", actor_type="user",
            note="客户推荐已通过渠道回执验证", source_type="candidate_event", source_id=event_id,
        )

    def decide_workflow_approval(self, approval_id: str, decision: str, note: str = "") -> dict[str, Any]:
        return self.workflow_engine.decide_approval(approval_id, decision, note)

    def get_workflow_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self.workflow_engine.get_artifact(artifact_id)

    def get_workflow_events(self, event_id: int = 0, workflow_id: str = "", limit: int = 100) -> dict[str, Any]:
        return self.workflow_engine.events_since(event_id, workflow_id, limit)

    def get_workflow_quality(self) -> dict[str, Any]:
        return self.workflow_engine.quality_metrics()

    def record_workflow_feedback(self, workflow_id: str, feedback_type: str, note: str = "", correction: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.workflow_engine.record_feedback(workflow_id, feedback_type, note, correction or {})

    def execute_skill(
        self, skill_id: str, *, context: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        selected = self._normalize_copilot_context(context or {})
        registered = self.skills.get(skill_id)
        if registered is None:
            raise ValueError(f"未注册或未启用的 Skill：{skill_id}")
        if registered.risk_level not in {"R0", "R1"}:
            raise ValueError(f"Skill {skill_id} 风险等级 {registered.risk_level} 必须走人工预检")
        started = time.time()
        conn = self._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO agent_skill_runs
                (skill_id,skill_version,context_type,context_id,risk_level,input_json,status)
                VALUES (?,?,?,?,?,?,'running')
                """,
                (str(skill_id), "pending", selected["type"], selected.get("id"), "R4", _dumps(inputs or {})),
            )
            audit_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()
        try:
            executed = self.skills.execute(str(skill_id), selected, inputs or {})
            spec = executed["skill"]
            result = executed["result"]
            conn = self._connect()
            try:
                conn.execute(
                    """
                    UPDATE agent_skill_runs SET skill_version=?,risk_level=?,output_json=?,status='completed',
                        finished_at=datetime('now','localtime') WHERE id=?
                    """,
                    (spec["version"], spec["risk_level"], _dumps(result), audit_id),
                )
                conn.commit()
            finally:
                conn.close()
            return {
                "ok": True, "audit_id": audit_id, "duration_ms": int((time.time() - started) * 1000),
                **executed,
            }
        except Exception as exc:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE agent_skill_runs SET status='failed',error=?,finished_at=datetime('now','localtime') WHERE id=?",
                    (str(exc)[:1000], audit_id),
                )
                conn.commit()
            finally:
                conn.close()
            raise

    def _skill_job_diagnosis(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        dashboard = self.get_dashboard()
        jobs = dashboard.get("p0_jobs") or []
        if context.get("type") == "job" and context.get("id"):
            jobs = [item for item in jobs if int(item.get("job_id") or 0) == int(context["id"])]
        return {
            "diagnosis": {"jobs": jobs, "funnel": (dashboard.get("analytics") or {}).get("funnel", {})},
            "references": [
                {"type": "job", "id": item.get("job_id"), "label": item.get("job"), "subtitle": item.get("client")}
                for item in jobs[:8]
            ],
            "suggested_actions": [],
        }

    def _skill_candidate_assessment(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job_candidate_id = int(context.get("id") or 0)
        if not job_candidate_id:
            raise ValueError("人选评估 Skill 需要人岗关系 ID")
        state = self.get_candidate_state(job_candidate_id)
        run = None
        if not state.get("assessment"):
            run = self.submit_assessment(
                job_candidate_id,
                trigger="workflow" if inputs.get("workflow_id") else "skill",
                wait=bool(inputs.get("workflow_id")),
            )
            if inputs.get("workflow_id"):
                state = self.get_candidate_state(job_candidate_id)
        return {
            "assessment": state.get("assessment"),
            "run": run,
            "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
            "suggested_actions": [],
        }

    def _skill_verification_plan(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job_candidate_id = int(context.get("id") or 0)
        state = self.get_candidate_state(job_candidate_id)
        assessment = state.get("assessment") or {}
        if not assessment:
            raise ValueError("请先完成人选评估")
        proposals = self.generate_proposals([job_candidate_id], limit=1)
        return {
            "questions": assessment.get("verification_questions") or [],
            "next_action": assessment.get("next_action") or "人工核验关键证据",
            "proposals": proposals.get("proposals") or [],
            "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
            "suggested_actions": [{"type": "open_candidate", "id": job_candidate_id, "label": "开始核验"}],
        }

    def _skill_communication_draft(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job_candidate_id = int(context.get("id") or 0)
        result = self.create_draft(job_candidate_id, str(inputs.get("instructions") or ""))
        return {
            "draft": result.get("draft") or "",
            "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
            "suggested_actions": [],
            "not_sent": True,
        }

    def _skill_liepin_resume_capture(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job_candidate_id = int(context.get("id") or 0)
        result = self.capture_liepin_resume(job_candidate_id, cdp_port=int(inputs.get("cdp_port") or 9223))
        return {
            **result,
            "references": [
                {
                    "type": "candidate", "id": job_candidate_id,
                    "label": result["resume"].get("name") or f"关系 #{job_candidate_id}",
                    "subtitle": "猎聘完整简历已补充",
                }
            ],
            "suggested_actions": [{"type": "open_candidate", "id": job_candidate_id, "label": "查看更新后的判断"}],
        }

    def capture_liepin_resume(self, job_candidate_id: int, *, cdp_port: int = 9223) -> dict[str, Any]:
        if not int(job_candidate_id or 0):
            raise ValueError("从猎聘补全简历需要人岗关系 ID")
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        identity = context.get("identity") or {}
        resumes = capture_open_liepin_resumes(int(cdp_port))
        matches = [resume for resume in resumes if resume_matches_identity(identity, resume)]
        if not matches:
            visible = "、".join(
                f"{resume.get('name') or '未识别'} / {resume.get('company') or '未识别公司'}"
                for resume in resumes[:5]
            )
            raise ValueError(
                f"已打开的猎聘简历与当前人选不匹配。当前人选：{identity.get('name') or '未识别'}；"
                f"猎聘页面：{visible or '无可读简历'}"
            )
        if len(matches) != 1:
            raise ValueError("有多个猎聘简历页面同时匹配当前人选，请只保留需要补全的详情页")
        resume = matches[0]
        relation = context["relation"]
        position = context["position"]
        profile_payload = {
            **resume,
            "profile_text": "\n".join(
                str(resume.get(key) or "")
                for key in ("work_text", "project_text", "education_text", "full_text")
                if resume.get(key)
            )[:60000],
            "capture_method": "asa_liepin_cdp_read_only",
            "job_candidate_id": int(job_candidate_id),
        }
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
        profile_summary = "\n".join(profile_parts).strip()[:60000]
        conn = self._connect()
        try:
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
            candidate_id = (context.get("candidate") or {}).get("id")
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
                "capture_method": "asa_liepin_cdp_read_only",
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
            conn.commit()
        finally:
            conn.close()
        assessment = self.submit_assessment(
            int(job_candidate_id), force=True, trigger="liepin_resume_capture"
        )
        return {
            "ok": True,
            "message": "猎聘完整简历已写入 ASA，正在重新评估当前人选。",
            "resume": {
                "resume_id": resume["resume_id"], "name": resume.get("name"),
                "company": resume.get("company"), "title": resume.get("title"),
                "work_chars": len(str(resume.get("work_text") or "")),
                "project_chars": len(str(resume.get("project_text") or "")),
                "education_chars": len(str(resume.get("education_text") or "")),
            },
            "source_profile_id": source_profile_id, "event_id": event_id,
            "updated": updated, "assessment": assessment,
            "candidate_update": {
                "candidate_id": candidate_id,
                "job_candidate_id": int(job_candidate_id),
                "name": resume.get("name") or identity.get("name"),
                "company": resume.get("company") or identity.get("company"),
                "title": resume.get("title") or identity.get("title"),
                "city": resume.get("city") or identity.get("city"),
                "education": resume.get("education") or identity.get("education"),
                "experience": resume.get("experience") or identity.get("experience"),
                "profile_summary": profile_summary,
                "source_url": resume.get("source_url"),
                "event": {
                    "id": event_id, "jobCandidateId": int(job_candidate_id),
                    "eventType": "resume_profile_captured", "eventStatus": "completed",
                    "eventTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "summary": summary, "client": position.get("client"), "job": position.get("job"),
                },
            },
        }

    def _snapshot_key(self, context: dict[str, Any]) -> str:
        raw = "|".join(
            [context["snapshot_hash"], PROMPT_VERSION, ASSESSMENT_VERSION, self.llm.model]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def stage_shadow_decision(self, assessment: dict[str, Any]) -> dict[str, Any]:
        hard_requirements = (assessment.get("criteria") or {}).get("hard_requirements") or []
        critical_not_met = any(
            item.get("status") == "not_met" and bool(item.get("critical", True))
            for item in hard_requirements
        )
        score = int(assessment.get("fit_score") or 0)
        confidence = float(assessment.get("confidence") or 0)
        coverage = float(assessment.get("evidence_coverage") or 0)
        recommendation = str(assessment.get("recommendation") or "")
        thresholds = self.config["automation"]
        high_score = int(thresholds["high_score"])
        low_score = int(thresholds["low_score"])
        min_confidence = float(thresholds["min_confidence"])
        min_coverage = float(thresholds["min_evidence_coverage"])
        if critical_not_met:
            proposed_stage = "待人工复核"
            rule_code = "critical_gate_not_met"
            reason = "存在关键硬门槛不满足，不自动淘汰，转人工复核。"
        elif score < low_score:
            proposed_stage = "待人工复核"
            rule_code = "low_score_review"
            reason = f"匹配评分 {score} 低于 {low_score}，转人工复核。"
        elif recommendation == "verify_first" or coverage < min_coverage:
            proposed_stage = "待核验"
            rule_code = "evidence_verification"
            reason = f"建议先核验或证据覆盖率 {coverage:.0%} 低于 {min_coverage:.0%}。"
        elif score >= high_score and confidence >= min_confidence and coverage >= min_coverage:
            proposed_stage = "复核通过待联系"
            rule_code = "high_confidence_pass"
            reason = f"评分 {score}、置信度 {confidence:.0%}、证据覆盖 {coverage:.0%} 达到内部推进阈值。"
        else:
            proposed_stage = "待人工复核"
            rule_code = "threshold_review"
            reason = "未同时满足自动推进阈值，保留人工复核。"
        return {
            "mode": "shadow",
            "executed": False,
            "action_type": "internal_stage_recommendation",
            "proposed_stage": proposed_stage,
            "rule_code": rule_code,
            "reason": reason,
        }

    def submit_assessment(
        self,
        job_candidate_id: int,
        *,
        force: bool = False,
        trigger: str = "manual",
        wait: bool = False,
        timeout: float = 90,
    ) -> dict[str, Any]:
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        snapshot_hash = self._snapshot_key(context)
        conn = self._connect()
        try:
            if not force:
                cached = conn.execute(
                    """
                    SELECT a.*,r.status AS run_status,r.model,r.prompt_version
                    FROM agent_candidate_assessments a
                    JOIN agent_runs r ON r.run_id=a.run_id
                    WHERE a.job_candidate_id=? AND a.snapshot_hash=? AND a.is_current=1
                      AND r.status='completed'
                    ORDER BY a.id DESC LIMIT 1
                    """,
                    (int(job_candidate_id), snapshot_hash),
                ).fetchone()
                if cached:
                    return {
                        "ok": True,
                        "cached": True,
                        "run_id": cached["run_id"],
                        "status": "completed",
                        "assessment": self._assessment_payload(cached),
                    }
        finally:
            conn.close()
        key = (int(job_candidate_id), snapshot_hash)
        with self._lock:
            active_run_id = self._active_by_snapshot.get(key)
            if active_run_id:
                payload = self.get_run(active_run_id)
                payload["coalesced"] = True
                return payload
            run_id = f"agent_{int(time.time())}_{secrets.token_hex(6)}"
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO agent_runs
                    (run_id,kind,context_type,context_id,snapshot_hash,status,trigger,model,prompt_version)
                    VALUES (?,?,?,?,?,'queued',?,?,?)
                    """,
                    (
                        run_id,
                        "candidate_assessment",
                        "job_candidate",
                        int(job_candidate_id),
                        snapshot_hash,
                        trigger,
                        self.llm.model,
                        PROMPT_VERSION,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            self._active_by_snapshot[key] = run_id
        if wait:
            self._run_assessment(run_id, context, snapshot_hash, key)
            return self.get_run(run_id)
        future = self.executor.submit(self._run_assessment, run_id, context, snapshot_hash, key)
        self._futures[run_id] = future
        return {"ok": True, "cached": False, "run_id": run_id, "status": "queued"}

    def _run_assessment(
        self,
        run_id: str,
        context: dict[str, Any],
        snapshot_hash: str,
        key: tuple[int, str],
    ) -> None:
        conn = self._connect()
        try:
            assessment_cursor = conn.execute(
                """
                UPDATE agent_runs SET status='running',started_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime') WHERE run_id=?
                """,
                (run_id,),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            raw_assessment = self.llm.assess(context["model_context"])
            assessment = normalize_assessment(raw_assessment, context)
            reviewer: dict[str, Any] = {}
            reviewer_used = 0
            if assessment["needs_review"]:
                reviewer_used = 1
                model_context = context["model_context"]
                review_context = {
                    "identity": model_context.get("identity", {}),
                    "candidate_profile": model_context.get("candidate_profile", {}),
                    "position": model_context.get("position", {}),
                    "events": (model_context.get("events") or [])[:12],
                    "learning_rules": model_context.get("learning_rules", []),
                }
                reviewer = self.llm.review(review_context, raw_assessment)
                if reviewer.get("decision") == "correct" and isinstance(reviewer.get("assessment"), dict):
                    assessment = normalize_assessment(reviewer["assessment"], context)
                elif reviewer.get("decision") == "abstain":
                    assessment["recommendation"] = "verify_first"
                    assessment["confidence"] = min(float(assessment["confidence"]), 0.49)
                    assessment["risks"] = list(dict.fromkeys(["审校器无法确认当前判断", *assessment["risks"]]))
            latest_context = build_candidate_context(self.db_path, int(context["relation"]["job_candidate_id"]))
            latest_snapshot = self._snapshot_key(latest_context)
            if latest_snapshot != snapshot_hash:
                self._finish_run(
                    run_id,
                    "stale",
                    error="评估期间候选人或岗位证据已变化，旧结果未晋升为当前判断",
                    reviewer_used=reviewer_used,
                )
                return
            self._persist_assessment(run_id, context, snapshot_hash, assessment, reviewer)
            self._finish_run(run_id, "completed", reviewer_used=reviewer_used)
        except Exception as exc:
            self._finish_run(run_id, "failed", error=str(exc)[:1000])
        finally:
            with self._lock:
                self._active_by_snapshot.pop(key, None)
                self._futures.pop(run_id, None)

    def submit_panel_review(
        self,
        job_candidate_id: int,
        *,
        force: bool = False,
        use_model: bool = True,
        trigger: str = "manual_panel",
        wait: bool = False,
        timeout: float = 120,
    ) -> dict[str, Any]:
        job_candidate_id = int(job_candidate_id)
        context = build_candidate_context(self.db_path, job_candidate_id)
        evidence_snapshot = self._snapshot_key(context)
        conn = self._connect()
        try:
            assessment_row = conn.execute(
                """
                SELECT * FROM agent_candidate_assessments
                WHERE job_candidate_id=? AND is_current=1
                ORDER BY id DESC LIMIT 1
                """,
                (job_candidate_id,),
            ).fetchone()
            if assessment_row is None:
                raise ValueError("请先完成当前人选的 Agent 评估")
            if assessment_row["snapshot_hash"] != evidence_snapshot:
                raise ValueError("当前判断已过期，请先重新评估")
            assessment = self._assessment_payload(assessment_row)
            if not force:
                cached = conn.execute(
                    """
                    SELECT * FROM agent_review_panels
                    WHERE job_candidate_id=? AND assessment_id=? AND snapshot_hash=?
                      AND status='completed' AND is_current=1
                    ORDER BY id DESC LIMIT 1
                    """,
                    (job_candidate_id, assessment_row["id"], evidence_snapshot),
                ).fetchone()
                if cached:
                    return {
                        "ok": True,
                        "cached": True,
                        "run_id": cached["run_id"],
                        "status": "completed",
                        "panel": self._panel_payload(conn, cached),
                    }
        finally:
            conn.close()

        panel_key_raw = "|".join(
            [evidence_snapshot, str(assessment["id"]), PANEL_VERSION, self.llm.model, str(bool(use_model))]
        )
        panel_snapshot = hashlib.sha256(panel_key_raw.encode("utf-8")).hexdigest()
        active_key = (job_candidate_id, panel_snapshot)
        with self._lock:
            active_run_id = self._active_panel_by_snapshot.get(active_key)
            if active_run_id:
                payload = self.get_run(active_run_id)
                payload["coalesced"] = True
                return payload
            run_id = f"panel_{int(time.time())}_{secrets.token_hex(6)}"
            panel_id = f"review_{int(time.time())}_{secrets.token_hex(6)}"
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO agent_runs
                    (run_id,kind,context_type,context_id,snapshot_hash,status,trigger,model,prompt_version)
                    VALUES (?,'candidate_panel_review','job_candidate',?,?,'queued',?,?,?)
                    """,
                    (
                        run_id,
                        job_candidate_id,
                        panel_snapshot,
                        trigger,
                        self.llm.model,
                        PANEL_VERSION,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_review_panels
                    (panel_id,run_id,job_candidate_id,assessment_id,snapshot_hash,status,model_mode,is_current)
                    VALUES (?,?,?,?,?,'queued',?,0)
                    """,
                    (
                        panel_id,
                        run_id,
                        job_candidate_id,
                        assessment["id"],
                        evidence_snapshot,
                        "hybrid" if use_model else "rules",
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            self._active_panel_by_snapshot[active_key] = run_id
            future = self.executor.submit(
                self._run_panel_review,
                run_id,
                panel_id,
                context,
                assessment,
                evidence_snapshot,
                active_key,
                bool(use_model),
            )
            self._futures[run_id] = future
        if wait:
            future.result(timeout=timeout)
            return self.get_run(run_id)
        return {
            "ok": True,
            "cached": False,
            "run_id": run_id,
            "panel_id": panel_id,
            "status": "queued",
        }

    def _run_panel_review(
        self,
        run_id: str,
        panel_id: str,
        context: dict[str, Any],
        assessment: dict[str, Any],
        evidence_snapshot: str,
        active_key: tuple[int, str],
        use_model: bool,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE agent_runs SET status='running',started_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE run_id=?",
                (run_id,),
            )
            conn.execute(
                "UPDATE agent_review_panels SET status='running' WHERE panel_id=?",
                (panel_id,),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            reviews: list[dict[str, Any]] = []
            model_enabled = bool(use_model)
            model_error = ""
            for role in ROLE_DEFINITIONS:
                source = "rules"
                error = model_error
                review = fallback_role_review(context, assessment, role)
                if model_enabled:
                    try:
                        raw = self.llm.role_review(role, role_payload(context, assessment, role))
                        review = normalize_role_review(raw, role)
                        source = "model"
                        error = ""
                    except Exception as exc:
                        model_error = str(exc)[:1000]
                        error = model_error
                        model_enabled = False
                review.update(
                    {
                        "source": source,
                        "model": self.llm.model if source == "model" else "rules-v2",
                        "error": error,
                    }
                )
                reviews.append(review)

            job_candidate_id = int(context["relation"]["job_candidate_id"])
            latest_context = build_candidate_context(self.db_path, job_candidate_id)
            latest_snapshot = self._snapshot_key(latest_context)
            conn = self._connect()
            try:
                current_assessment = conn.execute(
                    """
                    SELECT id,snapshot_hash FROM agent_candidate_assessments
                    WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1
                    """,
                    (job_candidate_id,),
                ).fetchone()
            finally:
                conn.close()
            if (
                latest_snapshot != evidence_snapshot
                or current_assessment is None
                or int(current_assessment["id"]) != int(assessment["id"])
            ):
                conn = self._connect()
                try:
                    conn.execute(
                        "UPDATE agent_review_panels SET status='stale',completed_at=datetime('now','localtime') WHERE panel_id=?",
                        (panel_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self._finish_run(
                    run_id,
                    "stale",
                    error="会审期间候选人、岗位或首轮判断已变化",
                )
                return

            synthesis = synthesize_panel(reviews, assessment, stopped=is_stopped(latest_context))
            source_set = {review["source"] for review in reviews}
            model_mode = "model" if source_set == {"model"} else "rules" if source_set == {"rules"} else "hybrid"
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE agent_review_panels SET is_current=0 WHERE job_candidate_id=? AND is_current=1",
                    (job_candidate_id,),
                )
                conn.execute(
                    """
                    UPDATE agent_review_panels
                    SET status='completed',model_mode=?,synthesis_json=?,is_current=1,
                        completed_at=datetime('now','localtime')
                    WHERE panel_id=?
                    """,
                    (model_mode, _dumps(synthesis), panel_id),
                )
                for review in reviews:
                    conn.execute(
                        """
                        INSERT INTO agent_role_reviews
                        (panel_id,role,verdict,confidence,findings_json,questions_json,
                         recommendation,source,model,error)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            panel_id,
                            review["role"],
                            review["verdict"],
                            review["confidence"],
                            _dumps(review["findings"]),
                            _dumps(review["questions"]),
                            review["recommendation"],
                            review["source"],
                            review["model"],
                            review["error"],
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            self._finish_run(run_id, "completed", reviewer_used=1)
        except Exception as exc:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE agent_review_panels SET status='failed',completed_at=datetime('now','localtime') WHERE panel_id=?",
                    (panel_id,),
                )
                conn.commit()
            finally:
                conn.close()
            self._finish_run(run_id, "failed", error=str(exc)[:1000])
        finally:
            with self._lock:
                self._active_panel_by_snapshot.pop(active_key, None)
                self._futures.pop(run_id, None)

    def _finish_run(
        self,
        run_id: str,
        status: str,
        *,
        error: str = "",
        reviewer_used: int = 0,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_runs SET status=?,error=?,reviewer_used=?,
                    finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                WHERE run_id=?
                """,
                (status, error, reviewer_used, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _persist_assessment(
        self,
        run_id: str,
        context: dict[str, Any],
        snapshot_hash: str,
        assessment: dict[str, Any],
        reviewer: dict[str, Any],
    ) -> None:
        relation = context["relation"]
        identity = context["identity"]
        position = context["position"]
        candidate = context.get("candidate", {})
        policy = {
            "stopped": is_stopped(context),
            "save_assessment": action_decision("save_assessment", context),
            "resume_review": action_decision("resume_review", context),
            "outreach": action_decision("outreach", context),
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE agent_candidate_assessments SET is_current=0 WHERE job_candidate_id=? AND is_current=1",
                (relation["job_candidate_id"],),
            )
            assessment_cursor = conn.execute(
                """
                INSERT INTO agent_candidate_assessments
                (run_id,job_candidate_id,candidate_id,person_id,job_id,client,job,snapshot_hash,
                 assessment_version,fit_score,fit_level,recommendation,confidence,evidence_coverage,
                 criteria_json,strengths_json,gaps_json,risks_json,verification_questions_json,
                 next_action,outreach_angle,citations_json,policy_json,reviewer_json,is_current)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    run_id,
                    relation["job_candidate_id"],
                    candidate.get("id"),
                    relation["person_id"],
                    relation.get("job_id"),
                    position.get("client"),
                    position.get("job"),
                    snapshot_hash,
                    ASSESSMENT_VERSION,
                    assessment["fit_score"],
                    assessment["fit_level"],
                    assessment["recommendation"],
                    assessment["confidence"],
                    assessment["evidence_coverage"],
                    _dumps(assessment["criteria"]),
                    _dumps(assessment["strengths"]),
                    _dumps(assessment["gaps"]),
                    _dumps(assessment["risks"]),
                    _dumps(assessment["verification_questions"]),
                    assessment["next_action"],
                    assessment["outreach_angle"],
                    _dumps(assessment["citations"]),
                    _dumps(policy),
                    _dumps(reviewer),
                ),
            )
            shadow = self.stage_shadow_decision(assessment)
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_stage_recommendations
                (job_candidate_id,assessment_id,snapshot_hash,current_stage,proposed_stage,
                 rule_code,reason,mode,status,executed,action_type,undo_until,updated_at)
                VALUES (?,?,?,?,?,?,?,'shadow','pending',0,'internal_stage_recommendation',
                        datetime('now','+10 minutes','localtime'),datetime('now','localtime'))
                """,
                (
                    relation["job_candidate_id"],
                    int(assessment_cursor.lastrowid),
                    snapshot_hash,
                    relation.get("clean_stage") or relation.get("raw_stage") or "",
                    shadow["proposed_stage"],
                    shadow["rule_code"],
                    shadow["reason"],
                ),
            )
            if candidate.get("id") is not None:
                self._upsert_candidate_intelligence(conn, candidate, identity, position, assessment)
            rule_ids = [
                int(rule["id"])
                for rule in context.get("learning_rules", [])
                if str(rule.get("id") or "").isdigit()
            ]
            if rule_ids:
                placeholders = ",".join("?" for _ in rule_ids)
                conn.execute(
                    f"UPDATE agent_learning_rules SET last_used_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                    tuple(rule_ids),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _upsert_candidate_intelligence(
        self,
        conn: sqlite3.Connection,
        candidate: dict[str, Any],
        identity: dict[str, Any],
        position: dict[str, Any],
        assessment: dict[str, Any],
    ) -> None:
        evidence = {
            "criteria": assessment["criteria"],
            "citations": assessment["citations"],
            "confidence": assessment["confidence"],
            "evidence_coverage": assessment["evidence_coverage"],
            "source": "a_system_agent_v1",
        }
        row = conn.execute(
            """
            SELECT id FROM candidate_intelligence
            WHERE candidate_id=? AND client=? AND position=?
            ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1
            """,
            (candidate["id"], position.get("client"), position.get("job")),
        ).fetchone()
        values = (
            identity.get("name") or candidate.get("name"),
            identity.get("company") or candidate.get("company"),
            assessment["fit_score"],
            assessment["fit_level"],
            _dumps(evidence),
            _dumps(assessment["risks"]),
            assessment["next_action"],
            _dumps(assessment["strengths"]),
            _dumps(assessment["gaps"]),
            _dumps(assessment["verification_questions"]),
            DECISION_LABELS[assessment["recommendation"]],
        )
        if row:
            conn.execute(
                """
                UPDATE candidate_intelligence
                SET candidate_name=?,candidate_company=?,fit_score=?,fit_level=?,evidence_json=?,
                    risk_json=?,next_action=?,strong_matches_json=?,weak_matches_json=?,
                    verification_questions_json=?,recommendation_decision=?,
                    last_evaluated_at=datetime('now','localtime'),model_version=?,
                    updated_at=datetime('now','localtime')
                WHERE id=?
                """,
                (*values, f"{ASSESSMENT_VERSION}:{self.llm.model}", row["id"]),
            )
            return
        next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM candidate_intelligence").fetchone()[0]
        conn.execute(
            """
            INSERT INTO candidate_intelligence
            (id,candidate_id,candidate_name,candidate_company,client,position,fit_score,fit_level,
             evidence_json,risk_json,next_action,last_evaluated_at,model_version,created_at,updated_at,
             strong_matches_json,weak_matches_json,verification_questions_json,recommendation_decision)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?,datetime('now','localtime'),
                    datetime('now','localtime'),?,?,?,?)
            """,
            (
                next_id,
                candidate["id"],
                values[0],
                values[1],
                position.get("client"),
                position.get("job"),
                *values[2:7],
                f"{ASSESSMENT_VERSION}:{self.llm.model}",
                *values[7:],
            ),
        )

    def _assessment_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return {
            "id": item.get("id"),
            "run_id": item.get("run_id"),
            "job_candidate_id": item.get("job_candidate_id"),
            "fit_score": item.get("fit_score"),
            "fit_level": item.get("fit_level"),
            "recommendation": item.get("recommendation"),
            "recommendation_label": DECISION_LABELS.get(str(item.get("recommendation")), str(item.get("recommendation") or "")),
            "confidence": item.get("confidence"),
            "evidence_coverage": item.get("evidence_coverage"),
            "criteria": _loads(item.get("criteria_json"), {}),
            "strengths": _loads(item.get("strengths_json"), []),
            "gaps": _loads(item.get("gaps_json"), []),
            "risks": _loads(item.get("risks_json"), []),
            "verification_questions": _loads(item.get("verification_questions_json"), []),
            "next_action": item.get("next_action") or "",
            "outreach_angle": item.get("outreach_angle") or "",
            "citations": _loads(item.get("citations_json"), []),
            "policy": _loads(item.get("policy_json"), {}),
            "reviewer": _loads(item.get("reviewer_json"), {}),
            "created_at": item.get("created_at"),
        }

    def _panel_payload(
        self, conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
    ) -> dict[str, Any]:
        item = dict(row)
        role_rows = conn.execute(
            "SELECT * FROM agent_role_reviews WHERE panel_id=? ORDER BY id",
            (item.get("panel_id"),),
        ).fetchall()
        roles = []
        for role_row in role_rows:
            role = _row(role_row)
            role["role_label"] = ROLE_DEFINITIONS.get(str(role.get("role")), {}).get(
                "label", role.get("role") or "审校角色"
            )
            role["findings"] = _loads(role.pop("findings_json", "[]"), [])
            role["questions"] = _loads(role.pop("questions_json", "[]"), [])
            roles.append(role)
        return {
            "id": item.get("id"),
            "panel_id": item.get("panel_id"),
            "run_id": item.get("run_id"),
            "job_candidate_id": item.get("job_candidate_id"),
            "assessment_id": item.get("assessment_id"),
            "snapshot_hash": item.get("snapshot_hash"),
            "status": item.get("status"),
            "model_mode": item.get("model_mode"),
            "synthesis": _loads(item.get("synthesis_json"), {}),
            "roles": roles,
            "created_at": item.get("created_at"),
            "completed_at": item.get("completed_at"),
        }

    def get_panel_state(self, job_candidate_id: int) -> dict[str, Any]:
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        current_snapshot = self._snapshot_key(context)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM agent_review_panels
                WHERE job_candidate_id=?
                ORDER BY is_current DESC,id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            return {
                "ok": True,
                "job_candidate_id": int(job_candidate_id),
                "stale": not row or row["snapshot_hash"] != current_snapshot,
                "panel": self._panel_payload(conn, row) if row else None,
            }
        finally:
            conn.close()

    def get_run(self, run_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            run = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                return {"ok": False, "error": f"找不到 Agent 运行：{run_id}"}
            assessment = conn.execute(
                "SELECT * FROM agent_candidate_assessments WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            panel = conn.execute(
                "SELECT * FROM agent_review_panels WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            payload = _row(run)
            payload["ok"] = payload["status"] not in {"failed"}
            if assessment:
                payload["assessment"] = self._assessment_payload(assessment)
            if panel:
                payload["panel"] = self._panel_payload(conn, panel)
            return payload
        finally:
            conn.close()

    def get_candidate_state(self, job_candidate_id: int) -> dict[str, Any]:
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        snapshot_hash = self._snapshot_key(context)
        conn = self._connect()
        try:
            assessment = conn.execute(
                """
                SELECT * FROM agent_candidate_assessments
                WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            latest_run = conn.execute(
                """
                SELECT run_id,status,error,trigger,created_at,updated_at,
                       CAST(strftime('%s','now','localtime')-
                            strftime('%s',COALESCE(updated_at,created_at)) AS INTEGER) AS age_seconds
                FROM agent_runs
                WHERE kind='candidate_assessment' AND context_type='job_candidate' AND context_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            latest_run_payload = _row(latest_run) if latest_run else None
            recent_failure = bool(
                latest_run_payload
                and latest_run_payload.get("status") in {"failed", "interrupted", "stale"}
                and int(latest_run_payload.get("age_seconds") or 0) < 600
            )
            active_run = bool(
                latest_run_payload and latest_run_payload.get("status") in {"queued", "running"}
            )
            panel_row = conn.execute(
                """
                SELECT * FROM agent_review_panels
                WHERE job_candidate_id=?
                ORDER BY is_current DESC,id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            verification_task = None
            task_columns = _table_columns(conn, "followup_tasks")
            if {"id", "job_candidate_id", "task_type", "status"}.issubset(task_columns):
                task_row = conn.execute(
                    """
                    SELECT * FROM followup_tasks
                    WHERE job_candidate_id=? AND task_type='agent_verification'
                      AND COALESCE(status,'open')='open'
                    ORDER BY COALESCE(priority,2) ASC,id DESC LIMIT 1
                    """,
                    (int(job_candidate_id),),
                ).fetchone()
                if task_row:
                    verification_task = _row(task_row)
                    verification_task["questions"] = (
                        self._assessment_payload(assessment).get("verification_questions", [])
                        if assessment
                        else []
                    )
            latest_verification = None
            if _table_exists(conn, "candidate_events"):
                verification_row = conn.execute(
                    """
                    SELECT id,event_status,event_time,summary,raw_json
                    FROM candidate_events
                    WHERE job_candidate_id=? AND event_type='agent_verification_completed'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (int(job_candidate_id),),
                ).fetchone()
                if verification_row:
                    latest_verification = _row(verification_row)
                    latest_verification["result"] = _loads(
                        latest_verification.pop("raw_json", "{}"), {}
                    )
            return {
                "ok": True,
                "job_candidate_id": int(job_candidate_id),
                "stale": not assessment or assessment["snapshot_hash"] != snapshot_hash,
                "stopped": is_stopped(context),
                "assessment": self._assessment_payload(assessment) if assessment else None,
                "latest_run": latest_run_payload,
                "auto_assess_allowed": not recent_failure and not active_run,
                "panel_stale": not panel_row or panel_row["snapshot_hash"] != snapshot_hash,
                "panel": self._panel_payload(conn, panel_row) if panel_row else None,
                "verification_task": verification_task,
                "latest_verification": latest_verification,
                "artifacts": self._candidate_agent_artifacts(conn, int(job_candidate_id), context),
                "actions": {
                    name: action_decision(name, context)
                    for name in ["assess", "save_draft", "create_task", "complete_task", "resume_review", "outreach", "candidate_merge"]
                },
            }
        finally:
            conn.close()

    def _candidate_agent_artifacts(self, conn: sqlite3.Connection, job_candidate_id: int, context: dict[str, Any]) -> list[dict[str, Any]]:
        relation = context.get("relation") or {}
        candidate_ids = {
            str(value) for value in (
                job_candidate_id,
                relation.get("job_candidate_id"),
                relation.get("source_candidate_id"),
                relation.get("person_id"),
            )
            if value not in (None, "")
        }
        rows = conn.execute(
            """
            SELECT * FROM agent_artifacts
            WHERE artifact_type IN ('recommendation_report','matching_report','external_action_receipt','outreach_draft_batch')
            ORDER BY id DESC LIMIT 80
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _row(row)
            metadata = _loads(item.pop("metadata_json"), {})
            workflow_context = {}
            if not metadata.get("job_candidate_id"):
                ctx_row = conn.execute(
                    "SELECT context_json FROM agent_workflow_context WHERE workflow_id=? ORDER BY id DESC LIMIT 1",
                    (item.get("workflow_id"),),
                ).fetchone()
                workflow_context = _loads(ctx_row["context_json"], {}) if ctx_row else {}
            metadata_values = {
                str(metadata.get("job_candidate_id") or ""),
                str(metadata.get("candidate_id") or ""),
                str(metadata.get("person_id") or ""),
                str(workflow_context.get("id") or "") if workflow_context.get("type") == "candidate" else "",
            }
            if not (candidate_ids & {value for value in metadata_values if value}):
                continue
            item["metadata"] = metadata
            if item.get("content") and len(str(item["content"])) > 800:
                item["content"] = str(item["content"])[-800:]
            result.append(item)
            if len(result) >= 12:
                break
        return result

    def _channel_analytics(self, conn: sqlite3.Connection) -> dict[str, Any]:
        labels = {
            "liepin": "猎聘",
            "xsaas": "X-SaaS",
            "talent_pool": "历史人才库",
            "other": "其他来源",
            "unknown": "未归因",
        }
        profile_sources: dict[int, set[str]] = {}
        inventory: dict[str, set[str]] = {}
        if _table_exists(conn, "source_profiles"):
            for row in conn.execute(
                "SELECT person_id,source_type FROM source_profiles WHERE person_id IS NOT NULL"
            ).fetchall():
                person_id = int(row["person_id"])
                source = str(row["source_type"] or "")
                profile_sources.setdefault(person_id, set()).add(source)
                channel = _channel_key(source)
                inventory.setdefault(channel, set()).add(f"p:{person_id}")

        candidate_sources: dict[int, str] = {}
        if "source" in _table_columns(conn, "candidates"):
            candidate_rows = conn.execute(
                """
                SELECT c.id,c.source,MIN(jc.person_id) AS person_id
                FROM candidates c
                LEFT JOIN job_candidates jc ON CAST(jc.source_candidate_id AS INTEGER)=c.id
                GROUP BY c.id,c.source
                """
            ).fetchall()
            for row in candidate_rows:
                if row["id"] is None:
                    continue
                candidate_id = int(row["id"])
                source = str(row["source"] or "")
                candidate_sources[candidate_id] = source
                channel = _channel_key(source)
                identity = (
                    f"p:{int(row['person_id'])}"
                    if row["person_id"] is not None
                    else f"c:{candidate_id}"
                )
                inventory.setdefault(channel, set()).add(identity)

        def fallback_channel(person_id: int) -> str:
            channels = {_channel_key(source) for source in profile_sources.get(person_id, set())}
            for key in ("xsaas", "liepin", "talent_pool", "other"):
                if key in channels:
                    return key
            return "unknown"

        event_flags: dict[int, list[tuple[str, str]]] = {}
        if _table_exists(conn, "candidate_events"):
            for row in conn.execute(
                "SELECT job_candidate_id,event_type,event_status FROM candidate_events WHERE job_candidate_id IS NOT NULL"
            ).fetchall():
                status = str(row["event_status"] or "").strip().lower()
                if status in {"undone", "void", "invalid", "retracted"}:
                    continue
                event_flags.setdefault(int(row["job_candidate_id"]), []).append(
                    (str(row["event_type"] or "").lower(), status)
                )

        metrics: dict[str, dict[str, int]] = {}
        relation_rows = conn.execute(
            """
            SELECT id,person_id,source_candidate_id,clean_stage,raw_stage,raw_status
            FROM job_candidates
            """
        ).fetchall()
        for row in relation_rows:
            candidate_id: int | None = None
            try:
                candidate_id = int(str(row["source_candidate_id"] or ""))
            except ValueError:
                pass
            channel = _channel_key(candidate_sources.get(candidate_id or -1, ""))
            if channel == "unknown":
                channel = fallback_channel(int(row["person_id"]))
            values = metrics.setdefault(
                channel,
                {
                    "intake": 0,
                    "valid": 0,
                    "reviewed": 0,
                    "review_passed": 0,
                    "contacted": 0,
                    "replied": 0,
                    "recommended": 0,
                    "interview": 0,
                },
            )
            values["intake"] += 1
            stage = " ".join(
                str(row[key] or "") for key in ("clean_stage", "raw_stage", "raw_status")
            ).lower()
            events = event_flags.get(int(row["id"]), [])
            stopped = any(
                token in stage
                for token in ("h5 ", "停止", "淘汰", "拒绝", "screen_rejected", "rejected", "xsaas_review_stop")
            )
            review_results = {
                status
                for event_type, status in events
                if event_type == "resume_review_completed" and status in {"continue", "stop"}
            }
            reviewed = bool(review_results) or stopped or any(
                token in stage for token in ("复核通过", "已触达", "已回复", "推荐", "面试", "offer", "谈薪", "入职")
            )
            review_passed = "continue" in review_results or (
                not stopped
                and any(token in stage for token in ("复核通过", "已触达", "已回复", "推荐", "面试", "offer", "谈薪", "入职"))
            )
            contacted = any(
                token in stage for token in ("已触达", "已联系", "微信", "回复", "推荐", "面试", "offer", "谈薪", "入职")
            ) or any(
                event_type
                in {
                    "liepin_outreach",
                    "candidate_outreach",
                    "candidate_contact_update",
                    "outreach_status_backfill",
                    "candidate_message_received",
                }
                for event_type, _ in events
            )
            replied = "回复" in stage or any(
                event_type == "candidate_message_received" for event_type, _ in events
            )
            recommended = any(token in stage for token in ("推荐", "面试", "offer", "谈薪", "入职")) or any(
                event_type in {"candidate_recommended", "client_recommendation"}
                for event_type, _ in events
            )
            interview = any(token in stage for token in ("面试", "offer", "谈薪", "入职")) or any(
                event_type in {"interview", "interview_scheduled", "offer", "hired"}
                for event_type, _ in events
            )
            values["valid"] += int(not stopped)
            values["reviewed"] += int(reviewed)
            values["review_passed"] += int(review_passed)
            values["contacted"] += int(contacted)
            values["replied"] += int(replied)
            values["recommended"] += int(recommended)
            values["interview"] += int(interview)
            inventory.setdefault(channel, set()).add(f"p:{int(row['person_id'])}")

        def rate(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 4) if denominator else 0.0

        rows: list[dict[str, Any]] = []
        for channel in set(inventory) | set(metrics):
            values = metrics.get(
                channel,
                {key: 0 for key in ("intake", "valid", "reviewed", "review_passed", "contacted", "replied", "recommended", "interview")},
            )
            intake = int(values["intake"])
            valid = int(values["valid"])
            reviewed = int(values["reviewed"])
            contacted = int(values["contacted"])
            maturity = "可比较" if intake >= 10 else "样本较少" if intake else "待激活"
            rows.append(
                {
                    "channel": channel,
                    "label": labels.get(channel, channel),
                    "inventory": len(inventory.get(channel, set())),
                    **values,
                    "valid_rate": rate(valid, intake),
                    "review_pass_rate": rate(int(values["review_passed"]), reviewed),
                    "contact_rate": rate(contacted, intake),
                    "reply_rate": rate(int(values["replied"]), contacted),
                    "downstream_rate": rate(max(int(values["recommended"]), int(values["interview"])), intake),
                    "maturity": maturity,
                }
            )
        order = {"liepin": 0, "xsaas": 1, "talent_pool": 2, "other": 3, "unknown": 4}
        rows.sort(key=lambda item: (-int(item["intake"]), order.get(str(item["channel"]), 9)))
        total_intake = sum(int(item["intake"]) for item in rows)
        attributed = sum(int(item["intake"]) for item in rows if item["channel"] != "unknown")
        main_channel = max(rows, key=lambda item: int(item["intake"]), default={})
        untapped = max(rows, key=lambda item: int(item["inventory"]) - int(item["intake"]), default={})
        return {
            "summary": {
                "total_intake": total_intake,
                "attributed": attributed,
                "coverage_rate": rate(attributed, total_intake),
                "main_channel": main_channel.get("label", "暂无"),
                "untapped_channel": untapped.get("label", "暂无"),
                "untapped_inventory": max(0, int(untapped.get("inventory", 0)) - int(untapped.get("intake", 0))),
            },
            "rows": rows,
        }

    def get_dashboard(self) -> dict[str, Any]:
        workbench = self.get_workbench(limit=50)
        conn = self._connect()
        try:
            stage_rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段') AS stage,
                       COUNT(*) AS total
                FROM job_candidates
                GROUP BY COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段')
                ORDER BY total DESC
                """
            ).fetchall()
            stage_counts = {str(row["stage"]): int(row["total"]) for row in stage_rows}

            def stage_sum(*tokens: str) -> int:
                return sum(
                    total
                    for stage, total in stage_counts.items()
                    if any(token.lower() in stage.lower() for token in tokens)
                )

            def ratio(numerator: int, denominator: int) -> float:
                return round(numerator / denominator, 4) if denominator else 0.0

            total_relations = sum(stage_counts.values())
            closed_relations = stage_sum("H5", "停止", "淘汰", "拒绝")
            effective_relations = total_relations - closed_relations
            contacted_relations = stage_sum("已触达", "X3", "回复", "推荐", "面试", "offer", "谈薪", "入职")
            replied_relations = stage_sum("回复")
            recommended_relations = stage_sum("推荐", "面试", "offer", "谈薪", "入职")
            interview_relations = stage_sum("面试", "offer", "谈薪", "入职")
            offer_relations = stage_sum("offer", "谈薪", "入职")
            funnel_jobs = [
                _row(row)
                for row in conn.execute(
                    """
                    WITH relations AS (
                        SELECT raw_client AS client,raw_position AS job,
                               COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段') AS stage
                        FROM job_candidates
                    )
                    SELECT client,job,COUNT(*) AS total,
                           SUM(CASE WHEN stage LIKE 'H5 %' OR stage LIKE '%停止%' OR stage LIKE '%淘汰%' OR stage LIKE '%拒绝%' THEN 0 ELSE 1 END) AS effective,
                           SUM(CASE WHEN stage LIKE '%已触达%' OR stage LIKE 'X3 %' OR stage LIKE '%回复%' OR stage LIKE '%推荐%' OR stage LIKE '%面试%' OR lower(stage) LIKE '%offer%' OR stage LIKE '%谈薪%' OR stage LIKE '%入职%' THEN 1 ELSE 0 END) AS contacted,
                           SUM(CASE WHEN stage LIKE '%回复%' THEN 1 ELSE 0 END) AS replied,
                           SUM(CASE WHEN stage LIKE 'H5 %' OR stage LIKE '%停止%' OR stage LIKE '%淘汰%' OR stage LIKE '%拒绝%' THEN 1 ELSE 0 END) AS stopped
                    FROM relations
                    GROUP BY client,job
                    ORDER BY total DESC,effective DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]

            funnel = {
                "total": total_relations,
                "effective": effective_relations,
                "closed": closed_relations,
                "pending_review": stage_sum("待复核", "S1", "X1"),
                "waiting_contact": stage_sum("待联系", "复核通过"),
                "contacted": contacted_relations,
                "replied": replied_relations,
                "recommended": recommended_relations,
                "interview": interview_relations,
                "offer": offer_relations,
                "rates": {
                    "effective": ratio(effective_relations, total_relations),
                    "contacted": ratio(contacted_relations, effective_relations),
                    "replied": ratio(replied_relations, contacted_relations),
                    "recommended": ratio(recommended_relations, effective_relations),
                },
                "jobs": funnel_jobs,
                "stages": [{"stage": stage, "total": total} for stage, total in list(stage_counts.items())[:12]],
            }

            channels = self._channel_analytics(conn)

            feedback: dict[str, Any] = {
                "open_tasks": 0,
                "overdue_tasks": 0,
                "reply_events_7d": 0,
                "client_feedback_7d": 0,
                "candidate_reply_avg_hours": None,
                "candidate_reply_samples": 0,
                "client_feedback_avg_hours": None,
                "client_feedback_samples": 0,
                "stalled_jobs": [],
                "stalled_job_count": 0,
            }
            if _table_exists(conn, "followup_tasks"):
                task_row = conn.execute(
                    """
                    SELECT SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_tasks,
                           SUM(CASE WHEN status='open' AND due_at<>'' AND datetime(due_at)<datetime('now','localtime') THEN 1 ELSE 0 END) AS overdue_tasks
                    FROM followup_tasks
                    """
                ).fetchone()
                feedback["open_tasks"] = int(task_row["open_tasks"] or 0)
                feedback["overdue_tasks"] = int(task_row["overdue_tasks"] or 0)
            if _table_exists(conn, "candidate_events"):
                event_row = conn.execute(
                    """
                    SELECT SUM(CASE WHEN event_type='candidate_message_received' THEN 1 ELSE 0 END) AS replies
                    FROM candidate_events
                    WHERE lower(COALESCE(event_status,'')) NOT IN ('undone','void','invalid','retracted')
                      AND datetime(CASE WHEN event_time LIKE '%Z' THEN datetime(event_time,'localtime') ELSE event_time END)>=datetime('now','-7 days','localtime')
                    """
                ).fetchone()
                feedback["reply_events_7d"] = int(event_row["replies"] or 0)
                response_row = conn.execute(
                    """
                    WITH valid_events AS (
                        SELECT job_candidate_id,event_type,
                               julianday(CASE WHEN event_time LIKE '%Z' THEN datetime(event_time,'localtime') ELSE event_time END) AS event_jd
                        FROM candidate_events
                        WHERE job_candidate_id IS NOT NULL
                          AND lower(COALESCE(event_status,'')) NOT IN ('undone','void','invalid','retracted')
                    ), replies AS (
                        SELECT job_candidate_id,MIN(event_jd) AS reply_jd
                        FROM valid_events WHERE event_type='candidate_message_received'
                        GROUP BY job_candidate_id
                    ), pairs AS (
                        SELECT r.job_candidate_id,r.reply_jd,MAX(e.event_jd) AS outreach_jd
                        FROM replies r JOIN valid_events e ON e.job_candidate_id=r.job_candidate_id
                        WHERE e.event_type IN ('liepin_outreach','candidate_outreach','candidate_contact_update','outreach_status_backfill')
                          AND e.event_jd<=r.reply_jd
                        GROUP BY r.job_candidate_id,r.reply_jd
                    )
                    SELECT COUNT(*) AS samples,AVG((reply_jd-outreach_jd)*24.0) AS avg_hours
                    FROM pairs WHERE outreach_jd IS NOT NULL AND reply_jd>=outreach_jd
                    """
                ).fetchone()
                feedback["candidate_reply_samples"] = int(response_row["samples"] or 0)
                if response_row["avg_hours"] is not None:
                    feedback["candidate_reply_avg_hours"] = round(float(response_row["avg_hours"]), 1)
            if _table_exists(conn, "client_feedback_events"):
                client_feedback_row = conn.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN datetime(COALESCE(feedback_time,created_at))>=datetime('now','-7 days','localtime') THEN 1 ELSE 0 END) AS recent
                    FROM client_feedback_events
                    """
                ).fetchone()
                feedback["client_feedback_samples"] = int(client_feedback_row["total"] or 0)
                feedback["client_feedback_7d"] = int(client_feedback_row["recent"] or 0)
            if _table_exists(conn, "jobs"):
                stalled_rows = [
                    _row(row)
                    for row in conn.execute(
                        """
                        SELECT j.id AS job_id,c.name AS client,j.title AS job,j.status,j.updated_at,
                               CAST(julianday('now','localtime')-julianday(j.updated_at) AS INTEGER) AS stalled_days
                        FROM jobs j JOIN clients c ON c.id=j.client_id
                        WHERE j.status IN ('P0紧急/待启动','已发布/推进中','已搜索/可筛人','已发布','有反馈/待复盘','谈薪中','已触达/跟进中')
                          AND datetime(COALESCE(j.updated_at,''))<datetime('now','-7 days','localtime')
                        ORDER BY stalled_days DESC,j.id
                        LIMIT 10
                        """
                    ).fetchall()
                ]
                feedback["stalled_jobs"] = stalled_rows
                feedback["stalled_job_count"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM jobs
                        WHERE status IN ('P0紧急/待启动','已发布/推进中','已搜索/可筛人','已发布','有反馈/待复盘','谈薪中','已触达/跟进中')
                          AND datetime(COALESCE(updated_at,''))<datetime('now','-7 days','localtime')
                        """
                    ).fetchone()[0]
                )

            quality = compute_evaluation(conn)
            score_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       AVG(fit_score) AS avg_score,AVG(confidence) AS avg_confidence,
                       AVG(evidence_coverage) AS avg_coverage,
                       SUM(CASE WHEN fit_score>=75 THEN 1 ELSE 0 END) AS high,
                       SUM(CASE WHEN fit_score>=55 AND fit_score<75 THEN 1 ELSE 0 END) AS medium,
                       SUM(CASE WHEN fit_score<55 THEN 1 ELSE 0 END) AS low,
                       SUM(CASE WHEN evidence_coverage>=0.75 THEN 1 ELSE 0 END) AS coverage_high,
                       SUM(CASE WHEN evidence_coverage>=0.50 AND evidence_coverage<0.75 THEN 1 ELSE 0 END) AS coverage_medium,
                       SUM(CASE WHEN evidence_coverage<0.50 THEN 1 ELSE 0 END) AS coverage_low
                FROM agent_candidate_assessments WHERE is_current=1
                """
            ).fetchone()
            failed_runs = int(
                conn.execute("SELECT COUNT(*) FROM agent_runs WHERE status='failed'").fetchone()[0]
            )
            latest_run_row = conn.execute(
                """
                WITH latest AS (
                    SELECT context_id,status,
                           ROW_NUMBER() OVER(PARTITION BY context_id ORDER BY id DESC) AS rn
                    FROM agent_runs WHERE context_type='job_candidate'
                )
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                FROM latest WHERE rn=1
                """
            ).fetchone()
            shadow_rows = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT sr.*,p.display_name AS candidate,jc.raw_client AS client,jc.raw_position AS job
                    FROM agent_stage_recommendations sr
                    JOIN job_candidates jc ON jc.id=sr.job_candidate_id
                    JOIN people p ON p.id=jc.person_id
                    WHERE sr.mode='shadow' AND sr.status='pending'
                    ORDER BY sr.id DESC LIMIT 12
                    """
                ).fetchall()
            ]
            memory_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN status='revoked' THEN 1 ELSE 0 END) AS revoked,
                       SUM(CASE WHEN hit_count>0 THEN 1 ELSE 0 END) AS used
                FROM agent_memories
                """
            ).fetchone()
            recall_row = conn.execute(
                """
                SELECT COUNT(*) AS recalls,SUM(adopted) AS adopted,SUM(conflict) AS conflicts
                FROM agent_memory_recalls
                """
            ).fetchone()
            rule_rows = {
                str(row["status"]): int(row["total"])
                for row in conn.execute(
                    "SELECT status,COUNT(*) AS total FROM agent_learning_rules GROUP BY status"
                ).fetchall()
            }
            quality.update(
                {
                    "current_total": int(score_row["total"] or 0),
                    "avg_score": round(float(score_row["avg_score"] or 0), 1),
                    "avg_confidence": round(float(score_row["avg_confidence"] or 0), 4),
                    "avg_coverage": round(float(score_row["avg_coverage"] or 0), 4),
                    "high_score_total": int(score_row["high"] or 0),
                    "medium_score_total": int(score_row["medium"] or 0),
                    "low_score_total": int(score_row["low"] or 0),
                    "failed_runs": failed_runs,
                    "latest_run_total": int(latest_run_row["total"] or 0),
                    "latest_failed_runs": int(latest_run_row["failed"] or 0),
                    "latest_completed_runs": int(latest_run_row["completed"] or 0),
                    "latest_failure_rate": ratio(
                        int(latest_run_row["failed"] or 0), int(latest_run_row["total"] or 0)
                    ),
                    "score_distribution": [
                        {"label": "75-100", "count": int(score_row["high"] or 0)},
                        {"label": "55-74", "count": int(score_row["medium"] or 0)},
                        {"label": "0-54", "count": int(score_row["low"] or 0)},
                    ],
                    "coverage_distribution": [
                        {"label": "覆盖充分", "count": int(score_row["coverage_high"] or 0)},
                        {"label": "部分覆盖", "count": int(score_row["coverage_medium"] or 0)},
                        {"label": "证据不足", "count": int(score_row["coverage_low"] or 0)},
                    ],
                    "shadow_total": len(shadow_rows),
                    "memory": {
                        "mode": self.config["memory"]["mode"],
                        "total": int(memory_row["total"] or 0),
                        "active": int(memory_row["active"] or 0),
                        "revoked": int(memory_row["revoked"] or 0),
                        "used": int(memory_row["used"] or 0),
                        "recalls": int(recall_row["recalls"] or 0),
                        "adopted": int(recall_row["adopted"] or 0),
                        "conflicts": int(recall_row["conflicts"] or 0),
                    },
                    "learning": {
                        "collecting": rule_rows.get("collecting", 0),
                        "pending": rule_rows.get("pending", 0),
                        "active": rule_rows.get("active", 0),
                        "suspended": rule_rows.get("suspended", 0),
                    },
                }
            )

            p0_jobs: list[dict[str, Any]] = []
            if _table_exists(conn, "job_pipeline_metrics"):
                p0_jobs = [
                    _row(row)
                    for row in conn.execute(
                        """
                        SELECT j.id AS job_id,c.name AS client,j.title AS job,j.status,
                               m.priority,m.risk,m.a_count,m.b_count,m.p0_count,m.p1_count,
                               m.contacted_count,m.pending_followup_count,m.data_gap,m.stop_condition
                        FROM jobs j JOIN clients c ON c.id=j.client_id
                        JOIN job_pipeline_metrics m ON m.id=(
                            SELECT MAX(m2.id) FROM job_pipeline_metrics m2 WHERE m2.job_id=j.id
                        )
                        WHERE COALESCE(m.priority,'') LIKE 'P0%'
                        ORDER BY COALESCE(m.data_gap,0) DESC,COALESCE(m.pending_followup_count,0) DESC,j.id
                        LIMIT 12
                        """
                    ).fetchall()
                ]

            proposals = self.list_proposals(status="pending", limit=20).get("proposals", [])
            items = workbench.get("items", [])
            top_actions = [
                {
                    "type": "candidate",
                    "id": item["job_candidate_id"],
                    "label": f"{item['candidate']} · {item['label']}",
                    "project": f"{item['client']} / {item['job']}",
                    "reason": item["reason"],
                    "priority": item["priority"],
                }
                for item in items[:3]
            ]
            exceptions = [item for item in items if item["kind"] in {"failed", "stale"}][:8]
            return {
                "ok": True,
                "summary": workbench.get("summary", {}),
                "runtime": workbench.get("runtime", {}),
                "top_actions": top_actions,
                "exceptions": exceptions,
                "pending_approvals": proposals,
                "p0_jobs": p0_jobs,
                "shadow_recommendations": shadow_rows,
                "analytics": {
                    "funnel": funnel,
                    "channels": channels,
                    "feedback": feedback,
                    "agent_quality": quality,
                },
            }
        finally:
            conn.close()

    def get_flow_inbox(
        self, queue: str = "今日待办", client: str = "", job: str = "", search: str = "",
        view: str = "action", limit: int = 100, **_: Any,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 100), 300))
        conn = self._connect()
        try:
            due_select = (
                "(SELECT MIN(t.due_at) FROM followup_tasks t WHERE t.job_candidate_id=jc.id AND COALESCE(t.status,'open')='open') AS due_at"
                if _table_exists(conn, "followup_tasks") else "NULL AS due_at"
            )
            p0_select = (
                """EXISTS(
                         SELECT 1 FROM job_pipeline_metrics m WHERE m.job_id=jc.job_id
                           AND m.id=(SELECT MAX(m2.id) FROM job_pipeline_metrics m2 WHERE m2.job_id=jc.job_id)
                           AND COALESCE(m.priority,'') LIKE 'P0%'
                       ) AS is_p0"""
                if _table_exists(conn, "job_pipeline_metrics") else "0 AS is_p0"
            )
            rows = conn.execute(
                f"""
                SELECT jc.id AS job_candidate_id,p.id AS person_id,p.display_name AS candidate,
                       p.current_company AS company,p.current_title AS title,
                       COALESCE(c.name,jc.raw_client,'') AS client,
                       COALESCE(j.title,jc.raw_position,'') AS job,
                       jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                       (SELECT e.event_type FROM candidate_events e WHERE e.job_candidate_id=jc.id ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS last_event_type,
                       (SELECT e.event_time FROM candidate_events e WHERE e.job_candidate_id=jc.id ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS last_event_time,
                       (SELECT e.summary FROM candidate_events e WHERE e.job_candidate_id=jc.id ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS last_event_summary,
                       (SELECT e.event_status FROM candidate_events e WHERE e.job_candidate_id=jc.id AND e.event_type='resume_review_completed' ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS review_status,
                       (SELECT e.event_time FROM candidate_events e WHERE e.job_candidate_id=jc.id AND e.event_type='resume_review_completed' ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS review_time,
                       (SELECT e.event_time FROM candidate_events e WHERE e.job_candidate_id=jc.id AND e.event_type='candidate_message_received' ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS reply_time,
                       (SELECT e.summary FROM candidate_events e WHERE e.job_candidate_id=jc.id AND e.event_type='candidate_message_received' ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS reply_summary,
                       (SELECT e.event_time FROM candidate_events e WHERE e.job_candidate_id=jc.id AND e.event_type IN ('candidate_message_sent','candidate_contact_update','candidate_outreach') ORDER BY e.event_time DESC,e.id DESC LIMIT 1) AS outreach_time,
                       {due_select},
                       {p0_select},
                       a.fit_score,a.recommendation,a.next_action,a.evidence_coverage
                FROM job_candidates jc
                JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id
                LEFT JOIN clients c ON c.id=j.client_id
                LEFT JOIN agent_candidate_assessments a ON a.id=(
                    SELECT MAX(a2.id) FROM agent_candidate_assessments a2
                    WHERE a2.job_candidate_id=jc.id AND a2.is_current=1
                )
                ORDER BY jc.id DESC
                """
            ).fetchall()
            now = time.time()
            items = []
            for row in rows:
                item = _row(row)
                stage = str(item.get("clean_stage") or "")
                raw = " ".join(
                    str(item.get(key) or "")
                    for key in ("raw_status", "last_event_summary", "last_event_type")
                )
                stopped = stage.startswith("H5 ") or str(item.get("review_status") or "").lower() == "stop"
                exception = any(token in raw.lower() for token in ("ambiguous", "duplicate", "未唯一定位", "重复", "异常"))
                replied = bool(item.get("reply_time")) and not stopped
                verification = not stopped and (
                    "待核验" in stage
                    or "待核验" in raw
                    or str(item.get("recommendation") or "") == "verify_first"
                )
                contacted = bool(item.get("outreach_time")) or stage in {"已触达", "X3 已申请加微信/待通过"}
                waiting_contact = not stopped and str(item.get("review_status") or "").lower() == "continue" and not contacted
                pending_review = not stopped and (
                    stage.startswith(("H1 ", "X1 ")) or "待复核" in stage or "待筛" in stage
                )
                overdue = False
                for value, days in ((item.get("due_at"), 0), (item.get("outreach_time"), 5)):
                    if not value or replied or stopped:
                        continue
                    try:
                        timestamp = time.mktime(time.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S"))
                        overdue = overdue or timestamp + days * 86400 < now
                    except ValueError:
                        pass
                if stopped:
                    queue_key = "历史"
                elif exception:
                    queue_key = "异常"
                elif replied:
                    queue_key = "已回复"
                elif overdue:
                    queue_key = "超时"
                elif verification:
                    queue_key = "待核验"
                elif waiting_contact:
                    queue_key = "待联系"
                elif pending_review:
                    queue_key = "待复核"
                else:
                    queue_key = "进行中"
                priority = (
                    (10000 if item.get("is_p0") else 0)
                    + (5000 if replied else 0) + (4000 if overdue else 0) + (3500 if exception else 0)
                    + (3000 if verification else 0) + (2000 if waiting_contact else 0) + (1000 if pending_review else 0)
                )
                item.update(
                    {
                        "queue": queue_key, "stopped": stopped, "overdue": overdue,
                        "exception": exception, "project": f"{item['client']} / {item['job']}",
                        "signal": item.get("reply_summary") or item.get("last_event_summary") or stage or "暂无动态",
                        "next_action": item.get("next_action") or (
                            "处理候选人回复" if replied else "核验关键证据" if verification else
                            "人工联系候选人" if waiting_contact else "人工复核推进方向" if pending_review else
                            "检查最近动态"
                        ),
                        "priority_score": priority,
                    }
                )
                items.append(item)
            active = [item for item in items if not item["stopped"]]
            summary = {
                "todo": sum(item["queue"] not in {"进行中"} for item in active),
                "replied": sum(item["queue"] == "已回复" for item in active),
                "overdue": sum(item["queue"] == "超时" for item in active),
                "exceptions": sum(item["queue"] == "异常" for item in active),
                "pending_review": sum(item["queue"] == "待复核" for item in active),
                "waiting_contact": sum(item["queue"] == "待联系" for item in active),
                "verification": sum(item["queue"] == "待核验" for item in active),
                "active": len(active), "history": len(items) - len(active),
            }
            selected = items if view == "history" or queue == "历史" else active
            queue_map = {
                "今日待办": {"异常", "已回复", "超时", "待核验", "待联系", "待复核"},
                "全部进行中": {"异常", "已回复", "超时", "待核验", "待联系", "待复核", "进行中"},
            }
            if view == "history" or queue == "历史":
                selected = [item for item in items if item["stopped"]]
            elif queue and queue not in {"今日待办", "全部进行中"}:
                selected = [item for item in selected if item["queue"] == queue]
            elif queue in queue_map:
                selected = [item for item in selected if item["queue"] in queue_map[queue]]
            if client:
                selected = [item for item in selected if item["client"] == client]
            if job:
                selected = [item for item in selected if item["job"] == job]
            if search:
                needle = str(search).lower()
                selected = [
                    item for item in selected
                    if needle in " ".join(str(item.get(key) or "") for key in ("candidate", "company", "title", "client", "job")).lower()
                ]
            selected.sort(key=lambda item: (-int(item["priority_score"]), str(item.get("updated_at") or "")), reverse=False)
            updated_at = max((str(item.get("updated_at") or item.get("last_event_time") or "") for item in items), default="")
            return {
                "ok": True, "summary": summary, "items": sanitize_payload(selected[:limit]), "total": len(selected),
                "filters": {"queue": queue, "client": client, "job": job, "search": search, "view": view},
                "version": hashlib.sha256(f"{updated_at}|{summary}|{len(selected)}".encode("utf-8")).hexdigest()[:16],
            }
        finally:
            conn.close()

    def get_flow_item(self, job_candidate_id: int) -> dict[str, Any]:
        inbox = self.get_flow_inbox(queue="全部进行中", limit=300)
        item = next(
            (row for row in inbox.get("items") or [] if int(row["job_candidate_id"]) == int(job_candidate_id)),
            None,
        )
        if item is None:
            history = self.get_flow_inbox(queue="历史", view="history", limit=300)
            item = next(
                (row for row in history.get("items") or [] if int(row["job_candidate_id"]) == int(job_candidate_id)),
                None,
            )
        if item is None:
            raise ValueError(f"找不到人岗关系：{job_candidate_id}")
        return {"ok": True, "item": item, "summary": inbox.get("summary", {})}

    def _normalize_copilot_context(self, context: dict[str, Any]) -> dict[str, Any]:
        context_type = str(context.get("type") or "global").strip().lower()
        if context_type not in {"global", "page", "job", "candidate", "queue"}:
            context_type = "global"
        context_id = None
        if context_type in {"job", "candidate"}:
            try:
                context_id = int(context.get("id") or 0) or None
            except (TypeError, ValueError):
                context_id = None
        page = str(context.get("page") or "").strip().lower()
        if page not in {"overview", "positions", "flow", "candidates"}:
            page = ""
        raw_filters = context.get("filters") if isinstance(context.get("filters"), dict) else {}
        filters = {
            key: " ".join(str(raw_filters.get(key) or "").split())[:120]
            for key in ("queue", "client", "job", "search", "view")
            if raw_filters.get(key) not in (None, "")
        }
        return {"type": context_type, "id": context_id, "page": page, "filters": filters}

    def _floating_bridge_evidence(self, context: dict[str, Any]) -> dict[str, Any]:
        bridge = context.get("bridge") if isinstance(context.get("bridge"), dict) else {}
        if not bridge:
            return {}
        surface = str(bridge.get("surface") or "").strip().lower()
        if surface not in {"liepin", "xsaas", "a_system", "native"}:
            return {}

        def compact(value: Any, limit: int) -> str:
            text = " ".join(str(value or "").split())
            return text[:limit]

        if surface == "native":
            frontmost_app = bridge.get("frontmost_app") if isinstance(bridge.get("frontmost_app"), dict) else {}
            window = bridge.get("window") if isinstance(bridge.get("window"), dict) else {}
            wechat = bridge.get("wechat") if isinstance(bridge.get("wechat"), dict) else {}
            blocks = wechat.get("text_blocks") if isinstance(wechat.get("text_blocks"), list) else []
            visible_text = (
                wechat.get("visible_text_clean")
                or wechat.get("combined_text")
                or "\n".join(str(item) for item in blocks)
            )
            message_blocks: list[dict[str, Any]] = []
            for item in (wechat.get("message_blocks") or [])[:40]:
                if not isinstance(item, dict):
                    continue
                text = compact(item.get("text"), 500)
                if not text:
                    continue
                message_blocks.append({
                    "text": text,
                    "side": compact(item.get("side"), 20),
                    "x": item.get("x"),
                    "y": item.get("y"),
                })
            ocr_quality = wechat.get("ocr_quality") if isinstance(wechat.get("ocr_quality"), dict) else {}
            raw_image_analysis = wechat.get("image_analysis") if isinstance(wechat.get("image_analysis"), dict) else {}
            classifications: list[dict[str, Any]] = []
            for item in (raw_image_analysis.get("classifications") or [])[:12]:
                if not isinstance(item, dict):
                    continue
                try:
                    confidence = round(float(item.get("confidence") or 0), 4)
                except (TypeError, ValueError):
                    confidence = 0.0
                label = compact(item.get("label"), 100)
                if label:
                    classifications.append({"label": label, "confidence": confidence})
            image_analysis = {
                "source": compact(raw_image_analysis.get("source"), 80),
                "ocr_text": compact(raw_image_analysis.get("ocr_text"), 12000),
                "classifications": classifications,
            }
            image_analysis = {
                key: value for key, value in image_analysis.items()
                if value not in (None, "", [])
            }
            try:
                text_block_count = max(0, int(wechat.get("text_block_count") or len(blocks)))
            except (TypeError, ValueError):
                text_block_count = len(blocks)
            evidence = {
                "source": "native",
                "page_type": "wechat_visible_window" if wechat else "native_window",
                "label": "微信当前可见窗口" if wechat else "当前 macOS 窗口",
                "app_name": compact(frontmost_app.get("name"), 80),
                "bundle_id": compact(frontmost_app.get("bundle_id"), 160),
                "window_title": compact(window.get("title") or wechat.get("window_title"), 180),
                "capture_mode": compact(wechat.get("capture_mode"), 40),
                "visible_text": compact(visible_text, 12000),
                "message_blocks": message_blocks,
                "ocr_quality": ocr_quality,
                "text_block_count": text_block_count,
                "bridge_status": compact(wechat.get("status") or bridge.get("status"), 180),
                "evidence_scope": "current_visible_window_ocr" if wechat else "window_metadata_only",
                "attachment_content_available": False,
                "visual_understanding_available": bool(image_analysis),
                "image_analysis": image_analysis,
                "untrusted_screen_content": True,
            }
            return {key: value for key, value in evidence.items() if value not in (None, "", [])}

        candidate = bridge.get("candidate") if isinstance(bridge.get("candidate"), dict) else {}
        profile = (
            candidate.get("profile_summary")
            or bridge.get("candidate_profile_text")
            or bridge.get("profile_summary")
            or bridge.get("page_text")
            or ""
        )
        evidence = {
            "source": surface,
            "page_type": compact(bridge.get("page_type"), 40),
            "candidate_name": compact(candidate.get("name") or bridge.get("candidate_name"), 40),
            "company": compact(candidate.get("company") or bridge.get("company"), 80),
            "title": compact(candidate.get("title") or bridge.get("candidate_title"), 120),
            "profile_summary": compact(profile, 4200),
            "bridge_status": compact(bridge.get("status"), 160),
            "source_url": compact(bridge.get("source_url") or bridge.get("url"), 260),
        }
        return {key: value for key, value in evidence.items() if value}

    def _uploaded_attachment_evidence(self, context: dict[str, Any]) -> dict[str, Any]:
        raw_items = context.get("uploaded_attachments") if isinstance(context.get("uploaded_attachments"), list) else []
        items: list[dict[str, Any]] = []
        for raw in raw_items[:3]:
            if not isinstance(raw, dict):
                continue
            file_name = Path(str(raw.get("file_name") or "").strip()).name[:180]
            if not file_name:
                continue
            extracted_text = str(raw.get("extracted_text") or "")[:18000]
            raw_analysis = raw.get("image_analysis") if isinstance(raw.get("image_analysis"), dict) else {}
            classifications: list[dict[str, Any]] = []
            for classification in (raw_analysis.get("classifications") or [])[:12]:
                if not isinstance(classification, dict):
                    continue
                label = " ".join(str(classification.get("label") or "").split())[:100]
                if not label:
                    continue
                try:
                    confidence = round(float(classification.get("confidence") or 0), 4)
                except (TypeError, ValueError):
                    confidence = 0.0
                classifications.append({"label": label, "confidence": confidence})
            image_analysis = {
                "source": " ".join(str(raw_analysis.get("source") or "pasted_clipboard_image").split())[:80],
                "ocr_text": str(raw_analysis.get("ocr_text") or extracted_text)[:12000],
                "classifications": classifications,
            }
            image_analysis = {key: value for key, value in image_analysis.items() if value not in (None, "", [])}
            try:
                size_bytes = max(0, int(raw.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size_bytes = 0
            items.append(
                {
                    "attachment_id": " ".join(str(raw.get("attachment_id") or "").split())[:80],
                    "file_name": file_name,
                    "file_type": " ".join(str(raw.get("file_type") or Path(file_name).suffix.lstrip(".")).split())[:20],
                    "mime_type": " ".join(str(raw.get("mime_type") or "").split())[:120],
                    "size_bytes": size_bytes,
                    "content_available": bool(raw.get("content_available") and (extracted_text or image_analysis)),
                    "extracted_text": extracted_text,
                    "truncated": bool(raw.get("truncated")),
                    "is_image": bool(raw.get("is_image")),
                    "image_analysis": image_analysis,
                    "status": " ".join(str(raw.get("status") or "").split())[:160],
                    "untrusted_document_content": True,
                }
            )
        if not items:
            return {}
        return {
            "scope": "user_selected_local_upload",
            "items": items,
            "content_available": any(item["content_available"] for item in items),
            "local_paths_exposed": False,
        }

    def _mentioned_jobs_for_copilot(self, message: str, limit: int = 5) -> list[dict[str, Any]]:
        cleaned = " ".join(str(message or "").split())
        if not cleaned:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,
                       COALESCE(m.priority,'') AS priority
                FROM jobs j
                JOIN clients c ON c.id=j.client_id
                LEFT JOIN job_pipeline_metrics m ON m.job_id=j.id
                WHERE COALESCE(j.status,'open')!='closed'
                ORDER BY CASE WHEN COALESCE(m.priority,'') LIKE 'P0%' THEN 0 ELSE 1 END, j.id DESC
                LIMIT 300
                """
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,'' AS priority
                FROM jobs j JOIN clients c ON c.id=j.client_id
                WHERE COALESCE(j.status,'open')!='closed'
                ORDER BY j.id DESC LIMIT 300
                """
            ).fetchall()
        finally:
            conn.close()
        # 岗位状态过滤（2026-07-22）：黑名单状态（待启动/暂停/只读快照/已拆分等）
        # 的岗位不作为可推荐/可定位结果返回；名单见 a_system_agent/job_status.py。
        rows = [row for row in rows if job_status_intake_allowed(row["status"])]
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            item = _row(row)
            client = str(item.get("client") or "")
            job = str(item.get("job") or "")
            score = 0
            job_id = int(item.get("id") or 0)
            if job_id and re.search(rf"(?:#\s*|岗位\s*#?\s*){job_id}(?!\d)", cleaned, re.I):
                score += 100
            if client and client in cleaned:
                score += 12
            for token in re.split(r"[\s/（）()、,，｜|]+", job):
                token = token.strip()
                if len(token) >= 2 and token in cleaned:
                    score += min(8, len(token))
            if "机械" in cleaned and "机械" in job:
                score += 10
            if "长越" in cleaned and "长越" in client:
                score += 10
            if score:
                item["summary"] = str(item.get("summary") or "")[:900]
                scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], int(pair[1].get("id") or 0)), reverse=True)
        return [item for _, item in scored[: max(1, min(int(limit or 5), 10))]]

    def get_copilot_focus(self, session_id: str) -> dict[str, Any] | None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT revision,context_type,context_id,client,job_id,candidate_id,action,
                       confidence,focus_json,evidence_json,conflicts_json,updated_at
                FROM agent_copilot_focus WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        focus = _loads(row["focus_json"], {})
        focus.update(
            {
                "session_id": session_id,
                "revision": int(row["revision"] or 1),
                "context": focus.get("context") or {
                    "type": row["context_type"] or "global",
                    "id": row["context_id"],
                },
                "client": focus.get("client") or row["client"] or "",
                "action": focus.get("action") or row["action"] or "",
                "confidence": float(row["confidence"] or 0),
                "evidence": _loads(row["evidence_json"], []),
                "conflicts": _loads(row["conflicts_json"], []),
                "updated_at": row["updated_at"],
            }
        )
        focus["needs_clarification"] = bool(focus.get("conflicts"))
        return focus

    @staticmethod
    def _copilot_action_kind(message: str) -> str:
        if (
            any(token in message for token in ("人选", "候选人"))
            and any(token in message for token in ("补充", "补池", "找", "搜索", "搜", "寻访"))
        ):
            return "candidate_sourcing"
        rules = (
            ("job_archive", ("归档岗位", "岗位归档", "关闭岗位", "岗位关闭", "没拆分的岗位", "未拆分的岗位")),
            ("job_split", ("拆分岗位", "岗位拆分", "拆成", "分成")),
            ("job_publish", ("发布岗位", "岗位发布", "上架岗位")),
            ("candidate_sourcing", ("补池", "寻访", "找人", "找些人选", "找候选人", "搜索人选")),
            ("candidate_outreach", ("触达", "开聊", "发送消息", "联系候选人")),
            ("candidate_review", ("复核", "初筛", "停止推进", "继续推进")),
            ("recommendation", ("推荐报告", "推荐给客户", "提交客户")),
            ("salary", ("谈薪", "薪资")),
        )
        for action, tokens in rules:
            if any(token in message for token in tokens):
                return action
        return ""

    def _copilot_focus_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
        context_type = str(context.get("type") or "global")
        try:
            context_id = int(context.get("id") or 0)
        except (TypeError, ValueError):
            context_id = 0
        if context_type not in {"job", "candidate"} or context_id <= 0:
            return {}
        conn = self._connect()
        try:
            if context_type == "job":
                row = conn.execute(
                    """
                    SELECT j.id,c.name AS client,j.title AS job,j.status
                    FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                    """,
                    (context_id,),
                ).fetchone()
                if row:
                    return {
                        "context": {"type": "job", "id": context_id},
                        "client": str(row["client"] or ""),
                        "job": {"id": context_id, "title": str(row["job"] or ""), "status": str(row["status"] or "")},
                        "candidate": {},
                    }
                return {}
            row = conn.execute(
                """
                SELECT jc.id,p.display_name,c.name AS client,j.id AS job_id,j.title AS job
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                WHERE jc.id=?
                """,
                (context_id,),
            ).fetchone()
            if row:
                return {
                    "context": {"type": "candidate", "id": context_id},
                    "client": str(row["client"] or ""),
                    "job": {"id": int(row["job_id"] or 0), "title": str(row["job"] or "")},
                    "candidate": {"id": context_id, "name": str(row["display_name"] or "")},
                }
        finally:
            conn.close()
        return {}

    def _copilot_context_from_focus(
        self, session_id: str, message: str, selected: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        focus = self.get_copilot_focus(session_id)
        current_clients = self._mentioned_client_names(message)
        current_jobs = self._mentioned_jobs_for_copilot(message)
        conflicts: list[dict[str, Any]] = []
        if len(current_clients) > 1:
            conflicts.append({"type": "ambiguous_client", "candidates": current_clients[:5]})
        if len(current_jobs) > 1:
            conflicts.append(
                {
                    "type": "ambiguous_job",
                    "candidates": [
                        {"id": item.get("id"), "client": item.get("client"), "job": item.get("job")}
                        for item in current_jobs[:5]
                    ],
                }
            )
        if conflicts:
            return dict(selected), conflicts

        selected_facts = self._copilot_focus_context_facts(selected)
        if selected_facts and current_clients and selected_facts.get("client") not in current_clients:
            return {"type": "global", "id": None, "page": selected.get("page") or "overview", "filters": {}}, []
        if selected_facts:
            return dict(selected), []
        if len(current_jobs) == 1:
            return {
                "type": "job", "id": int(current_jobs[0]["id"]),
                "page": selected.get("page") or "positions", "filters": {},
            }, []
        if not focus:
            return dict(selected), []
        focus_context = focus.get("context") if isinstance(focus.get("context"), dict) else {}
        focus_client = str(focus.get("client") or "")
        if current_clients and focus_client not in current_clients:
            return dict(selected), []
        selected_candidate_id: int | None = None
        if str(selected.get("type") or "") == "candidate":
            try:
                selected_candidate_id = int(selected.get("id") or 0) or None
            except (TypeError, ValueError):
                selected_candidate_id = None
        focus_candidate = focus.get("candidate") if isinstance(focus.get("candidate"), dict) else {}
        focus_candidate_id = focus_candidate.get("id") or (
            focus_context.get("id") if focus_context.get("type") == "candidate" else None
        )
        try:
            focus_candidate_id = int(focus_candidate_id or 0) or None
        except (TypeError, ValueError):
            focus_candidate_id = None
        if (
            selected_candidate_id
            and focus_candidate_id
            and selected_candidate_id != focus_candidate_id
        ):
            # 消息明确附带了与旧焦点不同的候选人页面上下文：页面事实优先，
            # continuation 恢复让位，不再复活旧候选人焦点。
            return dict(selected), []
        continuation = _is_short_ack(message) or any(
            token in message
            for token in ("继续", "刚才", "之前", "那个", "这个", "当前", "上述", "按刚才", "按之前", "按此", "再找")
        )
        if (
            continuation
            and focus_context.get("type") in {"job", "candidate"}
            and focus_context.get("id")
            and float(focus.get("confidence") or 0) >= 0.7
        ):
            return {
                "type": str(focus_context["type"]),
                "id": int(focus_context["id"]),
                "page": "positions" if focus_context["type"] == "job" else "candidates",
                "filters": {},
            }, []
        return dict(selected), []

    def _copilot_workflow_outcome_context(
        self,
        message: str,
        selected: dict[str, Any],
        mentioned_jobs: list[dict[str, Any]],
        existing_focus: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """为 Copilot 注入所涉岗位的寻访轮次业务终态与渠道漏斗（全部 DB 实读）。

        岗位解析顺序：当前页面岗位 → 消息唯一提及岗位 → 会话焦点岗位。
        每轮给出 business_outcome 中文语义（复用 classify_business_outcome 口径）
        与 agent_sourcing_funnel 行；历史无漏斗行时标注"该轮未记录渠道明细"，
        不向 LLM 提供任何可编造数字的空间。
        """
        job_id: int | None = None
        if str(selected.get("type") or "") == "job":
            try:
                job_id = int(selected.get("id") or 0) or None
            except (TypeError, ValueError):
                job_id = None
        if job_id is None and len(mentioned_jobs) == 1:
            try:
                job_id = int(mentioned_jobs[0].get("id") or 0) or None
            except (TypeError, ValueError):
                job_id = None
        if job_id is None and existing_focus:
            focus_context = existing_focus.get("context") if isinstance(existing_focus.get("context"), dict) else {}
            if (
                focus_context.get("type") == "job"
                and float(existing_focus.get("confidence") or 0) >= 0.7
            ):
                try:
                    job_id = int(focus_context.get("id") or 0) or None
                except (TypeError, ValueError):
                    job_id = None
        if job_id is None:
            return {}

        asked_round: int | None = None
        round_match = re.search(r"第\s*(\d+)\s*轮", str(message or ""))
        if round_match:
            asked_round = int(round_match.group(1))

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT w.workflow_id,w.status,w.business_outcome,w.created_at,w.updated_at,
                       g.objective,g.context_json
                FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                WHERE g.context_type='job' AND g.context_id=?
                  AND w.status NOT IN ('cancelled','superseded')
                ORDER BY w.created_at ASC, w.id ASC
                """,
                (job_id,),
            ).fetchall()
            rounds: list[dict[str, Any]] = []
            for row in rows:
                workflow_id = str(row["workflow_id"])
                context = _loads(row["context_json"], {})
                # 轮次编号按用户可见时间线（取消/被取代不计）；classify_business_outcome
                # 的寻访判定只用于标注 is_sourcing，不用于剔除轮次——
                # 否则"再多找些人选"这类措辞的寻访轮会被跳过，编号与用户口径错位。
                is_sourcing = sourcing_target_stats(conn, row["objective"], context, workflow_id) is not None
                outcome = str(row["business_outcome"] or "") or classify_business_outcome(conn, workflow_id)
                funnel_rows = conn.execute(
                    """
                    SELECT channel,status,query_count,recall_count,extracted_count,dedupe_count,
                           unique_count,detail_complete,detail_partial,detail_failed,
                           intake_duplicate_count,intake_new_count,assessed_count,high_score_count,
                           zero_attribution,error
                    FROM agent_sourcing_funnel WHERE workflow_id=? ORDER BY channel ASC, id ASC
                    """,
                    (workflow_id,),
                ).fetchall()
                channels: list[dict[str, Any]] = []
                channel_segments: list[str] = []
                for funnel in funnel_rows:
                    attribution = str(funnel["zero_attribution"] or "") or None
                    attribution_label = ZERO_RESULT_ATTRIBUTION_LABELS.get(attribution or "")
                    error_text = str(funnel["error"] or "").strip()
                    channel = {
                        "channel": str(funnel["channel"] or ""),
                        "status": str(funnel["status"] or ""),
                        "query_count": int(funnel["query_count"] or 0),
                        "recall_count": int(funnel["recall_count"] or 0),
                        "extracted_count": int(funnel["extracted_count"] or 0),
                        "dedupe_count": int(funnel["dedupe_count"] or 0),
                        "unique_count": int(funnel["unique_count"] or 0),
                        "detail_complete": int(funnel["detail_complete"] or 0),
                        "detail_partial": int(funnel["detail_partial"] or 0),
                        "detail_failed": int(funnel["detail_failed"] or 0),
                        "intake_duplicate_count": int(funnel["intake_duplicate_count"] or 0),
                        "intake_new_count": int(funnel["intake_new_count"] or 0),
                        "assessed_count": int(funnel["assessed_count"] or 0),
                        "high_score_count": int(funnel["high_score_count"] or 0),
                        "zero_attribution": attribution,
                        "zero_attribution_label": attribution_label or None,
                        "error": error_text[-160:] or None,
                    }
                    channels.append(channel)
                    # 与前端漏斗展示同一行文格式（T2），逐轮绑定数字，防止跨轮引用
                    segment = (
                        f"{channel['channel']}：查询 {channel['query_count']} 组 → 召回 {channel['recall_count']}"
                        f" → 抽取 {channel['extracted_count']} → 排重后 {channel['unique_count']}"
                        f" → 详情（完整 {channel['detail_complete']} / 部分 {channel['detail_partial']} / 失败 {channel['detail_failed']}）"
                        f" → 入库新增 {channel['intake_new_count']}（排重命中 {channel['intake_duplicate_count']}）"
                        f" → 评估 {channel['assessed_count']}（高分 {channel['high_score_count']}）"
                    )
                    if attribution_label:
                        segment += f"；0 召回原因：{attribution_label}"
                    channel_segments.append(segment)
                round_index = len(rounds) + 1
                outcome_label = BUSINESS_OUTCOME_LABELS.get(outcome or "")
                headline = outcome_label or ("寻访轮次" if is_sourcing else "非寻访类工作流")
                detail_text = "；".join(channel_segments) if channel_segments else "该轮未记录渠道明细"
                rounds.append(
                    {
                        "round_index": round_index,
                        "workflow_id": workflow_id,
                        "status": str(row["status"] or ""),
                        "is_sourcing": is_sourcing,
                        "business_outcome": outcome,
                        "business_outcome_label": outcome_label or None,
                        "updated_at": str(row["updated_at"] or ""),
                        "channels": channels,
                        "funnel_note": "" if channels else "该轮未记录渠道明细",
                        "summary_text": f"第 {round_index} 轮（{str(row['updated_at'] or '')}）：{headline}；{detail_text}",
                    }
                )
        finally:
            conn.close()
        if not rounds:
            return {}
        return {
            "job_id": job_id,
            "asked_round": asked_round,
            "rounds": rounds[-8:],
            "semantics": (
                "completed_target_met/completed_needs_review/completed_pool_insufficient 均为本轮完成（仅达标情况不同），"
                "只有 failed_technical 是技术失败；每轮 summary_text 是该轮的完整事实，"
                "回答某一轮时只能用该轮的数字，funnel_note 标注未记录渠道明细的轮次不得借用其他轮次数字"
            ),
        }

    def _persist_copilot_focus(
        self,
        session_id: str,
        message: str,
        selected: dict[str, Any],
        *,
        structured: dict[str, Any] | None = None,
        conflicts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        previous = self.get_copilot_focus(session_id) or {}
        structured = dict(structured or {})
        conflicts = list(conflicts or [])
        facts = self._copilot_focus_context_facts(selected)
        mentioned_clients = self._mentioned_client_names(message)
        mentioned_jobs = self._mentioned_jobs_for_copilot(message)
        if not facts and len(mentioned_jobs) == 1:
            facts = self._copilot_focus_context_facts({"type": "job", "id": mentioned_jobs[0].get("id")})

        context_value = dict(previous.get("context") or {"type": "global", "id": None})
        client = str(previous.get("client") or "")
        job = dict(previous.get("job") or {})
        candidate = dict(previous.get("candidate") or {})
        confidence = float(previous.get("confidence") or 0)
        selected_candidate_id: int | None = None
        if str(selected.get("type") or "") == "candidate":
            try:
                selected_candidate_id = int(selected.get("id") or 0) or None
            except (TypeError, ValueError):
                selected_candidate_id = None
        previous_candidate_id = candidate.get("id") or (
            context_value.get("id") if context_value.get("type") == "candidate" else None
        )
        try:
            previous_candidate_id = int(previous_candidate_id or 0) or None
        except (TypeError, ValueError):
            previous_candidate_id = None
        candidate_conflict = bool(
            selected_candidate_id
            and previous_candidate_id
            and selected_candidate_id != previous_candidate_id
        )
        if facts:
            # 页面事实优先：新候选人已入库时直接采用新页面候选人为焦点。
            context_value = dict(facts["context"])
            client = str(facts.get("client") or "")
            job = dict(facts.get("job") or {})
            candidate = dict(facts.get("candidate") or {})
            confidence = 1.0
        elif candidate_conflict:
            # 页面候选人已切换但新候选人未入库：清空候选人焦点并降权
            # （confidence 低于 continuation 阈值 0.7），不再钉住旧候选人。
            context_value = {"type": "global", "id": None}
            job = {}
            candidate = {}
            confidence = 0.4
        elif len(mentioned_clients) == 1 and mentioned_clients[0] != client:
            context_value = {"type": "global", "id": None}
            client = mentioned_clients[0]
            job = {}
            candidate = {}
            confidence = 0.85

        grounding = selected.get("grounding") if isinstance(selected.get("grounding"), dict) else {}
        direction_text = "\n".join([message, json.dumps(grounding, ensure_ascii=False)])
        directions = list(previous.get("directions") or [])
        for label, tokens in (
            ("PC", ("PC", "pc", "电脑")),
            ("服务器", ("服务器", "server", "Server")),
            ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
        ):
            if any(token in direction_text for token in tokens) and label not in directions:
                directions.append(label)

        attachments = list(previous.get("attachments") or [])
        uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
        for item in uploaded.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("file_name") or "").strip()[:180]
            if name and name not in attachments:
                attachments.append(name)
        for name in grounding.get("attachment_names") or []:
            name = str(name or "").strip()[:180]
            if name and name not in attachments:
                attachments.append(name)

        constraints = list(previous.get("constraints") or [])
        for clause in re.split(r"[，。；;！!？?]", message):
            clause = clause.strip()
            if clause and any(token in clause for token in ("不要", "不能", "必须", "只", "先", "不允许")):
                if clause not in constraints:
                    constraints.append(clause[:160])

        evidence = list(previous.get("evidence") or [])
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if facts:
            evidence.append({"source": "current_context", "type": context_value.get("type"), "id": context_value.get("id"), "at": stamp})
        if mentioned_clients:
            evidence.append({"source": "explicit_message", "clients": mentioned_clients[:5], "at": stamp})
        if grounding:
            evidence.append({"source": grounding.get("source") or "workflow_grounding", "job_id": grounding.get("job_id"), "at": stamp})
        if attachments:
            evidence.append({"source": "session_attachment", "files": attachments[-3:], "at": stamp})

        action = self._copilot_action_kind(message)
        if not action and _is_short_ack(message):
            action = str(previous.get("action") or "")
        focus = {
            "context": context_value,
            "client": client,
            "job": job,
            "candidate": candidate,
            "objective": message if action else str(previous.get("objective") or ""),
            "action": action or str(previous.get("action") or ""),
            "directions": directions[-6:],
            "attachments": attachments[-8:],
            "constraints": constraints[-8:],
            "confidence": round(confidence, 3),
        }
        revision = int(previous.get("revision") or 0) + 1
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO agent_copilot_focus
                (session_id,revision,context_type,context_id,client,job_id,candidate_id,action,
                 confidence,focus_json,evidence_json,conflicts_json,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
                ON CONFLICT(session_id) DO UPDATE SET
                    revision=excluded.revision,context_type=excluded.context_type,context_id=excluded.context_id,
                    client=excluded.client,job_id=excluded.job_id,candidate_id=excluded.candidate_id,
                    action=excluded.action,confidence=excluded.confidence,focus_json=excluded.focus_json,
                    evidence_json=excluded.evidence_json,conflicts_json=excluded.conflicts_json,
                    updated_at=datetime('now','localtime')
                """,
                (
                    session_id, revision, context_value.get("type") or "global", context_value.get("id"),
                    client, job.get("id"), candidate.get("id"), focus["action"], focus["confidence"],
                    _dumps(focus), _dumps(evidence[-16:]), _dumps(conflicts[-8:]),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_copilot_focus(session_id) or focus

    def get_copilot_session(self, session_id: str, limit: int = 100) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {"ok": True, "session_id": "", "messages": [], "business_focus": None}
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT role,content,context_type,context_id,structured_json,created_at
                FROM agent_copilot_messages WHERE session_id=? ORDER BY id DESC LIMIT ?
                """,
                (session_id, max(1, min(int(limit or 100), 200))),
            ).fetchall()
            messages = []
            for row in reversed(rows):
                structured = _loads(row["structured_json"], {})
                messages.append(
                    {
                        "role": row["role"], "content": row["content"],
                        "context": {"type": row["context_type"], "id": row["context_id"]},
                        "references": structured.get("references") or [],
                        "suggested_actions": structured.get("suggested_actions") or [],
                        "skill_runs": structured.get("skill_runs") or [],
                        "goal": structured.get("goal"),
                        "workflow": structured.get("workflow"),
                        "plan_summary": structured.get("plan_summary") or [],
                        "business_focus": structured.get("business_focus"),
                        # R9/R12-b：透传持久化的 pending_intent，浮窗恢复会话时可重渲染确认卡
                        #（确认/取消终态是 UI 本地态；过期或已执行的意图确认时会走 409 漂移路径）。
                        "pending_intent": structured.get("pending_intent"),
                        "created_at": row["created_at"],
                    }
                )
            return {
                "ok": True,
                "session_id": session_id,
                "messages": messages,
                "business_focus": self.get_copilot_focus(session_id),
            }
        finally:
            conn.close()

    def list_copilot_sessions(self, limit: int = 30) -> dict[str, Any]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT messages.session_id,
                       COUNT(*) AS message_count,
                       MAX(messages.id) AS latest_id,
                       MAX(messages.created_at) AS updated_at,
                       (SELECT content FROM agent_copilot_messages first_user
                        WHERE first_user.session_id=messages.session_id AND first_user.role='user'
                        ORDER BY first_user.id LIMIT 1) AS title,
                       (SELECT content FROM agent_copilot_messages latest_message
                        WHERE latest_message.session_id=messages.session_id
                        ORDER BY latest_message.id DESC LIMIT 1) AS preview,
                       (SELECT context_type FROM agent_copilot_messages latest_context
                        WHERE latest_context.session_id=messages.session_id
                        ORDER BY latest_context.id DESC LIMIT 1) AS context_type,
                       (SELECT context_id FROM agent_copilot_messages latest_context
                        WHERE latest_context.session_id=messages.session_id
                        ORDER BY latest_context.id DESC LIMIT 1) AS context_id
                FROM agent_copilot_messages messages
                GROUP BY messages.session_id
                ORDER BY latest_id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 30), 100)),),
            ).fetchall()
            sessions = []
            for row in rows:
                item = _row(row)
                item["title"] = str(item.get("title") or "未命名对话")[:80]
                item["preview"] = " ".join(str(item.get("preview") or "").split())[:120]
                sessions.append(item)
            return {"ok": True, "sessions": sessions}
        finally:
            conn.close()

    def _copilot_conversation_history(self, session_id: str, limit: int = 16) -> list[dict[str, str]]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT role,content FROM agent_copilot_messages
                WHERE session_id=? AND role IN ('user','assistant')
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, max(2, min(int(limit or 16), 24))),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"role": str(row["role"]), "content": str(row["content"] or "")[:1800]}
            for row in reversed(rows)
        ]

    def _copilot_session_business_evidence(self, session_id: str, limit: int = 8) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {"clients": [], "jobs": [], "directions": [], "attachment_names": []}
        focus = self.get_copilot_focus(session_id) or {}
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT role,content,context_type,context_id,structured_json
                FROM agent_copilot_messages
                WHERE session_id=? AND role IN ('user','assistant')
                ORDER BY id DESC LIMIT ?
                """,
                (session_id, max(2, min(int(limit or 8), 16))),
            ).fetchall()
        finally:
            conn.close()

        text_parts: list[str] = []
        strong_client_parts: list[str] = []
        job_ids: list[int] = []
        attachment_names: list[str] = [str(item) for item in focus.get("attachments") or [] if str(item).strip()]

        def add_job_id(value: Any) -> None:
            try:
                job_id = int(value or 0)
            except (TypeError, ValueError):
                return
            if job_id > 0 and job_id not in job_ids:
                job_ids.append(job_id)

        focus_client = str(focus.get("client") or "").strip()
        if focus_client:
            strong_client_parts.append(focus_client)
        focus_job = focus.get("job") if isinstance(focus.get("job"), dict) else {}
        add_job_id(focus_job.get("id"))

        for row in rows:
            content = str(row["content"] or "")[:2400]
            if content:
                text_parts.append(content)
            if str(row["context_type"] or "") == "job":
                add_job_id(row["context_id"])
            structured = _loads(row["structured_json"], {})
            for item in structured.get("mentioned_jobs") or []:
                if isinstance(item, dict):
                    add_job_id(item.get("id"))
                    text_parts.extend([str(item.get("client") or ""), str(item.get("job") or "")])
                    strong_client_parts.append(str(item.get("client") or ""))
            for item in structured.get("references") or []:
                if isinstance(item, dict) and item.get("type") == "job":
                    add_job_id(item.get("id"))
                    text_parts.extend([str(item.get("label") or ""), str(item.get("subtitle") or "")])
                    strong_client_parts.append(str(item.get("subtitle") or ""))
            uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
            for item in uploaded.get("items") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("file_name") or "")[:180]
                if name and name not in attachment_names:
                    attachment_names.append(name)
                text_parts.extend([name, str(item.get("extracted_text") or "")[:18000]])

        evidence_text = "\n".join(part for part in text_parts if part)
        strong_clients = self._mentioned_client_names("\n".join(strong_client_parts))
        clients = strong_clients or self._mentioned_client_names(evidence_text)
        directions = list(focus.get("directions") or [])
        directions.extend(
            label
            for label, tokens in (
                ("PC", ("PC", "pc", "电脑")),
                ("服务器", ("服务器", "server", "Server")),
                ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
            )
            if any(token in evidence_text for token in tokens) and label not in directions
        )
        jobs: list[dict[str, Any]] = []
        if job_ids:
            conn = self._connect()
            try:
                placeholders = ",".join("?" for _ in job_ids)
                found = conn.execute(
                    f"""
                    SELECT j.id,c.name AS client,j.title AS job,j.status
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    WHERE j.id IN ({placeholders})
                    """,
                    job_ids,
                ).fetchall()
                indexed = {int(row["id"]): _row(row) for row in found}
                jobs = [indexed[job_id] for job_id in job_ids if job_id in indexed]
            finally:
                conn.close()
        return {
            "clients": clients,
            "jobs": jobs,
            "directions": directions,
            "attachment_names": attachment_names,
        }

    def _ground_copilot_goal(
        self, message: str, selected: dict[str, Any], session_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        job_write = bool(
            any(token in message for token in ("更新", "拆分", "拆成", "分成", "新建", "建立", "归档", "关闭"))
            and any(token in message for token in ("岗位", "职位", "岗位库"))
        )
        sourcing = any(token in message for token in ("补池", "寻访", "找人", "候选人", "人选")) and any(
            token in message for token in ("补充", "继续", "再找", "搜索", "搜", "找", "寻访", "多渠道")
        )
        publishing = any(token in message for token in ("发布", "上架")) and any(
            token in message for token in ("岗位", "职位")
        )
        job_bound = job_write or sourcing or publishing
        if not job_bound:
            return dict(selected), {}, ""

        evidence = self._copilot_session_business_evidence(session_id)
        current_clients = self._mentioned_client_names(message)
        client_candidates = current_clients or evidence["clients"]
        client_candidates = list(dict.fromkeys(client_candidates))
        selected_job: dict[str, Any] = {}
        if selected.get("type") == "job" and selected.get("id"):
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT j.id,c.name AS client,j.title AS job,j.status
                    FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                    """,
                    (int(selected["id"]),),
                ).fetchone()
                selected_job = _row(row)
            finally:
                conn.close()
        if selected_job:
            client_candidates = [str(selected_job["client"])]

        target_job = selected_job
        current_jobs = self._mentioned_jobs_for_copilot(message)
        if not target_job and len(current_jobs) == 1:
            target_job = current_jobs[0]
        client = client_candidates[0] if len(client_candidates) == 1 else ""
        recent_jobs = [item for item in evidence["jobs"] if not client or item.get("client") == client]
        archive_reference = any(token in message for token in ("之前", "那个", "原来", "旧", "没拆分", "未拆分", "合并"))
        if not target_job and client and archive_reference:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT j.id,c.name AS client,j.title AS job,j.status
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    WHERE c.name=? AND j.title LIKE '%技术市场%'
                    ORDER BY j.id DESC
                    """,
                    (client,),
                ).fetchall()
            finally:
                conn.close()
            merged = [
                _row(row) for row in rows
                if any(token in str(row["job"] or "") for token in ("或PC", "服务器或", "/服务器", "／服务器"))
            ]
            if len(merged) == 1:
                target_job = merged[0]
        if not target_job and len(recent_jobs) == 1:
            target_job = recent_jobs[0]
        if target_job:
            client = str(target_job.get("client") or client)

        current_direction_text = message
        directions = [
            label
            for label, tokens in (
                ("PC", ("PC", "pc", "电脑")),
                ("服务器", ("服务器", "server", "Server")),
                ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
            )
            if any(token in current_direction_text for token in tokens)
        ]
        continuation = archive_reference or _is_short_ack(message) or any(token in message for token in ("这个", "上述", "按刚才", "按之前", "按此"))
        if continuation:
            directions = list(dict.fromkeys([*directions, *evidence["directions"]]))

        missing: list[str] = []
        if not client:
            missing.append("客户")
        if any(token in message for token in ("归档", "关闭")) and not target_job:
            missing.append("要归档的岗位")
        if (sourcing or publishing) and not target_job:
            missing.append("唯一岗位")
        split_requested = any(token in message for token in ("拆分", "拆成", "分成", "三个", "分别"))
        if split_requested and not directions:
            missing.append("拆分方向")
        if missing:
            known = ""
            if client_candidates:
                known = f"当前识别到客户候选：{'、'.join(client_candidates[:3])}。"
            clarification = (
                f"结论：还不能建立写入计划，缺少{'、'.join(missing)}。\n\n"
                f"{known}请补充{'、'.join(missing)}后再执行。"
            )
            return dict(selected), {}, clarification

        grounded = dict(selected)
        if target_job:
            grounded.update({"type": "job", "id": int(target_job["id"]), "page": "positions", "filters": {}})
        goal_inputs = {
            "client": client,
            "directions": directions,
            "archive_legacy": bool(any(token in message for token in ("归档", "旧", "没拆分", "未拆分", "合并"))),
        }
        grounded["goal_inputs"] = goal_inputs
        grounded["goal_grounding"] = {
            "source": "current_context" if selected_job else "recent_session_evidence",
            "client": client,
            "job_id": int(target_job["id"]) if target_job else None,
            "job": target_job.get("job") if target_job else "",
            "directions": directions,
            "attachment_names": evidence["attachment_names"][:3],
            "validated_against_v3": True,
        }
        return grounded, goal_inputs, ""

    def _pending_strategy_clarification(self, session_id: str) -> dict[str, Any]:
        """本会话最近一条四锚点提问清单记录；仅 status=pending 时返回（S4-1）。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return {}
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT structured_json FROM agent_copilot_messages
                WHERE session_id=? AND role='assistant' AND structured_json LIKE '%strategy_clarification%'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        data = _loads(row["structured_json"], {}) if row else {}
        pending = data.get("strategy_clarification") if isinstance(data, dict) else None
        if isinstance(pending, dict) and pending.get("status") == "pending" and pending.get("job_id"):
            return pending
        return {}

    def _sourcing_strategy_gate(
        self, goal_request: str, goal_context: dict[str, Any], *, floating_compact: bool = False
    ) -> dict[str, Any]:
        """S4-1 L3 提问门控（PRD §1 最高优先单点）：四锚点缺失 ≥2 且知识库无对应
        岗位原型时，不创建寻访工作流，改为输出四锚点提问清单。仅作用于寻访类目标。"""
        text = str(goal_request or "").lower()
        sourcing_like = any(token in text for token in ("补充", "补池", "寻访", "找人", "搜索", "搜人", "再找", "继续找", "多找")) or any(
            token in text for token in ("人选", "候选人")
        )
        if not sourcing_like or goal_context.get("type") != "job" or not goal_context.get("id"):
            return {"action": "proceed"}
        try:
            job = self.capability_runtime._job(goal_context)
        except ValueError:
            return {"action": "proceed"}
        archetype, match_trace = strategy_v2.match_job_archetype(job.get("client"), job.get("title"))
        classification = strategy_v2.classify_strategy_input(job, archetype=archetype)
        classification["trace"] = [*match_trace, *classification["trace"]]
        if archetype or len(classification.get("missing_anchors") or []) < 2:
            return {"action": "proceed"}
        answer = strategy_v2.build_clarification_answer(job, classification, floating_compact=floating_compact)
        pending = {
            "status": "pending",
            "job_id": int(goal_context["id"]),
            "client": str(job.get("client") or ""),
            "job": str(job.get("title") or ""),
            "original_objective": " ".join(str(goal_request or "").split()),
            "input_level": str(classification.get("input_level") or "L3"),
            "missing_anchors": list(classification.get("missing_anchors") or []),
            "questions": strategy_v2.build_anchor_questions(job, classification),
            "trace": list(classification.get("trace") or [])[-12:],
        }
        return {"action": "ask", "answer": answer, "pending": pending}

    def _mentioned_client_names(self, message: str) -> list[str]:
        text = " ".join(str(message or "").split())
        if not text:
            return []
        conn = self._connect()
        try:
            rows = conn.execute("SELECT name FROM clients ORDER BY length(name) DESC, id").fetchall()
        finally:
            conn.close()
        return [str(row["name"]) for row in rows if str(row["name"] or "") in text]

    def store_memory(
        self, *, scope_type: str, scope_id: Any, memory_type: str, content: str,
        source_type: str, source_id: Any = None, confidence: float = 1.0,
    ) -> dict[str, Any]:
        scope_type = str(scope_type or "").strip().lower()
        if scope_type not in {"global", "client", "job", "candidate"}:
            raise ValueError("未知记忆范围")
        content = " ".join(str(content or "").split())
        if not content:
            raise ValueError("记忆内容不能为空")
        normalized_scope_id = "" if scope_type == "global" else str(scope_id or "").strip()
        if scope_type != "global" and not normalized_scope_id:
            raise ValueError("非全局记忆必须有 scope_id")
        content_hash = hashlib.sha256(
            f"{scope_type}|{normalized_scope_id}|{memory_type}|{content}".encode("utf-8")
        ).hexdigest()
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT id,status FROM agent_memories WHERE content_hash=?", (content_hash,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE agent_memories SET status='active',confidence=?,updated_at=datetime('now','localtime'),revoked_at=NULL WHERE id=?",
                    (max(0.0, min(float(confidence), 1.0)), existing["id"]),
                )
                memory_id = int(existing["id"])
                deduplicated = True
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO agent_memories
                    (scope_type,scope_id,memory_type,content,source_type,source_id,confidence,content_hash)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        scope_type, normalized_scope_id or None, str(memory_type or "fact"), content,
                        str(source_type or "manual"), str(source_id or "") or None,
                        max(0.0, min(float(confidence), 1.0)), content_hash,
                    ),
                )
                memory_id = int(cursor.lastrowid)
                deduplicated = False
            conn.commit()
            return {"ok": True, "memory_id": memory_id, "deduplicated": deduplicated}
        finally:
            conn.close()

    def _ensure_sourcing_attribution(self, conn: sqlite3.Connection, job_candidate_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM agent_sourcing_attributions WHERE job_candidate_id=? ORDER BY id",
            (int(job_candidate_id),),
        ).fetchall()
        if rows:
            return [_row(row) for row in rows]
        relation = conn.execute(
            """
            SELECT jc.id,jc.job_id,jc.source_candidate_id,c.id AS candidate_id,c.source,c.notes
            FROM job_candidates jc
            LEFT JOIN candidates c ON CAST(c.id AS TEXT)=jc.source_candidate_id
            WHERE jc.id=?
            """,
            (int(job_candidate_id),),
        ).fetchone()
        if relation is None:
            return []
        event = conn.execute(
            """
            SELECT raw_json,source_table FROM candidate_events
            WHERE job_candidate_id=? AND event_type IN ('search_shortlisted','xsaas_search_shortlisted')
            ORDER BY COALESCE(event_time,''),id LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone()
        raw = _loads(event["raw_json"], {}) if event else {}
        notes = str(relation["notes"] or "")
        match = re.search(r"(?:^|[｜|])query=([^｜|]+)", notes)
        query = str(raw.get("source_query") or raw.get("query") or (match.group(1).strip() if match else "") or "未记录关键词")
        channel = str(raw.get("channel") or raw.get("source") or relation["source"] or "unknown").lower()
        source_round = str(raw.get("source_round") or raw.get("round") or "")
        source_purpose = ""
        workflow_id = ""
        strategy_hash = ""
        strategy_model = ""
        strategy_rows = conn.execute(
            """
            SELECT s.workflow_id,s.output_json
            FROM agent_workflow_steps s
            JOIN agent_workflow_context wc ON wc.workflow_id=s.workflow_id
            WHERE s.capability_id='search_strategy' AND s.status='completed'
              AND json_extract(wc.context_json,'$.type')='job'
              AND CAST(json_extract(wc.context_json,'$.id') AS INTEGER)=?
            ORDER BY s.updated_at DESC,s.id DESC LIMIT 20
            """,
            (int(relation["job_id"]),),
        ).fetchall()
        for strategy_row in strategy_rows:
            output = _loads(strategy_row["output_json"], {})
            strategy = output.get("strategy") if isinstance(output.get("strategy"), dict) else {}
            channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
            matched_entry = next(
                (
                    entry for entry in channels.get(channel, [])
                    if isinstance(entry, dict) and str(entry.get("query") or "").strip() == query
                ),
                None,
            )
            if matched_entry:
                workflow_id = str(strategy_row["workflow_id"] or "")
                strategy_hash = hashlib.sha256(_dumps(strategy).encode("utf-8")).hexdigest()
                generation = strategy.get("generation") if isinstance(strategy.get("generation"), dict) else {}
                strategy_model = str(generation.get("model") or "")
                source_round = str(matched_entry.get("round") or source_round)
                source_purpose = str(matched_entry.get("purpose") or "")
                break
        cursor = conn.execute(
            """
            INSERT INTO agent_sourcing_attributions
            (job_candidate_id,candidate_id,job_id,workflow_id,strategy_hash,strategy_model,
             channel,source_query,source_round,source_purpose)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(job_candidate_id), int(relation["candidate_id"] or 0) or None, int(relation["job_id"]),
                workflow_id or None, strategy_hash or None, strategy_model or None,
                channel, query, source_round, source_purpose,
            ),
        )
        row = conn.execute("SELECT * FROM agent_sourcing_attributions WHERE id=?", (cursor.lastrowid,)).fetchone()
        return [_row(row)] if row else []

    def record_sourcing_business_signal(
        self, job_candidate_id: int, signal_type: str, *, actor_type: str,
        note: str = "", source_type: str = "business_event", source_id: Any = None,
    ) -> dict[str, Any]:
        signal_type = str(signal_type or "").strip()
        if signal_type not in SOURCING_SIGNAL_WEIGHTS:
            raise ValueError("未知寻访学习信号")
        dedupe_key = f"{source_type}:{source_id}" if source_id not in (None, "") else f"{job_candidate_id}:{signal_type}:{actor_type}"
        conn = self._connect()
        try:
            attributions = self._ensure_sourcing_attribution(conn, int(job_candidate_id))
            if not attributions:
                conn.commit()
                return {"ok": True, "recorded": False, "reason": "没有可归因的寻访关键词"}
            inserted = 0
            for attribution in attributions:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_sourcing_feedback
                    (dedupe_key,attribution_id,job_candidate_id,job_id,signal_type,actor_type,weight,note,source_type,source_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"{dedupe_key}:{attribution['id']}", attribution["id"], int(job_candidate_id), attribution["job_id"],
                        signal_type, actor_type, SOURCING_SIGNAL_WEIGHTS[signal_type], str(note or "")[:800],
                        source_type, str(source_id or "") or None,
                    ),
                )
                inserted += int(cursor.rowcount > 0)
            updated_memories: list[int] = []
            for attribution in attributions:
                aggregate = conn.execute(
                    """
                    SELECT COUNT(*) AS signal_count,COUNT(DISTINCT sf.job_candidate_id) AS candidate_count,
                           ROUND(SUM(sf.weight),2) AS score,
                           SUM(sf.signal_type='review_pass') AS review_pass,
                           SUM(sf.signal_type='contacted') AS contacted,
                           SUM(sf.signal_type='recommended') AS recommended,
                           SUM(sf.signal_type='stopped') AS stopped,
                           SUM(sf.signal_type IN ('client_approved','client_interview','client_offer','client_hired')) AS client_positive,
                           SUM(sf.signal_type='client_rejected') AS client_rejected
                    FROM agent_sourcing_feedback sf
                    JOIN agent_sourcing_attributions sa ON sa.id=sf.attribution_id
                    WHERE sa.job_id=? AND sa.channel=? AND sa.source_query=?
                    """,
                    (attribution["job_id"], attribution["channel"], attribution["source_query"]),
                ).fetchone()
                job_row = conn.execute(
                    "SELECT c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                    (attribution["job_id"],),
                ).fetchone()
                if not job_row:
                    continue
                score = float(aggregate["score"] or 0)
                verdict = "优先保留" if score >= 3 else "有正向证据" if score > 0 else "建议降权" if score < 0 else "继续观察"
                content = (
                    f"{job_row['client']}/{job_row['job']} 寻访经验：{attribution['channel']} 关键词“{attribution['source_query']}”{verdict}；"
                    f"复核通过 {int(aggregate['review_pass'] or 0)}，联系 {int(aggregate['contacted'] or 0)}，"
                    f"推荐 {int(aggregate['recommended'] or 0)}，停止 {int(aggregate['stopped'] or 0)}，"
                    f"客户正向 {int(aggregate['client_positive'] or 0)}，客户否决 {int(aggregate['client_rejected'] or 0)}，经验分 {score:g}。"
                )
                memory_source_id = f"job:{attribution['job_id']}|{attribution['channel']}|{attribution['source_query']}"
                content_hash = hashlib.sha256(f"job|{attribution['job_id']}|sourcing_performance|{content}".encode("utf-8")).hexdigest()
                existing = conn.execute(
                    "SELECT id FROM agent_memories WHERE source_type='sourcing_performance' AND source_id=?",
                    (memory_source_id,),
                ).fetchone()
                confidence = min(1.0, 0.58 + 0.07 * int(aggregate["signal_count"] or 0))
                if existing:
                    conn.execute(
                        """
                        UPDATE agent_memories SET content=?,confidence=?,content_hash=?,status='active',
                          revoked_at=NULL,updated_at=datetime('now','localtime') WHERE id=?
                        """,
                        (content, confidence, content_hash, existing["id"]),
                    )
                    memory_id = int(existing["id"])
                else:
                    cursor = conn.execute(
                        """
                        INSERT INTO agent_memories
                        (scope_type,scope_id,memory_type,content,source_type,source_id,confidence,content_hash)
                        VALUES ('job',?,'sourcing_performance',?,'sourcing_performance',?,?,?)
                        """,
                        (str(attribution["job_id"]), content, memory_source_id, confidence, content_hash),
                    )
                    memory_id = int(cursor.lastrowid)
                updated_memories.append(memory_id)
            conn.commit()
            return {
                "ok": True, "recorded": bool(inserted), "inserted": inserted,
                "signal_type": signal_type, "weight": SOURCING_SIGNAL_WEIGHTS[signal_type],
                "attributions": attributions, "memory_ids": sorted(set(updated_memories)),
            }
        finally:
            conn.close()

    def search_memories(
        self, query: str = "", *, context_type: str = "global", context_id: Any = None,
        client: str = "", job: str = "", limit: int | None = None,
    ) -> dict[str, Any]:
        if not self.config["memory"].get("enabled") or self.config["memory"].get("mode") == "off":
            return {"ok": True, "mode": "off", "memories": []}
        limit = max(1, min(int(limit or self.config["memory"]["result_limit"]), 20))
        scopes: list[tuple[str, str]] = [("global", "")]
        if client:
            scopes.append(("client", str(client)))
        if job:
            scopes.append(("job", str(job)))
        if context_type == "job" and context_id:
            scopes.append(("job", str(context_id)))
        if context_type == "candidate" and context_id:
            scopes.append(("candidate", str(context_id)))
        clauses = ["(scope_type=? AND COALESCE(scope_id,'')=?)" for _ in scopes]
        params: list[Any] = [value for pair in scopes for value in pair]
        sql = f"SELECT * FROM agent_memories WHERE status='active' AND ({' OR '.join(clauses)})"
        cleaned_query = " ".join(str(query or "").split())
        sql += " ORDER BY confidence DESC,updated_at DESC LIMIT ?"
        params.append(max(limit, int(self.config["memory"]["candidate_limit"])))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            candidates = [_row(row) for row in rows]
            if cleaned_query:
                query_chars = {char for char in cleaned_query if not char.isspace()}
                for item in candidates:
                    content_chars = {char for char in str(item.get("content") or "") if not char.isspace()}
                    item["relevance"] = round(len(query_chars & content_chars) / max(1, len(query_chars)), 4)
                candidates.sort(
                    key=lambda item: (float(item.get("relevance") or 0), float(item.get("confidence") or 0), int(item.get("id") or 0)),
                    reverse=True,
                )
            semantic_reranked = False
            conflict_ids: list[int] = []
            if cleaned_query and candidates and self.llm.model != "unavailable":
                try:
                    ranked = self.llm.rank_memories(cleaned_query, sanitize_payload(candidates))
                    ordered_ids = [int(value) for value in ranked.get("ordered_ids") or []]
                    order = {memory_id: index for index, memory_id in enumerate(ordered_ids)}
                    candidates.sort(key=lambda item: order.get(int(item["id"]), len(order)))
                    conflict_ids = [int(value) for value in ranked.get("conflict_ids") or []]
                    semantic_reranked = True
                except Exception:
                    semantic_reranked = False
            memories = candidates[:limit]
            ids = [int(item["id"]) for item in memories]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE agent_memories SET hit_count=hit_count+1,last_used_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                """
                INSERT INTO agent_memory_recalls
                (query_hash,context_type,context_id,memory_ids_json,mode,adopted,conflict)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    hashlib.sha256(cleaned_query.encode("utf-8")).hexdigest(), context_type,
                    str(context_id or "") or None, _dumps(ids), self.config["memory"]["mode"],
                    1 if self.config["memory"]["mode"] == "active" and ids else 0,
                    len([memory_id for memory_id in ids if memory_id in conflict_ids]),
                ),
            )
            conn.commit()
            for item in memories:
                item.pop("content_hash", None)
            return {
                "ok": True, "mode": self.config["memory"]["mode"], "memories": memories,
                "semantic_reranked": semantic_reranked, "conflict_ids": conflict_ids,
            }
        finally:
            conn.close()

    def list_memories(self, *, status: str = "active", scope_type: str = "", limit: int = 50) -> dict[str, Any]:
        if status not in {"active", "revoked", "all"}:
            raise ValueError("未知记忆状态")
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if scope_type:
            clauses.append("scope_type=?")
            params.append(scope_type)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT id,scope_type,scope_id,memory_type,content,source_type,source_id,confidence,status,hit_count,last_used_at,created_at FROM agent_memories {where} ORDER BY id DESC LIMIT ?",
                (*params, max(1, min(int(limit or 50), 200))),
            ).fetchall()
            return {"ok": True, "memories": [_row(row) for row in rows]}
        finally:
            conn.close()

    def revoke_memory(self, memory_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE agent_memories SET status='revoked',revoked_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=? AND status='active'",
                (int(memory_id),),
            )
            if conn.total_changes != 1:
                raise ValueError("记忆不存在或已撤销")
            conn.commit()
            return {"ok": True, "memory_id": int(memory_id), "status": "revoked"}
        finally:
            conn.close()

    def _route_copilot_skills(self, message: str, context: dict[str, Any]) -> list[str]:
        routes: list[str] = []
        normalized = message.lower()

        def add(skill_id: str) -> None:
            spec = self.skills.get(skill_id)
            if spec and context["type"] in spec.supported_contexts and skill_id not in routes:
                routes.append(skill_id)

        if "opencli" in normalized:
            add("opencli_usage")
            if any(token in message for token in ("当前页面", "浏览器", "网页", "Chrome", "chrome", "页面状态")):
                add("opencli_browser_read")
        if (
            context["type"] == "candidate" and "猎聘" in message
            and any(token in message for token in ("抓取", "补全", "补充", "读取", "简历"))
        ):
            return ["liepin_resume_capture"]

        direct_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("job_intake", ("岗位接入", "录入岗位", "新岗位", "岗位需求", "需求接入")),
            ("jd_calibration", ("jd校准", "JD校准", "岗位校准", "校准岗位", "硬门槛")),
            ("job_library_update", ("更新岗位", "拆分岗位", "新建岗位", "建立岗位", "岗位库更新")),
            ("job_diagnosis", ("岗位", "风险", "漏斗", "诊断", "驾驶舱", "看板")),
            ("talent_pool_search", ("人才库", "历史人才", "存量人选", "库里", "搜库", "检索人才")),
            ("search_strategy", ("寻访策略", "搜索策略", "怎么找", "搜人策略", "目标公司", "关键词")),
            ("job_publish_prepare", ("发布准备", "岗位发布准备", "准备发布", "发布草稿", "上架准备")),
            ("candidate_assessment", ("评估", "匹配", "判断", "合不合适", "适配", "推荐吗")),
            ("verification_plan", ("核验", "验证", "缺什么", "待核验", "核实", "问题清单")),
            ("communication_draft", ("草稿", "怎么联系", "沟通话术", "怎么聊", "私聊话术")),
            ("resume_export", ("导出简历", "简历导出", "结构化简历", "简历文档")),
            ("candidate_batch_assessment", ("批量评估", "批量判断", "批量匹配", "评估这一批")),
            ("matching_report", ("匹配报告", "人岗匹配报告", "匹配分析", "人岗分析")),
            ("recommendation_report", ("推荐报告", "嘉驰推荐", "推荐材料", "候选人报告")),
            ("reply_triage", ("回复识别", "回复分流", "回复待办", "回复处理", "回复 triage")),
            ("communication_draft_batch", ("批量草稿", "批量话术", "批量沟通", "草稿这一批")),
            ("outreach_prepare", ("触达准备", "准备触达", "锁定触达", "触达草稿", "外呼准备")),
            ("interview_followup", ("面试反馈", "面试跟进", "面试纪要", "客户反馈")),
            ("salary_verification", ("薪资核验", "薪资验证", "薪资报告", "薪资证明")),
            ("salary_negotiation", ("谈薪", "薪资谈判", "谈薪风险", "薪资风险")),
            ("decision_coaching", ("决策辅导", "候选人决策", "决策建议", "offer决策")),
            ("onboarding_followup", ("入职跟进", "onboarding", "入职计划", "入职事项")),
            ("project_retrospective", ("项目复盘", "复盘", "结案总结", "项目总结")),
        )
        for skill_id, tokens in direct_rules:
            if _contains_any(message, tokens):
                add(skill_id)
        return routes[: max(1, int(self.config["runtime"]["copilot_max_skills"]))]

    def copilot(
        self,
        message: str,
        *,
        session_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = " ".join(str(message or "").split())
        if not normalized:
            raise ValueError("请输入问题")
        stable_session_id = str(session_id or "").strip() or f"copilot_{secrets.token_hex(6)}"
        with self._copilot_locks_guard:
            session_lock = self._copilot_session_locks.setdefault(stable_session_id, threading.RLock())
        with session_lock:
            return self._copilot_impl(normalized, session_id=stable_session_id, context=context)

    def _copilot_impl(
        self,
        message: str,
        *,
        session_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = " ".join(str(message or "").split())
        if not message:
            raise ValueError("请输入问题")
        raw_context = dict(context or {})
        floating_compact = str(raw_context.get("display_mode") or "").strip() == "floating_compact" or str(raw_context.get("source") or "").strip() == "asa_floating"
        selected = self._normalize_copilot_context(raw_context)
        selected, focus_conflicts = self._copilot_context_from_focus(session_id, message, selected)
        existing_focus = self.get_copilot_focus(session_id)
        conversation_history = self._copilot_conversation_history(session_id)
        last_assistant_message = next(
            (
                str(item.get("content") or "")
                for item in reversed(conversation_history)
                if item.get("role") == "assistant"
            ),
            "",
        )
        explicit_sourcing_confirmation = bool(
            re.fullmatch(
                r"(?:可以|确认|现在)?(?:开始|继续|重新|执行)?(?:搜索|寻访)(?:吧|了|可以)?",
                message,
                re.I,
            )
        )
        short_sourcing_confirmation = bool(
            _is_short_ack(message)
            and any(token in last_assistant_message for token in ("重新搜索", "开始搜索", "执行多渠道寻访", "按新条件"))
        )
        focus_context = (
            existing_focus.get("context")
            if existing_focus and isinstance(existing_focus.get("context"), dict)
            else {}
        )
        if (
            (explicit_sourcing_confirmation or short_sourcing_confirmation)
            and selected.get("type") not in {"job", "candidate"}
            and focus_context.get("type") == "job"
            and focus_context.get("id")
            and float(existing_focus.get("confidence") or 0) >= 0.7
        ):
            selected = {
                "type": "job",
                "id": int(focus_context["id"]),
                "page": "positions",
                "filters": {},
            }
        sourcing_focus = bool(
            existing_focus
            and existing_focus.get("action") == "candidate_sourcing"
            and selected.get("type") == "job"
            and selected.get("id")
            and float(existing_focus.get("confidence") or 0) >= 0.7
        )
        auto_start_sourcing = sourcing_focus and (
            explicit_sourcing_confirmation or short_sourcing_confirmation
        )
        goal_request = message
        if auto_start_sourcing:
            prior_objective = str(existing_focus.get("objective") or "").strip()
            prior_is_sourcing = bool(
                any(token in prior_objective for token in ("补池", "寻访", "找人", "搜索人选", "搜索候选人"))
                or (
                    any(token in prior_objective for token in ("人选", "候选人"))
                    and any(
                        token in prior_objective
                        for token in ("补充", "再找", "继续找", "找人", "搜索", "搜人")
                    )
                )
            )
            if not prior_is_sourcing:
                prior_objective = next(
                    (
                        str(item.get("content") or "").strip()
                        for item in reversed(conversation_history)
                        if item.get("role") == "user"
                        and any(token in str(item.get("content") or "") for token in ("人选", "候选人"))
                        and any(
                            token in str(item.get("content") or "")
                            for token in ("补充", "补池", "再找", "继续找", "找人", "搜索", "搜人", "寻访")
                        )
                    ),
                    "",
                )
            if not prior_objective:
                focus_job = existing_focus.get("job") if isinstance(existing_focus.get("job"), dict) else {}
                target = str(focus_job.get("title") or "当前岗位")
                prior_objective = f"继续为{target}补充候选人"
            refinements = [
                str(item.get("content") or "").strip()
                for item in conversation_history
                if item.get("role") == "user"
                and any(
                    token in str(item.get("content") or "")
                    for token in ("放宽", "年限", "职级", "行业", "方向", "条件", "要求")
                )
            ][-5:]
            adjustment = "；".join(dict.fromkeys(item for item in refinements if item))
            goal_request = prior_objective
            if adjustment:
                goal_request += f"。本轮条件调整：{adjustment}"
            confirmed_detail = next(
                (
                    " ".join(str(item.get("content") or "").split())[:360]
                    for item in reversed(conversation_history)
                    if item.get("role") == "assistant"
                    and any(token in str(item.get("content") or "") for token in ("年限", "职级"))
                    and any(
                        token in str(item.get("content") or "")
                        for token in ("年以上", "资深工程师", "主管", "经理", "总监")
                    )
                ),
                "",
            )
            if confirmed_detail:
                goal_request += f"。已确认细化条件：{confirmed_detail}"
            goal_request += "。确认执行多渠道寻访"
        mentioned_clients = self._mentioned_client_names(message)
        primary_client = mentioned_clients[0] if mentioned_clients else str((existing_focus or {}).get("client") or "该客户")
        forced_answer = None
        if re.search(r"(?:性子|性质)结构", message) and not any(token in message for token in ("公司性质", "组织结构", "薪资结构")):
            forced_answer = (
                f"你是想问{primary_client}的公司性质/组织结构，还是薪资结构？\n\n"
                "请确认一个方向，我再按对应证据回答。"
            )
        goal_workflow = None
        goal_patterns = (
            r"(?:补充|补池|寻访|找|搜索|搜|再找|继续找)\s*(?:些|一批)?\s*\d*\s*(?:位|个|名|人)?\s*(?:合适|匹配|合适的)?(?:人选|候选人)",
            r"(?:多渠道|猎聘|X-?SaaS|x-?saas).*(?:寻访|搜索|找人|找候选人|补池)",
            r"(?:更新|拆分|拆成|分成|新建|建立).*(?:岗位|职位|岗位库)",
            r"(?:岗位|职位).*(?:更新|拆分|拆成|分成|新建|建立)",
            r"(?:归档|关闭).*(?:岗位|职位)",
            r"(?:岗位|职位).*(?:归档|关闭)",
            r"(?:整理|生成|制作).*(?:推荐报告|谈薪|薪资|面试反馈|入职)",
            r"推进今天.*(?:回复|人选|待办)",
            r"(?:发布|上架|发).*岗位",
            r"(?:触达|发送|开聊|沟通).*(?:候选人|人选|这一批|当前)",
            r"推荐给客户",
            r"(?:客户推荐|提交客户|推给客户)",
            r"(?:身份合并|合并人选|同一人|重复人选)",
            r"(?:确认|锁定).*(?:offer|Offer|入职条件)",
            r"(?:记住|沉淀|保存).*(?:规则|经验|记忆)",
        )
        # R9：CoreService 判定该消息是待确认的候选人写入意图时置
        # suppress_goal_intent，此处不再路由工作流级目标（防止同一条
        # 消息既产生确认卡片又建立/启动工作流）。
        suppress_goal_intent = bool(raw_context.get("suppress_goal_intent"))
        # S4-1 L3 提问清单门控：上一轮若因四锚点缺失 ≥2 且无岗位原型而出过提问
        # 清单，本轮顾问“直接搜/先搜”类回复视为放行（consultant_override，推断项
        # 保持 inferred+confidence），锚点类回复并入策略上下文；两者都还原原始
        # 寻访目标继续建工作流。
        pending_clarification = self._pending_strategy_clarification(session_id)
        strategy_gate_clarification: dict[str, Any] = {}
        strategy_gate_pending_record: dict[str, Any] = {}
        strategy_gate_force_goal = False
        if pending_clarification and not suppress_goal_intent:
            pending_job_id = int(pending_clarification.get("job_id") or 0)
            selected_job_id = int(selected["id"]) if selected.get("type") == "job" and selected.get("id") else 0
            focused_job_id = int(focus_context["id"]) if focus_context.get("type") == "job" and focus_context.get("id") else 0
            targeted_job_id = selected_job_id or focused_job_id
            same_job = not targeted_job_id or targeted_job_id == pending_job_id
            override_reply = strategy_v2.is_direct_search_override(message) or (explicit_sourcing_confirmation and same_job)
            new_goal_intent = any(re.search(pattern, message, re.I) for pattern in goal_patterns)
            if override_reply and same_job:
                strategy_gate_clarification = {
                    "consultant_override": True,
                    "asked_questions": True,
                    "input_level": str(pending_clarification.get("input_level") or "L3"),
                    "missing_anchors": list(pending_clarification.get("missing_anchors") or []),
                    "original_objective": str(pending_clarification.get("original_objective") or ""),
                }
                if not auto_start_sourcing:
                    goal_request = str(pending_clarification.get("original_objective") or message)
                    strategy_gate_force_goal = True
            elif (
                same_job
                and not auto_start_sourcing
                and not new_goal_intent
                and strategy_v2.looks_like_anchor_answer(message)
            ):
                strategy_gate_clarification = {
                    "consultant_override": False,
                    "consultant_answers": message,
                    "asked_questions": True,
                    "input_level": str(pending_clarification.get("input_level") or "L3"),
                    "missing_anchors": list(pending_clarification.get("missing_anchors") or []),
                    "original_objective": str(pending_clarification.get("original_objective") or ""),
                }
                goal_request = f"{pending_clarification.get('original_objective') or message}。顾问锚点补充：{message}"
                strategy_gate_force_goal = True
        if not suppress_goal_intent and (auto_start_sourcing or strategy_gate_force_goal or any(re.search(pattern, message, re.I) for pattern in goal_patterns)):
            ground_base = selected
            if strategy_gate_force_goal and pending_clarification.get("job_id"):
                ground_base = {"type": "job", "id": int(pending_clarification["job_id"]), "page": "positions", "filters": {}}
            goal_context, _, grounding_error = self._ground_copilot_goal(goal_request, ground_base, session_id)
            if grounding_error:
                forced_answer = grounding_error
            else:
                strategy_gate = (
                    {"action": "proceed"}
                    if strategy_gate_clarification
                    else self._sourcing_strategy_gate(goal_request, goal_context, floating_compact=floating_compact)
                )
                if strategy_gate.get("action") == "ask":
                    # 红线：提问清单场景不创建 workflow_id，不声称已启动，无任何外部执行。
                    forced_answer = str(strategy_gate.get("answer") or "")
                    strategy_gate_pending_record = strategy_gate.get("pending") or {}
                else:
                    if strategy_gate_clarification:
                        goal_context["strategy_clarification"] = strategy_gate_clarification
                    try:
                        goal_workflow = self.create_goal(goal_request, goal_context)
                        if auto_start_sourcing:
                            goal_workflow = self.start_workflow(goal_workflow["workflow"]["workflow_id"])
                        selected = self._normalize_copilot_context(goal_context)
                        if selected.get("type") in {"job", "candidate"} and selected.get("id"):
                            focus_conflicts = []
                    except ValueError as exc:
                        forced_answer = (
                            f"结论：未建立工作流，执行对象校验未通过。\n\n"
                            f"下一步：{str(exc)[:180]}。"
                        )
        context_type = selected["type"]
        context_id = selected.get("id")
        dashboard = self.get_dashboard()
        selected_payload: dict[str, Any] = dict(selected)
        if existing_focus:
            selected_payload["business_focus"] = existing_focus
        references: list[dict[str, Any]] = []
        suggested_actions: list[dict[str, Any]] = []
        if context_type == "candidate" and context_id:
            candidate_context = build_candidate_context(self.db_path, context_id)
            state = self.get_candidate_state(context_id)
            assessment = state.get("assessment") or {}
            stopped_context = is_stopped(candidate_context)
            stop_review = _latest_event(candidate_context, "resume_review_completed")
            stop_stage_event = _latest_event(candidate_context, "candidate_stage_update")
            selected_payload.update(
                {
                    "candidate": candidate_context.get("identity", {}),
                    "position": candidate_context.get("position", {}),
                    "stage": (candidate_context.get("relation") or {}).get("clean_stage") or "",
                    "stopped": stopped_context,
                    "latest_stop": {
                        "review": stop_review,
                        "stage": stop_stage_event,
                    },
                    "assessment": {
                        key: assessment.get(key)
                        for key in ["fit_score", "fit_level", "recommendation", "confidence", "evidence_coverage", "strengths", "gaps", "risks", "next_action"]
                    },
                }
            )
            identity = candidate_context.get("identity", {})
            position = candidate_context.get("position", {})
            references.append(
                {
                    "type": "candidate",
                    "id": context_id,
                    "label": identity.get("name") or f"关系 #{context_id}",
                    "subtitle": f"{position.get('client','')} / {position.get('job','')}",
                }
            )
            suggested_actions.append({"type": "open_candidate", "id": context_id, "label": "打开人选"})
            if stopped_context:
                stage = str((candidate_context.get("relation") or {}).get("clean_stage") or "已停止")
                identity_label = identity.get("name") or f"关系 #{context_id}"
                project_label = f"{position.get('client','')} / {position.get('job','')}".strip(" /")
                stop_summary = (
                    str(stop_review.get("summary") or "").strip()
                    or str(stop_stage_event.get("summary") or "").strip()
                    or "已有人工停止记录"
                )
                if _is_short_ack(message):
                    if floating_compact:
                        answer = (
                            f"结论：已确认，{identity_label} 保持“{stage}”。\n\n"
                            "下一步：不用继续推进；如需重启，先做人工状态纠正。"
                        )
                    else:
                        answer = (
                            f"结论：已确认，{identity_label} 当前保持“{stage}”。\n\n"
                            f"依据：{project_label} 的最新复核结果为停止推进；记录为：{stop_summary}。\n\n"
                            "下一步：无需再推进或触达。若你要重新启用这个人选，需要先到人选详情里做人工状态纠正/重新复核。"
                        )
                else:
                    if floating_compact:
                        answer = (
                            f"结论：不能继续推进，{identity_label} 已是“{stage}”。\n\n"
                            "下一步：保持归档；重启前先人工纠正状态。"
                        )
                    else:
                        answer = (
                            f"结论：当前不能继续推进，{identity_label} 已处于“{stage}”。\n\n"
                            f"依据：{project_label} 的最新停止记录是：{stop_summary}。\n\n"
                            "下一步：保持历史归档；如需重新考虑，先打开人选详情查看记录，再人工纠正状态或重新复核。"
                        )
                business_focus = self._persist_copilot_focus(
                    session_id, message, selected_payload,
                    structured=selected_payload, conflicts=focus_conflicts,
                )
                conn = self._connect()
                try:
                    conn.executemany(
                        """
                        INSERT INTO agent_copilot_messages
                        (session_id,context_type,context_id,role,content,structured_json)
                        VALUES (?,?,?,?,?,?)
                        """,
                        [
                            (session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                            (
                                session_id, context_type, context_id, "assistant", answer,
                                _dumps({
                                    "references": references, "suggested_actions": suggested_actions,
                                    "skill_runs": [], "business_focus": business_focus,
                                }),
                            ),
                        ],
                    )
                    conn.commit()
                finally:
                    conn.close()
                return {
                    "ok": True,
                    "session_id": session_id,
                    "answer": answer,
                    "context": {"type": context_type, "id": context_id},
                    "references": references,
                    "suggested_actions": suggested_actions,
                    "skill_runs": [],
                    "business_focus": business_focus,
                }
        elif context_type == "job" and context_id:
            conn = self._connect()
            try:
                job = conn.execute(
                    """
                    SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,
                           COUNT(jc.id) AS candidate_total
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    LEFT JOIN job_candidates jc ON jc.job_id=j.id
                    WHERE j.id=? GROUP BY j.id
                    """,
                    (context_id,),
                ).fetchone()
            finally:
                conn.close()
            if job:
                selected_payload.update(_row(job))
                references.append({"type": "job", "id": context_id, "label": job["job"], "subtitle": job["client"]})
                suggested_actions.append({"type": "open_job", "id": context_id, "label": "打开岗位"})
        elif context_type == "queue":
            inbox = self.get_flow_inbox(**selected.get("filters", {}))
            selected_payload["queue"] = {
                "filters": selected.get("filters", {}),
                "summary": inbox.get("summary", {}),
                "items": (inbox.get("items") or [])[:12],
            }
            references = [
                {"type": "candidate", "id": item["job_candidate_id"], "label": item["candidate"], "subtitle": item["project"]}
                for item in (inbox.get("items") or [])[:5]
            ]
        elif context_type == "page":
            selected_payload["page"] = selected.get("page") or "overview"
        bridge_evidence = self._floating_bridge_evidence(raw_context)
        attachment_skill_run = None
        attachment_evidence: dict[str, Any] = {}
        raw_bridge = raw_context.get("bridge") if isinstance(raw_context.get("bridge"), dict) else {}
        raw_wechat = raw_bridge.get("wechat") if isinstance(raw_bridge.get("wechat"), dict) else {}
        if (
            attachment_read_requested(message)
            and str(raw_bridge.get("surface") or "").strip().lower() == "native"
            and bool(raw_wechat)
        ):
            try:
                attachment_skill_context = {**raw_context, "type": "page"}
                attachment_skill_run = self.execute_skill(
                    "document_understanding",
                    context=attachment_skill_context,
                    inputs={
                        "request": message,
                        "bridge": raw_bridge,
                    },
                )
                attachment_evidence = (attachment_skill_run.get("result") or {}).get("attachment_evidence") or {}
            except Exception as exc:
                attachment_skill_run = {
                    "skill": {"id": "document_understanding", "label": "本机文档理解"},
                    "ok": False,
                    "error": str(exc)[:500],
                }
        if attachment_evidence:
            selected_payload["attachment_evidence"] = attachment_evidence
            if bridge_evidence:
                bridge_evidence["attachment_content_available"] = any(
                    bool(item.get("content_available"))
                    for item in attachment_evidence.get("items") or []
                )
        uploaded_attachment_evidence = self._uploaded_attachment_evidence(raw_context)
        if uploaded_attachment_evidence:
            selected_payload["uploaded_attachment_evidence"] = uploaded_attachment_evidence
            for item in uploaded_attachment_evidence.get("items") or []:
                references.append(
                    {
                        "type": "local_attachment",
                        "id": item.get("attachment_id") or "",
                        "label": item.get("file_name") or "本地附件",
                        "subtitle": item.get("status") or "用户粘贴/选择的附件",
                    }
                )
        history_text = " ".join(
            item["content"] for item in conversation_history if item.get("role") == "user"
        )
        has_confirmed_salary_structure = bool(
            re.search(r"(?:12\s*(?:薪|个月固定)?\s*[+＋]\s*3|13\s*(?:薪|个月固定)?\s*[+＋]\s*5)", history_text)
        )
        has_attachment_salary_evidence = any(
            bool(item.get("content_available"))
            for item in (attachment_evidence.get("items") or [])
        ) or bool(uploaded_attachment_evidence.get("content_available"))
        if (
            forced_answer is None
            and "薪资结构" in message
            and mentioned_clients
            and not has_confirmed_salary_structure
            and not has_attachment_salary_evidence
        ):
            forced_answer = (
                f"系统里还没有{primary_client}已确认的客户级薪资结构，岗位预算不能替代固定薪资与奖金月数。\n\n"
                "下一步：补充 Dylan/财务确认的固定月数、年终奖月数和适用条件。"
            )
        pending_image_analysis = bool(
            image_analysis_requested(message)
            and bridge_evidence.get("source") == "native"
            and bridge_evidence.get("page_type") == "wechat_visible_window"
            and not bridge_evidence.get("image_analysis")
        )
        if pending_image_analysis:
            suggested_actions.append(
                {
                    "type": "native_action",
                    "id": "recognizeWeChatImage",
                    "label": "打开并识别当前图片",
                }
            )
        if bridge_evidence:
            selected_payload["page_evidence"] = bridge_evidence
            references.append(
                {
                    "type": bridge_evidence.get("source") or "page",
                    "id": bridge_evidence.get("source_url") or "",
                    "label": bridge_evidence.get("candidate_name") or bridge_evidence.get("label") or "当前页面",
                    "subtitle": bridge_evidence.get("bridge_status") or bridge_evidence.get("page_type") or "页面桥接证据",
                }
            )
            for item in attachment_evidence.get("items") or []:
                references.append(
                    {
                        "type": "native_attachment",
                        "id": "",
                        "label": item.get("file_name") or "微信附件",
                        "subtitle": item.get("status") or "本机附件读取证据",
                    }
                )
            unread_attachment = next(
                (
                    item for item in (attachment_evidence.get("items") or [])
                    if item.get("file_name") and not item.get("content_available")
                ),
                None,
            )
            if unread_attachment and bridge_evidence.get("source") == "native":
                suggested_actions.append(
                    {
                        "type": "floating_action",
                        "id": f"open_wechat_attachment::{unread_attachment['file_name']}",
                        "label": "打开并读取当前附件",
                    }
                )
            if (
                bridge_evidence.get("source") == "liepin"
                and any(token in message for token in ("录入", "入库", "加入人才库", "保存到人才库"))
            ):
                if floating_compact:
                    answer = (
                        "结论：可以继续，但还没到写库完成态。\n\n"
                        "下一步：先补全简历并定位；未选岗位时将先入库为人才库储备（不挂岗位）。"
                    )
                else:
                    answer = (
                        "结论：可以继续，但当前不是直接写库完成态。\n\n"
                        "依据：当前猎聘页面已识别到候选人"
                        f"{bridge_evidence.get('candidate_name') or '当前人选'}，"
                        f"页面状态为“{bridge_evidence.get('bridge_status') or '已同步'}”。"
                        "ASA 需要先做页面采集、客户/岗位定位和入库预检，确认唯一后才能写入人才库。\n\n"
                        "下一步：我已把动作识别为“补全简历并定位”。请在浮窗或猎聘桥接入口执行该动作；"
                        "如果当前页面尚未选择客户/岗位，确认后将先入库为人才库储备（不挂岗位），之后可再补选客户和岗位。"
                    )
                suggested_actions.extend(
                    [
                        {"type": "floating_action", "id": "fill_resume", "label": "补全简历并定位"},
                        {"type": "floating_action", "id": "refresh_bridge", "label": "刷新页面识别"},
                    ]
                )
                business_focus = self._persist_copilot_focus(
                    session_id, message, selected_payload,
                    structured=selected_payload, conflicts=focus_conflicts,
                )
                conn = self._connect()
                try:
                    conn.executemany(
                        """
                        INSERT INTO agent_copilot_messages
                        (session_id,context_type,context_id,role,content,structured_json)
                        VALUES (?,?,?,?,?,?)
                        """,
                        [
                            (session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                            (
                                session_id, context_type, context_id, "assistant", answer,
                                _dumps({
                                    "references": references, "suggested_actions": suggested_actions,
                                    "skill_runs": [], "business_focus": business_focus,
                                }),
                            ),
                        ],
                    )
                    conn.commit()
                finally:
                    conn.close()
                return {
                    "ok": True,
                    "session_id": session_id,
                    "answer": answer,
                    "context": {"type": context_type, "id": context_id},
                    "references": references,
                    "suggested_actions": suggested_actions,
                    "skill_runs": [],
                    "business_focus": business_focus,
                }
        mentioned_jobs = self._mentioned_jobs_for_copilot(message)
        if mentioned_jobs:
            selected_payload["mentioned_jobs"] = mentioned_jobs
            for item in mentioned_jobs[:3]:
                references.append(
                    {"type": "job", "id": item.get("id"), "label": item.get("job"), "subtitle": item.get("client")}
                )
        workflow_outcome_context = self._copilot_workflow_outcome_context(
            message, selected, mentioned_jobs, existing_focus
        )
        if not references:
            references = [
                {"type": item["type"], "id": item["id"], "label": item["label"], "subtitle": item["project"]}
                for item in dashboard.get("top_actions", [])[:5]
            ]
        routed_skills = [] if goal_workflow or forced_answer is not None else self._route_copilot_skills(message, selected)
        business_routed_skills = list(routed_skills)
        needs_browser_assist = any(
            (self.skills.get(skill_id) and self.skills.get(skill_id).adapter_type == "browser")
            for skill_id in business_routed_skills
        )
        if needs_browser_assist:
            for support_skill in ("opencli_usage", "opencli_browser_read"):
                if support_skill not in routed_skills:
                    routed_skills.append(support_skill)
        skill_runs = [attachment_skill_run] if attachment_skill_run else []
        for skill_id in routed_skills:
            try:
                skill_inputs: dict[str, Any] = {}
                if skill_id == "opencli_usage":
                    skill_inputs["command"] = "browser" if any(token in message for token in ("浏览器", "网页", "Chrome", "chrome", "页面")) else "list"
                elif skill_id == "opencli_browser_read":
                    skill_inputs = {"args": "asa state", "timeout_seconds": 20}
                skill_run = self.execute_skill(skill_id, context=selected, inputs=skill_inputs)
                skill_runs.append(skill_run)
                result = skill_run.get("result") or {}
                references.extend(result.get("references") or [])
                suggested_actions.extend(result.get("suggested_actions") or [])
            except Exception as exc:
                skill_runs.append({"skill": {"id": skill_id}, "ok": False, "error": str(exc)[:500]})
        memories = self.search_memories(
            message, context_type=context_type, context_id=context_id,
            client=str(selected_payload.get("client") or selected_payload.get("position", {}).get("client") or ""),
            job=str(selected_payload.get("job") or selected_payload.get("position", {}).get("job") or ""),
        )
        payload = {
            "question": message,
            "response_mode": "floating_compact" if floating_compact else "default",
            "conversation_history": conversation_history,
            "selected_context": selected_payload,
            "dashboard": {
                "summary": dashboard.get("summary", {}),
                "top_actions": dashboard.get("top_actions", [])[:5],
                "exceptions": dashboard.get("exceptions", [])[:5],
                "p0_jobs": dashboard.get("p0_jobs", [])[:8],
                "analytics": dashboard.get("analytics", {}),
            },
            "skill_results": [
                item.get("result") if item.get("ok") else {
                    "skill_id": (item.get("skill") or {}).get("id"),
                    "ok": False,
                    "error": item.get("error"),
                }
                for item in skill_runs
            ],
        }
        if memories.get("mode") == "active":
            payload["approved_memories"] = memories.get("memories") or []
        if workflow_outcome_context:
            payload["workflow_outcome"] = workflow_outcome_context
        capture_run = next(
            (
                item for item in skill_runs
                if (item.get("skill") or {}).get("id") == "liepin_resume_capture"
            ),
            None,
        )
        if goal_workflow:
            plan_steps = goal_workflow.get("steps") or []
            risk_steps = [step for step in plan_steps if step.get("risk_level") in {"R2", "R3"}]
            if auto_start_sourcing and floating_compact:
                answer = (
                    "结论：已按确认条件建立并启动新一轮多渠道寻访。\n\n"
                    "下一步：内部诊断和策略完成后，在待审批里批准本次外部寻访。"
                )
            elif auto_start_sourcing:
                answer = (
                    "已按本轮确认条件建立并启动新的多渠道寻访工作流。\n\n"
                    "ASA 会先完成岗位缺口诊断、历史人才库排重和搜索策略；运行到外部渠道前仍会生成一次性审批，"
                    "批准后才会执行猎聘和 X-SaaS 搜索。"
                )
            elif floating_compact:
                answer = (
                    f"结论：目标已建立，计划共 {len(plan_steps)} 步。\n\n"
                    f"下一步：先查看计划；{len(risk_steps)} 个风险节点会单次确认。"
                )
            else:
                answer = (
                    f"已建立目标：{goal_workflow['goal']['title']}\n\n"
                    f"ASA 已生成 {len(plan_steps)} 步执行计划，其中 {len(risk_steps)} 步需要单次人工确认。"
                    "当前尚未执行，你可以开始、修改或取消这个目标。"
                )
            references.extend(
                [
                    {"type": goal_workflow["goal"]["context"].get("type"), "id": goal_workflow["goal"]["context"].get("id"), "label": goal_workflow["goal"]["title"], "subtitle": "ASA 目标"}
                ]
            )
            if not auto_start_sourcing:
                suggested_actions.append(
                    {"type": "start_workflow", "id": goal_workflow["workflow"]["workflow_id"], "label": "开始执行"}
                )
            suggested_actions.append(
                {"type": "open_workflow", "id": goal_workflow["workflow"]["workflow_id"], "label": "查看计划"}
            )
        elif (
            any((item.get("skill") or {}).get("id") == "opencli_usage" for item in skill_runs)
            and not any(
                (item.get("skill") or {}).get("id") not in {"opencli_usage", "opencli_browser_read"}
                for item in skill_runs
            )
        ):
            usage_run = next(item for item in skill_runs if (item.get("skill") or {}).get("id") == "opencli_usage")
            opencli_result = ((usage_run.get("result") or {}).get("opencli") or {}) if usage_run.get("ok") else {}
            browser_run = next((item for item in skill_runs if (item.get("skill") or {}).get("id") == "opencli_browser_read"), None)
            browser_result = ((browser_run.get("result") or {}).get("opencli") or {}) if browser_run and browser_run.get("ok") else {}
            if usage_run.get("ok") and opencli_result.get("ok"):
                if floating_compact:
                    answer = (
                        "结论：能用，ASA 后端已接入 OpenCLI。\n\n"
                        "下一步：可用于只读查看浏览器和网页状态。"
                    )
                else:
                    answer = (
                        "结论：能用。ASA 后端已经接入本机 OpenCLI，并刚刚通过 skill 调用验证成功。\n\n"
                        "依据：`opencli_usage` 调用了本机 `/Users/messi/.hermes/node/bin/opencli`，返回码为 0。\n\n"
                        "下一步：ASA 现在可以用 OpenCLI 做只读浏览器/网页状态读取；点击、填写、发送等写动作仍需要走审批工作流。"
                    )
                if browser_run:
                    if browser_result.get("ok"):
                        answer += "\n\n当前浏览器只读状态也已读取成功。"
                    else:
                        answer += "\n\n当前浏览器只读状态暂未读到，可能还没有绑定 OpenCLI browser session。"
            else:
                reason = opencli_result.get("stderr") or opencli_result.get("reason") or usage_run.get("error") or "OpenCLI 调用失败"
                answer = (
                    f"结论：OpenCLI 后端调用未成功。\n\n"
                    f"下一步：检查 OpenCLI/Node 路径。错误：{str(reason)[:180]}"
                )
        elif pending_image_analysis:
            answer = (
                "结论：需要打开当前微信图片后才能识别。\n\n"
                "下一步：确认“打开并识别当前图片”；ASA 会本地识别后自动回答。"
            )
        elif capture_run and capture_run.get("ok"):
            capture_result = capture_run.get("result") or {}
            resume = capture_result.get("resume") or {}
            if floating_compact:
                answer = (
                    f"结论：已补全 {resume.get('name') or '当前人选'} 的简历。\n\n"
                    "下一步：ASA 会重新评估当前人岗关系。"
                )
            else:
                answer = (
                    f"已从猎聘补全 {resume.get('name') or '当前人选'} 的完整简历。\n\n"
                    f"工作经历 {int(resume.get('work_chars') or 0)} 字，"
                    f"项目经历 {int(resume.get('project_chars') or 0)} 字，"
                    f"教育经历 {int(resume.get('education_chars') or 0)} 字。\n\n"
                    "简历已写入人才库，并正在重新评估当前人岗关系。"
                )
        elif capture_run:
            if floating_compact:
                answer = (
                    "结论：简历补全失败。\n\n"
                    "下一步：打开匹配的猎聘简历详情页后重试。"
                )
            else:
                answer = (
                    "未能从猎聘补全当前人选的简历。\n\n"
                    f"原因：{capture_run.get('error') or '猎聘简历读取失败'}\n\n"
                    "请在猎聘打开与 ASA 当前选中人选一致的简历详情页后重试。"
                )
        elif forced_answer is not None:
            answer = forced_answer
        else:
            answer = self.llm.copilot(sanitize_payload(payload))
        focus_context = (
            (goal_workflow.get("goal") or {}).get("context")
            if goal_workflow else selected_payload
        ) or selected_payload
        business_focus = self._persist_copilot_focus(
            session_id, message, focus_context,
            structured=selected_payload, conflicts=focus_conflicts,
        )
        assistant_structured = {
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": skill_runs,
            "goal": goal_workflow.get("goal") if goal_workflow else None,
            "workflow": goal_workflow.get("workflow") if goal_workflow else None,
            "plan_summary": [
                {
                    "id": step.get("id"),
                    "capability_id": step.get("capability_id"),
                    "label": step.get("business_label"),
                    "status": step.get("status"),
                    "risk_level": step.get("risk_level"),
                }
                for step in (goal_workflow.get("steps") or [])
            ] if goal_workflow else [],
            "business_focus": business_focus,
        }
        if strategy_gate_pending_record:
            assistant_structured["strategy_clarification"] = strategy_gate_pending_record
        elif strategy_gate_clarification:
            assistant_structured["strategy_clarification"] = {
                "status": "resolved",
                "job_id": int(pending_clarification.get("job_id") or 0) if pending_clarification else 0,
                "consultant_override": bool(strategy_gate_clarification.get("consultant_override")),
                "consultant_answers": str(strategy_gate_clarification.get("consultant_answers") or ""),
                "input_level": str(strategy_gate_clarification.get("input_level") or ""),
                "missing_anchors": list(strategy_gate_clarification.get("missing_anchors") or []),
            }
        conn = self._connect()
        try:
            conn.executemany(
                """
                INSERT INTO agent_copilot_messages
                (session_id,context_type,context_id,role,content,structured_json)
                VALUES (?,?,?,?,?,?)
                """,
                [
                    (session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                    (
                        session_id,
                        context_type,
                        context_id,
                        "assistant",
                        answer,
                        _dumps(assistant_structured),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "ok": True,
            "session_id": session_id,
            "answer": answer,
            "context": {"type": context_type, "id": context_id},
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": skill_runs,
            "goal_id": goal_workflow["goal"]["goal_id"] if goal_workflow else None,
            "workflow_id": goal_workflow["workflow"]["workflow_id"] if goal_workflow else None,
            "goal": goal_workflow.get("goal") if goal_workflow else None,
            "workflow": goal_workflow.get("workflow") if goal_workflow else None,
            "plan_summary": [
                {
                    "id": step.get("id"),
                    "capability_id": step.get("capability_id"),
                    "label": step.get("business_label"),
                    "status": step.get("status"),
                    "risk_level": step.get("risk_level"),
                    "reason": step.get("reason"),
                }
                for step in (goal_workflow.get("steps") or [])
            ] if goal_workflow else [],
            "approvals": goal_workflow.get("approvals") if goal_workflow else [],
            "artifacts": goal_workflow.get("artifacts") if goal_workflow else [],
            "progress": goal_workflow.get("progress") if goal_workflow else None,
            "memory": {"mode": memories.get("mode"), "hits": len(memories.get("memories") or [])},
            "business_focus": business_focus,
        }

    def chat(self, job_candidate_id: int, message: str, session_id: str = "") -> dict[str, Any]:
        message = " ".join(str(message or "").split())
        if not message:
            raise ValueError("请输入问题")
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        state = self.get_candidate_state(int(job_candidate_id))
        assessment = state.get("assessment") or {}
        if not assessment:
            raise ValueError("请先完成当前人选的 Agent 评估")
        if "为什么" in message:
            parts = [*assessment.get("strengths", []), *assessment.get("gaps", []), *assessment.get("risks", [])]
            answer = "；".join(parts[:8]) or assessment.get("next_action") or "当前证据不足。"
        elif any(token in message for token in ["缺什么", "还缺", "核验"]):
            parts = [*assessment.get("gaps", []), *assessment.get("verification_questions", [])]
            answer = "；".join(parts[:8]) or "当前没有额外核验项。"
        else:
            answer = self.llm.chat(context["model_context"], assessment, message)
        session_id = session_id or f"candidate_{job_candidate_id}_{secrets.token_hex(4)}"
        conn = self._connect()
        try:
            conn.executemany(
                """
                INSERT INTO agent_messages(session_id,job_candidate_id,role,content,structured_json)
                VALUES (?,?,?,?,?)
                """,
                [
                    (session_id, int(job_candidate_id), "user", message, "{}"),
                    (session_id, int(job_candidate_id), "assistant", answer, _dumps({"assessment_id": assessment.get("id")})),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "session_id": session_id, "answer": answer}

    def record_feedback(
        self,
        assessment_id: int,
        feedback_type: str,
        *,
        corrected: dict[str, Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        feedback_type = str(feedback_type or "").strip().lower()
        if feedback_type not in {"approve", "correct", "reject"}:
            raise ValueError("feedback_type 必须是 approve/correct/reject")
        conn = self._connect()
        try:
            assessment = conn.execute(
                "SELECT id,job_candidate_id,client,job FROM agent_candidate_assessments WHERE id=?",
                (int(assessment_id),),
            ).fetchone()
            if assessment is None:
                raise ValueError(f"找不到 Agent 评估：{assessment_id}")
            cursor = conn.execute(
                """
                INSERT INTO agent_feedback
                (assessment_id,job_candidate_id,feedback_type,corrected_json,note)
                VALUES (?,?,?,?,?)
                """,
                (assessment["id"], assessment["job_candidate_id"], feedback_type, _dumps(corrected or {}), note),
            )
            proposal = None
            if feedback_type == "correct" and corrected:
                rule_key = hashlib.sha256(
                    f"{assessment['client']}|{assessment['job']}|{_dumps(corrected)}".encode("utf-8")
                ).hexdigest()[:20]
                existing_rule = conn.execute(
                    """
                    SELECT * FROM agent_learning_rules
                    WHERE rule_key=? ORDER BY version DESC,id DESC LIMIT 1
                    """,
                    (rule_key,),
                ).fetchone()
                if existing_rule:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO agent_learning_evidence
                        (rule_id,assessment_id,job_candidate_id,signal_type)
                        VALUES (?,?,?,'explicit_correction')
                        """,
                        (existing_rule["id"], assessment["id"], assessment["job_candidate_id"]),
                    )
                    counts = conn.execute(
                        """
                        SELECT COUNT(*) AS support_count,COUNT(DISTINCT job_candidate_id) AS candidate_count
                        FROM agent_learning_evidence WHERE rule_id=? AND signal_type='explicit_correction'
                        """,
                        (existing_rule["id"],),
                    ).fetchone()
                    next_status = "pending" if (
                        int(counts["support_count"] or 0) >= int(self.config["learning"]["minimum_support"])
                        and int(counts["candidate_count"] or 0) >= int(self.config["learning"]["minimum_candidates"])
                    ) else "collecting"
                    conn.execute(
                        """
                        UPDATE agent_learning_rules
                        SET support_count=?,candidate_count=?,
                            status=CASE WHEN status IN ('active','revoked','suspended') THEN status ELSE ? END,
                            last_supported_at=datetime('now','localtime'),
                            source_assessment_id=?,updated_at=datetime('now','localtime')
                        WHERE id=?
                        """,
                        (
                            int(counts["support_count"] or 0), int(counts["candidate_count"] or 0),
                            next_status, assessment["id"], existing_rule["id"],
                        ),
                    )
                    proposal = {
                        "id": existing_rule["id"],
                        "rule_key": rule_key,
                        "version": existing_rule["version"],
                        "status": existing_rule["status"] if existing_rule["status"] in {"active", "revoked", "suspended"} else next_status,
                        "support_count": int(counts["support_count"] or 0),
                        "candidate_count": int(counts["candidate_count"] or 0),
                        "aggregated": True,
                    }
                else:
                    version_row = conn.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM agent_learning_rules WHERE rule_key=?",
                        (rule_key,),
                    ).fetchone()
                    version = int(version_row[0])
                    rule_cursor = conn.execute(
                        """
                        INSERT INTO agent_learning_rules
                        (rule_key,scope_type,client,job,rule_type,rule_json,status,version,
                         source_assessment_id,support_count,candidate_count,last_supported_at)
                        VALUES (?,'job',?,?,? ,?,'collecting',?,?,1,1,datetime('now','localtime'))
                        """,
                        (
                            rule_key,
                            assessment["client"],
                            assessment["job"],
                            "assessment_correction",
                            _dumps(corrected),
                            version,
                            assessment["id"],
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO agent_learning_evidence
                        (rule_id,assessment_id,job_candidate_id,signal_type)
                        VALUES (?,?,?,'explicit_correction')
                        """,
                        (rule_cursor.lastrowid, assessment["id"], assessment["job_candidate_id"]),
                    )
                    proposal = {
                        "id": rule_cursor.lastrowid,
                        "rule_key": rule_key,
                        "version": version,
                        "status": "collecting",
                        "support_count": 1,
                        "candidate_count": 1,
                        "aggregated": False,
                    }
            if feedback_type == "reject" and corrected and corrected.get("rule_id"):
                rule_id = int(corrected["rule_id"])
                rule = conn.execute(
                    "SELECT id,status FROM agent_learning_rules WHERE id=?", (rule_id,)
                ).fetchone()
                if rule is None:
                    raise ValueError(f"找不到学习规则：{rule_id}")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_learning_evidence
                    (rule_id,assessment_id,job_candidate_id,signal_type)
                    VALUES (?,?,?,'contradiction')
                    """,
                    (rule_id, assessment["id"], assessment["job_candidate_id"]),
                )
                signals = conn.execute(
                    """
                    SELECT signal_type FROM agent_learning_evidence WHERE rule_id=?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (rule_id, int(self.config["learning"]["contradiction_window"])),
                ).fetchall()
                contradiction_count = sum(row["signal_type"] == "contradiction" for row in signals)
                contradiction_rate = contradiction_count / max(1, len(signals))
                next_status = rule["status"]
                if (
                    rule["status"] == "active" and contradiction_count >= 3
                    and contradiction_rate > float(self.config["learning"]["pause_rate"])
                ):
                    next_status = "suspended"
                conn.execute(
                    """
                    UPDATE agent_learning_rules SET contradiction_count=?,status=?,updated_at=datetime('now','localtime')
                    WHERE id=?
                    """,
                    (contradiction_count, next_status, rule_id),
                )
                proposal = {
                    "id": rule_id, "status": next_status,
                    "contradiction_count": contradiction_count,
                    "contradiction_rate": round(contradiction_rate, 4),
                }
            conn.commit()
            memory = None
            if feedback_type in {"approve", "correct"}:
                memory_content = note or (
                    f"人工纠正 ASA 判断：{_dumps(corrected or {})}"
                    if feedback_type == "correct" else "人工确认当前 ASA 判断准确"
                )
                memory = self.store_memory(
                    scope_type="job", scope_id=assessment["job"],
                    memory_type="assessment_feedback", content=memory_content,
                    source_type="agent_feedback", source_id=cursor.lastrowid,
                    confidence=1.0,
                )
            return {"ok": True, "feedback_id": cursor.lastrowid, "learning_proposal": proposal, "memory": memory}
        finally:
            conn.close()

    def create_draft(self, job_candidate_id: int, instructions: str = "") -> dict[str, Any]:
        state = self.get_candidate_state(int(job_candidate_id))
        assessment = state.get("assessment") or {}
        if not assessment:
            raise ValueError("请先完成当前人选的 Agent 评估")
        instructions = " ".join(str(instructions or "").split())
        key_raw = f"draft|{job_candidate_id}|{assessment.get('id')}|{instructions}"
        idempotency_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
        existing = self.get_action(idempotency_key)
        if existing and existing.get("status") == "executed":
            return {"ok": True, "cached": True, **_loads(existing.get("result_json"), {})}
        context = build_candidate_context(self.db_path, int(job_candidate_id))
        prompt = "请生成一条尚未发送的候选人联系草稿，必须明确客户和岗位，不承诺薪资或流程结果。"
        if instructions:
            prompt += f" 补充要求：{instructions}"
        draft = self.llm.chat(context["model_context"], assessment, prompt).strip()
        if not draft:
            raise ValueError("模型未生成有效草稿")
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS talk_draft_audits(
                  id INT,task_id INT,reply_id INT,candidate_name TEXT,strategy TEXT,score INT,
                  reason TEXT,risk TEXT,missing TEXT,draft TEXT,created_at TEXT)
                """
            )
            next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM talk_draft_audits").fetchone()[0]
            conn.execute(
                """
                INSERT INTO talk_draft_audits
                (id,task_id,reply_id,candidate_name,strategy,score,reason,risk,missing,draft,created_at)
                VALUES (?,NULL,NULL,?,'a_system_agent_v1',?,?,?,?,?,datetime('now','localtime'))
                """,
                (
                    next_id,
                    context["identity"].get("name"),
                    assessment.get("fit_score") or 0,
                    assessment.get("recommendation_label") or "",
                    "；".join(assessment.get("risks") or []),
                    "；".join(assessment.get("gaps") or []),
                    draft,
                ),
            )
            result = {"draft_id": next_id, "draft": draft, "sent": False}
            self._insert_action(
                conn,
                idempotency_key=idempotency_key,
                job_candidate_id=int(job_candidate_id),
                action_type="save_draft",
                request={"instructions": instructions, "assessment_id": assessment.get("id")},
                result=result,
                status="executed",
            )
            conn.commit()
            return {"ok": True, "cached": False, **result}
        finally:
            conn.close()

    def get_action(self, idempotency_key: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_actions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return _row(row) if row else None
        finally:
            conn.close()

    def _insert_action(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        job_candidate_id: int,
        action_type: str,
        request: dict[str, Any],
        result: dict[str, Any],
        status: str,
    ) -> None:
        decision = action_decision(action_type)
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_actions
            (run_id,job_candidate_id,idempotency_key,action_type,risk_level,request_json,
             preflight_json,result_json,status,created_at,executed_at)
            VALUES (NULL,?,?,?,?,?,'{}',?,?,datetime('now','localtime'),
                    CASE WHEN ?='executed' THEN datetime('now','localtime') ELSE NULL END)
            """,
            (
                int(job_candidate_id),
                idempotency_key,
                action_type,
                decision["risk_level"],
                _dumps(request),
                _dumps(result),
                status,
                status,
            ),
        )

    def record_external_action(
        self,
        *,
        job_candidate_id: int,
        action_type: str,
        request: dict[str, Any],
        result: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        decision = action_decision(action_type)
        if decision["decision"] != "allow":
            raise ValueError(decision["reason"])
        existing = self.get_action(idempotency_key)
        if existing and existing.get("status") == "executed":
            return {"ok": True, "cached": True, **_loads(existing.get("result_json"), {})}
        conn = self._connect()
        try:
            self._insert_action(
                conn,
                idempotency_key=idempotency_key,
                job_candidate_id=int(job_candidate_id),
                action_type=action_type,
                request=request,
                result=result,
                status="executed",
            )
            conn.commit()
            return {"ok": True, "cached": False, **result}
        finally:
            conn.close()

    def get_workbench(self, limit: int = 12) -> dict[str, Any]:
        limit = max(1, min(int(limit or 12), 50))
        scan_limit = min(500, max(120, limit * 8))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT jc.id AS job_candidate_id,jc.raw_client AS client,jc.raw_position AS job,
                       jc.clean_stage,jc.raw_stage,jc.updated_at,
                       p.display_name AS name,p.current_company AS company,p.current_title AS title,
                       a.id AS assessment_id,a.snapshot_hash AS assessment_snapshot_hash,
                       a.fit_score,a.fit_level,a.recommendation,a.confidence,
                       a.evidence_coverage,a.next_action,a.created_at AS assessed_at,
                       (SELECT r.status FROM agent_runs r
                        WHERE r.kind='candidate_assessment' AND r.context_type='job_candidate' AND r.context_id=jc.id
                        ORDER BY r.id DESC LIMIT 1) AS latest_run_status,
                       (SELECT r.error FROM agent_runs r
                        WHERE r.kind='candidate_assessment' AND r.context_type='job_candidate' AND r.context_id=jc.id
                        ORDER BY r.id DESC LIMIT 1) AS latest_run_error,
                       (SELECT CAST(strftime('%s','now','localtime')-
                                    strftime('%s',COALESCE(r.updated_at,r.created_at)) AS INTEGER)
                        FROM agent_runs r
                        WHERE r.kind='candidate_assessment' AND r.context_type='job_candidate' AND r.context_id=jc.id
                        ORDER BY r.id DESC LIMIT 1) AS latest_run_age_seconds,
                       (SELECT COUNT(*) FROM agent_feedback f
                        WHERE f.assessment_id=a.id) AS feedback_count
                FROM job_candidates jc
                JOIN people p ON p.id=jc.person_id
                LEFT JOIN agent_candidate_assessments a
                  ON a.job_candidate_id=jc.id AND a.is_current=1
                WHERE COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'
                  AND COALESCE((SELECT c.status FROM candidates c
                                WHERE CAST(c.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
                                ORDER BY c.id DESC LIMIT 1),'')
                      NOT IN ('screen_rejected','rejected','client_rejected','eliminated','closed')
                ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC
                LIMIT ?
                """,
                (scan_limit,),
            ).fetchall()
            proposal_counts = {
                str(row["status"]): int(row["total"])
                for row in conn.execute(
                    "SELECT status,COUNT(*) AS total FROM agent_action_proposals GROUP BY status"
                ).fetchall()
            }
            verification_tasks: list[dict[str, Any]] = []
            if _table_exists(conn, "followup_tasks"):
                verification_tasks = []
                for row in conn.execute(
                    """
                    SELECT t.id AS task_id,t.job_candidate_id,t.priority,t.due_at,t.reason,
                           t.created_at,p.display_name AS candidate,
                           jc.raw_client AS client,jc.raw_position AS job,
                           a.verification_questions_json
                    FROM followup_tasks t
                    JOIN job_candidates jc ON jc.id=t.job_candidate_id
                    JOIN people p ON p.id=jc.person_id
                    LEFT JOIN agent_candidate_assessments a
                      ON a.job_candidate_id=t.job_candidate_id AND a.is_current=1
                    WHERE t.task_type='agent_verification'
                      AND COALESCE(t.status,'open')='open'
                    ORDER BY COALESCE(t.priority,2) ASC,
                             COALESCE(t.due_at,t.created_at) ASC,t.id DESC
                    LIMIT 50
                    """
                ).fetchall():
                    task = _row(row)
                    task["questions"] = _loads(
                        task.pop("verification_questions_json", "[]"), []
                    )
                    verification_tasks.append(task)
            recent_runs = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT r.run_id,r.context_id AS job_candidate_id,r.status,r.trigger,r.model,
                           r.error,r.created_at,r.updated_at,
                           p.display_name AS candidate,jc.raw_client AS client,jc.raw_position AS job
                    FROM agent_runs r
                    LEFT JOIN job_candidates jc
                      ON r.context_type='job_candidate' AND jc.id=r.context_id
                    LEFT JOIN people p ON p.id=jc.person_id
                    WHERE r.kind='candidate_assessment'
                    ORDER BY r.id DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
        finally:
            conn.close()

        items: list[dict[str, Any]] = []
        counts = {
            "unassessed": 0,
            "stale": 0,
            "verification": 0,
            "human_review": 0,
            "failed": 0,
        }
        for row in rows:
            item = _row(row)
            job_candidate_id = int(item["job_candidate_id"])
            try:
                context = build_candidate_context(self.db_path, job_candidate_id)
            except (LookupError, ValueError, sqlite3.Error):
                continue
            if is_stopped(context):
                continue
            current_snapshot_hash = self._snapshot_key(context)
            stale = not item.get("assessment_id") or item.get("assessment_snapshot_hash") != current_snapshot_hash
            latest_status = str(item.get("latest_run_status") or "")
            kind = ""
            label = ""
            reason = ""
            priority = 0
            if (
                latest_status in {"failed", "interrupted"}
                and stale
                and int(item.get("latest_run_age_seconds") or 0) < 600
            ):
                kind = "failed"
                label = "评估运行失败"
                reason = str(item.get("latest_run_error") or "模型或服务暂时不可用")
                priority = 100
            elif not item.get("assessment_id"):
                kind = "unassessed"
                label = "待首次评估"
                reason = "尚无 Agent 人岗判断"
                priority = 92
            elif stale:
                kind = "stale"
                label = "判断已过期"
                reason = "候选人、岗位或学习规则证据已变化"
                priority = 88
            elif item.get("recommendation") == "verify_first":
                kind = "verification"
                label = "待核验"
                reason = str(item.get("next_action") or "核验关键证据后再判断")
                priority = 78 + int((1 - float(item.get("evidence_coverage") or 0)) * 10)
            elif not int(item.get("feedback_count") or 0):
                kind = "human_review"
                label = "待确认判断"
                reason = str(item.get("next_action") or "人工确认 Agent 判断")
                priority = 72 if item.get("recommendation") == "priority_review" else 58
            if not kind:
                continue
            counts[kind] += 1
            items.append(
                {
                    "job_candidate_id": job_candidate_id,
                    "assessment_id": item.get("assessment_id"),
                    "candidate": item.get("name") or "",
                    "company": item.get("company") or "",
                    "title": item.get("title") or "",
                    "client": item.get("client") or "",
                    "job": item.get("job") or "",
                    "stage": item.get("clean_stage") or item.get("raw_stage") or "",
                    "kind": kind,
                    "label": label,
                    "reason": reason,
                    "priority": priority,
                    "stale": stale,
                    "fit_score": item.get("fit_score"),
                    "fit_level": item.get("fit_level"),
                    "recommendation": item.get("recommendation") or "",
                    "latest_run_status": latest_status,
                }
            )
        items.sort(key=lambda item: (-int(item["priority"]), int(item["job_candidate_id"])))
        return {
            "ok": True,
            "runtime": {
                "model": self.llm.model,
                "provider": (
                    "deepseek_official"
                    if "api.deepseek.com" in str(getattr(self.llm, "base_url", "")).lower()
                    else "local_or_compatible"
                ),
                "base_url": str(getattr(self.llm, "base_url", "")),
                "approval_mode": "human_in_the_loop",
                "latest_status": recent_runs[0]["status"] if recent_runs else "idle",
            },
            "summary": {
                **counts,
                "attention_total": sum(counts.values()),
                "pending_confirmations": proposal_counts.get("pending", 0),
                "approved_pending_execution": proposal_counts.get("approved", 0),
                "open_verification_tasks": len(verification_tasks),
            },
            "items": items[:limit],
            "verification_tasks": verification_tasks,
            "recent_runs": recent_runs,
        }

    def ensure_verification_task(self, job_candidate_id: int, assessment: dict[str, Any]) -> int | None:
        conn = self._connect()
        try:
            existing = conn.execute(
                """
                SELECT id FROM followup_tasks
                WHERE job_candidate_id=? AND task_type='agent_verification'
                  AND COALESCE(status,'open')='open'
                ORDER BY id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone() if _table_exists(conn, "followup_tasks") else None
            if existing:
                return int(existing["id"])
        finally:
            conn.close()
        questions = [str(item).strip() for item in assessment.get("verification_questions") or [] if str(item).strip()]
        reason = "；".join(questions[:5]) or str(assessment.get("next_action") or "补充关键证据后由 ASA 重新评估")
        candidate = build_candidate_context(self.db_path, int(job_candidate_id))
        return self.capability_runtime._followup(
            candidate,
            "agent_verification",
            reason,
            {"priority": 2, "step_id": "assessment_verify_first"},
            days=2,
        )

    def batch_assess(
        self,
        job_candidate_ids: list[int] | None = None,
        *,
        limit: int = 5,
        trigger: str = "agent_workbench_batch",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 5), 8))
        ids = []
        for value in job_candidate_ids or []:
            candidate_id = int(value)
            if candidate_id > 0 and candidate_id not in ids:
                ids.append(candidate_id)
        if not ids:
            ids = [
                int(item["job_candidate_id"])
                for item in self.get_workbench(limit=50)["items"]
                if item["kind"] in {"unassessed", "stale", "failed"}
            ]
        started: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for job_candidate_id in ids[:limit]:
            state = self.get_candidate_state(job_candidate_id)
            if state.get("stopped"):
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前关系已人工停止"})
                continue
            if not state.get("stale"):
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前判断仍有效"})
                continue
            if not state.get("auto_assess_allowed"):
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "运行中或处于失败冷却"})
                continue
            started.append(self.submit_assessment(job_candidate_id, trigger=trigger))
        return {"ok": True, "started": started, "skipped": skipped, "limit": limit}

    def auto_assess_all(
        self,
        *,
        limit: int = 50,
        trigger: str = "overview_auto_queue",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), 50))
        items = self.get_workbench(limit=limit)["items"]
        candidate_ids = [
            int(item["job_candidate_id"])
            for item in items
            if item["kind"] in {"unassessed", "stale"}
        ]
        started = []
        for job_candidate_id in candidate_ids:
            result = self.submit_assessment(job_candidate_id, trigger=trigger)
            started.append({"job_candidate_id": job_candidate_id, **result})
        return {
            "ok": True,
            "queued_total": len(started),
            "started": started,
            "limit": limit,
        }

    def _proposal_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        return {
            "id": item.get("id"),
            "proposal_id": item.get("proposal_id"),
            "job_candidate_id": item.get("job_candidate_id"),
            "assessment_id": item.get("assessment_id"),
            "candidate": item.get("candidate") or "",
            "company": item.get("company") or "",
            "title": item.get("candidate_title") or "",
            "client": item.get("client") or "",
            "job": item.get("job") or "",
            "action_type": item.get("action_type"),
            "risk_level": item.get("risk_level"),
            "title_text": item.get("title"),
            "rationale": item.get("rationale") or "",
            "request": _loads(item.get("request_json"), {}),
            "preflight": _loads(item.get("preflight_json"), {}),
            "status": item.get("status"),
            "reviewed_at": item.get("reviewed_at"),
            "review_note": item.get("review_note") or "",
            "expires_at": item.get("expires_at"),
            "created_at": item.get("created_at"),
        }

    def list_proposals(self, status: str = "pending", limit: int = 20) -> dict[str, Any]:
        status = str(status or "pending").strip().lower()
        allowed = {"pending", "approved", "rejected", "executed", "failed", "all"}
        if status not in allowed:
            raise ValueError("未知提案状态")
        limit = max(1, min(int(limit or 20), 100))
        where = "" if status == "all" else "WHERE p.status=?"
        params: tuple[Any, ...] = (limit,) if status == "all" else (status, limit)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT p.*,pe.display_name AS candidate,pe.current_company AS company,
                       pe.current_title AS candidate_title,jc.raw_client AS client,jc.raw_position AS job
                FROM agent_action_proposals p
                JOIN job_candidates jc ON jc.id=p.job_candidate_id
                JOIN people pe ON pe.id=jc.person_id
                {where}
                ORDER BY CASE p.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                         p.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return {"ok": True, "status": status, "proposals": [self._proposal_payload(row) for row in rows]}
        finally:
            conn.close()

    def generate_proposals(
        self,
        job_candidate_ids: list[int] | None = None,
        *,
        limit: int = 12,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 12), 50))
        ids: list[int] = []
        for value in job_candidate_ids or []:
            candidate_id = int(value)
            if candidate_id > 0 and candidate_id not in ids:
                ids.append(candidate_id)
        conn = self._connect()
        try:
            if ids:
                placeholders = ",".join("?" for _ in ids[:limit])
                rows = conn.execute(
                    f"""
                    SELECT * FROM agent_candidate_assessments
                    WHERE is_current=1 AND job_candidate_id IN ({placeholders})
                    ORDER BY id DESC
                    """,
                    tuple(ids[:limit]),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_candidate_assessments
                    WHERE is_current=1 AND recommendation IN ('priority_review','verify_first')
                    ORDER BY id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            created: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for row in rows:
                job_candidate_id = int(row["job_candidate_id"])
                context = build_candidate_context(self.db_path, job_candidate_id)
                current_snapshot_hash = self._snapshot_key(context)
                if is_stopped(context):
                    skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前关系已人工停止"})
                    continue
                if row["snapshot_hash"] != current_snapshot_hash:
                    skipped.append({"job_candidate_id": job_candidate_id, "reason": "判断已过期"})
                    continue
                assessment = self._assessment_payload(row)
                if assessment["recommendation"] not in {"priority_review", "verify_first"}:
                    skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前判断不生成任务建议"})
                    continue
                reason = assessment.get("next_action") or "人工核验 Agent 判断中的关键证据"
                request = {
                    "job_candidate_id": job_candidate_id,
                    "task_type": "agent_verification",
                    "reason": reason,
                    "due_at": "",
                    "priority": 1 if assessment["recommendation"] == "priority_review" else 2,
                    "write": True,
                }
                dedupe_key = hashlib.sha256(
                    f"proposal|{row['id']}|create_task|{_dumps(request)}".encode("utf-8")
                ).hexdigest()
                proposal_id = f"proposal_{row['id']}_{dedupe_key[:10]}"
                decision = action_decision("create_task", context)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_action_proposals
                    (proposal_id,job_candidate_id,assessment_id,snapshot_hash,dedupe_key,
                     action_type,risk_level,title,rationale,request_json,status,expires_at)
                    VALUES (?,?,?,?,?,'create_task',?,?,?,?,'pending',datetime('now','+7 days','localtime'))
                    """,
                    (
                        proposal_id,
                        job_candidate_id,
                        row["id"],
                        current_snapshot_hash,
                        dedupe_key,
                        decision["risk_level"],
                        "创建核验任务",
                        reason,
                        _dumps(request),
                    ),
                )
                conn.execute(
                    """
                    UPDATE agent_action_proposals
                    SET status='pending',review_note='',reviewed_at=NULL,action_id=NULL,
                        updated_at=datetime('now','localtime')
                    WHERE dedupe_key=? AND status='failed' AND snapshot_hash=?
                    """,
                    (dedupe_key, current_snapshot_hash),
                )
                proposal = conn.execute(
                    """
                    SELECT p.*,pe.display_name AS candidate,pe.current_company AS company,
                           pe.current_title AS candidate_title,jc.raw_client AS client,jc.raw_position AS job
                    FROM agent_action_proposals p
                    JOIN job_candidates jc ON jc.id=p.job_candidate_id
                    JOIN people pe ON pe.id=jc.person_id
                    WHERE p.dedupe_key=?
                    """,
                    (dedupe_key,),
                ).fetchone()
                if proposal:
                    created.append(self._proposal_payload(proposal))
            conn.commit()
            return {"ok": True, "proposals": created, "skipped": skipped}
        finally:
            conn.close()

    def proposal_preflight(self, proposal_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_action_proposals WHERE proposal_id=?",
                (str(proposal_id or ""),),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到 Agent 提案：{proposal_id}")
            if row["status"] not in {"pending", "approved"}:
                raise ValueError(f"提案当前状态不可确认：{row['status']}")
            if row["expires_at"] and conn.execute(
                "SELECT datetime(?) < datetime('now','localtime')", (row["expires_at"],)
            ).fetchone()[0]:
                conn.execute(
                    "UPDATE agent_action_proposals SET status='failed',review_note='提案已过期',updated_at=datetime('now','localtime') WHERE id=?",
                    (row["id"],),
                )
                conn.commit()
                raise ValueError("提案已过期，请重新生成")
            context = build_candidate_context(self.db_path, int(row["job_candidate_id"]))
            snapshot_hash = self._snapshot_key(context)
            if snapshot_hash != row["snapshot_hash"]:
                raise ValueError("提案依据已变化，请重新评估并生成")
            decision = action_decision(str(row["action_type"]), context)
            if decision["decision"] == "deny":
                raise ValueError(decision["reason"])
            token = secrets.token_urlsafe(24)
            signature = hashlib.sha256(
                f"{row['proposal_id']}|{row['snapshot_hash']}|{row['action_type']}|{row['request_json']}".encode("utf-8")
            ).hexdigest()
            preflight = {
                "decision": "confirm",
                "policy": decision,
                "proposal_id": row["proposal_id"],
                "action_type": row["action_type"],
                "request": _loads(row["request_json"], {}),
            }
            conn.execute(
                "UPDATE agent_action_proposals SET preflight_json=?,updated_at=datetime('now','localtime') WHERE id=?",
                (_dumps(preflight), row["id"]),
            )
            conn.commit()
            with self._lock:
                self._proposal_confirmations[token] = {
                    "proposal_id": row["proposal_id"],
                    "signature": signature,
                    "expires_at": time.time() + 300,
                }
            return {
                "ok": True,
                **preflight,
                "confirmation_token": token,
                "expires_in": 300,
            }
        finally:
            conn.close()

    def decide_proposal(
        self,
        proposal_id: str,
        confirmation_token: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        decision = str(decision or "").strip().lower()
        if decision not in {"approve", "reject"}:
            raise ValueError("decision 必须是 approve/reject")
        with self._lock:
            confirmation = self._proposal_confirmations.pop(str(confirmation_token or ""), None)
        if not confirmation or confirmation["expires_at"] < time.time():
            raise ValueError("提案确认令牌无效或已过期")
        if confirmation["proposal_id"] != str(proposal_id):
            raise ValueError("提案确认令牌与提案不匹配")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_action_proposals WHERE proposal_id=?",
                (str(proposal_id),),
            ).fetchone()
            if row is None or row["status"] not in {"pending", "approved"}:
                raise ValueError("提案已变化，请重新预检")
            signature = hashlib.sha256(
                f"{row['proposal_id']}|{row['snapshot_hash']}|{row['action_type']}|{row['request_json']}".encode("utf-8")
            ).hexdigest()
            if signature != confirmation["signature"]:
                raise ValueError("提案内容已变化，请重新预检")
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                """
                UPDATE agent_action_proposals
                SET status=?,review_note=?,reviewed_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime')
                WHERE id=?
                """,
                (status, str(note or ""), row["id"]),
            )
            conn.commit()
            return {
                "ok": True,
                "proposal_id": row["proposal_id"],
                "status": status,
                "action_type": row["action_type"],
                "job_candidate_id": row["job_candidate_id"],
                "request": _loads(row["request_json"], {}),
                "dedupe_key": row["dedupe_key"],
            }
        finally:
            conn.close()

    def finish_proposal(
        self,
        proposal_id: str,
        *,
        success: bool,
        action_id: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        status = "executed" if success else "failed"
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_action_proposals
                SET status=?,action_id=?,review_note=CASE WHEN ?='' THEN review_note ELSE ? END,
                    updated_at=datetime('now','localtime')
                WHERE proposal_id=? AND status='approved'
                """,
                (status, action_id, str(note or ""), str(note or ""), str(proposal_id)),
            )
            if conn.total_changes != 1:
                raise ValueError("提案不是待执行状态")
            conn.commit()
            return {"ok": True, "proposal_id": proposal_id, "status": status}
        finally:
            conn.close()

    def list_learning_rules(self, status: str = "all", limit: int = 50) -> dict[str, Any]:
        status = str(status or "all").strip().lower()
        if status not in {"all", "collecting", "pending", "active", "suspended", "revoked"}:
            raise ValueError("未知学习规则状态")
        limit = max(1, min(int(limit or 50), 200))
        where = "" if status == "all" else "WHERE status=?"
        params: tuple[Any, ...] = (limit,) if status == "all" else (status, limit)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM agent_learning_rules
                {where}
                ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'collecting' THEN 1 WHEN 'active' THEN 2 WHEN 'suspended' THEN 3 ELSE 4 END,
                         support_count DESC,id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            rules = []
            for row in rows:
                item = _row(row)
                item["rule"] = _loads(item.pop("rule_json", "{}"), {})
                rules.append(item)
            return {"ok": True, "status": status, "rules": rules}
        finally:
            conn.close()

    def get_evaluation(self, *, persist: bool = False) -> dict[str, Any]:
        conn = self._connect()
        try:
            metrics = compute_evaluation(conn)
            snapshot_id = ""
            if persist:
                snapshot_id = f"evaluation_{int(time.time())}_{secrets.token_hex(5)}"
                conn.execute(
                    """
                    INSERT INTO agent_evaluation_snapshots
                    (snapshot_id,metrics_json,sample_size)
                    VALUES (?,?,?)
                    """,
                    (snapshot_id, _dumps(metrics), int(metrics.get("reviewed_total") or 0)),
                )
                conn.commit()
            history_rows = conn.execute(
                """
                SELECT snapshot_id,metrics_json,sample_size,created_at
                FROM agent_evaluation_snapshots ORDER BY id DESC LIMIT 12
                """
            ).fetchall()
            history = [
                {
                    "snapshot_id": row["snapshot_id"],
                    "sample_size": row["sample_size"],
                    "created_at": row["created_at"],
                    "metrics": _loads(row["metrics_json"], {}),
                }
                for row in history_rows
            ]
            return {
                "ok": True,
                "snapshot_id": snapshot_id,
                "persisted": bool(persist),
                "metrics": metrics,
                "history": history,
            }
        finally:
            conn.close()

    def learning_preflight(self, rule_id: int) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_learning_rules WHERE id=?", (int(rule_id),)
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到学习规则：{rule_id}")
            if row["status"] != "pending":
                raise ValueError(f"学习规则当前状态不可确认：{row['status']}")
            if (
                int(row["support_count"] or 0) < int(self.config["learning"]["minimum_support"])
                or int(row["candidate_count"] or 0) < int(self.config["learning"]["minimum_candidates"])
            ):
                raise ValueError("学习规则尚未达到支持样本阈值")
            token = secrets.token_urlsafe(24)
            signature = hashlib.sha256(
                f"{row['id']}|{row['rule_key']}|{row['version']}|{row['rule_json']}".encode("utf-8")
            ).hexdigest()
            with self._lock:
                self._learning_confirmations[token] = {
                    "rule_id": int(row["id"]),
                    "signature": signature,
                    "expires_at": time.time() + 300,
                }
            return {
                "ok": True,
                "decision": "confirm",
                "confirmation_token": token,
                "expires_in": 300,
                "rule": {
                    "id": row["id"],
                    "client": row["client"],
                    "job": row["job"],
                    "rule_type": row["rule_type"],
                    "rule": _loads(row["rule_json"], {}),
                    "version": row["version"],
                    "support_count": row["support_count"],
                    "candidate_count": row["candidate_count"],
                },
            }
        finally:
            conn.close()

    def learning_commit(self, rule_id: int, confirmation_token: str) -> dict[str, Any]:
        with self._lock:
            confirmation = self._learning_confirmations.pop(str(confirmation_token or ""), None)
        if not confirmation or confirmation["expires_at"] < time.time():
            raise ValueError("学习规则确认令牌无效或已过期")
        if confirmation["rule_id"] != int(rule_id):
            raise ValueError("学习规则确认令牌与规则不匹配")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_learning_rules WHERE id=?", (int(rule_id),)
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("学习规则已变化，请重新预检")
            signature = hashlib.sha256(
                f"{row['id']}|{row['rule_key']}|{row['version']}|{row['rule_json']}".encode("utf-8")
            ).hexdigest()
            if signature != confirmation["signature"]:
                raise ValueError("学习规则内容已变化，请重新预检")
            conn.execute(
                """
                UPDATE agent_learning_rules SET status='revoked',revoked_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime')
                WHERE rule_key=? AND status='active' AND id<>?
                """,
                (row["rule_key"], row["id"]),
            )
            conn.execute(
                """
                UPDATE agent_learning_rules SET status='active',approved_at=datetime('now','localtime'),
                    updated_at=datetime('now','localtime') WHERE id=?
                """,
                (row["id"],),
            )
            conn.commit()
            result = {"ok": True, "rule_id": row["id"], "status": "active"}
        finally:
            conn.close()
        self.store_memory(
            scope_type="job", scope_id=row["job"], memory_type="approved_learning_rule",
            content=f"已批准的岗位判断规则：{row['rule_json']}",
            source_type="agent_learning_rule", source_id=row["id"], confidence=1.0,
        )
        return result
