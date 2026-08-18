from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from .database import connect, json_value, transaction
from .stop_reasons import NEUTRAL_SOURCING_STOP_REASONS, STOP_REASON_LABELS, normalize_stop_reason


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


# 版本化推荐包客户反馈类型枚举（非法值 → 409；label 用于时间线与回执展示）。
PACKAGE_FEEDBACK_TYPE_LABELS = {
    "approved": "客户认可",
    "interview": "安排面试",
    "rejected": "客户否决",
    "hold": "暂缓推进",
    "other": "其他反馈",
}


# P3-b 新旧反馈表口径统一：新表 recommendation_package_feedback 写入时同事务双写旧表
# client_feedback_events，存量报表脚本（generate_workflow_status_report /
# generate_position_dashboard 等约 10 个旧读方）零改动即可读到新口径反馈。
# 反馈类型映射到旧表写方 record_client_feedback.FEEDBACK_OPTIONS 的口径（含 status_after 约定）；
# 新表没有的字段（原因标签/下一步）留空，source='recommendation_package' 留痕，
# 包版本链路经 event_id → candidate_events.raw_json（含 package_id/package_version/feedback_id）可追溯。
LEGACY_FEEDBACK_TYPE_MAP = {
    "approved": ("approved", "client_approved"),
    "interview": ("interviewing", "interviewing"),
    "rejected": ("rejected", "client_rejected"),
    "hold": ("hold", "hold"),
    "other": ("other", ""),
}


# 与 scripts/record_client_feedback.py 的 SCHEMA 保持一致（IF NOT EXISTS，不覆盖既有表）。
LEGACY_CLIENT_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT NOT NULL,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    feedback_type TEXT NOT NULL,
    status_after TEXT,
    reason_tags_json TEXT DEFAULT '[]',
    feedback_detail TEXT,
    next_action TEXT,
    source TEXT DEFAULT 'manual',
    feedback_time TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


# 生命周期一等事件（三期驾驶舱）：面试/Offer/入职统一为候选人结构化时间线事件。
# label 用于时间线摘要与回执；statuses 限定 event_status 合法取值（缺省用 default_status）；
# followup_task/followup_days 定义自动生成的跟进待办（只建内部任务，不自动对外沟通）。
LIFECYCLE_EVENT_TYPES: dict[str, dict[str, Any]] = {
    "interview_scheduled": {"label": "面试安排", "statuses": ("scheduled", "cancelled"), "default_status": "scheduled", "followup_task": "interview_followup", "followup_days": 2},
    "interview_completed": {"label": "面试完成", "statuses": ("completed", "passed", "failed"), "default_status": "completed", "followup_task": "interview_followup", "followup_days": 1},
    "offer_extended": {"label": "Offer 发出", "statuses": ("extended", "withdrawn"), "default_status": "extended", "followup_task": "offer_followup", "followup_days": 2},
    "offer_accepted": {"label": "Offer 已接受", "statuses": ("accepted",), "default_status": "accepted", "followup_task": "onboarding_followup", "followup_days": 7},
    "offer_declined": {"label": "Offer 已拒绝", "statuses": ("declined",), "default_status": "declined", "followup_task": "offer_followup", "followup_days": 1},
    "onboarded": {"label": "确认入职", "statuses": ("recorded",), "default_status": "recorded", "followup_task": "onboarding_followup", "followup_days": 7},
}


STRUCTURED_CANDIDATE_ACTIONS = {
    "confirm_advance": "advance",
    "confirm_stop": "stop",
}


IDEMPOTENCY_LEASE_MINUTES = 5


class IdempotencyConflict(ValueError):
    """The same idempotency key cannot safely execute another action."""


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


