from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _row(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row else {}


def _mask_candidate_name(value: Any) -> str:
    """对外列表只暴露遮罩姓名；已遮罩（含 * / 某 / 先生 / 女士 / 老师）的保持原样。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "*" in text or "某" in text or text.endswith(("先生", "女士", "老师")):
        return text
    return text[:1] + "**"


STAGE_ORDER = [
    "job_intake", "jd_calibration", "job_library", "search_strategy", "sourcing", "resume_capture",
    "assessment", "verification", "outreach", "reply", "recommendation", "interview",
    "salary", "decision", "offer", "onboarding", "retrospective",
]

SOURCING_OBJECTIVE_TOKENS = ("补充", "补池", "寻访", "找人", "搜索")

BUSINESS_OUTCOMES = (
    "completed_target_met",
    "completed_needs_review",
    "completed_pool_insufficient",
    "failed_technical",
)

# 业务终态的中文语义（与 classify_business_outcome 判定口径一一对应，单一来源）。
# 前端 statusMapping.ts 在仓外另有文案，含义必须与这里保持一致：
# completed_* 三态都是"本轮完成"（仅达标情况不同），只有 failed_technical 是技术失败。
BUSINESS_OUTCOME_LABELS = {
    "completed_target_met": "本轮完成，达成目标人数",
    "completed_needs_review": "本轮完成，合格人数不足，有待复核人选",
    "completed_pool_insufficient": "本轮完成，合格人数不足",
    "failed_technical": "技术失败（执行过程中断，未完成本轮寻访）",
}


def sourcing_target_stats(conn: Any, objective: Any, context: dict[str, Any], workflow_id: str) -> dict[str, int] | None:
    """寻访类目标的达标信号（score_75_plus/verify_first 等）；非寻访目标返回 None。"""
    objective_text = str(objective or "")
    if not any(token in objective_text for token in SOURCING_OBJECTIVE_TOKENS):
        return None
    if context.get("type") != "job" or not context.get("id"):
        return None
    match = re.search(r"(\d+)\s*(?:位|个|人)", objective_text)
    target = (min(100, int(match.group(1))) if match else 0) or 10
    step = conn.execute(
        """
        SELECT output_json FROM agent_workflow_steps
        WHERE workflow_id=? AND capability_id='candidate_batch_assessment'
        ORDER BY sequence DESC LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    queue = _loads(step["output_json"], {}).get("assessment_queue") if step else {}
    if not isinstance(queue, dict):
        queue = {}
    stats = {
        "target": int(target),
        "assessed": int(queue.get("completed") or queue.get("assessed") or 0),
        "score_75_plus": int(queue.get("score_75_plus") or 0),
        "verify_first": int(queue.get("verify_first") or 0),
        "low_score": int(queue.get("low_score") or 0),
    }
    if stats["assessed"]:
        return stats
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS assessed,
                   SUM(CASE WHEN a.fit_score>=75 THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN a.recommendation='verify_first' THEN 1 ELSE 0 END) AS verify_first,
                   SUM(CASE WHEN a.fit_score<55 THEN 1 ELSE 0 END) AS low_score
            FROM agent_candidate_assessments a
            JOIN job_candidates jc ON jc.id=a.job_candidate_id
            JOIN agent_runs r ON r.run_id=a.run_id
            WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
            """,
            (int(context["id"]),),
        ).fetchone()
        stats.update({
            "assessed": int(row["assessed"] or 0),
            "score_75_plus": int(row["high"] or 0),
            "verify_first": int(row["verify_first"] or 0),
            "low_score": int(row["low_score"] or 0),
        })
    except Exception:
        pass
    return stats


def classify_business_outcome(conn: Any, workflow_id: str) -> str | None:
    """从库中数据推导工作流的业务终态（business_outcome），与引擎写入口径一致。

    - 非寻访类目标（不符合寻访判定条件）→ None
    - 终端为 failed（步骤失败/异常路径）→ 'failed_technical'
    - 寻访类且 score_75_plus >= target → 'completed_target_met'
    - 寻访类未达标但仍有待人工复核人选（verify_first > 0）→ 'completed_needs_review'
    - 寻访类未达标且待复核为 0 → 'completed_pool_insufficient'
    - blocked 的步骤阻塞/缺输入来源（无 goal_target_checked 终局事件）→ None
    - 非终局状态（queued/running/waiting_*/cancelled/superseded 等）→ None
    """
    row = conn.execute(
        """
        SELECT w.status AS workflow_status,g.objective AS objective,g.context_json AS context_json
        FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
        WHERE w.workflow_id=?
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        return None
    stats = sourcing_target_stats(conn, row["objective"], _loads(row["context_json"], {}), workflow_id)
    if stats is None:
        return None
    status = str(row["workflow_status"] or "")
    if status == "failed":
        return "failed_technical"
    if status not in {"blocked", "completed"}:
        return None
    if status == "blocked":
        checked = conn.execute(
            "SELECT 1 FROM agent_step_events WHERE workflow_id=? AND event_type='goal_target_checked' LIMIT 1",
            (workflow_id,),
        ).fetchone()
        if checked is None:
            return None
    if stats["score_75_plus"] >= stats["target"]:
        return "completed_target_met"
    if stats["verify_first"] > 0:
        return "completed_needs_review"
    return "completed_pool_insufficient"


class WorkflowPlanMixin:
    """规划阶段：上下文解析、模板/LLM 计划、路由规则、计划校验、
    目标创建与策略修订/撤回。

    方法体自 workflow.py 逐字节迁移（P2-1），语义不变。
    """

    MAX_STEPS = 12

    SEMANTIC_ACTION_LABELS = {
        "candidate_sourcing": ("sourcing", "寻访"),
        "new_candidate_outreach": ("sourcing", "寻访"),
        "candidate_outreach": ("outreach", "候选人触达"),
        "candidate_review": ("candidate_review", "候选人核验"),
        "job_publish": ("job_publish", "岗位发布"),
        "job_split": ("job_library", "岗位拆分"),
        "job_archive": ("job_library", "岗位归档"),
        "recommendation": ("recommendation_report", "推荐报告"),
        "salary": ("salary", "谈薪处理"),
    }

    STANDARD_PLAYBOOK_MARKERS = {
        "job_library_update", "job_publish_prepare", "multi_channel_sourcing",
        "recommendation_report", "outreach_prepare", "salary_verification",
        "interview_followup", "offer_confirmation",
    }

    ACTION_LABELS = (
        ("multi_channel_sourcing", "sourcing", "寻访"),
        ("job_publish_execute", "job_publish", "岗位发布"),
        ("job_publish_prepare", "job_publish", "岗位发布准备"),
        ("job_library_update", "job_library", "岗位库更新"),
        ("outreach_execute", "outreach", "候选人触达"),
        ("client_recommendation", "recommendation", "客户推荐"),
        ("recommendation_report", "recommendation_report", "推荐报告"),
        ("salary_negotiation", "salary", "谈薪处理"),
        ("candidate_batch_assessment", "assessment", "批量评估"),
    )


    @classmethod
    def _action_label(cls, steps: list[dict[str, Any]]) -> tuple[str, str]:
        capability_ids = {str(step.get("capability_id") or "") for step in steps}
        for capability_id, action_key, label in cls.ACTION_LABELS:
            if capability_id in capability_ids:
                return action_key, label
        first = steps[0] if steps else {}
        return str(first.get("capability_id") or "workflow"), str(first.get("business_label") or "工作流")


    @staticmethod
    def _target_candidate_count(objective: str) -> str:
        match = re.search(
            r"(\d{1,3})\s*(?:名|位|个)?\s*(?:合适的?|匹配的?)?\s*(?:候选人|人选)",
            objective,
        )
        return f"{int(match.group(1))}人" if match else ""


    def _title_context_label(self, conn, selected: dict[str, Any]) -> str:
        context_type = str(selected.get("type") or "global")
        context_id = selected.get("id")
        if context_type == "job" and context_id:
            row = conn.execute(
                """
                SELECT c.name AS client,j.title AS job
                FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                """,
                (int(context_id),),
            ).fetchone()
            if row:
                return f"{row['client']}｜{row['job']}"
        if context_type == "candidate" and context_id:
            row = conn.execute(
                """
                SELECT p.display_name AS candidate,c.name AS client,j.title AS job
                FROM job_candidates jc
                JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id
                LEFT JOIN clients c ON c.id=j.client_id
                WHERE jc.id=?
                """,
                (int(context_id),),
            ).fetchone()
            if row:
                project = " / ".join(value for value in (row["client"], row["job"]) if value)
                return f"{row['candidate']}｜{project}" if project else str(row["candidate"])
        return ""


    def _format_goal_title(
        self,
        conn,
        objective: str,
        selected: dict[str, Any],
        steps: list[dict[str, Any]],
        *,
        round_number: int | None = None,
    ) -> str:
        action_key, action_label = self._action_label(steps)
        understanding = selected.get("intent_understanding") if isinstance(selected.get("intent_understanding"), dict) else {}
        semantic_action = str(understanding.get("action") or "")
        if semantic_action in self.SEMANTIC_ACTION_LABELS:
            action_key, action_label = self.SEMANTIC_ACTION_LABELS[semantic_action]
            if semantic_action == "recommendation" and self._recommendation_requires_send(objective):
                action_key, action_label = "recommendation", "客户推荐"
        if action_key == "sourcing":
            action_label = f"第{round_number}轮寻访" if round_number else "寻访记录"
            target_count = self._target_candidate_count(objective)
            if target_count:
                action_label += f" · {target_count}"
        context_label = self._title_context_label(conn, selected)
        if context_label:
            return f"{context_label}｜{action_label}"[:80]
        compact_objective = re.split(r"[。；;]", objective, maxsplit=1)[0].strip()
        return f"{action_label}｜{compact_objective}"[:80]


    def _next_round_number(
        self,
        conn,
        selected: dict[str, Any],
        action_key: str,
    ) -> int | None:
        if action_key != "sourcing":
            return None
        rows = conn.execute(
            """
            SELECT w.plan_json
            FROM agent_goals g JOIN agent_workflows w ON w.goal_id=g.goal_id
            WHERE g.context_type=? AND COALESCE(g.context_id,-1)=COALESCE(?,-1)
              AND w.status NOT IN ('cancelled','superseded')
            ORDER BY g.id
            """,
            (str(selected.get("type") or "global"), selected.get("id")),
        ).fetchall()
        count = 0
        for row in rows:
            plan = _loads(row["plan_json"], {})
            previous_key, _ = self._action_label(plan.get("steps") if isinstance(plan.get("steps"), list) else [])
            if previous_key == action_key:
                count += 1
        return count + 1


    def _refresh_goal_titles(self) -> None:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT g.id,g.objective,g.context_json,w.plan_json,w.status
                FROM agent_goals g JOIN agent_workflows w ON w.goal_id=g.goal_id
                ORDER BY g.id
                """
            ).fetchall()
            round_counts: dict[tuple[str, Any, str], int] = {}
            updates: list[tuple[str, int]] = []
            for row in rows:
                selected = _loads(row["context_json"], {})
                plan = _loads(row["plan_json"], {})
                steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
                action_key, _ = self._action_label(steps)
                counter_key = (str(selected.get("type") or "global"), selected.get("id"), action_key)
                round_number = None
                is_revision = bool(selected.get("revision_number"))
                if is_revision or str(row["status"] or "") in {"cancelled", "superseded"}:
                    continue
                if action_key == "sourcing":
                    round_number = round_counts.get(counter_key, 0) + 1
                    round_counts[counter_key] = round_number
                title = self._format_goal_title(
                    conn,
                    str(row["objective"] or ""),
                    selected,
                    steps,
                    round_number=round_number,
                )
                updates.append((title, int(row["id"])))
            if updates:
                conn.executemany(
                    "UPDATE agent_goals SET title=?,updated_at=updated_at WHERE id=?",
                    updates,
                )
                conn.commit()
        finally:
            conn.close()


    def _resolve_context(self, objective: str, context: dict[str, Any]) -> dict[str, Any]:
        selected = self.service._normalize_copilot_context(context)
        if selected.get("type") in {"job", "candidate"} and selected.get("id"):
            return selected
        conn = self._connect()
        try:
            has_metrics = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_pipeline_metrics'"
            ).fetchone() is not None
            priority_select = ",COALESCE(m.priority,'') AS priority" if has_metrics else ",'' AS priority"
            priority_join = "LEFT JOIN job_pipeline_metrics m ON m.job_id=j.id" if has_metrics else ""
            jobs = conn.execute(
                f"""
                SELECT j.id,c.name AS client,j.title AS job{priority_select}
                FROM jobs j JOIN clients c ON c.id=j.client_id
                {priority_join}
                WHERE j.status NOT LIKE '%关闭%'
                ORDER BY LENGTH(c.name)+LENGTH(j.title) DESC
                """
            ).fetchall()
            matched_jobs = [row for row in jobs if row["client"] in objective and row["job"] in objective]
            if len(matched_jobs) == 1:
                return {"type": "job", "id": int(matched_jobs[0]["id"]), "page": selected.get("page") or "positions", "filters": {}}
            matched_clients = {row["client"] for row in jobs if row["client"] and row["client"] in objective}
            if len(matched_clients) == 1:
                client = next(iter(matched_clients))
                fuzzy_jobs = []
                for row in jobs:
                    if row["client"] != client:
                        continue
                    title_parts = [
                        part.strip()
                        for part in re.split(r"[（(／/]", str(row["job"] or ""))
                        if len(part.strip()) >= 4
                    ]
                    if any(part in objective for part in title_parts):
                        fuzzy_jobs.append(row)
                if len(fuzzy_jobs) == 1:
                    return {"type": "job", "id": int(fuzzy_jobs[0]["id"]), "page": selected.get("page") or "positions", "filters": {}}
                p0_jobs = [row for row in fuzzy_jobs if str(row["priority"] or "").startswith("P0")]
                if len(p0_jobs) == 1:
                    return {"type": "job", "id": int(p0_jobs[0]["id"]), "page": selected.get("page") or "positions", "filters": {}}
            people = conn.execute(
                """
                SELECT jc.id,p.display_name,c.name AS client,j.title AS job
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                ORDER BY jc.id DESC
                """
            ).fetchall()
            name_matches = [row for row in people if row["display_name"] and row["display_name"] in objective]
            matched_people = [
                row for row in name_matches
                if (row["client"] and row["client"] in objective) or (row["job"] and row["job"] in objective)
            ] or name_matches
            if len(matched_people) == 1:
                return {"type": "candidate", "id": int(matched_people[0]["id"]), "page": "candidates", "filters": {}}
        finally:
            conn.close()
        return selected


    @staticmethod
    def _step(capability_id: str, label: str, stage: str, reason: str, depends: list[str] | None = None) -> dict[str, Any]:
        return {
            "capability_id": capability_id, "business_label": label, "business_stage": stage,
            "reason": reason, "depends_on": depends or [], "inputs": {},
        }


    def _template_plan(self, objective: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        text = objective.lower()
        understanding = context.get("intent_understanding") if isinstance(context.get("intent_understanding"), dict) else {}
        semantic_action = str(understanding.get("action") or "")
        use_keyword_fallback = semantic_action in {"", "none"}
        steps: list[dict[str, Any]] = []
        add = lambda *args: steps.append(self._step(*args))

        if semantic_action == "candidate_review":
            if context.get("type") == "job":
                add("job_diagnosis", "锁定岗位核验范围", "jd_calibration", "读取岗位漏斗、风险提示和当前待核验人选，不触发外部渠道")
                add("talent_pool_search", "整理候选人核验队列", "verification", "优先继承上一轮指出的判断过期、待确认和已触达未回复人选", ["job_diagnosis"])
                add("candidate_pool_filter", "候选池分级过滤", "assessment", "按岗位硬性证据（简历原文）批量分级过滤，输出 A/B/C 名单并排除禁挖", ["talent_pool_search"])
                add("candidate_batch_assessment", "生成逐人核验点", "assessment", "基于现有 v3 简历和评估结果形成可推进、待核验和停止建议", ["candidate_pool_filter"])
            elif context.get("type") == "candidate":
                add("candidate_assessment", "复核当前人岗判断", "assessment", "基于当前岗位、简历和已有评估重新检查匹配判断")
                add("verification_plan", "生成候选人核验清单", "verification", "列出证据缺口和需要顾问确认的关键问题", ["candidate_assessment"])
            else:
                add("candidate_batch_assessment", "生成批量核验点", "assessment", "基于当前队列形成逐人核验清单")
        elif (
            semantic_action in {"job_split", "job_archive"}
            or (
                use_keyword_fallback
                and any(token in text for token in ("更新", "拆分", "新建", "建立"))
                and any(token in text for token in ("岗位", "职位", "岗位库"))
            )
        ):
            add("job_diagnosis", "诊断当前岗位库状态", "jd_calibration", "先读取当前 jobs、positions 与岗位画像")
            add("job_library_update", "更新岗位库", "job_library", "写入岗位库属于内部高影响动作，必须单次确认", ["job_diagnosis"])
        elif semantic_action == "job_publish" or (
            use_keyword_fallback
            and any(token in text for token in ("发布", "上架", "发"))
            and any(token in text for token in ("岗位", "职位"))
        ):
            add("jd_calibration", "校准岗位要求", "jd_calibration", "发布前核对岗位名称、地点、薪资和硬门槛")
            add("job_publish_prepare", "准备岗位发布", "job_intake", "生成发布草稿、预检并读回关键字段", ["jd_calibration"])
            add("job_publish_execute", "发布猎聘岗位", "job_intake", "正式发布属于对外动作，必须批量确认后逐项审计", ["job_publish_prepare"])
        elif (
            context.get("type") == "job"
            and use_keyword_fallback
            and "推进" in text
            and not any(token in text for token in ("推进人选", "推进候选人", "已推进", "推进到"))
        ):
            add("job_diagnosis", "诊断岗位当前进展", "jd_calibration", "核对岗位要求、优先级、漏斗和当前阻塞")
            add("talent_pool_search", "盘点并排重现有人才池", "sourcing", "先复用历史人才库并识别未复核、停滞和判断过期人选", ["job_diagnosis"])
            add("candidate_batch_assessment", "复核现有人选与证据缺口", "assessment", "优先处理当前候选池，形成可推进、待核验和停止建议", ["talent_pool_search"])
            add("search_strategy", "补齐岗位寻访策略", "search_strategy", "根据现有人才池缺口更新目标公司、关键词和渠道配比", ["candidate_batch_assessment"])
            add("multi_channel_sourcing", "准备新一轮多渠道寻访", "sourcing", "仅在现有人才池不足时进入猎聘和 X-SaaS，外部执行前单独确认", ["search_strategy"])
        elif semantic_action in {"candidate_sourcing", "new_candidate_outreach"} or (
            use_keyword_fallback
            and (
                any(token in text for token in ("补充", "补池", "寻访", "找人", "搜索", "搜人"))
                or (
                    any(target in text for target in ("人选", "候选人"))
                    and any(action in text for action in ("找", "搜", "补", "寻访"))
                )
            )
        ):
            add("job_diagnosis", "诊断岗位人才缺口", "jd_calibration", "先确认岗位要求、优先级和当前漏斗")
            add("talent_pool_search", "检索历史人才库", "sourcing", "优先复用已有候选人并完成排重", ["job_diagnosis"])
            add("search_strategy", "生成多渠道寻访策略", "search_strategy", "根据缺口制定目标公司和关键词", ["talent_pool_search"])
            add("multi_channel_sourcing", "执行多渠道寻访", "sourcing", "猎聘和 X-SaaS 浏览器执行需要人工确认", ["search_strategy"])
            add("candidate_batch_assessment", "评估新增候选人", "assessment", "对新增关系自动评估并分流", ["multi_channel_sourcing"])
        elif semantic_action == "recommendation" or (
            use_keyword_fallback
            and any(token in text for token in ("推荐报告", "可推荐", "推荐材料", "推荐给客户"))
        ):
            add("candidate_assessment", "复核人岗判断", "assessment", "报告必须基于当前证据和岗位硬门槛")
            add("verification_plan", "检查待核验信息", "verification", "先暴露报告中的证据缺口", ["candidate_assessment"])
            add("matching_report", "生成匹配分析", "recommendation", "形成内部可审计的人岗分析", ["verification_plan"])
            add("recommendation_report", "生成嘉驰推荐报告", "recommendation", "生成客户可预览的推荐报告草稿", ["matching_report"])
            if self._recommendation_requires_send(objective):
                add("client_recommendation", "提交客户推荐", "recommendation", "对外提交前锁定推荐报告并单独确认客户渠道", ["recommendation_report"])
            if any(token in text for token in ("谈薪", "薪资", "竞争offer", "竞争 offer")):
                add("salary_verification", "核验薪资证据", "salary", "整理谈薪材料前先检查流水、期望和竞争机会证据", ["recommendation_report"])
                add("salary_negotiation", "整理谈薪材料", "salary", "输出薪资差距、风险和候选人决策下一步", ["salary_verification"])
        elif semantic_action == "candidate_outreach" or (
            use_keyword_fallback
            and any(token in text for token in ("正向回复", "回复", "跟进", "触达", "联系"))
        ):
            add("reply_triage", "识别回复与待办", "reply", "先区分正向回复、薪资、地点和拒绝信号")
            add("communication_draft_batch", "生成沟通草稿", "outreach", "根据当前阶段和证据生成未发送草稿", ["reply_triage"])
            add("outreach_prepare", "锁定触达草稿", "outreach", "锁定本批候选人的待发送文案和预检对象", ["communication_draft_batch"])
            if semantic_action == "candidate_outreach" or (
                context.get("type") in {"candidate", "queue"} and any(token in text for token in ("发送", "触达", "联系"))
            ):
                add("outreach_execute", "执行候选人触达", "outreach", "发送消息属于对外动作，必须批量确认后逐人审计", ["outreach_prepare"])
        elif semantic_action == "salary" or (
            use_keyword_fallback
            and any(token in text for token in ("谈薪", "薪资", "竞争offer", "竞争 offer"))
        ):
            add("salary_verification", "核验薪资证据", "salary", "区分税务、工资和一次性收入证据")
            add("salary_negotiation", "整理谈薪风险", "salary", "分析差距、竞争机会和决策时间线", ["salary_verification"])
            add("decision_coaching", "生成决策辅导方案", "decision", "针对非薪资顾虑形成沟通方案", ["salary_negotiation"])
        elif any(token in text for token in ("面试", "终面", "客户反馈")):
            add("interview_followup", "整理面试与客户反馈", "interview", "记录阶段事实并生成后续核验清单")
        elif any(token in text for token in ("offer", "入职", "onboard")):
            add("offer_confirmation", "确认 Offer 条件", "offer", "Offer 条件和状态变化必须人工确认")
            add("onboarding_followup", "创建入职跟进", "onboarding", "记录入职时间、背调和风险", ["offer_confirmation"])
            add("project_retrospective", "生成项目复盘", "retrospective", "沉淀渠道、判断和推进效果", ["onboarding_followup"])
        else:
            add("job_diagnosis", "诊断当前招聘状态", "jd_calibration", "先识别当前阶段、阻塞和优先行动")

        if len(steps) > self.MAX_STEPS:
            raise ValueError(f"目标计划超过 {self.MAX_STEPS} 步限制")
        keys: list[str] = []
        for index, step in enumerate(steps, 1):
            step["step_key"] = f"step_{index}_{step['capability_id']}"
            mapped = []
            for dependency in step.pop("depends_on"):
                match = next((key for key in keys if key.endswith("_" + dependency)), "")
                if match:
                    mapped.append(match)
            step["depends_on"] = mapped
            keys.append(step["step_key"])
        return steps


    @staticmethod
    def _recommendation_requires_send(objective: str) -> bool:
        return any(
            token in str(objective or "")
            for token in ("推荐给客户", "提交客户", "推给客户", "客户推荐")
        )


    def _plan(self, objective: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        fallback = self._template_plan(objective, context)
        understanding = context.get("intent_understanding") if isinstance(context.get("intent_understanding"), dict) else {}
        if str(understanding.get("action") or "") in self.SEMANTIC_ACTION_LABELS:
            return self._apply_routing_rules(fallback, context)
        if any(step["capability_id"] in self.STANDARD_PLAYBOOK_MARKERS for step in fallback):
            return self._apply_routing_rules(fallback, context)
        capabilities = [
            {
                "id": item["id"], "label": item["label"], "stage": item["business_stage"],
                "risk_level": item["risk_level"], "supported_contexts": item["supported_contexts"],
                "input_schema": item["input_schema"],
            }
            for item in self.service.skills.list()
            if item.get("enabled") and context.get("type") in item.get("supported_contexts", [])
        ]
        try:
            proposed = self.service.llm.plan_workflow({
                "objective": objective, "context": context, "capabilities": capabilities,
                "approved_routing_rules": self._routing_instructions(context),
                "fallback_template": [{"capability_id": item["capability_id"], "reason": item["reason"]} for item in fallback],
            })
            raw_steps = proposed.get("steps") if isinstance(proposed, dict) else None
            if not isinstance(raw_steps, list) or not raw_steps:
                return self._apply_routing_rules(fallback, context)
            steps: list[dict[str, Any]] = []
            for index, raw in enumerate(raw_steps[: self.MAX_STEPS], 1):
                if not isinstance(raw, dict):
                    raise ValueError("Planner 步骤必须是对象")
                capability = self.service.skills.get(str(raw.get("capability_id") or ""))
                if capability is None:
                    raise ValueError("Planner 引用了非白名单能力")
                dependency_indexes = raw.get("depends_on") or ([index - 1] if index > 1 else [])
                if not isinstance(dependency_indexes, list) or any(not isinstance(value, int) or value < 1 or value >= index for value in dependency_indexes):
                    raise ValueError("Planner 依赖关系无效")
                raw_inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
                safe_inputs = {key: value for key, value in raw_inputs.items() if not str(key).startswith("_")}
                steps.append({
                    "capability_id": capability.id, "business_label": capability.label or capability.id,
                    "business_stage": capability.business_stage, "reason": str(raw.get("reason") or "由 ASA 目标规划器选择")[:500],
                    "depends_on": [f"step_{value}_{steps[value - 1]['capability_id']}" for value in dependency_indexes],
                    "inputs": safe_inputs, "step_key": f"step_{index}_{capability.id}",
                })
            return self._apply_routing_rules(steps, context)
        except Exception:
            return self._apply_routing_rules(fallback, context)


    def _routing_instructions(self, context: dict[str, Any]) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT rule_json FROM agent_learning_rules WHERE status='active' AND rule_type='workflow_routing' ORDER BY version,id").fetchall()
            instructions = []
            for row in rows:
                rule = _loads(row["rule_json"], {})
                if rule.get("context_type") not in (None, "", context.get("type")):
                    continue
                if rule.get("instruction"):
                    instructions.append(str(rule["instruction"])[:500])
            return instructions
        finally:
            conn.close()


    def _apply_routing_rules(self, steps: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT rule_json FROM agent_learning_rules WHERE status='active' AND rule_type='workflow_routing' ORDER BY version,id"
            ).fetchall()
        except Exception:
            rows = []
        finally:
            conn.close()
        result = list(steps)
        for row in rows:
            rule = _loads(row["rule_json"], {})
            if rule.get("context_type") not in (None, "", context.get("type")):
                continue
            removable = set(rule.get("remove_capabilities") or [])
            result = [step for step in result if step["capability_id"] not in removable or (self.service.skills.get(step["capability_id"]) or type("X", (), {"risk_level": "R4"})()).risk_level in {"R2", "R3"}]
        return result[: self.MAX_STEPS]


    def _validate_plan(self, steps: list[dict[str, Any]], context: dict[str, Any]) -> None:
        if not steps or len(steps) > self.MAX_STEPS:
            raise ValueError("目标计划为空或超过步数限制")
        understanding = context.get("intent_understanding") if isinstance(context.get("intent_understanding"), dict) else {}
        semantic_action = str(understanding.get("action") or "")
        context_type = str(context.get("type") or "global")
        allowed_contexts = {
            "candidate_sourcing": {"job"},
            "new_candidate_outreach": {"job"},
            "candidate_outreach": {"candidate", "queue"},
            "candidate_review": {"job", "candidate", "queue"},
            "job_publish": {"job"},
            "recommendation": {"candidate"},
            "salary": {"candidate"},
        }
        if semantic_action in allowed_contexts and context_type not in allowed_contexts[semantic_action]:
            expected = "或".join(sorted(allowed_contexts[semantic_action]))
            raise ValueError(f"动作 {semantic_action} 执行前必须唯一定位 {expected} 对象")

        capability_ids = {str(step.get("capability_id") or "") for step in steps}
        required_capabilities = {
            "candidate_sourcing": {"multi_channel_sourcing", "candidate_batch_assessment"},
            "new_candidate_outreach": {"multi_channel_sourcing", "candidate_batch_assessment"},
            "candidate_outreach": {"outreach_prepare", "outreach_execute"},
            "job_publish": {"job_publish_prepare", "job_publish_execute"},
            "job_split": {"job_library_update"},
            "job_archive": {"job_library_update"},
            "recommendation": {"recommendation_report"},
            "salary": {"salary_verification", "salary_negotiation"},
        }.get(semantic_action, set())
        if semantic_action == "recommendation" and self._recommendation_requires_send(
            str(understanding.get("objective") or "")
        ):
            required_capabilities = required_capabilities | {"client_recommendation"}
        if semantic_action == "candidate_review":
            required_capabilities = (
                {"candidate_assessment", "verification_plan"}
                if context_type == "candidate"
                else {"candidate_batch_assessment"}
            )
        missing_capabilities = required_capabilities - capability_ids
        if missing_capabilities:
            raise ValueError(
                f"动作 {semantic_action} 的计划缺少必要能力：{'、'.join(sorted(missing_capabilities))}"
            )
        seen: set[str] = set()
        for step in steps:
            capability = self.service.skills.get(step["capability_id"])
            if capability is None:
                raise ValueError(f"计划引用未注册能力：{step['capability_id']}")
            if capability.risk_level == "R4":
                raise ValueError(f"计划包含永久禁止能力：{capability.id}")
            if context["type"] not in capability.supported_contexts and "global" not in capability.supported_contexts:
                raise ValueError(f"能力 {capability.id} 不支持 {context['type']} 上下文")
            if any(dep not in seen for dep in step["depends_on"]):
                raise ValueError(f"步骤依赖无效：{step['step_key']}")
            seen.add(step["step_key"])


    def _ground_write_steps(
        self, objective: str, selected: dict[str, Any], steps: list[dict[str, Any]], raw_context: dict[str, Any]
    ) -> None:
        raw_inputs = raw_context.get("goal_inputs") if isinstance(raw_context.get("goal_inputs"), dict) else {}
        grounding = raw_context.get("goal_grounding") if isinstance(raw_context.get("goal_grounding"), dict) else {}
        client = str(raw_inputs.get("client") or grounding.get("client") or "").strip()
        if not client and selected.get("type") == "job" and selected.get("id"):
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT c.name FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                    (int(selected["id"]),),
                ).fetchone()
                client = str(row["name"] or "") if row else ""
            finally:
                conn.close()
        if not client:
            clients = self.service._mentioned_client_names(objective)
            client = clients[0] if len(clients) == 1 else ""

        required_contexts = {
            "multi_channel_sourcing": {"job"},
            "job_publish_prepare": {"job"},
            "job_publish_execute": {"job"},
            "outreach_execute": {"candidate", "queue"},
            "client_recommendation": {"candidate"},
            "offer_confirmation": {"candidate"},
            "identity_merge_preflight": {"candidate"},
        }
        for step in steps:
            required = required_contexts.get(step["capability_id"])
            if required and selected.get("type") not in required:
                expected = "或".join(sorted(required))
                raise ValueError(f"能力 {step['capability_id']} 执行前必须唯一定位 {expected} 对象")
            if required and selected.get("type") in {"job", "candidate"} and not selected.get("id"):
                raise ValueError(f"能力 {step['capability_id']} 执行前缺少唯一对象 ID")
            if step["capability_id"] != "job_library_update":
                continue
            if not client:
                raise ValueError("创建岗位库写入工作流前必须唯一确认客户")
            step["inputs"]["client"] = client
            directions = raw_inputs.get("directions")
            if isinstance(directions, list):
                cleaned = [str(item).strip() for item in directions if str(item).strip()]
                if cleaned:
                    step["inputs"]["directions"] = list(dict.fromkeys(cleaned))[:6]
            if raw_inputs.get("archive_legacy") is not None:
                step["inputs"]["archive_legacy"] = bool(raw_inputs.get("archive_legacy"))
            archive_requested = any(token in objective for token in ("归档", "关闭", "旧岗位", "没拆分", "未拆分", "合并岗位"))
            if archive_requested and not (selected.get("type") == "job" and selected.get("id")):
                raise ValueError("归档岗位前必须唯一定位现有岗位")
            split_requested = any(token in objective for token in ("拆分", "拆成", "分成", "三个方向", "分别建岗"))
            if split_requested and not step["inputs"].get("directions"):
                raise ValueError("拆分岗位前必须明确拆分方向")


    def create_goal(
        self,
        objective: str,
        context: dict[str, Any] | None = None,
        priority: int = 2,
        *,
        plan_override: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        objective = " ".join(str(objective or "").split())
        if not objective:
            raise ValueError("目标不能为空")
        raw_context = dict(context or {})
        selected = self._resolve_context(objective, raw_context)
        grounding = raw_context.get("goal_grounding") if isinstance(raw_context.get("goal_grounding"), dict) else {}
        if grounding:
            selected["grounding"] = {
                key: value for key, value in grounding.items()
                if key in {"source", "client", "job_id", "job", "directions", "attachment_names", "validated_against_v3"}
            }
        # S4-1：Copilot L3 提问门控的顾问交互结果（放行/锚点回复）随工作流上下文
        # 传递给 search_strategy 步骤，用于 strategy_v2 的 consultant_override 与 inferred 留痕。
        clarification = raw_context.get("strategy_clarification") if isinstance(raw_context.get("strategy_clarification"), dict) else {}
        if clarification:
            selected["strategy_clarification"] = {
                key: clarification[key]
                for key in ("consultant_override", "consultant_answers", "asked_questions", "input_level", "missing_anchors", "original_objective")
                if key in clarification
            }
        understanding = raw_context.get("intent_understanding") if isinstance(raw_context.get("intent_understanding"), dict) else {}
        if understanding:
            selected["intent_understanding"] = {
                key: understanding[key]
                for key in (
                    "version", "speech_act", "action", "objective", "target", "constraints",
                    "refers_to_previous", "confidence", "source_message",
                )
                if key in understanding
            }
        turn_decision = raw_context.get("turn_decision") if isinstance(raw_context.get("turn_decision"), dict) else {}
        if turn_decision:
            selected["turn_decision"] = turn_decision
        constraint_ledger = [
            {
                "id": str(item.get("id") or "")[:40],
                "quote": str(item.get("quote") or "").strip()[:180],
                "kind": str(item.get("kind") or "other")[:32],
            }
            for item in (raw_context.get("constraint_ledger") or [])
            if isinstance(item, dict) and str(item.get("quote") or "").strip()
        ]
        if constraint_ledger:
            selected["constraint_ledger"] = constraint_ledger[-24:]
        locked_constraints = [
            str(item).strip()[:180]
            for item in (raw_context.get("locked_constraints") or [])
            if str(item).strip()
        ]
        if locked_constraints:
            selected["locked_constraints"] = list(dict.fromkeys(locked_constraints))[-12:]
        steps = [dict(step) for step in plan_override] if plan_override is not None else self._plan(objective, selected)
        for step in steps:
            step["depends_on"] = list(step.get("depends_on") or [])
            step["inputs"] = dict(step.get("inputs") or {})
        for step in steps:
            step["inputs"]["objective"] = objective
        self._ground_write_steps(objective, selected, steps, raw_context)
        self._validate_plan(steps, selected)
        goal_id = f"goal_{secrets.token_hex(6)}"
        workflow_id = f"workflow_{secrets.token_hex(6)}"
        snapshot_hash = hashlib.sha256(_dumps(selected).encode("utf-8")).hexdigest()
        conn = self._connect()
        try:
            action_key, _ = self._action_label(steps)
            round_number = self._next_round_number(conn, selected, action_key)
            title = self._format_goal_title(
                conn,
                objective,
                selected,
                steps,
                round_number=round_number,
            )
            conn.execute(
                """
                INSERT INTO agent_goals
                (goal_id,objective,title,context_type,context_id,context_json,priority,status)
                VALUES (?,?,?,?,?,?,?,'draft')
                """,
                (goal_id, objective, title, selected["type"], selected.get("id"), _dumps(selected), max(0, min(int(priority), 3))),
            )
            conn.execute(
                """
                INSERT INTO agent_workflows
                (workflow_id,goal_id,version,current_stage,status,plan_json)
                VALUES (?,?,1,?,'planned',?)
                """,
                (workflow_id, goal_id, steps[0]["business_stage"], _dumps({"objective": objective, "steps": steps})),
            )
            for sequence, step in enumerate(steps, 1):
                capability = self.service.skills.get(step["capability_id"])
                conn.execute(
                    """
                    INSERT INTO agent_workflow_steps
                    (workflow_id,step_key,sequence,capability_id,business_label,business_stage,risk_level,
                     reason,depends_on_json,input_json,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'pending')
                    """,
                    (
                        workflow_id, step["step_key"], sequence, capability.id,
                        step["business_label"], step["business_stage"], capability.risk_level,
                        step["reason"], _dumps(step["depends_on"]), _dumps(step["inputs"]),
                    ),
                )
            conn.execute(
                "INSERT INTO agent_workflow_context (workflow_id,snapshot_hash,context_json) VALUES (?,?,?)",
                (workflow_id, snapshot_hash, _dumps(selected)),
            )
            self._event(conn, workflow_id, None, "workflow_planned", "planned", f"ASA 已生成 {len(steps)} 步执行计划")
            conn.commit()
        finally:
            conn.close()
        return self.get_workflow(workflow_id)


    @staticmethod
    def _plan_identity(
        workflow_id: str,
        workflow_version: Any,
        plan: dict[str, Any],
        goal_context: dict[str, Any],
    ) -> dict[str, Any]:
        revision_number = int(goal_context.get("revision_number") or 0)
        plan_version = revision_number + 1
        payload = {
            "workflow_id": workflow_id,
            "workflow_version": int(workflow_version or 1),
            "plan_version": plan_version,
            "context": {
                "type": goal_context.get("type"),
                "id": goal_context.get("id"),
                "revision_root_workflow_id": goal_context.get("revision_root_workflow_id"),
            },
            "plan": plan,
        }
        return {
            "workflow_id": workflow_id,
            "version": plan_version,
            "plan_hash": hashlib.sha256(_dumps(payload).encode("utf-8")).hexdigest(),
        }


    def revise_workflow(
        self,
        workflow_id: str,
        instruction: str,
        *,
        effective_constraints: list[dict[str, Any]] | None = None,
        constraint_changes: list[dict[str, Any]] | None = None,
        turn_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        instruction = " ".join(str(instruction or "").split())
        if not instruction:
            raise ValueError("修改说明不能为空")
        current = self.get_workflow(workflow_id)
        if current["workflow"]["status"] not in {"planned", "queued", "paused", "waiting_approval", "blocked", "failed"}:
            raise ValueError("工作流已进入外部执行或已结束，不能直接修订策略")
        sourcing_step = next(
            (step for step in current["steps"] if step.get("capability_id") == "multi_channel_sourcing"),
            None,
        )
        if sourcing_step is None or sourcing_step.get("status") not in {"pending", "waiting_approval", "blocked", "failed"}:
            raise ValueError("外部寻访已经开始，当前策略不能原地替换")
        old_context = dict(current["goal"].get("context") or {})
        revision_root_workflow_id = str(
            old_context.get("revision_root_workflow_id")
            or old_context.get("revision_of_workflow_id")
            or workflow_id
        )
        plan_source = current
        if revision_root_workflow_id != workflow_id:
            try:
                root = self.get_workflow(revision_root_workflow_id)
                root_capabilities = [step.get("capability_id") for step in root.get("steps") or []]
                if "search_strategy" in root_capabilities and "multi_channel_sourcing" in root_capabilities:
                    plan_source = root
            except ValueError:
                pass
        source_plan = dict(plan_source["workflow"].get("plan") or {})
        source_steps = source_plan.get("steps") if isinstance(source_plan.get("steps"), list) else []
        if not source_steps:
            raise ValueError("原工作流缺少可继承的步骤计划")
        # 规划器会把“修改/更新 + 岗位”识别为岗位库写入。修订说明保留在审计上下文，
        # 进入规划器的文本改用“本轮寻访条件调整”，避免策略修订被错误路由到 job_library_update。
        planning_instruction = re.sub(r"修改|更新|修订", "调整", instruction)
        base_objective = str(plan_source["goal"].get("objective") or current["goal"]["objective"])
        for change in constraint_changes or []:
            if not isinstance(change, dict):
                continue
            previous_quote = str(change.get("previous_quote") or "").strip()
            replacement = str(change.get("quote") or "").strip() if change.get("operation") == "replace" else ""
            if previous_quote:
                base_objective = base_objective.replace(previous_quote, replacement)
        effective_quotes = [
            str(item.get("quote") or "").strip()
            for item in (effective_constraints or [])
            if isinstance(item, dict) and str(item.get("quote") or "").strip()
        ]
        target_constraint = next(
            (
                str(item.get("quote") or "")
                for item in (effective_constraints or [])
                if isinstance(item, dict) and item.get("kind") == "target_count"
            ),
            "",
        )
        target_match = re.search(r"(\d+)\s*(?:位|个|名|人)", target_constraint)
        if target_match and re.search(r"\d+\s*(?:位|个|名|人)", base_objective):
            target_count = target_match.group(1)
            base_objective = re.sub(
                r"\d+\s*(位|个|名|人)",
                lambda match: f"{target_count}{match.group(1)}",
                base_objective,
                count=1,
            )
        objective = f"{base_objective}；本轮寻访条件调整：{planning_instruction}"
        if effective_quotes:
            objective += f"；本轮有效约束：{'；'.join(dict.fromkeys(effective_quotes))}"
        inherited_steps: list[dict[str, Any]] = []
        for source_step in source_steps:
            step = dict(source_step)
            step["depends_on"] = list(source_step.get("depends_on") or [])
            step["inputs"] = dict(source_step.get("inputs") or {})
            step["inputs"]["objective"] = objective
            inherited_steps.append(step)
        self._validate_plan(inherited_steps, current["goal"]["context"])
        revised = self.create_goal(
            objective,
            current["goal"]["context"],
            plan_override=inherited_steps,
        )
        revised_workflow_id = str(revised["workflow"]["workflow_id"])
        revised_goal_id = str(revised["goal"]["goal_id"])
        revision_number = int(old_context.get("revision_number") or 0) + 1
        revised_context = dict(revised["goal"].get("context") or {})
        revised_context.update({
            "revision_of_workflow_id": workflow_id,
            "revision_root_workflow_id": revision_root_workflow_id,
            "revision_number": revision_number,
            "revision_instruction": instruction,
        })
        if effective_constraints is not None:
            revised_context["locked_constraints"] = effective_quotes[-24:]
            revised_context["constraint_ledger"] = [
                {
                    "id": str(item.get("id") or ""),
                    "quote": str(item.get("quote") or ""),
                    "kind": str(item.get("kind") or "other"),
                }
                for item in effective_constraints
                if isinstance(item, dict) and str(item.get("quote") or "").strip()
            ][-24:]
        if turn_decision:
            revised_context["turn_decision"] = turn_decision
        base_title = re.sub(
            r"\s*·\s*修订\d+$",
            "",
            str(plan_source["goal"].get("title") or current["goal"].get("title") or current["goal"]["objective"]),
        )
        revised_title = f"{base_title} · 修订{revision_number}"[:80]
        conn = self._connect()
        try:
            latest = conn.execute(
                "SELECT status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            latest_step = conn.execute(
                "SELECT status FROM agent_workflow_steps WHERE workflow_id=? AND capability_id='multi_channel_sourcing' ORDER BY sequence LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if (
                latest is None
                or latest["status"] not in {"planned", "queued", "paused", "waiting_approval", "blocked", "failed"}
                or latest_step is None
                or latest_step["status"] not in {"pending", "waiting_approval", "blocked", "failed"}
            ):
                conn.close()
                self.cancel_workflow(revised_workflow_id, "原工作流状态已变化，取消本次策略修订")
                raise ValueError("原工作流状态已变化，请刷新后重新修订")
            # 撤回快照：记录 supersede 前的源工作流状态，供 revert_workflow_revision 单事务恢复
            source_goal = conn.execute(
                "SELECT status FROM agent_goals WHERE goal_id=?", (current["goal"]["goal_id"],)
            ).fetchone()
            undo_snapshot = {
                "source_workflow_id": workflow_id,
                "source_status": str(latest["status"]),
                "source_goal_status": str(source_goal["status"]) if source_goal else "",
                "steps": {
                    str(row["id"]): str(row["status"])
                    for row in conn.execute(
                        "SELECT id,status FROM agent_workflow_steps WHERE workflow_id=? AND status IN ('pending','waiting_approval','approved','blocked','failed')",
                        (workflow_id,),
                    ).fetchall()
                },
                "pending_approvals": [
                    str(row["approval_id"])
                    for row in conn.execute(
                        "SELECT approval_id FROM agent_approvals WHERE workflow_id=? AND status='pending'",
                        (workflow_id,),
                    ).fetchall()
                ],
            }
            conn.execute(
                "UPDATE agent_approvals SET status='superseded',decision_note='策略已修订，旧审批失效',decided_at=datetime('now','localtime') WHERE workflow_id=? AND status='pending'",
                (workflow_id,),
            )
            conn.execute(
                "UPDATE agent_workflow_steps SET status='cancelled',error='策略已修订，旧步骤失效',updated_at=datetime('now','localtime') WHERE workflow_id=? AND status IN ('pending','waiting_approval','approved','blocked','failed')",
                (workflow_id,),
            )
            conn.execute("UPDATE agent_workflows SET status='superseded',updated_at=datetime('now','localtime') WHERE workflow_id=?", (workflow_id,))
            conn.execute(
                "UPDATE agent_goals SET status='superseded',result_summary=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                (f"已由 {revised_workflow_id} 的策略修订版替代", current["goal"]["goal_id"]),
            )
            conn.execute(
                "UPDATE agent_goals SET title=?,context_json=?,updated_at=datetime('now','localtime') WHERE goal_id=?",
                (revised_title, _dumps(revised_context), revised_goal_id),
            )
            conn.execute("DELETE FROM agent_workflow_steps WHERE workflow_id=?", (revised_workflow_id,))
            conn.execute(
                "UPDATE agent_workflows SET current_stage=?,active_step_id=NULL,status='planned',plan_json=?,updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (
                    inherited_steps[0]["business_stage"],
                    _dumps({"objective": objective, "steps": inherited_steps}),
                    revised_workflow_id,
                ),
            )
            for sequence, step in enumerate(inherited_steps, 1):
                capability = self.service.skills.get(str(step.get("capability_id") or ""))
                if capability is None:
                    raise ValueError(f"原工作流包含未注册能力：{step.get('capability_id')}")
                conn.execute(
                    """
                    INSERT INTO agent_workflow_steps
                    (workflow_id,step_key,sequence,capability_id,business_label,business_stage,risk_level,
                     reason,depends_on_json,input_json,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'pending')
                    """,
                    (
                        revised_workflow_id, step["step_key"], sequence, capability.id,
                        step["business_label"], step["business_stage"], capability.risk_level,
                        step["reason"], _dumps(step["depends_on"]), _dumps(step["inputs"]),
                    ),
                )
            conn.execute(
                "UPDATE agent_workflow_context SET context_json=? WHERE workflow_id=?",
                (_dumps(revised_context), revised_workflow_id),
            )
            conn.execute(
                "UPDATE agent_step_events SET summary=? WHERE workflow_id=? AND event_type='workflow_planned'",
                (f"ASA 已继承原工作流的 {len(inherited_steps)} 步计划并进入策略修订", revised_workflow_id),
            )
            self._event(
                conn, workflow_id, None, "workflow_superseded", "superseded",
                f"策略修订后由 {revised_workflow_id} 替代",
                {"revised_workflow_id": revised_workflow_id, "instruction": instruction},
            )
            self._event(
                conn, revised_workflow_id, None, "workflow_revised", "planned",
                f"由 {workflow_id} 生成策略修订版",
                {"source_workflow_id": workflow_id, "revision_number": revision_number, "undo": undo_snapshot},
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return self.get_workflow(revised_workflow_id)


    def revert_workflow_revision(self, revised_workflow_id: str) -> dict[str, Any]:
        """撤销一次策略修订：恢复被 supersede 的源工作流，取消修订版。

        守卫（任一不满足抛 ValueError，API 409）：
        - 修订版仍 planned 且全部步骤 pending（没人启动/改过，对应 PRD「手动修改后撤回失效」）
        - 修订版 context 携带 revision_of_workflow_id
        - 源工作流仍处于 superseded（防重复撤回）
        - 最新 workflow_revised 事件带 undo 快照
        """
        conn = self._connect()
        try:
            revised = conn.execute(
                "SELECT w.status,w.goal_id,g.context_json FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id WHERE w.workflow_id=?",
                (revised_workflow_id,),
            ).fetchone()
            if revised is None:
                raise ValueError("工作流不存在")
            context = _loads(revised["context_json"], {}) or {}
            source_workflow_id = str(context.get("revision_of_workflow_id") or "").strip()
            if not source_workflow_id:
                raise ValueError("该工作流不是策略修订版，不能撤回")
            if revised["status"] != "planned":
                raise ValueError("修订版已开始执行，不能撤回")
            active_steps = conn.execute(
                "SELECT COUNT(*) FROM agent_workflow_steps WHERE workflow_id=? AND status!='pending'",
                (revised_workflow_id,),
            ).fetchone()[0]
            if active_steps:
                raise ValueError("修订版已有步骤被处理，不能撤回")
            source = conn.execute(
                "SELECT status,goal_id FROM agent_workflows WHERE workflow_id=?",
                (source_workflow_id,),
            ).fetchone()
            if source is None:
                raise ValueError("源工作流不存在")
            if source["status"] != "superseded":
                raise ValueError("源工作流已恢复，本次修订无需撤回")
            event = conn.execute(
                """
                SELECT detail_json FROM agent_step_events
                WHERE workflow_id=? AND event_type='workflow_revised'
                ORDER BY id DESC LIMIT 1
                """,
                (revised_workflow_id,),
            ).fetchone()
            undo = (_loads(event["detail_json"], {}) if event else {}).get("undo") or {}
            if str(undo.get("source_workflow_id") or "") != source_workflow_id or not undo.get("source_status"):
                raise ValueError("缺少撤回快照，不能安全撤回")
            # 恢复源工作流 / 目标 / 步骤 / 审批
            conn.execute(
                "UPDATE agent_workflows SET status=?,updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (undo["source_status"], source_workflow_id),
            )
            if undo.get("source_goal_status"):
                conn.execute(
                    "UPDATE agent_goals SET status=?,result_summary=NULL,updated_at=datetime('now','localtime') WHERE goal_id=?",
                    (undo["source_goal_status"], source["goal_id"]),
                )
            for step_id, status in (undo.get("steps") or {}).items():
                conn.execute(
                    "UPDATE agent_workflow_steps SET status=?,error=NULL,updated_at=datetime('now','localtime') WHERE id=? AND status='cancelled' AND error='策略已修订，旧步骤失效'",
                    (status, int(step_id)),
                )
            for approval_id in undo.get("pending_approvals") or []:
                conn.execute(
                    "UPDATE agent_approvals SET status='pending',decision_note=NULL,decided_at=NULL WHERE approval_id=? AND status='superseded'",
                    (approval_id,),
                )
            # 取消修订版（与 cancel_workflow 同语义，同事务内联）
            conn.execute(
                "UPDATE agent_workflows SET status='cancelled',finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE workflow_id=?",
                (revised_workflow_id,),
            )
            conn.execute(
                "UPDATE agent_goals SET status='cancelled',result_summary='策略修订已撤回',finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE goal_id=?",
                (revised["goal_id"],),
            )
            conn.execute(
                "UPDATE agent_workflow_steps SET status='cancelled',updated_at=datetime('now','localtime') WHERE workflow_id=? AND status IN ('pending','waiting_approval','approved')",
                (revised_workflow_id,),
            )
            conn.execute(
                "UPDATE agent_approvals SET status='cancelled',decided_at=datetime('now','localtime') WHERE workflow_id=? AND status='pending'",
                (revised_workflow_id,),
            )
            self._event(
                conn, source_workflow_id, None, "workflow_revision_reverted", str(undo["source_status"]),
                f"策略修订 {revised_workflow_id} 已撤回，恢复为最新版本",
                {"revised_workflow_id": revised_workflow_id},
            )
            self._event(
                conn, revised_workflow_id, None, "workflow_revision_reverted", "cancelled",
                f"修订已撤回，{source_workflow_id} 恢复为最新版本",
                {"source_workflow_id": source_workflow_id},
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return self.get_workflow(source_workflow_id)

