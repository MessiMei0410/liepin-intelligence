"""Copilot session/context state management (split from copilot_handler.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
import json, re
from datetime import datetime
from typing import Any

from ._shared import (
    _dumps,
    _loads,
    _row,
    _is_short_ack,
)
from .workflow import BUSINESS_OUTCOME_LABELS, classify_business_outcome, sourcing_target_stats
from .capability_runtime import ZERO_RESULT_ATTRIBUTION_LABELS
from .conversation_state import (
    TERMINAL_WORKFLOW_STATUSES,
    build_context_state,
)

# Cross-module references (split from copilot_handler.py)
from .copilot_evidence import _continued_sourcing_requested, _copilot_context_job_id, _copilot_context_job_record, _copilot_focus_from_joined_row, _explicitly_mentioned_job_ids, _jobs_relevant_to_selected_context, _new_candidate_outreach_requested
from .copilot_intent import _COPILOT_SEMANTIC_ACTIONS


def _format_workflow_strategy_answer(workflow_context: dict[str, Any], *, expanded: bool = False) -> str:
    strategy = workflow_context.get("strategy") if isinstance(workflow_context.get("strategy"), dict) else None
    workflow = workflow_context.get("workflow") if isinstance(workflow_context.get("workflow"), dict) else {}
    if not strategy:
        return "结论：这个任务还没有通过校验的寻访策略。\n\n下一步：先完成“生成多渠道寻访策略”步骤。"

    channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
    liepin = channels.get("liepin") if isinstance(channels.get("liepin"), list) else []
    xsaas = channels.get("xsaas") if isinstance(channels.get("xsaas"), list) else []
    pending_r3 = any(
        approval.get("status") == "pending" and approval.get("risk_level") == "R3"
        for approval in (workflow_context.get("approvals") or [])
    )
    approval_text = "当前待 R3 审批，尚未执行外部搜索。" if pending_r3 else f"当前工作流状态：{workflow.get('status') or '未知'}。"

    def values(items: list[dict[str, Any]], key: str, limit: int) -> str:
        result = [str(item.get(key) or "").strip() for item in items if isinstance(item, dict)]
        result = [item for item in result if item]
        if expanded:
            limit = len(result)
        suffix = f"；另有 {len(result) - limit} 项" if len(result) > limit else ""
        return "；".join(result[:limit]) + suffix

    companies = [str(item).strip() for item in (strategy.get("target_companies") or []) if str(item).strip()]
    hard_requirements = [
        {"value": item} for item in (strategy.get("hard_requirements") or []) if str(item).strip()
    ]
    negative_rules = [
        {"value": item} for item in (strategy.get("negative_rules") or []) if str(item).strip()
    ]
    risk_points = [
        {"value": item} for item in (strategy.get("risk_points") or []) if str(item).strip()
    ]
    summary = str(strategy.get("summary") or "").strip() or "按当前通过校验的 strategy_v2 执行。"
    lines = [
        f"结论：{summary}{approval_text}",
        f"猎聘 {len(liepin)} 组：{values(liepin, 'query', 6) or '未配置'}",
        f"X-SaaS {len(xsaas)} 组：{values(xsaas, 'query', 6) or '未配置'}",
    ]
    if companies:
        company_limit = len(companies) if expanded else 10
        lines.append(f"目标公司：{'、'.join(companies[:company_limit])}" + (f"等 {len(companies)} 家" if len(companies) > company_limit else ""))
    if hard_requirements:
        lines.append(f"硬条件：{values(hard_requirements, 'value', 5)}")
    if negative_rules:
        lines.append(f"排除：{values(negative_rules, 'value', 4)}")
    if risk_points:
        lines.append(f"风险：{values(risk_points, 'value', 4)}")
    lines.append("下一步：批准后只搜索、排重并进入待复核，不发送消息。" if pending_r3 else "下一步：按工作流当前状态继续。")
    return "\n\n".join(lines)


def _format_context_mismatch_answer(
    conflicts: list[dict[str, Any]], *, floating_compact: bool = False
) -> str:
    """Ask before answering when the visible/focused object conflicts with the user's explicit client."""
    ambiguous_job = next((item for item in conflicts if item.get("type") == "ambiguous_job"), None)
    if ambiguous_job:
        labels = [
            f"{item.get('job') or '未命名岗位'}（岗位 {item.get('id')}）"
            for item in (ambiguous_job.get("candidates") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return (
            "结论：还不能唯一确定目标岗位；这句话同时指向多个岗位，"
            "暂不把预算或其他事实归到任何一个岗位，也不创建任务。\n\n"
            f"依据：{'、'.join(labels) if labels else '当前识别到多个岗位'}。\n\n"
            "下一步：请补充唯一的岗位名称或岗位编号。"
        )
    ambiguous_client = next((item for item in conflicts if item.get("type") == "ambiguous_client"), None)
    if ambiguous_client:
        candidates = "、".join(
            str(item).strip()
            for item in (ambiguous_client.get("candidates") or [])
            if str(item).strip()
        )
        return (
            "结论：这句话同时提到多个客户，暂不读取或创建任务。\n\n"
            f"依据：{candidates or '当前识别到多个客户'}。\n\n"
            "下一步：请明确客户和岗位。"
        )
    mismatch = next((item for item in conflicts if item.get("type") == "context_client_mismatch"), None)
    if not mismatch:
        return ""
    selected_client = str(mismatch.get("selected_client") or "当前上下文").strip()
    mentioned = [
        str(item or "").strip()
        for item in (mismatch.get("mentioned_clients") or [])
        if str(item or "").strip()
    ]
    mentioned_label = "、".join(mentioned) or "你提到的客户"
    if floating_compact:
        return (
            f"对象不一致：当前是{selected_client}，你提到{mentioned_label}。\n\n"
            "下一步：先确认要问哪个客户/岗位。"
        )
    return (
        f"我这里的上下文对象不一致：当前页面/会话焦点是「{selected_client}」，"
        f"但你这句提到「{mentioned_label}」。\n\n"
        "请先确认要问哪个客户或岗位，我再按对应上下文回答。"
    )


def _copilot_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    if str(context.get("type") or "") == "workflow":
        return self._copilot_workflow_context_facts(context)
    return self._copilot_focus_context_facts(context)


def _copilot_context_from_focus(
    self, session_id: str, message: str, selected: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    focus = self.get_copilot_focus(session_id)
    current_clients = self._mentioned_client_names(message)
    selected_facts = self._copilot_context_facts(selected)
    raw_current_jobs = self._mentioned_jobs_for_copilot(message)
    current_jobs = _jobs_relevant_to_selected_context(
        raw_current_jobs,
        selected,
        selected_facts,
        message,
    )
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
    # A uniquely named/numbered job outranks a stale client/page focus. Do this
    # before the client mismatch check so cross-client references resolve to the
    # job the consultant actually named.
    explicit_job_ids = _explicitly_mentioned_job_ids(message, current_jobs)
    if len(current_jobs) == 1 and explicit_job_ids:
        try:
            mentioned_job_id = int(current_jobs[0].get("id") or 0) or None
        except (TypeError, ValueError):
            mentioned_job_id = None
        if mentioned_job_id:
            return {
                "type": "job", "id": mentioned_job_id,
                "page": "positions", "filters": {},
            }, []

    if conflicts:
        return dict(selected), conflicts

    if selected_facts and current_clients and selected_facts.get("client") not in current_clients:
        conflicts.append({
            "type": "context_client_mismatch",
            "selected_client": selected_facts.get("client"),
            "mentioned_clients": current_clients[:5],
        })
        return {"type": "global", "id": None, "page": selected.get("page") or "overview", "filters": {}}, conflicts
    selected_job_id = _copilot_context_job_id(selected, selected_facts)
    if len(current_jobs) == 1 and selected_job_id is not None:
        try:
            mentioned_job_id = int(current_jobs[0].get("id") or 0) or None
        except (TypeError, ValueError):
            mentioned_job_id = None
        if mentioned_job_id and mentioned_job_id != selected_job_id and explicit_job_ids:
            # A uniquely named/numbered job in the current sentence outranks a
            # stale page focus. Broad client-only mentions are filtered back to
            # the selected job by _jobs_relevant_to_selected_context above.
            return {
                "type": "job", "id": mentioned_job_id,
                "page": "positions", "filters": {},
            }, []
    # 模糊结果追问（"寻访结果呢"类，未提及岗位/客户名）优先会话焦点岗位，
    # 避免前端残留页面 context 把主线串到其他岗位（2026-08-07 长越→电源专家串台）。
    if (
        selected_facts
        and selected.get("type") == "job"
        and not current_jobs
        and not current_clients
        and re.search(r"(?:结果|进展|情况|怎么样|如何)", message)
        and focus
    ):
        focus_context = focus.get("context") if isinstance(focus.get("context"), dict) else {}
        if (
            focus_context.get("type") == "job"
            and focus_context.get("id")
            and str(selected.get("id")) != str(focus_context.get("id"))
            and float(focus.get("confidence") or 0) >= 0.7
        ):
            return {
                "type": "job", "id": int(focus_context["id"]),
                "page": "positions", "filters": {},
            }, []
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
    continuation = _is_short_ack(message) or _new_candidate_outreach_requested(message) or _continued_sourcing_requested(message) or any(
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
    facts = self._copilot_context_facts(selected)
    mentioned_clients = self._mentioned_client_names(message)
    resolution_selected = selected
    resolution_facts = facts
    if not resolution_facts and isinstance(previous, dict):
        previous_context = previous.get("context") if isinstance(previous.get("context"), dict) else {}
        if previous_context.get("type") in {"job", "candidate", "workflow"} and previous_context.get("id"):
            previous_facts = self._copilot_context_facts(previous_context)
            if previous_facts:
                resolution_selected = previous_context
                resolution_facts = previous_facts
    mentioned_jobs = _jobs_relevant_to_selected_context(
        self._mentioned_jobs_for_copilot(message),
        resolution_selected,
        resolution_facts,
        message,
    )
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

    understanding = structured.get("intent_understanding") if isinstance(structured.get("intent_understanding"), dict) else {}
    turn_decision = structured.get("turn_decision") if isinstance(structured.get("turn_decision"), dict) else {}
    effective_constraints = [
        item
        for item in (turn_decision.get("effective_constraints") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    if effective_constraints:
        constraint_ledger = effective_constraints[-24:]
        constraints = [str(item.get("quote") or "").strip() for item in constraint_ledger]
    elif turn_decision.get("constraint_changes"):
        constraint_ledger = []
        constraints = []
    else:
        constraint_ledger = list(previous.get("constraint_ledger") or [])
        constraints = list(previous.get("constraints") or [])

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

    semantic_action = str(understanding.get("action") or "")
    pending_workflow = (
        dict(previous.get("pending_workflow") or {})
        if isinstance(previous.get("pending_workflow"), dict)
        else {}
    )
    current_workflow = (
        dict(previous.get("current_workflow") or {})
        if isinstance(previous.get("current_workflow"), dict)
        else {}
    )
    workflow_intent = (
        structured.get("workflow_intent")
        if isinstance(structured.get("workflow_intent"), dict)
        else None
    )
    if workflow_intent is not None:
        pending_workflow = dict(workflow_intent) if workflow_intent.get("status") == "planned" else {}
        current_workflow = (
            dict(workflow_intent)
            if workflow_intent.get("status") not in TERMINAL_WORKFLOW_STATUSES
            else {}
        )
    elif turn_decision.get("effect") == "cancel_plan" or str(understanding.get("speech_act") or "") == "cancel":
        pending_workflow = {}
        current_workflow = {}
    active_workflow = pending_workflow or current_workflow
    action = semantic_action if semantic_action in _COPILOT_SEMANTIC_ACTIONS and semantic_action != "none" else ""
    if not action and active_workflow:
        action = str(active_workflow.get("action") or previous.get("action") or "")
    elif not action and _is_short_ack(message):
        action = str(previous.get("action") or "") if active_workflow else ""
    semantic_objective = str(understanding.get("objective") or "").strip()
    objective = semantic_objective if action else ""
    if active_workflow and not objective:
        objective = str(active_workflow.get("objective") or previous.get("objective") or "")
    focus = {
        "context": context_value,
        "client": client,
        "job": job,
        "candidate": candidate,
        "objective": objective,
        "action": action,
        "directions": directions[-6:],
        "attachments": attachments[-8:],
        "constraints": constraints[-8:],
        "constraint_ledger": constraint_ledger[-24:],
        "understanding": understanding,
        "turn_decision": turn_decision,
        "pending_workflow": pending_workflow,
        "current_workflow": current_workflow,
        "confidence": round(confidence, 3),
    }
    previous_state = previous.get("conversation_state") if isinstance(previous.get("conversation_state"), dict) else {}
    if not previous_state and active_workflow:
        previous_state = {
            "active_goal": {
                "action": str(active_workflow.get("action") or action),
                "objective": str(active_workflow.get("objective") or objective),
                "status": str(active_workflow.get("status") or "active"),
                "workflow_id": str(active_workflow.get("workflow_id") or ""),
            },
            "pending_plan": dict(pending_workflow),
            "constraints": list(constraint_ledger),
        }
    conversation_state = build_context_state(
        previous_state,
        message=message,
        context=selected,
        business_focus=focus,
        understanding=understanding,
        decision=turn_decision,
        workflow_intent=workflow_intent,
        now=stamp,
    )
    focus["conversation_state"] = conversation_state
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
        conn.execute(
            """
            INSERT INTO agent_copilot_state(session_id,revision,state_json,updated_at)
            VALUES (?,?,?,datetime('now','localtime'))
            ON CONFLICT(session_id) DO UPDATE SET
                revision=excluded.revision,state_json=excluded.state_json,
                updated_at=datetime('now','localtime')
            """,
            (session_id, int(conversation_state.get("revision") or 1), _dumps(conversation_state)),
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
            uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
            uploaded_items = []
            for item in (uploaded.get("items") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                uploaded_items.append({
                    key: item.get(key)
                    for key in (
                        "attachment_id", "file_name", "file_type", "mime_type", "size_bytes",
                        "content_available", "truncated", "is_image", "status",
                    )
                })
            message_context = {"type": row["context_type"], "id": row["context_id"]}
            if uploaded_items:
                message_context["uploaded_attachments"] = uploaded_items
            messages.append(
                {
                    "role": row["role"], "content": row["content"],
                    "context": message_context,
                    "references": structured.get("references") or [],
                    "suggested_actions": structured.get("suggested_actions") or [],
                    "skill_runs": structured.get("skill_runs") or [],
                    "goal": structured.get("goal"),
                    "workflow": structured.get("workflow"),
                    "plan_ref": structured.get("plan_ref"),
                    "plan_summary": structured.get("plan_summary") or [],
                    "workflow_progress": structured.get("workflow_progress"),
                    "business_focus": structured.get("business_focus"),
                    "turn_decision": structured.get("turn_decision"),
                    # R9/R12-b：透传持久化的 pending_intent，浮窗恢复会话时可重渲染确认卡
                    #（确认/取消终态是 UI 本地态；过期或已执行的意图确认时会走 409 漂移路径）。
                    "pending_intent": structured.get("pending_intent"),
                    "action_card": structured.get("action_card"),
                    "action_cards": structured.get("action_cards") or [],
                    "model_participation": structured.get("model_participation"),
                    "analysis_card": structured.get("analysis_card"),
                    # 策略建议补丁：浮窗恢复会话时可重渲染「应用到策略」操作栏
                    "strategy_patch": structured.get("strategy_patch"),
                    "strategy_patch_applied": bool(structured.get("strategy_patch_applied")),
                    "strategy_patch_revised_workflow_id": structured.get("strategy_patch_revised_workflow_id"),
                    "strategy_patch_reverted": bool(structured.get("strategy_patch_reverted")),
                    "strategy_patch_restored_workflow_id": structured.get("strategy_patch_restored_workflow_id"),
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


def list_copilot_sessions(
    self,
    limit: int = 30,
    query: str = "",
    include_archived: bool = False,
) -> dict[str, Any]:
    query = " ".join(str(query or "").split())[:120]
    # 转义 LIKE 通配符，避免 q=% / q=_ 匹配全部会话
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    search = f"%{escaped}%"
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            WITH rollup AS (
                SELECT messages.session_id,
                       COUNT(*) AS message_count,
                       MAX(messages.id) AS latest_id,
                       MAX(messages.created_at) AS message_updated_at,
                       (SELECT content FROM agent_copilot_messages first_user
                        WHERE first_user.session_id=messages.session_id AND first_user.role='user'
                        ORDER BY first_user.id LIMIT 1) AS derived_title,
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
            )
            SELECT rollup.session_id, rollup.message_count, rollup.latest_id,
                   COALESCE(metadata.updated_at, rollup.message_updated_at) AS updated_at,
                   COALESCE(NULLIF(metadata.title, ''), rollup.derived_title) AS title,
                   rollup.preview, rollup.context_type, rollup.context_id,
                   metadata.archived_at,
                   focus.revision AS focus_revision,
                   focus.context_type AS focus_context_type,
                   focus.context_id AS focus_context_id,
                   focus.client AS focus_client,
                   focus.action AS focus_action,
                   focus.confidence AS focus_confidence,
                   focus.focus_json,
                   focus.evidence_json AS focus_evidence_json,
                   focus.conflicts_json AS focus_conflicts_json,
                   focus.updated_at AS focus_updated_at
            FROM rollup
            LEFT JOIN agent_copilot_sessions metadata ON metadata.session_id=rollup.session_id
            LEFT JOIN agent_copilot_focus focus ON focus.session_id=rollup.session_id
            WHERE (? OR metadata.archived_at IS NULL)
              AND (? = '' OR COALESCE(NULLIF(metadata.title, ''), rollup.derived_title, '') LIKE ? ESCAPE '\\'
                   OR COALESCE(rollup.preview, '') LIKE ? ESCAPE '\\')
            ORDER BY latest_id DESC
            LIMIT ?
            """,
            (int(bool(include_archived)), query, search, search, max(1, min(int(limit or 30), 100))),
        ).fetchall()
        sessions = []
        for row in rows:
            item = _row(row)
            item["title"] = str(item.get("title") or "未命名对话")[:80]
            item["preview"] = " ".join(str(item.get("preview") or "").split())[:120]
            item["archived"] = bool(item.pop("archived_at", None))
            item["business_focus"] = _copilot_focus_from_joined_row(row)
            for key in [key for key in item if key.startswith("focus_") or key == "focus_json"]:
                item.pop(key, None)
            sessions.append(item)
        return {"ok": True, "sessions": sessions}
    finally:
        conn.close()


def update_copilot_session(
    self,
    session_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
    clear_focus: bool = False,
) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    normalized_title = " ".join(str(title or "").split()) if title is not None else None
    if title is not None and not normalized_title:
        raise ValueError("Agent task title cannot be empty")
    conn = self._connect()
    try:
        # 存在性口径与列表查询一致：仅 focus/metadata 中有记录但没有消息的会话
        # 在列表里永远不可见，PATCH 应按不存在处理（与 GET detail 的 404 语义一致）。
        exists = conn.execute(
            "SELECT 1 FROM agent_copilot_messages WHERE session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
        if exists is None:
            raise LookupError("Agent task not found")
        conn.execute(
            """INSERT INTO agent_copilot_sessions(session_id,title,archived_at,updated_at)
               VALUES (?, ?, CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END, datetime('now','localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 title=CASE WHEN ? THEN excluded.title ELSE agent_copilot_sessions.title END,
                 archived_at=CASE WHEN ? THEN excluded.archived_at ELSE agent_copilot_sessions.archived_at END,
                 updated_at=datetime('now','localtime')""",
            (
                session_id,
                normalized_title,
                int(archived is True),
                int(title is not None),
                int(archived is not None),
            ),
        )
        if clear_focus:
            conn.execute("DELETE FROM agent_copilot_focus WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM agent_copilot_state WHERE session_id=?", (session_id,))
        conn.commit()
        row = conn.execute(
            """SELECT metadata.session_id,
                      COALESCE(NULLIF(metadata.title, ''),
                        (SELECT content FROM agent_copilot_messages first_user
                         WHERE first_user.session_id=metadata.session_id AND first_user.role='user'
                         ORDER BY first_user.id LIMIT 1)) AS title,
                      metadata.archived_at,
                      focus.revision AS focus_revision,
                      focus.context_type AS focus_context_type,
                      focus.context_id AS focus_context_id,
                      focus.client AS focus_client,
                      focus.action AS focus_action,
                      focus.confidence AS focus_confidence,
                      focus.focus_json,
                      focus.evidence_json AS focus_evidence_json,
                      focus.conflicts_json AS focus_conflicts_json,
                      focus.updated_at AS focus_updated_at
               FROM agent_copilot_sessions metadata
               LEFT JOIN agent_copilot_focus focus ON focus.session_id=metadata.session_id
               WHERE metadata.session_id=?""",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "ok": True,
        "session_id": session_id,
        "title": str(row["title"] or "未命名对话")[:80],
        "archived": bool(row["archived_at"]),
        "business_focus": _copilot_focus_from_joined_row(row),
    }


def archive_all_copilot_sessions(self) -> dict[str, Any]:
    """Archive every visible Copilot task while preserving messages and focus evidence."""
    conn = self._connect()
    try:
        rows = conn.execute(
            """SELECT DISTINCT messages.session_id
                 FROM agent_copilot_messages messages
                 LEFT JOIN agent_copilot_sessions metadata ON metadata.session_id=messages.session_id
                WHERE metadata.archived_at IS NULL
                ORDER BY messages.session_id"""
        ).fetchall()
        session_ids = [str(row["session_id"]) for row in rows]
        conn.executemany(
            """INSERT INTO agent_copilot_sessions(session_id,title,archived_at,updated_at)
               VALUES (?, NULL, datetime('now','localtime'), datetime('now','localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 archived_at=datetime('now','localtime'),
                 updated_at=datetime('now','localtime')""",
            [(session_id,) for session_id in session_ids],
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "archived_count": len(session_ids),
        "session_ids": session_ids,
    }


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
    if not job_ids:
        inferred_jobs = self._mentioned_jobs_for_copilot(evidence_text)
        if len(inferred_jobs) == 1:
            add_job_id(inferred_jobs[0].get("id"))
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
    new_candidate_outreach = _new_candidate_outreach_requested(message)
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
    job_bound = job_write or sourcing or publishing or new_candidate_outreach
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
    selected_facts = self._copilot_context_facts(selected)
    current_jobs = _jobs_relevant_to_selected_context(
        self._mentioned_jobs_for_copilot(message),
        selected,
        selected_facts,
        message,
    )
    if not target_job and len(current_jobs) == 1:
        target_job = current_jobs[0]
    if not target_job:
        context_job = _copilot_context_job_record(selected_facts)
        context_client = str(context_job.get("client") or "")
        if context_job and (not client_candidates or not context_client or context_client in client_candidates):
            target_job = context_job
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
    if (sourcing or publishing or new_candidate_outreach) and not target_job:
        missing.append("唯一岗位")
    split_requested = any(token in message for token in ("拆分", "拆成", "分成", "三个", "分别"))
    if split_requested and not directions:
        missing.append("拆分方向")
    if missing:
        if new_candidate_outreach and not target_job:
            return dict(selected), {}, "你要为哪个岗位补充并触达新候选人？"
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
