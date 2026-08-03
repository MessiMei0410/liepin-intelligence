from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .database import DEFAULT_DB, connect, json_value, transaction
from .intent import (
    direct_candidate_action,
    direct_candidate_update,
    intent_signature,
    parse_candidate_intent,
)
from .stop_reasons import STOP_REASON_LABELS, UNLABELED_STOP_REASON_LABEL, normalize_stop_reason
from a_system_agent import knowledge_base as kb_consumption


STOP_TOKENS = ("停止", "淘汰", "不推进", "拒绝", "关闭")
STOP_STAGES = ("初筛不通过", "停止推进", "已停止", "淘汰", "关闭")
STOP_STATUSES = {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
CANDIDATE_ACTION_LABELS = {
    "advance": "复核通过",
    "contact": "已联系",
    "recommend": "已推荐给客户",
    "stop": "复核不通过",
}
CANDIDATE_UPDATE_LABELS = {
    "read_no_reply": "已读未回复",
}
READ_ONLY_COPILOT_ACTIONS = {"open_candidate", "open_job", "open_queue", "open_analysis"}
NON_BUSINESS_COPILOT_ACTIONS = {*READ_ONLY_COPILOT_ACTIONS, "native_action", "floating_action", "open_workflow"}
IDEMPOTENCY_LEASE_MINUTES = 5


class IdempotencyConflict(ValueError):
    """The same idempotency key cannot safely execute another action."""


def _without_workflow_source_claim(answer: Any) -> str:
    """Do not let an Agent-mode analysis imply sourcing ran without a workflow."""
    text = str(answer or "")
    if re.search(r"(?:寻访|搜索).{0,8}(?:已启动|已开始|已经开始|已执行)", text):
        return "已完成查询和分析，尚未创建寻访工作流，因此没有启动寻访。"
    return text


def _candidate_action_card(intent: dict[str, Any]) -> dict[str, Any]:
    """Expose the existing candidate preflight/commit chain as an action card.

    Candidate writes deliberately do not create an agent_action_proposals row:
    their existing one-time preflight token remains authoritative.  A nullable
    proposal_id makes that boundary explicit to every action-card consumer.
    """
    candidate = dict(intent.get("candidate") or {})
    return {
        "proposal_id": None,
        "capability_id": "candidate_action",
        "action_kind": "internal_write",
        "risk_level": "R2",
        "context": {
            "type": "candidate",
            "id": candidate.get("id"),
            "candidate": candidate.get("name"),
            "client": candidate.get("client"),
            "job": candidate.get("job"),
            "stage": candidate.get("stage"),
        },
        "evidence": [
            {"label": "建议原因", "value": intent.get("reason") or "顾问已发起候选人状态动作"},
            {"label": "目标动作", "value": intent.get("action_label") or intent.get("action")},
        ],
        "blocked_reasons": [],
        "next_actions": [
            {"type": "confirm_candidate_intent", "label": "确认执行"},
            {"type": "cancel_candidate_intent", "label": "取消"},
        ],
        "preflight": {"required": True, "expires_at": intent.get("expires_at")},
        "post_check": "candidate_stage",
    }


def _workflow_action_card(result: dict[str, Any]) -> dict[str, Any] | None:
    workflow_id = str(result.get("workflow_id") or "").strip()
    if not workflow_id:
        return None
    approvals = [item for item in (result.get("approvals") or []) if isinstance(item, dict) and item.get("status") == "pending"]
    approval = approvals[0] if approvals else {}
    risk_level = str(approval.get("risk_level") or "R1")
    step = next((item for item in (result.get("plan_summary") or []) if isinstance(item, dict) and item.get("status") not in {"completed", "skipped"}), {})
    understanding = result.get("intent_understanding") if isinstance(result.get("intent_understanding"), dict) else {}
    constraints = [
        str(item.get("quote") or "").strip()
        for item in (understanding.get("constraints") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    plan_ref = dict(result.get("plan_ref") or {})
    workflow_status = str((result.get("workflow") or {}).get("status") or "")
    next_actions = [{"type": "open_workflow", "id": workflow_id, "label": "查看计划"}]
    if workflow_status == "planned" and plan_ref.get("plan_hash"):
        next_actions.append({
            "type": "start_workflow",
            "id": workflow_id,
            "label": "确认计划并准备",
            "plan_ref": plan_ref,
        })
    if approval.get("approval_id"):
        next_actions.append({
            "type": "workflow_approval",
            "id": approval["approval_id"],
            "label": "批准本次外部寻访" if risk_level == "R3" else "批准执行",
        })
    return {
        "proposal_id": None,
        "capability_id": str(step.get("capability_id") or "workflow"),
        "action_kind": "external_write" if risk_level == "R3" else "internal_write",
        "risk_level": risk_level,
        "context": {
            "type": "workflow",
            "id": workflow_id,
            "title": (result.get("goal") or {}).get("title"),
            "plan_ref": plan_ref,
        },
        "evidence": [
            {"label": "工作流", "value": workflow_id},
            {"label": "当前步骤", "value": step.get("label") or (result.get("workflow") or {}).get("current_stage") or "准备执行"},
            *([{"label": "理解目标", "value": understanding.get("objective")}] if understanding.get("objective") else []),
            *([{"label": "原话约束", "value": "；".join(constraints)}] if constraints else []),
        ],
        "blocked_reasons": ["外部动作仍需 R3 单次审批"] if risk_level == "R3" and approval else [],
        "next_actions": next_actions,
        "post_check": "workflow_id",
    }


def _enforce_copilot_action_boundary(result: dict[str, Any]) -> dict[str, Any]:
    """Keep every transport honest about business execution and executable controls."""
    workflow_id = str(result.get("workflow_id") or "").strip()
    cards = [item for item in (result.get("action_cards") or []) if isinstance(item, dict)]
    if not workflow_id:
        result["answer"] = _without_workflow_source_claim(result.get("answer"))
    allowed = set(NON_BUSINESS_COPILOT_ACTIONS)
    result["suggested_actions"] = [
        item
        for item in (result.get("suggested_actions") or [])
        if isinstance(item, dict)
        and str(item.get("type") or "") in allowed
    ]
    if not cards:
        result.pop("action_card", None)
        result.pop("action_cards", None)
    return result


def _public_effective_strategy(metadata: Any, row: Any) -> dict[str, Any]:
    """Project a search artifact to job UI without restricted strategy material."""
    source = metadata if isinstance(metadata, dict) else {}
    v2 = source.get("strategy_v2") if isinstance(source.get("strategy_v2"), dict) else {}
    plan = source.get("plan") if isinstance(source.get("plan"), dict) else {}
    company_tiers: list[dict[str, Any]] = []
    for tier in v2.get("step2_target_pool") or []:
        if not isinstance(tier, dict):
            continue
        companies = [
            str(item.get("name") or "").strip()
            for item in (tier.get("companies") or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and not str(item.get("source") or "").startswith("restricted")
        ]
        if companies:
            company_tiers.append({
                "path": str(tier.get("path") or ""),
                "tier": str(tier.get("tier") or ""),
                "companies": companies,
                "rationale": str(tier.get("rationale") or ""),
            })
    keyword_groups = [
        {
            "group": str(group.get("group") or ""),
            "targets": str(group.get("targets") or ""),
            "terms": [str(term).strip() for term in (group.get("terms") or []) if str(term).strip()],
        }
        for group in (v2.get("step4_keyword_groups") or [])
        if isinstance(group, dict)
    ]
    context = json_value(row["context_json"], {}) if row else {}
    return {
        "status": str(row["workflow_status"] or "") if row else "",
        "plan_version": int(context.get("revision_number") or 0) + 1,
        "generated_at": str(row["created_at"] or "") if row else "",
        "summary": str(
            (v2.get("step1_job_essence") or {}).get("statement")
            or plan.get("strategy_summary")
            or ""
        ),
        "input_level": str(v2.get("input_level") or ""),
        "company_tiers": company_tiers,
        "level_mapping": dict(v2.get("step3_level_mapping") or {}),
        "keyword_groups": keyword_groups,
        "expectation": dict(v2.get("step5_expectation") or {}),
        "consultant_constraints": [
            {
                "type": str(item.get("type") or item.get("kind") or "other"),
                "rule": str(item.get("rule") or item.get("quote") or "").strip(),
            }
            for item in (plan.get("consultant_constraints") or [])
            if isinstance(item, dict) and str(item.get("rule") or item.get("quote") or "").strip()
        ],
        "audit": {
            "workflow_id": str(row["workflow_id"] or "") if row else "",
            "artifact_id": str(row["artifact_id"] or "") if row else "",
            "schema_version": str(source.get("schema_version") or v2.get("schema_version") or ""),
        },
    }

# 确认通道文案：新识别出的意图不直写，先由用户确认（PRD 阶段 4 R9）。
CANDIDATE_ACTION_CONFIRM_TEXTS = {
    "advance": "将标记 {name} 复核通过并继续推进，确认？",
    "contact": "将记录 {name} 已联系，确认？",
    "recommend": "将记录 {name} 已推荐给客户，确认？",
    "stop": "将停止推进 {name}，确认？",
}


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


def _is_stopped(stage: Any, raw_status: Any) -> bool:
    stage_text = str(stage or "")
    return any(token in stage_text for token in STOP_STAGES) or str(raw_status or "").lower() in STOP_STATUSES


_RESUME_OVERVIEW_SECTION_RE = re.compile(
    r"^(工作经历|工作经验|项目经历|项目经验|教育经历|教育背景|技能(?:特长)?|语言能力|自我评价|附件简历)",
    re.MULTILINE,
)


def _resume_overview_summary(resume: dict[str, Any]) -> str:
    """Return only the resume header for the overview API field, never the resume body."""
    for key in ("profile_text", "notes"):
        text = str(resume.get(key) or "").strip()
        if not text:
            continue
        match = _RESUME_OVERVIEW_SECTION_RE.search(text)
        header = text[:match.start()] if match else text
        return header.strip()[:800]
    return ""


def _explicit_candidate_action(message: str) -> str:
    # 直写层已迁入 asa_core.intent（R9 分层解析器），语义逐字保留。
    return direct_candidate_action(message)


def _explicit_candidate_update(message: str) -> str:
    # 直写层已迁入 asa_core.intent（R9 分层解析器），语义逐字保留。
    return direct_candidate_update(message)


def _candidate_action_already_applied(detail: dict[str, Any], action: str) -> bool:
    if action == "stop":
        return bool(detail.get("is_stopped"))
    stage = str(detail.get("clean_stage") or "")
    if _is_stopped(stage, detail.get("raw_status")):
        return False
    match = re.match(r"^S(\d+)\b", stage)
    stage_number = int(match.group(1)) if match else 0
    if action == "advance":
        return stage.startswith("X2 ") or stage_number >= 2
    if action == "contact":
        return stage_number >= 3
    if action == "recommend":
        return stage_number >= 7
    return False


def _funnel_detail(complete: int, partial: int, failed: int) -> dict[str, Any]:
    total = complete + partial + failed
    return {
        "complete": complete,
        "partial": partial,
        "failed": failed,
        "complete_rate": round(complete / total, 4) if total else None,
    }


class CoreService:
    def __init__(self, db_path: Path = DEFAULT_DB, agent_service: Any | None = None, analytics_service: Any | None = None) -> None:
        self.db_path = db_path
        self.agent_service = agent_service
        self.analytics_service = analytics_service
        self._preflight_tokens: dict[str, tuple[int, str, datetime]] = {}
        self._preflight_lock = threading.Lock()
        self._bootstrap_cache: dict[str, Any] | None = None
        self._bootstrap_cache_ts: float = 0.0
        self._bootstrap_cache_ttl: float = 5.0

    def bootstrap(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._bootstrap_cache and (now - self._bootstrap_cache_ts) < self._bootstrap_cache_ttl:
            return self._bootstrap_cache
        dashboard = self.dashboard()
        result = {
            "ok": True,
            "core": {"status": "connected", "db": str(self.db_path), "api_version": "v1"},
            "user": {"id": "local", "name": "本机顾问"},
            "counts": dashboard["counts"],
            "features": {
                "workflows": True,
                "copilot": True,
                "desktop_bridge": True,
                "legacy_admin": True,
            },
        }
        self._bootstrap_cache = result
        self._bootstrap_cache_ts = now
        return result

    def copilot(self, message: str, *, session_id: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("copilot service unavailable")
        raw_context = dict(context or {})
        analysis_request = self._copilot_analysis_request(message, raw_context)
        if analysis_request:
            return self._run_copilot_analysis(
                message, session_id=session_id, context=raw_context,
                catalog_id=analysis_request[0], scope=analysis_request[1],
            )
        action = _explicit_candidate_action(message)
        candidate_id = 0
        if str(raw_context.get("type") or "").strip() == "candidate":
            try:
                candidate_id = int(raw_context.get("id") or 0)
            except (TypeError, ValueError):
                candidate_id = 0

        action_result: dict[str, Any] | None = None
        candidate_update_result: dict[str, Any] | None = None
        action_blocked = ""
        detail: dict[str, Any] = {}
        if action and candidate_id:
            detail = self.candidate(candidate_id)["candidate"]
            if detail["is_stopped"] and action != "stop":
                action_blocked = "该人选已经停止推进，不能直接执行新的推进动作；如需重启，请先做人工状态纠正。"
            elif _candidate_action_already_applied(detail, action):
                action_result = {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "action": action,
                    "stage": detail.get("clean_stage"),
                    "already_applied": True,
                }
            else:
                normalized = re.sub(r"\s+", " ", str(message or "")).strip()
                request_seed = f"{session_id}|{candidate_id}|{action}|{normalized}"
                request_hash = hashlib.sha256(request_seed.encode("utf-8")).hexdigest()[:20]
                request_id = f"copilot_candidate_{request_hash}"

                def commit_action() -> dict[str, Any]:
                    preflight = self.candidate_preflight(candidate_id, action)
                    return self.candidate_commit(
                        candidate_id,
                        action,
                        f"ASA Copilot 指令：{normalized[:160]}",
                        preflight["token"],
                    )

                action_result, _ = self.execute_idempotent(
                    operation="candidate.commit",
                    request_id=request_id,
                    idempotency_key=request_id,
                    payload={"candidate_id": candidate_id, "action": action, "message": normalized},
                    target_type="job_candidate",
                    target_id=str(candidate_id),
                    action=commit_action,
                    actor="user",
                    surface="asa_copilot",
                )
        update_type = _explicit_candidate_update(message)
        if update_type and candidate_id:
            detail = self.candidate(candidate_id)["candidate"]
            normalized = re.sub(r"\s+", " ", str(message or "")).strip()
            request_seed = f"{session_id}|{candidate_id}|{update_type}|{normalized}"
            request_hash = hashlib.sha256(request_seed.encode("utf-8")).hexdigest()[:20]
            request_id = f"copilot_candidate_note_{request_hash}"

            def record_update() -> dict[str, Any]:
                return self.record_candidate_update(candidate_id, update_type)

            candidate_update_result, _ = self.execute_idempotent(
                operation="candidate.note",
                request_id=request_id,
                idempotency_key=request_id,
                payload={"candidate_id": candidate_id, "update_type": update_type, "message": normalized},
                target_type="job_candidate",
                target_id=str(candidate_id),
                action=record_update,
                actor="user",
                surface="asa_copilot",
            )

        # R9：直写层未命中时走分层解析的确认层。新识别出的意图不直写，
        # 产出 pending_intent（含签名 + preflight token），由确认端点执行。
        pending_intent: dict[str, Any] | None = None
        pending_blocked = ""
        pending_unresolved = False
        confirm_intent: dict[str, Any] = {}
        if not action and not update_type:
            parsed_intent = parse_candidate_intent(message)
            if parsed_intent.get("tier") == "confirm" and parsed_intent.get("kind") == "candidate_action":
                confirm_intent = parsed_intent
                pending_action = str(parsed_intent["action"])
                if not candidate_id:
                    # 目标指代无法唯一定位：只追问，绝不产生 pending_intent。
                    pending_unresolved = True
                else:
                    detail = self.candidate(candidate_id)["candidate"]
                    if detail["is_stopped"] and pending_action != "stop":
                        pending_blocked = "该人选已经停止推进，不能直接执行新的推进动作；如需重启，请先做人工状态纠正。"
                    elif _candidate_action_already_applied(detail, pending_action):
                        pass  # 已处于目标状态：无需确认，按普通问答处理
                    else:
                        try:
                            preflight = self.candidate_preflight(candidate_id, pending_action)
                        except ValueError as exc:
                            pending_blocked = str(exc)
                        else:
                            pending_message = re.sub(r"\s+", " ", str(message or "")).strip()
                            pending_intent = {
                                "kind": "candidate_action",
                                "action": pending_action,
                                "action_label": CANDIDATE_ACTION_LABELS[pending_action],
                                "target_scope": "current_candidate",
                                "confidence": parsed_intent["confidence"],
                                "reason": parsed_intent["reason"],
                                "candidate": {
                                    "id": candidate_id,
                                    "name": detail["name"],
                                    "stage": detail.get("clean_stage"),
                                    "job": detail.get("job"),
                                    "client": detail.get("client"),
                                },
                                "confirm_text": CANDIDATE_ACTION_CONFIRM_TEXTS[pending_action].format(name=detail["name"]),
                                "intent_hash": intent_signature("candidate_action", pending_action, candidate_id, pending_message),
                                "preflight_token": preflight["token"],
                                "expires_at": preflight["expires_at"],
                                "message": pending_message,
                            }

        agent_context = dict(raw_context)
        if confirm_intent:
            # 该消息是候选人写入指令（待确认），抑制工作流级意图路由，
            # 防止同一条消息既产生确认卡片又建立/启动工作流。
            agent_context["suppress_goal_intent"] = True
        result = self.agent_service.copilot(message, session_id=session_id, context=agent_context)
        workflow_card = _workflow_action_card(result)
        if workflow_card:
            result["action_card"] = workflow_card
            result["action_cards"] = [workflow_card]
        if action_result:
            action_label = CANDIDATE_ACTION_LABELS[action]
            result["candidate_action"] = action_result
            result["answer"] = (
                f"已同步到 ASA：{detail['name']} {action_label}，"
                f"当前阶段为“{action_result.get('stage') or detail.get('clean_stage') or '已更新'}”。"
            )
        elif candidate_update_result:
            update_label = CANDIDATE_UPDATE_LABELS.get(update_type or "", "跟进情况")
            result["candidate_update"] = candidate_update_result
            result["suggested_actions"] = []
            result["answer"] = (
                f"已更新：{detail.get('name') or '当前人选'}的{update_label}已写入备注和业务时间线，"
                f"当前阶段保持“{candidate_update_result.get('stage') or detail.get('clean_stage') or '已更新'}”。"
            )
        elif action_blocked:
            result["answer"] = f"这条指令未写入 ASA：{action_blocked}"
            result["write_blocked"] = True
        elif action:
            result["answer"] = "这条指令尚未写入 ASA：当前没有唯一定位到人岗关系，请先打开对应候选人后重试。"
            result["write_blocked"] = True
        elif update_type:
            result["answer"] = "这条跟进记录尚未写入 ASA：当前没有唯一定位到人岗关系，请先打开对应候选人后重试。"
            result["suggested_actions"] = []
            result["write_blocked"] = True
        elif pending_intent:
            result["pending_intent"] = pending_intent
            result["action_card"] = _candidate_action_card(pending_intent)
            result["action_cards"] = [result["action_card"]]
            result["answer"] = f"{pending_intent['confirm_text']}\n\n未确认前不会写入 ASA。"
        elif pending_blocked:
            result["answer"] = f"这条指令未写入 ASA：{pending_blocked}"
            result["write_blocked"] = True
        elif pending_unresolved:
            result["answer"] = "这条指令尚未写入 ASA：当前没有唯一定位到人岗关系，请先打开对应候选人后重试。"
            result["write_blocked"] = True
        result = _enforce_copilot_action_boundary(result)
        if action or update_type or pending_intent or workflow_card:
            result_session_id = str(result.get("session_id") or session_id or "")
            if result_session_id:
                with transaction(self.db_path) as conn:
                    existing = conn.execute(
                        """SELECT structured_json FROM agent_copilot_messages
                           WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1""",
                        (result_session_id,),
                    ).fetchone()
                    structured = json.loads(existing["structured_json"] or "{}") if existing else {}
                if not isinstance(structured, dict):
                    structured = {}
                structured.update({
                    "references": result.get("references") or [],
                    "suggested_actions": result.get("suggested_actions") or [],
                    "skill_runs": result.get("skill_runs") or [],
                    "business_focus": result.get("business_focus") or {},
                    "action_card": result.get("action_card"),
                    "action_cards": result.get("action_cards") or [],
                })
                if workflow_card:
                    structured.update({
                        "goal": result.get("goal"), "workflow": result.get("workflow"),
                        "plan_summary": result.get("plan_summary") or [],
                        "workflow_progress": {
                            "workflow_id": workflow_card["context"]["id"],
                            "status": (result.get("workflow") or {}).get("status") or "queued",
                            "completed": (result.get("progress") or {}).get("completed") or 0,
                            "total": (result.get("progress") or {}).get("total") or len(result.get("plan_summary") or []),
                            "label": (result.get("workflow") or {}).get("current_stage") or "准备执行",
                            "pending_approvals": result.get("approvals") or [],
                        },
                    })
                if candidate_update_result:
                    structured["candidate_update"] = candidate_update_result
                if pending_intent:
                    structured["pending_intent"] = pending_intent
                with transaction(self.db_path) as conn:
                    conn.execute(
                        """
                        UPDATE agent_copilot_messages SET content=?,structured_json=?
                         WHERE id=(
                           SELECT id FROM agent_copilot_messages
                            WHERE session_id=? AND role='assistant'
                            ORDER BY id DESC LIMIT 1
                         )
                        """,
                        (result["answer"], json.dumps(structured, ensure_ascii=False), result_session_id),
                    )
        return result

    def _copilot_analysis_request(
        self, message: str, context: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        if not self.analytics_service:
            return None
        text = " ".join(str(message or "").lower().split())
        context_type = str(context.get("type") or "")
        context_id = context.get("id")
        try:
            numeric_context_id = int(context_id) if context_id else None
        except (TypeError, ValueError):
            numeric_context_id = None
        if any(token in text for token in ("数据质量", "重复数据", "缺失数据", "异常状态")):
            return "data_quality", {}
        if any(token in text for token in ("渠道效果", "渠道表现", "哪个渠道", "渠道转化")):
            return "channel_performance", {"days": 30, **({"job_id": numeric_context_id} if context_type == "job" and numeric_context_id else {})}
        if any(token in text for token in ("工作流漏斗", "流程漏斗", "步骤完成率")):
            return "workflow_funnel", {"workflow_id": str(context_id)} if context_type == "workflow" and context_id else {}
        if context_type == "job" and numeric_context_id and any(token in text for token in ("岗位健康", "岗位漏斗", "岗位卡在哪", "卡在哪里", "覆盖怎么样")):
            return "job_health", {"job_id": numeric_context_id, "days": 30}
        overview_intent = any(token in text for token in ("经营概览", "今日概览", "今天先做什么", "今天最需要", "今天优先", "今日待办分析"))
        if overview_intent:
            return "operations_overview", {"days": 7}
        return None

    def _run_copilot_analysis(
        self,
        message: str,
        *,
        session_id: str,
        context: dict[str, Any],
        catalog_id: str,
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        stable_session_id = str(session_id or "").strip() or f"copilot_{secrets.token_hex(6)}"
        run = self.analytics_service.create_run(catalog_id, message, scope)
        analysis = run["result"]
        metrics = list(analysis.get("metrics") or [])[:5]
        card = {
            "schema_version": "analysis_card_v1",
            "run_id": analysis["run_id"],
            "catalog_id": analysis["catalog_id"],
            "status": analysis["status"],
            "headline": analysis["headline"],
            "metrics": metrics,
            "data_as_of": analysis["data_as_of"],
            "scope": analysis["scope"],
            "references": list(analysis.get("references") or [])[:5],
            "open_analysis": {"type": "open_analysis", "id": analysis["run_id"], "label": "查看完整分析"},
        }
        metric_lines = [
            f"{item.get('label')}：{'数据不足' if item.get('value') is None else item.get('value')}"
            for item in metrics[:4]
        ]
        answer = f"结论：{analysis['headline']}\n\n依据：" + "；".join(metric_lines) + "。\n\n下一步：可打开完整分析查看口径和引用。"
        structured = {
            "analysis_card": card,
            "references": card["references"],
            "suggested_actions": [card["open_analysis"]],
            "business_focus": {},
        }
        context_type = str(context.get("type") or "global")
        raw_context_id = context.get("id")
        try:
            context_id = int(raw_context_id) if context_type in {"job", "candidate"} and raw_context_id else None
        except (TypeError, ValueError):
            context_id = None
        with transaction(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO agent_copilot_messages
                   (session_id,context_type,context_id,role,content,structured_json)
                   VALUES (?,?,?,?,?,?)""",
                [
                    (stable_session_id, context_type, context_id, "user", message, json.dumps(context, ensure_ascii=False)),
                    (stable_session_id, context_type, context_id, "assistant", answer, json.dumps(structured, ensure_ascii=False)),
                ],
            )
        return {
            "ok": bool(run.get("ok")), "session_id": stable_session_id, "answer": answer,
            "context": {"type": context_type, "id": raw_context_id},
            "analysis_card": card, "references": card["references"],
            "suggested_actions": [card["open_analysis"]], "action_cards": [],
        }

    def confirm_copilot_intent(
        self,
        intent: dict[str, Any],
        *,
        intent_hash: str,
        candidate_id: int,
        preflight_token: str,
        message: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """执行已确认的候选人意图（PRD 阶段 4 R9 确认通道）。

        重新校验意图签名与候选人当前状态（防状态漂移），命中后走与直写
        相同的 candidate_commit（preflight token）→ 审计 → 幂等链路。
        任何校验失败抛 ValueError，由 API 层映射为 409。
        """
        payload_intent = dict(intent or {})
        kind = str(payload_intent.get("kind") or "")
        confirmed_action = str(payload_intent.get("action") or "")
        if kind != "candidate_action" or confirmed_action not in CANDIDATE_ACTION_LABELS:
            raise ValueError("不支持确认的意图类型，请回到会话重新发起")
        try:
            target_id = int(candidate_id)
        except (TypeError, ValueError):
            raise ValueError("意图目标缺失，请回到会话重新发起") from None
        normalized = re.sub(r"\s+", " ", str(message or payload_intent.get("message") or "")).strip()
        expected_hash = intent_signature(kind, confirmed_action, target_id, normalized)
        if not intent_hash or not secrets.compare_digest(expected_hash, str(intent_hash)):
            raise ValueError("意图签名校验失败，请回到会话重新发起确认")
        if not preflight_token:
            raise ValueError("缺少 preflight token，请回到会话重新发起确认")

        request_seed = f"{session_id}|{target_id}|{confirmed_action}|confirm|{normalized}"
        request_hash = hashlib.sha256(request_seed.encode("utf-8")).hexdigest()[:20]
        request_id = f"copilot_intent_confirm_{request_hash}"

        def commit_action() -> dict[str, Any]:
            # candidate_commit 内部重新校验 preflight token 与候选人当前
            # 状态：确认期间被停止的人选对任何动作（含重复停止）都会在此
            # 抛 ValueError → 409，沿用既有“已停止的再停止→409”语义。
            return self.candidate_commit(
                target_id,
                confirmed_action,
                f"ASA Copilot 确认指令：{normalized[:160]}",
                preflight_token,
            )

        action_result, _ = self.execute_idempotent(
            operation="candidate.commit",
            request_id=request_id,
            idempotency_key=request_id,
            payload={"candidate_id": target_id, "action": confirmed_action, "message": normalized, "channel": "copilot_intent_confirm"},
            target_type="job_candidate",
            target_id=str(target_id),
            action=commit_action,
            actor="user",
            surface="asa_copilot",
        )
        detail = self.candidate(target_id)["candidate"]
        action_label = CANDIDATE_ACTION_LABELS[confirmed_action]
        return {
            "ok": True,
            "candidate_action": action_result,
            "answer": (
                f"已确认并同步到 ASA：{detail['name']} {action_label}，"
                f"当前阶段为\u201c{action_result.get('stage') or detail.get('clean_stage') or '已更新'}\u201d。"
            ),
        }

    def copilot_stream(self, message: str, *, session_id: str = "", context: dict[str, Any] | None = None):
        """Phase 1.1：流式 copilot 回答生成器。复用 agent_service 的 copilot_stream_generator。"""
        if not self.agent_service:
            raise RuntimeError("copilot service unavailable")
        for sse_event in self.agent_service.copilot_stream_generator(message, session_id=session_id, context=context):
            yield sse_event

    def _persist_agent_action_cards(self, result: dict[str, Any]) -> None:
        """Persist Core-owned cards without replacing Agent-owned structured fields."""
        result_session_id = str(result.get("session_id") or "").strip()
        if not result_session_id:
            return
        with transaction(self.db_path) as conn:
            existing = conn.execute(
                """SELECT structured_json FROM agent_copilot_messages
                   WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1""",
                (result_session_id,),
            ).fetchone()
            if not existing:
                return
            try:
                structured = json.loads(existing["structured_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                structured = {}
            if not isinstance(structured, dict):
                structured = {}
            structured.update({
                "references": result.get("references") or [],
                "suggested_actions": result.get("suggested_actions") or [],
                "skill_runs": result.get("skill_runs") or [],
                "business_focus": result.get("business_focus") or {},
                "action_card": result.get("action_card"),
                "action_cards": result.get("action_cards") or [],
            })
            conn.execute(
                """UPDATE agent_copilot_messages SET structured_json=?
                   WHERE id=(SELECT id FROM agent_copilot_messages
                             WHERE session_id=? AND role='assistant'
                             ORDER BY id DESC LIMIT 1)""",
                (json.dumps(structured, ensure_ascii=False), result_session_id),
            )

    def copilot_agent(self, message: str, *, session_id: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run Agent-mode reads through the same action-card boundary as Copilot."""
        if not self.agent_service:
            raise RuntimeError("copilot service unavailable")
        normalized = " ".join(str(message or "").split())
        raw_context = dict(context or {})

        # Candidate mutations retain their existing Core preflight/commit token
        # chain even when the user selected the Agent-mode composer shortcut.
        parsed_intent = parse_candidate_intent(normalized)
        if (
            _explicit_candidate_action(normalized)
            or _explicit_candidate_update(normalized)
            or parsed_intent.get("tier") == "confirm"
        ):
            return self.copilot(normalized, session_id=session_id, context=raw_context)

        result = self.agent_service.copilot_agent(normalized, session_id=session_id, context=raw_context)
        workflow_card = _workflow_action_card(result)
        if workflow_card:
            result["action_card"] = workflow_card
            result["action_cards"] = [workflow_card]
        else:
            # Suggestions from a tool-using answer are informational unless
            # Core can point to a created workflow/action-card record.
            result.pop("action_card", None)
            result.pop("action_cards", None)
            result["answer"] = _without_workflow_source_claim(result.get("answer"))
        result["suggested_actions"] = [
            item for item in (result.get("suggested_actions") or [])
            if isinstance(item, dict) and str(item.get("type") or "") in READ_ONLY_COPILOT_ACTIONS
        ]
        self._persist_agent_action_cards(result)
        return result

    def record_copilot_event(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Copilot 埋点（PRD §9），委托 agent_service 落 agent_copilot_events 表。"""
        if not self.agent_service:
            raise RuntimeError("copilot service unavailable")
        return self.agent_service.record_copilot_event(session_id, event, payload)

    def list_agent_proposals(self, status: str = "pending", limit: int = 20) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("agent service unavailable")
        return self.agent_service.list_proposals(status, limit)

    def generate_agent_proposals(self, job_candidate_ids: list[int], limit: int = 12) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("agent service unavailable")
        return self.agent_service.generate_proposals(job_candidate_ids, limit=limit)

    def preflight_agent_proposal(self, proposal_id: str) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("agent service unavailable")
        return self.agent_service.proposal_preflight(proposal_id)

    def decide_agent_proposal(
        self,
        proposal_id: str,
        confirmation_token: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("agent service unavailable")
        result = self.agent_service.decide_proposal(proposal_id, confirmation_token, decision, note)
        if result["status"] == "rejected":
            return result
        return self.agent_service.execute_proposal(result)

    def agent_action_metrics(self, days: int = 7) -> dict[str, Any]:
        """Read-only seven-day operating metrics for the action-card loop."""
        window_days = max(1, min(int(days or 7), 30))
        conn = connect(self.db_path)
        try:
            since = f"-{window_days} days"
            def count(sql: str, params: tuple[Any, ...] = ()) -> int:
                return int(conn.execute(sql, params).fetchone()[0] or 0)

            generated = count(
                "SELECT count(*) FROM agent_action_proposals WHERE datetime(created_at) >= datetime('now','localtime',?)",
                (since,),
            )
            confirmed = count(
                """SELECT count(*) FROM agent_action_proposals
                   WHERE status IN ('approved','executed','failed')
                     AND datetime(COALESCE(reviewed_at,updated_at,created_at)) >= datetime('now','localtime',?)""",
                (since,),
            )
            rejected = count(
                """SELECT count(*) FROM agent_action_proposals
                   WHERE status='rejected' AND datetime(COALESCE(reviewed_at,updated_at,created_at)) >= datetime('now','localtime',?)""",
                (since,),
            )
            executed = count(
                """SELECT count(*) FROM agent_action_proposals
                   WHERE status='executed' AND datetime(COALESCE(updated_at,created_at)) >= datetime('now','localtime',?)""",
                (since,),
            )
            failed = count(
                """SELECT count(*) FROM agent_action_proposals
                   WHERE status='failed' AND datetime(COALESCE(updated_at,created_at)) >= datetime('now','localtime',?)""",
                (since,),
            )
            has_copilot_messages = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_copilot_messages'"
            ).fetchone() is not None
            clarification = count(
                """SELECT count(*) FROM agent_copilot_messages
                   WHERE role='assistant'
                     AND (
                       json_extract(structured_json,'$.turn_decision.effect')='clarify'
                       OR json_extract(structured_json,'$.intent_understanding.needs_clarification')=1
                     )
                     AND datetime(created_at) >= datetime('now','localtime',?)""",
                (since,),
            ) if has_copilot_messages else 0
            has_approvals = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_approvals'"
            ).fetchone() is not None
            r3_total = count(
                """SELECT count(*) FROM agent_approvals
                   WHERE risk_level='R3' AND datetime(created_at) >= datetime('now','localtime',?)""",
                (since,),
            ) if has_approvals else 0
            r3_approved = count(
                """SELECT count(*) FROM agent_approvals
                   WHERE risk_level='R3' AND status='approved'
                     AND datetime(COALESCE(decided_at,created_at)) >= datetime('now','localtime',?)""",
                (since,),
            ) if has_approvals else 0
            rate = lambda numerator, denominator: round(numerator / denominator, 4) if denominator else None
            return {
                "ok": True,
                "window_days": window_days,
                "metrics": {
                    "action_cards_generated": generated,
                    "confirmed": confirmed,
                    "rejected": rejected,
                    "executed": executed,
                    "failed": failed,
                    "needs_clarification": clarification,
                    "r3_approvals": {"total": r3_total, "approved": r3_approved, "approval_rate": rate(r3_approved, r3_total)},
                    "confirmation_rate": rate(confirmed, generated),
                    "rejection_rate": rate(rejected, generated),
                    "execution_failure_rate": rate(failed, executed + failed),
                },
            }
        finally:
            conn.close()

    def dashboard(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            counts = {
                "active_jobs": conn.execute(
                    """SELECT count(*) FROM jobs
                       WHERE COALESCE(lifecycle_stage,'') IN ('sourcing','published','active_pipeline','client_feedback','offer')
                         AND COALESCE(status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')"""
                ).fetchone()[0],
                "candidates": conn.execute("SELECT count(*) FROM job_candidates").fetchone()[0],
                "pending_candidates": conn.execute(
                    """SELECT count(*) FROM job_candidates
                       WHERE COALESCE(clean_stage,'') NOT LIKE '%初筛不通过%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%停止%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%淘汰%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%关闭%'
                         AND lower(COALESCE(raw_status,'')) NOT IN ('screen_rejected','xsaas_review_stop','stopped','closed','rejected')"""
                ).fetchone()[0],
                "pending_approvals": conn.execute(
                    "SELECT count(*) FROM agent_approvals WHERE status='pending'"
                ).fetchone()[0],
                "pending_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='pending'"
                ).fetchone()[0],
                "executed_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='executed'"
                ).fetchone()[0],
                "failed_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='failed'"
                ).fetchone()[0],
            }
            workflows = [
                _row(row)
                for row in conn.execute(
                    """SELECT w.workflow_id,w.status,w.business_outcome,w.current_stage,w.updated_at,g.title,g.progress
                       FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                       WHERE w.archived_at IS NULL
                       ORDER BY w.updated_at DESC LIMIT 8"""
                )
            ]
            recent = [
                _row(row)
                for row in conn.execute(
                    """SELECT event_key,event_time,operation,target_type,target_id,result
                       FROM v_asa_audit_events ORDER BY COALESCE(event_time,'') DESC LIMIT 12"""
                )
            ]
            return {"ok": True, "counts": counts, "workflows": workflows, "recent_events": recent}
        finally:
            conn.close()

    def jobs(self, *, query: str = "", status: str = "", include_archived: bool = False, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if query:
            clauses.append("(j.title LIKE ? OR c.name LIKE ? OR j.location LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if status:
            clauses.append("(j.status=? OR j.lifecycle_stage=?)")
            params.extend([status, status])
        if not include_archived:
            clauses.append("COALESCE(j.lifecycle_stage,'') NOT IN ('archived','closed','cancelled')")
            clauses.append("COALESCE(j.status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')")
        where = " AND ".join(clauses)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                f"SELECT count(*) FROM jobs j JOIN clients c ON c.id=j.client_id WHERE {where}", params
            ).fetchone()[0]
            rows = [
                _row(row)
                for row in conn.execute(
                    f"""SELECT j.id,j.title,j.job_code,j.location,j.status,j.lifecycle_stage,j.summary,
                               j.hard_requirements,j.updated_at,c.id client_id,c.name client,
                               d.priority,d.risk,d.stop_condition,
                               count(jc.id) candidate_count,
                               sum(CASE WHEN jc.id IS NOT NULL
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%初筛不通过%'
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%停止%'
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%淘汰%'
                                             AND lower(COALESCE(jc.raw_status,'')) NOT IN ('screen_rejected','xsaas_review_stop','stopped','closed','rejected')
                                        THEN 1 ELSE 0 END) active_candidate_count
                          FROM jobs j JOIN clients c ON c.id=j.client_id
                          LEFT JOIN v_job_dashboard d ON d.client=c.name AND d.job=j.title
                          LEFT JOIN job_candidates jc ON jc.job_id=j.id
                         WHERE {where}
                         GROUP BY j.id ORDER BY
                           CASE WHEN d.priority LIKE '%P0-最急%' THEN 0 ELSE 1 END,
                           active_candidate_count DESC,j.updated_at DESC LIMIT ? OFFSET ?""",
                    [*params, min(max(limit, 1), 200), max(offset, 0)],
                )
            ]
            return {"ok": True, "items": rows, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    def job(self, job_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            base = conn.execute(
                """
                SELECT j.*,c.name AS client,
                       m.priority,m.risk,m.stop_condition,m.a_count,m.b_count,m.p0_count,m.p1_count,
                       m.published_count,m.under_review_count,m.contacted_count,m.pending_followup_count,
                       m.next_keywords_json,m.target_companies_json,m.exclude_terms_json,m.data_gap
                FROM jobs j JOIN clients c ON c.id=j.client_id
                LEFT JOIN job_pipeline_metrics m ON m.id=(
                    SELECT m2.id FROM job_pipeline_metrics m2 WHERE m2.job_id=j.id ORDER BY m2.id DESC LIMIT 1
                )
                WHERE j.id=?
                """,
                (int(job_id),),
            ).fetchone()
            if not base:
                raise LookupError("job not found")
            item = _row(base)
            for source, target in (
                ("next_keywords_json", "next_keywords"),
                ("target_companies_json", "metric_target_companies"),
                ("exclude_terms_json", "exclude_terms"),
            ):
                item[target] = json_value(item.pop(source, "[]"), [])

            position = conn.execute(
                "SELECT * FROM positions WHERE client=? AND title=? ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1",
                (item["client"], item["title"]),
            ).fetchone()
            item["position"] = _row(position)
            profile = conn.execute(
                "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1",
                (item["client"], item["title"]),
            ).fetchone()
            profile_item = _row(profile)
            for field in (
                "hard_requirements_json", "ability_keywords_json", "target_companies_json",
                "exclusion_tags_json", "search_keywords_json", "source_position_ids_json",
                "soft_preferences_json", "pitch_points_json", "risk_points_json",
            ):
                if field in profile_item:
                    profile_item[field.removesuffix("_json")] = json_value(profile_item.pop(field), [])
            item["profile"] = profile_item

            candidate_rows = conn.execute(
                """
                SELECT jc.id,p.id AS person_id,p.display_name AS name,p.current_company,p.current_title,
                       p.city,p.education,p.experience,jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                       COALESCE(sp.source_type,CASE WHEN jc.source_candidate_id IS NOT NULL THEN 'liepin' END,'talent_pool') AS source_type
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN source_profiles sp ON sp.id=(
                    SELECT sp2.id FROM source_profiles sp2 WHERE sp2.person_id=p.id ORDER BY sp2.id DESC LIMIT 1
                )
                WHERE jc.job_id=? ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC
                """,
                (int(job_id),),
            ).fetchall()
            candidates = []
            for row in candidate_rows:
                candidate = _row(row)
                candidate["is_stopped"] = _is_stopped(candidate.get("clean_stage"), candidate.get("raw_status"))
                candidates.append(candidate)
            item["candidates"] = candidates
            item["funnel"] = {
                "total": len(candidates),
                "active": sum(not candidate["is_stopped"] for candidate in candidates),
                "stopped": sum(candidate["is_stopped"] for candidate in candidates),
                "contacted": sum(
                    any(token in str(candidate.get("clean_stage") or "") for token in ("已触达", "已联系", "已沟通", "已推荐", "面试", "Offer"))
                    for candidate in candidates if not candidate["is_stopped"]
                ),
                "recommended": sum(
                    any(token in str(candidate.get("clean_stage") or "") for token in ("已推荐", "客户", "面试", "Offer"))
                    for candidate in candidates if not candidate["is_stopped"]
                ),
            }
            stages: dict[str, int] = {}
            for candidate in candidates:
                stage = str(candidate.get("clean_stage") or candidate.get("flow_bucket") or "待复核")
                stages[stage] = stages.get(stage, 0) + 1
            item["stages"] = [
                {"stage": stage, "count": count}
                for stage, count in sorted(stages.items(), key=lambda pair: (-pair[1], pair[0]))
            ]
            item["events"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,event_type,event_status,event_time,summary,job_candidate_id,person_id
                    FROM candidate_events WHERE job_id=?
                    ORDER BY COALESCE(event_time,'') DESC,id DESC LIMIT 60
                    """,
                    (int(job_id),),
                ).fetchall()
            ]
            item["search_experiments"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,channel,query,result_count,viewed_count,extracted_count,recommended_count,
                           reply_count,positive_reply_count,noise_notes,status,run_time,updated_at
                    FROM search_experiments WHERE client=? AND position=?
                    ORDER BY COALESCE(updated_at,run_time,created_at) DESC,id DESC LIMIT 30
                    """,
                    (item["client"], item["title"]),
                ).fetchall()
            ]
            latest_strategy = conn.execute(
                """
                SELECT a.artifact_id,a.workflow_id,a.metadata_json,a.created_at,
                       w.status AS workflow_status,g.context_json
                FROM agent_artifacts a
                JOIN agent_workflows w ON w.workflow_id=a.workflow_id
                JOIN agent_goals g ON g.goal_id=a.goal_id
                WHERE a.artifact_type='search_strategy'
                  AND g.context_type='job' AND g.context_id=?
                  AND w.status NOT IN ('cancelled','superseded','archived')
                ORDER BY datetime(a.created_at) DESC,a.id DESC
                LIMIT 1
                """,
                (int(job_id),),
            ).fetchone()
            item["latest_effective_strategy"] = (
                _public_effective_strategy(json_value(latest_strategy["metadata_json"], {}), latest_strategy)
                if latest_strategy
                else None
            )
            item["followups"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,job_candidate_id,candidate_name,candidate_company,task_type,priority,due_at,status,reason,updated_at
                    FROM followup_tasks WHERE client=? AND position=? AND COALESCE(status,'open') NOT IN ('closed','completed','done')
                    ORDER BY COALESCE(due_at,'9999-12-31'),priority,id DESC LIMIT 40
                    """,
                    (item["client"], item["title"]),
                ).fetchall()
            ]
            # S4-2：客户画像挂载（PRD §3.1）。只注入白名单六类（赛道/卖点/面试流程/
            # 用人偏好/目标池/注意事项）；知识库缺失或异常一律降级为未挂载，绝不影响岗位详情。
            try:
                match, _trace = kb_consumption.match_client_profile(item.get("client"))
                item["client_profile"] = (
                    {
                        "matched": True,
                        "name": match["name"],
                        "rule": match["rule"],
                        "needs_confirmation": match["needs_confirmation"],
                        "context": kb_consumption.profile_context(match["profile"]),
                    }
                    if match
                    else {"matched": False}
                )
            except Exception:
                item["client_profile"] = {"matched": False}
            return {"ok": True, "job": item}
        finally:
            conn.close()

    def candidates(
        self, *, query: str = "", job_id: int | None = None, stage: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if query:
            clauses.append("(p.display_name LIKE ? OR p.current_company LIKE ? OR p.current_title LIKE ? OR j.title LIKE ?)")
            params.extend([f"%{query}%"] * 4)
        if job_id:
            clauses.append("jc.job_id=?")
            params.append(job_id)
        if stage:
            clauses.append("(jc.clean_stage=? OR jc.flow_bucket=?)")
            params.extend([stage, stage])
        where = " AND ".join(clauses)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                f"""SELECT count(*) FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                    LEFT JOIN jobs j ON j.id=jc.job_id WHERE {where}""", params
            ).fetchone()[0]
            rows = [
                _row(row)
                for row in conn.execute(
                    f"""SELECT jc.id,p.id person_id,p.display_name name,p.current_company,p.current_title,
                               p.city,p.education,p.experience,j.id job_id,j.title job,c.name client,
                               jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                               COALESCE(sp.source_type,CASE WHEN jc.source_candidate_id IS NOT NULL THEN 'liepin' END,'talent_pool') source_type
                          FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                          LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                          LEFT JOIN source_profiles sp ON sp.id=(SELECT sp2.id FROM source_profiles sp2 WHERE sp2.person_id=p.id ORDER BY sp2.id DESC LIMIT 1)
                         WHERE {where} ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC LIMIT ? OFFSET ?""",
                    [*params, min(max(limit, 1), 200), max(offset, 0)],
                )
            ]
            return {"ok": True, "items": rows, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    def candidate(self, candidate_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            base = conn.execute(
                """SELECT jc.*,p.display_name name,p.current_company,p.current_title,p.city,p.education,p.experience,
                          j.title job,c.name client,
                          legacy.skills AS legacy_profile_text,legacy.notes AS legacy_notes,
                          legacy.source AS legacy_source,legacy.xsaas_id AS legacy_xsaas_id
                     FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                     LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                     LEFT JOIN candidates legacy ON CAST(legacy.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
                    WHERE jc.id=?""",
                (candidate_id,),
            ).fetchone()
            if not base:
                raise LookupError("candidate not found")
            item = _row(base)
            profiles = [_row(row) for row in conn.execute(
                "SELECT * FROM source_profiles WHERE person_id=? ORDER BY COALESCE(source_date,''),id DESC", (item["person_id"],)
            )]
            links = [_row(row) for row in conn.execute(
                "SELECT source_system,source_entity_type,source_entity_id,source_url,metadata_json FROM entity_source_links WHERE canonical_type='person' AND canonical_id=? ORDER BY id DESC",
                (str(item["person_id"]),),
            )]
            for link in links:
                link["metadata"] = json_value(link.pop("metadata_json"), {})
            known_urls = {str(link.get("source_url") or "") for link in links}
            event_sources = conn.execute(
                """
                SELECT source_table,source_id,raw_json
                  FROM candidate_events
                 WHERE job_candidate_id=? OR person_id=?
                 ORDER BY COALESCE(event_time,'') DESC,id DESC
                """,
                (candidate_id, item["person_id"]),
            ).fetchall()
            event_profile_payloads: list[dict[str, Any]] = []
            for source in event_sources:
                raw = json_value(source["raw_json"], {})
                if not isinstance(raw, dict):
                    raw = {}
                if any(raw.get(key) for key in ("full_text", "profile_text", "candidate_profile_text", "content")):
                    event_profile_payloads.append(raw)
                source_url = str(
                    raw.get("source_url")
                    or raw.get("resume_url")
                    or (source["source_id"] if str(source["source_id"] or "").startswith("http") else "")
                ).strip()
                if not source_url.startswith("http") or source_url in known_urls:
                    continue
                source_hint = f"{source['source_table'] or ''} {source_url}".lower()
                source_system = "xsaas" if "xsaas" in source_hint or "x-saas" in source_hint else "liepin"
                links.append(
                    {
                        "source_system": source_system,
                        "source_entity_type": "external_profile",
                        "source_entity_id": source_url,
                        "source_url": source_url,
                        "metadata": {"resolved_from": "candidate_events"},
                    }
                )
                known_urls.add(source_url)
            resume: dict[str, Any] = {}
            for profile in profiles:
                raw = json_value(profile.get("raw_json"), {})
                if len(str(raw.get("full_text") or raw.get("profile_text") or "")) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
                    resume = raw
                source_url = str(raw.get("source_url") or raw.get("resume_url") or "").strip()
                source_type = str(profile.get("source_type") or "").strip().lower()
                source_candidate_id = str(profile.get("source_candidate_id") or "").strip()
                if not source_url and source_candidate_id:
                    if source_type == "liepin":
                        source_url = (
                            "https://h.liepin.com/resume/showresumedetail/"
                            f"?showsearchfeedback=1&res_id_encode={source_candidate_id}"
                        )
                    elif source_type in {"xsaas", "x-saas"}:
                        source_url = f"https://headhunt.x-saas.com.cn/#/app/candidate/info/{source_candidate_id}"
                if source_url.startswith("http") and source_url not in known_urls:
                    links.append(
                        {
                            "source_system": "xsaas" if source_type in {"xsaas", "x-saas"} else "liepin",
                            "source_entity_type": "source_profile",
                            "source_entity_id": source_candidate_id or source_url,
                            "source_url": source_url,
                            "metadata": {"resolved_from": "source_profiles"},
                        }
                    )
                    known_urls.add(source_url)
            for raw in event_profile_payloads:
                candidate_text = str(
                    raw.get("full_text")
                    or raw.get("profile_text")
                    or raw.get("candidate_profile_text")
                    or raw.get("content")
                    or ""
                )
                current_text = str(resume.get("full_text") or resume.get("profile_text") or "")
                if len(candidate_text) > len(current_text):
                    resume = {
                        **raw,
                        "profile_text": raw.get("profile_text") or raw.get("candidate_profile_text") or candidate_text,
                        "full_text": raw.get("full_text") or candidate_text,
                    }
            legacy_profile_text = str(item.get("legacy_profile_text") or "").strip()
            if len(legacy_profile_text) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
                resume = {
                    "profile_text": legacy_profile_text,
                    "full_text": legacy_profile_text,
                    "notes": item.get("legacy_notes") or "",
                    "source": item.get("legacy_source") or "",
                    "backfilled_from": "candidates.skills",
                }
            events = [_row(row) for row in conn.execute(
                "SELECT id,event_type,event_status,event_time,summary,source_table,source_id FROM candidate_events WHERE job_candidate_id=? OR (job_candidate_id IS NULL AND person_id=?) ORDER BY COALESCE(event_time,'') DESC,id DESC LIMIT 100",
                (candidate_id, item["person_id"]),
            )]
            relations = [_row(row) for row in conn.execute(
                """SELECT jc.id,j.id job_id,j.title job,c.name client,jc.clean_stage,jc.flow_bucket,jc.updated_at
                     FROM job_candidates jc LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                    WHERE jc.person_id=? ORDER BY COALESCE(jc.updated_at,'') DESC""", (item["person_id"],)
            )]
            item["resume"] = {
                "summary": _resume_overview_summary(resume),
                "full_text": resume.get("full_text") or "",
                "work_text": resume.get("work_text") or "",
                "project_text": resume.get("project_text") or "",
                "education_text": resume.get("education_text") or "",
                "raw": resume,
            }
            item["source_links"] = links
            item["events"] = events
            item["job_relations"] = relations
            attributions = [_row(row) for row in conn.execute(
                """
                SELECT sa.*,
                       COALESCE(SUM(sf.weight),0) AS learning_score,
                       COUNT(sf.id) AS signal_count,
                       SUM(sf.signal_type='review_pass') AS review_pass_count,
                       SUM(sf.signal_type='contacted') AS contacted_count,
                       SUM(sf.signal_type='recommended') AS recommended_count,
                       SUM(sf.signal_type='stopped') AS stopped_count,
                       SUM(sf.signal_type IN ('client_approved','client_interview','client_offer','client_hired')) AS client_positive_count,
                       SUM(sf.signal_type='client_rejected') AS client_rejected_count
                FROM agent_sourcing_attributions sa
                LEFT JOIN agent_sourcing_feedback sf ON sf.attribution_id=sa.id
                WHERE sa.job_candidate_id=?
                GROUP BY sa.id ORDER BY sa.id
                """,
                (candidate_id,),
            )]
            item["sourcing_attributions"] = attributions
            item["is_stopped"] = _is_stopped(item.get("clean_stage"), item.get("raw_status"))
            # stop_reason 保留旧语义（备注文本）；R10 新增 stop_reason_code/label 枚举视图。
            stop_reason_code = str(item.get("stop_reason") or "").strip()
            item["stop_reason_code"] = stop_reason_code if item["is_stopped"] else ""
            item["stop_reason_label"] = STOP_REASON_LABELS.get(stop_reason_code, "") if item["is_stopped"] else ""
            item["stop_reason"] = item.get("clean_reason") if item["is_stopped"] else ""
            for internal_key in ("legacy_profile_text", "legacy_notes", "legacy_source", "legacy_xsaas_id"):
                item.pop(internal_key, None)
            return {"ok": True, "candidate": item}
        finally:
            conn.close()

    def workflow(self, workflow_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_workflow(workflow_id)
        raise RuntimeError("workflow service unavailable")

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

    def create_mapping_task(self, job_id: int, *, trigger: str = "manual") -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_mapping_task(job_id, trigger=trigger)
        raise RuntimeError("workflow service unavailable")

    def get_mapping_task(self, job_id: int, artifact_id: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_mapping_task(job_id, artifact_id)
        raise RuntimeError("workflow service unavailable")

    def generate_candidate_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.generate_candidate_assessment(candidate_id, job_id)
        raise RuntimeError("workflow service unavailable")

    def get_candidate_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_candidate_assessment(candidate_id, job_id)
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

    def audit_events(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            items = [_row(row) for row in conn.execute(
                "SELECT * FROM v_asa_audit_events ORDER BY COALESCE(event_time,'') DESC LIMIT ? OFFSET ?",
                (min(max(limit, 1), 500), max(offset, 0)),
            )]
            return {"ok": True, "items": items}
        finally:
            conn.close()

    def stop_reasons_summary(self) -> dict[str, Any]:
        """停止原因统计（PRD 阶段 4 R10）：8 枚举计数 + 中文标签。

        停止判定与 _is_stopped 同口径；stop_reason 为 NULL/空/未知值的
        历史行单独归入"未标注"，不并入任何枚举。
        """
        stage_clause = " OR ".join("clean_stage LIKE ?" for _ in STOP_STAGES)
        status_clause = ",".join("?" for _ in STOP_STATUSES)
        where = f"({stage_clause}) OR lower(COALESCE(raw_status,'')) IN ({status_clause})"
        params: list[Any] = [f"%{token}%" for token in STOP_STAGES] + sorted(STOP_STATUSES)
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                f"""SELECT COALESCE(NULLIF(trim(stop_reason),''),'') AS code, COUNT(*) AS n
                      FROM job_candidates WHERE {where} GROUP BY code""",
                params,
            ).fetchall()
        finally:
            conn.close()
        counts = {str(row["code"]): int(row["n"]) for row in rows}
        items = [
            {"reason": code, "label": label, "count": counts.pop(code, 0)}
            for code, label in STOP_REASON_LABELS.items()
        ]
        unlabeled = sum(counts.values())
        return {
            "ok": True,
            "total_stopped": unlabeled + sum(item["count"] for item in items),
            "items": items,
            "unlabeled": {"label": UNLABELED_STOP_REASON_LABEL, "count": unlabeled},
        }

    def execute_idempotent(
        self,
        *,
        operation: str,
        request_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        target_type: str,
        target_id: str,
        action: Callable[[], dict[str, Any]],
        actor: str = "local",
        surface: str = "asa_web",
    ) -> tuple[dict[str, Any], bool]:
        if not request_id or not idempotency_key:
            raise ValueError("request_id and Idempotency-Key are required")
        # target 也纳入 hash：同一 key+body 打到不同 target 必须判 409 冲突，
        # 否则路径参数里的目标（如 sessions/{id}）会被静默重放成第一个 target 的响应。
        request_hash = hashlib.sha256(
            json.dumps(
                {"payload": payload, "target_type": target_type, "target_id": target_id},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        conflict = ""
        with transaction(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM api_idempotency WHERE idempotency_key=? AND operation=?",
                (idempotency_key, operation),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("idempotency key reused with a different payload")
                if existing["response_json"]:
                    replay = json_value(existing["response_json"], {})
                    if isinstance(replay.get("receipt"), dict):
                        replay["receipt"]["idempotent_replay"] = True
                    return replay, True
                if str(existing["status"] or "") == "failed":
                    error = json_value(existing["error_json"], {})
                    detail = str(error.get("message") or "unknown error")
                    conflict = f"previous request failed; create a new request after reconciliation: {detail}"
                else:
                    expires_at = str(existing["expires_at"] or "")
                    expired = bool(
                        expires_at
                        and datetime.fromisoformat(expires_at.replace(" ", "T")) <= datetime.now()
                    )
                    if expired:
                        error = {
                            "type": "abandoned_processing",
                            "message": "processing lease expired before a result was recorded",
                            "outcome": "unknown",
                        }
                        conn.execute(
                            """
                            UPDATE api_idempotency
                               SET status='failed',response_status=500,error_json=?,updated_at=datetime('now','localtime'),expires_at=NULL
                             WHERE id=?
                            """,
                            (json.dumps(error, ensure_ascii=False), int(existing["id"])),
                        )
                        conflict = "previous request failed; its final business outcome is unknown"
                    else:
                        conflict = "request is already processing"
            else:
                expires_at = (datetime.now() + timedelta(minutes=IDEMPOTENCY_LEASE_MINUTES)).isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO api_idempotency
                        (idempotency_key,operation,request_id,request_hash,status,expires_at,updated_at)
                    VALUES (?,?,?,?, 'processing',?,datetime('now','localtime'))
                    """,
                    (idempotency_key, operation, request_id, request_hash, expires_at),
                )
        if conflict:
            raise IdempotencyConflict(conflict)
        try:
            response = action()
        except Exception as exc:
            audit_id = f"audit_{secrets.token_hex(8)}"
            response_status = 409 if isinstance(exc, ValueError) else 404 if isinstance(exc, LookupError) else 500
            error = {
                "type": type(exc).__name__,
                "message": str(exc)[:500] or type(exc).__name__,
            }
            with transaction(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE api_idempotency
                       SET status='failed',response_status=?,error_json=?,updated_at=datetime('now','localtime'),expires_at=NULL
                     WHERE idempotency_key=? AND operation=?
                    """,
                    (response_status, json.dumps(error, ensure_ascii=False), idempotency_key, operation),
                )
                conn.execute(
                    """INSERT INTO audit_events
                       (event_id,actor,surface,request_id,operation,target_type,target_id,result,metadata_json)
                       VALUES (?,?,?,?,?,?,?,'failed',?)""",
                    (
                        audit_id,
                        actor,
                        surface,
                        request_id,
                        operation,
                        target_type,
                        target_id,
                        json.dumps({"idempotency_key": idempotency_key, "error": error}, ensure_ascii=False),
                    ),
                )
            raise
        audit_id = f"audit_{secrets.token_hex(8)}"
        response = {**response, "receipt": {"audit_event_id": audit_id, "request_id": request_id, "idempotent_replay": False}}
        with transaction(self.db_path) as conn:
            conn.execute(
                """
                UPDATE api_idempotency
                   SET status='completed',response_status=200,response_json=?,error_json=NULL,
                       updated_at=datetime('now','localtime'),expires_at=NULL
                 WHERE idempotency_key=? AND operation=?
                """,
                (json.dumps(response, ensure_ascii=False), idempotency_key, operation),
            )
            conn.execute(
                """INSERT INTO audit_events(event_id,actor,surface,request_id,operation,target_type,target_id,after_json,result,metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (audit_id, actor, surface, request_id, operation, target_type, target_id,
                 json.dumps(response, ensure_ascii=False), "success", json.dumps({"idempotency_key": idempotency_key})),
            )
        return response, False

    def _record_sourcing_signal_safely(
        self,
        candidate_id: int,
        signal: str,
        *,
        actor_type: str,
        note: str,
        source_type: str,
        source_id: int,
    ) -> dict[str, Any] | None:
        if not signal or not self.agent_service:
            return None
        try:
            return self.agent_service.record_sourcing_business_signal(
                candidate_id,
                signal,
                actor_type=actor_type,
                note=note,
                source_type=source_type,
                source_id=source_id,
            )
        except Exception as exc:
            failure = {
                "recorded": False,
                "error": "learning backflow failed; the business write remains committed",
                "error_type": type(exc).__name__,
            }
            try:
                with transaction(self.db_path) as conn:
                    conn.execute(
                        """INSERT INTO audit_events
                           (event_id,actor,surface,request_id,operation,target_type,target_id,result,business_event_type,business_event_id,metadata_json)
                           VALUES (?,?,?,?,?,?,?,'failed',?,?,?)""",
                        (
                            f"audit_{secrets.token_hex(8)}",
                            "system",
                            "asa_core",
                            f"learning_backflow_{source_id}",
                            "candidate.learning_backflow",
                            "job_candidate",
                            str(candidate_id),
                            source_type,
                            str(source_id),
                            json.dumps({**failure, "signal": signal}, ensure_ascii=False),
                        ),
                    )
            except Exception:
                pass
            return failure

    def record_candidate_update(self, candidate_id: int, update_type: str) -> dict[str, Any]:
        if update_type not in CANDIDATE_UPDATE_LABELS:
            raise ValueError("unsupported candidate update")
        label = CANDIDATE_UPDATE_LABELS[update_type]
        note = f"ASA Copilot 跟进记录：{label}"
        stage_changed = False
        with transaction(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT person_id,job_id,clean_stage,flow_bucket,raw_status,source_candidate_id
                  FROM job_candidates WHERE id=?
                """,
                (candidate_id,),
            ).fetchone()
            if not row:
                raise LookupError("candidate not found")
            current_stage = str(row["clean_stage"] or "")
            current_status = str(row["raw_status"] or "")
            next_stage = current_stage
            next_bucket = str(row["flow_bucket"] or "")
            next_status = current_status
            stage_match = re.match(r"^S(\d+)\b", current_stage)
            stage_number = int(stage_match.group(1)) if stage_match else 0
            later_stage = stage_number >= 3 or any(
                token in current_stage for token in ("已联系", "已触达", "回复", "推荐", "面试", "Offer", "谈薪", "入职")
            ) or current_status.lower() in {"contacted", "replied", "recommended", "interview", "offer", "onboarded"}
            if not _is_stopped(current_stage, current_status) and not later_stage:
                next_stage = "S3 已联系/待回复"
                next_bucket = "联系推进"
                next_status = "contacted"
                stage_changed = True
                conn.execute(
                    """
                    UPDATE job_candidates
                       SET clean_stage=?,flow_bucket=?,raw_status=?,raw_stage=?,clean_reason=?,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (next_stage, next_bucket, next_status, next_stage, note, candidate_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE job_candidates
                       SET clean_reason=CASE
                             WHEN trim(COALESCE(clean_reason,''))='' THEN ?
                             ELSE trim(clean_reason) || '｜' || ?
                           END,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (note, note, candidate_id),
                )

            source_candidate_id = str(row["source_candidate_id"] or "").strip()
            if source_candidate_id.isdigit():
                conn.execute(
                    """
                    UPDATE candidates
                       SET status=CASE WHEN ? THEN 'contacted' ELSE status END,
                           notes=CASE
                             WHEN trim(COALESCE(notes,''))='' THEN ?
                             ELSE trim(notes) || '｜' || ?
                           END,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (stage_changed, note, note, int(source_candidate_id)),
                )
            cursor = conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
                VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'api_v1')
                """,
                (
                    candidate_id, row["person_id"], row["job_id"], "candidate_contact_update", update_type,
                    note, json.dumps({"update_type": update_type, "actor": "user"}, ensure_ascii=False),
                ),
            )
        learning = self._record_sourcing_signal_safely(
            candidate_id,
            "contacted" if stage_changed else "",
            actor_type="user",
            note=note,
            source_type="candidate_event",
            source_id=int(cursor.lastrowid),
        )
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "update_type": update_type,
            "label": label,
            "stage": next_stage,
            "business_event_id": cursor.lastrowid,
            "sourcing_learning": learning,
        }

    def candidate_preflight(self, candidate_id: int, action: str) -> dict[str, Any]:
        if action not in {"advance", "review", "contact", "recommend", "stop"}:
            raise ValueError("unsupported candidate action")
        detail = self.candidate(candidate_id)["candidate"]
        if detail["is_stopped"]:
            raise ValueError("该人选关系已停止推进；如需重新启用，请先执行人工状态纠正")
        token = secrets.token_urlsafe(24)
        expires = datetime.now() + timedelta(minutes=5)
        with self._preflight_lock:
            now = datetime.now()
            self._preflight_tokens = {key: value for key, value in self._preflight_tokens.items() if value[2] > now}
            self._preflight_tokens[token] = (candidate_id, action, expires)
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": action,
            "candidate": {"id": candidate_id, "name": detail["name"], "stage": detail.get("clean_stage")},
            "impact": "候选人关系状态将更新，并写入业务时间线和统一审计。",
        }

    def candidate_commit(self, candidate_id: int, action: str, note: str, preflight_token: str, *, reason: str = "") -> dict[str, Any]:
        with self._preflight_lock:
            grant = self._preflight_tokens.pop(preflight_token, None)
        if not grant or grant[0] != candidate_id or grant[1] != action or grant[2] <= datetime.now():
            raise ValueError("preflight token is invalid, expired, or already used")
        if action not in {"advance", "review", "contact", "recommend", "stop"}:
            raise ValueError("unsupported candidate action")
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT person_id,job_id,clean_stage,raw_status,source_candidate_id FROM job_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if not row:
                raise LookupError("candidate not found")
            if _is_stopped(row["clean_stage"], row["raw_status"]):
                raise ValueError("该人选关系已停止推进；禁止直接复核、推进或重复停止")
            current_detail = {"clean_stage": row["clean_stage"], "raw_status": row["raw_status"], "is_stopped": False}
            if action in {"advance", "contact", "recommend"} and _candidate_action_already_applied(current_detail, action):
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "action": action,
                    "stage": row["clean_stage"],
                    "already_applied": True,
                }
            is_xsaas = str(row["clean_stage"] or "").startswith("X") or str(row["raw_status"] or "").startswith("xsaas_")
            mapping = {
                "advance": (
                    "X2 X-SaaS复核通过/待人工联系" if is_xsaas else "S2 复核通过/待联系",
                    "待人工联系/转猎聘或微信" if is_xsaas else "待联系",
                    "resume_review_completed",
                    "continue",
                    "xsaas_review_continue" if is_xsaas else "review_passed",
                    "review_pass",
                    "new",
                ),
                "review": ("S1 新增寻访/待复核", "待复核", "candidate_review_requested", "review", "pending_review", "", "new"),
                "contact": ("S3 已联系/待回复", "联系推进", "candidate_contact_update", "contacted", "contacted", "contacted", "contacted"),
                "recommend": ("S7 已推荐客户/待反馈", "客户推荐", "candidate_recommended", "recommended", "recommended", "recommended", "recommended"),
                "stop": (
                    "H5 最近寻访/初筛不通过",
                    "最近寻访",
                    "resume_review_completed",
                    "stop",
                    "xsaas_review_stop" if is_xsaas else "screen_rejected",
                    "stopped",
                    "screen_rejected",
                ),
            }
            stage, bucket, event_type, event_status, raw_status, learning_signal, candidate_status = mapping[action]
            stop_reason = ""
            if action == "stop":
                # R10 停止原因标准化：reason 命中 8 枚举→存枚举值；缺失/未知/自由
                # 文本→存 'other' 并把原文并入备注（不报错阻断，note-only 旧载荷不变）。
                stop_reason, note = normalize_stop_reason(reason, note)
            event_reason = note or stage
            if stop_reason:
                conn.execute(
                    """
                    UPDATE job_candidates
                       SET clean_stage=?,flow_bucket=?,raw_status=?,raw_stage=?,clean_reason=?,
                           stop_reason=?,updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (stage, bucket, raw_status, stage, event_reason, stop_reason, candidate_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE job_candidates
                       SET clean_stage=?,flow_bucket=?,raw_status=?,raw_stage=?,clean_reason=?,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (stage, bucket, raw_status, stage, event_reason, candidate_id),
                )
            source_candidate_id = str(row["source_candidate_id"] or "").strip()
            if source_candidate_id.isdigit():
                conn.execute(
                    """
                    UPDATE candidates
                       SET status=?,
                           notes=CASE
                             WHEN ?='' THEN notes
                             WHEN trim(COALESCE(notes,''))='' THEN ?
                             ELSE trim(notes) || '｜' || ?
                           END,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (candidate_status, note, note, note, int(source_candidate_id)),
                )
            event_raw: dict[str, Any] = {"action": action, "note": note, "actor": "user"}
            if stop_reason:
                event_raw["stop_reason"] = stop_reason
                event_raw["stop_reason_label"] = STOP_REASON_LABELS[stop_reason]
            cursor = conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
                   VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'api_v1')""",
                (candidate_id, row["person_id"], row["job_id"], event_type, event_status, event_reason,
                 json.dumps(event_raw, ensure_ascii=False)),
            )
        learning = self._record_sourcing_signal_safely(
            candidate_id,
            learning_signal,
            actor_type="user",
            note=note or stage,
            source_type="candidate_event",
            source_id=int(cursor.lastrowid),
        )
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "action": action,
            "stage": stage,
            "stop_reason": stop_reason,
            "stop_reason_label": STOP_REASON_LABELS.get(stop_reason, ""),
            "business_event_id": cursor.lastrowid,
            "sourcing_learning": learning,
        }

    # ------------------------------------------------------------------
    # S7-1：人才流动雷达（路由层薄封装，业务在 AgentService/a_system_agent.radar_scan）
    # ------------------------------------------------------------------

    def create_radar_scan(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_radar_scan()
        raise RuntimeError("workflow service unavailable")

    def get_latest_radar_scan(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_latest_radar_scan()
        raise RuntimeError("workflow service unavailable")

    # ------------------------------------------------------------------
    # S7-2：雷达联动（路由层薄封装，业务在 AgentService）
    # ------------------------------------------------------------------

    def start_mapping_from_radar(self, company: str, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.start_mapping_from_radar(company, job_id)
        raise RuntimeError("workflow service unavailable")

    def activate_radar_company(self, company: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.activate_radar_company(company)
        raise RuntimeError("workflow service unavailable")

    # ------------------------------------------------------------------
    # S7-3：雷达周报（路由层薄封装，业务在 AgentService/a_system_agent.radar_weekly）
    # ------------------------------------------------------------------

    def create_radar_weekly_report(self, *, push_copilot: bool = True) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_radar_weekly_report(push_copilot=push_copilot)
        raise RuntimeError("workflow service unavailable")

    def get_latest_radar_weekly_report(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_latest_radar_weekly_report()
        raise RuntimeError("workflow service unavailable")
