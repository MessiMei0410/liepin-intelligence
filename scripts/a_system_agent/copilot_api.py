"""Copilot public API, events, summaries and SSE streaming (split from copilot_handler.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
import json, secrets, threading
from typing import Any

from ._shared import (
    _dumps,
    _loads,
    _table_exists,
)
from .context import build_candidate_context
from .copilot_tools import generate_proactive_suggestions
from .conversation_state import (
    deterministic_context_summary,
)

# Cross-module references (split from copilot_handler.py)
from .copilot_evidence import _build_strategy_patch, _candidate_evidence_question, _copilot_assessment_context, _copilot_job_evidence, _copilot_response_detail, _format_candidate_evidence_answer


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


def chat(self, job_candidate_id: int, message: str, session_id: str = "") -> dict[str, Any]:
    message = " ".join(str(message or "").split())
    if not message:
        raise ValueError("请输入问题")
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    state = self.get_candidate_state(int(job_candidate_id))
    assessment = state.get("assessment") or {}
    if not assessment:
        raise ValueError("请先完成当前人选的 Agent 评估")
    if _copilot_response_detail(message) == "expanded":
        answer = _format_candidate_evidence_answer(assessment)
    elif any(token in message for token in ["缺什么", "还缺", "核验"]):
        answer = _format_candidate_evidence_answer(assessment, gaps_only=True)
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


# ---- Phase 1.2: 对话摘要记忆 ----

_CONVERSATION_SUMMARY_THRESHOLD = 6  # 每 6 轮用户消息生成一次摘要
_CONVERSATION_HISTORY_WINDOW = 8     # 保留最近 8 轮完整历史

_SUMMARY_SYSTEM_PROMPT = """你是 ASA 对话摘要器。将 copilot 对话历史压缩为结构化摘要。
只输出 JSON，不执行业务动作。摘要字段：
- stage: 当前业务阶段（如 "候选人评估中"/"寻访策略制定"/"待触达"/"面试跟进中"）
- entities: [{type: "job"|"candidate"|"client", id, name_or_title}] 涉及的关键实体
- decisions: ["已确认的决策列表"] 
- pending: ["待处理的待办"]
- key_facts: ["对话中确认的关键事实"]
只返回 JSON：{"stage":"...","entities":[...],"decisions":[...],"pending":[...],"key_facts":[...]}
"""


def _ensure_copilot_summaries_table(self, conn: Any) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_copilot_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            message_range_start INTEGER NOT NULL DEFAULT 0,
            message_range_end INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cs_session ON agent_copilot_summaries(session_id)")


def _ensure_copilot_events_table(self, conn: Any) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_copilot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ce_session ON agent_copilot_events(session_id)")


def record_copilot_event(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """记录 Copilot 事件，并将策略确认卡的终态写回会话以支持恢复展示。"""
    event = str(event or "").strip()
    if not event:
        raise ValueError("event 不能为空")
    session_id = str(session_id or "")
    payload = payload if isinstance(payload, dict) else {}
    conn = self._connect()
    try:
        _ensure_copilot_events_table(self, conn)
        duplicate_strategy_receipt = False
        if event == "copilot_strategy_applied" and (payload.get("artifact_id") or payload.get("revision") is not None):
            for row in conn.execute(
                """
                SELECT payload_json FROM agent_copilot_events
                WHERE session_id=? AND event=? ORDER BY id DESC LIMIT 50
                """,
                (session_id, event),
            ).fetchall():
                previous = _loads(row["payload_json"], {}) or {}
                if (
                    str(previous.get("workflow_id") or "") == str(payload.get("workflow_id") or "")
                    and previous.get("revision") == payload.get("revision")
                    and str(previous.get("artifact_id") or "") == str(payload.get("artifact_id") or "")
                ):
                    duplicate_strategy_receipt = True
                    break
        if not duplicate_strategy_receipt:
            conn.execute(
                "INSERT INTO agent_copilot_events (session_id,event,payload_json) VALUES (?,?,?)",
                (session_id, event, _dumps(payload)),
            )
        # 不新增交互接口：复用浮窗原有事件通道，将修订结果写回产生该确认卡的
        # assistant structured_json。仅匹配同一 session 内的策略卡，避免跨工作流串写。
        if event in {"copilot_strategy_applied", "copilot_strategy_reverted"} and _table_exists(conn, "agent_copilot_messages"):
            rows = conn.execute(
                """
                SELECT id,structured_json FROM agent_copilot_messages
                WHERE session_id=? AND role='assistant' ORDER BY id DESC
                """,
                (session_id,),
            ).fetchall()
            if event == "copilot_strategy_applied":
                workflow_id = str(payload.get("workflow_id") or "").strip()
                for row in rows:
                    structured = _loads(row["structured_json"], {}) or {}
                    patch = structured.get("strategy_patch") if isinstance(structured.get("strategy_patch"), dict) else {}
                    if workflow_id and str(patch.get("workflow_id") or "") == workflow_id:
                        structured.update({
                            "strategy_patch_applied": True,
                            "strategy_patch_revised_workflow_id": str(payload.get("revised_workflow_id") or "").strip() or None,
                            "strategy_patch_revision": payload.get("revision"),
                            "strategy_patch_artifact_id": payload.get("artifact_id"),
                            "strategy_patch_applied_count": payload.get("applied"),
                        })
                        conn.execute(
                            "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                            (_dumps(structured), row["id"]),
                        )
                        break
            else:
                revised_workflow_id = str(payload.get("workflow_id") or "").strip()
                restored_workflow_id = str(payload.get("restored_workflow_id") or "").strip()
                for row in rows:
                    structured = _loads(row["structured_json"], {}) or {}
                    if revised_workflow_id and str(structured.get("strategy_patch_revised_workflow_id") or "") == revised_workflow_id:
                        structured.update({
                            "strategy_patch_reverted": True,
                            "strategy_patch_restored_workflow_id": restored_workflow_id,
                        })
                        conn.execute(
                            "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                            (_dumps(structured), row["id"]),
                        )
                        break
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "idempotent_replay": duplicate_strategy_receipt}


def _maybe_summarize_copilot_conversation(self, session_id: str) -> dict[str, Any] | None:
    """检查是否需要产生新的对话摘要。返回新摘要 dict 或 None。"""
    conn = self._connect()
    try:
        _ensure_copilot_summaries_table(self, conn)
        # 统计自上次摘要后的用户消息数
        last_summary = conn.execute(
            "SELECT MAX(message_range_end) FROM agent_copilot_summaries WHERE session_id=?",
            (session_id,),
        ).fetchone()
        last_end = int(last_summary[0]) if last_summary and last_summary[0] else 0
        user_count = conn.execute(
            "SELECT COUNT(*) FROM agent_copilot_messages WHERE session_id=? AND role='user' AND id>?",
            (session_id, last_end),
        ).fetchone()[0]
        if user_count < _CONVERSATION_SUMMARY_THRESHOLD:
            return None
        # 获取需要摘要的消息范围
        messages = conn.execute(
            """SELECT role,content FROM agent_copilot_messages
               WHERE session_id=? AND id>? AND role IN ('user','assistant')
               ORDER BY id""",
            (session_id, last_end),
        ).fetchall()
        if not messages:
            return None
        # 构建对话文本
        conversation_text = "\n".join(
            f"{'顾问' if row[0]=='user' else 'ASA'}: {row[1][:300]}"
            for row in messages[-20:]  # 最多取最近20条
        )
        context_state = self.get_copilot_context_state(session_id)
        deterministic_summary = deterministic_context_summary(context_state)
        # 调用 LLM 生成摘要；结构化状态始终作为保底事实，模型只补充表达。
        try:
            summary_text = self.llm._request(
                _SUMMARY_SYSTEM_PROMPT,
                {"conversation": conversation_text, "context_state": context_state},
                temperature=0.05,
                operation="copilot_summary",
            )
            model_summary = json.loads(summary_text.strip())
            if not isinstance(model_summary, dict):
                raise ValueError("summary must be an object")
            summary = dict(deterministic_summary)
            if str(model_summary.get("stage") or "").strip():
                summary["stage"] = str(model_summary["stage"]).strip()[:120]
            for key in ("entities", "decisions", "pending", "key_facts"):
                model_values = model_summary.get(key) if isinstance(model_summary.get(key), list) else []
                base_values = summary.get(key) if isinstance(summary.get(key), list) else []
                if key == "entities":
                    combined = [*base_values, *[item for item in model_values if isinstance(item, dict)]]
                    seen_entities: set[tuple[str, str, str]] = set()
                    summary[key] = []
                    for item in combined:
                        marker = (str(item.get("type") or ""), str(item.get("id") or ""), str(item.get("name_or_title") or ""))
                        if marker not in seen_entities:
                            seen_entities.add(marker)
                            summary[key].append(item)
                else:
                    summary[key] = list(dict.fromkeys(str(item) for item in [*base_values, *model_values] if str(item).strip()))[-16:]
        except Exception:
            summary = deterministic_summary
        # 持久化
        max_id = conn.execute(
            "SELECT MAX(id) FROM agent_copilot_messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0] or 0
        conn.execute(
            "INSERT INTO agent_copilot_summaries (session_id,summary_json,message_range_start,message_range_end) VALUES (?,?,?,?)",
            (session_id, json.dumps(summary, ensure_ascii=False), last_end, max_id),
        )
        conn.commit()
        return summary
    finally:
        conn.close()


def _copilot_conversation_context(self, session_id: str, conversation_history: list[dict[str, Any]]) -> dict[str, Any]:
    """构建注入 payload 的对话上下文（历史窗口 + 摘要）。"""
    conn = self._connect()
    try:
        _ensure_copilot_summaries_table(self, conn)
        summaries = conn.execute(
            "SELECT summary_json FROM agent_copilot_summaries WHERE session_id=? ORDER BY id DESC LIMIT 3",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    recent_history = conversation_history[-_CONVERSATION_HISTORY_WINDOW * 2:]  # user+assistant pairs
    return {
        "recent_history": recent_history,
        "summaries": [
            json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for row in summaries
        ] if summaries else [],
        "state": self.get_copilot_context_state(session_id),
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    """格式化 SSE 事件字符串。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def copilot_stream_generator(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
):
    """Expose the canonical Copilot result over SSE without duplicating decisions."""
    normalized = " ".join(str(message or "").split())
    if not normalized:
        yield _sse("error", {"error": "请输入问题"})
        return
    # SSE is transport only. Every message uses the canonical decision path so
    # clarification, focus, safety gates and workflow creation cannot diverge.
    result = self.copilot(normalized, session_id=session_id, context=context)
    yield _sse("context", {
        "session_id": result.get("session_id"),
        "context": result.get("context") or {},
        "references": result.get("references") or [],
        "suggested_actions": result.get("suggested_actions") or [],
        "understanding_card": result.get("understanding_card"),
    })
    answer = str(result.get("answer") or "")
    for offset in range(0, len(answer), 80):
        yield _sse("text", {"content": answer[offset:offset + 80]})
    yield _sse("done", result)
    return

