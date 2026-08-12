"""Handler extracted from service.py — Strategy v2, mapping, proposals, learning, radar.

All functions receive 'self' (AgentService instance) as first parameter.
"""

from __future__ import annotations
import hashlib, json, os, secrets, sqlite3, time
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ._shared import (
    _dumps, _loads, _row, _table_exists, _table_columns,
)
from .context import build_candidate_context
from .policy import action_decision, is_stopped
from .schema import ensure_schema

def get_strategy_review(self, workflow_id: str) -> dict[str, Any]:
    """S4-3：读取工作流最新策略复盘；工作流不存在/无复盘均抛 LookupError（API 404）。"""
    from . import strategy_review

    conn = self._connect()
    try:
        exists = conn.execute("SELECT 1 FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
        if exists is None:
            raise LookupError(f"工作流不存在：{workflow_id}")
        payload = strategy_review.get_strategy_review(conn, workflow_id)
        if payload is None:
            raise LookupError(f"该工作流还没生成原因分析：{workflow_id}")
        return {"ok": True, **payload}
    finally:
        conn.close()


def rebuild_strategy_review(self, workflow_id: str) -> dict[str, Any]:
    """S4-3：按需重算复盘（存量终局工作流补生成；幂等覆盖，version 自增 + history）。

    工作流不存在抛 LookupError（404）；非终局（completed/blocked/failed）抛 ValueError（409）。
    """
    from . import strategy_review

    conn = self._connect()
    try:
        artifact_id, review = strategy_review.rebuild_for_workflow(conn, workflow_id)
        previous = conn.execute(
            """
            SELECT id,detail_json
            FROM agent_step_events
            WHERE workflow_id=?
              AND event_type IN ('strategy_review_generated','strategy_review_rebuilt')
            ORDER BY id DESC LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
        if previous is not None:
            previous_detail = _loads(previous["detail_json"], {})
            conn.execute(
                """
                UPDATE agent_step_events
                   SET event_type='strategy_review_superseded',
                       status='resolved',
                       summary=?,
                       detail_json=?
                 WHERE id=?
                """,
                (
                    f"历史策略复盘已被第 {review.get('version', 1)} 版重算替代。",
                    _dumps({
                        **previous_detail,
                        "superseded_by": artifact_id,
                        "superseded_by_version": review.get("version"),
                    }),
                    int(previous["id"]),
                ),
            )
        workflow = conn.execute(
            "SELECT status FROM agent_workflows WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        self.workflow_engine._event(
            conn,
            workflow_id,
            None,
            "strategy_review_rebuilt",
            str(workflow["status"] if workflow is not None else "completed"),
            f"策略复盘已重算：{review.get('verdict_label')}（{artifact_id}）",
            {
                "artifact_id": artifact_id,
                "verdict": review.get("verdict"),
                "version": review.get("version"),
            },
        )
        conn.commit()
        return {"ok": True, "workflow_id": workflow_id, "artifact_id": artifact_id, "review": review}
    finally:
        conn.close()


def apply_strategy_review_diff_decisions(self, workflow_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """S4-3c：顾问逐项采纳/拒绝复盘 diff——status 落 artifact（upsert 可重复覆盖），
    同一写动作追加 strategy_v2.consultant_edits 并写 explicit_corrections 学习信号。

    工作流不存在/无复盘抛 LookupError（404）；diff_id 未知或 status 非法抛 ValueError（409）。
    """
    from . import strategy_review

    conn = self._connect()
    try:
        payload = strategy_review.apply_diff_decisions(conn, workflow_id, decisions)
        conn.commit()
        return payload
    finally:
        conn.close()


def create_mapping_task(self, job_id: int, *, trigger: str = "manual", collector: Any = None) -> dict[str, Any]:
    """S5-1：发起 Mapping 直挖——目标团队定位 + 名单生成，落 mapping_task artifact 并写 job 时间线。

    岗位不存在抛 LookupError（404）；岗位无 strategy_v2 策略或 trigger 非法抛 ValueError（409）。
    红线：不自动触达；无来源人名拒写；禁挖名单照常过滤；restricted 仅白名单出库。
    collector 可注入（测试用本地 fixture，绝不打外网；缺省为标准库只读采集器）。
    """
    from . import knowledge_base, mapping_task, strategy_v2

    conn = self._connect()
    try:
        job = conn.execute(
            "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise LookupError(f"岗位不存在：{job_id}")
        if trigger not in mapping_task.TRIGGERS:
            raise ValueError(f"trigger 必须是 {'/'.join(mapping_task.TRIGGERS)}")
        strategy = conn.execute(
            """
            SELECT w.workflow_id,g.goal_id,a.artifact_id,a.metadata_json
            FROM agent_workflows w
            JOIN agent_goals g ON g.goal_id=w.goal_id
            JOIN agent_artifacts a ON a.workflow_id=w.workflow_id AND a.artifact_type='search_strategy'
            WHERE g.context_type='job' AND g.context_id=?
            ORDER BY a.id DESC LIMIT 1
            """,
            (int(job_id),),
        ).fetchone()
        if strategy is None:
            raise ValueError(f"岗位 {job_id} 还没有寻访策略（strategy_v2），无法发起 Mapping 直挖")
        strategy_doc = strategy_v2.extract_strategy_v2(strategy["metadata_json"])
        if strategy_doc is None:
            raise ValueError(f"岗位 {job_id} 的策略 artifact 不是 strategy_v2 格式，无法发起 Mapping 直挖")

        archetype = None
        archetypes, _load_trace = strategy_v2.load_job_archetypes()
        archetype_id = str(strategy_doc.get("archetype_id") or "")
        for item in archetypes:
            if str(item.get("archetype_id") or "") == archetype_id:
                archetype = item
                break
        graph, _graph_trace = knowledge_base.load_company_graph()

        doc = mapping_task.build_mapping_task(
            job_id=int(job_id),
            trigger=trigger,
            strategy_ref=str(strategy["artifact_id"]),
            strategy_doc=strategy_doc,
            client=str(job["client"] or ""),
            job_title=str(job["title"] or ""),
            graph=graph,
            archetype=archetype,
            collector=collector,
        )
        doc["workflow_id"] = str(strategy["workflow_id"])
        doc["goal_id"] = str(strategy["goal_id"])
        artifact_id = mapping_task.upsert_mapping_task(conn, doc)
        stats = doc.get("stats") or {}
        summary = (
            f"发起 Mapping 直挖：目标团队 {stats.get('teams', 0)} 个、候选目标人 {stats.get('candidates', 0)} 位"
            f"（禁挖过滤 {stats.get('banned_filtered', 0)}、无来源拒收 {stats.get('rejected_no_source', 0)}、"
            f"采集失败 {stats.get('failures_count', 0)} 次已留痕）。名单仅供顾问本人决策，系统不自动触达。"
        )
        conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (NULL,NULL,?,'mapping_task_created','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
            """,
            (
                int(job_id),
                summary,
                json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "workflow_id": doc["workflow_id"],
                        "trigger": trigger,
                        "teams": stats.get("teams", 0),
                        "candidates": stats.get("candidates", 0),
                    },
                    ensure_ascii=False,
                ),
                artifact_id,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "job_id": int(job_id),
            "workflow_id": doc["workflow_id"],
            "artifact_id": artifact_id,
            "mapping_task": doc,
        }
    finally:
        conn.close()


