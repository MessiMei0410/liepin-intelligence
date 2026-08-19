from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from .database import DEFAULT_DB, connect, json_value, transaction
from .service_candidate_actions import (  # noqa: F401 模块级兼容 re-export（既有测试/调用方不变）
    CANDIDATE_ACTION_CONFIRM_TEXTS,
    CANDIDATE_ACTION_LABELS,
    CANDIDATE_UPDATE_LABELS,
    IDEMPOTENCY_LEASE_MINUTES,
    LIFECYCLE_EVENT_TYPES,
    PACKAGE_FEEDBACK_TYPE_LABELS,
    STOP_STAGES,
    STOP_STATUSES,
    STOP_TOKENS,
    STRUCTURED_CANDIDATE_ACTIONS,
    CandidateActionsMixin,
    IdempotencyConflict,
    _candidate_action_already_applied,
    _candidate_action_card,
    _is_stopped,
    _row,
)
from .service_copilot_bridge import (  # noqa: F401 模块级兼容 re-export（既有测试/调用方不变）
    NON_BUSINESS_COPILOT_ACTIONS,
    READ_ONLY_COPILOT_ACTIONS,
    CopilotBridgeMixin,
    _enforce_copilot_action_boundary,
    _explicit_candidate_action,
    _explicit_candidate_update,
    _is_copilot_correction,
    _is_sourcing_result_action_card,
    _without_workflow_source_claim,
    _workflow_action_card,
    _COPILOT_CORRECTION_RE,
)
from .service_dedupe import CandidateDedupeMixin  # noqa: F401
from .service_workflow_ops import WorkflowOpsMixin, _funnel_detail  # noqa: F401
from .stop_reasons import STOP_REASON_LABELS, UNLABELED_STOP_REASON_LABEL
from a_system_agent import knowledge_base as kb_consumption


