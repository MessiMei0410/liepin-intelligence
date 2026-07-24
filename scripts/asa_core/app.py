from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import liepin_workbench_server as legacy
from a_system_agent.service import AgentService

from .database import DEFAULT_DB, migrate
from .service import CoreService


ASA_WEB_DIST = Path(os.environ.get("ASA_WEB_DIST", "/Users/messi/Documents/ASA/dist")).expanduser()
ASA_APP_USER_AGENT_PREFIX = "ASAApp/"


class WriteEnvelope(BaseModel):
    request_id: str = Field(min_length=4)


class WorkflowCreate(WriteEnvelope):
    objective: str = Field(min_length=2)
    context: dict[str, Any] = Field(default_factory=dict)
    priority: int = 2


class WorkflowAction(WriteEnvelope):
    instruction: str = ""
    note: str = ""


class ApprovalDecision(WriteEnvelope):
    decision: str
    note: str = ""


class CopilotMessage(WriteEnvelope):
    message: str = Field(min_length=1)
    session_id: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


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
    core = CoreService(db_path, agent)
    if runtime:
        runtime.state.core_service = core

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        migration = migrate(db_path)
        app.state.migration = migration
        if runtime:
            runtime.start()
        yield
        if runtime:
            runtime.close()
        agent.close()

    app = FastAPI(title="ASA Core", version="1.0.0", lifespan=lifespan)
    app.state.core = core
    app.state.legacy = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
        allow_origin_regex=r"https://([A-Za-z0-9-]+\.)*(liepin\.com|x-saas\.com\.cn)",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "asa-core", "version": "1.0.0", "db": str(db_path)}

    @app.get("/api/v1/bootstrap")
    def bootstrap() -> dict[str, Any]: return core.bootstrap()

    @app.get("/api/v1/dashboard")
    def dashboard() -> dict[str, Any]: return core.dashboard()

    @app.get("/api/v1/jobs")
    def jobs(q: str = "", status: str = "", include_archived: bool = False, limit: int = Query(100, le=200), offset: int = 0) -> dict[str, Any]:
        return core.jobs(query=q, status=status, include_archived=include_archived, limit=limit, offset=offset)

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: int) -> dict[str, Any]: return core.job(job_id)

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
    def candidate_assessment_generate(candidate_id: int, body: WriteEnvelope, job_id: int = Query(...), idempotency_key: str = Header(alias="Idempotency-Key")):
        # S6-1：生成/重新生成判人评估（职业轨迹 + 跳槽质量史）。走 execute_idempotent 幂等 + 审计，
        # 重放返回首次响应；404=人选/岗位不存在或不匹配；409=无简历语料/敏感扫描命中/模型不可用。
        # 同人同岗重复 POST 更新原 artifact（as_of 刷新），不重复建行。评估只辅助判断，不做决策。
        return idem("candidate.assessment_generate", body, idempotency_key, "job_candidate", f"{candidate_id}:{job_id}",
                    lambda: core.generate_candidate_assessment(candidate_id, job_id))

    @app.get("/api/v1/candidates/{candidate_id}/assessments")
    def candidate_assessment_get(candidate_id: int, job_id: int = Query(...)) -> dict[str, Any]:
        # S6-1：读取同人同岗判人评估；人选/岗位不存在或不匹配、尚无评估 → 404（LookupError 全局映射）
        return core.get_candidate_assessment(candidate_id, job_id)

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

    def idem(operation: str, body: WriteEnvelope, key: str, target_type: str, target_id: str, action):
        try:
            response, _ = core.execute_idempotent(
                operation=operation, request_id=body.request_id, idempotency_key=key,
                payload=body.model_dump(), target_type=target_type, target_id=target_id, action=action,
            )
            return response
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/workflows", status_code=201)
    def create_workflow(body: WorkflowCreate, idempotency_key: str = Header(alias="Idempotency-Key")):
        return idem("workflow.create", body, idempotency_key, "workflow", "new", lambda: agent.create_goal(body.objective, body.context, body.priority))

    @app.post("/api/v1/workflows/{workflow_id}/{action_name}")
    def workflow_action(workflow_id: str, action_name: str, body: WorkflowAction, idempotency_key: str = Header(alias="Idempotency-Key")):
        actions = {
            "start": lambda: agent.start_workflow(workflow_id),
            "revise": lambda: agent.revise_workflow(workflow_id, body.instruction),
            "cancel": lambda: agent.cancel_workflow(workflow_id, body.note),
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

    @app.get("/asa-app", include_in_schema=False)
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
    path = override_path or request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    url = f"http://127.0.0.1:{runtime.port}{path}{query}"
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
    uvicorn.run(create_app(db_path=Path(args.db).expanduser(), host=args.host, port=args.port), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
