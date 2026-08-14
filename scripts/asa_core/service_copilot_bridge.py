from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any

from .database import connect, transaction
from .intent import (
    direct_candidate_action,
    direct_candidate_update,
    intent_signature,
    parse_candidate_intent,
)
from .service_candidate_actions import (
    CANDIDATE_ACTION_CONFIRM_TEXTS,
    CANDIDATE_ACTION_LABELS,
    CANDIDATE_UPDATE_LABELS,
    STRUCTURED_CANDIDATE_ACTIONS,
    _candidate_action_already_applied,
    _candidate_action_card,
)


READ_ONLY_COPILOT_ACTIONS = {
    "open_candidate", "open_job", "open_queue", "open_analysis",
    "view_a_candidates", "compare_top_candidates",
}


NON_BUSINESS_COPILOT_ACTIONS = {
    *READ_ONLY_COPILOT_ACTIONS, "native_action", "floating_action", "open_workflow",
    "continue_sourcing", "relax_search", "generate_contact_queue",
    "generate_contact_script", "confirm_advance", "confirm_stop", "end_round",
}


def _without_workflow_source_claim(answer: Any) -> str:
    """Do not let an Agent-mode analysis imply sourcing ran without a workflow."""
    text = str(answer or "")
    # 两种语序都要拦截：“寻访已启动”与“已启动寻访/我已经开始搜索人选”。
    if re.search(
        r"(?:寻访|搜索).{0,8}(?:已启动|已开始|已经开始|已执行)"
        r"|(?:已启动|已开始|已经开始|已执行).{0,6}(?:寻访|搜索)",
        text,
    ):
        return "已完成查询和分析，尚未创建寻访工作流，因此没有启动寻访。"
    return text


_COPILOT_CORRECTION_RE = re.compile(
    r"(?:不是(?:这个意思|这样)|刚才(?:不对|错了|理解错了)|纠正(?:一下|下)?|更正(?:一下|下)?|改成|改为|调整为|去掉|删除|移除|不再要求|不用卡)",
    re.IGNORECASE,
)


def _is_copilot_correction(message: str) -> bool:
    return bool(_COPILOT_CORRECTION_RE.search(" ".join(str(message or "").split())))


def _is_sourcing_result_action_card(card: Any) -> bool:
    return isinstance(card, dict) and card.get("type") == "sourcing_result"


