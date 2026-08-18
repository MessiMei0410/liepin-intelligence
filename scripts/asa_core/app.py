from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

SCRIPT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import liepin_workbench_server as legacy
from a_system_agent.service import AgentService
from a_system_agent.native_attachments import (
    MAX_ATTACHMENT_BYTES,
    SUPPORTED_EXTENSIONS,
    extract_local_document,
)

from .analytics import AnalyticsService
from .company_calibration import CompanyCalibrationService
from .database import DEFAULT_DB, migrate, transaction
from .knowledge_proposals import KnowledgeProposalService
from .scheduler import Scheduler
from .service import CoreService


ASA_WEB_DIST = Path(os.environ.get("ASA_WEB_DIST", str(REPO_DIR / "asa-web" / "dist"))).expanduser()
ASA_APP_USER_AGENT_PREFIX = "ASAApp/"
# DSH 桥接：本地共享密钥（0600）供 Core 下发 + DSH 常驻服务器校验；缺失=未启用鉴权。
ASA_DSH_TOKEN_FILE = Path(os.environ.get("ASA_DSH_TOKEN_FILE", str(Path.home() / ".dsh" / "asa-bridge-token"))).expanduser()
ASA_DSH_RESIDENT_URL = os.environ.get("ASA_DSH_RESIDENT_URL", "http://127.0.0.1:8891/turn")


def read_dsh_token() -> str:
    try:
        return ASA_DSH_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
TRUSTED_BROWSER_ORIGINS = {
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "chrome-extension://aihpahceageafhjhedhmeikhcfbfoffn",
    "chrome-extension://cecifklpjckkbclegnmapegnedelapjh",
}


def trusted_browser_origins(host: str, port: int) -> set[str]:
    origins = set(TRUSTED_BROWSER_ORIGINS)
    if str(host or "").strip().lower() in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}:
        origins.update({f"http://127.0.0.1:{int(port)}", f"http://localhost:{int(port)}"})
    return origins

# Bootstrap 接口内存缓存 (TTL 5 秒)
_bootstrap_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_BOOTSTRAP_CACHE_TTL = 5.0


class WriteEnvelope(BaseModel):
    request_id: str = Field(min_length=4)


class MemoryStore(WriteEnvelope):
    scope_type: str = Field(default="global", pattern="^(global|client|job|candidate)$")
    scope_id: str | None = None
    memory_type: str = "fact"
    content: str = Field(min_length=1)
    source_type: str = "copilot"
    confidence: float = 1.0


class SchedulerTaskCreate(WriteEnvelope):
    name: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    cron_expr: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class SchedulerTaskPatch(WriteEnvelope):
    action: Literal["pause", "resume"]


class WorkflowCreate(WriteEnvelope):
    objective: str = Field(min_length=2)
    context: dict[str, Any] = Field(default_factory=dict)
    priority: int = 2


class WorkflowAction(WriteEnvelope):
    instruction: str = ""
    note: str = ""
    expected_plan_version: int | None = None
    expected_plan_hash: str = ""


class ApprovalDecision(WriteEnvelope):
    decision: str
    note: str = ""


class CopilotMessage(WriteEnvelope):
    message: str = Field(min_length=1)
    session_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class CandidateListRefreshBody(BaseModel):
    """名单卡刷新：仅需 job_id（路径）+ 可选 bonder 标记（原卡有固晶优先组时传 true 保持分组）。"""
    bonder: bool = False
    filter_mode: str = ""


class CopilotAttachmentUpload(WriteEnvelope):
    file_name: str = Field(min_length=1, max_length=240)
    mime_type: str = Field(default="", max_length=160)
    size_bytes: int = Field(ge=1, le=MAX_ATTACHMENT_BYTES)
    content_base64: str = Field(min_length=1, max_length=36 * 1024 * 1024)


class CopilotAttachmentResponseItem(BaseModel):
    attachment_id: str
    access_token: str
    file_name: str
    file_type: str
    mime_type: str
    size_bytes: int
    content_available: bool
    truncated: bool
    is_image: bool
    status: str


class CopilotAttachmentUploadResponse(BaseModel):
    ok: bool
    attachment: CopilotAttachmentResponseItem


class CopilotSessionPatch(WriteEnvelope):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    archived: bool | None = None
    clear_focus: bool = False


class CopilotReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    id: str | int
    label: str
    subtitle: str | None = None
    href: str | None = None


class CopilotMessageResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["user", "assistant"]
    content: str
    context: dict[str, Any] = Field(default_factory=dict)
    references: list[CopilotReferenceResponse] = Field(default_factory=list)
    suggested_actions: list[dict[str, Any]] = Field(default_factory=list)
    business_focus: dict[str, Any] | None = None
    workflow_id: str | None = None
    workflow_progress: dict[str, Any] | None = None
    pending_intent: dict[str, Any] | None = None
    action_card: dict[str, Any] | None = None
    model_participation: dict[str, Any] | None = None
    created_at: str | None = None


class CopilotSessionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str
    title: str
    preview: str
    message_count: int = Field(ge=0)
    updated_at: str | None = None
    context_type: str | None = None
    context_id: str | int | None = None
    business_focus: dict[str, Any] | None = None
    archived: bool = False


class CopilotSessionListResponse(BaseModel):
    ok: bool = True
    sessions: list[CopilotSessionSummaryResponse] = Field(default_factory=list)


class CopilotTurnRecordRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=20000)
    answer: str = Field(default="", max_length=100000)
    context: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="dsh", max_length=40)
    model: str = Field(default="", max_length=120)


class CopilotTurnRecordResponse(BaseModel):
    ok: bool = True
    session_id: str = ""
    recorded: bool = False


class CopilotSessionDetailResponse(BaseModel):
    ok: bool = True
    session_id: str
    messages: list[CopilotMessageResponse] = Field(default_factory=list)
    business_focus: dict[str, Any] | None = None
    total: int = 0
    has_more: bool = False


class CopilotMessageSearchMatchResponse(BaseModel):
    role: str
    created_at: str | None = None
    content: str = ""
    snippet: str = ""
    newer_count: int = Field(ge=0)


class CopilotMessageSearchResponse(BaseModel):
    ok: bool = True
    session_id: str
    query: str = ""
    matches: list[CopilotMessageSearchMatchResponse] = Field(default_factory=list)
    total: int = 0


class CopilotSessionUpdateResponse(BaseModel):
    ok: bool = True
    session_id: str
    title: str
    archived: bool
    business_focus: dict[str, Any] | None = None


class CopilotSessionBulkArchiveResponse(BaseModel):
    ok: bool = True
    archived_count: int = Field(ge=0)
    session_ids: list[str] = Field(default_factory=list)


class CopilotEvent(WriteEnvelope):
    session_id: str = ""
    event: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class CopilotIntentConfirm(WriteEnvelope):
    intent: dict[str, Any] = Field(default_factory=dict)
    intent_hash: str = ""
    candidate_id: int = 0
    preflight_token: str = ""
    message: str = ""
    session_id: str = ""


class DiffDecision(BaseModel):
    diff_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class StrategyReviewDiffDecisions(WriteEnvelope):
    decisions: list[DiffDecision] = Field(min_length=1)


class StrategyItemEdits(WriteEnvelope):
    # 寻访策略结构化按项编辑：edits 逐项为 {op, ...}（op 枚举见 strategy_editor.SUPPORTED_OPS，
    # 服务层逐项校验并给可读 409）；编辑落新 strategy revision，不原地替换。
    edits: list[dict[str, Any]] = Field(min_length=1)
    note: str = ""
    expected_strategy_hash: str = ""
    preflight_token: str = ""


class MappingTaskCreate(WriteEnvelope):
    # S5-1：decision_tree_exhausted=扩池决策树 escalate_mapping 步触发；manual=顾问手动发起
    trigger: str = "manual"


class MappingCandidatePatch(WriteEnvelope):
    # S5-2：状态机 PATCH；status/consultant_note 至少其一（路由侧校验，缺省 → 422）。
    # 七态枚举 + 合法迁移由状态机校验（非法 → 409）；intaken 只能经 intake 动作到达。
    status: str | None = None
    consultant_note: str | None = None


class RadarScanCreate(WriteEnvelope):
    # S7-1：手动触发一次人才流动雷达扫描（无额外表单字段；同日幂等更新同一 artifact）
    pass


class RadarStartMappingCreate(WriteEnvelope):
    # S7-2：对最新雷达榜单里的一家公司发起 Mapping 直挖（trigger=radar 由后端锚定，前端不传）。
    # 同日幂等：同岗位同公司当天重复发起返回已存在任务卡，不重复创建。
    company: str = Field(min_length=1)
    job_id: int = Field(gt=0)


class RadarWeeklyReportCreate(WriteEnvelope):
    # S7-3：生成雷达周报（无额外表单字段；同日幂等更新同一 artifact；生成后推 Copilot 提醒）
    pass


class AssessmentAdvisorActionPatch(WriteEnvelope):
    # S6-1b：判人评估顾问动作写回；action 四枚举（非法 → 409），note 选填（改判时附口径）。
    action: str = Field(min_length=1)
    note: str = ""


