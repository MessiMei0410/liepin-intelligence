from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from .batch_stop import apply_batch_stop, batch_stop_summary


COMMAND_TTL_MINUTES = 30
PREFLIGHT_TTL_SECONDS = 300
STOP_STAGE_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _command_hash(command: dict[str, Any], snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_dumps({"command": command, "snapshot": snapshot}).encode("utf-8")).hexdigest()


def _public_command(row: Any) -> dict[str, Any]:
    command = _loads(row["command_json"], {})
    return {
        "version": "copilot_command_v1",
        "command_id": str(row["command_id"]),
        "session_id": str(row["session_id"]),
        "command_type": str(row["command_type"]),
        "status": str(row["status"]),
        "source_message": str(row["source_message"]),
        "target": _loads(row["target_json"], {}),
        "operations": list(command.get("operations") or []),
        "condition_version": int(row["condition_version"] or 0),
        "command_hash": str(row["command_hash"]),
        "snapshot": _loads(row["snapshot_json"], {}),
        "impact": _loads(row["impact_json"], {}),
        "requires_r3": bool(command.get("requires_r3")),
        "source_command_id": str(command.get("source_command_id") or ""),
        "expires_at": str(row["expires_at"] or ""),
        "workflow_id": str(row["workflow_id"] or ""),
        "result": _loads(row["result_json"], {}),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def interaction_card_for_command(command: dict[str, Any], receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the compact card projection used by restored and live messages."""
    target = dict(command.get("target") or {})
    command_type = str(command.get("command_type") or "")
    labels = {
        "recommendation_report": "生成推荐报告",
        "candidate_batch_stop": "停止推进候选人",
        "workflow_create": "创建寻访工作流",
    }
    impact = dict(command.get("impact") or {})
    count = int(impact.get("affected_count") or 0)
    unit = str(impact.get("unit") or "")
    if receipt:
        state = "result"
        impact_text = str(receipt.get("summary") or "本轮已完成服务端回查")
    else:
        state = "pending"
        impact_text = (
            f"确认后生成 {count or 1} 份报告草稿，不会自动对外发送" if unit == "report" else
            f"确认后创建 {count or 1} 个工作流，外部寻访仍需 R3 审批" if unit == "workflow" else
            f"确认后修改 {count or 0} 条候选人记录"
        )
    return {
        "version": "interaction_card_v1", "show": True, "state": state,
        "target": target, "action": command_type,
        "action_label": labels.get(command_type, command_type or "业务操作"),
        "impact": impact, "impact_text": impact_text,
        "key_conditions": [], "requires_r3": bool(command.get("requires_r3")),
        "pending_command": None if receipt else command,
        "execution_receipt": receipt,
        "details": {
            "objective": str(command.get("source_message") or ""),
            "operations": list(command.get("operations") or []),
            "command_hash": str(command.get("command_hash") or ""),
        },
    }


def _sync_command_message(self, command: dict[str, Any], receipt: dict[str, Any] | None = None) -> None:
    conn = self._connect()
    try:
        rows = conn.execute(
            "SELECT id,structured_json FROM agent_copilot_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC",
            (str(command.get("session_id") or ""),),
        ).fetchall()
        matched = False
        for row in rows:
            structured = _loads(row["structured_json"], {})
            pending = structured.get("pending_command") if isinstance(structured, dict) else {}
            if str((pending or {}).get("command_id") or "") != str(command.get("command_id") or ""):
                continue
            is_pending = str(command.get("status") or "") == "pending"
            structured["pending_command"] = command if is_pending else None
            if not is_pending:
                structured["confirmed_command"] = command
            if receipt:
                structured["execution_receipt"] = receipt
                structured["interaction_card"] = interaction_card_for_command(command, receipt)
                if isinstance(receipt.get("references"), list):
                    structured["references"] = receipt["references"]
                if isinstance(receipt.get("suggested_actions"), list):
                    structured["suggested_actions"] = receipt["suggested_actions"]
            conn.execute(
                "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?", (_dumps(structured), row["id"])
            )
            conn.commit()
            matched = True
            break
        # A refreshed command deliberately replaces an expired card.  It has no
        # new chat turn, so bind it to the most recent assistant response.
        if not matched and str(command.get("status") or "") == "pending" and rows:
            row = rows[0]
            structured = _loads(row["structured_json"], {})
            if not isinstance(structured, dict):
                structured = {}
            previous_card = (
                dict(structured.get("interaction_card"))
                if isinstance(structured.get("interaction_card"), dict)
                else {}
            )
            refreshed_card = interaction_card_for_command(command)
            if previous_card:
                previous_details = (
                    dict(previous_card.get("details"))
                    if isinstance(previous_card.get("details"), dict)
                    else {}
                )
                refreshed_details = dict(refreshed_card.get("details") or {})
                refreshed_card = {
                    **previous_card,
                    **refreshed_card,
                    "key_conditions": list(previous_card.get("key_conditions") or []),
                    "details": {**previous_details, **refreshed_details},
                }
            structured["pending_command"] = command
            structured["interaction_card"] = refreshed_card
            conn.execute(
                "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?", (_dumps(structured), row["id"])
            )
            conn.commit()
        pending_state = command if str(command.get("status") or "") == "pending" else {}
        state_row = conn.execute(
            "SELECT state_json FROM agent_copilot_state WHERE session_id=?",
            (str(command.get("session_id") or ""),),
        ).fetchone()
        if state_row:
            state = _loads(state_row["state_json"], {})
            state["pending_command"] = pending_state
            conn.execute(
                "UPDATE agent_copilot_state SET state_json=?,updated_at=datetime('now','localtime') WHERE session_id=?",
                (_dumps(state), str(command.get("session_id") or "")),
            )
        focus_row = conn.execute(
            "SELECT focus_json FROM agent_copilot_focus WHERE session_id=?",
            (str(command.get("session_id") or ""),),
        ).fetchone()
        if focus_row:
            focus = _loads(focus_row["focus_json"], {})
            conversation_state = focus.get("conversation_state") if isinstance(focus.get("conversation_state"), dict) else {}
            conversation_state["pending_command"] = pending_state
            focus["conversation_state"] = conversation_state
            conn.execute(
                "UPDATE agent_copilot_focus SET focus_json=?,updated_at=datetime('now','localtime') WHERE session_id=?",
                (_dumps(focus), str(command.get("session_id") or "")),
            )
        conn.commit()
    finally:
        conn.close()


def _sync_created_workflow_focus(
    self,
    command: dict[str, Any],
    created: dict[str, Any],
    *,
    source_message: str,
) -> None:
    workflow = dict(created.get("workflow") or {})
    goal = dict(created.get("goal") or {})
    plan_ref = dict(created.get("plan_ref") or {})
    workflow_id = str(workflow.get("workflow_id") or "")
    if not workflow_id:
        return
    command_json = dict(command.get("command_json") or {})
    inputs = dict(command_json.get("inputs") or {})
    goal_context = dict(inputs.get("context") or {})
    understanding = dict(goal_context.get("intent_understanding") or {})
    action = str(understanding.get("action") or "candidate_sourcing")
    target = dict(command.get("target") or {})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    workflow_ref = {
        "workflow_id": workflow_id,
        "status": str(workflow.get("status") or "planned"),
        "version": int(plan_ref.get("version") or 1),
        "plan_hash": str(plan_ref.get("plan_hash") or ""),
        "action": action,
        "objective": str(goal.get("objective") or inputs.get("objective") or ""),
        "locked_constraints": list(goal_context.get("locked_constraints") or []),
        "constraint_ledger": list(goal_context.get("constraint_ledger") or []),
        "source_message": source_message,
    }
    conn = self._connect()
    try:
        state_row = conn.execute(
            "SELECT state_json FROM agent_copilot_state WHERE session_id=?",
            (str(command.get("session_id") or ""),),
        ).fetchone()
        state = _loads(state_row["state_json"], {}) if state_row else {}
        state["active_goal"] = {
            "action": action,
            "objective": workflow_ref["objective"],
            "source_quote": source_message,
            "status": workflow_ref["status"],
            "target": target,
            "updated_at": now,
            "workflow_id": workflow_id,
        }
        state["pending_plan"] = dict(workflow_ref)
        state["pending_command"] = {}
        state["updated_at"] = now
        if state_row:
            conn.execute(
                "UPDATE agent_copilot_state SET state_json=?,updated_at=? WHERE session_id=?",
                (_dumps(state), now, str(command.get("session_id") or "")),
            )
        focus_row = conn.execute(
            "SELECT focus_json FROM agent_copilot_focus WHERE session_id=?",
            (str(command.get("session_id") or ""),),
        ).fetchone()
        if focus_row:
            focus = _loads(focus_row["focus_json"], {})
            focus["action"] = action
            focus["objective"] = workflow_ref["objective"]
            focus["current_workflow"] = dict(workflow_ref)
            focus["pending_workflow"] = dict(workflow_ref)
            focus["conversation_state"] = state
            focus["updated_at"] = now
            conn.execute(
                "UPDATE agent_copilot_focus SET focus_json=?,updated_at=? WHERE session_id=?",
                (_dumps(focus), now, str(command.get("session_id") or "")),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_command(
    self,
    *,
    session_id: str,
    source_message: str,
    command_type: str,
    target: dict[str, Any],
    command: dict[str, Any],
    snapshot: dict[str, Any],
    impact: dict[str, Any],
    condition_version: int,
) -> dict[str, Any]:
    command_hash = _command_hash(command, snapshot)
    command_id = f"cmd_{secrets.token_hex(12)}"
    expires_at = (datetime.now() + timedelta(minutes=COMMAND_TTL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    conn = self._connect()
    superseded_ids: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        superseded_ids = [
            str(row["command_id"])
            for row in conn.execute(
                "SELECT command_id FROM agent_copilot_commands WHERE session_id=? AND status='pending'",
                (session_id,),
            ).fetchall()
        ]
        conn.execute(
            "UPDATE agent_copilot_commands SET status='superseded',updated_at=datetime('now','localtime') "
            "WHERE session_id=? AND status='pending'",
            (session_id,),
        )
        conn.execute(
            """
            INSERT INTO agent_copilot_commands
            (command_id,session_id,source_message,command_type,target_json,command_json,
             snapshot_json,impact_json,condition_version,command_hash,status,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)
            """,
            (
                command_id, session_id, source_message, command_type, _dumps(target),
                _dumps(command), _dumps(snapshot), _dumps(impact), int(condition_version),
                command_hash, expires_at,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
        created = _public_command(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    for superseded_id in superseded_ids:
        try:
            superseded = get_copilot_command(self, superseded_id)["command"]
            _sync_command_message(self, superseded)
        except LookupError:
            pass
    return created


def create_batch_stop_command(
    self,
    *,
    session_id: str,
    source_message: str,
    job_id: int,
    items: list[dict[str, Any]],
    condition_version: int = 0,
) -> dict[str, Any]:
    if not session_id:
        raise ValueError("缺少会话，不能创建待确认命令")
    if not items:
        raise ValueError("没有需要停止推进的人选")
    conn = self._connect()
    try:
        placeholders = ",".join("?" for _ in items)
        rows = conn.execute(
            f"SELECT id,job_id,clean_stage FROM job_candidates WHERE id IN ({placeholders})",
            tuple(int(item["jc_id"]) for item in items),
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}
        snapshot_items: list[dict[str, Any]] = []
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            jc_id = int(item.get("jc_id") or 0)
            row = by_id.get(jc_id)
            if row is None or int(row["job_id"] or 0) != int(job_id):
                raise ValueError(f"人选 {jc_id} 不属于当前岗位，命令未创建")
            stage = str(row["clean_stage"] or "")
            if any(token in stage for token in STOP_STAGE_TOKENS):
                continue
            normalized = dict(item)
            normalized["jc_id"] = jc_id
            normalized_items.append(normalized)
            snapshot_items.append({"jc_id": jc_id, "job_id": int(job_id), "stage": stage})
        if not normalized_items:
            raise ValueError("当前筛选结果中没有可停止推进的人选")

        reasons: dict[str, int] = {}
        for item in normalized_items:
            label = str(item.get("stop_reason_label") or "其他")
            reasons[label] = reasons.get(label, 0) + 1
        target = {"type": "job", "id": int(job_id)}
        command = {
            "operations": [
                {"order": 1, "action": "filter_candidates", "effect": "read", "label": "筛选当前岗位候选池"},
                {"order": 2, "action": "batch_stop", "effect": "internal_write", "label": "停止不匹配人选推进", "depends_on": [1]},
                {"order": 3, "action": "list_candidates", "effect": "read", "label": "回查并返回更新后名单", "depends_on": [2]},
            ],
            "items": normalized_items,
            "requires_r3": False,
        }
        snapshot = {"job_id": int(job_id), "items": snapshot_items}
        impact = {
            "affected_count": len(normalized_items),
            "reason_distribution": reasons,
            "summary": batch_stop_summary(normalized_items),
        }
    except Exception:
        raise
    finally:
        conn.close()
    return _insert_command(
        self,
        session_id=session_id,
        source_message=source_message,
        command_type="candidate_batch_stop",
        target=target,
        command=command,
        snapshot=snapshot,
        impact=impact,
        condition_version=condition_version,
    )


def create_recommendation_report_command(
    self,
    *,
    session_id: str,
    source_message: str,
    candidate_id: int | None = None,
    attachment_candidate: dict[str, Any] | None = None,
    condition_version: int = 0,
    requires_r3: bool = False,
) -> dict[str, Any]:
    if not session_id:
        raise ValueError("缺少会话，不能创建待确认命令")
    attachment = dict(attachment_candidate or {})
    conn = self._connect()
    try:
        if candidate_id:
            row = conn.execute(
                """
                SELECT jc.id,jc.job_id,jc.clean_stage,p.display_name,c.name AS client,j.title AS job
                  FROM job_candidates jc
                  JOIN people p ON p.id=jc.person_id
                  LEFT JOIN jobs j ON j.id=jc.job_id
                  LEFT JOIN clients c ON c.id=j.client_id
                 WHERE jc.id=?
                """,
                (int(candidate_id),),
            ).fetchone()
            if row is None:
                raise ValueError("候选人不存在，不能生成报告命令")
            name = str(row["display_name"] or f"人选 #{candidate_id}")
            target = {
                "type": "candidate", "id": int(candidate_id), "label": name,
                "client": str(row["client"] or ""), "job": str(row["job"] or ""),
            }
            snapshot = {
                "candidate": {
                    "jc_id": int(candidate_id), "job_id": int(row["job_id"] or 0),
                    "stage": str(row["clean_stage"] or ""),
                }
            }
            inputs: dict[str, Any] = {}
        else:
            attachment_id = str(attachment.get("attachment_id") or "")
            if not attachment_id:
                raise ValueError("附件缺少可验证标识，不能生成报告命令")
            row = conn.execute(
                """
                SELECT attachment_id,file_name,content_sha256,expires_at,status
                  FROM agent_copilot_attachments
                 WHERE attachment_id=? AND session_id=?
                """,
                (attachment_id, session_id),
            ).fetchone()
            if row is None:
                raise ValueError("附件已失效或不属于当前会话，请重新上传")
            name = str(attachment.get("name") or "附件人选")
            target = {
                "type": "attachment_candidate", "id": attachment_id, "label": name,
                "client": str(attachment.get("customer") or ""),
                "job": str(attachment.get("position") or ""),
            }
            snapshot = {
                "attachment": {
                    "attachment_id": attachment_id,
                    "content_sha256": str(row["content_sha256"] or ""),
                    "file_name": str(row["file_name"] or ""),
                }
            }
            inputs = {
                "attachment_id": attachment_id,
                "name": name,
                "customer": str(attachment.get("customer") or ""),
                "position": str(attachment.get("position") or ""),
            }
    finally:
        conn.close()
    command = {
        "operations": [
            {"order": 1, "action": "validate_candidate", "effect": "read", "label": "核验报告对象和前置条件"},
            {"order": 2, "action": "generate_recommendation_report", "effect": "internal_write", "label": "生成推荐报告草稿", "depends_on": [1]},
            {"order": 3, "action": "return_report", "effect": "read", "label": "回查并返回报告结果", "depends_on": [2]},
        ],
        "inputs": inputs,
        "requires_r3": bool(requires_r3),
    }
    impact = {
        "affected_count": 1,
        "unit": "report",
        "summary": f"为{name}生成推荐报告草稿",
    }
    return _insert_command(
        self,
        session_id=session_id,
        source_message=source_message,
        command_type="recommendation_report",
        target=target,
        command=command,
        snapshot=snapshot,
        impact=impact,
        condition_version=condition_version,
    )


def create_workflow_command(
    self,
    *,
    session_id: str,
    source_message: str,
    objective: str,
    context: dict[str, Any],
    condition_version: int = 0,
    start_after_create: bool = False,
    requires_r3: bool = False,
) -> dict[str, Any]:
    if not session_id:
        raise ValueError("缺少会话，不能创建待确认命令")
    target_type = str(context.get("type") or "")
    target_id = int(context.get("id") or 0)
    if target_type not in {"job", "candidate"} or not target_id:
        raise ValueError("工作流创建前必须唯一定位岗位或候选人")
    conn = self._connect()
    try:
        if target_type == "job":
            row = conn.execute(
                """
                SELECT j.id,j.title,j.location,j.status,j.updated_at,c.name AS client
                  FROM jobs j LEFT JOIN clients c ON c.id=j.client_id
                 WHERE j.id=?
                """,
                (target_id,),
            ).fetchone()
            if row is None:
                raise ValueError("岗位不存在，不能创建工作流命令")
            target = {
                "type": "job", "id": target_id, "label": str(row["title"] or f"岗位 #{target_id}"),
                "client": str(row["client"] or ""),
            }
            snapshot = {
                "job": {
                    "id": target_id, "title": str(row["title"] or ""),
                    "client": str(row["client"] or ""), "location": str(row["location"] or ""),
                    "status": str(row["status"] or ""), "updated_at": str(row["updated_at"] or ""),
                }
            }
        else:
            row = conn.execute(
                """
                SELECT jc.id,jc.job_id,jc.clean_stage,p.display_name,c.name AS client,j.title AS job
                  FROM job_candidates jc
                  JOIN people p ON p.id=jc.person_id
                  LEFT JOIN jobs j ON j.id=jc.job_id
                  LEFT JOIN clients c ON c.id=j.client_id
                 WHERE jc.id=?
                """,
                (target_id,),
            ).fetchone()
            if row is None:
                raise ValueError("候选人不存在，不能创建工作流命令")
            target = {
                "type": "candidate", "id": target_id,
                "label": str(row["display_name"] or f"人选 #{target_id}"),
                "client": str(row["client"] or ""), "job": str(row["job"] or ""),
            }
            snapshot = {
                "candidate": {
                    "jc_id": target_id, "job_id": int(row["job_id"] or 0),
                    "stage": str(row["clean_stage"] or ""),
                }
            }
    finally:
        conn.close()
    command = {
        "operations": [
            {"order": 1, "action": "validate_workflow_context", "effect": "read", "label": "核验客户、岗位和条件版本"},
            {"order": 2, "action": "create_workflow", "effect": "internal_write", "label": "创建可审计工作流", "depends_on": [1]},
            {"order": 3, "action": "return_workflow", "effect": "read", "label": "回查并返回工作流卡", "depends_on": [2]},
        ],
        "inputs": {
            "objective": str(objective or "").strip(),
            "context": dict(context),
            "start_after_create": bool(start_after_create),
        },
        "requires_r3": bool(requires_r3),
    }
    impact = {
        "affected_count": 1,
        "unit": "workflow",
        "summary": f"为{target['client']}{target['label']}创建工作流",
    }
    return _insert_command(
        self,
        session_id=session_id,
        source_message=source_message,
        command_type="workflow_create",
        target=target,
        command=command,
        snapshot=snapshot,
        impact=impact,
        condition_version=condition_version,
    )


def get_copilot_command(self, command_id: str) -> dict[str, Any]:
    conn = self._connect()
    try:
        row = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise LookupError("待确认命令不存在")
        return {"ok": True, "command": _public_command(row)}
    finally:
        conn.close()


def _validate_snapshot(conn: Any, command: dict[str, Any]) -> None:
    if command.get("command_type") == "workflow_create":
        snapshot = command.get("snapshot") or {}
        expected_job = snapshot.get("job") or {}
        expected_candidate = snapshot.get("candidate") or {}
        if expected_job:
            row = conn.execute(
                """
                SELECT j.id,j.title,j.location,j.status,j.updated_at,c.name AS client
                  FROM jobs j LEFT JOIN clients c ON c.id=j.client_id
                 WHERE j.id=?
                """,
                (int(expected_job.get("id") or 0),),
            ).fetchone()
            if row is None:
                raise ValueError("岗位已不存在，请重新发起工作流命令")
            current = {
                "id": int(row["id"]), "title": str(row["title"] or ""),
                "client": str(row["client"] or ""), "location": str(row["location"] or ""),
                "status": str(row["status"] or ""), "updated_at": str(row["updated_at"] or ""),
            }
            if current != expected_job:
                raise ValueError("岗位信息已变化，请按最新上下文重新发起工作流命令")
            return
        row = conn.execute(
            "SELECT job_id,clean_stage FROM job_candidates WHERE id=?",
            (int(expected_candidate.get("jc_id") or 0),),
        ).fetchone()
        if row is None:
            raise ValueError("候选人已不存在，请重新发起工作流命令")
        current_candidate = {
            "jc_id": int(expected_candidate.get("jc_id") or 0),
            "job_id": int(row["job_id"] or 0),
            "stage": str(row["clean_stage"] or ""),
        }
        if current_candidate != expected_candidate:
            raise ValueError("候选人阶段或归属已变化，请重新发起工作流命令")
        return
    if command.get("command_type") == "recommendation_report":
        candidate = (command.get("snapshot") or {}).get("candidate") or {}
        if candidate:
            row = conn.execute(
                "SELECT job_id,clean_stage FROM job_candidates WHERE id=?",
                (int(candidate.get("jc_id") or 0),),
            ).fetchone()
            if row is None or int(row["job_id"] or 0) != int(candidate.get("job_id") or 0):
                raise ValueError("报告对象已变化，请重新选择候选人")
            if str(row["clean_stage"] or "") != str(candidate.get("stage") or ""):
                raise ValueError("候选人阶段已变化，请重新确认报告命令")
            return
        attachment = (command.get("snapshot") or {}).get("attachment") or {}
        row = conn.execute(
            """
            SELECT content_sha256 FROM agent_copilot_attachments
             WHERE attachment_id=? AND session_id=? AND expires_at>datetime('now','localtime')
            """,
            (str(attachment.get("attachment_id") or ""), str(command.get("session_id") or "")),
        ).fetchone()
        if row is None or str(row["content_sha256"] or "") != str(attachment.get("content_sha256") or ""):
            raise ValueError("附件已失效或内容已变化，请重新上传后确认")
        return
    job_id = int((command.get("target") or {}).get("id") or 0)
    for item in list((command.get("snapshot") or {}).get("items") or []):
        row = conn.execute(
            "SELECT job_id,clean_stage FROM job_candidates WHERE id=?", (int(item.get("jc_id") or 0),)
        ).fetchone()
        if row is None or int(row["job_id"] or 0) != job_id:
            raise ValueError("命令对象已变化，请重新筛选后确认")
        if str(row["clean_stage"] or "") != str(item.get("stage") or ""):
            raise ValueError("候选人阶段已变化，请重新筛选后确认")


def preflight_copilot_command(self, command_id: str) -> dict[str, Any]:
    conn = self._connect()
    try:
        row = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise LookupError("待确认命令不存在")
        command = _public_command(row)
        if command["status"] != "pending":
            raise ValueError(f"命令当前状态为 {command['status']}，不能再次预检")
        if str(row["expires_at"] or "") <= datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
            conn.execute("UPDATE agent_copilot_commands SET status='expired' WHERE command_id=?", (command_id,))
            conn.commit()
            raise ValueError("命令已过期，请重新发起")
        expected_revision = int(command.get("condition_version") or 0)
        state_row = conn.execute(
            "SELECT revision FROM agent_copilot_state WHERE session_id=?",
            (str(command.get("session_id") or ""),),
        ).fetchone()
        current_revision = int(state_row["revision"] or 0) if state_row else 0
        if expected_revision and current_revision != expected_revision:
            conn.execute(
                "UPDATE agent_copilot_commands SET status='superseded',updated_at=datetime('now','localtime') "
                "WHERE command_id=? AND status='pending'",
                (command_id,),
            )
            conn.commit()
            _sync_command_message(self, {**command, "status": "superseded"})
            raise ValueError("会话条件或对象已变化，请按最新上下文重新发起命令")
        _validate_snapshot(conn, command)
    finally:
        conn.close()
    token = secrets.token_urlsafe(32)
    self._copilot_command_confirmations[token] = {
        "command_id": command_id,
        "command_hash": command["command_hash"],
        "expires_at": time.time() + PREFLIGHT_TTL_SECONDS,
    }
    return {"ok": True, "command": command, "confirmation_token": token, "expires_in": PREFLIGHT_TTL_SECONDS}


def refresh_copilot_command(
    self,
    command_id: str,
    *,
    request_id: str,
    expected_command_hash: str,
) -> dict[str, Any]:
    """Re-preflight an expired command without performing its business operation."""
    if not request_id:
        raise ValueError("缺少刷新请求标识")
    conn = self._connect()
    try:
        # Serialize the complete refresh so concurrent retries cannot fork into
        # multiple replacement commands for one idempotency key.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise LookupError("待确认命令不存在")
        command = _public_command(row)
        if str(command.get("command_hash") or "") != str(expected_command_hash or ""):
            raise ValueError("命令内容已变化，请重新输入业务指令")
        if command["status"] == "pending" and str(row["expires_at"] or "") > datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
            conn.commit()
            return {"ok": True, "command": command, "refreshed": False}
        replacement = conn.execute(
            """
            SELECT * FROM agent_copilot_commands
             WHERE session_id=? AND json_extract(command_json,'$.source_command_id')=?
               AND json_extract(command_json,'$.refresh_request_id')=?
             ORDER BY id DESC LIMIT 1
            """,
            (command["session_id"], command_id, request_id),
        ).fetchone()
        if replacement:
            conn.commit()
            return {"ok": True, "command": _public_command(replacement), "refreshed": True, "replayed": True}
        if command["status"] not in {"pending", "expired"}:
            raise ValueError(f"命令当前状态为 {command['status']}，不能重新预检")
        newer_pending = conn.execute(
            "SELECT command_id FROM agent_copilot_commands WHERE session_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (command["session_id"],),
        ).fetchone()
        if newer_pending and str(newer_pending["command_id"]) != command_id:
            raise ValueError("会话已有更新的待确认命令，请使用最新行动卡")
        expected_revision = int(command.get("condition_version") or 0)
        state_row = conn.execute(
            "SELECT revision FROM agent_copilot_state WHERE session_id=?", (command["session_id"],)
        ).fetchone()
        current_revision = int(state_row["revision"] or 0) if state_row else 0
        if expected_revision and current_revision != expected_revision:
            raise ValueError("会话条件或对象已变化，请重新输入或上传附件")
        _validate_snapshot(conn, command)
        conn.execute(
            "UPDATE agent_copilot_commands SET status='expired',updated_at=datetime('now','localtime') WHERE command_id=? AND status='pending'",
            (command_id,),
        )
        command_json = dict(_loads(row["command_json"], {}))
        command_json["source_command_id"] = command_id
        command_json["refresh_request_id"] = request_id
        snapshot = dict(command["snapshot"])
        replacement_id = f"cmd_{secrets.token_hex(12)}"
        replacement_expires_at = (
            datetime.now() + timedelta(minutes=COMMAND_TTL_MINUTES)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO agent_copilot_commands
            (command_id,session_id,source_message,command_type,target_json,command_json,
             snapshot_json,impact_json,condition_version,command_hash,status,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)
            """,
            (
                replacement_id,
                command["session_id"],
                command["source_message"],
                command["command_type"],
                _dumps(command["target"]),
                _dumps(command_json),
                _dumps(snapshot),
                _dumps(command["impact"]),
                int(command.get("condition_version") or 0),
                _command_hash(command_json, snapshot),
                replacement_expires_at,
            ),
        )
        conn.commit()
        replacement_row = conn.execute(
            "SELECT * FROM agent_copilot_commands WHERE command_id=?", (replacement_id,)
        ).fetchone()
        replacement_command = _public_command(replacement_row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _sync_command_message(self, {**command, "status": "expired"})
    _sync_command_message(self, replacement_command)
    return {"ok": True, "command": replacement_command, "refreshed": True}


def decide_copilot_command(
    self,
    command_id: str,
    *,
    decision: str,
    confirmation_token: str,
    note: str = "",
) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise ValueError("decision 必须是 approve 或 reject")
    # Keep a successful grant until its short expiry so transport retries with the
    # same token can receive the stored receipt instead of creating a duplicate.
    confirmation = self._copilot_command_confirmations.get(confirmation_token)
    if not confirmation or confirmation.get("command_id") != command_id:
        raise ValueError("确认令牌无效，请重新预检")
    if float(confirmation.get("expires_at") or 0) < time.time():
        raise ValueError("确认令牌已过期，请重新预检")

    with self._lock:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                raise LookupError("待确认命令不存在")
            command = _public_command(row)
            if command["status"] != "pending":
                if command["status"] == "executed":
                    return {"ok": True, "command": command, "receipt": command.get("result") or {}, "replayed": True}
                raise ValueError(f"命令当前状态为 {command['status']}，不能执行")
            if command["command_hash"] != confirmation.get("command_hash"):
                raise ValueError("命令内容已变化，请重新预检")
            expected_revision = int(command.get("condition_version") or 0)
            state_row = conn.execute(
                "SELECT revision FROM agent_copilot_state WHERE session_id=?",
                (str(command.get("session_id") or ""),),
            ).fetchone()
            current_revision = int(state_row["revision"] or 0) if state_row else 0
            if expected_revision and current_revision != expected_revision:
                conn.execute(
                    "UPDATE agent_copilot_commands SET status='superseded',updated_at=datetime('now','localtime') "
                    "WHERE command_id=? AND status='pending'",
                    (command_id,),
                )
                conn.commit()
                _sync_command_message(self, {**command, "status": "superseded"})
                raise ValueError("预检后会话条件或对象已变化，请重新发起命令")
            _validate_snapshot(conn, command)
            command_json = _loads(row["command_json"], {})
        finally:
            conn.close()

        if decision == "reject":
            self._copilot_command_confirmations.pop(confirmation_token, None)
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE agent_copilot_commands SET status='rejected',updated_at=datetime('now','localtime') WHERE command_id=? AND status='pending'",
                    (command_id,),
                )
                conn.commit()
            finally:
                conn.close()
            rejected = get_copilot_command(self, command_id)
            _sync_command_message(self, rejected["command"])
            return rejected

        target = command.get("target") or {}
        try:
            if command.get("command_type") == "candidate_batch_stop":
                items = list(command_json.get("items") or [])
                outcome = apply_batch_stop(
                    self.db_path, int(target.get("id") or 0), items, actor="copilot", source="copilot_command"
                )
                receipt = {
                    "version": "execution_receipt_v1",
                    "state": "已完成",
                    "summary": f"批量停止推进 {int(outcome.get('applied') or 0)} 人（{batch_stop_summary(items)}）",
                    "succeeded": int(outcome.get("applied") or 0),
                    "skipped": int(outcome.get("skipped") or 0),
                    "failed": 0,
                    "verified": True,
                    "scope": target,
                    "reasons": list((command.get("impact") or {}).get("reason_distribution") or {}),
                    "note": note[:500],
                }
            elif command.get("command_type") == "recommendation_report":
                inputs = dict(command_json.get("inputs") or {})
                attachment_id = str(inputs.pop("attachment_id", "") or "")
                context = {"type": "candidate", "id": target.get("id")}
                if attachment_id:
                    conn = self._connect()
                    try:
                        attachment_row = conn.execute(
                            """
                            SELECT file_name,extracted_text FROM agent_copilot_attachments
                             WHERE attachment_id=? AND session_id=? AND expires_at>datetime('now','localtime')
                            """,
                            (attachment_id, str(command.get("session_id") or "")),
                        ).fetchone()
                    finally:
                        conn.close()
                    if attachment_row is None:
                        raise ValueError("附件已失效，请重新上传后生成报告")
                    inputs["attachment_candidate"] = {
                        **inputs,
                        "file_name": str(attachment_row["file_name"] or ""),
                        "resume_text": str(attachment_row["extracted_text"] or "")[:18000],
                    }
                    context = {"type": "candidate", "id": None}
                skill_run = self.execute_skill("recommendation_report", context=context, inputs=inputs)
                if not skill_run.get("ok"):
                    raise RuntimeError(str(skill_run.get("error") or "推荐报告能力执行失败"))
                result = dict(skill_run.get("result") or {})
                blocked = bool(result.get("blocked"))
                artifacts = [dict(item) for item in result.get("artifacts") or [] if isinstance(item, dict)]
                if not blocked and not artifacts:
                    raise RuntimeError("推荐报告能力未返回可核验产物")
                receipt = {
                    "version": "execution_receipt_v1",
                    "state": "流程阻塞" if blocked else "已完成",
                    "summary": str(result.get("summary") or ("推荐报告生成受阻" if blocked else "推荐报告草稿已生成")),
                    "succeeded": 0 if blocked else len(artifacts) or 1,
                    "skipped": 0,
                    "failed": 1 if blocked else 0,
                    "verified": True,
                    "scope": target,
                    "references": list(result.get("references") or []),
                    "suggested_actions": list(result.get("suggested_actions") or []),
                    "artifacts": artifacts,
                    "next_step": (
                        "完成前置核验后重新发起报告命令"
                        if blocked
                        else "如需推荐给客户，仍须通过独立 R3 审批"
                        if command.get("requires_r3")
                        else "发送前请顾问复核报告内容"
                    ),
                    "note": note[:500],
                }
            elif command.get("command_type") == "workflow_create":
                inputs = dict(command_json.get("inputs") or {})
                objective = str(inputs.get("objective") or "").strip()
                context = dict(inputs.get("context") or {})
                context["copilot_command_id"] = command_id
                conn = self._connect()
                try:
                    existing = conn.execute(
                        """
                        SELECT w.workflow_id
                          FROM agent_goals g JOIN agent_workflows w ON w.goal_id=g.goal_id
                         WHERE json_extract(g.context_json,'$.copilot_command_id')=?
                         ORDER BY w.id DESC LIMIT 1
                        """,
                        (command_id,),
                    ).fetchone()
                finally:
                    conn.close()
                created = self.get_workflow(str(existing["workflow_id"])) if existing else self.create_goal(objective, context)
                workflow_id = str((created.get("workflow") or {}).get("workflow_id") or "")
                if not workflow_id:
                    raise RuntimeError("工作流创建后未返回可核验 workflow_id")
                if bool(inputs.get("start_after_create")) and str((created.get("workflow") or {}).get("status") or "") == "planned":
                    plan_ref = dict(created.get("plan_ref") or {})
                    created = self.start_workflow(
                        workflow_id,
                        expected_plan_version=int(plan_ref.get("version") or 1),
                        expected_plan_hash=str(plan_ref.get("plan_hash") or ""),
                    )
                verified = self.get_workflow(workflow_id)
                _sync_created_workflow_focus(self, {**command, "command_json": command_json}, verified, source_message=command["source_message"])
                workflow = dict(verified.get("workflow") or {})
                receipt = {
                    "version": "execution_receipt_v1",
                    "state": "已完成",
                    "summary": f"工作流已创建：{str((verified.get('goal') or {}).get('title') or objective)}",
                    "succeeded": 1, "skipped": 0, "failed": 0, "verified": True,
                    "scope": target, "workflow_id": workflow_id,
                    "references": [{
                        "type": "workflow", "id": workflow_id,
                        "label": str((verified.get("goal") or {}).get("title") or "工作流"),
                        "subtitle": str(workflow.get("current_stage") or workflow.get("status") or "已创建"),
                    }],
                    "suggested_actions": [{"type": "open_workflow", "id": workflow_id, "label": "打开工作流"}],
                    "next_step": (
                        "内部步骤已开始；外部寻访仍须在工作流卡中完成独立 R3 审批"
                        if bool(inputs.get("start_after_create"))
                        else "打开工作流核对计划；外部寻访仍须独立 R3 审批"
                    ),
                    "note": note[:500],
                }
            else:
                raise ValueError(f"暂不支持执行命令类型：{command.get('command_type')}")
            status = "executed"
        except Exception as exc:
            item_count = int((command.get("impact") or {}).get("affected_count") or 1)
            receipt = {
                "version": "execution_receipt_v1", "state": "技术失败", "summary": "业务命令未完成",
                "succeeded": 0, "skipped": 0, "failed": item_count, "verified": True,
                "scope": target, "failure_reason": str(exc)[:500],
            }
            status = "failed"
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_copilot_commands
                   SET status=?,result_json=?,workflow_id=?,confirmed_at=datetime('now','localtime'),
                       executed_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                 WHERE command_id=? AND status='pending'
                """,
                (status, _dumps(receipt), str(receipt.get("workflow_id") or command.get("workflow_id") or ""), command_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM agent_copilot_commands WHERE command_id=?", (command_id,)).fetchone()
        finally:
            conn.close()
        public = _public_command(updated)
        _sync_command_message(self, public, receipt)
        if status == "failed":
            raise ValueError(str(receipt.get("failure_reason") or "命令执行失败"))
        return {"ok": True, "command": public, "receipt": receipt}


def latest_confirmable_copilot_command(self, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    conn = self._connect()
    try:
        latest_message = conn.execute(
            "SELECT role,structured_json FROM agent_copilot_messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if latest_message is None or str(latest_message["role"]) != "assistant":
            return {}
        command_row = conn.execute(
            "SELECT * FROM agent_copilot_commands WHERE session_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _public_command(command_row) if command_row else {}
    finally:
        conn.close()


def supersede_latest_confirmable_copilot_command(self, session_id: str) -> dict[str, Any]:
    command = latest_confirmable_copilot_command(self, session_id)
    command_id = str(command.get("command_id") or "")
    if not command_id:
        return {}
    conn = self._connect()
    try:
        conn.execute(
            """
            UPDATE agent_copilot_commands
               SET status='superseded',updated_at=datetime('now','localtime')
             WHERE command_id=? AND status='pending'
            """,
            (command_id,),
        )
        conn.commit()
    finally:
        conn.close()
    superseded = get_copilot_command(self, command_id)["command"]
    _sync_command_message(self, superseded)
    return superseded
