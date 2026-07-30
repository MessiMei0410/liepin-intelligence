"""Handler extracted from service.py — see _shared.py for shared helpers and constants.

All functions receive 'self' (AgentService instance) as first parameter.
"""

from __future__ import annotations
import hashlib, json, os, secrets, shlex, subprocess, time
from datetime import datetime
from pathlib import Path
from typing import Any

from ._shared import (
    _dumps, _loads, _row, _table_exists, _table_columns,
    PANEL_VERSION, ASSESSMENT_VERSION,
    OPENCLI_BIN, OPENCLI_BROWSER_READ_COMMANDS, OPENCLI_BROWSER_TAB_READ_COMMANDS,
    DECISION_LABELS,
)
from .capability_runtime import ExternalPhaseError, RecruitingCapabilityRuntime, ZERO_RESULT_ATTRIBUTION_LABELS
from .context import build_candidate_context
from .native_attachments import resolve_wechat_attachments
from .policy import action_decision, is_stopped
from .privacy import sanitize_payload
from .schema import ensure_schema
from .skills import SkillRegistry, SkillSpec

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


def get_workflow(self, workflow_id: str) -> dict[str, Any]:
    return self.workflow_engine.get_workflow(workflow_id)


def get_workflow_summary(self, workflow_id: str) -> dict[str, Any]:
    return self.workflow_engine.get_workflow_summary(workflow_id)


def get_workflow_step(self, workflow_id: str, step_id: int) -> dict[str, Any]:
    return self.workflow_engine.get_workflow_step(workflow_id, step_id)


def get_workflow_candidates(self, workflow_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return self.workflow_engine.get_workflow_candidates(workflow_id, limit, offset)


def start_workflow(
    self,
    workflow_id: str,
    *,
    expected_plan_version: int | None = None,
    expected_plan_hash: str = "",
) -> dict[str, Any]:
    return self.workflow_engine.start_workflow(
        workflow_id,
        expected_plan_version=expected_plan_version,
        expected_plan_hash=expected_plan_hash,
    )


def revise_workflow(
    self,
    workflow_id: str,
    instruction: str,
    *,
    effective_constraints: list[dict[str, Any]] | None = None,
    constraint_changes: list[dict[str, Any]] | None = None,
    turn_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return self.workflow_engine.revise_workflow(
        workflow_id,
        instruction,
        effective_constraints=effective_constraints,
        constraint_changes=constraint_changes,
        turn_decision=turn_decision,
    )


def revert_workflow_revision(self, revised_workflow_id: str) -> dict[str, Any]:
    return self.workflow_engine.revert_workflow_revision(revised_workflow_id)


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
        continuation_request = result.pop("_continuation_request", None)
        if isinstance(continuation_request, dict):
            self.workflow_engine.checkpoint_external_continuation(
                step_id, result, continuation_request,
            )
            self.schedule_external_workflow_step(
                step_id, capability_id, continuation_request,
            )
            return
        self.workflow_engine.complete_external_step(step_id, result)
    except ExternalPhaseError as exc:
        self.workflow_engine.fail_external_step(
            step_id,
            str(exc),
            {
                "phase": exc.phase,
                "external_action_executed": True,
                "partial_result": exc.partial_result,
                "detail": exc.detail,
                "retry_mode": "audit_only",
                "request": {"client": request.get("client"), "job": request.get("job")},
            },
        )
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