class JobProfileFeedbackCreate(WriteEnvelope):
    # S8：岗位画像顾问纠正（"不对"按钮）；item_type 四枚举 duty/tool/deliverable/customer，
    # item_key 为条目原文（后端规范化归并键），非法 → 409。
    item_type: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    item_label: str = ""
    note: str = ""


class CalibrationReportCreate(WriteEnvelope):
    # S6-4：校准周报手动触发（无额外表单字段；markdown 输出到 work/calibration/，不进 git）
    pass


class CandidateAction(BaseModel):
    request_id: str = Field(min_length=4)
    candidate_id: int
    action: str
    note: str = ""
    reason: str = ""
    preflight_token: str = ""


class SourcingAdjustmentDecision(WriteEnvelope):
    pass


class SourcingAdjustmentEffectResponse(BaseModel):
    baseline: dict[str, int]
    current: dict[str, int]
    diff: dict[str, int]


class SourcingAdjustmentItemResponse(BaseModel):
    id: int
    job_id: int
    candidate_id: int | None = None
    candidate_name: str = ""
    candidate_display_name: str = ""
    adjust_type: Literal[
        "add_keyword", "remove_keyword", "exclude_company", "add_company",
        "add_filter", "adjust_salary_range",
    ]
    value: str
    rationale: str = ""
    confidence: float = 0.5
    status: Literal["pending", "accepted", "applied", "ignored"]
    created_at: str = ""
    accepted_at: str | None = None
    applied_at: str | None = None
    applied_round: int | None = None
    applied_workflow_id: str | None = None
    applied_artifact_id: str | None = None
    effect: SourcingAdjustmentEffectResponse | None = None


class SourcingAdjustmentSummaryResponse(BaseModel):
    pending: int = 0
    accepted: int = 0
    applied: int = 0
    ignored: int = 0


class SourcingAdjustmentListResponse(BaseModel):
    ok: bool = True
    items: list[SourcingAdjustmentItemResponse] = Field(default_factory=list)
    summary: SourcingAdjustmentSummaryResponse


class SourcingAdjustmentDecisionResponse(SourcingAdjustmentItemResponse):
    ok: bool = True
    already_accepted: bool = False
    receipt: dict[str, Any] | None = None


class ConsultantRecommendationPreflight(WriteEnvelope):
    candidate_id: int


class ConsultantRecommendationCommit(ConsultantRecommendationPreflight):
    # 顾问确认推荐必须附原因；缺失/空串在服务层也兜底拒绝（409）。
    reason: str = Field(min_length=1)
    preflight_token: str = ""


class PackageFeedbackCreate(WriteEnvelope):
    # 版本化推荐包客户反馈：feedback_type 五枚举 approved/interview/rejected/hold/other
    # （非法 → 409），content 必填（空白 → 409），feedback_time 选填（默认服务端当前时间）。
    feedback_type: str = Field(min_length=1)
    content: str = Field(min_length=1)
    feedback_time: str = ""


class PackageUpgradePreflight(WriteEnvelope):
    # 推荐包升版预检（P3-a）：package_id 同时在路径与 body（路径为准）。
    package_id: str = Field(min_length=1)


class PackageUpgradeCommit(PackageUpgradePreflight):
    # 推荐包升版提交：preflight 一次性 token + Idempotency-Key 幂等。
    preflight_token: str = ""


class JobWeeklyReportCreate(WriteEnvelope):
    # 岗位周报手动生成（无额外表单字段；同周幂等更新同一 artifact，version 自增留痕）
    pass


class LifecycleEventCreate(WriteEnvelope):
    # 生命周期一等事件（面试/Offer/入职）：event_type 六枚举 interview_scheduled/interview_completed/
    # offer_extended/offer_accepted/offer_declined/onboarded（非法 → 409）；occurred_at 选填
    # （默认服务端当前时间，格式非法 → 409）；event_status 选填（缺省用事件类型默认状态，非法 → 409）。
    event_type: str = Field(min_length=1)
    occurred_at: str = ""
    event_status: str = ""
    notes: str = ""


class KnowledgeProposalGenerate(WriteEnvelope):
    # 知识增补提案生成（二期知识飞轮）：确定性扫描停止原因/客户反馈/确认推荐聚类，
    # 证据不足只留候选；同内容幂等不重复提案。
    limit: int = Field(default=50, ge=1, le=200)


class KnowledgeProposalDecision(WriteEnvelope):
    # 两段确认：decision 二枚举 accept/reject；reject 必须附 note（服务层兜底 409）。
    confirmation_token: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    note: str = ""


class CompanyCalibrationSubmit(WriteEnvelope):
    # 二期知识飞轮：核心公司校准提交。status 三枚举 calibrated/rejected/needs_review
    # （非法 → 409）；公司必须已在图谱（404）；同内容重复提交不 bump version（服务层幂等）。
    company_name: str = Field(min_length=1)
    status: str = "calibrated"
    track: str = ""
    product_lines: list[str] = Field(default_factory=list)
    skill_tags: list[str] = Field(default_factory=list)
    level_system: str = ""
    no_poach: bool = False
    non_compete: bool = False
    note: str = ""
    calibrated_by: str = "consultant"


class ProposalGenerate(WriteEnvelope):
    job_candidate_ids: list[int] = Field(default_factory=list)
    limit: int = Field(default=12, ge=1, le=50)


class ProposalDecision(WriteEnvelope):
    confirmation_token: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    note: str = ""


class AnalyticsRunCreate(WriteEnvelope):
    catalog_id: str = Field(min_length=1)
    question: str = ""
    scope: dict[str, Any] = Field(default_factory=dict)


class AnalyticsTemplateCreate(AnalyticsRunCreate):
    name: str = Field(min_length=1)
    schedule_kind: str = "manual"
    schedule_enabled: bool = False
    schedule_time: str = "09:00"
    schedule_weekday: int = Field(default=0, ge=0, le=6)
    timezone: str = "Asia/Shanghai"


class AnalyticsTemplatePatch(WriteEnvelope):
    name: str | None = None
    catalog_id: str | None = None
    question: str | None = None
    scope: dict[str, Any] | None = None
    schedule_kind: str | None = None
    schedule_enabled: bool | None = None
    schedule_time: str | None = None
    schedule_weekday: int | None = Field(default=None, ge=0, le=6)
    timezone: str | None = None


class InboxStatePatch(WriteEnvelope):
    state: str = Field(min_length=1)
    source_revision: str = ""


class LegacyRuntime:
    def __init__(self, external_host: str, external_port: int) -> None:
        self.server = None
        self.state = legacy.WorkbenchState(
            Path(legacy.DEFAULT_DB).expanduser(), Path(legacy.DEFAULT_OUTPUT_DIR).expanduser(), external_host, external_port
        )
        self.port = 0

    def start(self) -> None:
        legacy.ensure_workbench_runtime_schema(self.state.db_path)
        handler = type("ASALegacyHandler", (legacy.WorkbenchHandler,), {"state": self.state})
        self.server = legacy.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, name="asa-legacy-compat", daemon=True).start()

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.state.close()


