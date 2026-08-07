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


class WorkflowEngine:
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

    def __init__(self, service: Any) -> None:
        self.service = service
        self._recover_interrupted()
        self._refresh_goal_titles()

    def _connect(self):
        return self.service._connect()

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
                add("candidate_batch_assessment", "生成逐人核验点", "assessment", "基于现有 v3 简历和评估结果形成可推进、待核验和停止建议", ["talent_pool_search"])
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

    def _event(self, conn, workflow_id: str, step_id: int | None, event_type: str, status: str, summary: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            """
            INSERT INTO agent_step_events (workflow_id,step_id,event_type,status,summary,detail_json)
            VALUES (?,?,?,?,?,?)
            """,
            (workflow_id, step_id, event_type, status, summary, _dumps(detail or {})),
        )

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
        _, metadata = self._latest_artifact_payload(conn, workflow_id, "search_strategy")
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
        return {**snapshot, "strategy_hash": strategy_hash, "ready": ready}

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
        return {}

    def _create_approval(self, conn, goal_id: str, workflow_id: str, step: Any) -> None:
        existing = conn.execute(
            "SELECT approval_id FROM agent_approvals WHERE step_id=? AND status='pending'", (step["id"],)
        ).fetchone()
        if existing:
            return
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
        exact_action.update(self._approval_preflight_details(conn, workflow_id, step))
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
            self._create_approval(conn, step["goal_id"], workflow_id, step)
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
                    self._create_approval(conn, workflow["goal_id"], workflow_id, pending)
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
            if approval["expires_at"] and str(approval["expires_at"]) < datetime.now().strftime("%Y-%m-%d %H:%M:%S"):
                conn.execute(
                    "UPDATE agent_approvals SET status=?,decided_at=datetime('now','localtime'),decision_note='点击时已过期，自动换新' WHERE id=?",
                    (f"expired_{approval['approval_id']}", approval["id"]),
                )
                step = conn.execute("SELECT s.*,w.goal_id FROM agent_workflow_steps s JOIN agent_workflows w ON w.workflow_id=s.workflow_id WHERE s.id=?", (approval["step_id"],)).fetchone()
                if step and step["status"] == "waiting_approval":
                    self._create_approval(conn, step["goal_id"], approval["workflow_id"], step)
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
                WHERE jc.job_id=? AND (a.id IS NOT NULL OR sa.id IS NOT NULL)
            """
            total = int(conn.execute(f"SELECT COUNT(*) {base}", (job_id,)).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT jc.id,p.id AS person_id,p.display_name,p.current_company,p.current_title,
                       jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                       a.fit_score,a.fit_level,a.recommendation,
                       sa.channel AS attribution_channel,sa.source_query AS attribution_query,
                       sa.source_round AS attribution_round,sa.workflow_id AS attribution_workflow_id
                {base}
                ORDER BY (a.fit_score IS NULL),a.fit_score DESC,jc.id DESC LIMIT ? OFFSET ?
                """,
                (job_id, limit, offset),
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
                    }
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
                    "updated_at": item["updated_at"],
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
