from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .workflow_plan import _dumps, _loads, _mask_candidate_name, _row
from .stage_breakdown import assessed_stage_breakdown, assessed_stage_summary


class WorkflowReviewMixin:
    """读取/回顾阶段：目标与工作流查询、步骤输出压缩、候选人分页、
    产物/事件读取、质量指标与反馈记录。

    方法体自 workflow.py 逐字节迁移（P2-1），语义不变。
    """

    def _context_label(self, conn, context: dict[str, Any]) -> str:
        context_type, context_id = context.get("type"), context.get("id")
        if context_type == "job" and context_id:
            row = conn.execute("SELECT c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?", (int(context_id),)).fetchone()
            if row:
                return f"{row['client']} / {row['job']}"
        if context_type == "candidate" and context_id:
            row = conn.execute("SELECT p.display_name,c.name AS client,j.title AS job FROM job_candidates jc JOIN people p ON p.id=jc.person_id LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id WHERE jc.id=?", (int(context_id),)).fetchone()
            if row:
                return f"{row['display_name']} · {row['client'] or ''} / {row['job'] or ''}"
        if context_type == "queue":
            return f"行动队列 · {json.dumps(context.get('filters') or {}, ensure_ascii=False)}"
        return f"{context_type or '全局'} #{context_id or '-'}"


    def _workflow_context(self, conn, workflow_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT context_json FROM agent_workflow_context WHERE workflow_id=? ORDER BY id DESC LIMIT 1", (workflow_id,)
        ).fetchone()
        return _loads(row["context_json"], {}) if row else {"type": "global", "id": None}


    def list_goals(self, status: str = "", limit: int = 30) -> dict[str, Any]:
        conn = self._connect()
        try:
            where = "WHERE g.status=?" if status else ""
            params: list[Any] = [status] if status else []
            params.append(max(1, min(int(limit or 30), 100)))
            rows = conn.execute(
                f"""
                SELECT g.*,w.workflow_id,w.current_stage,w.status AS workflow_status,
                       (SELECT COUNT(*) FROM agent_approvals a WHERE a.goal_id=g.goal_id AND a.status='pending') AS pending_approvals,
                       (SELECT COUNT(*) FROM agent_artifacts ar WHERE ar.goal_id=g.goal_id) AS artifact_count
                FROM agent_goals g LEFT JOIN agent_workflows w ON w.goal_id=g.goal_id
                {where} ORDER BY g.updated_at DESC,g.id DESC LIMIT ?
                """,
                params,
            ).fetchall()
            return {"ok": True, "goals": [self._goal_public(row) for row in rows]}
        finally:
            conn.close()


    def _goal_public(self, row: Any) -> dict[str, Any]:
        item = _row(row)
        item["context"] = _loads(item.pop("context_json", "{}"), {})
        return item


    @staticmethod
    def _compact_step_output(output: dict[str, Any]) -> dict[str, Any]:
        """Keep workflow first paint useful while full output stays on the step endpoint."""
        def size(value: Any) -> int:
            try:
                return len(_dumps(value).encode("utf-8"))
            except (TypeError, ValueError):
                return 0

        def artifact_index(value: Any) -> list[dict[str, Any]]:
            return [
                {
                    key: item.get(key)
                    for key in ("type", "title", "mime_type", "validation_status")
                    if item.get(key) not in (None, "")
                }
                for item in (value if isinstance(value, list) else [])[:12]
                if isinstance(item, dict)
            ]

        def request_index(value: Any) -> dict[str, Any]:
            request = value if isinstance(value, dict) else {}
            preflight = request.get("preflight") if isinstance(request.get("preflight"), dict) else {}
            inner = preflight.get("preflight") if isinstance(preflight.get("preflight"), dict) else preflight
            channels = inner.get("channels") if isinstance(inner, dict) else {}
            return {
                key: request.get(key)
                for key in ("workflow_id", "client", "job", "target_count", "query_plan_hash")
                if request.get(key) not in (None, "")
            } | ({"preflight": {"preflight": {"channels": channels}}} if isinstance(channels, dict) and channels else {})

        def channel_run_index(value: Any) -> list[dict[str, Any]]:
            runs = value if isinstance(value, list) else []
            compact: list[dict[str, Any]] = []
            for item in runs[:20]:
                if not isinstance(item, dict):
                    continue
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                candidates = result.get("candidates")
                compact.append({
                    "channel": item.get("channel"),
                    "status": item.get("status"),
                    "quality": item.get("quality"),
                    "zero_attribution": item.get("zero_attribution"),
                    "result": {
                        "candidates": len(candidates) if isinstance(candidates, list) else int(candidates or 0),
                        "status": result.get("status"),
                        "error": result.get("error"),
                    },
                })
            return compact

        def external_result_index(value: Any) -> dict[str, Any]:
            external = value if isinstance(value, dict) else {}
            result: dict[str, Any] = {
                key: external.get(key)
                for key in ("run_id", "verified", "channel_risk_stop")
                if external.get(key) not in (None, "")
            }
            result["channel_runs"] = channel_run_index(external.get("channel_runs"))
            intake = external.get("intake") if isinstance(external.get("intake"), dict) else {}
            applied = intake.get("applied") if isinstance(intake.get("applied"), dict) else {}
            intake_counts = applied.get("intake") if isinstance(applied.get("intake"), dict) else {}
            if applied:
                result["intake"] = {"applied": {
                    "client": applied.get("client"),
                    "job": applied.get("job"),
                    "ok": applied.get("ok"),
                    "intake": {
                        key: intake_counts.get(key)
                        for key in ("applied", "inserted", "planned", "skipped_existing")
                        if intake_counts.get(key) is not None
                    },
                }}
            shadow = external.get("opencli_shadow") if isinstance(external.get("opencli_shadow"), dict) else {}
            if shadow:
                result["opencli_shadow"] = {
                    "enabled": shadow.get("enabled"),
                    "channels": [
                        {
                            key: item.get(key)
                            for key in ("channel", "status", "comparison")
                            if item.get(key) not in (None, "")
                        }
                        for item in (shadow.get("channels") if isinstance(shadow.get("channels"), list) else [])[:10]
                        if isinstance(item, dict)
                    ],
                }
            for key in ("sourcing_funnel", "coverage_certificate", "continuation"):
                value_for_key = external.get(key)
                if value_for_key is not None and size(value_for_key) <= 24_000:
                    result[key] = value_for_key
            return result

        compact: dict[str, Any] = {"_summary_only": True, "full_detail_available": True}
        for key, value in output.items():
            if key == "artifacts":
                compact[key] = artifact_index(value)
            elif key in {"auto_execute_request", "external_request"}:
                compact[key] = request_index(value)
            elif key == "external_result":
                compact[key] = external_result_index(value)
            elif key == "query_plan_v1" and isinstance(value, dict):
                cells = value.get("cells") if isinstance(value.get("cells"), list) else []
                compact[key] = {
                    "schema_version": value.get("schema_version"),
                    "plan_hash": value.get("plan_hash"),
                    "cell_count": len(cells),
                }
            elif key == "strategy_v2" and isinstance(value, dict):
                compact[key] = {
                    name: value.get(name)
                    for name in (
                        "schema_version", "archetype_id", "input_level", "coverage_report",
                        # 顾问判断是工作流策略首屏的解释层，保留在摘要中；关键词/公司池仍按原规则裁剪。
                        "consultant_judgement",
                    )
                    if value.get(name) is not None
                }
            elif key == "assessment_queue" and isinstance(value, dict):
                compact[key] = {
                    name: item
                    for name, item in value.items()
                    if not isinstance(item, (dict, list))
                }
            elif key == "audit" and isinstance(value, dict):
                compact[key] = {
                    name: item
                    for name, item in value.items()
                    if name not in {"stdout", "stderr"} and not isinstance(item, (dict, list))
                }
            elif size(value) <= 48_000:
                compact[key] = value
            elif isinstance(value, list):
                compact[key] = {"item_count": len(value), "summary_only": True}
            elif isinstance(value, dict):
                compact[key] = {"field_count": len(value), "summary_only": True}
        return compact


    def _step_item(self, row: Any, goal_context: dict[str, Any], *, summary_only: bool = False) -> dict[str, Any]:
        """单步骤的对外结构：解析 JSON 列，并为岗位批量评估步骤实时注入评估队列。"""
        item = _row(row)
        for source, target, default in (
            ("depends_on_json", "depends_on", []), ("input_json", "inputs", {}),
            ("output_json", "output", {}), ("references_json", "references", []),
            ("verification_json", "verification", {}), ("recovery_json", "recovery", {}),
        ):
            item[target] = _loads(item.pop(source), default)
        if (
            item.get("capability_id") == "candidate_batch_assessment"
            and goal_context.get("type") == "job"
            and goal_context.get("id")
        ):
            assessed_items = self.service._current_assessed_candidates(int(goal_context["id"]))
            queue = item["output"].get("assessment_queue")
            if not isinstance(queue, dict):
                queue = {}
                item["output"]["assessment_queue"] = queue
            queue["assessed_items"] = assessed_items
            queue["completed"] = len(assessed_items)
            queue["score_75_plus"] = len([entry for entry in assessed_items if int(entry.get("fit_score") or 0) >= 75])
            queue["verify_first"] = len([entry for entry in assessed_items if entry.get("recommendation") == "verify_first"])
            queue["low_score"] = len([entry for entry in assessed_items if int(entry.get("fit_score") or 0) < 55])
            # 归因口径：把人选当前阶段分布注入队列（stopped=已分流淘汰，不算「没动」）。
            # stage_summary 为标量，compact 压缩后仍保留；与名单/漏斗同一套计数。
            queue["stage_breakdown"] = assessed_stage_breakdown(assessed_items)
            queue["stage_summary"] = assessed_stage_summary(assessed_items)
            if int(queue.get("started") or 0) == 0:
                item["output"]["summary"] = f"本轮没有新增待评估人选；岗位当前已有 {len(assessed_items)} 位评估结果。"
        if summary_only and len(_dumps(item["output"]).encode("utf-8")) > 160_000:
            item["output"] = self._compact_step_output(item["output"])
        return item


    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            if self._refresh_expired_approvals(conn, workflow_id):
                conn.commit()
            workflow = conn.execute("SELECT * FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            goal = conn.execute("SELECT * FROM agent_goals WHERE goal_id=?", (workflow["goal_id"],)).fetchone()
            steps = conn.execute("SELECT * FROM agent_workflow_steps WHERE workflow_id=? ORDER BY sequence", (workflow_id,)).fetchall()
            approvals = conn.execute("SELECT * FROM agent_approvals WHERE workflow_id=? ORDER BY id DESC", (workflow_id,)).fetchall()
            artifacts = conn.execute("SELECT * FROM agent_artifacts WHERE workflow_id=? ORDER BY id DESC", (workflow_id,)).fetchall()
            events = conn.execute("SELECT * FROM agent_step_events WHERE workflow_id=? ORDER BY id DESC LIMIT 100", (workflow_id,)).fetchall()
            goal_context = _loads(goal["context_json"], {})
            step_items = [self._step_item(row, goal_context) for row in steps]
            approval_items = []
            for row in approvals:
                item = _row(row)
                item["preflight"] = _loads(item.pop("preflight_json"), {})
                item["preflight"].setdefault("object_label", self._context_label(conn, item["preflight"].get("object") or {}))
                item["preflight"].setdefault("channel", {
                    "multi_channel_sourcing": "猎聘 + X-SaaS", "job_publish_prepare": "猎聘",
                    "job_publish_execute": "猎聘", "outreach_execute": "猎聘职聊",
                    "client_recommendation": "指定客户渠道", "offer_confirmation": "ASA 内部",
                    "job_library_update": "ASA 内部",
                }.get(item.get("action_type"), "ASA 内部"))
                legacy_effects = {
                    "multi_channel_sourcing": ("不新增候选人、不触达", "搜索结果排重后仅进入待复核，不发送消息"),
                    "job_library_update": ("岗位库保持当前记录", "更新 jobs、positions、position_profiles 派生字段和岗位指标缓存"),
                    "job_publish_prepare": ("岗位尚未填入猎聘发布表单", "只填草稿并读回字段，不正式发布"),
                    "job_publish_execute": ("岗位尚未正式发布", "正式提交岗位，并以结果页或职位列表为准"),
                    "outreach_execute": ("候选人尚未收到本次消息", "发送审批卡中的单条消息并读回会话"),
                    "client_recommendation": ("客户尚未收到本次推荐", "发送锁定版本的推荐报告并等待渠道回执"),
                    "offer_confirmation": ("Offer 条件尚未在 ASA 确认", "记录经人工确认的 Offer 条件，不代表候选人接受"),
                }
                if item.get("action_type") in legacy_effects and item["preflight"].get("before") == "当前业务状态不变":
                    item["preflight"]["before"], item["preflight"]["after"] = legacy_effects[item["action_type"]]
                item.pop("token_hash", None)
                approval_items.append(item)
            artifact_items = []
            for row in artifacts:
                item = _row(row)
                item["metadata"] = _loads(item.pop("metadata_json"), {})
                item["has_content"] = bool(str(item.get("content") or "").strip())
                item["has_file"] = bool(str(item.get("file_path") or "").strip())
                # 工作流详情只返回产物索引。正文与本地路径必须通过受限产物接口按需读取，
                # 避免 sourcing_ticket 等大正文拖慢详情轮询，也不向浏览器暴露绝对路径。
                item.pop("content", None)
                item.pop("file_path", None)
                artifact_items.append(item)
            workflow_item = _row(workflow)
            workflow_item["plan"] = _loads(workflow_item.pop("plan_json"), {})
            plan_ref = self._plan_identity(
                workflow_id,
                workflow_item.get("version"),
                workflow_item["plan"],
                goal_context,
            )
            workflow_item["plan_version"] = plan_ref["version"]
            workflow_item["plan_hash"] = plan_ref["plan_hash"]
            workflow_item.setdefault("business_outcome", None)
            goal_item = self._goal_public(goal)
            goal_item.setdefault("business_outcome", None)
            revision_links = self._workflow_revision_links(conn, workflow_id)
            artifact_summary = self._artifact_summary(workflow_item.get("status"), step_items, artifact_items)
            return {
                "ok": True, "goal": goal_item, "workflow": workflow_item,
                "plan_ref": plan_ref,
                "business_outcome": workflow_item.get("business_outcome") or goal_item.get("business_outcome"),
                **revision_links,
                "steps": step_items, "approvals": approval_items, "artifacts": artifact_items,
                "artifact_summary": artifact_summary,
                "events": [_row(row) for row in events],
                "progress": {"completed": len([s for s in step_items if s["status"] in {"completed", "skipped"}]), "total": len(step_items), "ratio": float(goal["progress"] or 0)},
                "quality": self.quality_metrics()["metrics"],
            }
        finally:
            conn.close()


    @staticmethod
    def _artifact_summary(status: Any, steps: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        if artifacts:
            return {
                "kind": "artifacts",
                "count": len(artifacts),
                "message": f"已生成 {len(artifacts)} 项可查看产物。",
            }

        status_text = str(status or "")
        assessment_step = next(
            (
                step for step in steps
                if step.get("capability_id") == "candidate_batch_assessment" and step.get("status") == "completed"
            ),
            None,
        )
        if assessment_step is not None:
            return {
                "kind": "business_records",
                "count": 0,
                "message": "本轮结果已写入候选人评估记录，不另生成文件产物。",
            }
        if status_text == "planned":
            message = "计划尚未执行，开始后这里会显示业务产物或结果去向。"
        elif status_text in {"queued", "running", "waiting_approval", "waiting_external"}:
            message = "工作流仍在执行，产物会在对应步骤完成并校验后出现。"
        elif status_text == "cancelled":
            message = "工作流已取消；取消前没有生成可查看产物。"
        elif status_text in {"failed", "blocked"}:
            message = "工作流未形成可查看产物，请先处理失败或阻塞步骤。"
        elif status_text == "completed":
            message = "本工作流完成的是核验或状态更新，结果已回写业务记录，不另生成文件产物。"
        else:
            message = "当前没有可查看产物。"
        return {"kind": "none", "count": 0, "message": message}


    def _workflow_revision_links(self, conn: Any, workflow_id: str) -> dict[str, Any]:
        current_id = workflow_id
        direct_successor = ""
        visited = {workflow_id}
        for _ in range(32):
            current = conn.execute(
                "SELECT status FROM agent_workflows WHERE workflow_id=?",
                (current_id,),
            ).fetchone()
            if current is None or current["status"] != "superseded":
                break
            event = conn.execute(
                """
                SELECT detail_json
                FROM agent_step_events
                WHERE workflow_id=? AND event_type='workflow_superseded'
                ORDER BY id DESC LIMIT 1
                """,
                (current_id,),
            ).fetchone()
            detail = _loads(event["detail_json"], {}) if event is not None else {}
            successor_id = str(detail.get("revised_workflow_id") or "").strip()
            if not successor_id or successor_id in visited:
                break
            successor = conn.execute(
                "SELECT 1 FROM agent_workflows WHERE workflow_id=?",
                (successor_id,),
            ).fetchone()
            if successor is None:
                break
            if not direct_successor:
                direct_successor = successor_id
            visited.add(successor_id)
            current_id = successor_id
        return {
            "superseded_by_workflow_id": direct_successor or None,
            "latest_revision_workflow_id": current_id,
        }


    def get_workflow_summary(self, workflow_id: str) -> dict[str, Any]:
        payload = self.get_workflow(workflow_id)
        steps = payload.get("steps") or []
        approvals = payload.get("approvals") or []
        artifacts = payload.get("artifacts") or []
        events = payload.get("events") or []
        workflow = payload.get("workflow") or {}
        goal = payload.get("goal") or {}
        pending_steps = [step for step in steps if step.get("status") in {"pending", "waiting_approval", "waiting_external", "blocked", "failed"}]
        running_steps = [step for step in steps if step.get("status") == "running"]
        next_step = (running_steps or pending_steps or steps[-1:])[0] if steps else {}
        pending_approvals = [item for item in approvals if item.get("status") == "pending"]
        return {
            "ok": True,
            "workflow_id": workflow.get("workflow_id"),
            "goal_id": goal.get("goal_id"),
            "title": goal.get("title") or goal.get("objective"),
            "status": workflow.get("status") or goal.get("status"),
            "superseded_by_workflow_id": payload.get("superseded_by_workflow_id"),
            "latest_revision_workflow_id": payload.get("latest_revision_workflow_id"),
            "business_outcome": workflow.get("business_outcome") or goal.get("business_outcome"),
            "progress": payload.get("progress") or {},
            "current_stage": workflow.get("current_stage"),
            "next_step": {
                "id": next_step.get("id"),
                "sequence": next_step.get("sequence"),
                "capability_id": next_step.get("capability_id"),
                "business_label": next_step.get("business_label"),
                "status": next_step.get("status"),
                "risk_level": next_step.get("risk_level"),
            } if next_step else {},
            "pending_approvals": [
                {
                    "approval_id": item.get("approval_id"),
                    "action_type": item.get("action_type"),
                    "risk_level": item.get("risk_level"),
                    "title": item.get("title"),
                    "expires_at": item.get("expires_at"),
                    "preflight": item.get("preflight") or {},
                }
                for item in pending_approvals[:5]
            ],
            "recent_artifacts": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "artifact_type": item.get("artifact_type"),
                    "title": item.get("title"),
                    "validation_status": item.get("validation_status"),
                    "created_at": item.get("created_at"),
                }
                for item in artifacts[:5]
            ],
            "recent_events": [
                {
                    "id": item.get("id"),
                    "event_type": item.get("event_type"),
                    "status": item.get("status"),
                    "summary": item.get("summary"),
                    "created_at": item.get("created_at"),
                }
                for item in events[:8]
            ],
            "automation_policy": {
                "R0": "内部只读/整理可自动执行",
                "R1": "内部低风险动作可自动执行",
                "R2": "需审批记录，可按预检锁定集合一次确认",
                "R3": "外部影响动作需审批记录、幂等审计和结果回读",
                "R4": "永久禁止自动执行",
            },
        }


    def get_workflow_step(self, workflow_id: str, step_id: int) -> dict[str, Any]:
        """单步骤详情：完整 output（含渠道审计 stdout 与实时注入的评估队列），按需取用。"""
        conn = self._connect()
        try:
            if self._refresh_expired_approvals(conn, workflow_id):
                conn.commit()
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            row = conn.execute(
                "SELECT * FROM agent_workflow_steps WHERE workflow_id=? AND id=?", (workflow_id, int(step_id))
            ).fetchone()
            if row is None:
                raise ValueError("步骤不存在")
            goal = conn.execute("SELECT context_json FROM agent_goals WHERE goal_id=?", (workflow["goal_id"],)).fetchone()
            goal_context = _loads(goal["context_json"], {}) if goal else {}
            return {"ok": True, "workflow_id": workflow_id, "step": self._step_item(row, goal_context)}
        finally:
            conn.close()


    def get_workflow_candidates(self, workflow_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """工作流候选人结果分页：岗位上下文下"已评估或有寻访归因"的人选，只含摘要字段。"""
        conn = self._connect()
        try:
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            goal = conn.execute("SELECT context_json FROM agent_goals WHERE goal_id=?", (workflow["goal_id"],)).fetchone()
            goal_context = _loads(goal["context_json"], {}) if goal else {}
            limit = max(1, min(int(limit or 50), 200))
            offset = max(0, int(offset or 0))
            if goal_context.get("type") != "job" or not goal_context.get("id"):
                return {"ok": True, "workflow_id": workflow_id, "items": [], "total": 0, "limit": limit, "offset": offset}
            job_id = int(goal_context["id"])
            base = """
                FROM job_candidates jc
                JOIN people p ON p.id=jc.person_id
                LEFT JOIN agent_candidate_assessments a ON a.id=(
                    SELECT a2.id FROM agent_candidate_assessments a2
                    JOIN agent_runs r2 ON r2.run_id=a2.run_id
                    WHERE a2.job_candidate_id=jc.id AND a2.is_current=1 AND r2.status='completed'
                    ORDER BY a2.id DESC LIMIT 1
                )
                LEFT JOIN agent_sourcing_attributions sa ON sa.id=(
                    SELECT sa2.id FROM agent_sourcing_attributions sa2
                    WHERE sa2.job_candidate_id=jc.id ORDER BY sa2.id DESC LIMIT 1
                )
                LEFT JOIN source_profiles sp ON sp.id=(
                    SELECT sp2.id FROM source_profiles sp2
                    WHERE sp2.person_id=jc.person_id AND sp2.source_type IN ('liepin','xsaas')
                    ORDER BY sp2.source_date DESC,sp2.id DESC LIMIT 1
                )
                LEFT JOIN candidate_events me ON me.id=(
                    SELECT ce.id FROM candidate_events ce
                    JOIN agent_artifacts cae ON cae.artifact_id=ce.source_id
                        AND cae.artifact_type='mapping_task' AND cae.workflow_id=?
                    WHERE ce.job_candidate_id=jc.id AND ce.source_table='mapping_task'
                      AND ce.event_type='mapping_intake'
                    ORDER BY COALESCE(ce.event_time,'') DESC,ce.id DESC LIMIT 1
                )
                LEFT JOIN agent_artifacts ma ON ma.artifact_id=me.source_id
                    AND ma.artifact_type='mapping_task'
                WHERE jc.job_id=? AND (a.id IS NOT NULL OR sa.id IS NOT NULL OR ma.id IS NOT NULL)
            """
            base_params = (workflow_id, job_id)
            total = int(conn.execute(f"SELECT COUNT(*) {base}", base_params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT jc.id,p.id AS person_id,p.display_name,p.current_company,p.current_title,
                       jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                       a.fit_score,a.fit_level,a.recommendation,
                       sa.channel AS attribution_channel,sa.source_query AS attribution_query,
                       sa.source_round AS attribution_round,sa.workflow_id AS attribution_workflow_id,
                       me.id AS mapping_event_id,me.source_id AS mapping_event_source_id,me.raw_json AS mapping_event_raw_json,
                       ma.artifact_id AS mapping_artifact_id,ma.workflow_id AS mapping_workflow_id,
                       sp.source_type AS resume_source_type,sp.source_date AS resume_source_date,
                       sp.raw_json AS resume_raw_json
                {base}
                ORDER BY (a.fit_score IS NULL),a.fit_score DESC,jc.id DESC LIMIT ? OFFSET ?
                """,
                (*base_params, limit, offset),
            ).fetchall()
            items = []
            for row in rows:
                item = _row(row)
                attribution = None
                if item.get("attribution_channel") is not None:
                    attribution = {
                        "channel": item["attribution_channel"],
                        "source_query": item["attribution_query"],
                        "source_round": item["attribution_round"],
                        "from_workflow": bool(item["attribution_workflow_id"]) and item["attribution_workflow_id"] == workflow_id,
                        "source_type": "sourcing",
                    }
                mapping_lineage = None
                if item.get("mapping_artifact_id"):
                    mapping_raw = _loads(item.get("mapping_event_raw_json"), {})
                    mapping_lineage = {
                        "source_type": "mapping",
                        "workflow_id": str(item.get("mapping_workflow_id") or workflow_id),
                        "artifact_id": str(item["mapping_artifact_id"]),
                        "candidate_index": mapping_raw.get("candidate_index"),
                        "event_id": item.get("mapping_event_id"),
                        "from_workflow": True,
                    }
                    if attribution is None:
                        attribution = {
                            "source_type": "mapping",
                            "workflow_id": mapping_lineage["workflow_id"],
                            "artifact_id": mapping_lineage["artifact_id"],
                            "candidate_index": mapping_lineage["candidate_index"],
                            "from_workflow": True,
                        }
                source_lineage = []
                if mapping_lineage:
                    source_lineage.append(mapping_lineage)
                if item.get("attribution_workflow_id") or item.get("attribution_channel"):
                    source_lineage.append({
                        "source_type": "sourcing",
                        "workflow_id": str(item.get("attribution_workflow_id") or ""),
                        "channel": item.get("attribution_channel") or "",
                        "source_query": item.get("attribution_query") or "",
                        "source_round": item.get("attribution_round") or "",
                        "from_workflow": bool(item.get("attribution_workflow_id")) and item["attribution_workflow_id"] == workflow_id,
                    })
                resume_payload = _loads(item.get("resume_raw_json"), {})
                resume_captured_at = str(resume_payload.get("captured_at") or item.get("resume_source_date") or "")
                resume_capture_status = "complete" if resume_captured_at else "not_requested"
                full_text = str(resume_payload.get("full_text") or "")
                intention = ""
                if "求职意向" in full_text:
                    section = full_text.split("求职意向", 1)[-1].strip()
                    intention = section.splitlines()[0] if section.splitlines() else ""
                if not intention:
                    intention = str(item.get("raw_status") or "")
                items.append({
                    "id": item["id"],
                    "person_id": item["person_id"],
                    "name": _mask_candidate_name(item["display_name"]),
                    "company": item["current_company"],
                    "title": item["current_title"],
                    "fit_score": item["fit_score"],
                    "fit_level": item["fit_level"],
                    "recommendation": item["recommendation"],
                    "stage": item["clean_stage"],
                    "flow_bucket": item["flow_bucket"],
                    "status": item["raw_status"],
                    "assessed": item["fit_score"] is not None,
                    "attribution": attribution,
                    "source_lineage": source_lineage,
                    "updated_at": item["updated_at"],
                    "resume_source_type": item.get("resume_source_type") or "",
                    "resume_capture_status": resume_capture_status,
                    "resume_captured_at": resume_captured_at,
                    "intention": intention[:200],
                })
            return {"ok": True, "workflow_id": workflow_id, "items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()


    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM agent_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if row is None:
                raise ValueError("产物不存在")
            item = _row(row)
            item["metadata"] = _loads(item.pop("metadata_json"), {})
            return {"ok": True, "artifact": item}
        finally:
            conn.close()


    def events_since(self, event_id: int = 0, workflow_id: str = "", limit: int = 100) -> dict[str, Any]:
        conn = self._connect()
        try:
            clauses = ["id>?"]
            params: list[Any] = [max(0, int(event_id or 0))]
            if workflow_id:
                clauses.append("workflow_id=?")
                params.append(workflow_id)
            params.append(max(1, min(int(limit or 100), 500)))
            rows = conn.execute(
                f"SELECT * FROM agent_step_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?", params
            ).fetchall()
            return {"ok": True, "events": [_row(row) for row in rows], "last_event_id": int(rows[-1]["id"]) if rows else int(event_id or 0)}
        finally:
            conn.close()


    def quality_metrics(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            status_rows = conn.execute("SELECT status,COUNT(*) total FROM agent_goals GROUP BY status").fetchall()
            statuses = {row["status"]: int(row["total"]) for row in status_rows}
            total = sum(statuses.values())
            started = total - statuses.get("draft", 0)
            completed = statuses.get("completed", 0)
            revised = statuses.get("superseded", 0)
            step_total, step_failed = conn.execute("SELECT COUNT(*),SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) FROM agent_workflow_steps").fetchone()
            failures = [_row(row) for row in conn.execute("SELECT capability_id,COUNT(*) failures FROM agent_workflow_steps WHERE status='failed' GROUP BY capability_id ORDER BY failures DESC").fetchall()]
            feedback_total, feedback_corrected = conn.execute("SELECT COUNT(*),SUM(CASE WHEN feedback_type='corrected' THEN 1 ELSE 0 END) FROM agent_workflow_feedback").fetchone()
            return {
                "ok": True,
                "metrics": {
                    "goals": total, "started": started, "completed": completed,
                    "plan_adoption_rate": round(started / max(1, total), 4),
                    "goal_completion_rate": round(completed / max(1, started), 4),
                    "plan_revision_rate": round(revised / max(1, total), 4),
                    "step_failure_rate": round(int(step_failed or 0) / max(1, int(step_total or 0)), 4),
                    "feedback_coverage_rate": round(int(feedback_total or 0) / max(1, total), 4),
                    "planner_correction_rate": round(int(feedback_corrected or 0) / max(1, int(feedback_total or 0)), 4),
                    "statuses": statuses, "capability_failures": failures,
                },
            }
        finally:
            conn.close()


    def record_feedback(self, workflow_id: str, feedback_type: str, note: str, correction: dict[str, Any]) -> dict[str, Any]:
        if feedback_type not in {"accurate", "corrected"}:
            raise ValueError("工作流反馈必须是 accurate 或 corrected")
        note = " ".join(str(note or "").split())[:1000]
        if feedback_type == "corrected" and not note:
            raise ValueError("需要调整时必须填写原因")
        conn = self._connect()
        try:
            workflow = conn.execute("SELECT goal_id FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
            if workflow is None:
                raise ValueError("工作流不存在")
            context = self._workflow_context(conn, workflow_id)
            conn.execute(
                "INSERT OR REPLACE INTO agent_workflow_feedback (workflow_id,goal_id,feedback_type,note,correction_json,context_type,context_id) VALUES (?,?,?,?,?,?,?)",
                (workflow_id, workflow["goal_id"], feedback_type, note, _dumps(correction), str(context.get("type") or "global"), str(context.get("id") or "")),
            )
            proposal = None
            if feedback_type == "corrected":
                normalized = re.sub(r"\s+", "", note.lower())
                rule_key = "workflow-routing:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
                existing = conn.execute("SELECT * FROM agent_learning_rules WHERE rule_key=? ORDER BY version DESC LIMIT 1", (rule_key,)).fetchone()
                matching = conn.execute("SELECT COUNT(*),COUNT(DISTINCT context_type||':'||COALESCE(context_id,'')) FROM agent_workflow_feedback WHERE feedback_type='corrected' AND REPLACE(LOWER(COALESCE(note,'')),' ','')=?", (normalized,)).fetchone()
                support, contexts = int(matching[0] or 0), int(matching[1] or 0)
                threshold = int(self.service.config["learning"]["minimum_support"])
                minimum_contexts = int(self.service.config["learning"]["minimum_candidates"])
                status = "pending" if support >= threshold and contexts >= minimum_contexts else "collecting"
                rule_json = {"context_type": context.get("type"), "instruction": note, **{key: value for key, value in correction.items() if key in {"remove_capabilities", "append_capabilities"}}}
                if existing:
                    conn.execute("UPDATE agent_learning_rules SET support_count=?,candidate_count=?,status=?,rule_json=?,last_supported_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE id=?", (support, contexts, status, _dumps(rule_json), existing["id"]))
                    rule_id = int(existing["id"])
                else:
                    cursor = conn.execute("INSERT INTO agent_learning_rules (rule_key,scope_type,rule_type,rule_json,status,support_count,candidate_count,last_supported_at) VALUES (?,'global','workflow_routing',?,?,?,?,datetime('now','localtime'))", (rule_key, _dumps(rule_json), status, support, contexts))
                    rule_id = int(cursor.lastrowid)
                proposal = {"rule_id": rule_id, "status": status, "support_count": support, "context_count": contexts}
            conn.commit()
            return {"ok": True, "feedback_type": feedback_type, "learning_proposal": proposal, "quality": self.quality_metrics()["metrics"]}
        finally:
            conn.close()

