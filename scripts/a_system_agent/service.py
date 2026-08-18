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
from .capability_runtime import (
    SERVICE_HANDLED_CAPABILITY_IDS,
    RecruitingCapabilityRuntime,
    ZERO_RESULT_ATTRIBUTION_LABELS,
    assert_workflow_capabilities_resolvable,
)
from .context import build_candidate_context
from .evaluation import compute_evaluation
from .job_status import job_status_intake_allowed
from .llm import BaseLLM, LLMError, PROMPT_VERSION, create_default_llm
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
from .privacy import sanitize_context_snapshot, sanitize_payload
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
    "stopped_neutral": 0.0,
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
    "stopped_neutral": "候选人意向不足停止",
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



# ---- Handler imports (methods split into separate modules) ----
from .copilot_handler import (
    copilot as _h_copilot,
    _copilot_impl as _h_copilot_impl,
    chat as _h_chat,
    _normalize_copilot_context as _h_normalize_copilot_context,
    _default_outreach_queue_inputs as _h_default_outreach_queue_inputs,
    _floating_bridge_evidence as _h_floating_bridge_evidence,
    _uploaded_attachment_evidence as _h_uploaded_attachment_evidence,
    _mentioned_jobs_for_copilot as _h_mentioned_jobs_for_copilot,
    get_copilot_focus as _h_get_copilot_focus,
    get_copilot_context_state as _h_get_copilot_context_state,
    _copilot_action_kind as _h_copilot_action_kind,
    _resolve_strategy_revision_workflow as _h_resolve_strategy_revision_workflow,
    _copilot_focus_context_facts as _h_copilot_focus_context_facts,
    _copilot_workflow_context_facts as _h_copilot_workflow_context_facts,
    _copilot_context_facts as _h_copilot_context_facts,
    _copilot_context_from_focus as _h_copilot_context_from_focus,
    _copilot_workflow_outcome_context as _h_copilot_workflow_outcome_context,
    _persist_copilot_focus as _h_persist_copilot_focus,
    get_copilot_session as _h_get_copilot_session,
    search_copilot_session_messages as _h_search_copilot_session_messages,
    list_copilot_sessions as _h_list_copilot_sessions,
    update_copilot_session as _h_update_copilot_session,
    archive_all_copilot_sessions as _h_archive_all_copilot_sessions,
    _copilot_conversation_history as _h_copilot_conversation_history,
    _copilot_session_business_evidence as _h_copilot_session_business_evidence,
    _ground_copilot_goal as _h_ground_copilot_goal,
    _pending_strategy_clarification as _h_pending_strategy_clarification,
    _sourcing_strategy_gate as _h_sourcing_strategy_gate,
    _mentioned_client_names as _h_mentioned_client_names,
    _route_copilot_skills as _h_route_copilot_skills,
    copilot_stream_generator as _h_copilot_stream_generator,
    _copilot_conversation_context as _h_copilot_conversation_context,
    _maybe_summarize_copilot_conversation as _h_maybe_summarize_copilot_conversation,
    _ensure_copilot_summaries_table as _h_ensure_copilot_summaries_table,
    record_copilot_event as _h_record_copilot_event,
)
from .sourcing_handler import (
    _ensure_sourcing_attribution as _h_ensure_sourcing_attribution,
    record_sourcing_business_signal as _h_record_sourcing_business_signal,
    analyze_stop_note as _h_analyze_stop_note,
    _channel_analytics as _h_channel_analytics,
    get_dashboard as _h_get_dashboard,
)
from .assessment_handler import (
    _current_assessed_candidates as _h_current_assessed_candidates,
    generate_candidate_assessment as _h_generate_candidate_assessment,
    get_candidate_assessment as _h_get_candidate_assessment,
    refresh_candidate_fit_assessment as _h_refresh_candidate_fit_assessment,
    update_candidate_assessment_advisor_action as _h_update_candidate_assessment_advisor_action,
    assessment_calibration_metrics as _h_assessment_calibration_metrics,
    generate_assessment_calibration_report as _h_generate_assessment_calibration_report,
    submit_assessment as _h_submit_assessment,
    _run_assessment as _h_run_assessment,
    submit_panel_review as _h_submit_panel_review,
    _run_panel_review as _h_run_panel_review,
    _finish_run as _h_finish_run,
    _persist_assessment as _h_persist_assessment,
    _upsert_candidate_intelligence as _h_upsert_candidate_intelligence,
    _assessment_payload as _h_assessment_payload,
    _panel_payload as _h_panel_payload,
    get_panel_state as _h_get_panel_state,
    get_run as _h_get_run,
    get_candidate_state as _h_get_candidate_state,
    _candidate_agent_artifacts as _h_candidate_agent_artifacts,
    _snapshot_key as _h_snapshot_key,
    stage_shadow_decision as _h_stage_shadow_decision,
    _skill_job_diagnosis as _h_skill_job_diagnosis,
    _skill_candidate_assessment as _h_skill_candidate_assessment,
    _skill_verification_plan as _h_skill_verification_plan,
    _skill_communication_draft as _h_skill_communication_draft,
    _skill_liepin_resume_capture as _h_skill_liepin_resume_capture,
    capture_liepin_resume as _h_capture_liepin_resume,
    ensure_verification_task as _h_ensure_verification_task,
    batch_assess as _h_batch_assess,
    auto_assess_all as _h_auto_assess_all,
)
from .workflow_handler import (
    get_workflow as _h_get_workflow,
    get_workflow_summary as _h_get_workflow_summary,
    get_workflow_step as _h_get_workflow_step,
    get_workflow_candidates as _h_get_workflow_candidates,
    start_workflow as _h_start_workflow,
    revise_workflow as _h_revise_workflow,
    revert_workflow_revision as _h_revert_workflow_revision,
    cancel_workflow as _h_cancel_workflow,
    pause_workflow as _h_pause_workflow,
    resume_workflow as _h_resume_workflow,
    archive_workflow as _h_archive_workflow,
    retry_workflow_step as _h_retry_workflow_step,
    complete_external_workflow_step as _h_complete_external_workflow_step,
    schedule_external_workflow_step as _h_schedule_external_workflow_step,
    _execute_external_workflow_step as _h_execute_external_workflow_step,
    validate_external_result as _h_validate_external_result,
    apply_external_result as _h_apply_external_result,
    decide_workflow_approval as _h_decide_workflow_approval,
    get_workflow_artifact as _h_get_workflow_artifact,
    get_workflow_events as _h_get_workflow_events,
    get_workflow_quality as _h_get_workflow_quality,
    record_workflow_feedback as _h_record_workflow_feedback,
    _execute_workflow_capability as _h_execute_workflow_capability,
    _run_opencli as _h_run_opencli,
    _skill_opencli_usage as _h_skill_opencli_usage,
    _skill_opencli_browser_read as _h_skill_opencli_browser_read,
    _skill_document_understanding as _h_skill_document_understanding,
)
from .strategy_handler import (
    get_strategy_review as _h_get_strategy_review,
    rebuild_strategy_review as _h_rebuild_strategy_review,
    apply_strategy_review_diff_decisions as _h_apply_strategy_review_diff_decisions,
    create_mapping_task as _h_create_mapping_task,
    get_mapping_task as _h_get_mapping_task,
    update_mapping_candidate as _h_update_mapping_candidate,
    regenerate_mapping_icebreaker as _h_regenerate_mapping_icebreaker,
    intake_mapping_candidate as _h_intake_mapping_candidate,
    submit_job_profile_refresh as _h_submit_job_profile_refresh,
    backflow_mapping_task as _h_backflow_mapping_task,
    mapping_metrics as _h_mapping_metrics,
    _proposal_payload as _h_proposal_payload,
    list_proposals as _h_list_proposals,
    generate_proposals as _h_generate_proposals,
    proposal_preflight as _h_proposal_preflight,
    decide_proposal as _h_decide_proposal,
    finish_proposal as _h_finish_proposal,
    execute_proposal as _h_execute_proposal,
    list_learning_rules as _h_list_learning_rules,
    learning_preflight as _h_learning_preflight,
    learning_commit as _h_learning_commit,
    create_radar_scan as _h_create_radar_scan,
    get_latest_radar_scan as _h_get_latest_radar_scan,
    start_mapping_from_radar as _h_start_mapping_from_radar,
    activate_radar_company as _h_activate_radar_company,
    create_radar_weekly_report as _h_create_radar_weekly_report,
    get_latest_radar_weekly_report as _h_get_latest_radar_weekly_report,
)
from .strategy_editor import (
    apply_strategy_item_edits as _h_apply_strategy_item_edits,
    preflight_strategy_item_edits as _h_preflight_strategy_item_edits,
)


