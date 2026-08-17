from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import query_builders
from . import sourcing_result_card
from .workflow_plan import _dumps, _loads, _row, sourcing_target_stats


class WorkflowExecuteMixin:
    """执行阶段：启动恢复、执行主循环、审批生命周期、外部渠道步骤、
    暂停/恢复/取消/归档/重试与终局收尾。

    方法体自 workflow.py 逐字节迁移（P2-1），语义不变。
    """

    def _recover_interrupted(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE agent_workflow_steps SET status='pending',error='服务重启后等待恢复',
                    updated_at=datetime('now','localtime') WHERE status='running'
                """
            )
            conn.execute(
                """
                UPDATE agent_workflows SET status='paused',updated_at=datetime('now','localtime')
                WHERE status='running'
                """
            )
            conn.execute(
                """
                UPDATE agent_goals SET status='paused',updated_at=datetime('now','localtime')
                WHERE status='running'
                """
            )
            conn.commit()
        finally:
            conn.close()


    def _event(self, conn, workflow_id: str, step_id: int | None, event_type: str, status: str, summary: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            """
            INSERT INTO agent_step_events (workflow_id,step_id,event_type,status,summary,detail_json)
            VALUES (?,?,?,?,?,?)
            """,
            (workflow_id, step_id, event_type, status, summary, _dumps(detail or {})),
        )


    def start_workflow(
        self,
        workflow_id: str,
        *,
        expected_plan_version: int | None = None,
        expected_plan_hash: str = "",
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT w.goal_id,w.status,w.version,w.plan_json,g.context_json
                FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                WHERE w.workflow_id=?
                """,
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise ValueError("工作流不存在")
            if row["status"] in {"completed", "cancelled", "superseded"}:
                raise ValueError(f"当前工作流不可启动：{row['status']}")
            if expected_plan_hash or expected_plan_version is not None:
                if row["status"] != "planned":
                    raise ValueError(f"待确认计划状态已变化：{row['status']}")
                identity = self._plan_identity(
                    workflow_id,
                    row["version"],
                    _loads(row["plan_json"], {}),
                    _loads(row["context_json"], {}),
                )
                if expected_plan_version is not None and int(expected_plan_version) != identity["version"]:
                    raise ValueError("待确认计划版本已变化，请查看新计划后重新确认")
                if expected_plan_hash and not secrets.compare_digest(str(expected_plan_hash), identity["plan_hash"]):
                    raise ValueError("待确认计划内容已变化，请查看新计划后重新确认")
            conn.execute(
                "UPDATE agent_workflows SET status='queued',started_at=COALESCE(started_at,datetime('now','localtime')),updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (workflow_id,),
            )
            conn.execute(
                "UPDATE agent_goals SET status='queued',started_at=COALESCE(started_at,datetime('now','localtime')),updated_at=datetime('now','localtime') WHERE goal_id=?",
                (row["goal_id"],),
            )
            self._event(conn, workflow_id, None, "workflow_queued", "queued", "目标已进入执行队列")
            conn.commit()
        finally:
            conn.close()
        self.service.executor.submit(self.run_workflow, workflow_id)
        return self.get_workflow(workflow_id)


    def _latest_artifact_payload(self, conn, workflow_id: str, artifact_type: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = conn.execute(
            """
            SELECT content,file_path,metadata_json FROM agent_artifacts
            WHERE workflow_id=? AND artifact_type=?
            ORDER BY id DESC LIMIT 1
            """,
            (workflow_id, artifact_type),
        ).fetchone()
        if row is None:
            return {}, {}
        content = row["content"] or ""
        if not content and row["file_path"]:
            path = Path(str(row["file_path"]))
            if path.exists():
                content = path.read_text(encoding="utf-8")
        return _loads(content, {}), _loads(row["metadata_json"], {})


    def _sourcing_strategy_snapshot(self, conn, workflow_id: str) -> dict[str, Any]:
        """Build the immutable strategy view shown at and verified after R3 approval."""
        artifact_row = conn.execute(
            """
            SELECT id,artifact_id,metadata_json FROM agent_artifacts
            WHERE workflow_id=? AND artifact_type='search_strategy'
            ORDER BY id DESC LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
        metadata = _loads(artifact_row["metadata_json"], {}) if artifact_row else {}
        plan = metadata.get("plan") if isinstance(metadata.get("plan"), dict) else {}
        strategy_v2 = metadata.get("strategy_v2") if isinstance(metadata.get("strategy_v2"), dict) else {}
        query_plan = metadata.get("query_plan_v1") if isinstance(metadata.get("query_plan_v1"), dict) else {}
        golden_replay = (
            metadata.get("golden_candidate_replay_v1")
            if isinstance(metadata.get("golden_candidate_replay_v1"), dict)
            else None
        )
        query_plan_hash = str(query_plan.get("plan_hash") or "")
        query_plan_valid = bool(
            query_plan.get("schema_version") == "query_plan_v1"
            and isinstance(query_plan.get("cells"), list)
            and query_plan.get("cells")
            and query_plan_hash
            and secrets.compare_digest(query_plan_hash, query_builders.query_plan_hash(query_plan))
        )
        goal = conn.execute(
            """
            SELECT g.objective FROM agent_goals g
            JOIN agent_workflows w ON w.goal_id=g.goal_id WHERE w.workflow_id=?
            """,
            (workflow_id,),
        ).fetchone()
        objective = str(goal["objective"] or "") if goal else ""
        target_match = re.search(r"(\d{1,3})\s*(?:名|位|个|人)", objective)
        target_count = min(100, int(target_match.group(1))) if target_match else 10
        channels = plan.get("channels") if isinstance(plan.get("channels"), dict) else {}
        pools: list[dict[str, Any]] = []
        for tier in strategy_v2.get("step2_target_pool") or []:
            if not isinstance(tier, dict):
                continue
            for company in tier.get("companies") or []:
                if isinstance(company, dict) and company.get("name"):
                    pools.append({
                        "name": company.get("name"), "tier": tier.get("tier"),
                        "path": tier.get("path"), "source": company.get("source"),
                        "confidence": company.get("confidence"),
                    })
        snapshot = {
            "workflow_id": workflow_id,
            "strategy_version": str(strategy_v2.get("schema_version") or metadata.get("schema_version") or ""),
            "objective": objective,
            "target_count": target_count,
            "summary": str(plan.get("strategy_summary") or ""),
            "channels": channels,
            "query_plan_v1": query_plan,
            "query_plan_hash": query_plan_hash,
            "channel_policy_v1": query_plan.get("channel_policy_v1") or {},
            "golden_candidate_replay_v1": golden_replay,
            "query_groups": strategy_v2.get("step4_keyword_groups") or [],
            "company_pool": pools,
            "locked_constraints": strategy_v2.get("consultant_constraints") or plan.get("consultant_constraints") or [],
            "input_level": strategy_v2.get("input_level"),
            "missing_anchors": strategy_v2.get("missing_anchors") or [],
        }
        strategy_hash = hashlib.sha256(_dumps(snapshot).encode("utf-8")).hexdigest()
        replay_valid = golden_replay is None or bool(golden_replay.get("passed"))
        ready = bool(plan and strategy_v2 and query_plan_valid and replay_valid and target_count > 0)
        return {
            **snapshot,
            "strategy_artifact_id": str(artifact_row["artifact_id"] or "") if artifact_row else "",
            "strategy_revision": int(metadata.get("edit_revision") or strategy_v2.get("edit_revision") or 0),
            "strategy_hash": strategy_hash,
            "ready": ready,
        }


    def _approval_preflight_details(self, conn, workflow_id: str, step: Any) -> dict[str, Any]:
        capability_id = step["capability_id"]
        if capability_id == "multi_channel_sourcing":
            snapshot = self._sourcing_strategy_snapshot(conn, workflow_id)
            return {
                "confirmation_mode": "single",
                "strategy_snapshot": snapshot,
                "strategy_hash": snapshot["strategy_hash"],
                "strategy_version": snapshot["strategy_version"],
                "target_count": snapshot["target_count"],
                "channels": snapshot["channels"],
                "channel_policy_v1": snapshot["channel_policy_v1"],
                "query_plan_v1": snapshot["query_plan_v1"],
                "query_plan_hash": snapshot["query_plan_hash"],
                "execution_semantics": snapshot["query_plan_v1"].get("execution_semantics") or {},
                "golden_candidate_replay_v1": snapshot["golden_candidate_replay_v1"],
                "query_groups": snapshot["query_groups"],
                "company_pool": snapshot["company_pool"],
                "locked_constraints": snapshot["locked_constraints"],
                "missing_anchors": snapshot["missing_anchors"],
                "exact_content": "批准后只能执行本卡所示策略 hash 对应的渠道关键词查询单元、渠道编排策略、公司池和目标人数；地点、职级、场景只用于召回后评估。",
            }
        if capability_id == "outreach_execute":
            batch, _ = self._latest_artifact_payload(conn, workflow_id, "outreach_draft_batch")
            items = batch.get("items") if isinstance(batch.get("items"), list) else []
            return {
                "confirmation_mode": "batch",
                "batch_limit": 20,
                "batch_size": len(items),
                "items": [
                    {
                        "type": "outreach", "status": item.get("status") or "pending",
                        "candidate": item.get("candidate"), "client": item.get("client"), "job": item.get("job"),
                        "channel": item.get("channel") or "猎聘职聊", "message": item.get("message"),
                        "message_hash": item.get("message_hash"), "before": item.get("before"), "after": item.get("after"),
                    }
                    for item in items[:20]
                ],
                "exact_content": "本批将只发送审批卡中列出的锁定文案；执行时不会重新生成消息。",
            }
        if capability_id == "job_publish_execute":
            draft, _ = self._latest_artifact_payload(conn, workflow_id, "job_publish_draft")
            readback, _ = self._latest_artifact_payload(conn, workflow_id, "job_publish_prepare_readback")
            fields = {
                key: draft.get(key)
                for key in (
                    "client_company", "job_title", "city_choice", "salary_low_k", "salary_high_k",
                    "salary_months", "job_category_choice", "industry_choice", "work_year_choice",
                    "education_choice", "recruit_count", "close_date", "description",
                )
                if key in draft
            }
            return {
                "confirmation_mode": "single",
                "batch_limit": 5,
                "batch_size": 1,
                "items": [{
                    "type": "job_publish", "status": "pending",
                    "client": draft.get("client_company"), "job": draft.get("job_title"),
                    "channel": "猎聘岗位发布", "fields": fields, "readback": readback,
                }],
                "draft": draft,
                "readback": readback,
                "exact_content": "正式发布将使用已通过预检读回的岗位字段。",
            }
        if capability_id == "candidate_relationship_cleanup":
            from .relationship_cleanup import build_relationship_cleanup_preview

            inputs = _loads(step["input_json"], {})
            context = self._workflow_context(conn, workflow_id)
            preview = build_relationship_cleanup_preview(
                self.service.db_path,
                int(context.get("id") or 0),
                scope_mode=str(inputs.get("scope_mode") or "nonmatching"),
            )
            return {
                "confirmation_mode": "batch",
                "batch_limit": 50,
                "batch_size": int(preview["relationship_count"]),
                "scope_mode": preview["scope_mode"],
                "candidate_records_preserved": True,
                "relationship_ids": [int(item["jc_id"]) for item in preview["items"]],
                "items": [
                    {
                        "type": "candidate_relationship_cleanup",
                        "status": "pending",
                        "job_candidate_id": item.get("jc_id"),
                        "candidate": item.get("name"),
                        "company": item.get("company"),
                        "title": item.get("title"),
                        "reason": item.get("reason"),
                        "before": "当前岗位推进关系有效",
                        "after": "当前岗位关系归档；人才主档保留",
                    }
                    for item in preview["items"][:50]
                ],
                "exact_content": (
                    f"批准后归档当前预览中的 {preview['relationship_count']} 条岗位候选关系；"
                    "不会删除人员或候选人主档，也不会修改人才库全局候选人状态。"
                ),
            }
        return {}


    def _create_approval(self, conn, goal_id: str, workflow_id: str, step: Any) -> bool:
        existing = conn.execute(
            "SELECT approval_id FROM agent_approvals WHERE step_id=? AND status='pending'", (step["id"],)
        ).fetchone()
        if existing:
            return True
        approval_id = f"approval_{secrets.token_hex(6)}"
        workflow_context = self._workflow_context(conn, workflow_id)
        effects = {
            "multi_channel_sourcing": ("不新增候选人、不触达", "搜索结果排重后仅进入待复核，不发送消息", "猎聘 + X-SaaS"),
            "job_library_update": ("岗位库保持当前记录", "更新 jobs、positions、position_profiles 派生字段和岗位指标缓存", "ASA 内部"),
            "job_publish_prepare": ("岗位尚未填入猎聘发布表单", "只填草稿并读回字段，不正式发布", "猎聘"),
            "job_publish_execute": ("岗位尚未正式发布", "正式提交岗位，并以结果页或职位列表为准", "猎聘"),
            "outreach_execute": ("候选人尚未收到本次消息", "发送审批卡中的单条消息并读回会话", "猎聘职聊"),
            "client_recommendation": ("客户尚未收到本次推荐", "发送锁定版本的推荐报告并等待渠道回执", "指定客户渠道"),
            "offer_confirmation": ("Offer 条件尚未在 ASA 确认", "记录经人工确认的 Offer 条件，不代表候选人接受", "ASA 内部"),
            "identity_merge_preflight": ("两份人才身份保持独立", "只生成身份对比，不执行合并", "ASA 内部"),
            "candidate_relationship_cleanup": (
                "候选人仍在当前岗位推进池",
                "审批卡列出的岗位关系进入可追溯归档；人才主档和全局状态保持不变",
                "ASA 内部",
            ),
            "memory_capture": ("信息尚未进入长期记忆", "经确认的信息进入当前范围记忆，可撤销", "ASA 内部"),
        }
        before, after, channel = effects.get(step["capability_id"], ("当前业务状态不变", "只执行审批卡中的本次动作", "ASA 内部"))
        exact_action = {
            "action": step["business_label"], "capability_id": step["capability_id"],
            "object": workflow_context, "object_label": self._context_label(conn, workflow_context), "channel": channel,
            "before": before, "after": after,
            "external_effect": step["risk_level"] == "R3",
            "irreversible": step["risk_level"] == "R3",
        }
        try:
            exact_action.update(self._approval_preflight_details(conn, workflow_id, step))
        except Exception as exc:
            from .relationship_cleanup import RelationshipCleanupScopeBlocked

            if not isinstance(exc, RelationshipCleanupScopeBlocked):
                raise
            reason = str(exc)
            conn.execute(
                "UPDATE agent_workflow_steps SET status='blocked',error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?",
                (reason[:1000], step["id"]),
            )
            conn.execute(
                "UPDATE agent_workflows SET status='blocked',active_step_id=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (workflow_id,),
            )
            conn.execute(
                "UPDATE agent_goals SET status='blocked',error=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                (reason[:1000], goal_id),
            )
            self._event(
                conn,
                workflow_id,
                step["id"],
                "approval_preflight_blocked",
                "blocked",
                reason,
                {"reason": "unsupported_candidate_filter_domain", "scope_mode": "nonmatching"},
            )
            return False
        if step["capability_id"] == "candidate_relationship_cleanup":
            locked_inputs = _loads(step["input_json"], {})
            locked_inputs["approved_relationship_ids"] = list(exact_action.get("relationship_ids") or [])
            conn.execute(
                "UPDATE agent_workflow_steps SET input_json=?,updated_at=datetime('now','localtime') WHERE id=?",
                (_dumps(locked_inputs), step["id"]),
            )
        token = secrets.token_urlsafe(18)
        conn.execute(
            """
            INSERT INTO agent_approvals
            (approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,preflight_json,
             status,token_hash,expires_at)
            VALUES (?,?,?,?,?,?,?,?,'pending',?,?)
            """,
            (
                approval_id, goal_id, workflow_id, step["id"], step["capability_id"],
                step["risk_level"], step["business_label"], _dumps(exact_action),
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.execute("UPDATE agent_workflow_steps SET status='waiting_approval',updated_at=datetime('now','localtime') WHERE id=?", (step["id"],))
        mode = "批量确认" if exact_action.get("confirmation_mode") == "batch" else "单次确认"
        self._event(conn, workflow_id, step["id"], "approval_required", "waiting_approval", f"{step['business_label']} 等待{mode}", exact_action)
        return True


    def _refresh_expired_approvals(self, conn, workflow_id: str) -> bool:
        changed = False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expired = conn.execute(
            "SELECT * FROM agent_approvals WHERE workflow_id=? AND status='pending' AND expires_at IS NOT NULL AND expires_at<?",
            (workflow_id, now),
        ).fetchall()
        for approval in expired:
            conn.execute(
                "UPDATE agent_approvals SET status=?,decided_at=datetime('now','localtime'),decision_note='自动过期换新' WHERE id=?",
                (f"expired_{approval['approval_id']}", approval["id"]),
            )
            changed = True
        orphan_steps = conn.execute(
            """
            SELECT s.*,w.goal_id
            FROM agent_workflow_steps s JOIN agent_workflows w ON w.workflow_id=s.workflow_id
            WHERE s.workflow_id=? AND s.status='waiting_approval'
              AND NOT EXISTS (
                SELECT 1 FROM agent_approvals a WHERE a.step_id=s.id AND a.status='pending'
              )
            """,
            (workflow_id,),
        ).fetchall()
        for step in orphan_steps:
            if self._create_approval(conn, step["goal_id"], workflow_id, step):
                self._event(conn, workflow_id, step["id"], "approval_refreshed", "waiting_approval", f"审批已自动换新：{step['business_label']}")
            changed = True
        return changed


    @staticmethod
    def _value_matches_kind(value: Any, kind: str) -> bool:
        return {
            "string": isinstance(value, str) and bool(value.strip()),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "boolean": isinstance(value, bool),
        }.get(kind, value is not None)


    def _verify_step_result(
        self,
        step: dict[str, Any],
        context: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        capability = self.service.skills.get(step.get("capability_id"))
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str, *, recoverable: bool = False) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail[:500], "recoverable": recoverable})

        if not isinstance(result, dict):
            check("result_object", False, "能力返回值不是对象")
        elif result.get("blocked") is True:
            check("business_precondition", True, str(result.get("summary") or "业务前置条件未满足"))
            return {
                "ok": True, "status": "blocked", "recoverable": False,
                "summary": str(result.get("summary") or "业务前置条件未满足"), "checks": checks,
            }
        else:
            check("result_object", True, "能力返回结构有效")

        if capability:
            for raw_key, kind in capability.output_schema.items():
                key = raw_key.rstrip("?")
                if raw_key.endswith("?") and key not in result:
                    continue
                check(
                    f"output:{key}",
                    key in result and self._value_matches_kind(result.get(key), kind),
                    f"输出字段 {key} 必须是有效 {kind}",
                )

        context_type = str(context.get("type") or "global")
        context_id = context.get("id")
        if context_type in {"job", "candidate"} and context_id:
            facts = self.service._copilot_focus_context_facts(context)
            check("context_object_exists", bool(facts), f"{context_type} #{context_id} 仍可从 v3 唯一读取")

        waiting_external = result.get("external_action_executed") is False
        if waiting_external:
            request = result.get("external_request") or result.get("auto_execute_request")
            check("external_request", isinstance(request, dict) and bool(request), "等待外部执行时必须保留结构化请求")

        declared_postcondition = result.get("postcondition") if isinstance(result.get("postcondition"), dict) else {}
        if declared_postcondition:
            verified = declared_postcondition.get("verified") is True
            check(
                "declared_postcondition", verified,
                str(declared_postcondition.get("reason") or "能力声明的后置条件未满足"),
                recoverable=bool(declared_postcondition.get("recoverable")),
            )

        expected_artifacts = set(capability.artifact_types if capability else ())
        if expected_artifacts and not waiting_external:
            result_artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
            found_artifacts = {str(item.get("type") or "") for item in result_artifacts if isinstance(item, dict)}
            conn = self._connect()
            try:
                if step.get("workflow_id") and step.get("id"):
                    found_artifacts.update(
                        str(row[0])
                        for row in conn.execute(
                            "SELECT artifact_type FROM agent_artifacts WHERE workflow_id=? AND step_id=?",
                            (step["workflow_id"], int(step["id"])),
                        ).fetchall()
                    )
            finally:
                conn.close()
            missing_artifacts = sorted(expected_artifacts - found_artifacts)
            check(
                "declared_artifacts", not missing_artifacts,
                "缺少产物：" + "、".join(missing_artifacts) if missing_artifacts else "声明产物齐全",
            )

        if step.get("capability_id") == "job_library_update" and not waiting_external:
            receipt = result.get("job_library_update") if isinstance(result.get("job_library_update"), dict) else {}
            changes = receipt.get("changes") if isinstance(receipt.get("changes"), list) else []
            check("job_library_receipt", bool(receipt.get("client")) and bool(changes), "岗位库写入回执包含客户和变更")
            conn = self._connect()
            try:
                for item in changes:
                    row = conn.execute(
                        """
                        SELECT j.id,c.name AS client,j.title FROM jobs j
                        JOIN clients c ON c.id=j.client_id WHERE j.id=?
                        """,
                        (int(item.get("job_id") or 0),),
                    ).fetchone()
                    check(
                        f"job_readback:{item.get('job_id')}",
                        bool(row) and row["client"] == receipt.get("client") and row["title"] == item.get("title"),
                        f"读回岗位 {item.get('title') or item.get('job_id')}", recoverable=True,
                    )
                for item in receipt.get("archived_legacy") or []:
                    row = conn.execute("SELECT lifecycle_stage FROM jobs WHERE id=?", (int(item.get("job_id") or 0),)).fetchone()
                    check(
                        f"archive_readback:{item.get('job_id')}",
                        bool(row) and row["lifecycle_stage"] == "archived",
                        f"读回归档岗位 {item.get('title') or item.get('job_id')}", recoverable=True,
                    )
            finally:
                conn.close()
            sync = receipt.get("sync") if isinstance(receipt.get("sync"), dict) else {}
            if not sync.get("skipped"):
                check("a_system_sync", sync.get("ok") is True, "A 系统同步与审计通过", recoverable=True)

        external_result = result.get("external_result") if isinstance(result.get("external_result"), dict) else {}
        if result.get("external_action_executed") is True and external_result:
            try:
                self.service.validate_external_result(str(step.get("capability_id") or ""), external_result)
                check("external_readback", True, "外部动作回执已验证")
            except ValueError as exc:
                check("external_readback", False, str(exc), recoverable=False)

        failed = [item for item in checks if not item["ok"]]
        recoverable = bool(failed) and all(item.get("recoverable") for item in failed)
        return {
            "ok": not failed,
            "status": "verified" if not failed else "failed",
            "recoverable": recoverable,
            "summary": "执行结果已通过后置校验" if not failed else "；".join(item["detail"] for item in failed)[:1000],
            "checks": checks,
        }


    def _recovery_plan(self, step: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any] | None:
        capability = self.service.skills.get(step.get("capability_id"))
        if (
            not capability or not capability.idempotent or not verification.get("recoverable")
            or int(step.get("retry_count") or 0) >= int(capability.retry_limit)
            or step.get("risk_level") in {"R2", "R3"}
        ):
            return None
        return {
            "action": "retry_same_step",
            "reason": verification.get("summary"),
            "attempt": int(step.get("retry_count") or 0) + 1,
            "max_attempts": int(capability.retry_limit),
            "requires_approval": False,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


    def run_workflow(self, workflow_id: str) -> None:
        conn = self._connect()
        try:
            workflow = conn.execute("SELECT * FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None or workflow["status"] in {"cancelled", "completed", "superseded", "paused"}:
                return
            goal_id = workflow["goal_id"]
            conn.execute("UPDATE agent_workflows SET status='running',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
            conn.execute("UPDATE agent_goals SET status='running',updated_at=datetime('now','localtime') WHERE goal_id=?", (goal_id,))
            conn.commit()
        finally:
            conn.close()

        while True:
            conn = self._connect()
            try:
                workflow = conn.execute("SELECT * FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
                if workflow is None or workflow["status"] in {"cancelled", "superseded", "blocked", "paused"}:
                    return
                steps = conn.execute("SELECT * FROM agent_workflow_steps WHERE workflow_id=? ORDER BY sequence", (workflow_id,)).fetchall()
                completed_keys = {step["step_key"] for step in steps if step["status"] in {"completed", "skipped"}}
                pending = next(
                    (
                        step for step in steps if step["status"] in {"pending", "approved"}
                        and all(dep in completed_keys for dep in _loads(step["depends_on_json"], []))
                    ),
                    None,
                )
                if pending is None:
                    if any(step["status"] == "waiting_approval" for step in steps):
                        conn.execute("UPDATE agent_workflows SET status='waiting_approval',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                        conn.execute("UPDATE agent_goals SET status='waiting_approval',updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                        conn.commit()
                        return
                    if any(step["status"] == "failed" for step in steps):
                        return
                    if any(step["status"] == "blocked" for step in steps):
                        conn.execute("UPDATE agent_workflows SET status='blocked',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                        conn.execute("UPDATE agent_goals SET status='blocked',updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                        conn.commit()
                        return
                    if all(step["status"] in {"completed", "skipped"} for step in steps):
                        self._finish(conn, workflow_id, workflow["goal_id"], steps)
                        conn.commit()
                    return
                approved = pending["status"] == "approved"
                if pending["risk_level"] in {"R2", "R3"} and not approved:
                    if not self._create_approval(conn, workflow["goal_id"], workflow_id, pending):
                        conn.commit()
                        return
                    conn.execute("UPDATE agent_workflows SET status='waiting_approval',active_step_id=?,updated_at=datetime('now','localtime') WHERE workflow_id=?", (pending["id"], workflow_id))
                    conn.execute("UPDATE agent_goals SET status='waiting_approval',updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                    conn.commit()
                    return
                claimed = conn.execute(
                    """
                    UPDATE agent_workflow_steps
                       SET status='running',started_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                     WHERE id=?
                       AND EXISTS (
                           SELECT 1 FROM agent_workflows
                            WHERE workflow_id=? AND status NOT IN ('cancelled','completed','superseded','paused')
                       )
                    """,
                    (pending["id"], workflow_id),
                )
                if claimed.rowcount != 1:
                    conn.commit()
                    return
                conn.execute(
                    """
                    UPDATE agent_workflows
                       SET active_step_id=?,current_stage=?,updated_at=datetime('now','localtime')
                     WHERE workflow_id=? AND status NOT IN ('cancelled','completed','superseded','paused')
                    """,
                    (pending["id"], pending["business_stage"], workflow_id),
                )
                self._event(conn, workflow_id, pending["id"], "step_started", "running", f"正在执行：{pending['business_label']}")
                conn.commit()
                context = self._workflow_context(conn, workflow_id)
                step_data = _row(pending)
            finally:
                conn.close()

            try:
                inputs = _loads(step_data["input_json"], {})
                inputs.update({"workflow_id": workflow_id, "step_id": step_data["id"], "capability_id": step_data["capability_id"]})
                if step_data["risk_level"] in {"R2", "R3"}:
                    inputs["_approval_granted"] = step_data["status"] == "approved"
                executed = self.service.skills.execute(step_data["capability_id"], context, inputs)
                result = executed.get("result") or {}
                verification = self._verify_step_result(step_data, context, result)
                conn = self._connect()
                try:
                    current_status = conn.execute(
                        "SELECT status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
                    ).fetchone()
                    if current_status is None or current_status["status"] in {"cancelled", "completed", "superseded", "paused"}:
                        return
                    if not verification["ok"]:
                        recovery = self._recovery_plan(step_data, verification)
                        output = {**result, "verification": verification}
                        if recovery:
                            conn.execute(
                                """
                                UPDATE agent_workflow_steps
                                   SET status='pending',output_json=?,verification_json=?,recovery_json=?,
                                       retry_count=retry_count+1,error=?,finished_at=NULL,
                                       updated_at=datetime('now','localtime') WHERE id=?
                                """,
                                (
                                    _dumps(output), _dumps(verification), _dumps(recovery),
                                    str(verification.get("summary") or "后置校验失败")[:1000], step_data["id"],
                                ),
                            )
                            conn.execute(
                                "UPDATE agent_workflows SET status='queued',updated_at=datetime('now','localtime') WHERE workflow_id=?",
                                (workflow_id,),
                            )
                            conn.execute(
                                "UPDATE agent_goals SET status='queued',error=NULL,updated_at=datetime('now','localtime') WHERE goal_id=?",
                                (workflow["goal_id"],),
                            )
                            self._event(
                                conn, workflow_id, step_data["id"], "step_verification_failed", "recovering",
                                str(verification.get("summary") or "执行结果未通过后置校验"), verification,
                            )
                            self._event(
                                conn, workflow_id, step_data["id"], "workflow_replanned", "queued",
                                f"已生成恢复计划并自动重试：{step_data['business_label']}", recovery,
                            )
                            conn.commit()
                            continue
                        # 最终后置校验失败时，只落库能力明确标记为非通过的诊断产物。
                        # 正常业务产物仍必须等校验通过后再落库，避免把未验证结果冒充交付物。
                        diagnostic_artifacts = [
                            artifact
                            for artifact in (result.get("artifacts") or [])
                            if isinstance(artifact, dict)
                            and str(artifact.get("validation_status") or "").lower()
                            in {"failed", "blocked", "warning", "needs_input"}
                        ]
                        diagnostic_ids = self._store_artifacts(
                            conn,
                            workflow["goal_id"],
                            workflow_id,
                            step_data["id"],
                            diagnostic_artifacts,
                        )
                        output["artifact_ids"] = diagnostic_ids
                        conn.execute(
                            """
                            UPDATE agent_workflow_steps
                               SET status='failed',output_json=?,verification_json=?,recovery_json='{}',
                                   error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                             WHERE id=?
                            """,
                            (
                                _dumps(output), _dumps(verification),
                                str(verification.get("summary") or "后置校验失败")[:1000], step_data["id"],
                            ),
                        )
                        conn.execute("UPDATE agent_workflows SET status='failed',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                        conn.execute(
                            "UPDATE agent_goals SET status='failed',error=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                            (str(verification.get("summary") or "后置校验失败")[:1000], workflow["goal_id"]),
                        )
                        self._event(
                            conn, workflow_id, step_data["id"], "step_verification_failed", "failed",
                            str(verification.get("summary") or "执行结果未通过后置校验"), verification,
                        )
                        conn.commit()
                        return
                    artifact_ids = self._store_artifacts(conn, workflow["goal_id"], workflow_id, step_data["id"], result.get("artifacts") or [])
                    consumption = result.get("sourcing_adjustment_consumption")
                    if step_data["capability_id"] == "search_strategy" and isinstance(consumption, dict):
                        result = {
                            **result,
                            "sourcing_adjustment_consumption": self._consume_sourcing_adjustments(
                                conn,
                                workflow_id=workflow_id,
                                step_id=int(step_data["id"]),
                                artifact_ids=artifact_ids,
                                request=consumption,
                            ),
                        }
                    output = {**result, "artifact_ids": artifact_ids, "verification": verification}
                    if result.get("blocked") is True:
                        step_status = "blocked"
                    else:
                        step_status = "waiting_external" if result.get("external_action_executed") is False else "completed"
                    conn.execute(
                        """
                        UPDATE agent_workflow_steps SET status=?,output_json=?,references_json=?,verification_json=?,error=NULL,
                            finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?
                        """,
                        (
                            step_status, _dumps(output), _dumps(result.get("references") or []),
                            _dumps(verification), step_data["id"],
                        ),
                    )
                    self._event(
                        conn, workflow_id, step_data["id"],
                        "step_blocked" if step_status == "blocked" else "external_result_required" if step_status == "waiting_external" else "step_completed",
                        step_status,
                        result.get("summary") or f"已完成：{step_data['business_label']}",
                        {"artifact_ids": artifact_ids},
                    )
                    self._event(
                        conn, workflow_id, step_data["id"], "step_result_verified", step_status,
                        verification["summary"], {"checks": verification.get("checks") or []},
                    )
                    self._update_progress(conn, workflow_id, workflow["goal_id"])
                    if step_status == "waiting_external":
                        conn.execute("UPDATE agent_workflows SET status='waiting_external',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                        conn.execute("UPDATE agent_goals SET status='waiting_external',updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                    elif step_status == "blocked":
                        conn.execute("UPDATE agent_workflows SET status='blocked',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                        conn.execute("UPDATE agent_goals SET status='blocked',error=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (result.get("summary") or "缺少必要输入", workflow["goal_id"]))
                    conn.commit()
                finally:
                    conn.close()
                if step_status == "waiting_external" and isinstance(result.get("auto_execute_request"), dict):
                    self.service.schedule_external_workflow_step(step_data["id"], step_data["capability_id"], result["auto_execute_request"])
                if result.get("external_action_executed") is False or result.get("blocked") is True:
                    return
            except Exception as exc:
                conn = self._connect()
                try:
                    current_status = conn.execute(
                        "SELECT status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
                    ).fetchone()
                    if current_status is None or current_status["status"] in {"cancelled", "completed", "superseded", "paused"}:
                        return
                    conn.execute(
                        "UPDATE agent_workflow_steps SET status='failed',error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?",
                        (str(exc)[:1000], step_data["id"]),
                    )
                    conn.execute("UPDATE agent_workflows SET status='failed',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                    conn.execute("UPDATE agent_goals SET status='failed',error=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (str(exc)[:1000], workflow["goal_id"]))
                    self._event(conn, workflow_id, step_data["id"], "step_failed", "failed", f"{step_data['business_label']} 失败：{str(exc)[:300]}")
                    conn.commit()
                finally:
                    conn.close()
                return


    def _store_artifacts(self, conn, goal_id: str, workflow_id: str, step_id: int, artifacts: list[dict[str, Any]]) -> list[str]:
        ids = []
        for artifact in artifacts[:12]:
            artifact_id = f"artifact_{secrets.token_hex(6)}"
            conn.execute(
                """
                INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact_id, goal_id, workflow_id, step_id,
                    str(artifact.get("type") or "note"), str(artifact.get("title") or "ASA 产物"),
                    str(artifact.get("mime_type") or "text/markdown"), str(artifact.get("file_path") or "") or None,
                    str(artifact.get("content") or "") or None, _dumps(artifact.get("metadata") or {}),
                    str(artifact.get("validation_status") or "passed"),
                ),
            )
            ids.append(artifact_id)
        return ids


    @staticmethod
    def _candidate_pool_baseline(conn: Any, job_id: int) -> dict[str, int]:
        row = conn.execute(
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
            "total": int(row["total"] or 0),
            "pending_review": int(row["pending_review"] or 0),
            "contacted": int(row["contacted"] or 0),
            "stopped": int(row["stopped"] or 0),
        }


    def _consume_sourcing_adjustments(
        self,
        conn: Any,
        *,
        workflow_id: str,
        step_id: int,
        artifact_ids: list[str],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        adjustment_ids = list(dict.fromkeys(
            int(value) for value in (request.get("adjustment_ids") or []) if str(value).isdigit() and int(value) > 0
        ))
        job_id = int(request.get("job_id") or 0)
        if not adjustment_ids or not job_id:
            return {**request, "status": "nothing_to_apply", "applied_ids": [], "not_applied_ids": adjustment_ids}
        artifact_id = ""
        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            row = conn.execute(
                f"""
                SELECT artifact_id FROM agent_artifacts
                 WHERE workflow_id=? AND step_id=? AND artifact_type='search_strategy'
                   AND artifact_id IN ({placeholders})
                 ORDER BY id DESC LIMIT 1
                """,
                (workflow_id, step_id, *artifact_ids),
            ).fetchone()
            artifact_id = str(row["artifact_id"] or "") if row else ""
        if not artifact_id:
            raise RuntimeError("寻访调整不能应用：本轮策略产物尚未成功落库")

        round_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM agent_workflow_steps
             WHERE workflow_id=? AND capability_id='search_strategy' AND status='completed'
            """,
            (workflow_id,),
        ).fetchone()
        applied_round = int(round_row["n"] or 0) + 1
        baseline = self._candidate_pool_baseline(conn, job_id)
        placeholders = ",".join("?" for _ in adjustment_ids)
        conn.execute(
            f"""
            UPDATE agent_sourcing_adjustments
               SET status='applied',applied_at=datetime('now','localtime'),applied_round=?,
                   baseline_json=?,applied_workflow_id=?,applied_artifact_id=?
             WHERE job_id=? AND status='accepted' AND id IN ({placeholders})
            """,
            (applied_round, _dumps(baseline), workflow_id, artifact_id, job_id, *adjustment_ids),
        )
        applied_ids = [
            int(row["id"])
            for row in conn.execute(
                f"""
                SELECT id FROM agent_sourcing_adjustments
                 WHERE job_id=? AND applied_workflow_id=? AND applied_artifact_id=?
                   AND id IN ({placeholders})
                 ORDER BY id
                """,
                (job_id, workflow_id, artifact_id, *adjustment_ids),
            ).fetchall()
        ]
        not_applied_ids = [value for value in adjustment_ids if value not in set(applied_ids)]
        status = "applied" if not not_applied_ids else "partially_applied" if applied_ids else "not_applied"
        receipt = {
            **request,
            "status": status,
            "workflow_id": workflow_id,
            "artifact_id": artifact_id,
            "applied_round": applied_round,
            "applied_ids": applied_ids,
            "not_applied_ids": not_applied_ids,
            "baseline": baseline,
        }
        self._event(
            conn,
            workflow_id,
            step_id,
            "sourcing_adjustments_applied",
            status,
            f"本轮策略已应用 {len(applied_ids)} 条顾问采纳的寻访调整",
            {
                "adjustment_ids": applied_ids,
                "not_applied_ids": not_applied_ids,
                "artifact_id": artifact_id,
                "applied_round": applied_round,
            },
        )
        return receipt


    def _update_progress(self, conn, workflow_id: str, goal_id: str) -> None:
        total, completed = conn.execute(
            "SELECT COUNT(*),SUM(CASE WHEN status IN ('completed','skipped') THEN 1 ELSE 0 END) FROM agent_workflow_steps WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        progress = round(float(completed or 0) / max(1, int(total or 0)), 4)
        conn.execute("UPDATE agent_goals SET progress=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (progress, goal_id))


    def _sourcing_target_status(self, conn, goal: Any, workflow_id: str) -> dict[str, int] | None:
        return sourcing_target_stats(conn, goal["objective"], _loads(goal["context_json"], {}), workflow_id)


    def _auto_strategy_review(self, conn, workflow_id: str, status: str) -> None:
        """S4-3：寻访类工作流终局后自动生成策略复盘（PRD §5，规则版 v1）。

        复盘生成失败绝不阻塞终局流转：异常只留事件留痕。幂等：重算覆盖同工作流旧复盘。
        """
        try:
            from . import strategy_review

            review = strategy_review.generate_for_workflow(conn, workflow_id)
            artifact_id = strategy_review.upsert_strategy_review(conn, review)
            self._event(
                conn, workflow_id, None, "strategy_review_generated", status,
                f"策略复盘已生成：{review.get('verdict_label')}（{artifact_id}）",
                {"artifact_id": artifact_id, "verdict": review.get("verdict"), "version": review.get("version")},
            )
        except Exception as exc:
            try:
                self._event(
                    conn, workflow_id, None, "strategy_review_failed", status,
                    f"策略复盘生成失败（不影响终局）：{str(exc)[:200]}",
                )
            except Exception:
                pass


    def _refresh_sourcing_coverage_assessment(self, conn, workflow_id: str) -> None:
        """Refresh the post-assessment count without changing the sourcing coverage claim."""
        try:
            row = conn.execute(
                """
                SELECT * FROM agent_sourcing_coverage_certificates
                WHERE workflow_id=? ORDER BY issued_at DESC,id DESC LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
            if row is None:
                return
            run_id = str(row["run_id"] or "")
            completed = int(conn.execute(
                """
                SELECT COUNT(DISTINCT a.job_candidate_id)
                FROM agent_candidate_recalls r
                JOIN agent_candidate_assessments a
                  ON a.job_candidate_id=r.job_candidate_id AND a.is_current=1
                WHERE r.run_id=?
                """,
                (run_id,),
            ).fetchone()[0])
            certificate = _loads(row["certificate_json"], {})
            if not isinstance(certificate, dict):
                return
            certificate["assessment"] = {"completed_unique_candidates": completed}
            certificate["issued_at"] = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE agent_sourcing_coverage_certificates
                   SET certificate_json=?,issued_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (_dumps(certificate), int(row["id"])),
            )
            step = conn.execute(
                """
                SELECT id,output_json FROM agent_workflow_steps
                WHERE workflow_id=? AND capability_id='multi_channel_sourcing'
                ORDER BY sequence DESC LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
            if step is not None:
                output = _loads(step["output_json"], {})
                if isinstance(output, dict):
                    external_result = output.get("external_result")
                    if isinstance(external_result, dict):
                        output["external_result"] = {
                            **external_result,
                            "coverage_certificate": certificate,
                        }
                        conn.execute(
                            "UPDATE agent_workflow_steps SET output_json=?,updated_at=datetime('now','localtime') WHERE id=?",
                            (_dumps(output), int(step["id"])),
                        )
            self._event(
                conn, workflow_id, int(step["id"]) if step is not None else None,
                "coverage_certificate_refreshed", "completed",
                f"覆盖证书已刷新：{completed} 位正式入库人选完成评估",
                {"run_id": run_id, "completed_unique_candidates": completed},
            )
        except Exception as exc:
            try:
                self._event(
                    conn, workflow_id, None, "coverage_certificate_refresh_failed", "warning",
                    f"覆盖证书评估计数刷新失败（不影响工作流终局）：{str(exc)[:200]}",
                )
            except Exception:
                pass


    def _finish(self, conn, workflow_id: str, goal_id: str, steps: list[Any]) -> None:
        goal = conn.execute("SELECT * FROM agent_goals WHERE goal_id=?", (goal_id,)).fetchone()
        self._refresh_sourcing_coverage_assessment(conn, workflow_id)
        target_status = self._sourcing_target_status(conn, goal, workflow_id) if goal is not None else None

        # Sourcing 结果卡：无论达标与否，寻访类工作流终局时生成结果摘要产物。
        # 结果卡生成失败不得阻塞终局流转。
        try:
            result_card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
            assessment_step = next((s for s in steps if s["capability_id"] == "candidate_batch_assessment"), None)
            result_step_id = int(assessment_step["id"]) if assessment_step else 0
            if result_card:
                self._store_artifacts(conn, goal_id, workflow_id, result_step_id, [{
                    "type": "sourcing_result",
                    "title": result_card["title"],
                    "mime_type": "application/json",
                    "content": json.dumps(result_card["summary"], ensure_ascii=False),
                    "metadata": {"action_card": result_card},
                    "validation_status": "passed",
                }])
                self._event(conn, workflow_id, result_step_id or None, "sourcing_result_card_generated", "completed", result_card["title"], result_card)
        except Exception as exc:
            self._event(conn, workflow_id, None, "sourcing_result_card_failed", "failed", f"结果卡生成失败：{str(exc)[:200]}")

        if target_status and target_status["score_75_plus"] < target_status["target"]:
            business_outcome = "completed_needs_review" if target_status["verify_first"] > 0 else "completed_pool_insufficient"
            summary = (
                f"已入库并评估 {target_status['assessed']} 位："
                f"{target_status['score_75_plus']} 位高分，{target_status['verify_first']} 位待核验；"
                f"目标 {target_status['target']} 位合适人选尚未完全达成。"
            )
            error = (
                f"当前确认高分人选 {target_status['score_75_plus']} 位，"
                f"另有 {target_status['verify_first']} 位需要补充简历或核验后再判断；"
                "可先复核 ASA 结果，仍不足时继续发起下一轮补池。"
            )
            conn.execute("UPDATE agent_workflows SET status='blocked',business_outcome=?,active_step_id=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?", (business_outcome, workflow_id))
            conn.execute(
                "UPDATE agent_goals SET status='blocked',business_outcome=?,progress=1,result_summary=?,error=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                (business_outcome, summary, error, goal_id),
            )
            self._event(conn, workflow_id, None, "goal_target_checked", "blocked", "评估完成，但目标人数尚未完全达成", target_status)
            self._auto_strategy_review(conn, workflow_id, "blocked")
            return
        artifact_count = int(conn.execute("SELECT COUNT(*) FROM agent_artifacts WHERE workflow_id=?", (workflow_id,)).fetchone()[0])
        summary = f"目标已完成：{len(steps)} 个步骤全部处理，生成 {artifact_count} 项产物；外部动作均经过独立审批和结果验证。"
        business_outcome = "completed_target_met" if target_status else None
        conn.execute("UPDATE agent_workflows SET status='completed',business_outcome=?,active_step_id=NULL,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE workflow_id=?", (business_outcome, workflow_id))
        conn.execute("UPDATE agent_goals SET status='completed',business_outcome=?,progress=1,result_summary=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE goal_id=?", (business_outcome, summary, goal_id))
        self._event(conn, workflow_id, None, "workflow_completed", "completed", summary)
        if target_status:
            self._auto_strategy_review(conn, workflow_id, "completed")


    def decide_approval(self, approval_id: str, decision: str, note: str = "") -> dict[str, Any]:
        if decision not in {"approve", "reject", "revise"}:
            raise ValueError("审批决定必须是 approve、reject 或 revise")
        conn = self._connect()
        try:
            approval = conn.execute("SELECT * FROM agent_approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if approval is None:
                raise ValueError("审批不存在")
            if approval["status"] != "pending":
                workflow_id = approval["workflow_id"]
                if self._refresh_expired_approvals(conn, workflow_id):
                    conn.commit()
                return self.get_workflow(workflow_id)
            if (
                decision == "approve"
                and approval["expires_at"]
                and str(approval["expires_at"]) < datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ):
                conn.execute(
                    "UPDATE agent_approvals SET status=?,decided_at=datetime('now','localtime'),decision_note='点击时已过期，自动换新' WHERE id=?",
                    (f"expired_{approval['approval_id']}", approval["id"]),
                )
                step = conn.execute("SELECT s.*,w.goal_id FROM agent_workflow_steps s JOIN agent_workflows w ON w.workflow_id=s.workflow_id WHERE s.id=?", (approval["step_id"],)).fetchone()
                if step and step["status"] == "waiting_approval":
                    if self._create_approval(conn, step["goal_id"], approval["workflow_id"], step):
                        self._event(conn, approval["workflow_id"], step["id"], "approval_refreshed", "waiting_approval", f"审批已自动换新：{step['business_label']}")
                conn.commit()
                return self.get_workflow(approval["workflow_id"])
            if decision == "approve" and str(approval["action_type"] or "") == "multi_channel_sourcing":
                preflight = _loads(approval["preflight_json"], {})
                approved_hash = str(preflight.get("strategy_hash") or "")
                current_snapshot = self._sourcing_strategy_snapshot(conn, str(approval["workflow_id"]))
                if not approved_hash or not current_snapshot.get("ready"):
                    raise ValueError("寻访审批缺少完整策略快照，请重新生成策略后审批")
                if not secrets.compare_digest(approved_hash, str(current_snapshot.get("strategy_hash") or "")):
                    raise ValueError("寻访策略已变化，原审批失效，请刷新后重新审批")
            status = {"approve": "approved", "reject": "rejected", "revise": "revision_requested"}[decision]
            # 决策性写入必须是条件更新：并发请求只允许一个把 pending 改为终态，
            # 落败方 rowcount=0，回滚并按"已决策"语义返回，不产生任何副作用。
            # 先写带 approval_id 的过渡态（天然唯一）：(step_id,status) 有唯一约束，
            # 同步骤已存在同终态的旧审批（如重试后再次批准）时直接落终态会冲突，
            # 必须先由下方的 history 标记让位；过渡态在同事务内对外不可见。
            decided = conn.execute(
                "UPDATE agent_approvals SET status=?,decision_note=?,decided_at=datetime('now','localtime') WHERE id=? AND status='pending'",
                (f"deciding_{approval['approval_id']}", note[:500], approval["id"]),
            )
            if decided.rowcount == 0:
                conn.rollback()
                if self._refresh_expired_approvals(conn, approval["workflow_id"]):
                    conn.commit()
                return self.get_workflow(approval["workflow_id"])
            conn.execute(
                """
                UPDATE agent_approvals
                   SET status=status || '_history_' || approval_id
                 WHERE step_id=? AND status=? AND id<>?
                """,
                (approval["step_id"], status, approval["id"]),
            )
            conn.execute(
                "UPDATE agent_approvals SET status=? WHERE id=?",
                (status, approval["id"]),
            )
            if decision == "approve":
                conn.execute("UPDATE agent_workflow_steps SET status='approved',updated_at=datetime('now','localtime') WHERE id=?", (approval["step_id"],))
                conn.execute("UPDATE agent_workflows SET status='queued',updated_at=datetime('now','localtime') WHERE workflow_id=?", (approval["workflow_id"],))
                conn.execute("UPDATE agent_goals SET status='queued',updated_at=datetime('now','localtime') WHERE goal_id=?", (approval["goal_id"],))
                self._event(conn, approval["workflow_id"], approval["step_id"], "approval_decided", "approved", f"已批准一次：{approval['title']}")
            else:
                conn.execute("UPDATE agent_workflow_steps SET status='skipped',error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?", (note or status, approval["step_id"]))
                self._event(conn, approval["workflow_id"], approval["step_id"], "approval_decided", status, f"{approval['title']} 未执行：{note or status}")
            conn.commit()
        finally:
            conn.close()
        if decision == "approve":
            self.service.executor.submit(self.run_workflow, approval["workflow_id"])
        return self.get_workflow(approval["workflow_id"])


    def cancel_workflow(self, workflow_id: str, note: str = "") -> dict[str, Any]:
        conn = self._connect()
        try:
            workflow = conn.execute("SELECT goal_id,status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            conn.execute("UPDATE agent_workflows SET status='cancelled',finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
            conn.execute("UPDATE agent_goals SET status='cancelled',result_summary=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE goal_id=?", (note or "用户取消", workflow["goal_id"]))
            conn.execute(
                """
                UPDATE agent_workflow_steps
                   SET status='cancelled',recovery_json='{}',finished_at=COALESCE(finished_at,datetime('now','localtime')),updated_at=datetime('now','localtime')
                 WHERE workflow_id=?
                   AND status IN ('pending','queued','running','waiting_approval','waiting_external','approved','paused')
                """,
                (workflow_id,),
            )
            conn.execute("UPDATE agent_approvals SET status='cancelled',decided_at=datetime('now','localtime') WHERE workflow_id=? AND status='pending'", (workflow_id,))
            self._event(conn, workflow_id, None, "workflow_cancelled", "cancelled", note or "用户取消目标")
            conn.commit()
        finally:
            conn.close()
        return self.get_workflow(workflow_id)


    def pause_workflow(self, workflow_id: str, note: str = "") -> dict[str, Any]:
        """Freeze a resumable workflow and invalidate any in-flight channel runner."""
        conn = self._connect()
        try:
            workflow = conn.execute(
                "SELECT goal_id,status,active_step_id FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            previous_status = str(workflow["status"] or "")
            if previous_status == "paused":
                return self.get_workflow(workflow_id)
            if previous_status not in {"queued", "running", "waiting_approval", "waiting_external"}:
                raise ValueError(f"当前工作流不能暂停：{previous_status or '状态待同步'}")
            if previous_status == "running":
                conn.execute(
                    "UPDATE agent_workflow_steps SET status='paused',updated_at=datetime('now','localtime') WHERE workflow_id=? AND status='running'",
                    (workflow_id,),
                )
            for step in conn.execute(
                "SELECT id,recovery_json FROM agent_workflow_steps WHERE workflow_id=? AND status='waiting_external'",
                (workflow_id,),
            ).fetchall():
                recovery = _loads(step["recovery_json"], {})
                if not isinstance(recovery, dict):
                    recovery = {}
                recovery["execution_token"] = f"paused-{secrets.token_hex(8)}"
                recovery["paused_at"] = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE agent_workflow_steps SET recovery_json=?,updated_at=datetime('now','localtime') WHERE id=?",
                    (_dumps(recovery), int(step["id"])),
                )
            conn.execute(
                "UPDATE agent_workflows SET status='paused',updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (workflow_id,),
            )
            conn.execute(
                "UPDATE agent_goals SET status='paused',updated_at=datetime('now','localtime') WHERE goal_id=?",
                (workflow["goal_id"],),
            )
            self._event(
                conn, workflow_id, workflow["active_step_id"], "workflow_paused", "paused",
                note or "用户暂停工作流，当前渠道将在本查询单元结束后停止。",
                {"resume_status": previous_status},
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_workflow(workflow_id)


    def resume_workflow(self, workflow_id: str, note: str = "") -> dict[str, Any]:
        """Resume a paused workflow from its last durable state."""
        continuation_requests: list[tuple[int, str, dict[str, Any]]] = []
        run_locally = False
        conn = self._connect()
        try:
            workflow = conn.execute(
                "SELECT goal_id,status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            if workflow["status"] != "paused":
                raise ValueError("当前工作流未处于暂停状态")
            paused_event = conn.execute(
                "SELECT detail_json FROM agent_step_events WHERE workflow_id=? AND event_type='workflow_paused' ORDER BY id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            pause_detail = _loads(paused_event["detail_json"], {}) if paused_event else {}
            resume_status = str(pause_detail.get("resume_status") or "queued")
            if resume_status == "waiting_external":
                rows = conn.execute(
                    "SELECT id,capability_id,recovery_json FROM agent_workflow_steps WHERE workflow_id=? AND status='waiting_external'",
                    (workflow_id,),
                ).fetchall()
                for step in rows:
                    recovery = _loads(step["recovery_json"], {})
                    request = recovery.get("request") if isinstance(recovery, dict) else None
                    if isinstance(request, dict) and request:
                        continuation_requests.append((int(step["id"]), str(step["capability_id"]), dict(request)))
                if not continuation_requests:
                    raise ValueError("暂停前的渠道请求不可恢复，请重试该步骤")
            elif resume_status == "waiting_approval":
                resume_status = "waiting_approval"
            else:
                resume_status = "queued"
                conn.execute(
                    "UPDATE agent_workflow_steps SET status='pending',updated_at=datetime('now','localtime') WHERE workflow_id=? AND status='paused'",
                    (workflow_id,),
                )
                run_locally = True
            conn.execute(
                "UPDATE agent_workflows SET status=?,updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (resume_status, workflow_id),
            )
            conn.execute(
                "UPDATE agent_goals SET status=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                (resume_status, workflow["goal_id"]),
            )
            self._event(
                conn, workflow_id, None, "workflow_resumed", resume_status,
                note or "工作流已恢复执行。", {"resumed_to": resume_status},
            )
            conn.commit()
        finally:
            conn.close()
        for step_id, capability_id, request in continuation_requests:
            self.service.schedule_external_workflow_step(step_id, capability_id, request)
        if run_locally:
            self.service.executor.submit(self.run_workflow, workflow_id)
        return self.get_workflow(workflow_id)


    def archive_workflow(self, workflow_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            workflow = conn.execute(
                "SELECT status,archived_at FROM agent_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            if workflow["status"] in {"queued", "running", "waiting_approval", "waiting_external", "paused"}:
                raise ValueError("执行中的工作流不能归档，请先取消")
            conn.execute(
                "UPDATE agent_workflows SET archived_at=COALESCE(archived_at,datetime('now','localtime')),updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (workflow_id,),
            )
            self._event(conn, workflow_id, None, "workflow_archived", "archived", "工作流已归档，业务记录和审计信息继续保留")
            conn.commit()
        finally:
            conn.close()
        return self.get_workflow(workflow_id)


    def retry_step(self, step_id: int) -> dict[str, Any]:
        audit_retry_request: dict[str, Any] | None = None
        workflow_id = ""
        capability_id = ""
        conn = self._connect()
        try:
            step = conn.execute("SELECT * FROM agent_workflow_steps WHERE id=?", (int(step_id),)).fetchone()
            if step is None or step["status"] not in {"failed", "blocked"}:
                raise ValueError("只能重试失败或阻塞步骤")
            capability = self.service.skills.get(step["capability_id"])
            if capability is None or not capability.idempotent or step["retry_count"] >= capability.retry_limit:
                raise ValueError("该步骤不可重试或已达到重试上限")
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (step["workflow_id"],)).fetchone()
            recovery = _loads(step["recovery_json"], {})
            workflow_id = str(step["workflow_id"])
            capability_id = str(step["capability_id"])
            if recovery.get("retry_mode") == "audit_only" and isinstance(recovery.get("partial_result"), dict):
                request = recovery.get("request") if isinstance(recovery.get("request"), dict) else {}
                audit_retry_request = {
                    "client": request.get("client"),
                    "job": request.get("job"),
                    "_audit_only_result": recovery["partial_result"],
                }
                conn.execute(
                    "UPDATE agent_workflow_steps SET status='waiting_external',retry_count=retry_count+1,error=NULL,updated_at=datetime('now','localtime') WHERE id=?",
                    (step["id"],),
                )
                conn.execute("UPDATE agent_workflows SET status='waiting_external',business_outcome=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                conn.execute("UPDATE agent_goals SET status='waiting_external',business_outcome=NULL,error=NULL,updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                self._event(
                    conn, workflow_id, step["id"], "step_retry", "waiting_external",
                    f"仅重试收尾审计，不重复渠道寻访：{step['business_label']}",
                    {"retry_mode": "audit_only"},
                )
            else:
                conn.execute("UPDATE agent_workflow_steps SET status='pending',retry_count=retry_count+1,error=NULL,updated_at=datetime('now','localtime') WHERE id=?", (step["id"],))
                conn.execute("UPDATE agent_workflows SET status='queued',business_outcome=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
                conn.execute("UPDATE agent_goals SET status='queued',business_outcome=NULL,error=NULL,updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
                self._event(conn, workflow_id, step["id"], "step_retry", "queued", f"重试：{step['business_label']}")
            conn.commit()
        finally:
            conn.close()
        if audit_retry_request is not None:
            self.service.schedule_external_workflow_step(int(step_id), capability_id, audit_retry_request)
        else:
            self.service.executor.submit(self.run_workflow, workflow_id)
        return self.get_workflow(workflow_id)


    def complete_external_step(self, step_id: int, result: dict[str, Any], execution_token: str = "") -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("渠道结果必须是对象")
        conn = self._connect()
        try:
            step = conn.execute("SELECT * FROM agent_workflow_steps WHERE id=?", (int(step_id),)).fetchone()
            if step is None or step["status"] != "waiting_external":
                raise ValueError("当前步骤不在等待渠道结果状态")
            workflow_status = conn.execute(
                "SELECT status FROM agent_workflows WHERE workflow_id=?", (step["workflow_id"],)
            ).fetchone()
            recovery = _loads(step["recovery_json"], {})
            if (
                workflow_status is None
                or workflow_status["status"] != "waiting_external"
                or (execution_token and str(recovery.get("execution_token") or "") != execution_token)
            ):
                return self.get_workflow(str(step["workflow_id"]))
            self.service.validate_external_result(step["capability_id"], result)
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (step["workflow_id"],)).fetchone()
            context = self._workflow_context(conn, step["workflow_id"])
            self.service.apply_external_result(step["capability_id"], context, result, step["workflow_id"])
            previous = _loads(step["output_json"], {})
            receipt_artifacts: list[dict[str, Any]] = []
            capability = self.service.skills.get(step["capability_id"])
            if capability and "external_action_receipt" in capability.artifact_types:
                receipt_artifacts.append(
                    {
                        "type": "external_action_receipt",
                        "title": f"{step['business_label']}回执",
                        "mime_type": "application/json",
                        "content": _dumps(result),
                        "metadata": {"verified": True, "capability_id": step["capability_id"]},
                        "validation_status": "passed",
                    }
                )
            output = {
                **previous,
                "summary": str(previous.get("summary") or f"渠道结果已验证：{step['business_label']}"),
                "external_action_executed": True,
                "external_result": result,
                "artifacts": receipt_artifacts or previous.get("artifacts") or [],
            }
            verification = self._verify_step_result(_row(step), context, output)
            if not verification["ok"]:
                conn.execute(
                    """
                    UPDATE agent_workflow_steps
                       SET status='failed',output_json=?,verification_json=?,error=?,
                           finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    (_dumps({**output, "verification": verification}), _dumps(verification), verification["summary"][:1000], step["id"]),
                )
                conn.execute("UPDATE agent_workflows SET status='failed',updated_at=datetime('now','localtime') WHERE workflow_id=?", (step["workflow_id"],))
                conn.execute("UPDATE agent_goals SET status='failed',error=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (verification["summary"][:1000], workflow["goal_id"]))
                self._event(conn, step["workflow_id"], step["id"], "step_verification_failed", "failed", verification["summary"], verification)
                conn.commit()
                return self.get_workflow(step["workflow_id"])
            receipt_ids = self._store_artifacts(
                conn, workflow["goal_id"], step["workflow_id"], int(step["id"]), receipt_artifacts,
            )
            conn.execute(
                """
                UPDATE agent_artifacts
                   SET validation_status='passed'
                 WHERE workflow_id=? AND step_id=? AND validation_status='pending_execution'
                """,
                (step["workflow_id"], int(step["id"])),
            )
            output["artifact_ids"] = list(dict.fromkeys([*(previous.get("artifact_ids") or []), *receipt_ids]))
            output["verification"] = verification
            conn.execute(
                "UPDATE agent_workflow_steps SET status='completed',output_json=?,verification_json=?,recovery_json='{}',error=NULL,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?",
                (_dumps(output), _dumps(verification), step["id"]),
            )
            conn.execute("UPDATE agent_workflows SET status='queued',updated_at=datetime('now','localtime') WHERE workflow_id=?", (step["workflow_id"],))
            conn.execute("UPDATE agent_goals SET status='queued',updated_at=datetime('now','localtime') WHERE goal_id=?", (workflow["goal_id"],))
            audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
            if audit.get("recovered_without_channel_rerun") is True:
                failed_audit = conn.execute(
                    """
                    SELECT id,detail_json
                    FROM agent_step_events
                    WHERE workflow_id=? AND step_id=? AND event_type='external_audit_failed'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (step["workflow_id"], int(step["id"])),
                ).fetchone()
                if failed_audit is not None:
                    detail = _loads(failed_audit["detail_json"], {})
                    conn.execute(
                        """
                        UPDATE agent_step_events
                           SET event_type='external_audit_recovered',
                               status='resolved',
                               summary=?,
                               detail_json=?
                         WHERE id=?
                        """,
                        (
                            "历史记录：渠道和入库成功，收尾审计曾阻塞；现已仅重跑审计并恢复。",
                            _dumps({**detail, "resolution": "audit_only_recovered"}),
                            int(failed_audit["id"]),
                        ),
                    )
            self._event(conn, step["workflow_id"], step["id"], "external_result_verified", "completed", f"渠道结果已验证：{step['business_label']}", result)
            self._event(conn, step["workflow_id"], step["id"], "step_result_verified", "completed", verification["summary"], {"checks": verification.get("checks") or []})
            self._update_progress(conn, step["workflow_id"], workflow["goal_id"])
            conn.commit()
        finally:
            conn.close()
        self.service.executor.submit(self.run_workflow, step["workflow_id"])
        return self.get_workflow(step["workflow_id"])


    def claim_external_execution(self, step_id: int, request: dict[str, Any]) -> str:
        """Persist a one-shot token so stale workers cannot write after a pause/resume."""
        conn = self._connect()
        try:
            step = conn.execute(
                """
                SELECT s.recovery_json,s.status,w.status AS workflow_status
                  FROM agent_workflow_steps s
                  JOIN agent_workflows w ON w.workflow_id=s.workflow_id
                 WHERE s.id=?
                """,
                (int(step_id),),
            ).fetchone()
            if step is None or step["status"] != "waiting_external" or step["workflow_status"] != "waiting_external":
                return ""
            recovery = _loads(step["recovery_json"], {})
            if not isinstance(recovery, dict):
                recovery = {}
            token = secrets.token_hex(12)
            recovery.update({
                "retry_mode": str(recovery.get("retry_mode") or "sourcing_continuation"),
                "request": {key: value for key, value in request.items() if key != "_workflow_execution_token"},
                "execution_token": token,
                "execution_claimed_at": datetime.now().isoformat(timespec="seconds"),
            })
            claimed = conn.execute(
                """
                UPDATE agent_workflow_steps SET recovery_json=?,updated_at=datetime('now','localtime')
                 WHERE id=? AND status='waiting_external'
                   AND EXISTS (
                       SELECT 1 FROM agent_workflows
                        WHERE workflow_id=(SELECT workflow_id FROM agent_workflow_steps WHERE id=?)
                          AND status='waiting_external'
                   )
                """,
                (_dumps(recovery), int(step_id), int(step_id)),
            )
            conn.commit()
            return token if claimed.rowcount == 1 else ""
        finally:
            conn.close()


    def external_step_is_active(self, step_id: int, execution_token: str = "") -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT s.status,s.recovery_json,w.status AS workflow_status
                  FROM agent_workflow_steps s
                  JOIN agent_workflows w ON w.workflow_id=s.workflow_id
                 WHERE s.id=?
                """,
                (int(step_id),),
            ).fetchone()
            return bool(
                row
                and row["status"] == "waiting_external"
                and row["workflow_status"] == "waiting_external"
                and (not execution_token or str(_loads(row["recovery_json"], {}).get("execution_token") or "") == execution_token)
            )
        finally:
            conn.close()


    def checkpoint_external_continuation(
        self,
        step_id: int,
        result: dict[str, Any],
        request: dict[str, Any],
        execution_token: str = "",
    ) -> dict[str, Any]:
        """Durably checkpoint a sourcing batch before scheduling its cursor continuation."""
        if not isinstance(result, dict) or not isinstance(request, dict):
            raise ValueError("外部续跑检查点必须包含结果和请求")
        query_plan = request.get("query_plan_v1") if isinstance(request.get("query_plan_v1"), dict) else {}
        plan_hash = str(request.get("query_plan_hash") or "")
        plan_ok, _ = query_builders.validate_query_plan_v1(query_plan)
        if not plan_ok or not plan_hash or not secrets.compare_digest(plan_hash, str(query_plan.get("plan_hash") or "")):
            raise ValueError("外部续跑请求未绑定有效的批准查询计划")
        conn = self._connect()
        try:
            step = conn.execute(
                """
                SELECT s.*,w.status AS workflow_status
                  FROM agent_workflow_steps s
                  JOIN agent_workflows w ON w.workflow_id=s.workflow_id
                 WHERE s.id=?
                """,
                (int(step_id),),
            ).fetchone()
            recovery_before = _loads(step["recovery_json"], {}) if step is not None else {}
            if (
                step is None
                or step["status"] != "waiting_external"
                or step["workflow_status"] != "waiting_external"
                or (execution_token and str(recovery_before.get("execution_token") or "") != execution_token)
            ):
                raise ValueError("当前步骤不在可继续的渠道结果状态")
            previous = _loads(step["output_json"], {})
            history = previous.get("continuation_history") if isinstance(previous.get("continuation_history"), list) else []
            summary = result.get("continuation") if isinstance(result.get("continuation"), dict) else {}
            checkpoint = {
                "run_id": result.get("run_id"),
                "completed_batches": summary.get("completed_batches"),
                "remaining_cells": summary.get("remaining_cells"),
                "coverage_status": (
                    result.get("coverage_certificate", {}).get("coverage_status")
                    if isinstance(result.get("coverage_certificate"), dict) else None
                ),
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
            output = {
                **previous,
                "summary": "渠道分页批次已完成，正在按原审批计划续跑",
                "external_action_executed": True,
                "external_result": result,
                "continuation_history": [*history[-19:], checkpoint],
            }
            recovery = {
                "retry_mode": "sourcing_continuation",
                "request": request,
                "partial_result": result,
                "recovery_claim": {
                    "pid": os.getpid(),
                    "claimed_at": datetime.now().isoformat(timespec="seconds"),
                },
            }
            conn.execute(
                """
                UPDATE agent_workflow_steps
                   SET output_json=?,recovery_json=?,error=NULL,updated_at=datetime('now','localtime')
                 WHERE id=?
                   AND status='waiting_external'
                   AND EXISTS (
                       SELECT 1 FROM agent_workflows
                           WHERE workflow_id=? AND status='waiting_external'
                   )
                """,
                (_dumps(output), _dumps(recovery), int(step_id), str(step["workflow_id"])),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("工作流已停止，忽略渠道续跑结果")
            self._event(
                conn,
                str(step["workflow_id"]),
                int(step_id),
                "external_continuation_checkpointed",
                "waiting_external",
                f"分页批次已完成，剩余 {int(summary.get('remaining_cells') or 0)} 个查询单元继续执行",
                checkpoint,
            )
            conn.commit()
            workflow_id = str(step["workflow_id"])
        finally:
            conn.close()
        return self.get_workflow(workflow_id)


    def recover_external_continuations(self) -> int:
        """Reschedule durable cursor continuations after a Core process restart."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT s.id,s.workflow_id,s.capability_id,s.recovery_json
                 FROM agent_workflow_steps s
                  JOIN agent_workflows w ON w.workflow_id=s.workflow_id
                 WHERE s.status='waiting_external'
                   AND w.status NOT IN ('cancelled','completed','superseded','paused')
                   AND s.recovery_json<>'{}'
                ORDER BY s.id
                """
            ).fetchall()
        finally:
            conn.close()
        scheduled = 0
        for row in rows:
            recovery = _loads(row["recovery_json"], {})
            request = recovery.get("request") if isinstance(recovery.get("request"), dict) else None
            if recovery.get("retry_mode") != "sourcing_continuation" or request is None:
                continue
            process_id = os.getpid()
            previous_claim = (
                recovery.get("recovery_claim")
                if isinstance(recovery.get("recovery_claim"), dict)
                else {}
            )
            if int(previous_claim.get("pid") or 0) == process_id:
                continue
            claimed_recovery = {
                **recovery,
                "recovery_claim": {
                    "pid": process_id,
                    "claimed_at": datetime.now().isoformat(timespec="seconds"),
                },
            }
            conn = self._connect()
            try:
                claimed = conn.execute(
                    """
                    UPDATE agent_workflow_steps
                       SET recovery_json=?,updated_at=datetime('now','localtime')
                     WHERE id=? AND status='waiting_external' AND recovery_json=?
                       AND EXISTS (
                           SELECT 1 FROM agent_workflows
                            WHERE workflow_id=? AND status='waiting_external'
                       )
                    """,
                    (_dumps(claimed_recovery), int(row["id"]), str(row["recovery_json"]), str(row["workflow_id"])),
                )
                conn.commit()
                if claimed.rowcount != 1:
                    continue
            finally:
                conn.close()
            self.service.schedule_external_workflow_step(
                int(row["id"]), str(row["capability_id"]), request,
            )
            scheduled += 1
        return scheduled


    def fail_external_step(self, step_id: int, error: str, failure: dict[str, Any] | None = None) -> dict[str, Any]:
        conn = self._connect()
        try:
            step = conn.execute("SELECT * FROM agent_workflow_steps WHERE id=?", (int(step_id),)).fetchone()
            if step is None or step["status"] != "waiting_external":
                return {"ok": False, "error": "步骤已不再等待渠道结果"}
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (step["workflow_id"],)).fetchone()
            failure = failure if isinstance(failure, dict) else {}
            side_effects_completed = failure.get("external_action_executed") is True
            if side_effects_completed:
                previous = _loads(step["output_json"], {})
                partial_result = failure.get("partial_result") if isinstance(failure.get("partial_result"), dict) else {}
                output = {
                    **previous,
                    "summary": error,
                    "external_action_executed": True,
                    "external_result": partial_result,
                }
                conn.execute(
                    "UPDATE agent_workflow_steps SET status='failed',output_json=?,recovery_json=?,error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?",
                    (_dumps(output), _dumps(failure), error[:1000], step["id"]),
                )
                conn.execute("UPDATE agent_workflows SET status='blocked',business_outcome=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?", (step["workflow_id"],))
                conn.execute("UPDATE agent_goals SET status='blocked',business_outcome=NULL,error=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (error[:1000], workflow["goal_id"]))
                self._event(
                    conn, step["workflow_id"], step["id"], "external_audit_failed", "blocked",
                    error[:500],
                    {"phase": failure.get("phase"), "retry_mode": failure.get("retry_mode"), "detail": failure.get("detail")},
                )
            else:
                conn.execute("UPDATE agent_workflow_steps SET status='failed',error=?,finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?", (error[:1000], step["id"]))
                conn.execute("UPDATE agent_workflows SET status='failed',business_outcome=NULL,updated_at=datetime('now','localtime') WHERE workflow_id=?", (step["workflow_id"],))
                conn.execute("UPDATE agent_goals SET status='failed',business_outcome=NULL,error=?,updated_at=datetime('now','localtime') WHERE goal_id=?", (error[:1000], workflow["goal_id"]))
                self._event(conn, step["workflow_id"], step["id"], "external_execution_failed", "failed", f"渠道执行失败：{error[:300]}")
            conn.commit()
            return self.get_workflow(step["workflow_id"])
        finally:
            conn.close()