# RETIRED: this pre-canonical tool loop is intentionally unbound. It remains
# temporarily for rollback archaeology; AgentService.copilot_agent is bound to
# copilot(), and all production traffic uses _generate_copilot_model_answer().

_MAX_TOOL_ROUNDS = 5  # 最多 5 轮工具调用


def _retired_legacy_copilot_agent(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retired pre-canonical tool loop; never bind this method in AgentService.
    
    与普通 copilot 的区别：LLM 可以主动调用工具（查DB、搜知识库等），
    工具结果注入对话后 LLM 综合给出最终回答。
    """
    from .copilot_tools import COPILOT_TOOLS, TOOL_EXECUTORS

    normalized = " ".join(str(message or "").split())
    if not normalized:
        raise ValueError("请输入问题")
    recent = self._copilot_conversation_history(session_id)
    awaiting_job_scope = bool(
        recent
        and recent[-1].get("role") == "assistant"
        and str(recent[-1].get("content") or "").strip() == "你要为哪个岗位补充并触达新候选人？"
    )
    if self._copilot_action_kind(normalized) or awaiting_job_scope:
        return self.copilot(normalized, session_id=session_id, context=context)
    stable_session_id = str(session_id or "").strip() or f"copilot_{secrets.token_hex(6)}"
    with self._copilot_locks_guard:
        session_lock = self._copilot_session_locks.setdefault(stable_session_id, threading.RLock())
    with session_lock:
        # ---- 复用 _copilot_impl 的预处理 ----
        message = " ".join(str(message or "").split())
        raw_context = dict(context or {})
        floating_compact = str(raw_context.get("display_mode") or "").strip() == "floating_compact"
        selected = self._normalize_copilot_context(raw_context)
        selected, focus_conflicts = self._copilot_context_from_focus(stable_session_id, message, selected)
        existing_focus = self.get_copilot_focus(stable_session_id)
        conversation_history = self._copilot_conversation_history(stable_session_id)
        if existing_focus:
            selected["business_focus"] = existing_focus

        context_type = selected["type"]
        context_id = selected.get("id")
        dashboard = self.get_dashboard()
        selected_payload: dict[str, Any] = dict(selected)
        references: list[dict[str, Any]] = []
        suggested_actions: list[dict[str, Any]] = []

        # 基础上下文注入
        if context_type == "candidate" and context_id:
            candidate_context = build_candidate_context(self.db_path, context_id)
            state = self.get_candidate_state(int(context_id))
            identity = candidate_context.get("identity", {})
            position = candidate_context.get("position", {})
            selected_payload["candidate"] = identity
            selected_payload["position"] = position
            selected_payload["assessment"] = _copilot_assessment_context(state.get("assessment") or {})
            references.append({"type": "candidate", "id": context_id, "label": identity.get("name") or "", "subtitle": f"{position.get('client','')}/{position.get('job','')}"})
        elif context_type == "job" and context_id:
            conn = self._connect()
            try:
                job = conn.execute(
                    "SELECT j.id,c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                    (context_id,),
                ).fetchone()
            finally:
                conn.close()
            if job:
                selected_payload["client"] = job["client"]
                selected_payload["job"] = job["job"]
                selected_payload["position"] = _copilot_job_evidence(self, int(context_id))
                references.append({"type": "job", "id": context_id, "label": job["job"], "subtitle": job["client"]})

        memories = self.search_memories(message, context_type=context_type, context_id=context_id)

        # ---- 工具调用 Agent 循环 ----
        response_mode = "floating_compact" if floating_compact else "default"
        response_detail = _copilot_response_detail(message)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": json.dumps({
                "question": message,
                "response_mode": response_mode,
                "response_detail": response_detail,
                "conversation": self._copilot_conversation_context(stable_session_id, conversation_history),
                "selected_context": selected_payload,
                "dashboard": {"summary": dashboard.get("summary", {}), "top_actions": dashboard.get("top_actions", [])[:5]},
                "approved_memories": memories.get("memories") or [] if memories.get("mode") == "active" else [],
            }, ensure_ascii=False)},
        ]

        tool_results: list[dict[str, Any]] = []
        final_answer = ""
        executed_calls: set[tuple[str, str]] = set()

        for round_num in range(_MAX_TOOL_ROUNDS):
            request_payload = {
                "question": message,
                "response_mode": response_mode,
                "response_detail": response_detail,
                "selected_context": selected_payload,
                "dashboard": {"summary": dashboard.get("summary", {}), "tool_round": round_num + 1},
            }
            response = self.llm.copilot_with_tools(request_payload, COPILOT_TOOLS, messages=messages)

            # 如果没有工具调用，直接用 content
            if not response.get("tool_calls"):
                final_answer = response.get("content", "")
                break

            messages.append({
                "role": "assistant",
                "content": response.get("content") or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                    }
                    for tc in response["tool_calls"]
                ],
            })
            new_call_executed = False
            for tc in response["tool_calls"]:
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                call_key = (tool_name, json.dumps(tool_args, sort_keys=True, ensure_ascii=False))
                if call_key in executed_calls:
                    result = {"success": False, "error": "本轮已返回相同查询结果，请直接据此作答。"}
                else:
                    executed_calls.add(call_key)
                    new_call_executed = True
                    executor = TOOL_EXECUTORS.get(tool_name)
                    if executor:
                        try:
                            result = executor(str(self.db_path), **tool_args)
                        except Exception as exc:
                            result = {"success": False, "error": str(exc)}
                    else:
                        result = {"success": False, "error": f"未知工具: {tool_name}"}
                tool_results.append({"tool": tool_name, "args": tool_args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
                references.append({
                    "type": "tool_result",
                    "id": tc.get("id", ""),
                    "label": f"🔧 {tool_name}",
                    "subtitle": "成功" if result.get("success") else str(result.get("error", ""))[:80],
                })

            if not new_call_executed or round_num == _MAX_TOOL_ROUNDS - 1:
                final = self.llm.copilot_with_tools(request_payload, COPILOT_TOOLS, messages=messages, allow_tools=False)
                final_answer = final.get("content", "") or "已完成查询，但暂未生成可用结论。"
                break

        if not final_answer:
            final_answer = "已执行工具查询，请查看上方结果。"

        agent_assessment = selected_payload.get("assessment")
        if (
            context_type == "candidate"
            and isinstance(agent_assessment, dict)
            and agent_assessment.get("criteria")
            and _candidate_evidence_question(message)
        ):
            final_answer = _format_candidate_evidence_answer(agent_assessment)

        # ---- 后处理（与 _copilot_impl 一致） ----
        business_focus = self._persist_copilot_focus(
            stable_session_id, message, selected_payload,
            structured=selected_payload, conflicts=focus_conflicts,
        )
        # 策略建议结构化（与 _copilot_impl 同语义）：回答含可落地策略建议时出 patch
        strategy_patch = _build_strategy_patch(self, message, final_answer, selected_payload)
        assistant_structured = {
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": [],
            "tool_calls": tool_results,
            "business_focus": business_focus,
            "model_participation": {
                "mode": "model_tools",
                "label": "模型生成 + 工具证据",
                "model": self.llm.model,
            },
        }
        if strategy_patch:
            assistant_structured["strategy_patch"] = strategy_patch
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO agent_copilot_messages (session_id,context_type,context_id,role,content,structured_json) VALUES (?,?,?,?,?,?)""",
                [
                    (stable_session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                    (stable_session_id, context_type, context_id, "assistant", final_answer, _dumps(assistant_structured)),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            self._maybe_summarize_copilot_conversation(stable_session_id)
        except Exception:
            pass
        return {
            "ok": True,
            "session_id": stable_session_id,
            "answer": final_answer,
            "context": {"type": context_type, "id": context_id},
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": [],
            "tool_calls": tool_results,
            "business_focus": business_focus,
            "model_participation": assistant_structured["model_participation"],
            "memory": {"mode": memories.get("mode"), "hits": len(memories.get("memories") or [])},
            "proactive_suggestions": generate_proactive_suggestions(str(self.db_path)),
            "strategy_patch": strategy_patch,
        }