class AgentService:
    def __init__(self, db_path: str | Path, llm: BaseLLM | None = None, max_workers: int | None = None) -> None:
        self.db_path = Path(db_path).expanduser()
        self.config = load_config()
        self.llm = llm or create_default_llm(self.config, db_path=self.db_path)
        # S6-2 动机维度公司近况采集器（只读公网页面）；测试注入 stub 防真实网络。
        self.assessment_signal_fetcher: Any = None
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
        self.workflow_engine.recover_external_continuations()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)

    def get_public_config(self) -> dict[str, Any]:
        return {
            "ok": True,
            "config": public_config(
                self.config,
                model_available=self.llm.model != "unavailable",
                strong_model_available=self.llm.has_strong_copilot_model(),
            ),
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
            ("candidate_pool_filter", "候选池分级过滤", "assessment", "R0", "native", ("pool_filter_result",)),
            ("outreach_queue", "触达队列生成", "outreach", "R1", "native", ("outreach_queue_proposals",)),
            ("pool_gap_advice", "缺口补池建议", "sourcing", "R0", "native", ("pool_gap_advice",)),
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
            "job_publish_execute": ("job",), "resume_export": ("candidate",), "candidate_batch_assessment": ("job", "candidate", "queue"), "candidate_pool_filter": ("job",),
            "outreach_queue": ("job", "candidate", "queue"), "pool_gap_advice": ("job",),
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
            action_kind = "external_write" if risk == "R3" else "internal_write" if risk == "R2" else "draft" if risk == "R1" else "read"
            preflight_mode = "required" if risk in {"R2", "R3"} else "preview" if risk == "R1" else "none"
            confirmation_surface = "workflow_approval" if risk == "R3" else "floating_card" if risk == "R2" else "none"
            post_check = "external_evidence" if risk == "R3" else "result" if risk in {"R1", "R2"} else "none"
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
                    action_kind=action_kind, preflight_mode=preflight_mode,
                    confirmation_surface=confirmation_surface, post_check=post_check,
                    audit_event_type=f"capability.{capability_id}",
                )
            )
        # 能力完整性门禁：注册的工作流能力必须能在启动时解析到确定性 Runner 或服务层处理器，
        # 避免“已注册但无执行实现”的能力在运行时才以原始异常暴露给用户。
        assert_workflow_capabilities_resolvable(
            workflow_capabilities,
            self.capability_runtime.deterministic_runner_ids(),
            SERVICE_HANDLED_CAPABILITY_IDS,
        )

    def list_skills(self) -> dict[str, Any]:
        return {"ok": True, "skills": self.skills.list()}

    def record_context_snapshot(self, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(source or "unknown").strip().lower()[:80] or "unknown"
        sanitized = sanitize_context_snapshot(payload if isinstance(payload, dict) else {})
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
                    "payload": sanitize_context_snapshot(_loads(row["payload_json"], {})),
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

    def get_flow_inbox(
        self, queue: str = "今日待办", client: str = "", job: str = "", search: str = "",
        view: str = "action", limit: int = 100, **_: Any,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit or 100), 1000))
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