class CandidateActionsMixin:
    """候选人动作域：preflight/commit 写链路、幂等原语、寻访调整、
    顾问确认推荐 + 版本化推荐包、生命周期一等事件。

    方法体自 service.py 逐字节迁移（P2-1），语义不变。
    """

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
        replay, is_replay = self.begin_idempotent(
            operation=operation,
            request_id=request_id,
            idempotency_key=idempotency_key,
            payload=payload,
            target_type=target_type,
            target_id=target_id,
        )
        if is_replay:
            return replay, True
        response = self.complete_idempotent(
            operation=operation,
            request_id=request_id,
            idempotency_key=idempotency_key,
            target_type=target_type,
            target_id=target_id,
            action=action,
            actor=actor,
            surface=surface,
        )
        return response, False

    def begin_idempotent(
        self,
        *,
        operation: str,
        request_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        target_type: str,
        target_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """幂等"开始"段：登记处理租约，重放/冲突语义与 execute_idempotent 完全一致。

        命中重放返回 (已登记响应, True)；否则租约已写入、返回 (None, False)，
        调用方随后必须走 complete_idempotent 登记最终结果（成功或 failed 落账）。
        """
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
        return None, False

    def complete_idempotent(
        self,
        *,
        operation: str,
        request_id: str,
        idempotency_key: str,
        target_type: str,
        target_id: str,
        action: Callable[[], dict[str, Any]],
        actor: str = "local",
        surface: str = "asa_web",
    ) -> dict[str, Any]:
        """幂等"完成"段：执行 action 并登记结果（completed + 审计；异常时 failed 落账后原样抛出）。"""
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
        return response

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

    def candidate_update_preflight(self, candidate_id: int, update_type: str) -> dict[str, Any]:
        if update_type not in CANDIDATE_UPDATE_LABELS:
            raise ValueError("unsupported candidate update")
        detail = self.candidate(candidate_id)["candidate"]
        if detail["is_stopped"]:
            raise ValueError("该人选关系已停止推进，不能新增跟进记录")
        token = secrets.token_urlsafe(24)
        expires = datetime.now() + timedelta(minutes=5)
        with self._preflight_lock:
            now = datetime.now()
            self._preflight_tokens = {key: value for key, value in self._preflight_tokens.items() if value[2] > now}
            self._preflight_tokens[token] = (candidate_id, update_type, expires)
        return {"ok": True, "token": token, "expires_at": expires.isoformat(timespec="seconds"), "action": update_type, "candidate": {"id": candidate_id, "name": detail["name"]}}

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
            if action == "review":
                # 评分复核只追加顾问判断事件。候选人可能已推进到联系、推荐等后续阶段，
                # 不能因为补记一次评分复核而把关系倒退回 S1。
                stage = str(row["clean_stage"] or "S1 新增寻访/待复核")
                bucket = str(row["clean_stage"] or "待复核")
                raw_status = str(row["raw_status"] or "pending_review")
            stop_reason = ""
            if action == "stop":
                # R10 停止原因标准化：reason 命中 8 枚举→存枚举值；缺失/未知/自由
                # 文本→存 'other' 并把原文并入备注（不报错阻断，note-only 旧载荷不变）。
                stop_reason, note = normalize_stop_reason(reason, note)
                if stop_reason in NEUTRAL_SOURCING_STOP_REASONS:
                    learning_signal = "stopped_neutral"
            event_reason = note or stage
            if action == "review":
                pass
            elif stop_reason:
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
            if action != "review" and source_candidate_id.isdigit():
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
        if action == "stop":
            # 停止备注 → 寻访调整指令（失败静默，不阻断主流程）。
            try:
                self._analyze_and_store_stop_note_adjustments(candidate_id, stop_reason, note)
            except Exception:
                pass
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

    @staticmethod
    def _fallback_stop_note_adjustments(note: str) -> list[dict[str, Any]]:
        """LLM 失败/返回空时的规则兜底：薪资、公司、城市。"""
        if not note:
            return []
        adjustments: list[dict[str, Any]] = []
        # 薪资数字 → adjust_salary_range
        salary_match = re.search(r"(\d+[\s]*[kwKW万])", note)
        if salary_match:
            adjustments.append({
                "type": "adjust_salary_range",
                "value": f"≤{salary_match.group(1).replace(' ', '')}",
                "rationale": note[:120],
                "confidence": 0.5,
            })
        # 明确公司名（含"公司/厂"）→ exclude_company
        company_match = re.search(r"([^，。；\n]{2,20}?(?:公司|厂))", note)
        if company_match:
            adjustments.append({
                "type": "exclude_company",
                "value": company_match.group(1).strip(),
                "rationale": note[:120],
                "confidence": 0.5,
            })
        # "只考虑/家在/期望"+城市 → add_filter
        city_match = re.search(r"(?:只考虑|家在|期望)\s*([^，。；\n]{1,10})", note)
        if city_match:
            adjustments.append({
                "type": "add_filter",
                "value": f"地点：{city_match.group(1).strip()}",
                "rationale": note[:120],
                "confidence": 0.5,
            })
        return adjustments

    def _analyze_and_store_stop_note_adjustments(
        self, candidate_id: int, stop_reason_code: str, note: str
    ) -> dict[str, Any]:
        """分析停止备注并写入 agent_sourcing_adjustments；失败返回空结果不抛异常。"""
        if not self.agent_service:
            return {"stored": 0, "error": "agent service unavailable"}
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT jc.job_id, jc.person_id,
                       p.display_name, p.current_company, p.current_title, p.city,
                       j.title AS job_title, c.name AS client_name
                FROM job_candidates jc
                JOIN people p ON p.id=jc.person_id
                JOIN jobs j ON j.id=jc.job_id
                JOIN clients c ON c.id=j.client_id
                WHERE jc.id=?
                """,
                (candidate_id,),
            ).fetchone()
            if not row:
                return {"stored": 0, "error": "candidate not found"}
            payload = {
                "note": note or "",
                "stop_reason_code": stop_reason_code or "",
                "stop_reason_label": STOP_REASON_LABELS.get(stop_reason_code, ""),
                "candidate": {
                    "display_name": row["display_name"] or "",
                    "current_company": row["current_company"] or "",
                    "current_title": row["current_title"] or "",
                    "city": row["city"] or "",
                },
                "job": {
                    "job_id": int(row["job_id"]) if row["job_id"] else 0,
                    "title": row["job_title"] or "",
                    "client": row["client_name"] or "",
                },
            }
            try:
                result = self.agent_service.analyze_stop_note(payload)
                adjustments = result.get("adjustments") if isinstance(result, dict) else None
            except Exception:
                adjustments = None
            if not isinstance(adjustments, list) or not adjustments:
                adjustments = self._fallback_stop_note_adjustments(note or "")
            stored = self._persist_stop_note_adjustments(
                conn, int(row["job_id"]) if row["job_id"] else 0, candidate_id, note or "", adjustments
            )
            return {"stored": stored}
        except Exception as exc:
            return {"stored": 0, "error": str(exc)}
        finally:
            conn.close()

    def _persist_stop_note_adjustments(
        self,
        conn: sqlite3.Connection,
        job_id: int,
        candidate_id: int,
        note: str,
        adjustments: list[dict[str, Any]],
    ) -> int:
        """幂等写入调整指令，返回实际插入条数。"""
        if not job_id or not adjustments:
            return 0
        valid_types = {"add_keyword", "remove_keyword", "exclude_company", "add_company", "add_filter", "adjust_salary_range"}
        stored = 0
        rationale = (note or "")[:120]
        for item in adjustments:
            if not isinstance(item, dict):
                continue
            adjust_type = str(item.get("type") or "").strip()
            value = str(item.get("value") or "").strip()
            if adjust_type not in valid_types or not value:
                continue
            dedupe_value = value.lower()
            dedupe_key = f"{job_id}|{adjust_type}|{dedupe_value}"
            confidence = float(item.get("confidence") or 0.5)
            if not 0.0 <= confidence <= 1.0:
                confidence = 0.5
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO agent_sourcing_adjustments
                    (job_id, candidate_id, adjust_type, value, rationale, confidence, dedupe_key)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (job_id, candidate_id, adjust_type, value, rationale, confidence, dedupe_key),
                )
                if cur.rowcount > 0:
                    stored += 1
            except Exception:
                continue
        conn.commit()
        return stored

    def list_sourcing_adjustments(self, job_id: int) -> dict[str, Any]:
        """岗位的停止备注寻访调整列表（含来源候选人姓名）与状态汇总。"""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT a.*, p.display_name AS candidate_name
                FROM agent_sourcing_adjustments a
                LEFT JOIN job_candidates jc ON jc.id=a.candidate_id
                LEFT JOIN people p ON p.id=jc.person_id
                WHERE a.job_id=?
                ORDER BY CASE a.status
                    WHEN 'pending' THEN 0 WHEN 'accepted' THEN 1 WHEN 'applied' THEN 2 ELSE 3
                END, a.id DESC
                """,
                (job_id,),
            ).fetchall()
            summary = {"pending": 0, "accepted": 0, "applied": 0, "ignored": 0}
            items: list[dict[str, Any]] = []
            current_pool: dict[str, int] | None = None
            for row in rows:
                status = str(row["status"] or "")
                if status in summary:
                    summary[status] += 1
                baseline_raw = row["baseline_json"] or ""
                baseline: dict[str, Any] = {}
                if baseline_raw:
                    try:
                        baseline = json.loads(baseline_raw)
                    except Exception:
                        baseline = {}
                # 已应用的调整：附带当前候选池值，供"调整前后效果对比"追踪。
                delta: dict[str, Any] | None = None
                if status == "applied" and baseline:
                    if current_pool is None:
                        current_pool = self._candidate_pool_snapshot(job_id)
                    delta = {
                        "baseline": baseline,
                        "current": current_pool,
                        "diff": {
                            key: int(current_pool.get(key, 0)) - int(baseline.get(key, 0))
                            for key in ("total", "pending_review", "contacted", "stopped")
                        },
                    }
                items.append({
                    "id": int(row["id"]),
                    "job_id": int(row["job_id"]),
                    "candidate_id": int(row["candidate_id"]) if row["candidate_id"] else None,
                    "candidate_name": row["candidate_name"] or "",
                    "candidate_display_name": row["candidate_name"] or "",
                    "adjust_type": row["adjust_type"],
                    "value": row["value"],
                    "rationale": row["rationale"] or "",
                    "confidence": float(row["confidence"] or 0.5),
                    "status": status,
                    "created_at": row["created_at"] or "",
                    "accepted_at": row["accepted_at"] or "",
                    "applied_at": row["applied_at"] or "",
                    "applied_round": int(row["applied_round"]) if row["applied_round"] else None,
                    "applied_workflow_id": row["applied_workflow_id"] or "",
                    "applied_artifact_id": row["applied_artifact_id"] or "",
                    "effect": delta,
                })
            return {"ok": True, "items": items, "summary": summary}
        finally:
            conn.close()

    def _candidate_pool_snapshot(self, job_id: int) -> dict[str, int]:
        """候选池当前快照（总池 / 待复核 / 已触达 / 已停止），口径与 capability_runtime 基线一致。"""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(clean_stage LIKE 'S1%' OR clean_stage LIKE 'X1%' OR clean_stage LIKE 'H1%' OR clean_stage LIKE 'H5%' OR clean_stage LIKE 'S2%') AS pending_review,
                  SUM(clean_stage LIKE 'S3%' OR clean_stage LIKE 'S4%' OR clean_stage LIKE 'S5%' OR clean_stage LIKE 'S6%' OR clean_stage LIKE 'S7%' OR clean_stage LIKE 'S8%' OR clean_stage LIKE 'S9%' OR clean_stage LIKE 'S10%' OR clean_stage LIKE 'S11%' OR clean_stage LIKE 'S12%' OR clean_stage LIKE 'S13%' OR clean_stage LIKE 'X2%' OR clean_stage LIKE 'X3%' OR clean_stage LIKE 'X4%' OR clean_stage LIKE 'X5%') AS contacted,
                  SUM(clean_stage LIKE '%停止%' OR clean_stage LIKE '%淘汰%' OR clean_stage LIKE '%不通过%') AS stopped
                FROM job_candidates WHERE job_id=?
                """,
                (job_id,),
            ).fetchone()
            return {
                "total": int(rows["total"] or 0),
                "pending_review": int(rows["pending_review"] or 0),
                "contacted": int(rows["contacted"] or 0),
                "stopped": int(rows["stopped"] or 0),
            }
        finally:
            conn.close()

    def confirm_sourcing_adjustment(self, adjustment_id: int) -> dict[str, Any]:
        """顾问采纳调整：pending → accepted；策略产物落库后才能进入 applied。"""
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM agent_sourcing_adjustments WHERE id=?",
                (adjustment_id,),
            ).fetchone()
            if current is None:
                raise LookupError("调整指令不存在")
            status = str(current["status"] or "")
            already_accepted = status == "accepted"
            if status == "pending":
                conn.execute(
                    """
                    UPDATE agent_sourcing_adjustments
                       SET status='accepted', accepted_at=datetime('now','localtime')
                     WHERE id=? AND status='pending'
                    """,
                    (adjustment_id,),
                )
            elif not already_accepted:
                raise LookupError("调整指令已应用或已忽略，不能再次采纳")
            row = conn.execute(
                """
                SELECT a.*,p.display_name AS candidate_name
                  FROM agent_sourcing_adjustments a
                  LEFT JOIN job_candidates jc ON jc.id=a.candidate_id
                  LEFT JOIN people p ON p.id=jc.person_id
                 WHERE a.id=?
                """,
                (adjustment_id,),
            ).fetchone()
            conn.commit()
            return {
                "ok": True,
                "id": int(row["id"]),
                "job_id": int(row["job_id"]),
                "candidate_id": int(row["candidate_id"]) if row["candidate_id"] else None,
                "candidate_name": row["candidate_name"] or "",
                "candidate_display_name": row["candidate_name"] or "",
                "adjust_type": row["adjust_type"],
                "value": row["value"],
                "rationale": row["rationale"] or "",
                "confidence": float(row["confidence"] or 0.5),
                "status": "accepted",
                "created_at": row["created_at"] or "",
                "accepted_at": row["accepted_at"] or "",
                "applied_at": None,
                "applied_round": None,
                "applied_workflow_id": None,
                "applied_artifact_id": None,
                "effect": None,
                "already_accepted": already_accepted,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ignore_sourcing_adjustment(self, adjustment_id: int) -> dict[str, Any]:
        """顾问忽略：pending → ignored。"""
        conn = connect(self.db_path)
        try:
            cursor = conn.execute(
                """
                UPDATE agent_sourcing_adjustments
                   SET status='ignored'
                 WHERE id=? AND status='pending'
                """,
                (adjustment_id,),
            )
            if cursor.rowcount == 0:
                raise LookupError("调整指令不存在或已不是待确认状态")
            row = conn.execute(
                """
                SELECT a.*,p.display_name AS candidate_name
                  FROM agent_sourcing_adjustments a
                  LEFT JOIN job_candidates jc ON jc.id=a.candidate_id
                  LEFT JOIN people p ON p.id=jc.person_id
                 WHERE a.id=?
                """,
                (adjustment_id,),
            ).fetchone()
            conn.commit()
            return {
                "ok": True,
                "id": int(row["id"]),
                "job_id": int(row["job_id"]),
                "candidate_id": int(row["candidate_id"]) if row["candidate_id"] else None,
                "candidate_name": row["candidate_name"] or "",
                "candidate_display_name": row["candidate_name"] or "",
                "adjust_type": row["adjust_type"],
                "value": row["value"],
                "rationale": row["rationale"] or "",
                "confidence": float(row["confidence"] or 0.5),
                "status": "ignored",
                "created_at": row["created_at"] or "",
                "accepted_at": row["accepted_at"] or "",
                "applied_at": row["applied_at"] or None,
                "applied_round": int(row["applied_round"]) if row["applied_round"] else None,
                "applied_workflow_id": row["applied_workflow_id"] or None,
                "applied_artifact_id": row["applied_artifact_id"] or None,
                "effect": None,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 顾问确认推荐事实链：preflight/commit + 岗位指标
    # ------------------------------------------------------------------

    def consultant_recommendation_preflight(self, candidate_id: int) -> dict[str, Any]:
        detail = self.candidate(candidate_id)["candidate"]
        if detail["is_stopped"]:
            raise ValueError("该人选关系已停止推进；无法确认推荐")
        conn = connect(self.db_path)
        try:
            existing = conn.execute(
                "SELECT confirmed_at FROM consultant_confirmed_recommendations WHERE job_candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
        finally:
            conn.close()
        token = secrets.token_urlsafe(24)
        expires = datetime.now() + timedelta(minutes=5)
        with self._preflight_lock:
            now = datetime.now()
            self._preflight_tokens = {key: value for key, value in self._preflight_tokens.items() if value[2] > now}
            self._preflight_tokens[token] = (candidate_id, "consultant_recommendation", expires)
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "consultant_recommendation",
            "candidate": {"id": candidate_id, "name": detail["name"], "stage": detail.get("clean_stage")},
            "already_confirmed": existing is not None,
            "confirmed_at": existing["confirmed_at"] if existing else None,
            "impact": "记录顾问确认的客户推荐事实（必须附原因），并写入业务时间线；同一人选仅确认一次。",
        }

    def consultant_recommendation_commit(self, candidate_id: int, reason: str, preflight_token: str) -> dict[str, Any]:
        reason = " ".join(str(reason or "").split())
        if not reason:
            raise ValueError("确认推荐必须填写原因（reason）")
        with self._preflight_lock:
            grant = self._preflight_tokens.pop(preflight_token, None)
        if not grant or grant[0] != candidate_id or grant[1] != "consultant_recommendation" or grant[2] <= datetime.now():
            raise ValueError("preflight token is invalid, expired, or already used")
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT person_id,job_id,clean_stage,raw_status FROM job_candidates WHERE id=?",
                (int(candidate_id),),
            ).fetchone()
            if not row:
                raise LookupError("candidate not found")
            if _is_stopped(row["clean_stage"], row["raw_status"]):
                raise ValueError("该人选关系已停止推进；禁止确认推荐")
            existing = conn.execute(
                "SELECT id,confirmed_at,event_id,reason FROM consultant_confirmed_recommendations WHERE job_candidate_id=?",
                (int(candidate_id),),
            ).fetchone()
            if existing:
                # 幂等回读：已确认不重复生成推荐包；历史确认缺包时补齐（同事务）。
                package = self._ensure_recommendation_package(conn, candidate_id, int(existing["id"]))
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "action": "consultant_recommendation",
                    "already_confirmed": True,
                    "confirmed_at": existing["confirmed_at"],
                    "reason": existing["reason"],
                    "event_id": existing["event_id"],
                    "package": package,
                }
            event_cursor = conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
                   VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'api_v1')""",
                (
                    int(candidate_id),
                    int(row["person_id"]),
                    int(row["job_id"]),
                    "consultant_confirmed_recommendation",
                    "confirmed",
                    f"顾问确认推荐：{reason}",
                    json.dumps(
                        {"action": "consultant_recommendation", "reason": reason, "actor": "consultant"},
                        ensure_ascii=False,
                    ),
                ),
            )
            event_id = int(event_cursor.lastrowid)
            try:
                fact_cursor = conn.execute(
                    """INSERT INTO consultant_confirmed_recommendations
                       (job_candidate_id,person_id,job_id,reason,confirmation_token,confirmed_by,confirmed_at,event_id,created_at)
                       VALUES (?,?,?,?,?,'consultant',datetime('now','localtime'),?,datetime('now','localtime'))""",
                    (
                        int(candidate_id),
                        int(row["person_id"]),
                        int(row["job_id"]),
                        reason,
                        preflight_token,
                        event_id,
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发/重复提交兜底：UNIQUE(job_candidate_id) 命中即视为已确认。
                existing = conn.execute(
                    "SELECT id,confirmed_at,event_id,reason FROM consultant_confirmed_recommendations WHERE job_candidate_id=?",
                    (int(candidate_id),),
                ).fetchone()
                package = self._ensure_recommendation_package(conn, candidate_id, int(existing["id"]))
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "action": "consultant_recommendation",
                    "already_confirmed": True,
                    "confirmed_at": existing["confirmed_at"],
                    "reason": existing["reason"],
                    "event_id": existing["event_id"],
                    "package": package,
                }
            confirmed_at = conn.execute(
                "SELECT confirmed_at FROM consultant_confirmed_recommendations WHERE id=?",
                (int(fact_cursor.lastrowid),),
            ).fetchone()["confirmed_at"]
            # 确认成功即生成版本化推荐包 v1（候选摘要+人岗证据+风险+待核验问题，同事务落库）。
            package = self._ensure_recommendation_package(conn, candidate_id, int(fact_cursor.lastrowid))
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "action": "consultant_recommendation",
            "reason": reason,
            "already_confirmed": False,
            "confirmed_at": confirmed_at,
            "event_id": event_id,
            "business_event_id": event_id,
            "package": package,
        }

    def consultant_recommendation_metrics(self, job_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (int(job_id),)).fetchone():
                raise LookupError("job not found")
            confirmed = int(
                conn.execute(
                    "SELECT COUNT(*) FROM consultant_confirmed_recommendations WHERE job_id=?",
                    (int(job_id),),
                ).fetchone()[0]
            )
            assessed = int(
                conn.execute(
                    """SELECT COUNT(DISTINCT a.job_candidate_id)
                         FROM agent_candidate_assessments a
                         JOIN agent_runs r ON r.run_id=a.run_id
                         JOIN job_candidates jc ON jc.id=a.job_candidate_id
                        WHERE a.is_current=1 AND r.status='completed' AND jc.job_id=?""",
                    (int(job_id),),
                ).fetchone()[0]
            )
            return {
                "ok": True,
                "job_id": int(job_id),
                "confirmed_recommendations": confirmed,
                "assessed_candidates": assessed,
                "rate": round(confirmed / assessed, 4) if assessed else None,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 版本化推荐包闭环：顾问确认推荐 → 聚合推荐包（候选摘要/人岗证据/风险/待核验问题）
    # → 客户反馈按包版本留痕并回写候选人事件时间线。
    # ------------------------------------------------------------------

    @staticmethod
    def _current_assessment(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row | None:
        """候选人当前有效判人评估（is_current=1 且 run 完成），供推荐包证据快照/升版判定复用。"""
        return conn.execute(
            """SELECT a.id,a.fit_score,a.fit_level,a.recommendation,a.confidence,a.evidence_coverage,
                      a.criteria_json,a.strengths_json,a.gaps_json,a.risks_json,a.verification_questions_json,
                      a.created_at
                 FROM agent_candidate_assessments a
                 JOIN agent_runs r ON r.run_id=a.run_id
                WHERE a.job_candidate_id=? AND a.is_current=1 AND r.status='completed'
                ORDER BY datetime(a.created_at) DESC,a.id DESC LIMIT 1""",
            (int(candidate_id),),
        ).fetchone()

    @staticmethod
    def _assessment_fingerprint(assessment: sqlite3.Row) -> str:
        """评估指纹：assessment_id + fit_score + fit_level + evidence_coverage + created_at。

        指纹变化即视为"评估已更新"，是推荐包升版（P3-a）的判定基准。
        """
        parts = [
            str(assessment["id"]),
            str(assessment["fit_score"]),
            str(assessment["fit_level"]),
            str(assessment["evidence_coverage"]),
            str(assessment["created_at"]),
        ]
        return "sha256:" + hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _package_snapshot_fingerprint(self, conn: sqlite3.Connection, package: sqlite3.Row) -> str:
        """推荐包 evidence 快照的评估指纹；旧包（无指纹字段）用快照 assessment_id 反查补齐。"""
        evidence = json_value(package["evidence_json"], {})
        fingerprint = str(evidence.get("fingerprint") or "")
        if fingerprint:
            return fingerprint
        assessment_id = evidence.get("assessment_id")
        if assessment_id:
            row = conn.execute(
                "SELECT id,fit_score,fit_level,evidence_coverage,created_at FROM agent_candidate_assessments WHERE id=?",
                (int(assessment_id),),
            ).fetchone()
            if row:
                return self._assessment_fingerprint(row)
        return ""

    def _package_evidence(self, assessment: sqlite3.Row | None) -> tuple[dict[str, Any], list[Any], list[Any]]:
        """评估行 → 推荐包证据快照（含指纹）+ 风险 + 待核验问题；无有效评估时如实标注证据缺失。"""
        if not assessment:
            return {"status": "no_current_assessment", "note": "暂无当前有效的判人评估，人岗匹配证据缺失"}, [], []
        evidence: dict[str, Any] = {
            "status": "ready",
            "assessment_id": int(assessment["id"]),
            "fit_score": assessment["fit_score"],
            "fit_level": assessment["fit_level"],
            "recommendation": assessment["recommendation"],
            "confidence": assessment["confidence"],
            "evidence_coverage": assessment["evidence_coverage"],
            "criteria": json_value(assessment["criteria_json"], {}),
            "strengths": json_value(assessment["strengths_json"], []),
            "gaps": json_value(assessment["gaps_json"], []),
            "assessed_at": assessment["created_at"],
            "fingerprint": self._assessment_fingerprint(assessment),
        }
        return (
            evidence,
            json_value(assessment["risks_json"], []),
            json_value(assessment["verification_questions_json"], []),
        )

    @staticmethod
    def _package_brief(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "package_id": row["package_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "recommendation_id": int(row["recommendation_id"]),
        }

    def _ensure_recommendation_package(self, conn: sqlite3.Connection, candidate_id: int, recommendation_id: int) -> dict[str, Any]:
        """确认推荐后幂等生成推荐包 v1：已存在则回读，不重复生成。"""
        existing = conn.execute(
            "SELECT * FROM recommendation_packages WHERE job_candidate_id=? ORDER BY version DESC LIMIT 1",
            (int(candidate_id),),
        ).fetchone()
        if existing:
            return self._package_brief(existing)
        fact = conn.execute(
            "SELECT id,job_candidate_id,person_id,job_id,reason,confirmed_by,confirmed_at FROM consultant_confirmed_recommendations WHERE id=?",
            (int(recommendation_id),),
        ).fetchone()
        if not fact:
            raise LookupError("consultant recommendation not found")
        base = conn.execute(
            """SELECT jc.clean_stage,jc.flow_bucket,p.display_name name,p.current_company,p.current_title,
                      p.city,p.education,p.experience,j.title job,c.name client
                 FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                 LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                WHERE jc.id=?""",
            (int(candidate_id),),
        ).fetchone()
        if not base:
            raise LookupError("candidate not found")
        assessment = self._current_assessment(conn, candidate_id)
        summary = {
            "name": base["name"],
            "current_company": base["current_company"] or "",
            "current_title": base["current_title"] or "",
            "city": base["city"] or "",
            "education": base["education"] or "",
            "experience": base["experience"] or "",
            "stage": base["clean_stage"] or base["flow_bucket"] or "",
            "job": {"id": int(fact["job_id"]), "title": base["job"] or "", "client": base["client"] or ""},
            "recommendation": {
                "id": int(fact["id"]),
                "reason": fact["reason"],
                "confirmed_by": fact["confirmed_by"],
                "confirmed_at": fact["confirmed_at"],
            },
        }
        if assessment:
            evidence, risks, questions = self._package_evidence(assessment)
        else:
            # 无当前有效评估时如实标注证据缺失，不伪造人岗证据。
            evidence, risks, questions = self._package_evidence(None)
        try:
            cursor = conn.execute(
                """INSERT INTO recommendation_packages
                   (package_id,job_candidate_id,person_id,job_id,recommendation_id,version,status,
                    summary_json,evidence_json,risks_json,verification_questions_json,created_at)
                   VALUES (?,?,?,?,?,1,'generated',?,?,?,?,datetime('now','localtime'))""",
                (
                    f"recpkg_{secrets.token_urlsafe(12)}",
                    int(candidate_id),
                    int(fact["person_id"]),
                    int(fact["job_id"]),
                    int(fact["id"]),
                    json.dumps(summary, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(risks if isinstance(risks, list) else [], ensure_ascii=False),
                    json.dumps(questions if isinstance(questions, list) else [], ensure_ascii=False),
                ),
            )
        except sqlite3.IntegrityError:
            # 并发兜底：UNIQUE(job_candidate_id, version) 命中即视为已生成，回读现有版本。
            existing = conn.execute(
                "SELECT * FROM recommendation_packages WHERE job_candidate_id=? ORDER BY version DESC LIMIT 1",
                (int(candidate_id),),
            ).fetchone()
            return self._package_brief(existing)
        row = conn.execute(
            "SELECT * FROM recommendation_packages WHERE id=?",
            (int(cursor.lastrowid),),
        ).fetchone()
        return self._package_brief(row)

    def list_recommendation_packages(self, candidate_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            if not conn.execute("SELECT 1 FROM job_candidates WHERE id=?", (int(candidate_id),)).fetchone():
                raise LookupError("candidate not found")
            rows = conn.execute(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM recommendation_package_feedback f WHERE f.package_id=p.package_id) AS feedback_count
                     FROM recommendation_packages p
                    WHERE p.job_candidate_id=? ORDER BY p.version DESC""",
                (int(candidate_id),),
            ).fetchall()
            items = [self._package_brief(row) | {"feedback_count": int(row["feedback_count"])} for row in rows]
            return {"ok": True, "candidate_id": int(candidate_id), "items": items}
        finally:
            conn.close()

    def get_recommendation_package(self, package_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM recommendation_packages WHERE package_id=?",
                (str(package_id or "").strip(),),
            ).fetchone()
            if not row:
                raise LookupError("recommendation package not found")
            feedback = [
                {
                    "id": int(item["id"]),
                    "feedback_type": item["feedback_type"],
                    "feedback_type_label": PACKAGE_FEEDBACK_TYPE_LABELS.get(item["feedback_type"], item["feedback_type"]),
                    "content": item["content"],
                    "feedback_time": item["feedback_time"],
                    "recorded_by": item["recorded_by"],
                    "created_at": item["created_at"],
                }
                for item in conn.execute(
                    "SELECT * FROM recommendation_package_feedback WHERE package_id=? ORDER BY datetime(feedback_time) DESC,id DESC",
                    (row["package_id"],),
                ).fetchall()
            ]
            payload = self._package_brief(row)
            # 升版合同（P3-a）：详情携带评估指纹与可升版标记。
            # upgradeable = 本包为最新版本 且 当前有效评估指纹 ≠ 包内证据快照指纹。
            evidence = json_value(row["evidence_json"], {})
            snapshot_fp = self._package_snapshot_fingerprint(conn, row)
            if snapshot_fp and not evidence.get("fingerprint"):
                evidence["fingerprint"] = snapshot_fp
            assessment = self._current_assessment(conn, int(row["job_candidate_id"]))
            latest_fp = self._assessment_fingerprint(assessment) if assessment else ""
            max_version = conn.execute(
                "SELECT MAX(version) FROM recommendation_packages WHERE job_candidate_id=?",
                (int(row["job_candidate_id"]),),
            ).fetchone()[0]
            is_latest = int(max_version or 0) == int(row["version"])
            payload.update(
                {
                    "ok": True,
                    "candidate_id": int(row["job_candidate_id"]),
                    "person_id": int(row["person_id"]),
                    "job_id": int(row["job_id"]),
                    "summary": json_value(row["summary_json"], {}),
                    "evidence": evidence,
                    "risks": json_value(row["risks_json"], []),
                    "verification_questions": json_value(row["verification_questions_json"], []),
                    "feedback": feedback,
                    "upgradeable": bool(is_latest and latest_fp and latest_fp != snapshot_fp),
                    "latest_assessment_id": int(assessment["id"]) if assessment else None,
                }
            )
            return payload
        finally:
            conn.close()

    def recommendation_package_upgrade_preflight(self, package_id: str) -> dict[str, Any]:
        """推荐包升版预检（P3-a）：包存在且为最新版本、评估指纹已更新才发一次性 token。

        409：历史版本只读 / 无当前有效评估 / 评估指纹一致无需升版。
        """
        package_id = str(package_id or "").strip()
        conn = connect(self.db_path)
        try:
            package = conn.execute(
                "SELECT * FROM recommendation_packages WHERE package_id=?",
                (package_id,),
            ).fetchone()
            if not package:
                raise LookupError("recommendation package not found")
            max_version = conn.execute(
                "SELECT MAX(version) FROM recommendation_packages WHERE job_candidate_id=?",
                (int(package["job_candidate_id"]),),
            ).fetchone()[0]
            if int(max_version or 0) != int(package["version"]):
                raise ValueError("历史版本推荐包为只读，请基于最新版本发起升版")
            snapshot_fp = self._package_snapshot_fingerprint(conn, package)
            assessment = self._current_assessment(conn, int(package["job_candidate_id"]))
            if not assessment:
                raise ValueError("暂无当前有效的判人评估，无法升版")
            latest_fp = self._assessment_fingerprint(assessment)
            if latest_fp == snapshot_fp:
                raise ValueError("评估未发生变化（指纹一致），无需升版")
        finally:
            conn.close()
        token = secrets.token_urlsafe(24)
        expires = datetime.now() + timedelta(minutes=5)
        with self._preflight_lock:
            now = datetime.now()
            self._preflight_tokens = {key: value for key, value in self._preflight_tokens.items() if value[2] > now}
            self._preflight_tokens[token] = (int(package["job_candidate_id"]), f"recommendation_package_upgrade:{package_id}", expires)
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "recommendation_package_upgrade",
            "package_id": package_id,
            "current_version": int(package["version"]),
            "latest_fingerprint": latest_fp,
            "latest_assessment_id": int(assessment["id"]),
            "impact": "将以当前有效评估重新生成推荐包新版本（summary 继承、证据快照换新），旧版本保留只读。",
        }

    def recommendation_package_upgrade_commit(self, package_id: str, preflight_token: str) -> dict[str, Any]:
        """推荐包升版提交（P3-a）：一次性 token + 指纹复核 + UNIQUE(job_candidate_id, version) 并发兜底。

        以 commit 时刻的指纹为准：指纹回退为一致 → 409；并发撞唯一键 → 回读现有最新版本。
        """
        package_id = str(package_id or "").strip()
        with self._preflight_lock:
            grant = self._preflight_tokens.pop(preflight_token, None)
        if not grant or grant[1] != f"recommendation_package_upgrade:{package_id}" or grant[2] <= datetime.now():
            raise ValueError("preflight token is invalid, expired, or already used")
        with transaction(self.db_path) as conn:
            package = conn.execute(
                "SELECT * FROM recommendation_packages WHERE package_id=?",
                (package_id,),
            ).fetchone()
            if not package or grant[0] != int(package["job_candidate_id"]):
                raise LookupError("recommendation package not found")
            max_version = conn.execute(
                "SELECT MAX(version) FROM recommendation_packages WHERE job_candidate_id=?",
                (int(package["job_candidate_id"]),),
            ).fetchone()[0]
            if int(max_version or 0) != int(package["version"]):
                raise ValueError("历史版本推荐包为只读，请基于最新版本发起升版")
            snapshot_fp = self._package_snapshot_fingerprint(conn, package)
            assessment = self._current_assessment(conn, int(package["job_candidate_id"]))
            if not assessment:
                raise ValueError("暂无当前有效的判人评估，无法升版")
            latest_fp = self._assessment_fingerprint(assessment)
            if latest_fp == snapshot_fp:
                raise ValueError("评估未发生变化（指纹一致），无需升版")
            evidence, risks, questions = self._package_evidence(assessment)
            new_version = int(package["version"]) + 1
            try:
                cursor = conn.execute(
                    """INSERT INTO recommendation_packages
                       (package_id,job_candidate_id,person_id,job_id,recommendation_id,version,status,
                        summary_json,evidence_json,risks_json,verification_questions_json,created_at)
                       VALUES (?,?,?,?,?,?,'generated',?,?,?,?,datetime('now','localtime'))""",
                    (
                        f"recpkg_{secrets.token_urlsafe(12)}",
                        int(package["job_candidate_id"]),
                        int(package["person_id"]),
                        int(package["job_id"]),
                        int(package["recommendation_id"]),
                        new_version,
                        # 人岗/推荐事实不变：summary 原样继承；仅证据快照换新评估。
                        package["summary_json"],
                        json.dumps(evidence, ensure_ascii=False),
                        json.dumps(risks if isinstance(risks, list) else [], ensure_ascii=False),
                        json.dumps(questions if isinstance(questions, list) else [], ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发兜底：UNIQUE(job_candidate_id, version) 命中即视为已升版，回读现有最新版本。
                existing = conn.execute(
                    "SELECT * FROM recommendation_packages WHERE job_candidate_id=? ORDER BY version DESC LIMIT 1",
                    (int(package["job_candidate_id"]),),
                ).fetchone()
                return {
                    "ok": True,
                    "package": self._package_brief(existing),
                    "previous_version": int(package["version"]),
                    "upgraded": False,
                    "already_upgraded": True,
                }
            row = conn.execute(
                "SELECT * FROM recommendation_packages WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return {
            "ok": True,
            "package": self._package_brief(row),
            "previous_version": int(package["version"]),
            "upgraded": True,
            "already_upgraded": False,
        }

    # ------------------------------------------------------------------
    # P3-b：客户反馈新旧表口径统一（新表写入同事务双写旧表，旧读方零改动）
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_legacy_feedback_schema(conn: sqlite3.Connection) -> None:
        """保证旧表存在且带 governance 扩展列（job_candidate_id/event_id）。

        与 a_system_workflow_governance 同款的 PRAGMA 幂等加列模式：
        任一方先加列另一方安全跳过；存量行不受影响。
        """
        conn.execute(LEGACY_CLIENT_FEEDBACK_SCHEMA)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(client_feedback_events)")}
        if "job_candidate_id" not in columns:
            conn.execute("ALTER TABLE client_feedback_events ADD COLUMN job_candidate_id INTEGER")
        if "event_id" not in columns:
            conn.execute("ALTER TABLE client_feedback_events ADD COLUMN event_id INTEGER")

    def _mirror_feedback_to_legacy_events(
        self,
        conn: sqlite3.Connection,
        *,
        package: sqlite3.Row,
        feedback_type: str,
        content: str,
        feedback_time: str,
        event_id: int,
    ) -> int:
        """把推荐包反馈按旧报表口径镜像进 client_feedback_events，返回旧表行 id。

        幂等由新表 UNIQUE(package_id, request_id) 保证：重放在上方提前返回，不会走到这里。
        """
        self._ensure_legacy_feedback_schema(conn)
        legacy_type, status_after = LEGACY_FEEDBACK_TYPE_MAP.get(feedback_type, ("other", ""))
        base = conn.execute(
            """SELECT jc.source_candidate_id, p.display_name, p.current_company,
                      j.title AS job_title, c.name AS client_name
                 FROM job_candidates jc
                 LEFT JOIN people p ON p.id=jc.person_id
                 LEFT JOIN jobs j ON j.id=jc.job_id
                 LEFT JOIN clients c ON c.id=j.client_id
                WHERE jc.id=?""",
            (int(package["job_candidate_id"]),),
        ).fetchone()
        candidate_id: int | None = None
        if base and base["source_candidate_id"] not in (None, ""):
            try:
                candidate_id = int(base["source_candidate_id"])
            except (TypeError, ValueError):
                candidate_id = None
        cursor = conn.execute(
            """INSERT INTO client_feedback_events
               (candidate_id,candidate_name,candidate_company,client,position,
                feedback_type,status_after,reason_tags_json,feedback_detail,next_action,
                source,feedback_time,job_candidate_id,event_id)
               VALUES (?,?,?,?,?,?,?,'[]',?,?,'recommendation_package',?,?,?)""",
            (
                candidate_id,
                str(base["display_name"] or "") if base else "",
                str(base["current_company"] or "") if base else "",
                str(base["client_name"] or "") if base else "",
                str(base["job_title"] or "") if base else "",
                legacy_type,
                status_after,
                content,
                "",
                feedback_time,
                int(package["job_candidate_id"]),
                int(event_id),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _legacy_feedback_event_id(conn: sqlite3.Connection, event_id: Any) -> int | None:
        """按 candidate_events 事件 id 回查旧表镜像行（幂等重放路径用）。"""
        if event_id is None:
            return None
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_feedback_events'"
        ).fetchone():
            return None
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(client_feedback_events)")}
        if "event_id" not in columns:
            return None
        row = conn.execute(
            "SELECT id FROM client_feedback_events WHERE event_id=? ORDER BY id DESC LIMIT 1",
            (int(event_id),),
        ).fetchone()
        return int(row[0]) if row else None

    def record_package_feedback(
        self,
        package_id: str,
        feedback_type: str,
        content: str,
        feedback_time: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        feedback_type = str(feedback_type or "").strip()
        if feedback_type not in PACKAGE_FEEDBACK_TYPE_LABELS:
            raise ValueError(f"未知客户反馈类型：{feedback_type or '空'}（可选：{'/'.join(PACKAGE_FEEDBACK_TYPE_LABELS)}）")
        content = " ".join(str(content or "").split())
        if not content:
            raise ValueError("客户反馈内容不能为空")
        feedback_time = str(feedback_time or "").strip()
        with transaction(self.db_path) as conn:
            package = conn.execute(
                "SELECT * FROM recommendation_packages WHERE package_id=?",
                (str(package_id or "").strip(),),
            ).fetchone()
            if not package:
                raise LookupError("recommendation package not found")
            if request_id:
                existing = conn.execute(
                    "SELECT id,event_id,feedback_time,created_at FROM recommendation_package_feedback WHERE package_id=? AND request_id=?",
                    (package["package_id"], request_id),
                ).fetchone()
                if existing:
                    # 表级幂等兜底：UNIQUE(package_id, request_id) 命中即回读，不重复写事件/双写。
                    return {
                        "ok": True,
                        "package_id": package["package_id"],
                        "package_version": int(package["version"]),
                        "candidate_id": int(package["job_candidate_id"]),
                        "feedback_id": int(existing["id"]),
                        "event_id": existing["event_id"],
                        "client_feedback_event_id": self._legacy_feedback_event_id(conn, existing["event_id"]),
                        "already_recorded": True,
                        "feedback": {
                            "id": int(existing["id"]),
                            "feedback_type": feedback_type,
                            "feedback_type_label": PACKAGE_FEEDBACK_TYPE_LABELS[feedback_type],
                            "content": content,
                            "feedback_time": existing["feedback_time"],
                            "recorded_by": "consultant",
                            "created_at": existing["created_at"],
                        },
                    }
            try:
                cursor = conn.execute(
                    """INSERT INTO recommendation_package_feedback
                       (package_id,package_version,job_candidate_id,person_id,job_id,feedback_type,content,
                        feedback_time,recorded_by,request_id,created_at)
                       VALUES (?,?,?,?,?,?,?,COALESCE(NULLIF(?,''),datetime('now','localtime')),'consultant',?,datetime('now','localtime'))""",
                    (
                        package["package_id"],
                        int(package["version"]),
                        int(package["job_candidate_id"]),
                        int(package["person_id"]),
                        int(package["job_id"]),
                        feedback_type,
                        content,
                        feedback_time,
                        request_id,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT id,event_id,feedback_time,created_at FROM recommendation_package_feedback WHERE package_id=? AND request_id=?",
                    (package["package_id"], request_id),
                ).fetchone()
                return {
                    "ok": True,
                    "package_id": package["package_id"],
                    "package_version": int(package["version"]),
                    "candidate_id": int(package["job_candidate_id"]),
                    "feedback_id": int(existing["id"]),
                    "event_id": existing["event_id"],
                    "client_feedback_event_id": self._legacy_feedback_event_id(conn, existing["event_id"]),
                    "already_recorded": True,
                    "feedback": {
                        "id": int(existing["id"]),
                        "feedback_type": feedback_type,
                        "feedback_type_label": PACKAGE_FEEDBACK_TYPE_LABELS[feedback_type],
                        "content": content,
                        "feedback_time": existing["feedback_time"],
                        "recorded_by": "consultant",
                        "created_at": existing["created_at"],
                    },
                }
            feedback_id = int(cursor.lastrowid)
            event_cursor = conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
                   VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'api_v1')""",
                (
                    int(package["job_candidate_id"]),
                    int(package["person_id"]),
                    int(package["job_id"]),
                    "client_feedback",
                    feedback_type,
                    f"客户反馈（推荐包 v{int(package['version'])}）：{PACKAGE_FEEDBACK_TYPE_LABELS[feedback_type]}——{content[:60]}",
                    json.dumps(
                        {
                            "action": "client_feedback",
                            "package_id": package["package_id"],
                            "package_version": int(package["version"]),
                            "feedback_id": feedback_id,
                            "feedback_type": feedback_type,
                            "content": content,
                            "actor": "consultant",
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            event_id = int(event_cursor.lastrowid)
            conn.execute(
                "UPDATE recommendation_package_feedback SET event_id=? WHERE id=?",
                (event_id, feedback_id),
            )
            # 结果回读：以落库行为准返回回执。
            row = conn.execute(
                "SELECT * FROM recommendation_package_feedback WHERE id=?",
                (feedback_id,),
            ).fetchone()
            # P3-b：同事务双写旧 client_feedback_events（旧报表口径零改动可见）。
            # 幂等由新表 UNIQUE(package_id, request_id) 保证——重放在上方提前返回，不会走到这里。
            legacy_event_id = self._mirror_feedback_to_legacy_events(
                conn,
                package=package,
                feedback_type=row["feedback_type"],
                content=row["content"],
                feedback_time=row["feedback_time"],
                event_id=event_id,
            )
        return {
            "ok": True,
            "package_id": row["package_id"],
            "package_version": int(row["package_version"]),
            "candidate_id": int(row["job_candidate_id"]),
            "feedback_id": feedback_id,
            "event_id": event_id,
            "client_feedback_event_id": legacy_event_id,
            "already_recorded": False,
            "feedback": {
                "id": feedback_id,
                "feedback_type": row["feedback_type"],
                "feedback_type_label": PACKAGE_FEEDBACK_TYPE_LABELS.get(row["feedback_type"], row["feedback_type"]),
                "content": row["content"],
                "feedback_time": row["feedback_time"],
                "recorded_by": row["recorded_by"],
                "created_at": row["created_at"],
            },
        }

    # ------------------------------------------------------------------
    # 生命周期一等事件：面试/Offer/入职结构化记录 + 自动跟进待办
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_lifecycle_time(value: str) -> str:
        """把 occurred_at 规范为 'YYYY-MM-DD HH:MM:SS'；空串表示用服务端当前时间。"""
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"事件时间格式非法：{text}（示例：2026-08-05 14:30）") from exc
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    def record_lifecycle_event(
        self,
        candidate_id: int,
        event_type: str,
        *,
        notes: str = "",
        occurred_at: str = "",
        event_status: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        event_type = str(event_type or "").strip()
        spec = LIFECYCLE_EVENT_TYPES.get(event_type)
        if not spec:
            raise ValueError(f"未知生命周期事件类型：{event_type or '空'}（可选：{'/'.join(LIFECYCLE_EVENT_TYPES)}）")
        notes = " ".join(str(notes or "").split())
        event_status = str(event_status or "").strip() or str(spec["default_status"])
        if event_status not in spec["statuses"]:
            raise ValueError(f"事件状态非法：{event_status}（{spec['label']} 可选：{'/'.join(spec['statuses'])}）")
        event_time = self._normalize_lifecycle_time(occurred_at)
        label = str(spec["label"])
        summary = f"{label}：{notes}" if notes else label
        with transaction(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT jc.id,jc.person_id,jc.job_id,jc.source_candidate_id,
                       p.display_name AS name,p.current_company,
                       j.title AS job,c.name AS client
                  FROM job_candidates jc
                  JOIN people p ON p.id=jc.person_id
                  LEFT JOIN jobs j ON j.id=jc.job_id
                  LEFT JOIN clients c ON c.id=j.client_id
                 WHERE jc.id=?
                """,
                (candidate_id,),
            ).fetchone()
            if not row:
                raise LookupError("candidate not found")
            if request_id:
                existing = conn.execute(
                    """SELECT id,event_status,event_time,summary FROM candidate_events
                       WHERE job_candidate_id=? AND event_type=? AND source_table='api_v1' AND source_id=?""",
                    (candidate_id, event_type, request_id),
                ).fetchone()
                if existing:
                    # 表级幂等兜底：request_id 命中即回读，不重复写事件与待办。
                    task = conn.execute(
                        """SELECT id,task_type,due_at,status FROM followup_tasks
                           WHERE job_candidate_id=? AND source_table='lifecycle_event' AND source_id=?
                           ORDER BY id DESC LIMIT 1""",
                        (candidate_id, int(existing["id"])),
                    ).fetchone()
                    return {
                        "ok": True,
                        "candidate_id": candidate_id,
                        "event_id": int(existing["id"]),
                        "followup_task_id": int(task["id"]) if task else None,
                        "already_recorded": True,
                        "event": {
                            "id": int(existing["id"]),
                            "event_type": event_type,
                            "event_type_label": label,
                            "event_status": existing["event_status"],
                            "event_time": existing["event_time"],
                            "summary": existing["summary"],
                        },
                        "followup": (
                            {"id": int(task["id"]), "task_type": task["task_type"], "due_at": task["due_at"], "status": task["status"]}
                            if task
                            else None
                        ),
                    }
            cursor = conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                   VALUES (?,?,?,?,?,COALESCE(NULLIF(?,''),datetime('now','localtime')),?,?,'api_v1',NULLIF(?,''))""",
                (
                    candidate_id,
                    int(row["person_id"]),
                    row["job_id"],
                    event_type,
                    event_status,
                    event_time,
                    summary[:1000],
                    json.dumps(
                        {
                            "action": "lifecycle_event",
                            "event_type": event_type,
                            "event_status": event_status,
                            "notes": notes,
                            "request_id": request_id,
                            "actor": "consultant",
                        },
                        ensure_ascii=False,
                    ),
                    request_id,
                ),
            )
            event_id = int(cursor.lastrowid)
            # 自动跟进待办：复用 followup_tasks 机制（与 Agent 生命周期 followup 同表同字段），
            # 截止时间相对事件发生时间推算；只建内部任务，不自动对外发任何消息。
            due_base = datetime.fromisoformat(event_time) if event_time else datetime.now()
            due_at = (due_base + timedelta(days=int(spec["followup_days"]))).strftime("%Y-%m-%d %H:%M:%S")
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            source_candidate_id = str(row["source_candidate_id"] or "").strip()
            # followup_tasks.id 非自增（表级 UNIQUE），沿用 capability_runtime._followup 的 MAX(id)+1 口径。
            task_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM followup_tasks").fetchone()[0])
            conn.execute(
                """INSERT INTO followup_tasks
                   (id,candidate_id,candidate_name,candidate_company,client,position,task_type,priority,due_at,status,reason,source_table,source_id,created_at,updated_at,job_candidate_id)
                   VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?)""",
                (
                    task_id,
                    int(source_candidate_id) if source_candidate_id.isdigit() else None,
                    row["name"],
                    row["current_company"],
                    row["client"],
                    row["job"],
                    str(spec["followup_task"]),
                    2,
                    due_at,
                    summary[:1000],
                    "lifecycle_event",
                    event_id,
                    now_text,
                    now_text,
                    candidate_id,
                ),
            )
            saved = conn.execute(
                "SELECT id,event_status,event_time,summary FROM candidate_events WHERE id=?",
                (event_id,),
            ).fetchone()
        return {
            "ok": True,
            "candidate_id": candidate_id,
            "event_id": event_id,
            "followup_task_id": task_id,
            "already_recorded": False,
            "event": {
                "id": event_id,
                "event_type": event_type,
                "event_type_label": label,
                "event_status": saved["event_status"],
                "event_time": saved["event_time"],
                "summary": saved["summary"],
            },
            "followup": {"id": task_id, "task_type": str(spec["followup_task"]), "due_at": due_at, "status": "open"},
        }