def create_app(*, db_path: Path = DEFAULT_DB, host: str = "127.0.0.1", port: int = 8765, start_legacy: bool = True) -> FastAPI:
    runtime = LegacyRuntime(host, port) if start_legacy else None
    agent = AgentService(db_path)
    analytics = AnalyticsService(db_path)
    core = CoreService(db_path, agent, analytics)
    knowledge = KnowledgeProposalService(db_path)
    calibration = CompanyCalibrationService(db_path)
    if runtime:
        runtime.state.core_service = core
        runtime.state.bind_agent_service(agent)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        migration = migrate(db_path)
        app.state.migration = migration
        if runtime:
            runtime.start()

        async def analytics_scheduler() -> None:
            while True:
                try:
                    outcome = await asyncio.to_thread(analytics.run_due_templates)
                    app.state.analytics_scheduler = {"status": "running", "last_result": outcome, "error": ""}
                except Exception as exc:
                    app.state.analytics_scheduler = {"status": "degraded", "last_result": None, "error": str(exc)[:500]}
                await asyncio.sleep(30)

        scheduler_task = asyncio.create_task(analytics_scheduler(), name="asa-analysis-scheduler")
        scheduler = Scheduler(db_path)
        scheduler.start()
        app.state.scheduler = scheduler
        # 注册 CKB 公司知识库每日刷新（幂等：已存在则不重复建）
        try:
            existing_names = {t["name"] for t in scheduler.list_tasks()}
            if "CKB公司知识库每日刷新" not in existing_names:
                scheduler.create_task(
                    "CKB公司知识库每日刷新", "company_kb_refresh", "30 4 * * *"
                )
                print("已注册 CKB 公司知识库每日刷新任务（每日 04:30）", flush=True)
        except Exception as e:
            print(f"注册 CKB 刷新任务失败（不影响启动）: {e}", flush=True)
        try:
            yield
        finally:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
            scheduler.stop()
            if runtime:
                runtime.close()
            agent.close()

    app = FastAPI(title="ASA Core", version="1.0.0", lifespan=lifespan)
    app.state.core = core
    app.state.analytics = analytics
    app.state.legacy = runtime
    browser_origins = trusted_browser_origins(host, port)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(browser_origins),
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-Requested-With"],
        max_age=600,
    )

    @app.middleware("http")
    async def enforce_browser_origin_boundary(request: Request, call_next):
        origin = str(request.headers.get("origin") or "").rstrip("/")
        if request.url.path.startswith("/api/") and origin and origin not in browser_origins:
            return JSONResponse(
                {"ok": False, "error": "browser origin is not authorized for the local ASA API"},
                status_code=403,
            )
        return await call_next(request)

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "asa-core", "version": "1.0.0", "db": str(db_path)}

    @app.get("/api/v1/bootstrap")
    def bootstrap() -> dict[str, Any]:
        now = time.time()
        cache_key = "_bootstrap"
        if cache_key in _bootstrap_cache:
            cached_at, cached_data = _bootstrap_cache[cache_key]
            if now - cached_at < _BOOTSTRAP_CACHE_TTL:
                return cached_data
        data = core.bootstrap()
        _bootstrap_cache[cache_key] = (now, data)
        return data

    @app.get("/api/v1/dashboard")
    def dashboard() -> dict[str, Any]: return core.dashboard()

    @app.get("/api/v1/agent/proposals")
    def agent_proposals(status: str = "pending", limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        try:
            return core.list_agent_proposals(status, limit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/agent/proposals/generate")
    def agent_proposals_generate(body: ProposalGenerate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("agent.proposals_generate", body, idempotency_key, "agent_proposal", "generate",
                    lambda: core.generate_agent_proposals(body.job_candidate_ids, body.limit))

    @app.post("/api/v1/agent/proposals/{proposal_id}/preflight")
    def agent_proposal_preflight(proposal_id: str, body: WriteEnvelope) -> dict[str, Any]:
        try:
            return core.preflight_agent_proposal(proposal_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/agent/proposals/{proposal_id}/decision")
    def agent_proposal_decision(proposal_id: str, body: ProposalDecision, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("agent.proposal_decision", body, idempotency_key, "agent_proposal", proposal_id,
                    lambda: core.decide_agent_proposal(proposal_id, body.confirmation_token, body.decision, body.note))

    @app.get("/api/v1/agent/metrics")
    def agent_action_metrics(days: int = Query(7, ge=1, le=30)) -> dict[str, Any]:
        return core.agent_action_metrics(days)

    @app.get("/api/v1/agent/model-audit")
    def agent_model_audit(
        limit: int = Query(50, ge=1, le=200),
        operation: str = Query("", max_length=80),
        status: str = Query("", max_length=32),
    ) -> dict[str, Any]:
        return core.model_audit(limit, operation, status)

    @app.get("/api/v1/jobs")
    def jobs(q: str = "", status: str = "", include_archived: bool = False, limit: int = Query(100, le=200), offset: int = 0) -> dict[str, Any]:
        return core.jobs(query=q, status=status, include_archived=include_archived, limit=limit, offset=offset)

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: int) -> dict[str, Any]: return core.job(job_id)

    @app.post("/api/v1/jobs/{job_id}/candidate-list/refresh")
    def job_candidate_list_refresh(job_id: int, body: CandidateListRefreshBody) -> dict[str, Any]:
        # 名单卡静态快照刷新：重新按库内最新状态生成 candidate_list 卡片。
        # 不写库、不建工作流、不走 LLM——纯查询重建；404=岗位不存在。
        return core.candidate_list_card(job_id, bonder=body.bonder, filter_mode=body.filter_mode)

    @app.get("/api/v1/jobs/{job_id}/profile-insights")
    def job_profile_insights_get(job_id: int) -> dict[str, Any]:
        # S8：岗位画像读取（这个岗位实际在干什么）。岗位不存在 → 404（LookupError 全局映射）；
        # 尚无画像 → 200 + status=not_generated 空结构（前端空态：履历还太少，学不出画像）。
        return core.get_job_profile_insights(job_id)

    @app.post("/api/v1/jobs/{job_id}/profile-insights/feedback")
    def job_profile_insights_feedback(job_id: int, body: JobProfileFeedbackCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S8：顾问纠正通道（"不对"按钮）。走 execute_idempotent 幂等 + 审计，重放返回首次响应；
        # 404=岗位不存在；409=item_type 非法 / item_key 为空。标记 disputed 不删除，
        # 聚合时排除并留痕统计——只是画像质量闭环，不接策略/评估消费。
        return idem("job.profile_insights_feedback", body, idempotency_key, "job", f"{job_id}:profile:{body.item_type}:{body.item_key}",
                    lambda: core.submit_job_profile_feedback(
                        job_id, item_type=body.item_type, item_key=body.item_key,
                        item_label=body.item_label, note=body.note))

    @app.get("/api/v1/candidates")
    def candidates(q: str = "", job_id: int | None = None, stage: str = "", limit: int = Query(100, le=200), offset: int = 0) -> dict[str, Any]:
        return core.candidates(query=q, job_id=job_id, stage=stage, limit=limit, offset=offset)

    @app.get("/api/v1/candidates/stop-reasons/summary")
    def stop_reasons_summary() -> dict[str, Any]: return core.stop_reasons_summary()

    @app.get("/api/v1/candidates/{candidate_id}")
    def candidate(candidate_id: int) -> dict[str, Any]: return core.candidate(candidate_id)

    @app.post("/api/v1/candidates/{candidate_id}/assessments")
    def candidate_assessment_generate(candidate_id: int, body: WriteEnvelope, job_id: int = Query(...), force: bool = Query(False), idempotency_key: str = Header(alias="Idempotency-Key")):
        # S6-1：生成/重新生成判人评估（职业轨迹 + 跳槽质量史）。走 execute_idempotent 幂等 + 审计，
        # 重放返回首次响应；404=人选/岗位不存在或不匹配；409=无简历语料/敏感扫描命中/模型不可用。
        # 同人同岗重复 POST 更新原 artifact（as_of 刷新），不重复建行。评估只辅助判断，不做决策。
        return idem("candidate.assessment_generate", body, idempotency_key, "job_candidate", f"{candidate_id}:{job_id}",
                    lambda: core.generate_candidate_assessment(candidate_id, job_id, force=force))

    @app.get("/api/v1/candidates/{candidate_id}/assessments")
    def candidate_assessment_get(candidate_id: int, job_id: int = Query(...)) -> dict[str, Any]:
        # S6-1：读取同人同岗判人评估；人选/岗位不存在或不匹配、尚无评估 → 404（LookupError 全局映射）
        return core.get_candidate_assessment(candidate_id, job_id)

    @app.post("/api/v1/candidates/{candidate_id}/fit-assessment")
    def candidate_fit_assessment_refresh(candidate_id: int, body: WriteEnvelope, job_id: int = Query(...), idempotency_key: str = Header(alias="Idempotency-Key")):
        # 匹配点分析「重新评估匹配」：强制重跑 Agent 人岗匹配评估（同步等待，通常 15-30 秒）。
        # 走 execute_idempotent 幂等 + 审计，重放返回首次响应；404=人选/岗位不存在或不匹配；
        # 409=模型输出非法或评估未完成。简历更新后用此入口刷新匹配结论。
        return idem("candidate.fit_assessment_refresh", body, idempotency_key, "job_candidate", f"{candidate_id}:{job_id}",
                    lambda: core.refresh_candidate_fit_assessment(candidate_id, job_id))

    @app.patch("/api/v1/candidates/{candidate_id}/assessments/{job_id}/advisor-action")
    def candidate_assessment_advisor_action(candidate_id: int, job_id: int, body: AssessmentAdvisorActionPatch, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S6-1b：顾问动作写回（accepted/modified/rejected/pending + 选填 note）。走 execute_idempotent
        # 幂等 + 审计，重放返回首次响应；404=人选/岗位不存在或不匹配、尚无评估；409=非法 action。
        # 只写 advisor_action/advisor_note/updated_at，artifact version 不 bump；已 action 可再改。
        return idem("candidate.assessment_advisor_action", body, idempotency_key, "job_candidate", f"{candidate_id}:{job_id}",
                    lambda: core.update_candidate_assessment_advisor_action(candidate_id, job_id, action=body.action, note=body.note))

    @app.get("/api/v1/assessments/calibration/metrics")
    def assessment_calibration_metrics() -> dict[str, Any]:
        # S6-4：采纳率度量（顾问点头率）——按维度×客户聚合采纳/改判/否决率（只读）；
        # 数据不足（<min_n）的分组三个率如实返回 null；totals 与库内 advisor_action 分布一致。
        return core.assessment_calibration_metrics()

    @app.post("/api/v1/assessments/calibration/report")
    def assessment_calibration_report(body: CalibrationReportCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S6-4：校准周报（周度手动触发，不做定时）。走 execute_idempotent 幂等 + 审计，
        # 重放返回首次响应；markdown 输出到 work/calibration/（不进 git），报告为内部留档不对外。
        return idem("assessment.calibration_report", body, idempotency_key, "assessment_calibration", "weekly",
                    lambda: core.generate_assessment_calibration_report())

    @app.get("/api/v1/workflows/{workflow_id}")
    def workflow(workflow_id: str) -> dict[str, Any]: return core.workflow(workflow_id)

    @app.get("/api/v1/artifacts/{artifact_id}")
    def workflow_artifact(artifact_id: str) -> dict[str, Any]:
        return core.workflow_artifact(artifact_id)

    @app.get("/api/v1/artifacts/{artifact_id}/file")
    def workflow_artifact_file(artifact_id: str):
        download = core.workflow_artifact_download(artifact_id)
        if download["kind"] == "file":
            return FileResponse(
                download["path"],
                media_type=download["mime_type"],
                filename=download["file_name"],
            )
        return Response(
            content=str(download["content"]).encode("utf-8"),
            media_type=download["mime_type"],
            headers={"Content-Disposition": f'attachment; filename="{download["file_name"]}"'},
        )

    @app.get("/api/v1/workflows/{workflow_id}/summary")
    def workflow_summary(workflow_id: str) -> dict[str, Any]:
        try:
            return core.workflow_summary(workflow_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/workflows/{workflow_id}/steps/{step_id}")
    def workflow_step(workflow_id: str, step_id: int) -> dict[str, Any]:
        try:
            return core.workflow_step(workflow_id, step_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/workflows/{workflow_id}/candidates")
    def workflow_candidates(workflow_id: str, limit: int = Query(50, le=200), offset: int = 0) -> dict[str, Any]:
        try:
            return core.workflow_candidates(workflow_id, limit, offset)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/workflows/{workflow_id}/sourcing-funnel")
    def workflow_sourcing_funnel(workflow_id: str) -> dict[str, Any]:
        # 无寻访运行时返回空结构（channels/runs 为空数组），不抛 404
        return core.workflow_sourcing_funnel(workflow_id)

    @app.get("/api/v1/workflows/{workflow_id}/strategy-review")
    def workflow_strategy_review(workflow_id: str) -> dict[str, Any]:
        # 无复盘或工作流不存在均 404（LookupError 由全局异常处理器映射）
        return core.workflow_strategy_review(workflow_id)

    @app.post("/api/v1/workflows/{workflow_id}/strategy-review/rebuild")
    def workflow_strategy_review_rebuild(workflow_id: str, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 按需重算（存量终局工作流补生成）：走 execute_idempotent 模式，重放返回首次响应
        return idem("workflow.strategy_review_rebuild", body, idempotency_key, "workflow", workflow_id,
                    lambda: core.rebuild_strategy_review(workflow_id))

    @app.patch("/api/v1/workflows/{workflow_id}/strategy-review/diffs")
    def workflow_strategy_review_diffs(workflow_id: str, body: StrategyReviewDiffDecisions, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S4-3c 顾问逐项采纳/拒绝落库：走 execute_idempotent 模式，重放返回首次响应；
        # 工作流/复盘不存在 → 404（LookupError），diff_id 未知或 status 非法 → 409（ValueError）
        return idem("workflow.strategy_review_diff_decisions", body, idempotency_key, "workflow", workflow_id,
                    lambda: core.apply_strategy_review_diff_decisions(
                        workflow_id, [item.model_dump() for item in body.decisions]))

    @app.post("/api/v1/workflows/{workflow_id}/strategy/edits")
    def workflow_strategy_item_edits(workflow_id: str, body: StrategyItemEdits, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 寻访策略按项编辑（关键词组/公司池/职级/顾问约束的增改删）：走 execute_idempotent
        # 幂等 + 审计，重放返回首次响应；404=工作流不存在或无 strategy_v2；409=外部寻访已开始、
        # 目标项不存在（状态漂移）、编辑后质量校验/查询计划编译不过（中文 detail 透出）。
        # 不绕过 R3：编辑后策略 hash 变化，waiting_approval 的旧审批卡作废并自动换新。
        return idem("workflow.strategy_item_edits", body, idempotency_key, "workflow", workflow_id,
                    lambda: core.apply_strategy_item_edits(
                        workflow_id, [dict(item) for item in body.edits], note=body.note,
                        expected_strategy_hash=body.expected_strategy_hash,
                        preflight_token=body.preflight_token))

    @app.post("/api/v1/workflows/{workflow_id}/strategy/edits/preflight")
    def workflow_strategy_item_edits_preflight(workflow_id: str, body: StrategyItemEdits):
        try:
            return core.strategy_item_edits_preflight(
                workflow_id, [dict(item) for item in body.edits],
                expected_strategy_hash=body.expected_strategy_hash,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/mapping-tasks/{artifact_id}")
    def job_mapping_task(job_id: int, artifact_id: str) -> dict[str, Any]:
        # S5-1：读取 Mapping 任务卡；岗位/artifact 不存在或不属于该岗位 → 404（LookupError 全局映射）
        return core.get_mapping_task(job_id, artifact_id)

    @app.post("/api/v1/jobs/{job_id}/mapping-tasks")
    def job_mapping_task_create(job_id: int, body: MappingTaskCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S5-1：扩池决策树 escalate_mapping 步的后端触发（或顾问手动）。走 execute_idempotent 幂等，
        # 重放返回首次响应；岗位不存在 → 404，无 strategy_v2/trigger 非法 → 409。
        return idem("job.mapping_task_create", body, idempotency_key, "job", str(job_id),
                    lambda: core.create_mapping_task(job_id, trigger=body.trigger))

    @app.patch("/api/v1/mapping-tasks/{artifact_id}/candidates/{index}")
    def mapping_candidate_patch(artifact_id: str, index: int, body: MappingCandidatePatch, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S5-2：任务卡候选状态机 PATCH（状态/备注）。走 execute_idempotent 幂等 + 审计；
        # 404=artifact/候选不存在；409=未知态/非法迁移/终态变更/直接置 intaken；422=body 缺字段。
        # confirmed 迁移成功自动生成破冰素材（质量不合格不阻断状态变更，素材不写入）。
        if body.status is None and body.consultant_note is None:
            raise HTTPException(422, "PATCH 至少需要 status 或 consultant_note 之一")
        return idem("mapping_task.candidate_update", body, idempotency_key, "mapping_task", f"{artifact_id}#{index}",
                    lambda: core.update_mapping_candidate(
                        artifact_id, index, status=body.status, consultant_note=body.consultant_note))

    @app.post("/api/v1/mapping-tasks/{artifact_id}/candidates/{index}/icebreaker")
    def mapping_candidate_icebreaker(artifact_id: str, index: int, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S5-2：重新生成破冰素材（人选卡"重新生成"按钮）。幂等；未确认人选 → 409；
        # 素材只存不送（发送动作永远由顾问本人执行）。
        return idem("mapping_task.icebreaker_regenerate", body, idempotency_key, "mapping_task", f"{artifact_id}#{index}",
                    lambda: core.regenerate_mapping_icebreaker(artifact_id, index))

    @app.post("/api/v1/mapping-tasks/{artifact_id}/candidates/{index}/intake")
    def mapping_candidate_intake(artifact_id: str, index: int, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S5-2：入库动作（仅 confirmed）。复用现有 intake 写入口径，幂等 + 审计；
        # 不写第二条 job_candidates（已有关系复用原 id）；禁挖/无来源/已停止关系 → 409。
        return idem("mapping_task.candidate_intake", body, idempotency_key, "mapping_task", f"{artifact_id}#{index}",
                    lambda: core.intake_mapping_candidate(artifact_id, index))

    @app.post("/api/v1/mapping-tasks/{artifact_id}/backflow")
    def mapping_task_backflow(artifact_id: str, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S5-3：知识回流（图谱 teams 扩展层）显式触发——运行时 Core 不自动写图谱。
        # 走 execute_idempotent 幂等 + 审计；同 artifact 重放返回首次响应（图谱只写一次）。
        # 404=artifact 不存在；409=无已确认团队数据/可回流团队全部禁挖/图谱缺失或结构异常。
        return idem("mapping_task.backflow", body, idempotency_key, "mapping_task", artifact_id,
                    lambda: core.backflow_mapping_task(artifact_id))

    @app.get("/api/v1/mapping-tasks/metrics")
    def mapping_metrics() -> dict[str, Any]:
        # S5-3：Mapping 评测指标聚合（PRD §8 四项口径，只读；数据不足的分组如实 null）。
        return core.mapping_metrics()

    @app.post("/api/v1/radar/scans")
    def radar_scan_create(body: RadarScanCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S7-1：人才流动雷达手动扫描（公司池=33 客户档案+mapping 已确认公司，公开信号只读检索）。
        # 走 execute_idempotent 幂等 + 审计，重放返回首次响应；同日重复扫描更新同一 artifact。
        # 红线：无来源信号拒写；禁挖照常过滤；榜单只出建议，系统不自动触达。
        return idem("radar.scan_create", body, idempotency_key, "radar", "global",
                    lambda: core.create_radar_scan())

    @app.get("/api/v1/radar/scans/latest")
    def radar_scan_latest() -> dict[str, Any]:
        # S7-1：读最新雷达榜单；尚无扫描 → 404（LookupError 全局映射）
        return core.get_latest_radar_scan()

    @app.post("/api/v1/radar/scans/latest/actions/start-mapping")
    def radar_start_mapping(body: RadarStartMappingCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S7-2：榜单一键发起 Mapping——trigger=radar 由后端锚定（前端不传 trigger），
        # 目标团队定位注入该公司未过期雷达信号上下文（信号内容不进 artifact 对外字段）。
        # 走 execute_idempotent 幂等 + 审计；同日同公司重复发起返回已存在任务卡（不重复建 task）。
        # 红线沿用 S5：无来源不进名单、禁挖过滤、不自动触达。
        return idem("radar.start_mapping", body, idempotency_key, "radar", f"mapping:{body.job_id}:{body.company}",
                    lambda: core.start_mapping_from_radar(body.company, body.job_id))

    @app.get("/api/v1/radar/scans/latest/actions/activate")
    def radar_activate(company: str = Query(..., min_length=1)) -> dict[str, Any]:
        # S7-2：激活存量人选——人才库该公司现职/曾任职候选清单（现职优先），只读不写库，
        # 动作由顾问本人执行；尚无雷达榜单 → 404（LookupError 全局映射）
        return core.activate_radar_company(company)

    @app.post("/api/v1/radar/weekly-report")
    def radar_weekly_report_create(body: RadarWeeklyReportCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # S7-3：雷达周报——本周 Top 信号 + 过期降权统计 + 榜单变化对比（缺上周如实标注"首期，无对比基线"），
        # markdown 落 work/radar/；生成后向 Copilot 仲裁层推一条提醒（只推条数和入口，不含敏感细节，
        # 推送失败不阻断周报）。走 execute_idempotent 幂等 + 审计；尚无雷达榜单 → 404。
        return idem("radar.weekly_report", body, idempotency_key, "radar", "weekly",
                    lambda: core.create_radar_weekly_report())

    @app.get("/api/v1/radar/weekly-report/latest")
    def radar_weekly_report_latest() -> dict[str, Any]:
        # S7-3：读最新雷达周报；尚无周报 → 404（LookupError 全局映射）
        return core.get_latest_radar_weekly_report()

    @app.get("/api/v1/audit-events")
    def audit_events(limit: int = Query(100, le=500), offset: int = 0) -> dict[str, Any]:
        return core.audit_events(limit, offset)

    @app.get("/api/v1/events")
    async def events(request: Request, workflow_id: str = "") -> StreamingResponse:
        async def stream():
            cursor = 0
            while not await request.is_disconnected():
                payload = agent.get_workflow_events(cursor, workflow_id, 100)
                for event in payload.get("events", []):
                    cursor = max(cursor, int(event.get("id") or 0))
                    yield f"id: {cursor}\nevent: workflow\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(3)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/copilot/messages")
    def copilot(body: CopilotMessage, idempotency_key: str = Header(alias="Idempotency-Key")) -> dict[str, Any]:
        return idem("copilot.message", body, idempotency_key, "copilot_session", body.session_id or "new",
                    lambda: core.copilot(body.message, session_id=body.session_id, context=body.context))

    attachment_parse_slots = threading.BoundedSemaphore(2)

    @app.post("/api/v1/copilot/attachments", response_model=CopilotAttachmentUploadResponse)
    def copilot_attachment(body: CopilotAttachmentUpload) -> dict[str, Any]:
        """Read a user-selected local document and return model-ready text without retaining its path."""
        file_name = str(body.file_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        suffix = Path(file_name).suffix.lower()
        if not file_name or suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(415, f"暂不支持读取 {suffix or '无扩展名'} 文件")
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(400, "附件内容编码无效") from exc
        if len(content) != body.size_bytes:
            raise HTTPException(400, "附件大小与上传声明不一致")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "附件超过 25 MB 读取上限")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="asa-attachment-", suffix=suffix, delete=False) as handle:
                handle.write(content)
                temporary_path = Path(handle.name)
            with attachment_parse_slots:
                extracted_text, truncated = extract_local_document(temporary_path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        content_available = bool(extracted_text.strip())
        attachment_id = f"att_{secrets.token_hex(16)}"
        access_token = secrets.token_urlsafe(32)
        status = (
            "已读取附件正文，超长内容已截取前 18000 字。"
            if truncated
            else "已读取附件正文。"
            if content_available
            else "文件已读取，但没有提取到可分析文本。"
        )
        with transaction(db_path) as conn:
            conn.execute("DELETE FROM agent_copilot_attachments WHERE expires_at<=datetime('now','localtime')")
            conn.execute(
                """
                INSERT INTO agent_copilot_attachments
                (attachment_id,access_token_hash,file_name,file_type,mime_type,size_bytes,
                 content_sha256,extracted_text,truncated,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attachment_id, hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
                    file_name, suffix.lstrip("."), body.mime_type, len(content),
                    hashlib.sha256(content).hexdigest(), extracted_text, int(truncated), status,
                ),
            )
        return {
            "ok": True,
            "attachment": {
                "attachment_id": attachment_id,
                "access_token": access_token,
                "file_name": file_name,
                "file_type": suffix.lstrip("."),
                "mime_type": body.mime_type,
                "size_bytes": len(content),
                "content_available": content_available,
                "truncated": truncated,
                "is_image": False,
                "status": status,
            },
        }

    @app.get("/api/v1/copilot/sessions", response_model=CopilotSessionListResponse)
    def copilot_sessions(
        limit: int = Query(30, ge=1, le=100),
        q: str = Query("", max_length=120),
        include_archived: bool = Query(False),
    ) -> dict[str, Any]:
        return agent.list_copilot_sessions(limit, q, include_archived)

    # 外部编排层（DSH）轮次回填：DSH 对话只存其服务器内存，回填后才会出现在
    # 会话列表（agent_copilot_messages rollup）并可刷新恢复。服务端按 request_id 幂等。
    @app.post("/api/v1/copilot/sessions/record-turn", response_model=CopilotTurnRecordResponse)
    def copilot_sessions_record_turn(body: CopilotTurnRecordRequest) -> dict[str, Any]:
        result = agent.record_external_copilot_turn(
            session_id=body.session_id,
            request_id=body.request_id,
            message=body.message,
            answer=body.answer,
            context=body.context,
            source=body.source,
            model=body.model,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "record failed")
        return result

    @app.post("/api/v1/copilot/sessions/archive-all", response_model=CopilotSessionBulkArchiveResponse)
    def copilot_sessions_archive_all(
        body: WriteEnvelope,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        return idem(
            "copilot.sessions_archive_all",
            body,
            idempotency_key,
            "copilot_sessions",
            "all",
            agent.archive_all_copilot_sessions,
        )

    @app.get("/api/v1/copilot/sessions/{session_id}", response_model=CopilotSessionDetailResponse)
    def copilot_session(
        session_id: str,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        result = agent.get_copilot_session(session_id, limit, offset)
        if not result.get("messages") and not result.get("business_focus"):
            raise HTTPException(status_code=404, detail="Agent task not found")
        return result

    @app.get("/api/v1/copilot/sessions/{session_id}/messages/search", response_model=CopilotMessageSearchResponse)
    def copilot_session_message_search(
        session_id: str,
        q: str = Query("", max_length=200),
        limit: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        return agent.search_copilot_session_messages(session_id, q, limit)

    @app.patch("/api/v1/copilot/sessions/{session_id}", response_model=CopilotSessionUpdateResponse)
    def copilot_session_update(
        session_id: str,
        body: CopilotSessionPatch,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        try:
            return idem(
                "copilot.session_update",
                body,
                idempotency_key,
                "copilot_session",
                session_id,
                lambda: agent.update_copilot_session(
                    session_id,
                    title=body.title,
                    archived=body.archived,
                    clear_focus=body.clear_focus,
                ),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/copilot/stream")
    async def copilot_stream(
        body: CopilotMessage,
        request: Request,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> StreamingResponse:
        """Canonical Copilot decision exposed as SSE transport."""
        key = idempotency_key or f"copilot-stream-{body.request_id}"
        # 先登记幂等租约：冲突/参数错误仍在流开始前走 HTTP 非 2xx（409 JSON），语义不变。
        try:
            replay, is_replay = await asyncio.to_thread(
                core.begin_idempotent,
                operation="copilot.message",
                request_id=body.request_id,
                idempotency_key=key,
                payload=body.model_dump(),
                target_type="copilot_session",
                target_id=body.session_id or "new",
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

        async def stream():
            # 流开始后的任何失败（计算或分片发送）都以 error 事件收尾，不裸断连；
            # 前端两种通道（HTTP 非 2xx / 流内 error）都兼容。
            try:
                if is_replay:
                    # 重放只放已登记结果，不重复 progress。
                    result = replay
                else:
                    progress = {"message": "请求已受理，正在处理"}
                    yield f"event: progress\ndata: {json.dumps(progress, ensure_ascii=False)}\n\n"
                    result = await asyncio.to_thread(
                        core.complete_idempotent,
                        operation="copilot.message",
                        request_id=body.request_id,
                        idempotency_key=key,
                        target_type="copilot_session",
                        target_id=body.session_id or "new",
                        action=lambda: core.copilot(body.message, session_id=body.session_id, context=body.context),
                        surface="asa_copilot_stream",
                    )
                context_payload = {
                    "session_id": result.get("session_id"),
                    "context": result.get("context") or {},
                    "references": result.get("references") or [],
                    "suggested_actions": result.get("suggested_actions") or [],
                }
                yield f"event: context\ndata: {json.dumps(context_payload, ensure_ascii=False)}\n\n"
                answer = str(result.get("answer") or "")
                for offset in range(0, len(answer), 80):
                    if await request.is_disconnected():
                        return
                    yield f"event: text\ndata: {json.dumps({'content': answer[offset:offset + 80]}, ensure_ascii=False)}\n\n"
                yield f"event: done\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
            except Exception as exc:
                error = {"error": str(exc)[:500] or type(exc).__name__}
                yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/copilot/agent")
    def copilot_agent(
        body: CopilotMessage,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """Compatibility alias; user turns use the canonical Copilot decision path."""
        key = idempotency_key or f"copilot-agent-{body.request_id}"
        return idem(
            "copilot.message",
            body,
            key,
            "copilot_session",
            body.session_id or "new",
            lambda: core.copilot(body.message, session_id=body.session_id, context=body.context),
        )

    @app.post("/api/v1/copilot/events")
    def copilot_event(body: CopilotEvent) -> dict[str, Any]:
        """Copilot 埋点（PRD §9）：本地统计语义，轻量写入不做幂等。"""
        try:
            return core.record_copilot_event(body.session_id, body.event, body.payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/v1/copilot/intents/confirm")
    def copilot_intent_confirm(body: CopilotIntentConfirm, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not body.candidate_id:
            raise HTTPException(400, "candidate_id is required")
        if not body.preflight_token:
            raise HTTPException(400, "preflight_token is required")
        if not body.intent_hash:
            raise HTTPException(400, "intent_hash is required")
        return idem("copilot.intent_confirm", body, idempotency_key, "job_candidate", str(body.candidate_id),
                    lambda: core.confirm_copilot_intent(
                        body.intent,
                        intent_hash=body.intent_hash,
                        candidate_id=body.candidate_id,
                        preflight_token=body.preflight_token,
                        message=body.message,
                        session_id=body.session_id,
                    ))

    # ---- 记忆系统（跨会话） ----
    @app.get("/api/v1/memories")
    def memories(query: str = "", scope_type: str = "", limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        if query:
            return agent.search_memories(query, context_type=scope_type or "global", limit=limit)
        return agent.list_memories(scope_type=scope_type or "", limit=limit)

    @app.post("/api/v1/memories", status_code=201)
    def memory_store(body: MemoryStore, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("memory.store", body, idempotency_key, "agent_memory", "",
                    lambda: agent.store_memory(
                        scope_type=body.scope_type, scope_id=body.scope_id,
                        memory_type=body.memory_type, content=body.content,
                        source_type=body.source_type, confidence=body.confidence,
                    ))

    @app.delete("/api/v1/memories/{memory_id}")
    def memory_revoke(memory_id: int, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("memory.revoke", body, idempotency_key, "agent_memory", str(memory_id),
                    lambda: agent.revoke_memory(memory_id))

    # ---- 内建调度器 ----
    @app.get("/api/v1/scheduler/tasks")
    def scheduler_tasks() -> dict[str, Any]:
        return {"ok": True, "tasks": app.state.scheduler.list_tasks()}

    @app.post("/api/v1/scheduler/tasks", status_code=201)
    def scheduler_task_create(body: SchedulerTaskCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("scheduler.create", body, idempotency_key, "scheduled_task", "",
                    lambda: {"ok": True, "task": app.state.scheduler.create_task(
                        body.name, body.task_type, body.cron_expr, body.params or None)})

    @app.patch("/api/v1/scheduler/tasks/{task_id}")
    def scheduler_task_patch(task_id: int, body: SchedulerTaskPatch) -> dict[str, Any]:
        if body.action == "pause":
            return app.state.scheduler.pause_task(task_id)
        return app.state.scheduler.resume_task(task_id)

    @app.delete("/api/v1/scheduler/tasks/{task_id}")
    def scheduler_task_delete(task_id: int, body: WriteEnvelope) -> dict[str, Any]:
        return app.state.scheduler.delete_task(task_id)

    @app.post("/api/v1/scheduler/tasks/{task_id}/run")
    def scheduler_task_run(task_id: int) -> dict[str, Any]:
        scheduler = app.state.scheduler
        scheduler.run_now(task_id)
        task = next((t for t in scheduler.list_tasks() if int(t["id"]) == task_id), None)
        return {"ok": True, "task": task}

    def idem(operation: str, body: WriteEnvelope, key: str, target_type: str, target_id: str, action):
        try:
            response, _ = core.execute_idempotent(
                operation=operation, request_id=body.request_id, idempotency_key=key,
                payload=body.model_dump(), target_type=target_type, target_id=target_id, action=action,
            )
            return response
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/analytics/catalog")
    def analytics_catalog() -> dict[str, Any]:
        return analytics.catalog()

    @app.post("/api/v1/analytics/runs", status_code=201)
    def analytics_run_create(body: AnalyticsRunCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "analytics.run_create", body, idempotency_key, "analysis_catalog", body.catalog_id,
            lambda: analytics.create_run(body.catalog_id, body.question, body.scope),
        )

    @app.get("/api/v1/analytics/runs/{run_id}")
    def analytics_run_get(run_id: str) -> dict[str, Any]:
        return analytics.get_run(run_id)

    @app.post("/api/v1/analytics/runs/{run_id}/refresh", status_code=201)
    def analytics_run_refresh(run_id: str, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "analytics.run_refresh", body, idempotency_key, "analysis_run", run_id,
            lambda: analytics.refresh_run(run_id),
        )

    @app.post("/api/v1/analytics/runs/{run_id}/export")
    def analytics_run_export(run_id: str, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "analytics.run_export", body, idempotency_key, "analysis_run", run_id,
            lambda: analytics.export_run(run_id),
        )

    @app.get("/api/v1/analytics/runs/{run_id}/download")
    def analytics_run_download(run_id: str) -> FileResponse:
        target = analytics.export_file(run_id)
        return FileResponse(
            target,
            media_type="text/markdown; charset=utf-8",
            filename=f"ASA-analysis-{run_id.removeprefix('analysis_')[:12]}.md",
        )

    @app.get("/api/v1/analytics/templates")
    def analytics_templates() -> dict[str, Any]:
        return analytics.list_templates()

    @app.post("/api/v1/analytics/templates", status_code=201)
    def analytics_template_create(body: AnalyticsTemplateCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "analytics.template_create", body, idempotency_key, "analysis_template", body.name,
            lambda: analytics.create_template(
                body.name, body.catalog_id, body.question, body.scope,
                schedule_kind=body.schedule_kind, schedule_enabled=body.schedule_enabled,
                schedule_time=body.schedule_time, schedule_weekday=body.schedule_weekday,
                timezone_name=body.timezone,
            ),
        )

    @app.patch("/api/v1/analytics/templates/{template_id}")
    def analytics_template_patch(template_id: str, body: AnalyticsTemplatePatch, idempotency_key: str = Header(alias="Idempotency-Key")):
        patch = body.model_dump(exclude={"request_id"}, exclude_unset=True)
        return idem(
            "analytics.template_patch", body, idempotency_key, "analysis_template", template_id,
            lambda: analytics.update_template(template_id, patch),
        )

    @app.post("/api/v1/analytics/templates/{template_id}/run", status_code=201)
    def analytics_template_run(template_id: str, body: WriteEnvelope, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "analytics.template_run", body, idempotency_key, "analysis_template", template_id,
            lambda: analytics.run_template(template_id),
        )

    @app.get("/api/v1/analytics/templates/{template_id}/runs")
    def analytics_template_runs(template_id: str, limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
        return analytics.list_template_runs(template_id, limit)

    @app.get("/api/v1/analytics/templates/{template_id}/trend")
    def analytics_template_trend(template_id: str, limit: int = Query(30, ge=2, le=100)) -> dict[str, Any]:
        return analytics.template_trend(template_id, limit)

    @app.delete("/api/v1/analytics/templates/{template_id}")
    def analytics_template_delete(template_id: str) -> dict[str, Any]:
        return analytics.delete_template(template_id)

    @app.get("/api/v1/workbench")
    def workbench(limit: int = Query(1000, ge=1, le=1000)) -> dict[str, Any]:
        # 响应语义：flow 固定按 1000 拉取（取「全部进行中」，由 analytics.workbench 映射成
        # 待判断/运行中/待客户/风险逾期/最近交付五个 lane；普通进行中项不进工作台），
        # summary 基于全量可见项统计（候选队列上限 1000，不随 limit 收缩）；
        # items 序列化窗口由 analytics.workbench 统一封顶 300，超限时
        # truncated=True 且 returned_count < summary.total，调用方按截断列表处理。
        flow = agent.get_flow_inbox(queue="全部进行中", limit=1000)
        return analytics.workbench(flow, limit)

    @app.get("/api/v1/inbox")
    def inbox(limit: int = Query(100, ge=1, le=300)) -> dict[str, Any]:
        # 与 /api/v1/workbench 同一序列化路径，limit 直接约束窗口（上限 300 与封顶一致）。
        flow = agent.get_flow_inbox(queue="今日待办", limit=1000)
        return analytics.workbench(flow, limit)

    @app.patch("/api/v1/inbox/{item_key:path}/state")
    def inbox_state(item_key: str, body: InboxStatePatch, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem(
            "inbox.state", body, idempotency_key, "inbox_item", item_key,
            lambda: analytics.set_inbox_state(item_key, body.state, body.source_revision),
        )

    @app.post("/api/v1/workflows", status_code=201)
    def create_workflow(body: WorkflowCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("workflow.create", body, idempotency_key, "workflow", "new", lambda: agent.create_goal(body.objective, body.context, body.priority))

    @app.post("/api/v1/workflows/{workflow_id}/{action_name}")
    def workflow_action(workflow_id: str, action_name: str, body: WorkflowAction, idempotency_key: str = Header(alias="Idempotency-Key")):
        actions = {
            "start": lambda: agent.start_workflow(
                workflow_id,
                expected_plan_version=body.expected_plan_version,
                expected_plan_hash=body.expected_plan_hash,
            ),
            "revise": lambda: agent.revise_workflow(workflow_id, body.instruction),
            "revert_revision": lambda: agent.revert_workflow_revision(workflow_id),
            "cancel": lambda: agent.cancel_workflow(workflow_id, body.note),
            "pause": lambda: agent.pause_workflow(workflow_id, body.note),
            "resume": lambda: agent.resume_workflow(workflow_id, body.note),
            "archive": lambda: agent.archive_workflow(workflow_id),
        }
        if action_name not in actions:
            raise HTTPException(404, "unknown workflow action")
        return idem(f"workflow.{action_name}", body, idempotency_key, "workflow", workflow_id, actions[action_name])

    @app.post("/api/v1/approvals/{approval_id}/decision")
    def approval(approval_id: str, body: ApprovalDecision, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("approval.decision", body, idempotency_key, "approval", approval_id,
                    lambda: agent.decide_workflow_approval(approval_id, body.decision, body.note))

    @app.post("/api/v1/candidate-actions/preflight")
    def candidate_preflight(body: CandidateAction) -> dict[str, Any]:
        try:
            return core.candidate_preflight(body.candidate_id, body.action)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/candidate-actions/commit")
    def candidate_commit(body: CandidateAction, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not body.preflight_token:
            raise HTTPException(400, "preflight_token is required")
        return idem("candidate.commit", body, idempotency_key, "job_candidate", str(body.candidate_id),
                    lambda: core.candidate_commit(body.candidate_id, body.action, body.note, body.preflight_token, reason=body.reason))

    @app.post("/api/v1/consultant-recommendations/preflight")
    def consultant_recommendation_preflight(body: ConsultantRecommendationPreflight) -> dict[str, Any]:
        try:
            return core.consultant_recommendation_preflight(body.candidate_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/consultant-recommendations/commit")
    def consultant_recommendation_commit(body: ConsultantRecommendationCommit, idempotency_key: str = Header(alias="Idempotency-Key")):
        if not body.preflight_token:
            raise HTTPException(400, "preflight_token is required")
        return idem("consultant_recommendation.commit", body, idempotency_key, "job_candidate", str(body.candidate_id),
                    lambda: core.consultant_recommendation_commit(body.candidate_id, body.reason, body.preflight_token))

    @app.get("/api/v1/jobs/{job_id}/recommendation-metrics")
    def job_recommendation_metrics(job_id: int) -> dict[str, Any]:
        # 顾问确认推荐岗位指标：confirmed_recommendations / assessed_candidates / rate。
        # 岗位不存在 → 404（LookupError 全局映射）；无评估 → rate=None。
        return core.consultant_recommendation_metrics(job_id)

    @app.get("/api/v1/jobs/{job_id}/sourcing-adjustments")
    def job_sourcing_adjustments(job_id: int) -> SourcingAdjustmentListResponse:
        # 停止备注 → 寻访调整指令列表；岗位不存在时返回空列表（不抛 404）。
        return SourcingAdjustmentListResponse.model_validate(core.list_sourcing_adjustments(job_id))

    @app.post("/api/v1/sourcing-adjustments/{adjustment_id}/confirm")
    def sourcing_adjustment_confirm(adjustment_id: int, body: SourcingAdjustmentDecision, idempotency_key: str = Header(alias="Idempotency-Key")) -> SourcingAdjustmentDecisionResponse:
        # 顾问采纳调整：pending → accepted；applied 只由成功落库的策略产物写入。
        return SourcingAdjustmentDecisionResponse.model_validate(
            idem("sourcing_adjustment.confirm", body, idempotency_key, "sourcing_adjustment", str(adjustment_id),
                 lambda: core.confirm_sourcing_adjustment(adjustment_id))
        )

    @app.post("/api/v1/sourcing-adjustments/{adjustment_id}/ignore")
    def sourcing_adjustment_ignore(adjustment_id: int, body: SourcingAdjustmentDecision, idempotency_key: str = Header(alias="Idempotency-Key")) -> SourcingAdjustmentDecisionResponse:
        # 顾问忽略调整：pending → ignored。
        return SourcingAdjustmentDecisionResponse.model_validate(
            idem("sourcing_adjustment.ignore", body, idempotency_key, "sourcing_adjustment", str(adjustment_id),
                 lambda: core.ignore_sourcing_adjustment(adjustment_id))
        )

    @app.get("/api/v1/candidates/{candidate_id}/recommendation-packages")
    def candidate_recommendation_packages(candidate_id: int) -> dict[str, Any]:
        # 版本化推荐包列表（按 version 倒序，含反馈计数）。候选人（job_candidate）不存在 → 404；
        # 尚未确认推荐 → 200 + 空列表。
        return core.list_recommendation_packages(candidate_id)

    @app.get("/api/v1/recommendation-packages/{package_id}")
    def recommendation_package_detail(package_id: str) -> dict[str, Any]:
        # 推荐包详情：候选摘要/人岗匹配证据（含评估指纹）/风险/待核验问题 + 已记录客户反馈；
        # upgradeable/latest_assessment_id 供升版入口判定（P3-a）；不存在 → 404。
        return core.get_recommendation_package(package_id)

    @app.post("/api/v1/recommendation-packages/{package_id}/feedback")
    def recommendation_package_feedback_create(package_id: str, body: PackageFeedbackCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 客户反馈记录：关联推荐包版本，回写候选人事件时间线（candidate_events），
        # 并同事务双写旧 client_feedback_events（P3-b 旧报表口径统一，旧读方零改动）。
        # 走 execute_idempotent 幂等 + 审计，重放返回首次响应；表级 UNIQUE(package_id, request_id) 兜底；
        # 404=推荐包不存在；409=反馈类型非法/内容为空。
        return idem("recommendation_package.feedback", body, idempotency_key, "recommendation_package", package_id,
                    lambda: core.record_package_feedback(package_id, body.feedback_type, body.content, body.feedback_time, body.request_id))

    @app.post("/api/v1/recommendation-packages/{package_id}/upgrade/preflight")
    def recommendation_package_upgrade_preflight(package_id: str, body: PackageUpgradePreflight) -> dict[str, Any]:
        # 推荐包升版预检（P3-a）：包存在（404）且为最新版本、评估指纹已更新（409 中文 detail），
        # 通过则发一次性 preflight token（5 分钟有效）。
        try:
            return core.recommendation_package_upgrade_preflight(package_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/recommendation-packages/{package_id}/upgrade/commit")
    def recommendation_package_upgrade_commit(package_id: str, body: PackageUpgradeCommit, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 推荐包升版提交（P3-a）：走 execute_idempotent 幂等 + 审计，重放返回首次响应；
        # 一次性 token 失效/指纹回退一致 → 409；UNIQUE(job_candidate_id, version) 并发兜底回读；
        # 404=推荐包不存在。
        if not body.preflight_token:
            raise HTTPException(400, "preflight_token is required")
        return idem("recommendation_package.upgrade", body, idempotency_key, "recommendation_package", package_id,
                    lambda: core.recommendation_package_upgrade_commit(package_id, body.preflight_token))

    @app.post("/api/v1/candidates/{candidate_id}/lifecycle-events")
    def candidate_lifecycle_event_create(candidate_id: int, body: LifecycleEventCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 生命周期一等事件（面试/Offer/入职）：写 candidate_events（与旧事件在时间线查询中统一返回）
        # 并自动生成跟进待办（followup_tasks，不自动对外发任何消息）。
        # 走 execute_idempotent 幂等 + 审计（重放返回首次响应）；事件表 source_id=request_id 兜底去重；
        # 404=候选人不存在；409=事件类型/状态/时间格式非法。
        return idem("candidate_lifecycle.record", body, idempotency_key, "job_candidate", str(candidate_id),
                    lambda: core.record_lifecycle_event(
                        candidate_id, body.event_type,
                        notes=body.notes, occurred_at=body.occurred_at,
                        event_status=body.event_status, request_id=body.request_id,
                    ))

    @app.post("/api/v1/jobs/{job_id}/weekly-report")
    def job_weekly_report_create(job_id: int, body: JobWeeklyReportCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 岗位自动周报：确定性组装（漏斗/有效推荐/渠道质量/风险/建议，不依赖 LLM），
        # markdown + 结构化 metadata 落 agent_artifacts；同周重复生成更新同一 artifact
        # （version 自增 + history 留痕），跨周生成新 artifact 并以上一期做对比基线。
        # 走 execute_idempotent 幂等 + 审计，重放返回首次响应；岗位不存在 → 404（LookupError 全局映射）。
        return idem("job.weekly_report", body, idempotency_key, "job", f"{job_id}:weekly",
                    lambda: core.generate_job_weekly_report(job_id))

    @app.get("/api/v1/jobs/{job_id}/weekly-reports")
    def job_weekly_reports(job_id: int, limit: int = Query(12, ge=1, le=52)) -> dict[str, Any]:
        # 岗位周报历史（新→旧）+ 最新一期摘要；尚无周报 → 200 + latest=None/items=[]；
        # 岗位不存在 → 404（LookupError 全局映射）；完整正文走 /api/v1/artifacts/{artifact_id}。
        return core.list_job_weekly_reports(job_id, limit)

    @app.get("/api/v1/knowledge-proposals")
    def knowledge_proposals(
        status: str = "pending",
        proposal_type: str = "",
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        # 二期知识飞轮：知识增补提案列表（默认 pending）；status/type 非法 → 409。
        try:
            return knowledge.list_proposals(status, proposal_type, limit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/knowledge-proposals/{proposal_id}")
    def knowledge_proposal_detail(proposal_id: str) -> dict[str, Any]:
        # 提案详情（内容 JSON + 可读证据列表）；不存在 → 404（LookupError 全局映射）。
        return knowledge.get_proposal(proposal_id)

    @app.post("/api/v1/knowledge-proposals/generate")
    def knowledge_proposals_generate(body: KnowledgeProposalGenerate, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 确定性生成（不依赖 LLM）：停止原因/客户反馈/确认推荐聚类，达阈值才出提案，
        # 证据不足只留候选；UNIQUE(proposal_type, content_key) 保证同内容不重复提案。
        # 走 execute_idempotent 幂等 + 审计，重放返回首次响应。
        return idem("knowledge_proposal.generate", body, idempotency_key, "knowledge_proposal", "generate",
                    lambda: knowledge.generate(body.limit))

    @app.post("/api/v1/knowledge-proposals/{proposal_id}/preflight")
    def knowledge_proposal_preflight(proposal_id: str, body: WriteEnvelope) -> dict[str, Any]:
        # 两段确认第一段：发 300s 确认令牌 + 内容签名；404=提案不存在；409=非 pending。
        try:
            return knowledge.preflight(proposal_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/knowledge-proposals/{proposal_id}/decision")
    def knowledge_proposal_decision(proposal_id: str, body: KnowledgeProposalDecision, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 两段确认第二段：校验令牌+签名未漂移后执行。accept → 写入对应知识文件
        # （图谱条目/confirmed_rules 文件，原子写 + 硬链接镜像同步，带 proposed_by 标记）；
        # reject → 落 rejected 含原因。走 execute_idempotent 幂等 + 审计，重放返回首次响应；
        # 404=提案不存在；409=令牌无效/过期/内容漂移/决策非法/拒绝缺原因/知识文件异常。
        return idem("knowledge_proposal.decision", body, idempotency_key, "knowledge_proposal", proposal_id,
                    lambda: knowledge.decide(proposal_id, body.confirmation_token, body.decision, body.note))

    @app.get("/api/v1/company-calibrations")
    def company_calibrations(
        q: str = "",
        status: str = "",
        limit: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        # 二期知识飞轮：核心公司待校准队列（默认 未校准+待复核 优先；q 按名称/赛道/主营业务
        # 搜索；status=calibrated/rejected/needs_review/pending/all 单选，非法 → 409）。
        try:
            return calibration.list_queue(query=q, status=status, limit=limit)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/company-calibrations/progress")
    def company_calibration_progress() -> dict[str, Any]:
        # 校准进度指示：已校准 N / 目标 50 + 各状态计数（只读）。
        return calibration.get_progress()

    @app.get("/api/v1/company-calibrations/{company_key}")
    def company_calibration_detail(company_key: str) -> dict[str, Any]:
        # 校准详情（图谱原始条目 + 校准记录）；公司不在图谱 → 404（LookupError 全局映射）。
        return calibration.get_calibration(company_key)

    @app.post("/api/v1/company-calibrations")
    def company_calibration_submit(body: CompanyCalibrationSubmit, idempotency_key: str = Header(alias="Idempotency-Key")):
        # 提交校准：按 company_key upsert，内容变化 version 自增，同内容重提不 bump（服务层
        # 幂等）；走 execute_idempotent 幂等 + 审计，重放返回首次响应。404=公司不在图谱；
        # 409=状态非法/公司名为空（中文 detail 透出）。
        return idem("company_calibration.submit", body, idempotency_key, "company_calibration", body.company_name,
                    lambda: calibration.submit(
                        body.company_name, status=body.status, track=body.track,
                        product_lines=body.product_lines, skill_tags=body.skill_tags,
                        level_system=body.level_system, no_poach=body.no_poach,
                        non_compete=body.non_compete, note=body.note,
                        calibrated_by=body.calibrated_by))

    def app_ui_allowed(request: Request) -> bool:
        return request.headers.get("user-agent", "").startswith(ASA_APP_USER_AGENT_PREFIX)

    def app_disabled_response() -> Response:
        return Response(
            """
<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>ASA App Only</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:40px;line-height:1.6;color:#17231f">
  <h1>ASA Web 已暂时禁用</h1>
  <p>请从 macOS 应用 <strong>/Users/messi/Applications/ASA.app</strong> 打开 ASA Agent。</p>
  <p>本机 Core 数据服务仍在运行，浏览器工作台入口当前不作为使用路径。</p>
</body>
</html>
            """.strip(),
            status_code=403,
            media_type="text/html; charset=utf-8",
        )

    def web_index() -> Response:
        index = ASA_WEB_DIST / "index.html"
        if not index.exists():
            return JSONResponse({"ok": False, "error": "ASA Web 尚未构建", "diagnostic": str(index)}, status_code=503)
        return FileResponse(index)

    @app.get("/api/v1/dsh-config", include_in_schema=False)
    def dsh_config(request: Request) -> Response:
        # DSH 桥接配置：仅供 ASA app（UA 前缀）读取，前端据此带 Bearer token 访问 DSH 常驻服务器。
        if not app_ui_allowed(request):
            return app_disabled_response()
        return JSONResponse({"token": read_dsh_token(), "url": ASA_DSH_RESIDENT_URL})

    @app.get("/asa-app", include_in_schema=False)
    @app.get("/asa-app/", include_in_schema=False)
    def app_web(request: Request) -> Response:
        if not app_ui_allowed(request):
            return app_disabled_response()
        return web_index()

    @app.get("/workbench", include_in_schema=False)
    @app.get("/asa", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    def disabled_web() -> Response:
        return app_disabled_response()

    @app.get("/admin/legacy", include_in_schema=False)
    async def legacy_admin(request: Request) -> Response:
        return await proxy_legacy(request, runtime, override_path="/workbench")

    if ASA_WEB_DIST.exists():
        assets = ASA_WEB_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    async def compatibility(request: Request, path: str) -> Response:
        return await proxy_legacy(request, runtime)

    return app


async def proxy_legacy(request: Request, runtime: LegacyRuntime | None, override_path: str = "") -> Response:
    if not runtime or not runtime.port:
        return JSONResponse({"ok": False, "error": "legacy compatibility unavailable"}, status_code=503)
    body = await request.body()
    from urllib.parse import urljoin, urlencode, parse_qs
    path = override_path or request.url.path
    safe_path = "/" + path.lstrip("/").replace("..", "")
    query_string = request.url.query
    url = urljoin(f"http://127.0.0.1:{runtime.port}", safe_path)
    if query_string:
        url = f"{url}?{query_string}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length", "connection"}}
    req = urllib.request.Request(url, data=body or None, method=request.method, headers=headers)
    try:
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=120)
        payload, status, response_headers = response.read(), response.status, response.headers
    except urllib.error.HTTPError as exc:
        payload, status, response_headers = exc.read(), exc.code, exc.headers
    passthrough = {k: v for k, v in response_headers.items() if k.lower() in {"content-type", "content-disposition", "cache-control"}}
    return Response(payload, status_code=status, headers=passthrough)


def main() -> int:
    parser = argparse.ArgumentParser(description="ASA Core FastAPI server")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(
        create_app(db_path=Path(args.db).expanduser(), host=args.host, port=args.port),
        host=args.host,
        port=args.port,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