# ---- Bind handler functions as class methods ----
# Copilot
AgentService.copilot = _h_copilot
AgentService._copilot_impl = _h_copilot_impl
AgentService._default_outreach_queue_inputs = _h_default_outreach_queue_inputs
AgentService.chat = _h_chat
AgentService._normalize_copilot_context = _h_normalize_copilot_context
AgentService._floating_bridge_evidence = _h_floating_bridge_evidence
AgentService._uploaded_attachment_evidence = _h_uploaded_attachment_evidence
AgentService._mentioned_jobs_for_copilot = _h_mentioned_jobs_for_copilot
AgentService.get_copilot_focus = _h_get_copilot_focus
AgentService.get_copilot_context_state = _h_get_copilot_context_state
AgentService._copilot_action_kind = staticmethod(_h_copilot_action_kind)
AgentService._resolve_strategy_revision_workflow = _h_resolve_strategy_revision_workflow
AgentService._copilot_focus_context_facts = _h_copilot_focus_context_facts
AgentService._copilot_workflow_context_facts = _h_copilot_workflow_context_facts
AgentService._copilot_context_facts = _h_copilot_context_facts
AgentService._copilot_context_from_focus = _h_copilot_context_from_focus
AgentService._copilot_workflow_outcome_context = _h_copilot_workflow_outcome_context
AgentService._persist_copilot_focus = _h_persist_copilot_focus
AgentService.get_copilot_session = _h_get_copilot_session
AgentService.search_copilot_session_messages = _h_search_copilot_session_messages
AgentService.list_copilot_sessions = _h_list_copilot_sessions
AgentService.update_copilot_session = _h_update_copilot_session
AgentService.archive_all_copilot_sessions = _h_archive_all_copilot_sessions
AgentService._copilot_conversation_history = _h_copilot_conversation_history
AgentService._copilot_session_business_evidence = _h_copilot_session_business_evidence
AgentService._ground_copilot_goal = _h_ground_copilot_goal
AgentService._pending_strategy_clarification = _h_pending_strategy_clarification
AgentService._sourcing_strategy_gate = _h_sourcing_strategy_gate
AgentService._mentioned_client_names = _h_mentioned_client_names
AgentService._route_copilot_skills = _h_route_copilot_skills
AgentService.copilot_stream_generator = _h_copilot_stream_generator
# Agent mode and streaming mode share the same turn compiler, context state,
# read-only tool loop, and action authorization boundary.
AgentService.copilot_agent = _h_copilot
AgentService._copilot_conversation_context = _h_copilot_conversation_context
AgentService._maybe_summarize_copilot_conversation = _h_maybe_summarize_copilot_conversation
AgentService._ensure_copilot_summaries_table = _h_ensure_copilot_summaries_table
AgentService.record_copilot_event = _h_record_copilot_event