def _public_effective_strategy(metadata: Any, row: Any) -> dict[str, Any]:
    """Project a search artifact to job UI without restricted strategy material."""
    source = metadata if isinstance(metadata, dict) else {}
    v2 = source.get("strategy_v2") if isinstance(source.get("strategy_v2"), dict) else {}
    plan = source.get("plan") if isinstance(source.get("plan"), dict) else {}
    company_tiers: list[dict[str, Any]] = []
    for tier in v2.get("step2_target_pool") or []:
        if not isinstance(tier, dict):
            continue
        companies = [
            str(item.get("name") or "").strip()
            for item in (tier.get("companies") or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and not str(item.get("source") or "").startswith("restricted")
        ]
        if companies:
            company_tiers.append({
                "path": str(tier.get("path") or ""),
                "tier": str(tier.get("tier") or ""),
                "companies": companies,
                "rationale": str(tier.get("rationale") or ""),
            })
    keyword_groups = [
        {
            "group": str(group.get("group") or ""),
            "targets": str(group.get("targets") or ""),
            "terms": [str(term).strip() for term in (group.get("terms") or []) if str(term).strip()],
        }
        for group in (v2.get("step4_keyword_groups") or [])
        if isinstance(group, dict)
    ]
    context = json_value(row["context_json"], {}) if row else {}
    return {
        "status": str(row["workflow_status"] or "") if row else "",
        "plan_version": int(context.get("revision_number") or 0) + 1,
        "generated_at": str(row["created_at"] or "") if row else "",
        "summary": str(
            (v2.get("step1_job_essence") or {}).get("statement")
            or plan.get("strategy_summary")
            or ""
        ),
        "input_level": str(v2.get("input_level") or ""),
        "company_tiers": company_tiers,
        "level_mapping": dict(v2.get("step3_level_mapping") or {}),
        "keyword_groups": keyword_groups,
        "expectation": dict(v2.get("step5_expectation") or {}),
        "consultant_constraints": [
            {
                "type": str(item.get("type") or item.get("kind") or "other"),
                "rule": str(item.get("rule") or item.get("quote") or "").strip(),
            }
            for item in (plan.get("consultant_constraints") or [])
            if isinstance(item, dict) and str(item.get("rule") or item.get("quote") or "").strip()
        ],
        "audit": {
            "workflow_id": str(row["workflow_id"] or "") if row else "",
            "artifact_id": str(row["artifact_id"] or "") if row else "",
            "schema_version": str(source.get("schema_version") or v2.get("schema_version") or ""),
        },
    }


_RESUME_OVERVIEW_SECTION_RE = re.compile(
    r"^(工作经历|工作经验|项目经历|项目经验|教育经历|教育背景|技能(?:特长)?|语言能力|自我评价|附件简历)",
    re.MULTILINE,
)


def _resume_overview_summary(resume: dict[str, Any]) -> str:
    """Return only the resume header for the overview API field, never the resume body."""
    for key in ("profile_text", "notes"):
        text = str(resume.get(key) or "").strip()
        if not text:
            continue
        match = _RESUME_OVERVIEW_SECTION_RE.search(text)
        header = text[:match.start()] if match else text
        return header.strip()[:800]
    return ""


class CoreService(CandidateActionsMixin, CopilotBridgeMixin, WorkflowOpsMixin, CandidateDedupeMixin):
    """ASA Core 服务门面。

    Mixin 组合 facade（P2-1）：候选人动作/预检/幂等在 CandidateActionsMixin
    （service_candidate_actions.py），copilot 消息桥接在 CopilotBridgeMixin
    （service_copilot_bridge.py），工作流操作在 WorkflowOpsMixin
    （service_workflow_ops.py）；本类保留仪表盘/岗位/候选人读取、审计、
    停止原因统计与雷达/周报薄封装。方法体逐字节迁移，语义不变。
    """

    def __init__(self, db_path: Path = DEFAULT_DB, agent_service: Any | None = None, analytics_service: Any | None = None) -> None:
        self.db_path = db_path
        self.agent_service = agent_service
        self.analytics_service = analytics_service
        # preflight token → (目标, 动作, 过期时间, 是否已激活)。
        # 候选人动作/审批决定/工作流动作的 token 铸造时为未激活，必须经 UI 确认
        # （POST /api/v1/write-confirmations/activate）激活后才可写入——这是 DSH 脑
        # 写动作的人确认机制闸门（模型工具面拿不到激活能力）；Python 脑 pending_intent
        # 确认链路（intents/confirm）不经 HTTP 写端点，自带签名确认，不受影响。
        self._preflight_tokens: dict[str, tuple[Any, str, datetime, bool]] = {}
        self._preflight_lock = threading.Lock()
        self._strategy_edit_tokens: dict[str, dict[str, Any]] = {}
        self._bootstrap_cache: dict[str, Any] | None = None
        self._bootstrap_cache_ts: float = 0.0
        self._bootstrap_cache_ttl: float = 5.0

    def bootstrap(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._bootstrap_cache and (now - self._bootstrap_cache_ts) < self._bootstrap_cache_ttl:
            return self._bootstrap_cache
        dashboard = self.dashboard()
        result = {
            "ok": True,
            "core": {"status": "connected", "db": str(self.db_path), "api_version": "v1"},
            "user": {"id": "local", "name": "本机顾问"},
            "counts": dashboard["counts"],
            "features": {
                "workflows": True,
                "copilot": True,
                "desktop_bridge": True,
                "legacy_admin": True,
            },
        }
        self._bootstrap_cache = result
        self._bootstrap_cache_ts = now
        return result

    def dashboard(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            counts = {
                "active_jobs": conn.execute(
                    """SELECT count(*) FROM jobs
                       WHERE COALESCE(lifecycle_stage,'') IN ('sourcing','published','active_pipeline','client_feedback','offer')
                         AND COALESCE(status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')"""
                ).fetchone()[0],
                "candidates": conn.execute("SELECT count(*) FROM job_candidates").fetchone()[0],
                "pending_candidates": conn.execute(
                    """SELECT count(*) FROM job_candidates
                       WHERE COALESCE(clean_stage,'') NOT LIKE '%初筛不通过%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%停止%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%淘汰%'
                         AND COALESCE(clean_stage,'') NOT LIKE '%关闭%'
                         AND lower(COALESCE(raw_status,'')) NOT IN ('screen_rejected','xsaas_review_stop','stopped','closed','rejected')"""
                ).fetchone()[0],
                "pending_approvals": conn.execute(
                    "SELECT count(*) FROM agent_approvals WHERE status='pending'"
                ).fetchone()[0],
                "pending_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='pending'"
                ).fetchone()[0],
                "executed_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='executed'"
                ).fetchone()[0],
                "failed_proposals": conn.execute(
                    "SELECT count(*) FROM agent_action_proposals WHERE status='failed'"
                ).fetchone()[0],
            }
            workflows = [
                _row(row)
                for row in conn.execute(
                    """SELECT w.workflow_id,w.status,w.business_outcome,w.current_stage,w.updated_at,g.title,g.progress
                       FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                       WHERE w.archived_at IS NULL
                       ORDER BY w.updated_at DESC LIMIT 8"""
                )
            ]
            recent = [
                _row(row)
                for row in conn.execute(
                    """SELECT event_key,event_time,operation,target_type,target_id,result
                       FROM v_asa_audit_events ORDER BY COALESCE(event_time,'') DESC LIMIT 12"""
                )
            ]
            return {"ok": True, "counts": counts, "workflows": workflows, "recent_events": recent}
        finally:
            conn.close()

    def jobs(self, *, query: str = "", status: str = "", include_archived: bool = False, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if query:
            clauses.append("(j.title LIKE ? OR c.name LIKE ? OR j.location LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        if status:
            clauses.append("(j.status=? OR j.lifecycle_stage=?)")
            params.extend([status, status])
        if not include_archived:
            clauses.append("COALESCE(j.lifecycle_stage,'') NOT IN ('archived','closed','cancelled')")
            clauses.append("COALESCE(j.status,'') NOT IN ('误归属-已迁移到视源电子','只读快照')")
        where = " AND ".join(clauses)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                f"SELECT count(*) FROM jobs j JOIN clients c ON c.id=j.client_id WHERE {where}", params
            ).fetchone()[0]
            rows = [
                _row(row)
                for row in conn.execute(
                    f"""SELECT j.id,j.title,j.job_code,j.location,j.status,j.lifecycle_stage,j.summary,
                               j.hard_requirements,j.updated_at,c.id client_id,c.name client,
                               d.priority,d.risk,d.stop_condition,
                               count(jc.id) candidate_count,
                               sum(CASE WHEN jc.id IS NOT NULL
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%初筛不通过%'
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%停止%'
                                             AND COALESCE(jc.clean_stage,'') NOT LIKE '%淘汰%'
                                             AND lower(COALESCE(jc.raw_status,'')) NOT IN ('screen_rejected','xsaas_review_stop','stopped','closed','rejected')
                                        THEN 1 ELSE 0 END) active_candidate_count
                          FROM jobs j JOIN clients c ON c.id=j.client_id
                          LEFT JOIN v_job_dashboard d ON d.client=c.name AND d.job=j.title
                          LEFT JOIN job_candidates jc ON jc.job_id=j.id
                         WHERE {where}
                         GROUP BY j.id ORDER BY
                           CASE WHEN d.priority LIKE '%P0-最急%' THEN 0 ELSE 1 END,
                           active_candidate_count DESC,j.updated_at DESC LIMIT ? OFFSET ?""",
                    [*params, min(max(limit, 1), 200), max(offset, 0)],
                )
            ]
            # 筛选模型覆盖巡检：岗位有活跃候选池但无确定性筛选域时给出显式标记，
            # 避免“系统不认识该岗位却静默错筛”再次无感知上线。
            from a_system_agent.candidate_pool_filter import job_filter_domain
            for item in rows:
                domain = job_filter_domain(str(item.get("title") or ""))
                item["filter_domain"] = domain
                item["filter_model_missing"] = domain is None and int(item.get("active_candidate_count") or 0) > 0
            return {"ok": True, "items": rows, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    def candidate_list_card(self, job_id: int, *, bonder: bool = False, filter_mode: str = "") -> dict[str, Any]:
        """重新生成岗位候选名单卡片（刷新静态快照，供前端名单卡\"刷新\"按钮调用）。

        - job 不存在 → 404（LookupError 全局映射）
        - bonder=True 时按固晶/共晶/键合关键词重建优先分组（原卡有该组时前端应传回）
        """
        if filter_mode == "grade_filter":
            from a_system_agent.candidate_pool_filter import filter_job_candidates, format_grade_card, job_filter_domain

            conn = connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT c.name AS client, j.title FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                    (int(job_id),),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                raise LookupError("job not found")
            client = str(row["client"] or "")
            title = str(row["title"] or "")
            domain = job_filter_domain(title)
            if domain not in {"mechanical", "software", "power"}:
                raise ValueError("该岗位暂无可用的严格筛选模型")
            result = filter_job_candidates(str(self.db_path), int(job_id), client=client, domain=domain)
            answer, card = format_grade_card(result, client=client, job_title=title, job_id=int(job_id))
            return {"ok": True, "answer": answer, "card": card}

        from a_system_agent.copilot_handler import _build_candidate_list_card

        answer, card = _build_candidate_list_card(str(self.db_path), int(job_id), "固晶" if bonder else "")
        if not card:
            raise LookupError("job not found")
        return {"ok": True, "answer": answer, "card": card}

    def candidate_subset_list_card(
        self,
        candidate_ids: list[int],
        title: str,
        *,
        groups: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """子集名单卡：把一组指定 job_candidates id 组装成 candidate_list 卡片。

        供精读/评审/去重等"指定一组候选人"场景（例：精读 20 人后"✅ 通过 4 人"）
        出可操作名单卡，替代静态 markdown 表格。与 candidate_list/refresh 同口径：
        纯查询组装——不写库、不建工作流、不走 LLM。

        - candidate_ids 为空 → 409（ValueError）
        - 库中不存在的 id 不报错，在 card.summary.skipped 注明
        - groups 可选分组 [{key,label,candidate_ids,priority?}]；未被任何组覆盖的
          id 自动归入末尾「未分组」；未传 groups 时全部进单一 "subset" 组
        - 卡片与整池 candidate_list 卡同 schema（type/title/context/summary/groups），
          额外带 subset=true 标记，前端据此隐藏"刷新"按钮（刷新语义只对整池卡成立）
        """
        requested: list[int] = []
        for raw in candidate_ids or []:
            cid = int(raw)
            if cid <= 0:
                raise ValueError("candidate_ids 必须全部是正整数（job_candidates.id）")
            if cid not in requested:
                requested.append(cid)
        if not requested:
            raise ValueError("candidate_ids 不能为空")
        text_title = str(title or "").strip()
        if not text_title:
            raise ValueError("title 不能为空")

        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT jc.id AS jc_id, p.display_name, p.current_company, p.current_title,
                       jc.clean_stage, jc.flow_bucket
                FROM job_candidates jc
                LEFT JOIN people p ON p.id = jc.person_id
                WHERE jc.id IN (%s)
                """
                % ",".join("?" * len(requested)),
                requested,
            ).fetchall()
        finally:
            conn.close()

        by_id = {int(r["jc_id"]): r for r in rows}
        skipped = [cid for cid in requested if cid not in by_id]

        def to_candidate(cid: int) -> dict[str, Any]:
            r = by_id[cid]
            return {
                "id": cid,
                "name": str(r["display_name"] or "未知"),
                "company": str(r["current_company"] or ""),
                "title": str(r["current_title"] or ""),
                "stage": str(r["clean_stage"] or ""),
                "flow_bucket": str(r["flow_bucket"] or ""),
            }

        card_groups: list[dict[str, Any]] = []
        covered: set[int] = set()
        for group in groups or []:
            gids = [int(i) for i in (group.get("candidate_ids") or []) if int(i) in by_id]
            covered.update(gids)
            card_groups.append({
                "key": str(group.get("key") or f"group{len(card_groups) + 1}"),
                "label": str(group.get("label") or "未命名分组"),
                "priority": bool(group.get("priority", False)),
                "candidates": [to_candidate(cid) for cid in gids],
            })
        if card_groups:
            leftover = [cid for cid in requested if cid in by_id and cid not in covered]
            if leftover:
                card_groups.append({
                    "key": "ungrouped", "label": "未分组",
                    "priority": False, "candidates": [to_candidate(cid) for cid in leftover],
                })
        else:
            card_groups.append({
                "key": "subset", "label": text_title,
                "priority": False,
                "candidates": [to_candidate(cid) for cid in requested if cid in by_id],
            })

        loaded = sum(len(g["candidates"]) for g in card_groups)
        _STOP_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")
        stopped = sum(
            1 for g in card_groups for c in g["candidates"]
            if any(k in c["stage"] for k in _STOP_TOKENS)
        )
        card = {
            "type": "candidate_list",
            "title": text_title,
            "context": {"type": "job", "id": int(context["id"])} if context else None,
            "summary": {
                "total": loaded, "active": loaded - stopped, "stopped": stopped,
                "requested": len(requested), "skipped": skipped,
            },
            "groups": card_groups,
            "subset": True,
        }

        def fmt(c: dict[str, Any]) -> str:
            parts = [c["name"]]
            if c["company"]:
                parts.append(c["company"])
            if c["title"]:
                parts.append(c["title"])
            label = " | ".join(dict.fromkeys(parts))
            return f"- {label}（{c['stage']}）" if c["stage"] else f"- {label}"

        lines = [f"## {text_title}"]
        head = f"共 {loaded} 人"
        if skipped:
            head += f"；{len(skipped)} 个 ID 在库中不存在已跳过（{', '.join(str(i) for i in skipped)}）"
        lines.append(head + "。\n")
        for group in card_groups:
            candidates = group["candidates"]
            if not candidates:
                continue
            lines.append(f"### {group['label']}（{len(candidates)} 人）")
            lines.extend(fmt(c) for c in candidates[:15])
            if len(candidates) > 15:
                lines.append(f"- …等 {len(candidates)} 人，完整名单见卡片")
            lines.append("")
        return {"ok": True, "answer": "\n".join(lines).rstrip(), "card": card}

    def job(self, job_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            base = conn.execute(
                """
                SELECT j.*,c.name AS client,
                       m.priority,m.risk,m.stop_condition,m.a_count,m.b_count,m.p0_count,m.p1_count,
                       m.published_count,m.under_review_count,m.contacted_count,m.pending_followup_count,
                       m.next_keywords_json,m.target_companies_json,m.exclude_terms_json,m.data_gap
                FROM jobs j JOIN clients c ON c.id=j.client_id
                LEFT JOIN job_pipeline_metrics m ON m.id=(
                    SELECT m2.id FROM job_pipeline_metrics m2 WHERE m2.job_id=j.id ORDER BY m2.id DESC LIMIT 1
                )
                WHERE j.id=?
                """,
                (int(job_id),),
            ).fetchone()
            if not base:
                raise LookupError("job not found")
            item = _row(base)
            for source, target in (
                ("next_keywords_json", "next_keywords"),
                ("target_companies_json", "metric_target_companies"),
                ("exclude_terms_json", "exclude_terms"),
            ):
                item[target] = json_value(item.pop(source, "[]"), [])

            position = conn.execute(
                "SELECT * FROM positions WHERE client=? AND title=? ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1",
                (item["client"], item["title"]),
            ).fetchone()
            item["position"] = _row(position)
            profile = conn.execute(
                "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1",
                (item["client"], item["title"]),
            ).fetchone()
            profile_item = _row(profile)
            for field in (
                "hard_requirements_json", "ability_keywords_json", "target_companies_json",
                "exclusion_tags_json", "search_keywords_json", "source_position_ids_json",
                "soft_preferences_json", "pitch_points_json", "risk_points_json",
            ):
                if field in profile_item:
                    profile_item[field.removesuffix("_json")] = json_value(profile_item.pop(field), [])
            item["profile"] = profile_item

            candidate_rows = conn.execute(
                """
                SELECT jc.id,p.id AS person_id,p.display_name AS name,p.current_company,p.current_title,
                       p.city,p.education,p.experience,jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                       COALESCE(sp.source_type,CASE WHEN jc.source_candidate_id IS NOT NULL THEN 'liepin' END,'talent_pool') AS source_type
                FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                LEFT JOIN source_profiles sp ON sp.id=(
                    SELECT sp2.id FROM source_profiles sp2 WHERE sp2.person_id=p.id ORDER BY sp2.id DESC LIMIT 1
                )
                WHERE jc.job_id=? ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC
                """,
                (int(job_id),),
            ).fetchall()
            candidates = []
            for row in candidate_rows:
                candidate = _row(row)
                candidate["is_stopped"] = _is_stopped(candidate.get("clean_stage"), candidate.get("raw_status"))
                candidates.append(candidate)
            item["candidates"] = candidates
            item["funnel"] = {
                "total": len(candidates),
                "active": sum(not candidate["is_stopped"] for candidate in candidates),
                "stopped": sum(candidate["is_stopped"] for candidate in candidates),
                "contacted": sum(
                    any(token in str(candidate.get("clean_stage") or "") for token in ("已触达", "已联系", "已沟通", "已推荐", "面试", "Offer"))
                    for candidate in candidates if not candidate["is_stopped"]
                ),
                "recommended": sum(
                    any(token in str(candidate.get("clean_stage") or "") for token in ("已推荐", "客户", "面试", "Offer"))
                    for candidate in candidates if not candidate["is_stopped"]
                ),
            }
            stages: dict[str, int] = {}
            for candidate in candidates:
                stage = str(candidate.get("clean_stage") or candidate.get("flow_bucket") or "待复核")
                stages[stage] = stages.get(stage, 0) + 1
            item["stages"] = [
                {"stage": stage, "count": count}
                for stage, count in sorted(stages.items(), key=lambda pair: (-pair[1], pair[0]))
            ]
            item["events"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,event_type,event_status,event_time,summary,job_candidate_id,person_id
                    FROM candidate_events WHERE job_id=?
                    ORDER BY COALESCE(event_time,'') DESC,id DESC LIMIT 60
                    """,
                    (int(job_id),),
                ).fetchall()
            ]
            item["search_experiments"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,channel,query,result_count,viewed_count,extracted_count,recommended_count,
                           reply_count,positive_reply_count,noise_notes,status,run_time,updated_at
                    FROM search_experiments WHERE client=? AND position=?
                    ORDER BY COALESCE(updated_at,run_time,created_at) DESC,id DESC LIMIT 30
                    """,
                    (item["client"], item["title"]),
                ).fetchall()
            ]
            latest_strategy = conn.execute(
                """
                SELECT a.artifact_id,a.workflow_id,a.metadata_json,a.created_at,
                       w.status AS workflow_status,g.context_json
                FROM agent_artifacts a
                JOIN agent_workflows w ON w.workflow_id=a.workflow_id
                JOIN agent_goals g ON g.goal_id=a.goal_id
                WHERE a.artifact_type='search_strategy'
                  AND g.context_type='job' AND g.context_id=?
                  AND w.status NOT IN ('cancelled','superseded','archived')
                ORDER BY datetime(a.created_at) DESC,a.id DESC
                LIMIT 1
                """,
                (int(job_id),),
            ).fetchone()
            item["latest_effective_strategy"] = (
                _public_effective_strategy(json_value(latest_strategy["metadata_json"], {}), latest_strategy)
                if latest_strategy
                else None
            )
            item["followups"] = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT id,job_candidate_id,candidate_name,candidate_company,task_type,priority,due_at,status,reason,updated_at
                    FROM followup_tasks WHERE client=? AND position=? AND COALESCE(status,'open') NOT IN ('closed','completed','done')
                    ORDER BY COALESCE(due_at,'9999-12-31'),priority,id DESC LIMIT 40
                    """,
                    (item["client"], item["title"]),
                ).fetchall()
            ]
            # S4-2：客户画像挂载（PRD §3.1）。只注入白名单六类（赛道/卖点/面试流程/
            # 用人偏好/目标池/注意事项）；知识库缺失或异常一律降级为未挂载，绝不影响岗位详情。
            try:
                match, _trace = kb_consumption.match_client_profile(item.get("client"))
                item["client_profile"] = (
                    {
                        "matched": True,
                        "name": match["name"],
                        "rule": match["rule"],
                        "needs_confirmation": match["needs_confirmation"],
                        "context": kb_consumption.profile_context(match["profile"]),
                    }
                    if match
                    else {"matched": False}
                )
            except Exception:
                item["client_profile"] = {"matched": False}
            return {"ok": True, "job": item}
        finally:
            conn.close()

    def candidates(
        self, *, query: str = "", job_id: int | None = None, stage: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if query:
            clauses.append("(p.display_name LIKE ? OR p.current_company LIKE ? OR p.current_title LIKE ? OR j.title LIKE ?)")
            params.extend([f"%{query}%"] * 4)
        if job_id:
            clauses.append("jc.job_id=?")
            params.append(job_id)
        if stage:
            clauses.append("(jc.clean_stage=? OR jc.flow_bucket=?)")
            params.extend([stage, stage])
        where = " AND ".join(clauses)
        conn = connect(self.db_path)
        try:
            total = conn.execute(
                f"""SELECT count(*) FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                    LEFT JOIN jobs j ON j.id=jc.job_id WHERE {where}""", params
            ).fetchone()[0]
            rows = [
                _row(row)
                for row in conn.execute(
                    f"""SELECT jc.id,p.id person_id,p.display_name name,p.current_company,p.current_title,
                               p.city,p.education,p.experience,j.id job_id,j.title job,c.name client,
                               jc.clean_stage,jc.flow_bucket,jc.raw_status,jc.updated_at,
                               COALESCE(sp.source_type,CASE WHEN jc.source_candidate_id IS NOT NULL THEN 'liepin' END,'talent_pool') source_type
                          FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                          LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                          LEFT JOIN source_profiles sp ON sp.id=(SELECT sp2.id FROM source_profiles sp2 WHERE sp2.person_id=p.id ORDER BY sp2.id DESC LIMIT 1)
                         WHERE {where} ORDER BY COALESCE(jc.updated_at,'') DESC,jc.id DESC LIMIT ? OFFSET ?""",
                    [*params, min(max(limit, 1), 200), max(offset, 0)],
                )
            ]
            return {"ok": True, "items": rows, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()

    def candidate(self, candidate_id: int) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            base = conn.execute(
                """SELECT jc.*,p.display_name name,p.current_company,p.current_title,p.city,p.education,p.experience,
                          j.title job,c.name client,
                          legacy.skills AS legacy_profile_text,legacy.notes AS legacy_notes,
                          legacy.source AS legacy_source,legacy.xsaas_id AS legacy_xsaas_id
                     FROM job_candidates jc JOIN people p ON p.id=jc.person_id
                     LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                     LEFT JOIN candidates legacy ON CAST(legacy.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
                    WHERE jc.id=?""",
                (candidate_id,),
            ).fetchone()
            if not base:
                raise LookupError("candidate not found")
            item = _row(base)
            profiles = [_row(row) for row in conn.execute(
                "SELECT * FROM source_profiles WHERE person_id=? ORDER BY COALESCE(source_date,''),id DESC", (item["person_id"],)
            )]
            links = [_row(row) for row in conn.execute(
                "SELECT source_system,source_entity_type,source_entity_id,source_url,metadata_json FROM entity_source_links WHERE canonical_type='person' AND canonical_id=? ORDER BY id DESC",
                (str(item["person_id"]),),
            )]
            for link in links:
                link["metadata"] = json_value(link.pop("metadata_json"), {})
            known_urls = {str(link.get("source_url") or "") for link in links}
            event_sources = conn.execute(
                """
                SELECT source_table,source_id,raw_json
                  FROM candidate_events
                 WHERE job_candidate_id=? OR person_id=?
                 ORDER BY COALESCE(event_time,'') DESC,id DESC
                """,
                (candidate_id, item["person_id"]),
            ).fetchall()
            event_profile_payloads: list[dict[str, Any]] = []
            for source in event_sources:
                raw = json_value(source["raw_json"], {})
                if not isinstance(raw, dict):
                    raw = {}
                if any(raw.get(key) for key in ("full_text", "profile_text", "candidate_profile_text", "content")):
                    event_profile_payloads.append(raw)
                source_url = str(
                    raw.get("source_url")
                    or raw.get("resume_url")
                    or (source["source_id"] if str(source["source_id"] or "").startswith("http") else "")
                ).strip()
                if not source_url.startswith("http") or source_url in known_urls:
                    continue
                source_hint = f"{source['source_table'] or ''} {source_url}".lower()
                source_system = "xsaas" if "xsaas" in source_hint or "x-saas" in source_hint else "liepin"
                links.append(
                    {
                        "source_system": source_system,
                        "source_entity_type": "external_profile",
                        "source_entity_id": source_url,
                        "source_url": source_url,
                        "metadata": {"resolved_from": "candidate_events"},
                    }
                )
                known_urls.add(source_url)
            resume: dict[str, Any] = {}
            for profile in profiles:
                raw = json_value(profile.get("raw_json"), {})
                if len(str(raw.get("full_text") or raw.get("profile_text") or "")) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
                    resume = raw
                source_url = str(raw.get("source_url") or raw.get("resume_url") or "").strip()
                source_type = str(profile.get("source_type") or "").strip().lower()
                source_candidate_id = str(profile.get("source_candidate_id") or "").strip()
                if not source_url and source_candidate_id:
                    if source_type == "liepin":
                        source_url = (
                            "https://h.liepin.com/resume/showresumedetail/"
                            f"?showsearchfeedback=1&res_id_encode={source_candidate_id}"
                        )
                    elif source_type in {"xsaas", "x-saas"}:
                        source_url = f"https://headhunt.x-saas.com.cn/#/app/candidate/info/{source_candidate_id}"
                if source_url.startswith("http") and source_url not in known_urls:
                    links.append(
                        {
                            "source_system": "xsaas" if source_type in {"xsaas", "x-saas"} else "liepin",
                            "source_entity_type": "source_profile",
                            "source_entity_id": source_candidate_id or source_url,
                            "source_url": source_url,
                            "metadata": {"resolved_from": "source_profiles"},
                        }
                    )
                    known_urls.add(source_url)
            for raw in event_profile_payloads:
                candidate_text = str(
                    raw.get("full_text")
                    or raw.get("profile_text")
                    or raw.get("candidate_profile_text")
                    or raw.get("content")
                    or ""
                )
                current_text = str(resume.get("full_text") or resume.get("profile_text") or "")
                if len(candidate_text) > len(current_text):
                    resume = {
                        **raw,
                        "profile_text": raw.get("profile_text") or raw.get("candidate_profile_text") or candidate_text,
                        "full_text": raw.get("full_text") or candidate_text,
                    }
            legacy_profile_text = str(item.get("legacy_profile_text") or "").strip()
            if len(legacy_profile_text) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
                resume = {
                    "profile_text": legacy_profile_text,
                    "full_text": legacy_profile_text,
                    "notes": item.get("legacy_notes") or "",
                    "source": item.get("legacy_source") or "",
                    "backfilled_from": "candidates.skills",
                }
            events = [_row(row) for row in conn.execute(
                "SELECT id,event_type,event_status,event_time,summary,source_table,source_id FROM candidate_events WHERE job_candidate_id=? OR (job_candidate_id IS NULL AND person_id=?) ORDER BY COALESCE(event_time,'') DESC,id DESC LIMIT 100",
                (candidate_id, item["person_id"]),
            )]
            relations = [_row(row) for row in conn.execute(
                """SELECT jc.id,j.id job_id,j.title job,c.name client,jc.clean_stage,jc.flow_bucket,jc.updated_at
                     FROM job_candidates jc LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
                    WHERE jc.person_id=? ORDER BY COALESCE(jc.updated_at,'') DESC""", (item["person_id"],)
            )]
            item["resume"] = {
                "summary": _resume_overview_summary(resume),
                "full_text": resume.get("full_text") or "",
                "work_text": resume.get("work_text") or "",
                "project_text": resume.get("project_text") or "",
                "education_text": resume.get("education_text") or "",
                "raw": resume,
            }
            item["source_links"] = links
            item["events"] = events
            item["job_relations"] = relations
            attributions = [_row(row) for row in conn.execute(
                """
                SELECT sa.*,
                       COALESCE(SUM(sf.weight),0) AS learning_score,
                       COUNT(sf.id) AS signal_count,
                       SUM(sf.signal_type='review_pass') AS review_pass_count,
                       SUM(sf.signal_type='contacted') AS contacted_count,
                       SUM(sf.signal_type='recommended') AS recommended_count,
                       SUM(sf.signal_type IN ('stopped','stopped_neutral')) AS stopped_count,
                       SUM(sf.signal_type IN ('client_approved','client_interview','client_offer','client_hired')) AS client_positive_count,
                       SUM(sf.signal_type='client_rejected') AS client_rejected_count
                FROM agent_sourcing_attributions sa
                LEFT JOIN agent_sourcing_feedback sf ON sf.attribution_id=sa.id
                WHERE sa.job_candidate_id=?
                GROUP BY sa.id ORDER BY sa.id
                """,
                (candidate_id,),
            )]
            item["sourcing_attributions"] = attributions
            sourcing_recalls = []
            recalls_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_candidate_recalls'"
            ).fetchone()
            if recalls_table:
                for row in conn.execute(
                    """
                    SELECT recall_id,run_id,workflow_id,strategy_hash,strategy_artifact_id,
                           strategy_revision,query_plan_hash,query_cell_id,query_family_ids_json,
                           query_provenance_json,channel,source_query,page_number,position_index,
                           fit_score,fit_level,duplicate_state,created_at
                    FROM agent_candidate_recalls
                    WHERE job_candidate_id=?
                    ORDER BY datetime(created_at) DESC,id DESC LIMIT 20
                    """,
                    (candidate_id,),
                ).fetchall():
                    recall = _row(row)
                    recall["query_family_ids"] = json_value(recall.pop("query_family_ids_json", "[]"), [])
                    recall["query_provenance"] = json_value(recall.pop("query_provenance_json", "[]"), [])
                    sourcing_recalls.append(recall)
            item["sourcing_recalls"] = sourcing_recalls
            # Unified source lineage: keep Mapping task provenance alongside channel recalls.
            # This is evidence only; it must not be inferred from task titles or URLs.
            source_lineage: list[dict[str, Any]] = []
            for row in conn.execute(
                """
                SELECT ce.id AS event_id,ce.source_id AS artifact_id,ce.raw_json,
                       aa.workflow_id,aa.title
                  FROM candidate_events ce
             LEFT JOIN agent_artifacts aa
                    ON aa.artifact_id=ce.source_id AND aa.artifact_type='mapping_task'
                 WHERE ce.job_candidate_id=? AND ce.source_table='mapping_task'
                 ORDER BY COALESCE(ce.event_time,'') DESC,ce.id DESC
                """,
                (candidate_id,),
            ).fetchall():
                raw = json_value(row["raw_json"], {})
                source_lineage.append(
                    {
                        "source_type": "mapping",
                        "workflow_id": str(row["workflow_id"] or ""),
                        "artifact_id": str(row["artifact_id"] or ""),
                        "artifact_title": str(row["title"] or ""),
                        "candidate_index": raw.get("candidate_index") if isinstance(raw, dict) else None,
                        "event_id": row["event_id"],
                    }
                )
            for attribution in attributions:
                source_lineage.append(
                    {
                        "source_type": "sourcing",
                        "workflow_id": str(attribution.get("workflow_id") or ""),
                        "strategy_artifact_id": str(attribution.get("strategy_artifact_id") or ""),
                        "channel": str(attribution.get("channel") or ""),
                        "source_query": str(attribution.get("source_query") or ""),
                        "source_round": attribution.get("source_round"),
                    }
                )
            for recall in sourcing_recalls:
                source_lineage.append(
                    {
                        "source_type": "sourcing_recall",
                        "workflow_id": str(recall.get("workflow_id") or ""),
                        "strategy_artifact_id": str(recall.get("strategy_artifact_id") or ""),
                        "channel": str(recall.get("channel") or ""),
                        "source_query": str(recall.get("source_query") or ""),
                        "source_round": recall.get("source_round"),
                        "query_cell_id": recall.get("query_cell_id"),
                    }
                )
            item["source_lineage"] = source_lineage
            report_rows = conn.execute(
                """
                SELECT DISTINCT a.id,a.artifact_id,a.workflow_id,a.artifact_type,a.title,
                                a.mime_type,a.validation_status,a.created_at
                FROM agent_artifacts a
                LEFT JOIN agent_goals g ON g.goal_id=a.goal_id
                WHERE a.artifact_type IN ('matching_report','recommendation_report','resume_document','salary_report')
                  AND (
                    CASE WHEN json_valid(a.metadata_json)
                         THEN CAST(json_extract(a.metadata_json,'$.job_candidate_id') AS INTEGER)
                         ELSE 0 END = ?
                    OR (g.context_type='candidate' AND CAST(g.context_id AS INTEGER)=?)
                  )
                ORDER BY datetime(a.created_at),a.id
                """,
                (candidate_id, candidate_id),
            ).fetchall()
            report_versions: dict[str, int] = {}
            report_artifacts: list[dict[str, Any]] = []
            for row in report_rows:
                report = _row(row)
                artifact_type = str(report.get("artifact_type") or "")
                report_versions[artifact_type] = report_versions.get(artifact_type, 0) + 1
                report["version"] = report_versions[artifact_type]
                report_artifacts.append(report)
            item["report_artifacts"] = list(reversed(report_artifacts))
            # 版本化推荐包摘要列表（顾问确认推荐后生成；无确认推荐为空列表，前端不渲染区块）。
            item["recommendation_packages"] = [
                self._package_brief(row) | {"feedback_count": int(row["feedback_count"])}
                for row in conn.execute(
                    """SELECT p.*,
                              (SELECT COUNT(*) FROM recommendation_package_feedback f WHERE f.package_id=p.package_id) AS feedback_count
                         FROM recommendation_packages p
                        WHERE p.job_candidate_id=? ORDER BY p.version DESC""",
                    (candidate_id,),
                ).fetchall()
            ]
            item["is_stopped"] = _is_stopped(item.get("clean_stage"), item.get("raw_status"))
            # stop_reason 保留旧语义（备注文本）；R10 新增 stop_reason_code/label 枚举视图。
            stop_reason_code = str(item.get("stop_reason") or "").strip()
            item["stop_reason_code"] = stop_reason_code if item["is_stopped"] else ""
            item["stop_reason_label"] = STOP_REASON_LABELS.get(stop_reason_code, "") if item["is_stopped"] else ""
            item["stop_reason"] = item.get("clean_reason") if item["is_stopped"] else ""
            for internal_key in ("legacy_profile_text", "legacy_notes", "legacy_source", "legacy_xsaas_id"):
                item.pop(internal_key, None)
            return {"ok": True, "candidate": item}
        finally:
            conn.close()

    def audit_events(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            items = [_row(row) for row in conn.execute(
                "SELECT * FROM v_asa_audit_events ORDER BY COALESCE(event_time,'') DESC LIMIT ? OFFSET ?",
                (min(max(limit, 1), 500), max(offset, 0)),
            )]
            return {"ok": True, "items": items}
        finally:
            conn.close()

    def list_approvals(self, status: str = "pending", limit: int = 100) -> dict[str, Any]:
        """只读审批列表：默认只返回 pending；status 传空串表示不按状态过滤。"""
        conn = connect(self.db_path)
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if status.strip():
                clauses.append("a.status=?")
                params.append(status.strip())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            items = [_row(row) for row in conn.execute(
                f"""SELECT a.approval_id,a.workflow_id,a.goal_id,a.risk_level,a.title,a.status,a.created_at,
                           g.title AS goal_title
                      FROM agent_approvals a
                      LEFT JOIN agent_goals g ON g.goal_id=a.goal_id
                      {where}
                     ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                (*params, min(max(int(limit), 1), 500)),
            )]
            return {"ok": True, "items": items}
        finally:
            conn.close()

    def model_audit(self, limit: int = 50, operation: str = "", status: str = "") -> dict[str, Any]:
        """Recent LLM calls with privacy-safe previews and a compact 24-hour summary."""
        conn = connect(self.db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_model_calls'"
            ).fetchone()
            if not exists:
                return {"ok": True, "items": [], "summary": {"total": 0, "failed": 0, "fallback": 0, "avg_duration_ms": 0}}
            clauses: list[str] = []
            params: list[Any] = []
            if operation.strip():
                clauses.append("operation=?")
                params.append(operation.strip())
            if status.strip():
                clauses.append("status=?")
                params.append(status.strip())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""SELECT call_id,operation,provider,model,status,validation_status,fallback_used,
                           duration_ms,input_tokens,output_tokens,request_hash,request_preview,
                           response_preview,error,created_at,finished_at
                      FROM agent_model_calls {where}
                     ORDER BY id DESC LIMIT ?""",
                (*params, min(max(int(limit), 1), 200)),
            ).fetchall()
            summary = conn.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed,
                          COALESCE(SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END),0) AS fallback,
                          CAST(COALESCE(AVG(CASE WHEN status!='running' THEN duration_ms END),0) AS INTEGER) AS avg_duration_ms
                     FROM agent_model_calls
                    WHERE created_at>=datetime('now','localtime','-1 day')"""
            ).fetchone()
            return {"ok": True, "items": [_row(row) for row in rows], "summary": _row(summary)}
        finally:
            conn.close()

    def stop_reasons_summary(self) -> dict[str, Any]:
        """停止原因统计（PRD 阶段 4 R10）：8 枚举计数 + 中文标签。

        停止判定与 _is_stopped 同口径；stop_reason 为 NULL/空/未知值的
        历史行单独归入"未标注"，不并入任何枚举。
        """
        stage_clause = " OR ".join("clean_stage LIKE ?" for _ in STOP_STAGES)
        status_clause = ",".join("?" for _ in STOP_STATUSES)
        where = f"({stage_clause}) OR lower(COALESCE(raw_status,'')) IN ({status_clause})"
        params: list[Any] = [f"%{token}%" for token in STOP_STAGES] + sorted(STOP_STATUSES)
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                f"""SELECT COALESCE(NULLIF(trim(stop_reason),''),'') AS code, COUNT(*) AS n
                      FROM job_candidates WHERE {where} GROUP BY code""",
                params,
            ).fetchall()
        finally:
            conn.close()
        counts = {str(row["code"]): int(row["n"]) for row in rows}
        items = [
            {"reason": code, "label": label, "count": counts.pop(code, 0)}
            for code, label in STOP_REASON_LABELS.items()
        ]
        unlabeled = sum(counts.values())
        return {
            "ok": True,
            "total_stopped": unlabeled + sum(item["count"] for item in items),
            "items": items,
            "unlabeled": {"label": UNLABELED_STOP_REASON_LABEL, "count": unlabeled},
        }

    # ------------------------------------------------------------------
    # S7-1：人才流动雷达（路由层薄封装，业务在 AgentService/a_system_agent.radar_scan）
    # ------------------------------------------------------------------

    def create_radar_scan(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_radar_scan()
        raise RuntimeError("workflow service unavailable")

    def get_latest_radar_scan(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_latest_radar_scan()
        raise RuntimeError("workflow service unavailable")

    # ------------------------------------------------------------------
    # S7-2：雷达联动（路由层薄封装，业务在 AgentService）
    # ------------------------------------------------------------------

    def start_mapping_from_radar(self, company: str, job_id: int) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.start_mapping_from_radar(company, job_id)
        raise RuntimeError("workflow service unavailable")

    def activate_radar_company(self, company: str) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.activate_radar_company(company)
        raise RuntimeError("workflow service unavailable")

    # ------------------------------------------------------------------
    # S7-3：雷达周报（路由层薄封装，业务在 AgentService/a_system_agent.radar_weekly）
    # ------------------------------------------------------------------

    def create_radar_weekly_report(self, *, push_copilot: bool = True) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.create_radar_weekly_report(push_copilot=push_copilot)
        raise RuntimeError("workflow service unavailable")

    def get_latest_radar_weekly_report(self) -> dict[str, Any]:
        if self.agent_service:
            return self.agent_service.get_latest_radar_weekly_report()
        raise RuntimeError("workflow service unavailable")

    # ------------------------------------------------------------------
    # 岗位自动周报（三期驾驶舱缺口）：确定性组装（asa_core.job_weekly_report），
    # 不依赖 LLM；同周幂等 upsert 版本化 artifact，跨周以上一期漏斗做对比基线。
    # ------------------------------------------------------------------

    def generate_job_weekly_report(self, job_id: int) -> dict[str, Any]:
        from . import job_weekly_report

        with transaction(self.db_path) as conn:
            doc = job_weekly_report.build_job_weekly_report(conn, job_id)
            artifact_id = job_weekly_report.upsert_job_weekly_report(conn, doc)
        return {
            "ok": True,
            "job_id": int(job_id),
            "artifact_id": artifact_id,
            "version": int(doc.get("version") or 1),
            "week_start": doc.get("week_start"),
            "week_end": doc.get("week_end"),
            "report": doc,
        }

    def list_job_weekly_reports(self, job_id: int, limit: int = 12) -> dict[str, Any]:
        from . import job_weekly_report

        conn = connect(self.db_path)
        try:
            return job_weekly_report.list_job_weekly_reports(conn, job_id, limit=limit)
        finally:
            conn.close()