def _workflow_action_card(result: dict[str, Any]) -> dict[str, Any] | None:
    # 已完成的寻访工作流会由 copilot_handler 附带 sourcing_result 结果卡；
    # 直接返回该结果卡，避免被 workflow 对象卡覆盖或在 copilot_agent 路径被清空。
    if _is_sourcing_result_action_card(result.get("action_card")):
        return result.get("action_card")
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
    goal = result.get("goal") or {}
    objective = str(understanding.get("objective") or goal.get("objective") or "本次任务").strip()
    plan_items = [item for item in (result.get("plan_summary") or []) if isinstance(item, dict)]
    completed_labels = [str(item.get("label") or "").strip() for item in plan_items if item.get("status") in {"completed", "skipped"} and str(item.get("label") or "").strip()]
    current_label = str(step.get("label") or (result.get("workflow") or {}).get("current_stage") or "准备执行")
    capabilities = {str(item.get("capability_id") or "") for item in plan_items}
    if "candidate_batch_assessment" in capabilities or str(understanding.get("action") or "") == "candidate_review":
        deliverable = "优先评估名单，以及每位候选人的命中依据"
    elif "multi_channel_sourcing" in capabilities:
        deliverable = "渠道结果、完整履历获取情况和可复核候选人名单"
    else:
        deliverable = "本次任务的可核验结果"
    external_in_scope = "multi_channel_sourcing" in capabilities
    next_actions = [{"type": "open_workflow", "id": workflow_id, "label": "查看计划"}]
    if workflow_status == "planned" and plan_ref.get("plan_hash"):
        next_actions.append({
            "type": "start_workflow",
            "id": workflow_id,
            "label": "开始执行本次任务",
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
            "title": goal.get("title"),
            "plan_ref": plan_ref,
        },
        "business_summary": {
            "task": objective,
            "completed": completed_labels,
            "current": current_label,
            "deliverable": deliverable,
            "scope_note": "本次不触发外部寻访" if not external_in_scope else "外部寻访仍须单次 R3 授权",
        },
        "evidence": [
            {"label": "当前步骤", "value": current_label},
            *([{"label": "本轮范围", "value": "不触发外部寻访"}] if not external_in_scope else []),
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


def _explicit_candidate_action(message: str) -> str:
    # 直写层已迁入 asa_core.intent（R9 分层解析器），语义逐字保留。
    return direct_candidate_action(message)


def _explicit_candidate_update(message: str) -> str:
    # 直写层已迁入 asa_core.intent（R9 分层解析器），语义逐字保留。
    return direct_candidate_update(message)


class CopilotBridgeMixin:
    """Copilot 消息桥接域：消息解析 → 候选人意图确认通道、分析卡、
    Agent 模式动作卡边界、动作提案委托与动作卡回路指标。

    方法体自 service.py 逐字节迁移（P2-1），语义不变。
    """

    def copilot(self, message: str, *, session_id: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.agent_service:
            raise RuntimeError("copilot service unavailable")
        raw_context = dict(context or {})
        correction_turn = _is_copilot_correction(message)
        if correction_turn:
            raw_context["correction_turn"] = True
        revoked_snapshot = self._invalidate_copilot_pending_actions(session_id, message) if correction_turn and session_id else []
        analysis_request = self._copilot_analysis_request(message, raw_context)
        if analysis_request:
            return self._run_copilot_analysis(
                message, session_id=session_id, context=raw_context,
                catalog_id=analysis_request[0], scope=analysis_request[1],
            )
        structured_action = raw_context.get("structured_action") if isinstance(raw_context.get("structured_action"), dict) else {}
        structured_candidate_action = STRUCTURED_CANDIDATE_ACTIONS.get(str(structured_action.get("type") or ""))
        parsed_intent = (
            {
                "kind": "candidate_action", "action": structured_candidate_action,
                "tier": "confirm", "confidence": 1.0,
                "reason": "顾问点击了结构化候选人动作",
            }
            if structured_candidate_action else parse_candidate_intent(message)
        )
        action = str(parsed_intent.get("action") or "") if parsed_intent.get("kind") == "candidate_action" else ""
        candidate_id = 0
        if str(raw_context.get("type") or "").strip() == "candidate":
            try:
                candidate_id = int(raw_context.get("id") or 0)
            except (TypeError, ValueError):
                candidate_id = 0
        if not candidate_id:
            candidate_name = str((raw_context.get("candidate") or "")).strip()
            if not candidate_name:
                lookup_conn = connect(self.db_path)
                try:
                    name_rows = lookup_conn.execute(
                        "SELECT DISTINCT p.display_name FROM people p WHERE length(trim(COALESCE(p.display_name,'')))>=2 LIMIT 500"
                    ).fetchall()
                finally:
                    lookup_conn.close()
                names = {
                    str(row["display_name"] or "").strip()
                    for row in name_rows
                    if str(row["display_name"] or "").strip() in message
                }
                names = {
                    name for name in names
                    if not any(other != name and len(other) > len(name) and name in other for other in names)
                }
                if len(names) == 1:
                    candidate_name = next(iter(names))
            if candidate_name:
                lookup_conn = connect(self.db_path)
                try:
                    rows = lookup_conn.execute(
                        "SELECT jc.id FROM job_candidates jc JOIN people p ON p.id=jc.person_id WHERE p.display_name=? ORDER BY jc.updated_at DESC,jc.id DESC LIMIT 2",
                        (candidate_name,),
                    ).fetchall()
                finally:
                    lookup_conn.close()
                if len(rows) == 1:
                    candidate_id = int(rows[0]["id"])

        action_result: dict[str, Any] | None = None
        detail: dict[str, Any] = {}
        update_type = _explicit_candidate_update(message)

        # 所有候选人状态动作统一走确认层。明确短句和扩展表达都只产出
        # pending_intent（含签名 + preflight token），由确认端点执行。
        pending_intent: dict[str, Any] | None = None
        pending_blocked = ""
        pending_unresolved = False
        confirm_intent: dict[str, Any] = {}
        if update_type and candidate_id:
            detail = self.candidate(candidate_id)["candidate"]
            try:
                preflight = self.candidate_update_preflight(candidate_id, update_type)
            except ValueError as exc:
                pending_blocked = str(exc)
            else:
                pending_message = re.sub(r"\s+", " ", str(message or "")).strip()
                pending_intent = {
                    "kind": "candidate_update", "action": update_type,
                    "action_label": CANDIDATE_UPDATE_LABELS[update_type], "target_scope": "current_candidate",
                    "confidence": 1.0, "reason": "顾问要求记录候选人跟进状态",
                    "candidate": {"id": candidate_id, "name": detail["name"], "stage": detail.get("clean_stage"), "job": detail.get("job"), "client": detail.get("client")},
                    "confirm_text": f"将记录 {detail['name']} {CANDIDATE_UPDATE_LABELS[update_type]}，确认？",
                    "intent_hash": intent_signature("candidate_update", update_type, candidate_id, pending_message),
                    "preflight_token": preflight["token"], "expires_at": preflight["expires_at"], "message": pending_message,
                }
        elif parsed_intent.get("kind") == "candidate_action":
            if parsed_intent.get("tier") in {"direct", "confirm"}:
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
                        action_result = {
                            "ok": True,
                            "candidate_id": candidate_id,
                            "action": pending_action,
                            "stage": detail.get("clean_stage"),
                            "already_applied": True,
                        }
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
        if candidate_id and str(agent_context.get("type") or "") not in {"candidate", "workflow"}:
            agent_context.update({"type": "candidate", "id": candidate_id, "candidate": candidate_name})
        if confirm_intent:
            # 该消息是候选人写入指令（待确认），抑制工作流级意图路由，
            # 防止同一条消息既产生确认卡片又建立/启动工作流。
            agent_context["suppress_goal_intent"] = True
            agent_context["candidate_intent"] = confirm_intent
        result = self.agent_service.copilot(message, session_id=session_id, context=agent_context)
        if revoked_snapshot:
            card = result.get("understanding_card")
            if isinstance(card, dict):
                card["revoked_understanding"] = revoked_snapshot[:3]
                card["important_correction"] = True
            result["revoked_actions"] = revoked_snapshot[:6]
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
        elif pending_intent:
            result["pending_intent"] = pending_intent
            result["action_card"] = _candidate_action_card(pending_intent)
            result["action_cards"] = [result["action_card"]]
            result["answer"] = f"{pending_intent['confirm_text']}\n\n未确认前不会写入 ASA。"
        elif pending_blocked:
            result["answer"] = f"这条指令未写入 ASA：{pending_blocked}"
            result["write_blocked"] = True
        elif action:
            result["answer"] = "这条指令尚未写入 ASA：当前没有唯一定位到人岗关系，请先打开对应候选人后重试。"
            result["write_blocked"] = True
        elif update_type:
            result["answer"] = "这条跟进记录尚未写入 ASA：当前没有唯一定位到人岗关系，请先打开对应候选人后重试。"
            result["suggested_actions"] = []
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
                # 已存在的寻访结果卡不应被 workflow 对象卡覆盖。
                keep_existing_result_card = _is_sourcing_result_action_card(structured.get("action_card"))
                structured.update({
                    "references": result.get("references") or [],
                    "suggested_actions": result.get("suggested_actions") or [],
                    "skill_runs": result.get("skill_runs") or [],
                    "business_focus": result.get("business_focus") or {},
                    "understanding_card": result.get("understanding_card"),
                    "execution_receipt": result.get("execution_receipt"),
                    "pending_intent": result.get("pending_intent"),
                    "action_card": result.get("action_card"),
                    "action_cards": result.get("action_cards") or [],
                })
                if keep_existing_result_card:
                    structured["action_card"] = result.get("action_card") if _is_sourcing_result_action_card(result.get("action_card")) else structured["action_card"]
                    structured["action_cards"] = result.get("action_cards") if any(_is_sourcing_result_action_card(c) for c in (result.get("action_cards") or [])) else structured.get("action_cards") or []
                if workflow_card:
                    structured.update({
                        "goal": result.get("goal"), "workflow": result.get("workflow"),
                        "plan_summary": result.get("plan_summary") or [],
                        "workflow_progress": {
                            "workflow_id": workflow_card["context"]["id"],
                            "status": (result.get("workflow") or {}).get("status") or "queued",
                            "business_outcome": (result.get("workflow") or {}).get("business_outcome")
                                or (result.get("goal") or {}).get("business_outcome"),
                            "completed": (result.get("progress") or {}).get("completed") or 0,
                            "total": (result.get("progress") or {}).get("total") or len(result.get("plan_summary") or []),
                            "label": (result.get("workflow") or {}).get("current_stage") or "准备执行",
                            "pending_approvals": result.get("approvals") or [],
                        },
                    })
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

    def _invalidate_copilot_pending_actions(self, session_id: str, message: str) -> list[dict[str, Any]]:
        """撤销依赖旧约束的待执行动作，并把旧理解保留为可审计快照。"""
        if not session_id:
            return []
        revoked: list[dict[str, Any]] = []
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id,structured_json FROM agent_copilot_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC",
                (session_id,),
            ).fetchall()
            for row in rows:
                try:
                    structured = json.loads(row["structured_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(structured, dict):
                    continue
                pending = structured.get("pending_intent")
                actions = structured.get("suggested_actions")
                card = structured.get("understanding_card")
                action_card = structured.get("action_card")
                card_actions = action_card.get("next_actions") if isinstance(action_card, dict) else []
                executable_actions = isinstance(actions, list) and any(
                    isinstance(item, dict) and (
                        item.get("confirmation_required") is True
                        or str(item.get("type") or "") in {"start_workflow", "floating_action", "workflow_approval"}
                    )
                    for item in actions
                )
                executable_card = isinstance(card_actions, list) and any(
                    isinstance(item, dict) and str(item.get("type") or "") in {
                        "start_workflow", "workflow_approval", "confirm_candidate_intent",
                    }
                    for item in card_actions
                )
                executable = isinstance(pending, dict) or executable_actions or executable_card or (
                    isinstance(card, dict) and (
                        card.get("needs_clarification") is True
                        or card.get("confirmation_required") is True
                        or card.get("execution_required") is True
                    )
                )
                if not executable or structured.get("invalidated"):
                    continue
                revoked.append({
                    "message_id": row["id"],
                    "action_ids": [
                        str(item.get("action_id"))
                        for item in [*(actions or []), *(card_actions or [])]
                        if isinstance(item, dict) and item.get("action_id")
                    ],
                    "action": (pending or {}).get("action") if isinstance(pending, dict) else None,
                    "workflow_id": (
                        (action_card.get("context") or {}).get("id")
                        if isinstance(action_card, dict) and isinstance(action_card.get("context"), dict)
                        else None
                    ),
                    "target": (pending or {}).get("candidate") if isinstance(pending, dict) else (card or {}).get("target") if isinstance(card, dict) else None,
                    "reason": "用户纠正或修改条件",
                })
                if isinstance(pending, dict):
                    revoked_token = str(pending.get("preflight_token") or "")
                    if revoked_token:
                        with self._preflight_lock:
                            self._preflight_tokens.pop(revoked_token, None)
                structured["invalidated"] = True
                structured["invalidated_at"] = datetime.now().isoformat(timespec="seconds")
                structured["invalidated_by"] = str(message or "")[:240]
                if isinstance(pending, dict):
                    pending["invalidated"] = True
                    pending["invalidated_reason"] = "用户纠正或修改条件"
                    structured["pending_intent"] = pending
                if isinstance(card, dict):
                    card["invalidated"] = True
                    card["invalidated_reason"] = "用户纠正或修改条件"
                    structured["understanding_card"] = card
                if isinstance(actions, list):
                    structured["suggested_actions"] = [
                        {**item, "invalidated": True, "invalidated_reason": "用户纠正或修改条件"}
                        if isinstance(item, dict) else item for item in actions
                    ]
                if isinstance(action_card, dict):
                    action_card["invalidated"] = True
                    action_card["invalidated_reason"] = "用户纠正或修改条件"
                    if isinstance(card_actions, list):
                        action_card["next_actions"] = [
                            {**item, "invalidated": True, "invalidated_reason": "用户纠正或修改条件"}
                            if isinstance(item, dict) else item for item in card_actions
                        ]
                    structured["action_card"] = action_card
                conn.execute(
                    "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                    (json.dumps(structured, ensure_ascii=False), row["id"]),
                )
        return revoked

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
        if kind not in {"candidate_action", "candidate_update"} or (
            confirmed_action not in CANDIDATE_ACTION_LABELS and confirmed_action not in CANDIDATE_UPDATE_LABELS
        ):
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
            if kind == "candidate_update":
                with self._preflight_lock:
                    grant = self._preflight_tokens.pop(preflight_token, None)
                if not grant or grant[0] != target_id or grant[1] != confirmed_action or grant[2] <= datetime.now():
                    raise ValueError("preflight token is invalid, expired, or already used")
                return self.record_candidate_update(target_id, confirmed_action)
            return self.candidate_commit(target_id, confirmed_action, f"ASA Copilot 确认指令：{normalized[:160]}", preflight_token)

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
        action_label = (CANDIDATE_ACTION_LABELS | CANDIDATE_UPDATE_LABELS)[confirmed_action]
        answer = (
            f"已确认并同步到 ASA：{detail['name']} {action_label}，"
            f"当前阶段为\u201c{action_result.get('stage') or detail.get('clean_stage') or '已更新'}\u201d。"
        )
        execution_receipt = {
            "version": "execution_receipt_v1", "state": "已完成", "summary": answer,
            "succeeded": 1, "skipped": 0, "failed": 0, "verified": True,
            "scope": {"type": "candidate", "id": target_id}, "next_step": "查看候选人最新状态",
        }
        if session_id:
            with transaction(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id,structured_json FROM agent_copilot_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC",
                    (session_id,),
                ).fetchall()
                for row in rows:
                    try:
                        structured = json.loads(row["structured_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    pending = structured.get("pending_intent") if isinstance(structured, dict) else None
                    if isinstance(pending, dict) and secrets.compare_digest(str(pending.get("intent_hash") or ""), str(intent_hash)):
                        structured["pending_intent"] = None
                        structured["execution_receipt"] = execution_receipt
                        conn.execute(
                            "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                            (json.dumps(structured, ensure_ascii=False), row["id"]),
                        )
                        break
        return {
            "ok": True,
            "candidate_action": action_result,
            "answer": answer,
            "execution_receipt": execution_receipt,
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
                "understanding_card": result.get("understanding_card"),
                "execution_receipt": result.get("execution_receipt"),
                "pending_intent": result.get("pending_intent"),
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