# Sourcing
AgentService._ensure_sourcing_attribution = _h_ensure_sourcing_attribution
AgentService.record_sourcing_business_signal = _h_record_sourcing_business_signal
AgentService.analyze_stop_note = _h_analyze_stop_note
AgentService._channel_analytics = _h_channel_analytics
AgentService.get_dashboard = _h_get_dashboard

# Assessment
AgentService._current_assessed_candidates = _h_current_assessed_candidates
AgentService.generate_candidate_assessment = _h_generate_candidate_assessment
AgentService.get_candidate_assessment = _h_get_candidate_assessment
AgentService.refresh_candidate_fit_assessment = _h_refresh_candidate_fit_assessment
AgentService.update_candidate_assessment_advisor_action = _h_update_candidate_assessment_advisor_action
AgentService.assessment_calibration_metrics = _h_assessment_calibration_metrics
AgentService.generate_assessment_calibration_report = _h_generate_assessment_calibration_report
AgentService.submit_assessment = _h_submit_assessment
AgentService._run_assessment = _h_run_assessment
AgentService.submit_panel_review = _h_submit_panel_review
AgentService._run_panel_review = _h_run_panel_review
AgentService._finish_run = _h_finish_run
AgentService._persist_assessment = _h_persist_assessment
AgentService._upsert_candidate_intelligence = _h_upsert_candidate_intelligence
AgentService._assessment_payload = _h_assessment_payload
AgentService._panel_payload = _h_panel_payload
AgentService.get_panel_state = _h_get_panel_state
AgentService.get_run = _h_get_run
AgentService.get_candidate_state = _h_get_candidate_state
AgentService._candidate_agent_artifacts = _h_candidate_agent_artifacts
AgentService._snapshot_key = _h_snapshot_key
AgentService.stage_shadow_decision = _h_stage_shadow_decision
AgentService._skill_job_diagnosis = _h_skill_job_diagnosis
AgentService._skill_candidate_assessment = _h_skill_candidate_assessment
AgentService._skill_verification_plan = _h_skill_verification_plan
AgentService._skill_communication_draft = _h_skill_communication_draft
AgentService._skill_liepin_resume_capture = _h_skill_liepin_resume_capture
AgentService.capture_liepin_resume = _h_capture_liepin_resume
AgentService.ensure_verification_task = _h_ensure_verification_task
AgentService.batch_assess = _h_batch_assess
AgentService.auto_assess_all = _h_auto_assess_all