def get_mapping_task(self, job_id: int, artifact_id: str) -> dict[str, Any]:
    """S5-1：读取岗位的 mapping_task；岗位/artifact 不存在或不属于该岗位抛 LookupError（404）。"""
    from . import mapping_task

    conn = self._connect()
    try:
        job = conn.execute("SELECT id FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        if job is None:
            raise LookupError(f"岗位不存在：{job_id}")
        payload = mapping_task.get_mapping_task(conn, artifact_id)
        if payload is None:
            raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
        owner = conn.execute(
            """
            SELECT 1 FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE w.workflow_id=? AND g.context_type='job' AND g.context_id=?
            """,
            (payload["workflow_id"], int(job_id)),
        ).fetchone()
        if owner is None:
            raise LookupError(f"Mapping 任务卡不属于岗位 {job_id}：{artifact_id}")
        return {"ok": True, "job_id": int(job_id), **payload}
    finally:
        conn.close()


def update_mapping_candidate(
    self,
    artifact_id: str,
    index: int,
    *,
    status: str | None = None,
    consultant_note: str | None = None,
) -> dict[str, Any]:
    """S5-2：任务卡候选状态机 PATCH（状态/备注）。confirmed 迁移自动生成破冰素材。

    artifact/index 不存在抛 LookupError（404）；未知态/非法迁移/终态变更/直接置 intaken
    抛 ValueError（409）。artifact version 不 bump，content 同步重生成。
    """
    from . import mapping_task

    conn = self._connect()
    try:
        result = mapping_task.apply_candidate_update(
            conn, artifact_id, int(index), status=status, consultant_note=consultant_note
        )
        conn.commit()
        return result
    finally:
        conn.close()


def regenerate_mapping_icebreaker(self, artifact_id: str, index: int) -> dict[str, Any]:
    """S5-2：重新生成破冰素材（仅已确认及之后状态）。质量不合格抛 ValueError（409）不写入。"""
    from . import mapping_task

    conn = self._connect()
    try:
        result = mapping_task.regenerate_icebreaker(conn, artifact_id, int(index))
        conn.commit()
        return result
    finally:
        conn.close()


def intake_mapping_candidate(self, artifact_id: str, index: int) -> dict[str, Any]:
    """S5-2：Mapping 候选入库（仅 confirmed）。复用现有 intake 写入口径，同一事务；
    不写第二条 job_candidates；禁挖/无来源/已停止关系抛 ValueError（409）。
    S8：入库成功后异步刷新该岗位画像（只抽新增人，失败不阻断入库回执）。
    """
    from . import mapping_task

    conn = self._connect()
    try:
        result = mapping_task.intake_candidate(conn, artifact_id, int(index))
        conn.commit()
    finally:
        conn.close()
    job_candidate_id = int(result.get("job_candidate_id") or 0)
    if job_candidate_id:
        result["job_profile_refresh"] = self.submit_job_profile_refresh(
            job_candidate_id, trigger="mapping_intake"
        )
    return result


def submit_job_profile_refresh(
    self, job_candidate_id: int, *, trigger: str = "manual", wait: bool = False
) -> dict[str, Any]:
    """S8 岗位画像增量刷新：人选入库/履历更新后，只抽取该人职责事实（LLM 一调）+
    确定性重算该岗画像（不重算整岗抽取）。后台线程执行；LLM 不可用或语料不足 →
    记 error/skipped，绝不阻断主流程（入库/简历捕获照常返回）。"""
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT job_id FROM job_candidates WHERE id=?", (int(job_candidate_id),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "scheduled": False, "reason": f"人岗关系不存在：{job_candidate_id}"}
    job_id = int(row["job_id"] or 0)

    def _run() -> dict[str, Any]:
        from . import job_profile_insights

        conn = self._connect()
        try:
            ensure_schema(conn)
            extraction = job_profile_insights.extract_duty_facts_for_candidate(
                conn, candidate_id=int(job_candidate_id), llm=self.llm
            )
            insight = job_profile_insights.aggregate_job_profile(
                conn, job_id=int(extraction["job_id"]), persist=True
            )
            conn.commit()
            return {
                "ok": True,
                "fact_count": extraction.get("fact_count", 0),
                "status": insight.get("status"),
                "source_count": insight.get("source_count"),
            }
        except Exception as exc:  # 画像学习失败绝不阻断主流程
            conn.rollback()
            return {"ok": False, "error": str(exc)[:200]}
        finally:
            conn.close()

    if wait:
        return {"scheduled": True, "trigger": trigger, "job_id": job_id, "result": _run()}
    self.executor.submit(_run)
    return {"ok": True, "scheduled": True, "trigger": trigger, "job_id": job_id}


def backflow_mapping_task(self, artifact_id: str, *, kb_dir: str | None = None, as_of: str = "") -> dict[str, Any]:
    """S5-3：知识回流——把任务卡已确认团队数据写入公司图谱 teams 扩展层（知识库维护流程）。

    只在显式触发时执行（运行时 Core 不自动写图谱）；图谱文件原子重写，
    除 teams/teams_external 相关键外原文件逐字节保留；同 artifact 幂等（更新 as_of 不重复条目）。
    artifact 不存在抛 LookupError（404）；无已确认团队/全部禁挖/图谱缺失或结构异常抛 ValueError（409）。
    """
    from . import graph_teams_backflow, knowledge_base, mapping_task
    from .strategy_v2 import knowledge_base_dir

    conn = self._connect()
    try:
        payload = mapping_task.get_mapping_task(conn, artifact_id)
        if payload is None:
            raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
        doc = payload["mapping_task"]
        client = str(doc.get("client") or "")
        restricted, _trace = knowledge_base.load_restricted_constraints(client, kb_dir=kb_dir)
        constraints = (restricted or {}).get("constraints") if isinstance(restricted, dict) else {}
        banned = [
            str(item) for item in (constraints or {}).get("banned_companies") or [] if str(item or "").strip()
        ]
        graph_path = (Path(kb_dir) if kb_dir else knowledge_base_dir()) / knowledge_base.COMPANY_GRAPH_FILE
        summary = graph_teams_backflow.backflow_teams(
            graph_path, doc, artifact_id=str(artifact_id), as_of=as_of, banned=banned
        )
        job_id = int(doc.get("job_id") or 0)
        if job_id:
            conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                VALUES (NULL,NULL,?,'mapping_task_backflow','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
                """,
                (
                    job_id,
                    f"Mapping 团队数据回流图谱：写入公司 {summary['companies_written']} 家、"
                    f"团队 {summary['teams_written']} 个（as_of {summary['as_of']}；"
                    f"禁挖跳过 {summary['skipped_banned']}，teams_external {summary['external_companies_written']} 家）。",
                    json.dumps(
                        {
                            "artifact_id": str(artifact_id),
                            "as_of": summary["as_of"],
                            "companies_written": summary["companies_written"],
                            "teams_written": summary["teams_written"],
                            "skipped_banned": summary["skipped_banned"],
                            "changed": summary["changed"],
                        },
                        ensure_ascii=False,
                    ),
                    str(artifact_id),
                ),
            )
            conn.commit()
        return {"ok": True, **summary}
    finally:
        conn.close()


def mapping_metrics(self) -> dict[str, Any]:
    """S5-3：Mapping 评测指标聚合（PRD §8 四项口径，只读；数据不足的分组如实 null）。"""
    from . import mapping_metrics

    conn = self._connect()
    try:
        metrics = mapping_metrics.compute_mapping_metrics(conn)
        return {
            "ok": True,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "metrics": metrics,
        }
    finally:
        conn.close()


def _proposal_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    proposal = {
        "id": item.get("id"),
        "proposal_id": item.get("proposal_id"),
        "job_candidate_id": item.get("job_candidate_id"),
        "assessment_id": item.get("assessment_id"),
        "candidate": item.get("candidate") or "",
        "company": item.get("company") or "",
        "title": item.get("candidate_title") or "",
        "client": item.get("client") or "",
        "job": item.get("job") or "",
        "action_type": item.get("action_type"),
        "risk_level": item.get("risk_level"),
        "title_text": item.get("title"),
        "rationale": item.get("rationale") or "",
        "request": _loads(item.get("request_json"), {}),
        "preflight": _loads(item.get("preflight_json"), {}),
        "status": item.get("status"),
        "reviewed_at": item.get("reviewed_at"),
        "review_note": item.get("review_note") or "",
        "expires_at": item.get("expires_at"),
        "created_at": item.get("created_at"),
    }
    # Stable protocol consumed by the native Copilot.  The proposal remains the
    # source of truth: this card only describes the allowed confirmation path.
    proposal["action_card"] = {
        "proposal_id": proposal["proposal_id"],
        "capability_id": "verification_plan" if proposal["action_type"] == "create_task" else f"proposal.{proposal['action_type']}",
        "action_kind": "internal_write" if proposal["action_type"] == "create_task" else "external_write",
        "risk_level": proposal["risk_level"],
        "context": {
            "type": "candidate",
            "id": proposal["job_candidate_id"],
            "candidate": proposal["candidate"],
            "client": proposal["client"],
            "job": proposal["job"],
        },
        "evidence": [
            {"label": "建议原因", "value": proposal["rationale"]},
            {"label": "动作", "value": proposal["title_text"]},
        ],
        "blocked_reasons": [],
        "next_actions": [
            {"type": "preflight", "label": "查看预检"},
            {"type": "decision", "decision": "approve", "label": "确认执行"},
            {"type": "decision", "decision": "reject", "label": "不执行"},
            {"type": "open_candidate", "id": proposal["job_candidate_id"], "label": "打开人选"},
        ],
        "post_check": "agent_action",
    }
    return proposal


def list_proposals(self, status: str = "pending", limit: int = 20) -> dict[str, Any]:
    status = str(status or "pending").strip().lower()
    allowed = {"pending", "approved", "rejected", "executed", "failed", "all"}
    if status not in allowed:
        raise ValueError("未知提案状态")
    limit = max(1, min(int(limit or 20), 100))
    where = "" if status == "all" else "WHERE p.status=?"
    params: tuple[Any, ...] = (limit,) if status == "all" else (status, limit)
    conn = self._connect()
    try:
        rows = conn.execute(
            f"""
            SELECT p.*,pe.display_name AS candidate,pe.current_company AS company,
                   pe.current_title AS candidate_title,jc.raw_client AS client,jc.raw_position AS job
            FROM agent_action_proposals p
            JOIN job_candidates jc ON jc.id=p.job_candidate_id
            JOIN people pe ON pe.id=jc.person_id
            {where}
            ORDER BY CASE p.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                     p.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {"ok": True, "status": status, "proposals": [self._proposal_payload(row) for row in rows]}
    finally:
        conn.close()


def generate_proposals(
    self,
    job_candidate_ids: list[int] | None = None,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 12), 50))
    ids: list[int] = []
    for value in job_candidate_ids or []:
        candidate_id = int(value)
        if candidate_id > 0 and candidate_id not in ids:
            ids.append(candidate_id)
    conn = self._connect()
    try:
        if ids:
            placeholders = ",".join("?" for _ in ids[:limit])
            rows = conn.execute(
                f"""
                SELECT * FROM agent_candidate_assessments
                WHERE is_current=1 AND job_candidate_id IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(ids[:limit]),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM agent_candidate_assessments
                WHERE is_current=1 AND recommendation IN ('priority_review','verify_first')
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in rows:
            job_candidate_id = int(row["job_candidate_id"])
            context = build_candidate_context(self.db_path, job_candidate_id)
            current_snapshot_hash = self._snapshot_key(context)
            if is_stopped(context):
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前关系已人工停止"})
                continue
            if row["snapshot_hash"] != current_snapshot_hash:
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "判断已过期"})
                continue
            assessment = self._assessment_payload(row)
            if assessment["recommendation"] not in {"priority_review", "verify_first"}:
                skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前判断不生成任务建议"})
                continue
            reason = assessment.get("next_action") or "人工核验 Agent 判断中的关键证据"
            request = {
                "job_candidate_id": job_candidate_id,
                "task_type": "agent_verification",
                "reason": reason,
                "due_at": "",
                "priority": 1 if assessment["recommendation"] == "priority_review" else 2,
                "write": True,
            }
            dedupe_key = hashlib.sha256(
                f"proposal|{row['id']}|create_task|{_dumps(request)}".encode("utf-8")
            ).hexdigest()
            proposal_id = f"proposal_{row['id']}_{dedupe_key[:10]}"
            decision = action_decision("create_task", context)
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_action_proposals
                (proposal_id,job_candidate_id,assessment_id,snapshot_hash,dedupe_key,
                 action_type,risk_level,title,rationale,request_json,status,expires_at)
                VALUES (?,?,?,?,?,'create_task',?,?,?,?,'pending',datetime('now','+7 days','localtime'))
                """,
                (
                    proposal_id,
                    job_candidate_id,
                    row["id"],
                    current_snapshot_hash,
                    dedupe_key,
                    decision["risk_level"],
                    "创建核验任务",
                    reason,
                    _dumps(request),
                ),
            )
            conn.execute(
                """
                UPDATE agent_action_proposals
                SET status='pending',review_note='',reviewed_at=NULL,action_id=NULL,
                    updated_at=datetime('now','localtime')
                WHERE dedupe_key=? AND status='failed' AND snapshot_hash=?
                """,
                (dedupe_key, current_snapshot_hash),
            )
            proposal = conn.execute(
                """
                SELECT p.*,pe.display_name AS candidate,pe.current_company AS company,
                       pe.current_title AS candidate_title,jc.raw_client AS client,jc.raw_position AS job
                FROM agent_action_proposals p
                JOIN job_candidates jc ON jc.id=p.job_candidate_id
                JOIN people pe ON pe.id=jc.person_id
                WHERE p.dedupe_key=?
                """,
                (dedupe_key,),
            ).fetchone()
            if proposal:
                created.append(self._proposal_payload(proposal))
        conn.commit()
        return {"ok": True, "proposals": created, "skipped": skipped}
    finally:
        conn.close()


