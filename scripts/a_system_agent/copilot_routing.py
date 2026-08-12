"""Copilot routing, strategy gate and main implementation (split from copilot_handler.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
import json, re, sqlite3
from typing import Any

from ._shared import (
    _dumps,
    _row,
    _is_short_ack,
    _contains_any,
    _latest_event,
)
from .privacy import sanitize_payload
from .policy import is_stopped
from .context import build_candidate_context
from .copilot_tools import generate_proactive_suggestions
from . import strategy_v2
from .native_attachments import attachment_read_requested, image_analysis_requested
from .turn_decision import build_turn_decision
from .conversation_state import (
    TERMINAL_WORKFLOW_STATUSES,
    _FACT_LABELS,
    fact_retract_requested,
    fact_scope_correction_request,
    latest_correctable_fact,
    stale_reason_text,
    undo_task_requested,
)

# Cross-module references (split from copilot_handler.py)
from .copilot_evidence import _build_fact_receipt, _build_strategy_patch, _candidate_evidence_question, _client_aliases, _confirmed_assistant_refinement, _continued_sourcing_requested, _copilot_assessment_context, _copilot_context_job_id, _copilot_job_evidence, _copilot_response_detail, _dedupe_copilot_references, _format_ambiguous_job_scope, _format_candidate_evidence_answer, _format_candidate_result_observation_answer, _format_job_budget_fact_answer, _format_non_action_fact_answer, _is_candidate_result_observation, _is_job_budget_fact_update, _jobs_relevant_to_selected_context, _new_candidate_outreach_requested, _persistable_attachment_payload, _stopped_candidate_action_requested, _strategy_revision_instruction
from .copilot_intent import _build_candidate_list_card, _build_candidate_list_composition_answer, _compact_workflow_context, _copilot_pending_plan, _copilot_plan_from_anchor, _copilot_plan_matches_selected, _interpret_copilot_message, _is_candidate_list_composition_question, _is_candidate_list_query, _is_job_requirement_message, _is_plain_query, _latest_assistant_plan_anchor, _latest_assistant_plan_confirmation, _plan_confirmation_reply, _requests_grade_filter, _salary_plan_confirmation_reply, _salary_recap_amounts, _workflow_strategy_question
from .copilot_sessions import _format_context_mismatch_answer, _format_workflow_strategy_answer


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
    return [
        str(row["name"])
        for row in rows
        if any(alias in text for alias in _client_aliases(str(row["name"] or "")))
    ]


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
        ("job_intake", ("岗位接入", "录入岗位", "接入岗位", "需求接入")),
        ("jd_calibration", ("jd校准", "JD校准", "岗位校准", "校准岗位", "硬门槛", "岗位需求", "分析岗位", "分析JD", "JD分析", "看看这个JD", "岗位要求", "JD")),
        ("job_library_update", ("更新岗位", "拆分岗位", "新建岗位", "建立岗位", "岗位库更新")),
        ("job_diagnosis", ("岗位诊断", "岗位风险", "岗位漏斗", "风险", "漏斗", "诊断", "驾驶舱", "看板")),
        ("talent_pool_search", ("人才库", "历史人才", "存量人选", "库里", "搜库", "检索人才")),
        ("search_strategy", ("寻访策略", "搜索策略", "怎么找", "搜人策略", "目标公司", "关键词")),
        ("job_publish_prepare", ("发布准备", "岗位发布准备", "准备发布", "发布草稿", "上架准备")),
        ("candidate_assessment", ("评估", "匹配", "判断", "合不合适", "适配", "推荐吗")),
        ("verification_plan", ("核验", "验证", "缺什么", "待核验", "核实", "问题清单")),
        ("communication_draft", ("草稿", "怎么联系", "沟通话术", "怎么聊", "私聊话术")),
        ("resume_export", ("导出简历", "简历导出", "结构化简历", "简历文档")),
        ("candidate_batch_assessment", ("批量评估", "批量判断", "批量匹配", "评估这一批")),
        ("candidate_pool_filter", ("过滤", "筛选", "名单", "分级", "重新过滤", "输出名单", "给名单", "把名单", "期望", "薪资上限", "只要.*万", "只要.*k", "江浙沪", "城市")),
        ("outreach_queue", ("转成触达队列", "触达队列", "排触达", "触达优先级", "P0队列", "按P0", "按P1")),
        ("pool_gap_advice", ("补池", "去哪补", "缺口", "目标公司", "补人", "还差哪些公司")),
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


def _generate_copilot_model_answer(
    self,
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the canonical answer model with read-only tools on the same turn state."""
    if not bool(self.config.get("runtime", {}).get("copilot_tools_enabled", True)):
        return self.llm.copilot(payload), [], []

    from .copilot_tools import COPILOT_TOOLS, TOOL_EXECUTORS

    max_rounds = max(1, min(int(self.config.get("runtime", {}).get("copilot_tool_rounds", 3)), 5))
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    ]
    executed_calls: set[tuple[str, str]] = set()
    tool_results: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []

    for round_num in range(max_rounds):
        response = self.llm.copilot_with_tools(payload, COPILOT_TOOLS, messages=messages)
        if not isinstance(response, dict):
            return str(response or "").strip(), tool_results, references
        calls = response.get("tool_calls") if isinstance(response.get("tool_calls"), list) else []
        if not calls:
            return str(response.get("content") or "").strip(), tool_results, references

        assistant_calls: list[dict[str, Any]] = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or f"tool_{round_num}_{index}")
            name = str(call.get("name") or "").strip()
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            assistant_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            })
        messages.append({
            "role": "assistant",
            "content": response.get("content") or None,
            "tool_calls": assistant_calls,
        })

        new_call_executed = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "").strip()
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            call_key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            executor = TOOL_EXECUTORS.get(name)
            if executor is None:
                result = {"success": False, "error": f"不允许的只读工具: {name or 'unknown'}"}
            elif call_key in executed_calls:
                result = {"success": False, "error": "本轮已返回相同查询，请直接使用已有结果作答。"}
            else:
                executed_calls.add(call_key)
                new_call_executed = True
                try:
                    result = executor(str(self.db_path), **arguments)
                except Exception as exc:
                    result = {"success": False, "error": str(exc)[:300]}
            tool_results.append({"tool": name, "args": arguments, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            })
            references.append({
                "type": "tool_result",
                "id": call_id,
                "label": name or "只读查询",
                "subtitle": "查询成功" if result.get("success") else str(result.get("error") or "查询失败")[:80],
            })

        if not new_call_executed or round_num == max_rounds - 1:
            final = self.llm.copilot_with_tools(
                payload,
                COPILOT_TOOLS,
                messages=messages,
                allow_tools=False,
            )
            return str(final.get("content") or "").strip(), tool_results, references
    return "", tool_results, references


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
    floating_compact = str(raw_context.get("display_mode") or "").strip() == "floating_compact"
    selected = self._normalize_copilot_context(raw_context)
    selected, focus_conflicts = self._copilot_context_from_focus(session_id, message, selected)
    existing_focus = self.get_copilot_focus(session_id)
    conversation_state = (
        dict(existing_focus.get("conversation_state"))
        if isinstance(existing_focus, dict) and isinstance(existing_focus.get("conversation_state"), dict)
        else {}
    )
    conversation_history = self._copilot_conversation_history(session_id)
    selected_facts = self._copilot_context_facts(selected)
    plan_reply = _plan_confirmation_reply(message)
    latest_plan_anchor = _latest_assistant_plan_anchor(self, session_id) if plan_reply else {}
    anchored_plan_ref, anchored_plan_state = (
        _copilot_plan_from_anchor(self, latest_plan_anchor, conversation_state)
        if latest_plan_anchor
        else ({}, {})
    )
    confirmation_plan_ref = (
        anchored_plan_ref
        if plan_reply
        and _copilot_plan_matches_selected(
            selected,
            selected_facts,
            anchored_plan_state,
            anchored_plan_ref,
        )
        else {}
    )
    if selected.get("type") == "workflow":
        focused_context = existing_focus.get("context") if isinstance(existing_focus, dict) and isinstance(existing_focus.get("context"), dict) else {}
        if focused_context.get("type") != "workflow" or focused_context.get("id") != selected.get("id"):
            # A persistent floating session may have been used for another job. Its chat
            # history is not evidence for a newly selected workflow.
            conversation_history = []
    last_assistant_message = next(
        (
            str(item.get("content") or "")
            for item in reversed(conversation_history)
            if item.get("role") == "assistant"
        ),
        "",
    )
    intent_understanding = _interpret_copilot_message(
        self,
        message,
        selected,
        selected_facts,
        existing_focus,
        conversation_history,
        last_assistant_message,
        confirmation_plan_ref,
    )
    # 谈薪复述卡确认：上一条 assistant 出了创建前复述卡，本轮是明确确认。
    salary_confirmation_resolved: dict[str, Any] = {}
    pending_salary_card = _latest_assistant_plan_confirmation(self, session_id)
    if pending_salary_card and _salary_plan_confirmation_reply(message):
        salary_confirmation_resolved = pending_salary_card
    # 纠错与撤销入口：先判 retract/undo-task/对象级纠错的明确措辞，
    # 再回落到既有 cancel/correct 逻辑；三类输入都绝不能触发新任务创建。
    state_facts = [
        item
        for item in (conversation_state.get("facts") or [])
        if isinstance(item, dict)
    ]
    undo_task_turn = undo_task_requested(message)
    scope_correction_turn = None if undo_task_turn else fact_scope_correction_request(message)
    fact_retract_turn = bool(not undo_task_turn and not scope_correction_turn and fact_retract_requested(message))
    correction_receipt = ""
    undo_task_info: dict[str, Any] = {}
    if undo_task_turn or scope_correction_turn or fact_retract_turn:
        intent_understanding.update({
            "speech_act": "correct",
            "action": "none",
            "target": {"type": "global", "id": None, "client": "", "label": ""},
            "constraints": [],
            "fact_updates": [],
            "action_evidence": [],
            "raw_constraint_changes": [],
            "confidence": max(float(intent_understanding.get("confidence") or 0.0), 0.9),
            "needs_clarification": False,
            "missing_fields": [],
            "clarification_question": "",
            "safe_for_action": False,
        })
    ambiguous_job_scope = any(
        item.get("type") == "ambiguous_job"
        for item in focus_conflicts
        if isinstance(item, dict)
    )
    if ambiguous_job_scope:
        # Do not let an otherwise valid budget/detail fact inherit the visible
        # page job when the same sentence explicitly names more than one job.
        intent_understanding.update({
            "speech_act": "other",
            "action": "none",
            "topic": str(intent_understanding.get("topic") or "job"),
            "target": {"type": "global", "id": None, "client": "", "label": ""},
            "fact_updates": [],
            "action_evidence": [],
            "refers_to_previous": False,
            "needs_clarification": True,
            "missing_fields": ["唯一岗位"],
            "clarification_question": "请补充唯一的岗位名称或岗位编号。",
            "safe_for_action": False,
        })
    if salary_confirmation_resolved and not ambiguous_job_scope:
        # 复述卡已确认：按原目标真正走 create_plan（由后续创建路径生成 planned 计划）。
        intent_understanding.update({
            "speech_act": "execute",
            "action": "salary",
            "topic": "salary",
            "objective": str(salary_confirmation_resolved.get("objective") or message),
            "target": dict(salary_confirmation_resolved.get("target") or {}),
            "constraints": [],
            "fact_updates": [],
            "action_evidence": [message],
            "refers_to_previous": True,
            "confidence": 0.95,
            "needs_clarification": False,
            "missing_fields": [],
            "clarification_question": "",
            "safe_for_action": True,
        })
    if (undo_task_turn or scope_correction_turn or fact_retract_turn) and not ambiguous_job_scope:
        active_context = (
            conversation_state.get("active_context")
            if isinstance(conversation_state.get("active_context"), dict)
            else {}
        )

        def _state_scope_label(scope: dict[str, Any]) -> str:
            scope_type = str(scope.get("type") or "")
            scope_id = scope.get("id")
            if scope_type == "job":
                title = str((active_context.get("job") or {}).get("title") or "").strip()
                return f"岗位「{title}」" if title else f"岗位 #{scope_id}"
            if scope_type == "candidate":
                name = str((active_context.get("candidate") or {}).get("name") or "").strip()
                return f"候选人「{name}」" if name else f"候选人 #{scope_id}"
            return "全局上下文"

        if fact_retract_turn:
            intent_understanding["fact_retract"] = True
            retract_target = latest_correctable_fact(state_facts)
            if retract_target:
                kind_label = _FACT_LABELS.get(str(retract_target.get("kind") or ""), "已确认事实")
                correction_receipt = (
                    f"结论：已撤销刚才记录的{kind_label}事实（原内容「{retract_target.get('quote')}」），不会用于后续判断。\n\n"
                    "下一步：如果内容有误，把正确内容再发我一次即可。"
                )
            else:
                correction_receipt = (
                    "结论：最近没有已记录的事实可撤销。\n\n"
                    "下一步：如果要纠正已记录的内容，直接告诉我正确信息。"
                )
        elif scope_correction_turn:
            previous_type = str(scope_correction_turn.get("previous_type") or "")
            scope_target = latest_correctable_fact(state_facts, previous_type)
            job_facts = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
            candidate_facts = (
                selected_facts.get("candidate") if isinstance(selected_facts.get("candidate"), dict) else {}
            )
            mode = str(scope_correction_turn.get("mode") or "")
            new_scope: dict[str, Any] = {}
            new_label = ""
            if mode == "job":
                job_id = job_facts.get("id") or (active_context.get("job") or {}).get("id")
                if job_id not in (None, ""):
                    new_scope = {"type": "job", "id": job_id}
                    title = str(job_facts.get("title") or job_facts.get("job") or (active_context.get("job") or {}).get("title") or "").strip()
                    new_label = f"岗位「{title}」" if title else f"岗位 #{job_id}"
            elif mode == "candidate":
                candidate_id = (
                    selected.get("id")
                    if str(selected.get("type") or "") == "candidate"
                    else candidate_facts.get("id") or (active_context.get("candidate") or {}).get("id")
                )
                if candidate_id not in (None, ""):
                    new_scope = {"type": "candidate", "id": candidate_id}
                    name = str(candidate_facts.get("name") or (active_context.get("candidate") or {}).get("name") or "").strip()
                    new_label = f"候选人「{name}」" if name else f"候选人 #{candidate_id}"
            elif mode == "named":
                named_jobs = self._mentioned_jobs_for_copilot(str(scope_correction_turn.get("name") or ""))
                if len(named_jobs) == 1:
                    named_job = named_jobs[0]
                    new_scope = {"type": "job", "id": named_job.get("id")}
                    new_label = f"岗位「{named_job.get('client') or ''} {named_job.get('job') or ''}」".replace("  ", " ")
            if scope_target is not None and new_scope.get("type") and new_scope.get("id") not in (None, ""):
                intent_understanding["fact_scope_correction"] = {
                    "previous_type": previous_type,
                    "new_scope": new_scope,
                }
                kind_label = _FACT_LABELS.get(str(scope_target.get("kind") or ""), "已确认事实")
                correction_receipt = (
                    f"结论：已把刚才记录的{kind_label}事实从{_state_scope_label(dict(scope_target.get('scope') or {}))}"
                    f"迁移到{new_label}（原内容「{scope_target.get('quote')}」），后续按新对象使用。\n\n"
                    "下一步：如果还有要纠正的内容，直接说明即可。"
                )
            elif scope_target is None:
                correction_receipt = (
                    "结论：最近没有已记录的事实可迁移。\n\n"
                    "下一步：先把事实告诉我，再说明要记到哪个对象。"
                )
            else:
                # 解析不出目标：走现有澄清路径，不瞎猜。
                intent_understanding["needs_clarification"] = True
                intent_understanding["missing_fields"] = ["要迁移到的岗位或候选人"]
                intent_understanding["clarification_question"] = "请明确这条事实要记到哪个岗位或候选人（名称或编号）。"
                correction_receipt = (
                    "结论：还不能确定这条事实要记到哪个对象，暂未迁移。\n\n"
                    "下一步：请明确说出目标岗位或候选人（名称或编号）。"
                )
        elif undo_task_turn:
            pending_state_plan = (
                conversation_state.get("pending_plan")
                if isinstance(conversation_state.get("pending_plan"), dict)
                else {}
            )
            undo_workflow_id = str(pending_state_plan.get("workflow_id") or "")
            undo_objective = str(pending_state_plan.get("objective") or "")
            if not undo_workflow_id and isinstance(existing_focus, dict):
                focus_pending = (
                    existing_focus.get("pending_workflow")
                    if isinstance(existing_focus.get("pending_workflow"), dict)
                    else {}
                )
                undo_workflow_id = str(focus_pending.get("workflow_id") or "")
                undo_objective = undo_objective or str(focus_pending.get("objective") or "")
            undo_status = ""
            if undo_workflow_id:
                try:
                    undo_state = self.get_workflow(undo_workflow_id)
                    undo_status = str((undo_state.get("workflow") or {}).get("status") or "")
                    undo_goal = undo_state.get("goal") if isinstance(undo_state.get("goal"), dict) else {}
                    undo_objective = undo_objective or str(undo_goal.get("objective") or undo_goal.get("title") or "")
                except (ValueError, sqlite3.Error):
                    undo_workflow_id = ""
            undo_task_info = {
                "workflow_id": undo_workflow_id,
                "status": undo_status,
                "objective": undo_objective,
            }
    understood_target = intent_understanding.get("target") if isinstance(intent_understanding.get("target"), dict) else {}
    if (
        understood_target.get("type") == "job"
        and understood_target.get("id")
        and selected.get("type") not in {"candidate", "workflow"}
    ):
        selected = {
            "type": "job", "id": int(understood_target["id"]),
            "page": "positions", "filters": {},
        }
        selected_facts = self._copilot_context_facts(selected)
        focus_conflicts = []
    pending_plan_ref, pending_plan_state = _copilot_pending_plan(self, selected, existing_focus)
    available_pending_plan_ref = dict(pending_plan_ref)
    if plan_reply:
        # Short confirmations use only the immediately presented plan. An old
        # focus plan remains available for inspection, but is not an implicit
        # authorization target.
        if confirmation_plan_ref:
            pending_plan_ref = dict(confirmation_plan_ref)
            pending_plan_state = dict(anchored_plan_state)
        else:
            pending_plan_ref = {}
            pending_plan_state = {}
    pending_goal_context = (
        (pending_plan_state.get("goal") or {}).get("context")
        if pending_plan_state else {}
    )
    previous_constraints = (
        pending_goal_context.get("constraint_ledger")
        or pending_goal_context.get("locked_constraints")
        or ((existing_focus or {}).get("constraint_ledger") if isinstance(existing_focus, dict) else [])
        or ((existing_focus or {}).get("constraints") if isinstance(existing_focus, dict) else [])
    )
    turn_decision = build_turn_decision(
        intent_understanding,
        message=message,
        previous_constraints=previous_constraints,
        pending_plan_ref=pending_plan_ref,
        raw_constraint_changes=intent_understanding.get("raw_constraint_changes"),
    )
    intent_understanding["safe_for_action"] = bool(turn_decision.get("safe_for_action"))
    intent_understanding["constraint_changes"] = list(turn_decision.get("constraint_changes") or [])
    intent_understanding["effective_constraints"] = list(turn_decision.get("effective_constraints") or [])
    semantic_action = str(intent_understanding.get("action") or "none")
    semantic_speech_act = str(intent_understanding.get("speech_act") or "other")
    # 规则优先：明确指令词（记住/别忘了等）覆盖 LLM 意图理解，避免被预算事实等分支抢先。
    if semantic_action != "memory_capture" and self._copilot_action_kind(message) == "memory_capture":
        semantic_action = "memory_capture"
    workflow_outcome_question = bool(
        "寻访" in message
        and re.search(r"(?:什么结果|结果如何|结果怎样|结果怎么样|进展如何|进展怎么样|情况如何|情况怎么样)", message)
    )
    workflow_strategy_question = _workflow_strategy_question(message, selected)
    semantic_constraints = [
        str(item.get("quote") or "").strip()
        for item in (turn_decision.get("effective_constraints") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    pending_scope_request = ""
    awaiting_unique_job_scope = bool(
        last_assistant_message.strip() == "你要为哪个岗位补充并触达新候选人？"
        # 多岗位歧义守卫的追问同样在等用户补充唯一岗位，澄清后恢复原动作。
        or "请补充唯一的岗位名称或岗位编号" in last_assistant_message
    )
    if (
        awaiting_unique_job_scope
        and selected.get("type") == "job"
        and selected.get("id")
    ):
        pending_scope_request = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(conversation_history)
                if item.get("role") == "user"
                and self._copilot_action_kind(str(item.get("content") or ""))
                in {"new_candidate_outreach", "candidate_sourcing"}
            ),
            "",
        )
    scope_clarification_resolved = bool(pending_scope_request)
    explicit_sourcing_confirmation = bool(
        semantic_action == "candidate_sourcing"
        and turn_decision.get("effect") == "start_plan"
        and intent_understanding.get("safe_for_action")
    )
    short_sourcing_confirmation = explicit_sourcing_confirmation and _is_short_ack(message)
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
    sourcing_focus = bool(pending_plan_ref and semantic_action == "candidate_sourcing")
    auto_start_sourcing = (
        sourcing_focus and explicit_sourcing_confirmation and not workflow_outcome_question
    )
    pending_sourcing_workflow: dict[str, Any] | None = None
    sourcing_revision_instruction = ""
    goal_request = message
    if salary_confirmation_resolved:
        # 复述卡确认后按原谈薪指令创建计划，而不是把“确认创建”当目标。
        goal_request = str(salary_confirmation_resolved.get("objective") or message)
    if auto_start_sourcing:
        pending_workflow = (
            existing_focus.get("pending_workflow")
            if isinstance(existing_focus, dict) and isinstance(existing_focus.get("pending_workflow"), dict)
            else {}
        )
        candidate_context = (pending_plan_state.get("goal") or {}).get("context") or {}
        same_target = bool(
            candidate_context.get("type") == "job"
            and candidate_context.get("id")
            and (
                selected.get("type") not in {"job", "candidate"}
                or str(candidate_context.get("id")) == str(selected.get("id"))
            )
        )
        if (pending_plan_state.get("workflow") or {}).get("status") == "planned" and same_target:
            pending_sourcing_workflow = pending_plan_state
            selected = {
                "type": "job",
                "id": int(candidate_context["id"]),
                "page": "positions",
                "filters": {},
            }
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
        pending_source_message = str(pending_workflow.get("source_message") or "").strip()
        refinements = [
            str(item.get("content") or "").strip()
            for item in conversation_history
            if item.get("role") == "user"
            and str(item.get("content") or "").strip() != pending_source_message
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
        if semantic_constraints:
            goal_request += f"。顾问原话约束：{'；'.join(dict.fromkeys(semantic_constraints))}"
        goal_request += "。确认执行多渠道寻访"
    elif scope_clarification_resolved:
        goal_request = pending_scope_request
    mentioned_clients = self._mentioned_client_names(message)
    primary_client = mentioned_clients[0] if mentioned_clients else str((existing_focus or {}).get("client") or "该客户")
    forced_answer = None
    fact_receipt: dict[str, Any] = {}
    workflow_cancelled = False
    started_new_plan = False
    cancelled_answer_override = ""
    salary_recap_pending: dict[str, Any] = {}
    goal_workflow = None
    if forced_answer is None and semantic_action == "memory_capture":
        # “记住 X/别忘了 X/记一下 X”：直接写入记忆库，不进入工作流路径。
        try:
            memory_content = re.sub(
                r"^.*?(?:记住这个|帮我记住|别忘了|记一下|记住)\s*[：:，,。]?\s*",
                "",
                " ".join(str(message or "").split()),
            ).strip()
            if not memory_content:
                forced_answer = "要记住什么？"
            else:
                scope_type, scope_id = "global", None
                if selected.get("type") == "candidate" and selected.get("id"):
                    scope_type, scope_id = "candidate", selected["id"]
                elif selected.get("type") == "job" and selected.get("id"):
                    scope_type, scope_id = "job", selected["id"]
                memory = self.store_memory(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    memory_type="fact",
                    content=memory_content,
                    source_type="copilot",
                    confidence=1.0,
                )
                forced_answer = f"已记住：{memory_content}（记忆ID {memory.get('memory_id')}）"
        except Exception as exc:  # 记忆写入失败不能拖垮 Copilot 回复
            forced_answer = f"记忆写入失败：{exc}"
    if correction_receipt:
        forced_answer = correction_receipt
    if undo_task_info and forced_answer is None:
        undo_workflow_id = str(undo_task_info.get("workflow_id") or "")
        undo_status = str(undo_task_info.get("status") or "")
        undo_objective = str(undo_task_info.get("objective") or "").strip()
        if undo_workflow_id and undo_status == "planned":
            try:
                goal_workflow = self.cancel_workflow(undo_workflow_id, message)
                workflow_cancelled = True
                task_label = f"「{undo_objective}」" if undo_objective else ""
                cancelled_answer_override = (
                    f"结论：已取消刚才创建的{task_label}任务，已记录的岗位/候选人事实保留。\n\n"
                    "下一步：如需重新发起，直接再说一次目标即可。"
                )
            except ValueError as exc:
                forced_answer = f"结论：未能撤销刚才创建的任务。\n\n下一步：{str(exc)[:180]}。"
        elif undo_workflow_id and undo_status and undo_status not in TERMINAL_WORKFLOW_STATUSES:
            forced_answer = (
                "结论：刚才创建的任务已开始执行，不能直接撤销。\n\n"
                "下一步：如需停止，只能走停止流程（在计划卡上暂停/停止，或明确说“暂停该任务”）。"
            )
        else:
            forced_answer = (
                "结论：本会话没有刚创建且尚未开始的任务可撤销。\n\n"
                "下一步：如果要取消待确认计划，直接说“取消计划”。"
            )
    if plan_reply and not confirmation_plan_ref and not salary_confirmation_resolved:
        stale_reason = ""
        if available_pending_plan_ref.get("workflow_id"):
            pending_state_plan = (
                conversation_state.get("pending_plan")
                if isinstance(conversation_state.get("pending_plan"), dict)
                else {}
            )
            if str(pending_state_plan.get("workflow_id") or "") == str(available_pending_plan_ref.get("workflow_id") or ""):
                stale_reason = str(pending_state_plan.get("stale_reason") or "")
        if stale_reason:
            forced_answer = (
                f"结论：待确认计划基于旧信息（{stale_reason_text(stale_reason)}），这句短确认不会启动它，当前没有启动任何任务。\n\n"
                "下一步：原计划仍保持“尚未开始”；请确认是否按新信息继续——回复“按新信息重新生成计划”我会基于最新事实重建计划，"
                "或明确说出要启动的岗位/候选人任务。"
            )
        elif available_pending_plan_ref.get("workflow_id"):
            forced_answer = (
                "结论：这句短确认没有绑定到上一条刚展示的计划，当前没有启动任何任务。\n\n"
                "下一步：原计划仍保持“尚未开始”；请重新打开该计划后，用计划卡上的确认动作，"
                "或明确说出要启动的岗位/候选人任务。"
            )
        else:
            forced_answer = (
                "结论：当前没有上一条刚展示且可确认的计划，没有启动任何任务。\n\n"
                "下一步：请明确说明要执行的岗位、候选人或具体动作。"
            )
    if re.search(r"(?:性子|性质)结构", message) and not any(token in message for token in ("公司性质", "组织结构", "薪资结构")):
        forced_answer = (
            f"你是想问{primary_client}的公司性质/组织结构，还是薪资结构？\n\n"
            "请确认一个方向，我再按对应证据回答。"
        )
    goal_patterns = (
        r"(?:补充|补池|寻访|再寻访|继续寻访|找|搜索|搜|再找|继续找)\s*(?:一些|些|若干|一批|一轮|新一批)?\s*\d*\s*(?:位|个|名|人)?\s*(?:合适|匹配|合适的)?(?:人选|候选人)",
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
    suppress_goal_intent = (
        bool(raw_context.get("suppress_goal_intent"))
        or workflow_outcome_question
        or workflow_strategy_question
    )
    context_conflicts: list[dict[str, Any]] = []
    if forced_answer is None and not suppress_goal_intent:
        context_mismatch_answer = _format_context_mismatch_answer(
            focus_conflicts,
            floating_compact=floating_compact,
        )
        if context_mismatch_answer:
            if _is_plain_query(message):
                # 查询类放行给 LLM：歧义冲突作为上下文注入，LLM 自然回答
                # （如“长越科技现在有几个在招岗位”→ 列出多岗位，而不是反问“唯一确定哪个”）。
                context_conflicts = [
                    item
                    for item in focus_conflicts
                    if item.get("type") in {"ambiguous_job", "ambiguous_client", "context_client_mismatch"}
                ]
                forced_answer = None
            else:
                forced_answer = context_mismatch_answer
    if forced_answer is None and _is_job_budget_fact_update(message):
        forced_answer = _format_job_budget_fact_answer(message, selected_facts)
        fact_receipt = _build_fact_receipt(message, intent_understanding, selected_facts, conversation_state)
    if forced_answer is None and _is_candidate_result_observation(message, intent_understanding):
        forced_answer = _format_candidate_result_observation_answer(
            message,
            selected_facts,
            existing_focus,
            floating_compact=floating_compact,
        )
        fact_receipt = _build_fact_receipt(message, intent_understanding, selected_facts, conversation_state)
    rule_evidence: str = ""
    if forced_answer is None and turn_decision.get("effect") == "answer":
        fact_answer = _format_non_action_fact_answer(
            message,
            intent_understanding,
            selected_facts,
        )
        if fact_answer:
            # 事实/观察陈述：规则先解析出“记录语义”作为证据，回答交给 LLM
            # 自然确认并给出下一步建议（模板仅作 LLM 空回答的兜底）。
            rule_evidence = fact_answer
            fact_receipt = _build_fact_receipt(message, intent_understanding, selected_facts, conversation_state)
    strategy_revision: dict[str, Any] | None = None
    strategy_revision_requested = bool(
        not suppress_goal_intent
        and turn_decision.get("effect") == "revise_plan"
        and intent_understanding.get("safe_for_action")
    )
    if strategy_revision_requested:
        source_workflow_id = str(pending_plan_ref.get("workflow_id") or "")
        revision_error = "" if source_workflow_id else "当前没有唯一待修订计划"
        change_parts = []
        for change in turn_decision.get("constraint_changes") or []:
            operation = str(change.get("operation") or "")
            if operation == "replace":
                change_parts.append(f"将“{change.get('previous_quote')}”替换为“{change.get('quote')}”")
            elif operation == "remove":
                change_parts.append(f"删除“{change.get('previous_quote')}”")
            elif operation == "add":
                change_parts.append(f"增加“{change.get('quote')}”")
        revision_instruction = "；".join(change_parts) or _strategy_revision_instruction(message, conversation_history)
        confirmed_assistant_detail = _confirmed_assistant_refinement(message, last_assistant_message)
        if confirmed_assistant_detail and confirmed_assistant_detail not in revision_instruction:
            revision_instruction += f"；用户本轮确认的上一轮细化：{confirmed_assistant_detail}"
        if revision_error:
            forced_answer = f"结论：尚未生成策略变更确认。\n\n下一步：{revision_error}"
        elif not revision_instruction:
            forced_answer = (
                "结论：已定位待修订的寻访工作流，但缺少明确修改条件。\n\n"
                "下一步：请说明要增加、删除或调整的经验、公司池、关键词或排除项。"
            )
        else:
            try:
                goal_workflow = self.revise_workflow(
                    source_workflow_id,
                    revision_instruction,
                    effective_constraints=list(turn_decision.get("effective_constraints") or []),
                    constraint_changes=list(turn_decision.get("constraint_changes") or []),
                    turn_decision=turn_decision,
                )
                strategy_revision = {
                    "source_workflow_id": source_workflow_id,
                    "revised_workflow_id": goal_workflow["workflow"]["workflow_id"],
                    "constraint_changes": list(turn_decision.get("constraint_changes") or []),
                }
                selected = self._normalize_copilot_context(goal_workflow["goal"]["context"])
                focus_conflicts = []
            except ValueError as exc:
                forced_answer = f"结论：未生成修订版。\n\n下一步：{str(exc)[:180]}。"
    if (
        not suppress_goal_intent
        and turn_decision.get("effect") == "cancel_plan"
        and intent_understanding.get("safe_for_action")
        and goal_workflow is None
        and forced_answer is None
    ):
        try:
            goal_workflow = self.cancel_workflow(str(pending_plan_ref["workflow_id"]), message)
            workflow_cancelled = True
            forced_answer = "结论：当前计划已取消，后续步骤不会继续执行。"
        except ValueError as exc:
            forced_answer = f"结论：未能取消当前计划。\n\n下一步：{str(exc)[:180]}。"
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
    semantic_goal_intent = bool(
        semantic_action in {
            "candidate_sourcing", "candidate_outreach", "job_publish", "job_split",
            "job_archive", "candidate_review", "recommendation", "salary",
        }
        and semantic_speech_act in {"propose", "execute", "confirm"}
        and intent_understanding.get("safe_for_action")
    )
    action_context_prompts = {
        "candidate_outreach": ({"candidate", "queue"}, "请先选择具体候选人，或明确要处理的待联系队列。"),
        "candidate_review": ({"job", "candidate", "queue"}, "请先选择要核验的岗位、候选人或候选队列。"),
        "recommendation": ({"candidate"}, "请先选择要生成报告或推荐给客户的具体候选人。"),
        "salary": ({"candidate"}, "请先选择要处理谈薪的具体候选人。"),
    }
    # 查询型名单请求直答：顾问要“名单/筛出/列表”时直接返回候选池，不建
    # 等待确认的执行计划（2026-08-10 长越机械人选名单卡在 create_plan）。
    # 必须放在 action_context_rule 之前，否则“请先选择要核验的岗位”会先抢答，
    # 名单拦截永远执行不到（kimi review #5）。
    candidate_list_answer = ""
    candidate_list_card: dict[str, Any] = {}
    if (
        forced_answer is None
        and not suppress_goal_intent
        and _is_candidate_list_query(message)
    ):
        list_job_id = 0
        # 消息里明确提到的唯一岗位优先；没有明确岗位时才回到当前人选/岗位焦点。
        mentioned = _jobs_relevant_to_selected_context(
            self._mentioned_jobs_for_copilot(message),
            selected,
            selected_facts,
            message,
        )
        if len(mentioned) == 1:
            list_job_id = int(mentioned[0]["id"])
        if not list_job_id:
            list_job_id = _copilot_context_job_id(selected, selected_facts) or 0
        if not list_job_id and isinstance(focus_context, dict) and focus_context.get("type") == "job" and focus_context.get("id"):
            list_job_id = int(focus_context["id"])
        if not list_job_id and mentioned_clients:
            client_jobs = self._mentioned_jobs_for_copilot(mentioned_clients[0], limit=10)
            if len(client_jobs) == 1:
                list_job_id = int(client_jobs[0]["id"])
            elif len(client_jobs) > 1:
                forced_answer = _format_ambiguous_job_scope(mentioned_clients[0], client_jobs)
        if list_job_id:
            # 分级过滤升级：消息含“过滤/分级/按证据筛选”等明确分级意图时，
            # 走 candidate_pool_filter 输出 A/B/C 分级名单（含禁挖排除），
            # 否则维持普通候选名单直答。
            if _requests_grade_filter(message):
                try:
                    import sqlite3 as _sqlite3
                    _conn = _sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                    _conn.row_factory = _sqlite3.Row
                    try:
                        _jrow = _conn.execute("SELECT c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?", (list_job_id,)).fetchone()
                    finally:
                        _conn.close()
                    _client_name = str(_jrow["client"]) if _jrow is not None else ""
                    from .candidate_pool_filter import filter_job_candidates, format_grade_list
                    _filter_result = filter_job_candidates(self.db_path, list_job_id, client=_client_name)
                    candidate_list_answer = format_grade_list(_filter_result)
                    # 分级名单同样生成结构化 action_card（前端渲染可点击名单弹窗）
                    _grade_groups: list[dict[str, Any]] = []
                    _grade_order = ("A-核心", "A-强", "B-中", "C-弱", "D-无证据", "D-无画像", "X-排除", "禁挖")
                    for _g in _grade_order:
                        _items = [c for c in (_filter_result.get("candidates") or []) if c.get("grade") == _g]
                        if _items:
                            _grade_groups.append({
                                "key": _g,
                                "label": _g,
                                "priority": _g.startswith("A"),
                                "candidates": [
                                    {
                                        "id": int(c.get("id") or 0),
                                        "name": c.get("name") or "",
                                        "company": c.get("company") or "",
                                        "title": c.get("title") or "",
                                        "stage": c.get("stage") or "",
                                        "flow_bucket": c.get("stage") or "",
                                    }
                                    for c in _items[:200]
                                ],
                            })
                    candidate_list_card = {
                        "type": "candidate_list",
                        "title": f"{_client_name}｜机械高级工程师（岗位 {list_job_id}）分级过滤名单",
                        "context": {"type": "job", "id": list_job_id},
                        "summary": {
                            "total": _filter_result.get("total") or 0,
                            "active": len([c for c in (_filter_result.get("candidates") or []) if c.get("grade", "").startswith(("A", "B", "C"))]),
                            "stopped": len([c for c in (_filter_result.get("candidates") or []) if c.get("grade") in ("X-排除", "禁挖", "D-无证据", "D-无画像")]),
                        },
                        "groups": _grade_groups,
                    }
                    if candidate_list_answer:
                        forced_answer = candidate_list_answer
                except Exception as _exc:
                    # 分级过滤失败时回落到普通名单，不让用户空手
                    import logging
                    logging.getLogger("copilot.grade_filter").exception("candidate_pool_filter failed for job %s: %s", list_job_id, _exc)
                    candidate_list_answer, candidate_list_card = _build_candidate_list_card(self.db_path, list_job_id, message)
                    if candidate_list_answer:
                        forced_answer = candidate_list_answer
            else:
                candidate_list_answer, candidate_list_card = _build_candidate_list_card(self.db_path, list_job_id, message)
                if candidate_list_answer:
                    forced_answer = candidate_list_answer
    # 名单构成质疑直答：用户问“怎么都是做光刻机的”时给出构成分析，
    # 而不是再输出一遍名单（2026-08-11 copilot_ad7e7086917d 答非所问修复）。
    if forced_answer is None and not suppress_goal_intent and _is_candidate_list_composition_question(message):
        comp_job_id = 0
        mentioned = _jobs_relevant_to_selected_context(
            self._mentioned_jobs_for_copilot(message),
            selected,
            selected_facts,
            message,
        )
        if len(mentioned) == 1:
            comp_job_id = int(mentioned[0]["id"])
        if not comp_job_id:
            comp_job_id = _copilot_context_job_id(selected, selected_facts) or 0
        if not comp_job_id and isinstance(focus_context, dict) and focus_context.get("type") == "job" and focus_context.get("id"):
            comp_job_id = int(focus_context["id"])
        if not comp_job_id and mentioned_clients:
            client_jobs = self._mentioned_jobs_for_copilot(mentioned_clients[0], limit=10)
            if len(client_jobs) == 1:
                comp_job_id = int(client_jobs[0]["id"])
            elif len(client_jobs) > 1:
                forced_answer = _format_ambiguous_job_scope(mentioned_clients[0], client_jobs)
        if comp_job_id:
            composition_answer = _build_candidate_list_composition_answer(self.db_path, comp_job_id, message)
            if composition_answer:
                forced_answer = composition_answer
    action_context_rule = action_context_prompts.get(semantic_action)
    if (
        forced_answer is None
        and semantic_goal_intent
        and turn_decision.get("effect") == "create_plan"
        and action_context_rule
        and selected.get("type") not in action_context_rule[0]
        and not candidate_list_answer
    ):
        # 语义动作需要岗位/候选人上下文但 selected 缺失时，先从会话焦点/历史证据
        # 补全目标岗位，避免有明确主线的会话被"请先选择"卡死（2026-08-07 郭杨评估指令被吞）。
        inferred_target: dict[str, Any] | None = None
        focus_snapshot = focus_context if isinstance(focus_context, dict) else {}
        focus_context_candidate = focus_snapshot if focus_snapshot.get("type") in {"job", "candidate"} and focus_snapshot.get("id") else {}
        if focus_context_candidate and float((existing_focus or {}).get("confidence") or 0) >= 0.7:
            inferred_target = {"type": str(focus_context_candidate["type"]), "id": int(focus_context_candidate["id"])}
        else:
            current_jobs = _jobs_relevant_to_selected_context(
                self._mentioned_jobs_for_copilot(message),
                selected,
                selected_facts,
                message,
            )
            if len(current_jobs) == 1:
                inferred_target = {"type": "job", "id": int(current_jobs[0]["id"])}
            evidence = self._copilot_session_business_evidence(session_id)
            evidence_jobs = list(evidence.get("jobs") or [])
            if inferred_target:
                pass
            elif len(evidence_jobs) == 1:
                inferred_target = {"type": "job", "id": int(evidence_jobs[0]["id"])}
            elif len(evidence_jobs) > 1:
                # 多岗位时取最近一条 assistant 引用/用户 context 的岗位（按消息倒序优先）。
                evidence_job_ids = {str(item.get("id")) for item in evidence_jobs}
                recent = conversation_history[-6:]
                for item in reversed(recent):
                    mentioned = self._mentioned_jobs_for_copilot(str(item.get("content") or ""))
                    mentioned = [item for item in mentioned if str(item.get("id")) in evidence_job_ids]
                    if len(mentioned) == 1:
                        inferred_target = {"type": "job", "id": int(mentioned[0]["id"])}
                        break
        if inferred_target:
            target_type = str(inferred_target["type"])
            selected = {
                "type": target_type, "id": int(inferred_target["id"]),
                "page": "positions" if target_type == "job" else "candidates", "filters": {},
            }
            selected_facts = self._copilot_context_facts(selected)
            focus_conflicts = []
        else:
            forced_answer = action_context_rule[1]
    if (
        forced_answer is None
        and semantic_action != "none"
        and intent_understanding.get("needs_clarification")
        and not suppress_goal_intent
    ):
        missing_text = "、".join(intent_understanding.get("missing_fields") or [])
        forced_answer = str(intent_understanding.get("clarification_question") or "").strip() or (
            f"我还缺少{missing_text}，确认后才能继续。" if missing_text else "我还不能唯一确定你的对象或动作，请再确认一句。"
        )
    stopped_candidate_action_blocked = False
    if selected.get("type") == "candidate" and selected.get("id"):
        try:
            stopped_candidate_action_blocked = is_stopped(
                build_candidate_context(self.db_path, int(selected["id"]))
            ) and _stopped_candidate_action_requested(
                message,
                intent_understanding,
                turn_decision,
            )
        except (sqlite3.Error, TypeError, ValueError):
            stopped_candidate_action_blocked = False
    start_pending_plan = bool(
        turn_decision.get("effect") == "start_plan"
        and pending_plan_state
        and intent_understanding.get("safe_for_action")
        and not workflow_outcome_question
        and not stopped_candidate_action_blocked
    )
    if start_pending_plan and forced_answer is None and not suppress_goal_intent and goal_workflow is None:
        try:
            goal_workflow = self.start_workflow(
                str(pending_plan_ref["workflow_id"]),
                expected_plan_version=int(pending_plan_ref["version"]),
                expected_plan_hash=str(pending_plan_ref["plan_hash"]),
            )
            selected = self._normalize_copilot_context(goal_workflow["goal"]["context"])
            focus_conflicts = []
        except ValueError as exc:
            forced_answer = f"结论：未启动已确认计划。\n\n下一步：{str(exc)[:180]}。"
    create_plan_requested = bool(
        turn_decision.get("effect") == "create_plan"
        and intent_understanding.get("safe_for_action")
    )
    if (
        create_plan_requested
        and semantic_action == "salary"
        and not salary_confirmation_resolved
        and not suppress_goal_intent
        and not stopped_candidate_action_blocked
        and goal_workflow is None
        and forced_answer is None
    ):
        # 高风险动作前置复述确认（谈薪）：创建前先出复述卡，明确确认后才真正
        # create_plan；寻访类维持现状不拦截。卡片写入 assistant structured_json，
        # 由 _latest_assistant_plan_confirmation 在下一轮锚定读取。
        recap_candidate = selected_facts.get("candidate") if isinstance(selected_facts.get("candidate"), dict) else {}
        recap_job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
        recap_label = str(recap_candidate.get("name") or "").strip() or " / ".join(
            part
            for part in (
                str(selected_facts.get("client") or recap_job.get("client") or "").strip(),
                str(recap_job.get("title") or recap_job.get("job") or "").strip(),
            )
            if part
        ) or "当前对象"
        current_amount, expected_amount = _salary_recap_amounts(state_facts)
        amounts_note = "来自已记录事实" if (current_amount or expected_amount) else "暂未提供"
        forced_answer = (
            f"结论：我理解你要对「{recap_label}」发起谈薪。"
            f"当前薪资 {current_amount or '未提供'}，期望 {expected_amount or '未提供'}（{amounts_note}）。"
            "确认后我将创建谈薪计划，回复“确认创建”。\n\n"
            "下一步：回复“确认创建”后我会生成待确认计划；对象或金额不对时直接纠正。"
        )
        salary_recap_pending = {
            "kind": "salary_plan",
            "action": "salary",
            "objective": goal_request,
            "target": {"type": str(selected.get("type") or "global"), "id": selected.get("id")},
            "source_message": message,
        }
    # 触达队列/排优先级属于内部整理动作（生成 P0/P1/P2 提案），
    # 不应进入 create_goal 建立对外工作流；改走 outreach_queue skill 路由。
    internal_queue_requested = any(
        token in message
        for token in ("转成触达队列", "触达队列", "排触达", "触达优先级", "P0队列", "P1队列", "按P0", "按P1", "按P2")
    )
    if goal_workflow is None and forced_answer is None and not suppress_goal_intent and (
        create_plan_requested or scope_clarification_resolved or strategy_gate_force_goal
    ) and not stopped_candidate_action_blocked and not internal_queue_requested:
        ground_base = selected
        if strategy_gate_force_goal and pending_clarification.get("job_id"):
            ground_base = {"type": "job", "id": int(pending_clarification["job_id"]), "page": "positions", "filters": {}}
        goal_context, _, grounding_error = self._ground_copilot_goal(goal_request, ground_base, session_id)
        if grounding_error:
            forced_answer = grounding_error
        else:
            if semantic_action == "candidate_sourcing" and not any(
                token in goal_request for token in ("补池", "寻访", "找人", "候选人", "人选", "搜索", "搜人")
            ):
                goal_request = f"为当前岗位补充候选人。顾问原话：{message}"
            if semantic_constraints:
                locked_text = "；".join(dict.fromkeys(semantic_constraints))
                if locked_text and locked_text not in goal_request:
                    goal_request += f"。顾问原话约束：{locked_text}"
            goal_context["intent_understanding"] = intent_understanding
            goal_context["turn_decision"] = turn_decision
            goal_context["constraint_ledger"] = list(turn_decision.get("effective_constraints") or [])
            goal_context["locked_constraints"] = list(dict.fromkeys(semantic_constraints))
            continued_sourcing = _continued_sourcing_requested(goal_request)
            if _new_candidate_outreach_requested(goal_request) or continued_sourcing:
                grounding = goal_context.get("goal_grounding") if isinstance(goal_context.get("goal_grounding"), dict) else {}
                client = str(grounding.get("client") or "该客户")
                job = str(grounding.get("job") or "当前岗位")
                # This is deliberately a sourcing plan. It may prepare a new batch, but
                # the R3 multi-channel step still prevents any external message from sending.
                grounded_goal = (
                    f"为{client}{job}补充并准备触达新候选人"
                    if _new_candidate_outreach_requested(goal_request)
                    else f"为{client}{job}继续补充候选人"
                )
                if goal_request not in grounded_goal:
                    grounded_goal += f"。顾问原始目标：{goal_request}"
                goal_request = grounded_goal
            strategy_gate = (
                {"action": "proceed"}
                if (
                    strategy_gate_clarification
                    or _new_candidate_outreach_requested(message)
                    or scope_clarification_resolved
                    or (
                        continued_sourcing
                        and isinstance(existing_focus, dict)
                        and str(existing_focus.get("action") or "") in {"candidate_sourcing", "strategy_revision"}
                    )
                )
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
                    if (turn_decision.get("authorization") or {}).get("mode") == "explicit_execute":
                        created_ref = dict(goal_workflow.get("plan_ref") or {})
                        goal_workflow = self.start_workflow(
                            goal_workflow["workflow"]["workflow_id"],
                            expected_plan_version=int(created_ref["version"]),
                            expected_plan_hash=str(created_ref["plan_hash"]),
                        )
                        started_new_plan = True
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
    selected_payload["intent_understanding"] = intent_understanding
    selected_payload["turn_decision"] = turn_decision
    current_workflow_context: dict[str, Any] = {}
    if goal_workflow:
        workflow = goal_workflow.get("workflow") or {}
        goal = goal_workflow.get("goal") or {}
        goal_context = goal.get("context") if isinstance(goal.get("context"), dict) else {}
        result_plan_ref = dict(goal_workflow.get("plan_ref") or {})
        selected_payload["workflow_intent"] = {
            "workflow_id": str(workflow.get("workflow_id") or ""),
            "status": str(workflow.get("status") or ""),
            "version": result_plan_ref.get("version"),
            "plan_hash": result_plan_ref.get("plan_hash"),
            "action": semantic_action,
            "objective": str(goal.get("objective") or ""),
            "locked_constraints": list(goal_context.get("locked_constraints") or []),
            "constraint_ledger": list(goal_context.get("constraint_ledger") or []),
            "source_message": message,
        }
    if selected.get("type") == "workflow" and selected_facts:
        workflow = dict(selected_facts.get("workflow") or {})
        job = dict(selected_facts.get("job") or {})
        selected_payload.update({
            "workflow": workflow,
            "client": str(selected_facts.get("client") or ""),
            "job": str(job.get("title") or ""),
            "job_id": job.get("id"),
            "workflow_context": workflow.get("context") or {},
            "business_focus": {
                "context": dict(selected_facts.get("context") or selected),
                "client": str(selected_facts.get("client") or ""),
                "job": job,
                "candidate": {},
                "confidence": 1.0,
            },
        })
        try:
            current_workflow_context = _compact_workflow_context(self.get_workflow(str(selected.get("id") or "")))
        except (sqlite3.Error, ValueError):
            current_workflow_context = {}
        if current_workflow_context:
            selected_payload["workflow_detail"] = current_workflow_context
            if workflow_strategy_question and forced_answer is None:
                forced_answer = _format_workflow_strategy_answer(
                    current_workflow_context,
                    expanded=_copilot_response_detail(message) == "expanded",
                )
    elif existing_focus:
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
                "assessment": _copilot_assessment_context(assessment),
            }
        )
        if (
            assessment
            and not is_stopped(candidate_context)
            and forced_answer is None
            and _candidate_evidence_question(message)
        ):
            # 主 Agent 对话也必须使用可核验的结构化证据，不能让模型把详细追问
            # 压缩成一句泛化结论；模型仍可在非证据型问题中自由回答。
            forced_answer = _format_candidate_evidence_answer(assessment)
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
        if stopped_context and (
            stopped_candidate_action_blocked
            or _stopped_candidate_action_requested(
                message,
                intent_understanding,
                turn_decision,
            )
        ):
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
            persisted_payload = _persistable_attachment_payload(selected_payload)
            business_focus = self._persist_copilot_focus(
                session_id, message, persisted_payload,
                structured=persisted_payload, conflicts=focus_conflicts,
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
                        (session_id, context_type, context_id, "user", message, _dumps(persisted_payload)),
                        (
                            session_id, context_type, context_id, "assistant", answer,
                            _dumps({
                                "references": references, "suggested_actions": suggested_actions,
                                "skill_runs": [], "business_focus": business_focus,
                                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
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
                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
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
            selected_payload["position"] = _copilot_job_evidence(self, int(context_id))
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
    uploaded_attachment_evidence = self._uploaded_attachment_evidence(raw_context, session_id)
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
                                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
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
                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
            }
    mentioned_jobs = _jobs_relevant_to_selected_context(
        self._mentioned_jobs_for_copilot(message),
        selected,
        selected_facts,
        message,
    )
    if mentioned_jobs:
        selected_payload["mentioned_jobs"] = mentioned_jobs
        for item in mentioned_jobs[:3]:
            references.append(
                {"type": "job", "id": item.get("id"), "label": item.get("job"), "subtitle": item.get("client")}
            )
    workflow_outcome_context = self._copilot_workflow_outcome_context(
        message, selected, mentioned_jobs, existing_focus
    )
    routed_skills = (
        []
        if goal_workflow or forced_answer is not None or workflow_outcome_question
        else self._route_copilot_skills(message, selected)
    )
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
            elif skill_id == "outreach_queue":
                # 自动取当前岗位 A 级候选人的 jc_id 作为触达队列；优先级按消息中关键词或默认 P1
                _jc_ids, _prios = self._default_outreach_queue_inputs(message, selected)
                skill_inputs = {"job_candidate_ids": _jc_ids, "priorities": _prios}
            skill_run = self.execute_skill(skill_id, context=selected, inputs=skill_inputs)
            skill_runs.append(skill_run)
            result = skill_run.get("result") or {}
            references.extend(result.get("references") or [])
            suggested_actions.extend(result.get("suggested_actions") or [])
        except Exception as exc:
            skill_runs.append({"skill": {"id": skill_id}, "ok": False, "error": str(exc)[:500]})
    # 仅在没有任何任务信号时才用 dashboard top_actions 兜底；
    # 否则用户会收到与当前对话无关的候选/岗位卡片（功能卡）。
    has_task_signal = bool(
        goal_workflow
        or forced_answer is not None
        or workflow_outcome_context
        or routed_skills
        or mentioned_jobs
        or attachment_skill_run
        or _is_job_requirement_message(message)
        or str(intent_understanding.get("action") or "none") != "none"
    )
    if not references and not has_task_signal:
        references = [
            {"type": item["type"], "id": item["id"], "label": item["label"], "subtitle": item["project"]}
            for item in dashboard.get("top_actions", [])[:5]
        ]
    memories = self.search_memories(
        message, context_type=context_type, context_id=context_id,
        client=str(selected_payload.get("client") or selected_payload.get("position", {}).get("client") or ""),
        job=str(selected_payload.get("job") or selected_payload.get("position", {}).get("job") or ""),
    )
    payload = {
        "question": message,
        "intent_understanding": intent_understanding,
        "response_mode": "floating_compact" if floating_compact else "default",
        "response_detail": _copilot_response_detail(message),
        "conversation": self._copilot_conversation_context(session_id, conversation_history),
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
    if context_conflicts:
        payload["context_conflicts"] = context_conflicts
    if workflow_outcome_context:
        payload["workflow_outcome"] = workflow_outcome_context
    capture_run = next(
        (
            item for item in skill_runs
            if (item.get("skill") or {}).get("id") == "liepin_resume_capture"
        ),
        None,
    )
    answer_source = "rules"
    model_tool_calls: list[dict[str, Any]] = []
    if goal_workflow:
        plan_steps = goal_workflow.get("steps") or []
        risk_steps = [step for step in plan_steps if step.get("risk_level") in {"R2", "R3"}]
        if workflow_cancelled:
            answer = cancelled_answer_override or "结论：当前计划已取消，后续步骤不会继续执行。"
        elif strategy_revision and floating_compact:
            answer = (
                "结论：已生成寻访策略修订版，旧计划和旧审批已失效。\n\n"
                "下一步：查看新计划，确认后再开始准备。"
            )
        elif strategy_revision:
            answer = (
                f"已生成策略修订版：{goal_workflow['goal']['title']}。\n\n"
                "旧工作流及其待审批已失效；修订版尚未开始，请查看新计划后确认。"
            )
        elif start_pending_plan or started_new_plan:
            answer = (
                f"正在执行：{goal_workflow['goal']['title']}。\n\n"
                "当前步骤完成后会给出可核验结果；涉及外部寻访时再单独请求 R3 授权。"
            )
        elif floating_compact:
            answer = (
                f"结论：目标已建立，计划共 {len(plan_steps)} 步。\n\n"
                f"下一步：先查看计划；{len(risk_steps)} 个风险节点会单次确认。"
            )
        else:
            answer = (
                f"已整理好本次任务：{goal_workflow['goal']['title']}。\n\n"
                f"当前尚未开始，共 {len(plan_steps)} 步；开始后会交付本轮可核验结果。"
            )
        references.extend(
            [
                {"type": goal_workflow["goal"]["context"].get("type"), "id": goal_workflow["goal"]["context"].get("id"), "label": goal_workflow["goal"]["title"], "subtitle": "ASA 目标"}
            ]
        )
        workflow_status = str((goal_workflow.get("workflow") or {}).get("status") or "")
        if workflow_status == "planned":
            suggested_actions.append(
                {
                    "type": "start_workflow",
                    "id": goal_workflow["workflow"]["workflow_id"],
                    "label": "确认新计划" if strategy_revision else "开始执行本次任务",
                    "plan_ref": goal_workflow.get("plan_ref") or {},
                }
            )
        if not workflow_cancelled:
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
        if rule_evidence:
            payload["rule_evidence"] = rule_evidence
        answer, model_tool_calls, tool_references = _generate_copilot_model_answer(
            self,
            sanitize_payload(payload),
        )
        references.extend(tool_references)
        if not answer:
            answer = rule_evidence or "当前查询已完成，但暂时没有生成可用结论。"
        answer_source = "model_tools" if model_tool_calls else "model"
    references = _dedupe_copilot_references(references)
    persisted_payload = _persistable_attachment_payload(selected_payload)
    focus_context = (
        (goal_workflow.get("goal") or {}).get("context")
        if goal_workflow else persisted_payload
    ) or persisted_payload
    business_focus = self._persist_copilot_focus(
        session_id, message, focus_context,
        structured=persisted_payload, conflicts=focus_conflicts,
    )
    assistant_structured = {
        "references": references,
        "suggested_actions": suggested_actions,
        "skill_runs": skill_runs,
        "goal": goal_workflow.get("goal") if goal_workflow else None,
        "workflow": (
            goal_workflow.get("workflow")
            if goal_workflow else current_workflow_context.get("workflow") or None
        ),
        "plan_ref": (
            goal_workflow.get("plan_ref")
            if goal_workflow else current_workflow_context.get("plan_ref") or None
        ),
        "plan_summary": [
            {
                "id": step.get("id"),
                "capability_id": step.get("capability_id"),
                "label": step.get("business_label") or step.get("label"),
                "status": step.get("status"),
                "risk_level": step.get("risk_level"),
            }
            for step in (
                (goal_workflow.get("steps") or [])
                if goal_workflow else (current_workflow_context.get("steps") or [])
            )
        ],
        "business_focus": business_focus,
        "intent_understanding": intent_understanding,
        "fact_receipt": fact_receipt or None,
        "turn_decision": turn_decision,
        "tool_calls": model_tool_calls,
        "model_participation": {
            "mode": answer_source,
            "label": (
                "模型生成 + 只读工具证据" if answer_source == "model_tools"
                else "模型生成 + 上下文约束" if answer_source == "model"
                else "规则生成"
            ),
            "model": (
                self.llm.copilot_runtime_metadata().get("model")
                if answer_source in {"model", "model_tools"} else None
            ),
            "routing": (
                self.llm.copilot_runtime_metadata()
                if answer_source in {"model", "model_tools"} else None
            ),
        },
    }
    if goal_workflow and not workflow_cancelled and not start_pending_plan and not started_new_plan:
        presented_workflow = goal_workflow.get("workflow") if isinstance(goal_workflow.get("workflow"), dict) else {}
        presented_plan = goal_workflow.get("plan_ref") if isinstance(goal_workflow.get("plan_ref"), dict) else {}
        presented_goal = goal_workflow.get("goal") if isinstance(goal_workflow.get("goal"), dict) else {}
        if (
            str(presented_workflow.get("status") or "") == "planned"
            and presented_plan.get("workflow_id")
            and presented_plan.get("plan_hash")
        ):
            assistant_structured["presented_plan_ref"] = {
                "workflow_id": presented_plan.get("workflow_id"),
                "version": presented_plan.get("version"),
                "plan_hash": presented_plan.get("plan_hash"),
                "action": str(presented_goal.get("action") or semantic_action or ""),
                "target": dict(presented_goal.get("context") or {}),
                "state_revision": int(
                    ((business_focus or {}).get("conversation_state") or {}).get("revision") or 0
                ),
            }
    if salary_recap_pending:
        salary_recap_pending["state_revision"] = int(
            ((business_focus or {}).get("conversation_state") or {}).get("revision") or 0
        )
        assistant_structured["pending_plan_confirmation"] = salary_recap_pending
    if strategy_revision:
        assistant_structured["workflow_revision"] = strategy_revision
    # 策略建议结构化：本轮未直接执行修订时，从回答中提取可应用的策略补丁
    strategy_patch = (
        _build_strategy_patch(self, message, answer, selected_payload, conversation_history)
        if strategy_revision is None and not workflow_strategy_question else None
    )
    if strategy_patch:
        assistant_structured["strategy_patch"] = strategy_patch
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
    if goal_workflow or current_workflow_context:
        workflow_state = goal_workflow or current_workflow_context
        workflow = workflow_state.get("workflow") or {}
        progress = workflow_state.get("progress") or {}
        assistant_structured["workflow_progress"] = {
            "workflow_id": workflow.get("workflow_id"),
            "status": workflow.get("status") or (workflow_state.get("goal") or {}).get("status") or "queued",
            "completed": progress.get("completed") or 0,
            "total": progress.get("total") or len(workflow_state.get("steps") or []),
            "label": workflow.get("current_stage") or "准备执行",
            "pending_approvals": [item for item in (workflow_state.get("approvals") or []) if item.get("status") == "pending"],
        }
    conn = self._connect()
    try:
        # 查询型名单直答：附带结构化名单卡（前端渲染可点击名单弹窗）。
        if candidate_list_card:
            assistant_structured["action_card"] = candidate_list_card
        # 寻访结果卡：已完成/阻塞的寻访工作流在对话中附带可展示的结果卡。
        if goal_workflow or current_workflow_context:
            workflow_state = goal_workflow or current_workflow_context
            workflow_id = str((workflow_state.get("workflow") or {}).get("workflow_id") or "")
            workflow_status = str(
                (workflow_state.get("workflow") or {}).get("status")
                or (workflow_state.get("goal") or {}).get("status")
                or ""
            )
            if workflow_id and workflow_status in {"completed", "blocked", "failed"}:
                try:
                    from . import sourcing_result_card

                    result_card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
                    if result_card:
                        assistant_structured["action_card"] = result_card
                except Exception:
                    # 结果卡生成失败不应阻塞主回复。
                    pass
        conn.executemany(
            """
            INSERT INTO agent_copilot_messages
            (session_id,context_type,context_id,role,content,structured_json)
            VALUES (?,?,?,?,?,?)
            """,
            [
                (session_id, context_type, context_id, "user", message, _dumps(persisted_payload)),
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
    # 决策审计：每轮把回合决策链写入 agent_copilot_events（payload 走 JSON，不改表结构）。
    try:
        self.record_copilot_event(
            session_id,
            "turn_decision",
            {
                "intent": str(intent_understanding.get("speech_act") or ""),
                "target": dict(intent_understanding.get("target") or {}),
                "action": str(intent_understanding.get("action") or "none"),
                "effect": str(turn_decision.get("effect") or ""),
                "workflow_created": bool(goal_workflow) and not workflow_cancelled,
                "evidence": [
                    str(item)
                    for item in ((turn_decision.get("authorization") or {}).get("evidence") or [])
                ],
                "fact_receipt": dict(fact_receipt) if fact_receipt else None,
                "confirmation_mode": (
                    "salary_confirmed"
                    if salary_confirmation_resolved
                    else "salary_precreate"
                    if salary_recap_pending
                    else "plan_anchor"
                    if (plan_reply and confirmation_plan_ref)
                    else ""
                ),
            },
        )
    except Exception:
        # 审计埋点失败不阻塞主回复。
        pass
    # Phase 1.2: 触发对话摘要（非阻塞，失败不影响主流程）
    try:
        self._maybe_summarize_copilot_conversation(session_id)
    except Exception:
        pass
    return {
        "ok": True,
        "session_id": session_id,
        "answer": answer,
        "context": {"type": context_type, "id": context_id},
        "references": references,
        "suggested_actions": suggested_actions,
        "skill_runs": skill_runs,
        "model_participation": assistant_structured["model_participation"],
        "goal_id": goal_workflow["goal"]["goal_id"] if goal_workflow else None,
        "workflow_id": (
            goal_workflow["workflow"]["workflow_id"]
            if goal_workflow else (current_workflow_context.get("workflow") or {}).get("workflow_id")
        ),
        "goal": goal_workflow.get("goal") if goal_workflow else None,
        "workflow": (
            goal_workflow.get("workflow")
            if goal_workflow else current_workflow_context.get("workflow") or None
        ),
        "plan_ref": (
            goal_workflow.get("plan_ref")
            if goal_workflow else current_workflow_context.get("plan_ref") or None
        ),
        "plan_summary": [
            {
                "id": step.get("id"),
                "capability_id": step.get("capability_id"),
                "label": step.get("business_label") or step.get("label"),
                "status": step.get("status"),
                "risk_level": step.get("risk_level"),
                "reason": step.get("reason"),
            }
            for step in (
                (goal_workflow.get("steps") or [])
                if goal_workflow else (current_workflow_context.get("steps") or [])
            )
        ],
        "approvals": (
            goal_workflow.get("approvals")
            if goal_workflow else current_workflow_context.get("approvals") or []
        ),
        "artifacts": (
            goal_workflow.get("artifacts")
            if goal_workflow else (
                [current_workflow_context["strategy"]]
                if current_workflow_context.get("strategy") else []
            )
        ),
        "progress": (
            goal_workflow.get("progress")
            if goal_workflow else current_workflow_context.get("progress") or None
        ),
        "workflow_revision": strategy_revision,
        "strategy_patch": strategy_patch,
        "memory": {"mode": memories.get("mode"), "hits": len(memories.get("memories") or [])},
        "business_focus": business_focus,
        "intent_understanding": intent_understanding,
        "fact_receipt": fact_receipt or None,
        "turn_decision": turn_decision,
        "tool_calls": model_tool_calls,
        "proactive_suggestions": generate_proactive_suggestions(str(self.db_path)),
        "action_card": assistant_structured.get("action_card"),
        "action_cards": [assistant_structured["action_card"]] if assistant_structured.get("action_card") else [],
    }