# Workflow
AgentService.get_workflow = _h_get_workflow
AgentService.get_workflow_summary = _h_get_workflow_summary
AgentService.get_workflow_step = _h_get_workflow_step
AgentService.get_workflow_candidates = _h_get_workflow_candidates
AgentService.start_workflow = _h_start_workflow
AgentService.revise_workflow = _h_revise_workflow
AgentService.revert_workflow_revision = _h_revert_workflow_revision
AgentService.cancel_workflow = _h_cancel_workflow
AgentService.pause_workflow = _h_pause_workflow
AgentService.resume_workflow = _h_resume_workflow
AgentService.archive_workflow = _h_archive_workflow
AgentService.retry_workflow_step = _h_retry_workflow_step
AgentService.complete_external_workflow_step = _h_complete_external_workflow_step
AgentService.schedule_external_workflow_step = _h_schedule_external_workflow_step
AgentService._execute_external_workflow_step = _h_execute_external_workflow_step
AgentService.validate_external_result = staticmethod(_h_validate_external_result)
AgentService.apply_external_result = _h_apply_external_result
AgentService.decide_workflow_approval = _h_decide_workflow_approval
AgentService.get_workflow_artifact = _h_get_workflow_artifact
AgentService.get_workflow_events = _h_get_workflow_events
AgentService.get_workflow_quality = _h_get_workflow_quality
AgentService.record_workflow_feedback = _h_record_workflow_feedback
AgentService._execute_workflow_capability = _h_execute_workflow_capability
AgentService._run_opencli = _h_run_opencli
AgentService._skill_opencli_usage = _h_skill_opencli_usage
AgentService._skill_opencli_browser_read = _h_skill_opencli_browser_read
AgentService._skill_document_understanding = _h_skill_document_understanding

# Strategy
AgentService.get_strategy_review = _h_get_strategy_review
AgentService.rebuild_strategy_review = _h_rebuild_strategy_review
AgentService.apply_strategy_review_diff_decisions = _h_apply_strategy_review_diff_decisions
AgentService.create_mapping_task = _h_create_mapping_task
AgentService.get_mapping_task = _h_get_mapping_task
AgentService.update_mapping_candidate = _h_update_mapping_candidate
AgentService.regenerate_mapping_icebreaker = _h_regenerate_mapping_icebreaker
AgentService.intake_mapping_candidate = _h_intake_mapping_candidate
AgentService.submit_job_profile_refresh = _h_submit_job_profile_refresh
AgentService.backflow_mapping_task = _h_backflow_mapping_task
AgentService.mapping_metrics = _h_mapping_metrics
AgentService._proposal_payload = _h_proposal_payload
AgentService.list_proposals = _h_list_proposals
AgentService.generate_proposals = _h_generate_proposals
AgentService.proposal_preflight = _h_proposal_preflight
AgentService.decide_proposal = _h_decide_proposal
AgentService.finish_proposal = _h_finish_proposal
AgentService.execute_proposal = _h_execute_proposal
AgentService.list_learning_rules = _h_list_learning_rules
AgentService.learning_preflight = _h_learning_preflight
AgentService.learning_commit = _h_learning_commit
AgentService.create_radar_scan = _h_create_radar_scan
AgentService.get_latest_radar_scan = _h_get_latest_radar_scan
AgentService.start_mapping_from_radar = _h_start_mapping_from_radar
AgentService.activate_radar_company = _h_activate_radar_company
AgentService.create_radar_weekly_report = _h_create_radar_weekly_report
AgentService.get_latest_radar_weekly_report = _h_get_latest_radar_weekly_report

# Strategy item edits（按项编辑）
AgentService.apply_strategy_item_edits = _h_apply_strategy_item_edits
AgentService.preflight_strategy_item_edits = _h_preflight_strategy_item_edits