# ------------------------------------------------------------------
# 触达队列 —— 候选池分级名单 → P0/P1/P2 触达提案（create_task / outreach_priority）
# ------------------------------------------------------------------

# P0 优先推进 / P1 本周推进 / P2 备选 → followup_tasks.priority 数字
OUTREACH_QUEUE_PRIORITIES = {
    "P0": (1, "优先推进"),
    "P1": (2, "本周推进"),
    "P2": (3, "备选"),
}


def _outreach_proposal_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    proposal = {
        "id": item.get("id"),
        "proposal_id": item.get("proposal_id"),
        "job_candidate_id": item.get("job_candidate_id"),
        "assessment_id": item.get("assessment_id"),
        "candidate": item.get("candidate") or "",
        "company": item.get("company") or "",
        "title": item.get("candidate_title") or "",
        "client": item.get("client") or "",
        "job": item.get("job") or "",
        "action_type": item.get("action_type"),
        "risk_level": item.get("risk_level"),
        "title_text": item.get("title"),
        "rationale": item.get("rationale") or "",
        "request": _loads(item.get("request_json"), {}),
        "preflight": _loads(item.get("preflight_json"), {}),
        "status": item.get("status"),
        "reviewed_at": item.get("reviewed_at"),
        "review_note": item.get("review_note") or "",
        "expires_at": item.get("expires_at"),
        "created_at": item.get("created_at"),
    }
    proposal["action_card"] = {
        "proposal_id": proposal["proposal_id"],
        "capability_id": "outreach_priority",
        "action_kind": "internal_write" if proposal["action_type"] == "create_task" else "external_write",
        "risk_level": proposal["risk_level"],
        "context": {
            "type": "candidate",
            "id": proposal["job_candidate_id"],
            "candidate": proposal["candidate"],
            "client": proposal["client"],
            "job": proposal["job"],
        },
        "evidence": [
            {"label": "建议原因", "value": proposal["rationale"]},
            {"label": "动作", "value": proposal["title_text"]},
        ],
        "blocked_reasons": [],
        "next_actions": [
            {"type": "preflight", "label": "查看预检"},
            {"type": "decision", "decision": "approve", "label": "确认执行"},
            {"type": "decision", "decision": "reject", "label": "不执行"},
            {"type": "open_candidate", "id": proposal["job_candidate_id"], "label": "打开人选"},
        ],
        "post_check": "agent_action",
    }
    return proposal


