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

    @app.get("/api/v1/candidates")
    def candidates(q: str = "", job_id: int | None = None, stage: str = "", limit: int = Query(100, le=200), offset: int = 0) -> dict[str, Any]:
        return core.candidates(query=q, job_id=job_id, stage=stage, limit=limit, offset=offset)

    @app.get("/api/v1/candidates/stop-reasons/summary")
    def stop_reasons_summary() -> dict[str, Any]: return core.stop_reasons_summary()

    @app.get("/api/v1/candidates/{candidate_id}")
    def candidate(candidate_id: int) -> dict[str, Any]: return core.candidate(candidate_id)

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