def generate_outreach_queue(
    conn: sqlite3.Connection,
    job_candidate_ids: list[int] | None = None,
    priorities: dict[int, str] | None = None,
) -> dict[str, Any]:
    """候选池分级名单 → 触达队列提案（action_type=create_task，task_type=outreach_priority）。

    输入 {job_candidate_id: "P0"/"P1"/"P2"}：为每个候选人生成一条触达提案，
    priority 映射 P0→1 / P1→2 / P2→3，rationale 以「触达队列 {P0/P1/P2}:
    优先推进/本周推进/备选」为前缀。同 jc 同优先级幂等（dedupe_key，
    INSERT OR IGNORE），重复调用返回既有提案。返回提案列表（含 proposal_id）。
    不碰 generate_proposals 的既有核验提案逻辑。
    """
    # 确保行可按下标取列（调用方可能传未设 row_factory 的连接）
    if getattr(conn, "row_factory", None) is None:
        conn.row_factory = sqlite3.Row
    norm: dict[int, str] = {}
    for key, value in (priorities or {}).items():
        try:
            jc_id = int(key)
        except (TypeError, ValueError):
            continue
        priority = str(value or "").strip().upper()
        if priority in OUTREACH_QUEUE_PRIORITIES:
            norm[jc_id] = priority
    ids: list[int] = []
    for value in job_candidate_ids or []:
        try:
            jc_id = int(value)
        except (TypeError, ValueError):
            continue
        if jc_id > 0 and jc_id not in ids:
            ids.append(jc_id)
    if not ids:
        return {"ok": True, "proposals": [], "skipped": []}

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT jc.id AS job_candidate_id,pe.display_name AS candidate,
               pe.current_company AS company,pe.current_title AS candidate_title,
               COALESCE(NULLIF(jc.raw_client,''),c.name) AS client,
               COALESCE(NULLIF(jc.raw_position,''),j.title) AS job,
               COALESCE(jc.updated_at,'') AS updated_at
        FROM job_candidates jc
        JOIN people pe ON pe.id=jc.person_id
        LEFT JOIN jobs j ON j.id=jc.job_id
        LEFT JOIN clients c ON c.id=j.client_id
        WHERE jc.id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    rows_by_id = {int(row["job_candidate_id"]): row for row in rows}

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for jc_id in ids:
        row = rows_by_id.get(jc_id)
        if row is None:
            skipped.append({"job_candidate_id": jc_id, "reason": "人岗关系不存在"})
            continue
        priority = norm.get(jc_id, "P2")
        numeric_priority, label = OUTREACH_QUEUE_PRIORITIES[priority]
        rationale = f"触达队列 {priority}: {label}"
        request = {
            "job_candidate_id": jc_id,
            "task_type": "outreach_priority",
            "reason": rationale,
            "due_at": "",
            "priority": numeric_priority,
            "write": True,
        }
        dedupe_key = hashlib.sha256(
            f"outreach_queue|{jc_id}|create_task|{_dumps(request)}".encode("utf-8")
        ).hexdigest()
        proposal_id = f"proposal_{jc_id}_{dedupe_key[:10]}"
        snapshot_hash = hashlib.sha256(
            f"outreach_queue|{jc_id}|{row['candidate']}|{row['company']}|"
            f"{row['client']}|{row['job']}|{row['updated_at']}".encode("utf-8")
        ).hexdigest()
        conn.execute(
            """
            INSERT OR IGNORE INTO agent_action_proposals
            (proposal_id,job_candidate_id,assessment_id,snapshot_hash,dedupe_key,
             action_type,risk_level,title,rationale,request_json,status,expires_at)
            VALUES (?,?,NULL,?,?,'create_task',?,?,?,?,'pending',datetime('now','+7 days','localtime'))
            """,
            (
                proposal_id,
                jc_id,
                snapshot_hash,
                dedupe_key,
                action_decision("create_task")["risk_level"],
                "创建触达任务",
                rationale,
                _dumps(request),
            ),
        )
        conn.execute(
            """
            UPDATE agent_action_proposals
            SET status='pending',review_note='',reviewed_at=NULL,action_id=NULL,
                updated_at=datetime('now','localtime')
            WHERE dedupe_key=? AND status='failed' AND snapshot_hash=?
            """,
            (dedupe_key, snapshot_hash),
        )
        proposal = conn.execute(
            """
            SELECT p.*,pe.display_name AS candidate,pe.current_company AS company,
                   pe.current_title AS candidate_title,
                   COALESCE(NULLIF(jc.raw_client,''),c.name) AS client,
                   COALESCE(NULLIF(jc.raw_position,''),j.title) AS job
            FROM agent_action_proposals p
            JOIN job_candidates jc ON jc.id=p.job_candidate_id
            JOIN people pe ON pe.id=jc.person_id
            LEFT JOIN jobs j ON j.id=jc.job_id
            LEFT JOIN clients c ON c.id=j.client_id
            WHERE p.dedupe_key=?
            """,
            (dedupe_key,),
        ).fetchone()
        if proposal is not None:
            created.append(_outreach_proposal_payload(proposal))
    conn.commit()
    return {"ok": True, "proposals": created, "skipped": skipped}


def execute_outreach_queue(
    db_path: str | os.PathLike[str],
    job_candidate_ids: list[int],
    priorities: dict[int, str],
) -> dict[str, Any]:
    """触达队列便捷入口：先以只读连接取 client/job 名，再以写连接生成触达提案并落库。

    返回 {"success": True, "proposals": [...]}；任何错误返回 {"success": False, "error": ...}。
    """
    db = Path(db_path).expanduser()
    try:
        ids: list[int] = []
        for value in job_candidate_ids or []:
            try:
                jc_id = int(value)
            except (TypeError, ValueError):
                continue
            if jc_id > 0 and jc_id not in ids:
                ids.append(jc_id)
        client_job: dict[int, dict[str, str]] = {}
        read_conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        read_conn.row_factory = sqlite3.Row
        try:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                for row in read_conn.execute(
                    f"""
                    SELECT jc.id,COALESCE(NULLIF(jc.raw_client,''),c.name) AS client,
                           COALESCE(NULLIF(jc.raw_position,''),j.title) AS job
                    FROM job_candidates jc
                    LEFT JOIN jobs j ON j.id=jc.job_id
                    LEFT JOIN clients c ON c.id=j.client_id
                    WHERE jc.id IN ({placeholders})
                    """,
                    tuple(ids),
                ).fetchall():
                    client_job[int(row["id"])] = {
                        "client": str(row["client"] or ""),
                        "job": str(row["job"] or ""),
                    }
        finally:
            read_conn.close()
        write_conn = sqlite3.connect(str(db))
        write_conn.row_factory = sqlite3.Row
        try:
            result = generate_outreach_queue(write_conn, ids, priorities)
            write_conn.commit()
            proposals = result.get("proposals") if isinstance(result, dict) else []
            for proposal in proposals or []:
                info = client_job.get(int(proposal.get("job_candidate_id") or 0))
                if info:
                    proposal.setdefault("client", info["client"])
                    proposal.setdefault("job", info["job"])
            return {"success": True, "proposals": proposals}
        finally:
            write_conn.close()
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def proposal_preflight(self, proposal_id: str) -> dict[str, Any]:
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_action_proposals WHERE proposal_id=?",
            (str(proposal_id or ""),),
        ).fetchone()
        if row is None:
            raise ValueError(f"找不到 Agent 提案：{proposal_id}")
        if row["status"] not in {"pending", "approved"}:
            raise ValueError(f"提案当前状态不可确认：{row['status']}")
        if row["expires_at"] and conn.execute(
            "SELECT datetime(?) < datetime('now','localtime')", (row["expires_at"],)
        ).fetchone()[0]:
            conn.execute(
                "UPDATE agent_action_proposals SET status='failed',review_note='提案已过期',updated_at=datetime('now','localtime') WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            raise ValueError("提案已过期，请重新生成")
        context = build_candidate_context(self.db_path, int(row["job_candidate_id"]))
        snapshot_hash = self._snapshot_key(context)
        if snapshot_hash != row["snapshot_hash"]:
            raise ValueError("提案依据已变化，请重新评估并生成")
        decision = action_decision(str(row["action_type"]), context)
        if decision["decision"] == "deny":
            raise ValueError(decision["reason"])
        token = secrets.token_urlsafe(24)
        signature = hashlib.sha256(
            f"{row['proposal_id']}|{row['snapshot_hash']}|{row['action_type']}|{row['request_json']}".encode("utf-8")
        ).hexdigest()
        preflight = {
            "decision": "confirm",
            "policy": decision,
            "proposal_id": row["proposal_id"],
            "action_type": row["action_type"],
            "request": _loads(row["request_json"], {}),
        }
        conn.execute(
            "UPDATE agent_action_proposals SET preflight_json=?,updated_at=datetime('now','localtime') WHERE id=?",
            (_dumps(preflight), row["id"]),
        )
        conn.commit()
        with self._lock:
            self._proposal_confirmations[token] = {
                "proposal_id": row["proposal_id"],
                "signature": signature,
                "expires_at": time.time() + 300,
            }
        return {
            "ok": True,
            **preflight,
            "confirmation_token": token,
            "expires_in": 300,
        }
    finally:
        conn.close()


def decide_proposal(
    self,
    proposal_id: str,
    confirmation_token: str,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    decision = str(decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise ValueError("decision 必须是 approve/reject")
    with self._lock:
        confirmation = self._proposal_confirmations.pop(str(confirmation_token or ""), None)
    if not confirmation or confirmation["expires_at"] < time.time():
        raise ValueError("提案确认令牌无效或已过期")
    if confirmation["proposal_id"] != str(proposal_id):
        raise ValueError("提案确认令牌与提案不匹配")
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_action_proposals WHERE proposal_id=?",
            (str(proposal_id),),
        ).fetchone()
        if row is None or row["status"] not in {"pending", "approved"}:
            raise ValueError("提案已变化，请重新预检")
        signature = hashlib.sha256(
            f"{row['proposal_id']}|{row['snapshot_hash']}|{row['action_type']}|{row['request_json']}".encode("utf-8")
        ).hexdigest()
        if signature != confirmation["signature"]:
            raise ValueError("提案内容已变化，请重新预检")
        status = "approved" if decision == "approve" else "rejected"
        conn.execute(
            """
            UPDATE agent_action_proposals
            SET status=?,review_note=?,reviewed_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (status, str(note or ""), row["id"]),
        )
        conn.commit()
        return {
            "ok": True,
            "proposal_id": row["proposal_id"],
            "status": status,
            "action_type": row["action_type"],
            "job_candidate_id": row["job_candidate_id"],
            "request": _loads(row["request_json"], {}),
            "dedupe_key": row["dedupe_key"],
        }
    finally:
        conn.close()


def finish_proposal(
    self,
    proposal_id: str,
    *,
    success: bool,
    action_id: int | None = None,
    note: str = "",
) -> dict[str, Any]:
    status = "executed" if success else "failed"
    conn = self._connect()
    try:
        conn.execute(
            """
            UPDATE agent_action_proposals
            SET status=?,action_id=?,review_note=CASE WHEN ?='' THEN review_note ELSE ? END,
                updated_at=datetime('now','localtime')
            WHERE proposal_id=? AND status='approved'
            """,
            (status, action_id, str(note or ""), str(note or ""), str(proposal_id)),
        )
        if conn.total_changes != 1:
            raise ValueError("提案不是待执行状态")
        conn.commit()
        return {"ok": True, "proposal_id": proposal_id, "status": status}
    finally:
        conn.close()


def execute_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
    """Execute the single internally-approved proposal action supported in v1.

    This lives beside proposal state transitions so Core and the legacy workbench
    cannot create different task, audit, or terminal-state semantics.
    """
    proposal_id = str(proposal.get("proposal_id") or "")
    action_type = str(proposal.get("action_type") or "")
    if action_type != "create_task":
        if proposal_id:
            try:
                self.finish_proposal(
                    proposal_id,
                    success=False,
                    note=f"不支持自动执行动作：{action_type or 'unknown'}",
                )
            except ValueError:
                pass
        raise ValueError(f"不支持自动执行动作：{action_type or 'unknown'}")

    job_candidate_id = int(proposal.get("job_candidate_id") or 0)
    dedupe_key = str(proposal.get("dedupe_key") or "")
    request = dict(proposal.get("request") or {})
    if not proposal_id or not job_candidate_id or not dedupe_key:
        raise ValueError("提案执行参数不完整")

    existing_action = self.get_action(dedupe_key)
    if existing_action and existing_action.get("status") == "executed":
        result = _loads(existing_action.get("result_json"), {})
        post_check = result.get("post_check") if isinstance(result.get("post_check"), dict) else {
            "mode": "result",
            "status": "unknown",
            "summary": "该任务由较早版本创建，未记录回查结果",
        }
        return {"ok": True, "cached": True, "proposal_id": proposal_id, "status": "executed", **result, "post_check": post_check}

    task_type = str(request.get("task_type") or "agent_verification")
    reason = str(request.get("reason") or "")
    due_at = str(request.get("due_at") or "") or (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    priority = int(request.get("priority") or 2)
    if priority not in {0, 1, 2, 3}:
        raise ValueError("任务优先级只能是 0-3")

    conn = self._connect()
    try:
        if not _table_exists(conn, "followup_tasks"):
            raise ValueError("跟进任务表尚未初始化")
        relation = conn.execute(
            """
            SELECT jc.id AS job_candidate_id,jc.person_id,jc.job_id,jc.source_candidate_id,
                   p.display_name,p.current_company,
                   COALESCE(NULLIF(jc.raw_client,''),c.name) AS client,
                   COALESCE(NULLIF(jc.raw_position,''),j.title) AS job
            FROM job_candidates jc
            JOIN people p ON p.id=jc.person_id
            LEFT JOIN jobs j ON j.id=jc.job_id
            LEFT JOIN clients c ON c.id=j.client_id
            WHERE jc.id=?
            """,
            (job_candidate_id,),
        ).fetchone()
        if relation is None:
            raise ValueError(f"没有找到推进关系 #{job_candidate_id}")
        existing_task = conn.execute(
            """
            SELECT id,due_at FROM followup_tasks
            WHERE job_candidate_id=? AND task_type=? AND COALESCE(reason,'')=?
              AND COALESCE(status,'open')='open'
            ORDER BY id DESC LIMIT 1
            """,
            (job_candidate_id, task_type, reason),
        ).fetchone()
        if existing_task is not None:
            task_result = {
                "task_id": existing_task["id"],
                "job_candidate_id": job_candidate_id,
                "due_at": existing_task["due_at"],
                "message": "已有相同的开放 Agent 任务",
            }
        else:
            cursor = conn.execute(
                """
                INSERT INTO followup_tasks (
                    candidate_id,candidate_name,candidate_company,client,position,
                    task_type,priority,due_at,status,reason,source_table,source_id,
                    created_at,updated_at,job_candidate_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'a_system_agent', ?,
                          datetime('now','localtime'),datetime('now','localtime'),?)
                """,
                (
                    int(relation["source_candidate_id"]) if str(relation["source_candidate_id"] or "").isdigit() else None,
                    relation["display_name"], relation["current_company"], relation["client"], relation["job"],
                    task_type, priority, due_at, reason, job_candidate_id, job_candidate_id,
                ),
            )
            task_id = int(cursor.lastrowid)
            conn.execute("UPDATE followup_tasks SET id=? WHERE rowid=?", (task_id, task_id))
            event = conn.execute(
                """
                INSERT INTO candidate_events (
                    job_candidate_id,person_id,job_id,event_type,event_status,event_time,
                    summary,raw_json,source_table,source_id
                ) VALUES (?, ?, ?, 'followup_task', 'open', datetime('now','localtime'), ?, ?, 'followup_tasks', ?)
                """,
                (
                    job_candidate_id, relation["person_id"], relation["job_id"], reason or task_type,
                    _dumps({"task_type": task_type, "priority": priority, "due_at": due_at}), str(task_id),
                ),
            )
            task_result = {
                "task_id": task_id,
                "event_id": int(event.lastrowid),
                "job_candidate_id": job_candidate_id,
                "due_at": due_at,
                "message": f"已创建跟进任务：{relation['display_name']}｜{task_type}",
            }
        conn.commit()
    except Exception as exc:
        conn.rollback()
        try:
            self.finish_proposal(proposal_id, success=False, note=str(exc)[:500])
        except ValueError:
            pass
        raise
    finally:
        conn.close()

    task_id = int(task_result.get("task_id") or 0)
    check_conn = self._connect()
    try:
        task_exists = bool(
            task_id
            and check_conn.execute(
                "SELECT 1 FROM followup_tasks WHERE id=? AND job_candidate_id=?", (task_id, job_candidate_id)
            ).fetchone()
        )
    finally:
        check_conn.close()
    post_check = {
        "mode": "result",
        "status": "verified" if task_exists else "failed",
        "task_id": task_id or None,
        "summary": "跟进任务已写入并可回查" if task_exists else "跟进任务写入后未能回查，请人工检查",
    }
    audited_result = {**task_result, "post_check": post_check}
    action_result = self.record_external_action(
        job_candidate_id=job_candidate_id,
        action_type="create_task",
        request=request,
        result=audited_result,
        idempotency_key=dedupe_key,
    )
    action = self.get_action(dedupe_key)
    self.finish_proposal(
        proposal_id,
        success=True,
        action_id=int(action["id"]) if action and action.get("id") else None,
    )
    return {"ok": True, "proposal_id": proposal_id, "status": "executed", **action_result, "post_check": post_check}


def list_learning_rules(self, status: str = "all", limit: int = 50) -> dict[str, Any]:
    status = str(status or "all").strip().lower()
    if status not in {"all", "collecting", "pending", "active", "suspended", "revoked"}:
        raise ValueError("未知学习规则状态")
    limit = max(1, min(int(limit or 50), 200))
    where = "" if status == "all" else "WHERE status=?"
    params: tuple[Any, ...] = (limit,) if status == "all" else (status, limit)
    conn = self._connect()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM agent_learning_rules
            {where}
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'collecting' THEN 1 WHEN 'active' THEN 2 WHEN 'suspended' THEN 3 ELSE 4 END,
                     support_count DESC,id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        rules = []
        for row in rows:
            item = _row(row)
            item["rule"] = _loads(item.pop("rule_json", "{}"), {})
            rules.append(item)
        return {"ok": True, "status": status, "rules": rules}
    finally:
        conn.close()


def learning_preflight(self, rule_id: int) -> dict[str, Any]:
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_learning_rules WHERE id=?", (int(rule_id),)
        ).fetchone()
        if row is None:
            raise ValueError(f"找不到学习规则：{rule_id}")
        if row["status"] != "pending":
            raise ValueError(f"学习规则当前状态不可确认：{row['status']}")
        if (
            int(row["support_count"] or 0) < int(self.config["learning"]["minimum_support"])
            or int(row["candidate_count"] or 0) < int(self.config["learning"]["minimum_candidates"])
        ):
            raise ValueError("学习规则尚未达到支持样本阈值")
        token = secrets.token_urlsafe(24)
        signature = hashlib.sha256(
            f"{row['id']}|{row['rule_key']}|{row['version']}|{row['rule_json']}".encode("utf-8")
        ).hexdigest()
        with self._lock:
            self._learning_confirmations[token] = {
                "rule_id": int(row["id"]),
                "signature": signature,
                "expires_at": time.time() + 300,
            }
        return {
            "ok": True,
            "decision": "confirm",
            "confirmation_token": token,
            "expires_in": 300,
            "rule": {
                "id": row["id"],
                "client": row["client"],
                "job": row["job"],
                "rule_type": row["rule_type"],
                "rule": _loads(row["rule_json"], {}),
                "version": row["version"],
                "support_count": row["support_count"],
                "candidate_count": row["candidate_count"],
            },
        }
    finally:
        conn.close()


def learning_commit(self, rule_id: int, confirmation_token: str) -> dict[str, Any]:
    with self._lock:
        confirmation = self._learning_confirmations.pop(str(confirmation_token or ""), None)
    if not confirmation or confirmation["expires_at"] < time.time():
        raise ValueError("学习规则确认令牌无效或已过期")
    if confirmation["rule_id"] != int(rule_id):
        raise ValueError("学习规则确认令牌与规则不匹配")
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_learning_rules WHERE id=?", (int(rule_id),)
        ).fetchone()
        if row is None or row["status"] != "pending":
            raise ValueError("学习规则已变化，请重新预检")
        signature = hashlib.sha256(
            f"{row['id']}|{row['rule_key']}|{row['version']}|{row['rule_json']}".encode("utf-8")
        ).hexdigest()
        if signature != confirmation["signature"]:
            raise ValueError("学习规则内容已变化，请重新预检")
        conn.execute(
            """
            UPDATE agent_learning_rules SET status='revoked',revoked_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime')
            WHERE rule_key=? AND status='active' AND id<>?
            """,
            (row["rule_key"], row["id"]),
        )
        conn.execute(
            """
            UPDATE agent_learning_rules SET status='active',approved_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime') WHERE id=?
            """,
            (row["id"],),
        )
        conn.commit()
        result = {"ok": True, "rule_id": row["id"], "status": "active"}
    finally:
        conn.close()
    self.store_memory(
        scope_type="job", scope_id=row["job"], memory_type="approved_learning_rule",
        content=f"已批准的岗位判断规则：{row['rule_json']}",
        source_type="agent_learning_rule", source_id=row["id"], confidence=1.0,
    )
    return result

# ------------------------------------------------------------------
# S7-1：人才流动雷达 —— 手动触发扫描 + 读最新榜单（同日幂等）
# ------------------------------------------------------------------


def create_radar_scan(
    self,
    *,
    collector: Any = None,
    extractor: Any = None,
    as_of: str = "",
    max_companies: int = 0,
    max_workers: int = 1,
) -> dict[str, Any]:
    """S7-1：人才流动雷达扫描——公司池检索公开信号 → LLM 抽取 → 榜单，落 radar_scan artifact。

    红线：全部公开信息只读；无来源信号拒写；来源 URL 必须来自真实检索结果；
    禁挖名单照常过滤；restricted 仅白名单出库；不自动触达（动作由顾问本人执行）。
    collector/extractor 可注入（测试用本地 fixture，绝不打外网/不依赖真实模型）。
    同日重复扫描更新同一 artifact（radar_scan_<scan_date>，version 自增）。
    """
    from . import radar_scan

    conn = self._connect()
    try:
        doc = radar_scan.build_radar_scan(
            conn,
            collector=collector,
            extractor=extractor,
            llm=self.llm,
            as_of=as_of,
            max_companies=max_companies,
            max_workers=max_workers,
            url_resolver=radar_scan.resolve_source_url,
        )
        artifact_id = radar_scan.upsert_radar_scan(conn, doc)
        conn.commit()
        stats = doc.get("stats") or {}
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "scan_date": doc.get("scan_date"),
            "ranking_file": doc.get("ranking_file"),
            "summary": (
                f"扫描公司 {stats.get('companies_scanned', 0)} 家，发现信号 {stats.get('signals_found', 0)} 条"
                f"（全部带来源链接）；失败 {stats.get('sources_failed', 0)} 次已留痕，"
                f"禁挖过滤 {stats.get('banned_filtered', 0)} 条。榜单仅供顾问本人决策，系统不自动触达。"
            ),
            "radar_scan": doc,
        }
    finally:
        conn.close()


def get_latest_radar_scan(self) -> dict[str, Any]:
    """S7-1：读取最新雷达榜单；尚无扫描抛 LookupError（404）。"""
    from . import radar_scan

    conn = self._connect()
    try:
        payload = radar_scan.get_latest_radar_scan(conn)
        if payload is None:
            raise LookupError("还没有雷达榜单：请先发起一次扫描（POST /api/v1/radar/scans）")
        return {"ok": True, **payload}
    finally:
        conn.close()

# ------------------------------------------------------------------
# S7-2：雷达联动——榜单一键发起 Mapping（trigger=radar）+ 激活存量人选清单
# ------------------------------------------------------------------


def start_mapping_from_radar(self, company: str, job_id: int, *, collector: Any = None) -> dict[str, Any]:
    """S7-2：对最新雷达榜单里的一家公司发起 Mapping 直挖（trigger="radar"）。

    - 调 mapping_task 既有创建链路，trigger="radar"；目标团队定位注入该公司所在榜单的
      未过期信号上下文（只提升定位质量，信号正文/链接不进 artifact 对外字段，见
      stats.radar_context 标记）；
    - strategy_ref 取该 job 最新 strategy_v2；岗位暂无策略时允许为 null（仅 radar 触发
      合法），并在 stats.radar_context.strategy_ref_missing 注明；
    - 同日幂等：同岗位同公司当天已有 trigger=radar 的 mapping_task → 返回已存在，
      不重复建 task（version 不变）；自然日起算（本地日期）。
    404：岗位不存在 / 尚无雷达榜单（LookupError）；409：公司名为空（ValueError）。
    红线沿用 S5：无来源不进名单、禁挖过滤、不自动触达；restricted 仅白名单出库。
    collector 可注入（测试用本地 fixture，绝不打外网）。
    """
    from . import knowledge_base, mapping_task, radar_scan, strategy_v2

    company = str(company or "").strip()
    if not company:
        raise ValueError("company 必须非空（要对哪家公司发起 Mapping）")
    conn = self._connect()
    try:
        job = conn.execute(
            "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise LookupError(f"岗位不存在：{job_id}")
        latest = radar_scan.get_latest_radar_scan(conn)
        if latest is None:
            raise LookupError("还没有雷达榜单：请先发起一次扫描（POST /api/v1/radar/scans）")

        # 岗位最新 strategy_v2（无则按 null 处理，stats 注明；radar 触发专属放宽）
        strategy = conn.execute(
            """
            SELECT w.workflow_id,g.goal_id,a.artifact_id,a.metadata_json
            FROM agent_workflows w
            JOIN agent_goals g ON g.goal_id=w.goal_id
            JOIN agent_artifacts a ON a.workflow_id=w.workflow_id AND a.artifact_type='search_strategy'
            WHERE g.context_type='job' AND g.context_id=?
            ORDER BY a.id DESC LIMIT 1
            """,
            (int(job_id),),
        ).fetchone()
        strategy_doc = None
        strategy_ref = ""
        workflow_id = ""
        goal_id = ""
        if strategy is not None:
            strategy_doc = strategy_v2.extract_strategy_v2(strategy["metadata_json"])
            strategy_ref = str(strategy["artifact_id"])
            workflow_id = str(strategy["workflow_id"])
            goal_id = str(strategy["goal_id"])
        if not workflow_id:
            # 无策略 artifact 时退回岗位最新工作流（upsert 幂等键需要 workflow_id）
            fallback = conn.execute(
                """
                SELECT w.workflow_id,g.goal_id
                FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                WHERE g.context_type='job' AND g.context_id=?
                ORDER BY w.id DESC LIMIT 1
                """,
                (int(job_id),),
            ).fetchone()
            if fallback is None:
                raise ValueError(f"岗位 {job_id} 还没有工作流，无法发起 Mapping 直挖")
            workflow_id = str(fallback["workflow_id"])
            goal_id = str(fallback["goal_id"])

        # 同日幂等：该工作流当天已有 trigger=radar 且同公司的 mapping_task → 返回已存在
        today = datetime.now().strftime("%Y-%m-%d")
        existing = mapping_task.get_mapping_task(conn, f"mapping_task_{workflow_id}")
        if existing is not None:
            existing_doc = existing.get("mapping_task") or {}
            existing_radar = (existing_doc.get("stats") or {}).get("radar_context") or {}
            if (
                existing_doc.get("trigger") == "radar"
                and str(existing_doc.get("generated_at") or "").startswith(today)
                and str(existing_radar.get("company") or "") == company
            ):
                return {
                    "ok": True,
                    "already_exists": True,
                    "job_id": int(job_id),
                    "workflow_id": workflow_id,
                    "artifact_id": str(existing.get("artifact_id") or ""),
                    "note": "今天已对该公司发起过 Mapping（trigger=radar），返回既有任务卡，未重复创建",
                    "mapping_task": existing_doc,
                }

        radar_context, scan_artifact_id = radar_scan.radar_context_by_company(conn)

        archetype = None
        graph = None
        if strategy_doc is not None:
            archetypes, _load_trace = strategy_v2.load_job_archetypes()
            archetype_id = str(strategy_doc.get("archetype_id") or "")
            for item in archetypes:
                if str(item.get("archetype_id") or "") == archetype_id:
                    archetype = item
                    break
            graph, _graph_trace = knowledge_base.load_company_graph()

        doc = mapping_task.build_mapping_task(
            job_id=int(job_id),
            trigger="radar",
            strategy_ref=strategy_ref,
            strategy_doc=strategy_doc,
            client=str(job["client"] or ""),
            job_title=str(job["title"] or ""),
            graph=graph,
            archetype=archetype,
            collector=collector,
            radar_context=radar_context or None,
            radar_company=company,
            radar_scan_ref=scan_artifact_id,
        )
        doc["workflow_id"] = workflow_id
        doc["goal_id"] = goal_id
        artifact_id = mapping_task.upsert_mapping_task(conn, doc)
        stats = doc.get("stats") or {}
        summary = (
            f"雷达联动发起 Mapping（{company}）：目标团队 {stats.get('teams', 0)} 个、"
            f"候选目标人 {stats.get('candidates', 0)} 位"
            f"（禁挖过滤 {stats.get('banned_filtered', 0)}、无来源拒收 {stats.get('rejected_no_source', 0)}）。"
            "名单仅供顾问本人决策，系统不自动触达。"
        )
        conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (NULL,NULL,?,'mapping_task_created','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
            """,
            (
                int(job_id),
                summary,
                json.dumps(
                    {
                        "artifact_id": artifact_id,
                        "workflow_id": workflow_id,
                        "trigger": "radar",
                        "radar_company": company,
                        "teams": stats.get("teams", 0),
                        "candidates": stats.get("candidates", 0),
                    },
                    ensure_ascii=False,
                ),
                artifact_id,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "already_exists": False,
            "job_id": int(job_id),
            "workflow_id": workflow_id,
            "artifact_id": artifact_id,
            "mapping_task": doc,
        }
    finally:
        conn.close()


def activate_radar_company(self, company: str, *, limit: int = 50) -> dict[str, Any]:
    """S7-2：激活存量人选——人才库里该公司现职/曾任职的候选人清单（现职优先）。

    只读不写库；动作（是否触达、怎么触达）永远由顾问本人执行。
    字段：候选人 id、遮罩名、当前职务、入库阶段（最近一条 job_candidates.clean_stage）、
    最近一次动作日期（candidate_events 最近 event_time，无则退回 updated_at/search_date）。
    404：尚无雷达榜单（LookupError）；409：公司名为空（ValueError）。
    """
    from . import radar_scan
    from .workflow import _mask_candidate_name

    company = str(company or "").strip()
    if not company:
        raise ValueError("company 必须非空（要查哪家公司的存量人选）")
    conn = self._connect()
    try:
        if radar_scan.get_latest_radar_scan(conn) is None:
            raise LookupError("还没有雷达榜单：请先发起一次扫描（POST /api/v1/radar/scans）")
        if not _table_exists(conn, "candidates"):
            return {"ok": True, "company": company, "total": 0, "candidates": [], "note": "人才库为空"}

        rows = conn.execute(
            "SELECT id,name,company,title,status,updated_at,search_date FROM candidates"
        ).fetchall()
        current_hits: list[dict[str, Any]] = []
        for row in rows:
            if radar_scan.company_matches(company, str(row["company"] or "")):
                current_hits.append(dict(row))

        # 曾任职：source_profiles.raw_json 文本命中该公司（现职未命中的人才库人选）
        current_ids = {int(row["id"]) for row in current_hits}
        history_hits: list[dict[str, Any]] = []
        if _table_exists(conn, "source_profiles") and _table_exists(conn, "job_candidates"):
            tokens = [token for token in radar_scan._company_alias_tokens(company) if len(token.strip()) >= 2]
            if tokens:
                like = tokens[0]
                profile_rows = conn.execute(
                    "SELECT person_id,raw_json FROM source_profiles WHERE raw_json LIKE ? LIMIT 500",
                    (f"%{like}%",),
                ).fetchall()
                candidate_rows = conn.execute(
                    """
                    SELECT c.id,c.name,c.company,c.title,c.status,c.updated_at,c.search_date,jc.person_id
                    FROM job_candidates jc JOIN candidates c ON CAST(c.id AS TEXT)=jc.source_candidate_id
                    """
                ).fetchall()
                by_person: dict[int, dict[str, Any]] = {}
                for row in candidate_rows:
                    person_id = int(row["person_id"] or 0)
                    if person_id and int(row["id"]) not in current_ids and person_id not in by_person:
                        by_person[person_id] = dict(row)
                for profile in profile_rows:
                    person_id = int(profile["person_id"] or 0)
                    hit = by_person.get(person_id)
                    if hit is not None:
                        history_hits.append(hit)

        def enrich(row: dict[str, Any], tenure: str) -> dict[str, Any]:
            stage_rows = conn.execute(
                """
                SELECT clean_stage,updated_at FROM job_candidates
                WHERE CAST(? AS TEXT)=source_candidate_id ORDER BY updated_at DESC LIMIT 1
                """,
                (str(row["id"]),),
            ).fetchall()
            stage = str(stage_rows[0]["clean_stage"] or "") if stage_rows else ""
            event_rows = conn.execute(
                """
                SELECT MAX(ce.event_time) AS last_action
                FROM candidate_events ce JOIN job_candidates jc ON jc.id=ce.job_candidate_id
                WHERE CAST(? AS TEXT)=jc.source_candidate_id
                """,
                (str(row["id"]),),
            ).fetchone()
            last_action = ""
            if event_rows is not None and event_rows["last_action"]:
                last_action = str(event_rows["last_action"])
            if not last_action:
                last_action = str(row.get("updated_at") or row.get("search_date") or "")
            return {
                "id": int(row["id"]),
                "name_masked": _mask_candidate_name(row.get("name")),
                "current_title": str(row.get("title") or ""),
                "current_company": str(row.get("company") or ""),
                "tenure": tenure,
                "stage": stage or str(row.get("status") or ""),
                "last_action_at": last_action,
            }

        items = [enrich(row, "现职") for row in current_hits]
        items.extend(enrich(row, "曾任职") for row in history_hits)
        items.sort(key=lambda item: (0 if item["tenure"] == "现职" else 1, item["last_action_at"]), reverse=False)
        items.sort(key=lambda item: item["last_action_at"], reverse=True)
        items.sort(key=lambda item: 0 if item["tenure"] == "现职" else 1)
        items = items[: max(1, int(limit))]
        return {
            "ok": True,
            "company": company,
            "total": len(items),
            "candidates": items,
            "note": "清单只读展示；是否触达、怎么触达由顾问本人决定，系统不自动触达",
        }
    finally:
        conn.close()

# ------------------------------------------------------------------
# S7-3：雷达周报——Top 信号/过期统计/榜单变化对比 + Copilot 提醒推送（同日幂等）
# ------------------------------------------------------------------


def create_radar_weekly_report(self, *, push_copilot: bool = True) -> dict[str, Any]:
    """S7-3：生成雷达周报（内容全部来自库内最近两期 radar_scan artifact，不现编）。

    缺上一期榜单 → 周报如实标注"首期，无对比基线"；尚无榜单抛 LookupError（404）。
    生成后向 Copilot 仲裁层推一条提醒（只推条数和入口，不含敏感细节）；
    推送失败不阻断周报（copilot.pushed=False 留痕）。同日重复生成更新同一 artifact。
    """
    from . import radar_weekly

    conn = self._connect()
    try:
        doc = radar_weekly.build_weekly_report(conn)
        artifact_id = radar_weekly.upsert_weekly_report(conn, doc)
        conn.commit()
    finally:
        conn.close()
    if push_copilot:
        copilot = radar_weekly.push_copilot_hint(doc.get("copilot_hint") or {})
    else:
        copilot = {"pushed": False, "note": "未推送（push_copilot=False）"}
    baseline = doc.get("baseline") or {}
    return {
        "ok": True,
        "artifact_id": artifact_id,
        "report_date": doc.get("report_date"),
        "report_file": doc.get("report_file"),
        "copilot": copilot,
        "summary": (
            f"周报 {doc.get('report_date')}：Top 信号 {len(doc.get('top_signals') or [])} 条，"
            f"过期降权 {doc.get('expired_signal_count', 0)} 条，"
            f"建议发起 Mapping {(doc.get('action_summary') or {}).get('mapping', 0)} 家"
            f"（{'对比基线 ' + str(baseline.get('scan_date')) if baseline.get('has_baseline') else '首期，无对比基线'}）。"
            "动作由顾问本人执行，系统不自动触达。"
        ),
        "weekly_report": doc,
    }


def get_latest_radar_weekly_report(self) -> dict[str, Any]:
    """S7-3：读取最新雷达周报；尚无周报抛 LookupError（404）。"""
    from . import radar_weekly

    conn = self._connect()
    try:
        payload = radar_weekly.get_latest_weekly_report(conn)
        if payload is None:
            raise LookupError("还没有雷达周报：请先生成（POST /api/v1/radar/weekly-report）")
        return {"ok": True, **payload}
    finally:
        conn.close()
