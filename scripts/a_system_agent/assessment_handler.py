"""Handler extracted from service.py — see _shared.py for shared helpers and constants.

All functions receive 'self' (AgentService instance) as first parameter.
"""

from __future__ import annotations
import hashlib, json, secrets, time
import sys
from datetime import datetime
from typing import Any

from ._shared import (
    _dumps, _loads, _row, _table_exists, _table_columns,
    ASSESSMENT_VERSION, PANEL_VERSION, DECISION_LABELS,
)
from .config import load_config
from .context import build_candidate_context
from .evaluation import compute_evaluation
from .llm import LLMError, PROMPT_VERSION
from .liepin_capture import capture_open_liepin_resumes, resume_matches_identity
from .panel import (
    ROLE_DEFINITIONS,
    fallback_role_review,
    normalize_role_review,
    role_payload,
    synthesize_panel,
)
from .policy import action_decision, is_stopped
from .privacy import sanitize_payload
from .resume_persist import persist_captured_resume, resume_profile_summary
from .schema import ensure_schema
from .scoring import normalize_assessment
from .skills import SkillRegistry, SkillSpec
from .workflow import BUSINESS_OUTCOME_LABELS, classify_business_outcome, sourcing_target_stats

def _current_assessed_candidates(self, job_id: int) -> list[dict[str, Any]]:
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT a.job_candidate_id,
                   p.display_name AS name,
                   p.current_company AS company,
                   p.current_title AS title,
                   p.city,p.education,p.experience,
                   a.fit_score,a.fit_level,a.recommendation,a.confidence,
                   a.next_action,a.created_at AS assessed_at,
                   jc.clean_stage AS current_stage,jc.raw_status AS raw_status,
                   COALESCE((
                       SELECT sp.source_type
                       FROM source_profiles sp
                       WHERE sp.person_id=p.id
                       ORDER BY sp.source_date DESC,sp.id DESC
                       LIMIT 1
                   ),'talent_pool') AS channel
            FROM agent_candidate_assessments a
            JOIN job_candidates jc ON jc.id=a.job_candidate_id
            JOIN people p ON p.id=jc.person_id
            JOIN agent_runs r ON r.run_id=a.run_id
            WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
            ORDER BY a.fit_score DESC,a.job_candidate_id DESC
            """,
            (int(job_id),),
        ).fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()


def generate_candidate_assessment(self, candidate_id: int, job_id: int, *, force: bool = False) -> dict[str, Any]:
    """S6-1：生成/重新生成判人评估（职业轨迹 + 跳槽质量史），落 candidate_assessment artifact。

    404：人选/岗位不存在或不匹配（LookupError）；409：无简历语料、敏感扫描命中、
    模型不可用或输出非法（ValueError/LLMError）。同人同岗幂等：更新原行，as_of 刷新。
    红线：评估只辅助不决策；敏感扫描命中拒写并记扫描日志；证据不过闸不落库。
    """
    from . import candidate_assessment
    from .workflow import _mask_candidate_name

    conn = self._connect()
    try:
        input_hash = candidate_assessment.assessment_input_hash(
            conn,
            candidate_id=int(candidate_id),
            job_id=int(job_id),
            llm=self.llm,
        )
        if not force:
            cached = candidate_assessment.get_assessment(conn, int(candidate_id), int(job_id))
            cached_doc = cached.get("assessment") if cached else None
            if isinstance(cached_doc, dict) and cached_doc.get("input_hash") == input_hash:
                return {
                    "ok": True,
                    "cached": True,
                    "cache_reason": "assessment_inputs_unchanged",
                    "candidate_id": int(candidate_id),
                    "job_id": int(job_id),
                    "artifact_id": cached["artifact_id"],
                    "assessment": cached_doc,
                }
        try:
            doc = candidate_assessment.run_assessment(
                conn,
                candidate_id=int(candidate_id),
                job_id=int(job_id),
                llm=self.llm,
                mask_name=_mask_candidate_name,
                signal_fetcher=self.assessment_signal_fetcher,
            )
        except LLMError as exc:
            raise ValueError(f"判人评估模型不可用或输出非法：{exc}") from exc
        doc["input_hash"] = input_hash
        doc["cache_policy"] = "source_fingerprint_daily_v1"
        artifact_id = candidate_assessment.persist_assessment(conn, doc)
        conn.commit()
        return {
            "ok": True,
            "cached": False,
            "candidate_id": int(candidate_id),
            "job_id": int(job_id),
            "artifact_id": artifact_id,
            "assessment": doc,
        }
    finally:
        conn.close()


def get_candidate_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
    """S6-1：读取同人同岗判人评估；人选/岗位/评估不存在抛 LookupError（404）。"""
    from . import candidate_assessment

    conn = self._connect()
    try:
        relation = conn.execute(
            "SELECT id,job_id FROM job_candidates WHERE id=?", (int(candidate_id),)
        ).fetchone()
        if relation is None:
            raise LookupError(f"人选不存在：{candidate_id}")
        if int(relation["job_id"] or 0) != int(job_id):
            raise LookupError(f"人选 {candidate_id} 不属于岗位 {job_id}")
        payload = candidate_assessment.get_assessment(conn, int(candidate_id), int(job_id))
        # 匹配点分析：附带当前有效的人岗匹配评估（agent_candidate_assessments，只读）。
        # 无 Agent 评估时回退候选人匹配初评（candidate_intelligence：强/弱匹配点 + 评分等级，
        # 覆盖几乎全部人选）；两者皆无 fit=None，前端不渲染该区块；不伪造、不触发新生成。
        fit = None
        fit_row = conn.execute(
            """
            SELECT a.* FROM agent_candidate_assessments a
            JOIN agent_runs r ON r.run_id=a.run_id
            WHERE a.job_candidate_id=? AND a.is_current=1 AND r.status='completed'
            ORDER BY a.id DESC LIMIT 1
            """,
            (int(candidate_id),),
        ).fetchone()
        if fit_row is not None:
            fit = self._assessment_payload(fit_row)
            fit["source"] = "agent_assessment"
        else:
            # candidate_intelligence 由外部 bootstrap 脚本建表（ensure_schema 不建），
            # 未跑 bootstrap 的库缺表时应优雅降级（fit=None），不得 500。
            intel_row = None
            if _table_exists(conn, "candidate_intelligence"):
                intel_row = conn.execute(
                    """
                    SELECT ci.* FROM job_candidates jc
                    JOIN candidate_intelligence ci ON ci.candidate_id = jc.source_candidate_id
                    WHERE jc.id=?
                    ORDER BY CASE WHEN ci.client=jc.raw_client AND ci.position=jc.raw_position THEN 0 ELSE 1 END,
                             datetime(COALESCE(ci.last_evaluated_at, ci.updated_at)) DESC, ci.id DESC LIMIT 1
                    """,
                    (int(candidate_id),),
                ).fetchone()
            if intel_row is not None:
                intel = dict(intel_row)
                fit = {
                    "source": "candidate_intelligence",
                    "fit_score": intel.get("fit_score"),
                    "fit_level": intel.get("fit_level"),
                    "recommendation": intel.get("recommendation_decision"),
                    "recommendation_label": intel.get("recommendation_decision"),
                    "criteria": {},
                    "strengths": _loads(intel.get("strong_matches_json"), []),
                    "gaps": _loads(intel.get("weak_matches_json"), []),
                    "risks": _loads(intel.get("risk_json"), []),
                    "next_action": intel.get("next_action") or "",
                    "created_at": intel.get("last_evaluated_at") or intel.get("updated_at"),
                }
        if payload is None:
            if fit is not None:
                # 有匹配评估但无判人评估：照常返回 200（assessment 缺省），前端只渲染匹配点分析。
                return {"ok": True, "candidate_id": int(candidate_id), "job_id": int(job_id), "fit": fit}
            raise LookupError(f"人选 {candidate_id} 在岗位 {job_id} 下还没有判人评估，请先 POST 生成")
        return {"ok": True, "candidate_id": int(candidate_id), "job_id": int(job_id), "fit": fit, **payload}
    finally:
        conn.close()


def refresh_candidate_fit_assessment(self, candidate_id: int, job_id: int) -> dict[str, Any]:
    """匹配点分析「重新评估匹配」：强制重跑 Agent 人岗匹配评估（同步等待，通常 15-30 秒）。

    404：人选/岗位不存在或不匹配（LookupError）；409：模型输出非法或评估未完成（ValueError）。
    成功返回最新 fit 块，结构同 get_candidate_assessment 的 fit。
    """
    conn = self._connect()
    try:
        relation = conn.execute(
            "SELECT id,job_id FROM job_candidates WHERE id=?", (int(candidate_id),)
        ).fetchone()
        if relation is None:
            raise LookupError(f"人选不存在：{candidate_id}")
        if int(relation["job_id"] or 0) != int(job_id):
            raise LookupError(f"人选 {candidate_id} 不属于岗位 {job_id}")
    finally:
        conn.close()
    run = self.submit_assessment(int(candidate_id), force=True, trigger="asa_app_manual", wait=True)
    if run.get("status") != "completed":
        raise ValueError(f"匹配评估未完成：{run.get('error') or run.get('status') or '未知原因'}")
    return {
        "ok": True,
        "candidate_id": int(candidate_id),
        "job_id": int(job_id),
        "fit": run.get("assessment"),
    }


def update_candidate_assessment_advisor_action(
    self, candidate_id: int, job_id: int, *, action: str, note: str = ""
) -> dict[str, Any]:
    """S6-1b：顾问动作写回（采纳/改判/否决，可附 note）。

    404：人选/岗位不存在或不匹配、尚无评估（LookupError）；409：非法 action（ValueError）。
    只更新 advisor_action/advisor_note/updated_at，version 不 bump；写岗位时间线留痕。
    """
    from . import candidate_assessment

    conn = self._connect()
    try:
        payload = candidate_assessment.apply_advisor_action(
            conn, candidate_id=int(candidate_id), job_id=int(job_id), action=action, note=note
        )
        conn.commit()
        return payload
    finally:
        conn.close()


def assessment_calibration_metrics(self) -> dict[str, Any]:
    """S6-4：采纳率度量（顾问点头率）——按维度×客户聚合采纳/改判/否决率。

    只读；totals 与库内 advisor_action 实际分布一致，数据不足的分组三个率如实 null。
    """
    from . import assessment_calibration

    conn = self._connect()
    try:
        return assessment_calibration.compute_metrics(conn)
    finally:
        conn.close()


def generate_assessment_calibration_report(self) -> dict[str, Any]:
    """S6-4：校准周报（手动触发）——markdown 写 work/calibration/（不进 git）。

    本周改判集中的维度 / 客户口径观察 / 系统性偏差建议；报告为内部留档，不对外输出。
    """
    from . import assessment_calibration

    conn = self._connect()
    try:
        return assessment_calibration.generate_report(conn)
    finally:
        conn.close()



def _skill_job_diagnosis(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    dashboard = self.get_dashboard()
    jobs = dashboard.get("p0_jobs") or []
    if context.get("type") == "job" and context.get("id"):
        jobs = [item for item in jobs if int(item.get("job_id") or 0) == int(context["id"])]
    return {
        "diagnosis": {"jobs": jobs, "funnel": (dashboard.get("analytics") or {}).get("funnel", {})},
        "references": [
            {"type": "job", "id": item.get("job_id"), "label": item.get("job"), "subtitle": item.get("client")}
            for item in jobs[:8]
        ],
        "suggested_actions": [],
    }


def _skill_candidate_assessment(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    job_candidate_id = int(context.get("id") or 0)
    if not job_candidate_id:
        raise ValueError("人选评估 Skill 需要人岗关系 ID")
    state = self.get_candidate_state(job_candidate_id)
    run = None
    if not state.get("assessment"):
        run = self.submit_assessment(
            job_candidate_id,
            trigger="workflow" if inputs.get("workflow_id") else "skill",
            wait=bool(inputs.get("workflow_id")),
        )
        if inputs.get("workflow_id"):
            state = self.get_candidate_state(job_candidate_id)
    return {
        "assessment": state.get("assessment"),
        "run": run,
        "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
        "suggested_actions": [],
    }


def _skill_verification_plan(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    job_candidate_id = int(context.get("id") or 0)
    state = self.get_candidate_state(job_candidate_id)
    assessment = state.get("assessment") or {}
    if not assessment:
        raise ValueError("请先完成人选评估")
    proposals = self.generate_proposals([job_candidate_id], limit=1)
    return {
        "questions": assessment.get("verification_questions") or [],
        "next_action": assessment.get("next_action") or "人工核验关键证据",
        "proposals": proposals.get("proposals") or [],
        "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
        "suggested_actions": [{"type": "open_candidate", "id": job_candidate_id, "label": "开始核验"}],
    }


def _skill_communication_draft(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    job_candidate_id = int(context.get("id") or 0)
    result = self.create_draft(job_candidate_id, str(inputs.get("instructions") or ""))
    return {
        "draft": result.get("draft") or "",
        "references": [{"type": "candidate", "id": job_candidate_id, "label": f"关系 #{job_candidate_id}"}],
        "suggested_actions": [],
        "not_sent": True,
    }


def _skill_liepin_resume_capture(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    job_candidate_id = int(context.get("id") or 0)
    result = self.capture_liepin_resume(job_candidate_id, cdp_port=int(inputs.get("cdp_port") or 9223))
    return {
        **result,
        "references": [
            {
                "type": "candidate", "id": job_candidate_id,
                "label": result["resume"].get("name") or f"关系 #{job_candidate_id}",
                "subtitle": "猎聘完整简历已补充",
            }
        ],
        "suggested_actions": [{"type": "open_candidate", "id": job_candidate_id, "label": "查看更新后的判断"}],
    }


def capture_liepin_resume(self, job_candidate_id: int, *, cdp_port: int = 9223) -> dict[str, Any]:
    if not int(job_candidate_id or 0):
        raise ValueError("从猎聘补全简历需要人岗关系 ID")
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    identity = context.get("identity") or {}
    # Keep the historical service-module injection point working after handler extraction.
    service_module = sys.modules.get("a_system_agent.service")
    capture = getattr(service_module, "capture_open_liepin_resumes", capture_open_liepin_resumes)
    resumes = capture(int(cdp_port))
    matches = [resume for resume in resumes if resume_matches_identity(identity, resume)]
    if not matches:
        visible = "、".join(
            f"{resume.get('name') or '未识别'} / {resume.get('company') or '未识别公司'}"
            for resume in resumes[:5]
        )
        raise ValueError(
            f"已打开的猎聘简历与当前人选不匹配。当前人选：{identity.get('name') or '未识别'}；"
            f"猎聘页面：{visible or '无可读简历'}"
        )
    if len(matches) != 1:
        raise ValueError("有多个猎聘简历页面同时匹配当前人选，请只保留需要补全的详情页")
    resume = matches[0]
    relation = context["relation"]
    position = context["position"]
    conn = self._connect()
    try:
        persisted = persist_captured_resume(
            conn,
            relation=relation,
            position=position,
            identity=identity,
            candidate_id=(context.get("candidate") or {}).get("id"),
            resume=resume,
            job_candidate_id=int(job_candidate_id),
            capture_method="asa_liepin_cdp_read_only",
        )
        conn.commit()
    finally:
        conn.close()
    source_profile_id = persisted["source_profile_id"]
    event_id = persisted["event_id"]
    updated = persisted["profile_updated"]
    summary = persisted["summary"]
    candidate_id = (context.get("candidate") or {}).get("id")
    profile_summary = resume_profile_summary(resume)
    assessment = self.submit_assessment(
        int(job_candidate_id), force=True, trigger="liepin_resume_capture"
    )
    # S8：简历更新后异步刷新该岗位画像（只抽该人事实 + 确定性重算聚合；失败不阻断捕获回执）
    profile_refresh = self.submit_job_profile_refresh(
        int(job_candidate_id), trigger="liepin_resume_capture"
    )
    return {
        "ok": True,
        "message": "猎聘完整简历已写入 ASA，正在重新评估当前人选。",
        "job_profile_refresh": profile_refresh,
        "resume": {
            "resume_id": resume["resume_id"], "name": resume.get("name"),
            "company": resume.get("company"), "title": resume.get("title"),
            "work_chars": len(str(resume.get("work_text") or "")),
            "project_chars": len(str(resume.get("project_text") or "")),
            "education_chars": len(str(resume.get("education_text") or "")),
        },
        "source_profile_id": source_profile_id, "event_id": event_id,
        "updated": updated, "assessment": assessment,
        "candidate_update": {
            "candidate_id": candidate_id,
            "job_candidate_id": int(job_candidate_id),
            "name": resume.get("name") or identity.get("name"),
            "company": resume.get("company") or identity.get("company"),
            "title": resume.get("title") or identity.get("title"),
            "city": resume.get("city") or identity.get("city"),
            "education": resume.get("education") or identity.get("education"),
            "experience": resume.get("experience") or identity.get("experience"),
            "profile_summary": profile_summary,
            "source_url": resume.get("source_url"),
            "event": {
                "id": event_id, "jobCandidateId": int(job_candidate_id),
                "eventType": "resume_profile_captured", "eventStatus": "completed",
                "eventTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": summary, "client": position.get("client"), "job": position.get("job"),
            },
        },
    }


def _snapshot_key(self, context: dict[str, Any]) -> str:
    raw = "|".join(
        [context["snapshot_hash"], PROMPT_VERSION, ASSESSMENT_VERSION, self.llm.model]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stage_shadow_decision(self, assessment: dict[str, Any]) -> dict[str, Any]:
    hard_requirements = (assessment.get("criteria") or {}).get("hard_requirements") or []
    critical_not_met = any(
        item.get("status") == "not_met" and bool(item.get("critical", True))
        for item in hard_requirements
    )
    score = int(assessment.get("fit_score") or 0)
    confidence = float(assessment.get("confidence") or 0)
    coverage = float(assessment.get("evidence_coverage") or 0)
    recommendation = str(assessment.get("recommendation") or "")
    thresholds = self.config["automation"]
    high_score = int(thresholds["high_score"])
    low_score = int(thresholds["low_score"])
    min_confidence = float(thresholds["min_confidence"])
    min_coverage = float(thresholds["min_evidence_coverage"])
    if critical_not_met:
        proposed_stage = "待人工复核"
        rule_code = "critical_gate_not_met"
        reason = "存在关键硬门槛不满足，不自动淘汰，转人工复核。"
    elif score < low_score:
        proposed_stage = "待人工复核"
        rule_code = "low_score_review"
        reason = f"匹配评分 {score} 低于 {low_score}，转人工复核。"
    elif recommendation == "verify_first" or coverage < min_coverage:
        proposed_stage = "待核验"
        rule_code = "evidence_verification"
        reason = f"建议先核验或证据覆盖率 {coverage:.0%} 低于 {min_coverage:.0%}。"
    elif score >= high_score and confidence >= min_confidence and coverage >= min_coverage:
        proposed_stage = "复核通过待联系"
        rule_code = "high_confidence_pass"
        reason = f"评分 {score}、置信度 {confidence:.0%}、证据覆盖 {coverage:.0%} 达到内部推进阈值。"
    else:
        proposed_stage = "待人工复核"
        rule_code = "threshold_review"
        reason = "未同时满足自动推进阈值，保留人工复核。"
    return {
        "mode": "shadow",
        "executed": False,
        "action_type": "internal_stage_recommendation",
        "proposed_stage": proposed_stage,
        "rule_code": rule_code,
        "reason": reason,
    }


def submit_assessment(
    self,
    job_candidate_id: int,
    *,
    force: bool = False,
    trigger: str = "manual",
    wait: bool = False,
    timeout: float = 90,
) -> dict[str, Any]:
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    snapshot_hash = self._snapshot_key(context)
    conn = self._connect()
    try:
        if not force:
            cached = conn.execute(
                """
                SELECT a.*,r.status AS run_status,r.model,r.prompt_version
                FROM agent_candidate_assessments a
                JOIN agent_runs r ON r.run_id=a.run_id
                WHERE a.job_candidate_id=? AND a.snapshot_hash=? AND a.is_current=1
                  AND r.status='completed'
                ORDER BY a.id DESC LIMIT 1
                """,
                (int(job_candidate_id), snapshot_hash),
            ).fetchone()
            if cached:
                return {
                    "ok": True,
                    "cached": True,
                    "run_id": cached["run_id"],
                    "status": "completed",
                    "assessment": self._assessment_payload(cached),
                }
    finally:
        conn.close()
    key = (int(job_candidate_id), snapshot_hash)
    coalesced_run_id = ""
    with self._lock:
        active_run_id = self._active_by_snapshot.get(key)
        if active_run_id:
            coalesced_run_id = active_run_id
        else:
            run_id = f"agent_{int(time.time())}_{secrets.token_hex(6)}"
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO agent_runs
                    (run_id,kind,context_type,context_id,snapshot_hash,status,trigger,model,prompt_version)
                    VALUES (?,?,?,?,?,'queued',?,?,?)
                    """,
                    (
                        run_id,
                        "candidate_assessment",
                        "job_candidate",
                        int(job_candidate_id),
                        snapshot_hash,
                        trigger,
                        self.llm.model,
                        PROMPT_VERSION,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            self._active_by_snapshot[key] = run_id
    if coalesced_run_id:
        payload = self.get_run(coalesced_run_id)
        if wait:
            deadline = time.monotonic() + max(0.1, float(timeout))
            while payload.get("status") in {"queued", "running"} and time.monotonic() < deadline:
                time.sleep(0.05)
                payload = self.get_run(coalesced_run_id)
        payload["coalesced"] = True
        return payload
    if wait:
        self._run_assessment(run_id, context, snapshot_hash, key)
        return self.get_run(run_id)
    future = self.executor.submit(self._run_assessment, run_id, context, snapshot_hash, key)
    self._futures[run_id] = future
    return {"ok": True, "cached": False, "run_id": run_id, "status": "queued"}


def _run_assessment(
    self,
    run_id: str,
    context: dict[str, Any],
    snapshot_hash: str,
    key: tuple[int, str],
) -> None:
    conn = self._connect()
    try:
        assessment_cursor = conn.execute(
            """
            UPDATE agent_runs SET status='running',started_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime') WHERE run_id=?
            """,
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        raw_assessment = self.llm.assess(context["model_context"])
        assessment = normalize_assessment(raw_assessment, context)
        reviewer: dict[str, Any] = {}
        reviewer_used = 0
        if assessment["needs_review"]:
            reviewer_used = 1
            model_context = context["model_context"]
            review_context = {
                "identity": model_context.get("identity", {}),
                "candidate_profile": model_context.get("candidate_profile", {}),
                "position": model_context.get("position", {}),
                "events": (model_context.get("events") or [])[:12],
                "learning_rules": model_context.get("learning_rules", []),
            }
            reviewer = self.llm.review(review_context, raw_assessment)
            if reviewer.get("decision") == "correct" and isinstance(reviewer.get("assessment"), dict):
                assessment = normalize_assessment(reviewer["assessment"], context)
            elif reviewer.get("decision") == "abstain":
                assessment["recommendation"] = "verify_first"
                assessment["confidence"] = min(float(assessment["confidence"]), 0.49)
                assessment["risks"] = list(dict.fromkeys(["审校器无法确认当前判断", *assessment["risks"]]))
        latest_context = build_candidate_context(self.db_path, int(context["relation"]["job_candidate_id"]))
        latest_snapshot = self._snapshot_key(latest_context)
        if latest_snapshot != snapshot_hash:
            self._finish_run(
                run_id,
                "stale",
                error="评估期间候选人或岗位证据已变化，旧结果未晋升为当前判断",
                reviewer_used=reviewer_used,
            )
            return
        self._persist_assessment(run_id, context, snapshot_hash, assessment, reviewer)
        self._finish_run(run_id, "completed", reviewer_used=reviewer_used)
    except Exception as exc:
        self._finish_run(run_id, "failed", error=str(exc)[:1000])
    finally:
        with self._lock:
            self._active_by_snapshot.pop(key, None)
            self._futures.pop(run_id, None)


def submit_panel_review(
    self,
    job_candidate_id: int,
    *,
    force: bool = False,
    use_model: bool = True,
    trigger: str = "manual_panel",
    wait: bool = False,
    timeout: float = 120,
) -> dict[str, Any]:
    job_candidate_id = int(job_candidate_id)
    context = build_candidate_context(self.db_path, job_candidate_id)
    evidence_snapshot = self._snapshot_key(context)
    conn = self._connect()
    try:
        assessment_row = conn.execute(
            """
            SELECT * FROM agent_candidate_assessments
            WHERE job_candidate_id=? AND is_current=1
            ORDER BY id DESC LIMIT 1
            """,
            (job_candidate_id,),
        ).fetchone()
        if assessment_row is None:
            raise ValueError("请先完成当前人选的 Agent 评估")
        if assessment_row["snapshot_hash"] != evidence_snapshot:
            raise ValueError("当前判断已过期，请先重新评估")
        assessment = self._assessment_payload(assessment_row)
        if not force:
            cached = conn.execute(
                """
                SELECT * FROM agent_review_panels
                WHERE job_candidate_id=? AND assessment_id=? AND snapshot_hash=?
                  AND status='completed' AND is_current=1
                ORDER BY id DESC LIMIT 1
                """,
                (job_candidate_id, assessment_row["id"], evidence_snapshot),
            ).fetchone()
            if cached:
                return {
                    "ok": True,
                    "cached": True,
                    "run_id": cached["run_id"],
                    "status": "completed",
                    "panel": self._panel_payload(conn, cached),
                }
    finally:
        conn.close()

    panel_key_raw = "|".join(
        [evidence_snapshot, str(assessment["id"]), PANEL_VERSION, self.llm.model, str(bool(use_model))]
    )
    panel_snapshot = hashlib.sha256(panel_key_raw.encode("utf-8")).hexdigest()
    active_key = (job_candidate_id, panel_snapshot)
    with self._lock:
        active_run_id = self._active_panel_by_snapshot.get(active_key)
        if active_run_id:
            payload = self.get_run(active_run_id)
            payload["coalesced"] = True
            return payload
        run_id = f"panel_{int(time.time())}_{secrets.token_hex(6)}"
        panel_id = f"review_{int(time.time())}_{secrets.token_hex(6)}"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO agent_runs
                (run_id,kind,context_type,context_id,snapshot_hash,status,trigger,model,prompt_version)
                VALUES (?,'candidate_panel_review','job_candidate',?,?,'queued',?,?,?)
                """,
                (
                    run_id,
                    job_candidate_id,
                    panel_snapshot,
                    trigger,
                    self.llm.model,
                    PANEL_VERSION,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_review_panels
                (panel_id,run_id,job_candidate_id,assessment_id,snapshot_hash,status,model_mode,is_current)
                VALUES (?,?,?,?,?,'queued',?,0)
                """,
                (
                    panel_id,
                    run_id,
                    job_candidate_id,
                    assessment["id"],
                    evidence_snapshot,
                    "hybrid" if use_model else "rules",
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._active_panel_by_snapshot[active_key] = run_id
        future = self.executor.submit(
            self._run_panel_review,
            run_id,
            panel_id,
            context,
            assessment,
            evidence_snapshot,
            active_key,
            bool(use_model),
        )
        self._futures[run_id] = future
    if wait:
        future.result(timeout=timeout)
        return self.get_run(run_id)
    return {
        "ok": True,
        "cached": False,
        "run_id": run_id,
        "panel_id": panel_id,
        "status": "queued",
    }


def _run_panel_review(
    self,
    run_id: str,
    panel_id: str,
    context: dict[str, Any],
    assessment: dict[str, Any],
    evidence_snapshot: str,
    active_key: tuple[int, str],
    use_model: bool,
) -> None:
    conn = self._connect()
    try:
        conn.execute(
            "UPDATE agent_runs SET status='running',started_at=datetime('now','localtime'),updated_at=datetime('now','localtime') WHERE run_id=?",
            (run_id,),
        )
        conn.execute(
            "UPDATE agent_review_panels SET status='running' WHERE panel_id=?",
            (panel_id,),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        reviews: list[dict[str, Any]] = []
        model_enabled = bool(use_model)
        model_error = ""
        for role in ROLE_DEFINITIONS:
            source = "rules"
            error = model_error
            review = fallback_role_review(context, assessment, role)
            if model_enabled:
                try:
                    raw = self.llm.role_review(role, role_payload(context, assessment, role))
                    review = normalize_role_review(raw, role)
                    source = "model"
                    error = ""
                except Exception as exc:
                    model_error = str(exc)[:1000]
                    error = model_error
                    model_enabled = False
            review.update(
                {
                    "source": source,
                    "model": self.llm.model if source == "model" else "rules-v2",
                    "error": error,
                }
            )
            reviews.append(review)

        job_candidate_id = int(context["relation"]["job_candidate_id"])
        latest_context = build_candidate_context(self.db_path, job_candidate_id)
        latest_snapshot = self._snapshot_key(latest_context)
        conn = self._connect()
        try:
            current_assessment = conn.execute(
                """
                SELECT id,snapshot_hash FROM agent_candidate_assessments
                WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1
                """,
                (job_candidate_id,),
            ).fetchone()
        finally:
            conn.close()
        if (
            latest_snapshot != evidence_snapshot
            or current_assessment is None
            or int(current_assessment["id"]) != int(assessment["id"])
        ):
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE agent_review_panels SET status='stale',completed_at=datetime('now','localtime') WHERE panel_id=?",
                    (panel_id,),
                )
                conn.commit()
            finally:
                conn.close()
            self._finish_run(
                run_id,
                "stale",
                error="会审期间候选人、岗位或首轮判断已变化",
            )
            return

        synthesis = synthesize_panel(reviews, assessment, stopped=is_stopped(latest_context))
        source_set = {review["source"] for review in reviews}
        model_mode = "model" if source_set == {"model"} else "rules" if source_set == {"rules"} else "hybrid"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE agent_review_panels SET is_current=0 WHERE job_candidate_id=? AND is_current=1",
                (job_candidate_id,),
            )
            conn.execute(
                """
                UPDATE agent_review_panels
                SET status='completed',model_mode=?,synthesis_json=?,is_current=1,
                    completed_at=datetime('now','localtime')
                WHERE panel_id=?
                """,
                (model_mode, _dumps(synthesis), panel_id),
            )
            for review in reviews:
                conn.execute(
                    """
                    INSERT INTO agent_role_reviews
                    (panel_id,role,verdict,confidence,findings_json,questions_json,
                     recommendation,source,model,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        panel_id,
                        review["role"],
                        review["verdict"],
                        review["confidence"],
                        _dumps(review["findings"]),
                        _dumps(review["questions"]),
                        review["recommendation"],
                        review["source"],
                        review["model"],
                        review["error"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._finish_run(run_id, "completed", reviewer_used=1)
    except Exception as exc:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE agent_review_panels SET status='failed',completed_at=datetime('now','localtime') WHERE panel_id=?",
                (panel_id,),
            )
            conn.commit()
        finally:
            conn.close()
        self._finish_run(run_id, "failed", error=str(exc)[:1000])
    finally:
        with self._lock:
            self._active_panel_by_snapshot.pop(active_key, None)
            self._futures.pop(run_id, None)


def _finish_run(
    self,
    run_id: str,
    status: str,
    *,
    error: str = "",
    reviewer_used: int = 0,
) -> None:
    conn = self._connect()
    try:
        conn.execute(
            """
            UPDATE agent_runs SET status=?,error=?,reviewer_used=?,
                finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
            WHERE run_id=?
            """,
            (status, error, reviewer_used, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_assessment(
    self,
    run_id: str,
    context: dict[str, Any],
    snapshot_hash: str,
    assessment: dict[str, Any],
    reviewer: dict[str, Any],
) -> None:
    relation = context["relation"]
    identity = context["identity"]
    position = context["position"]
    candidate = context.get("candidate", {})
    policy = {
        "stopped": is_stopped(context),
        "save_assessment": action_decision("save_assessment", context),
        "resume_review": action_decision("resume_review", context),
        "outreach": action_decision("outreach", context),
    }
    conn = self._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE agent_candidate_assessments SET is_current=0 WHERE job_candidate_id=? AND is_current=1",
            (relation["job_candidate_id"],),
        )
        assessment_cursor = conn.execute(
            """
            INSERT INTO agent_candidate_assessments
            (run_id,job_candidate_id,candidate_id,person_id,job_id,client,job,snapshot_hash,
             assessment_version,fit_score,fit_level,recommendation,confidence,evidence_coverage,
             criteria_json,strengths_json,gaps_json,risks_json,verification_questions_json,
             next_action,outreach_angle,citations_json,policy_json,reviewer_json,is_current)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (
                run_id,
                relation["job_candidate_id"],
                candidate.get("id"),
                relation["person_id"],
                relation.get("job_id"),
                position.get("client"),
                position.get("job"),
                snapshot_hash,
                ASSESSMENT_VERSION,
                assessment["fit_score"],
                assessment["fit_level"],
                assessment["recommendation"],
                assessment["confidence"],
                assessment["evidence_coverage"],
                _dumps(assessment["criteria"]),
                _dumps(assessment["strengths"]),
                _dumps(assessment["gaps"]),
                _dumps(assessment["risks"]),
                _dumps(assessment["verification_questions"]),
                assessment["next_action"],
                assessment["outreach_angle"],
                _dumps(assessment["citations"]),
                _dumps(policy),
                _dumps(reviewer),
            ),
        )
        shadow = self.stage_shadow_decision(assessment)
        conn.execute(
            """
            INSERT OR REPLACE INTO agent_stage_recommendations
            (job_candidate_id,assessment_id,snapshot_hash,current_stage,proposed_stage,
             rule_code,reason,mode,status,executed,action_type,undo_until,updated_at)
            VALUES (?,?,?,?,?,?,?,'shadow','pending',0,'internal_stage_recommendation',
                    datetime('now','+10 minutes','localtime'),datetime('now','localtime'))
            """,
            (
                relation["job_candidate_id"],
                int(assessment_cursor.lastrowid),
                snapshot_hash,
                relation.get("clean_stage") or relation.get("raw_stage") or "",
                shadow["proposed_stage"],
                shadow["rule_code"],
                shadow["reason"],
            ),
        )
        if candidate.get("id") is not None:
            self._upsert_candidate_intelligence(conn, candidate, identity, position, assessment)
        rule_ids = [
            int(rule["id"])
            for rule in context.get("learning_rules", [])
            if str(rule.get("id") or "").isdigit()
        ]
        if rule_ids:
            placeholders = ",".join("?" for _ in rule_ids)
            conn.execute(
                f"UPDATE agent_learning_rules SET last_used_at=datetime('now','localtime') WHERE id IN ({placeholders})",
                tuple(rule_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upsert_candidate_intelligence(
    self,
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    identity: dict[str, Any],
    position: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    evidence = {
        "criteria": assessment["criteria"],
        "citations": assessment["citations"],
        "confidence": assessment["confidence"],
        "evidence_coverage": assessment["evidence_coverage"],
        "source": "a_system_agent_v1",
    }
    row = conn.execute(
        """
        SELECT id FROM candidate_intelligence
        WHERE candidate_id=? AND client=? AND position=?
        ORDER BY COALESCE(updated_at,'') DESC,id DESC LIMIT 1
        """,
        (candidate["id"], position.get("client"), position.get("job")),
    ).fetchone()
    values = (
        identity.get("name") or candidate.get("name"),
        identity.get("company") or candidate.get("company"),
        assessment["fit_score"],
        assessment["fit_level"],
        _dumps(evidence),
        _dumps(assessment["risks"]),
        assessment["next_action"],
        _dumps(assessment["strengths"]),
        _dumps(assessment["gaps"]),
        _dumps(assessment["verification_questions"]),
        DECISION_LABELS[assessment["recommendation"]],
    )
    if row:
        conn.execute(
            """
            UPDATE candidate_intelligence
            SET candidate_name=?,candidate_company=?,fit_score=?,fit_level=?,evidence_json=?,
                risk_json=?,next_action=?,strong_matches_json=?,weak_matches_json=?,
                verification_questions_json=?,recommendation_decision=?,
                last_evaluated_at=datetime('now','localtime'),model_version=?,
                updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (*values, f"{ASSESSMENT_VERSION}:{self.llm.model}", row["id"]),
        )
        return
    next_id = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM candidate_intelligence").fetchone()[0]
    conn.execute(
        """
        INSERT INTO candidate_intelligence
        (id,candidate_id,candidate_name,candidate_company,client,position,fit_score,fit_level,
         evidence_json,risk_json,next_action,last_evaluated_at,model_version,created_at,updated_at,
         strong_matches_json,weak_matches_json,verification_questions_json,recommendation_decision)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?,datetime('now','localtime'),
                datetime('now','localtime'),?,?,?,?)
        """,
        (
            next_id,
            candidate["id"],
            values[0],
            values[1],
            position.get("client"),
            position.get("job"),
            *values[2:7],
            f"{ASSESSMENT_VERSION}:{self.llm.model}",
            *values[7:],
        ),
    )


def _assessment_payload(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": item.get("id"),
        "run_id": item.get("run_id"),
        "job_candidate_id": item.get("job_candidate_id"),
        "fit_score": item.get("fit_score"),
        "fit_level": item.get("fit_level"),
        "recommendation": item.get("recommendation"),
        "recommendation_label": DECISION_LABELS.get(str(item.get("recommendation")), str(item.get("recommendation") or "")),
        "confidence": item.get("confidence"),
        "evidence_coverage": item.get("evidence_coverage"),
        "criteria": _loads(item.get("criteria_json"), {}),
        "strengths": _loads(item.get("strengths_json"), []),
        "gaps": _loads(item.get("gaps_json"), []),
        "risks": _loads(item.get("risks_json"), []),
        "verification_questions": _loads(item.get("verification_questions_json"), []),
        "next_action": item.get("next_action") or "",
        "outreach_angle": item.get("outreach_angle") or "",
        "citations": _loads(item.get("citations_json"), []),
        "policy": _loads(item.get("policy_json"), {}),
        "reviewer": _loads(item.get("reviewer_json"), {}),
        "created_at": item.get("created_at"),
    }


def _panel_payload(
    self, conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]
) -> dict[str, Any]:
    item = dict(row)
    role_rows = conn.execute(
        "SELECT * FROM agent_role_reviews WHERE panel_id=? ORDER BY id",
        (item.get("panel_id"),),
    ).fetchall()
    roles = []
    for role_row in role_rows:
        role = _row(role_row)
        role["role_label"] = ROLE_DEFINITIONS.get(str(role.get("role")), {}).get(
            "label", role.get("role") or "审校角色"
        )
        role["findings"] = _loads(role.pop("findings_json", "[]"), [])
        role["questions"] = _loads(role.pop("questions_json", "[]"), [])
        roles.append(role)
    return {
        "id": item.get("id"),
        "panel_id": item.get("panel_id"),
        "run_id": item.get("run_id"),
        "job_candidate_id": item.get("job_candidate_id"),
        "assessment_id": item.get("assessment_id"),
        "snapshot_hash": item.get("snapshot_hash"),
        "status": item.get("status"),
        "model_mode": item.get("model_mode"),
        "synthesis": _loads(item.get("synthesis_json"), {}),
        "roles": roles,
        "created_at": item.get("created_at"),
        "completed_at": item.get("completed_at"),
    }


def get_panel_state(self, job_candidate_id: int) -> dict[str, Any]:
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    current_snapshot = self._snapshot_key(context)
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT * FROM agent_review_panels
            WHERE job_candidate_id=?
            ORDER BY is_current DESC,id DESC LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone()
        return {
            "ok": True,
            "job_candidate_id": int(job_candidate_id),
            "stale": not row or row["snapshot_hash"] != current_snapshot,
            "panel": self._panel_payload(conn, row) if row else None,
        }
    finally:
        conn.close()


def get_run(self, run_id: str) -> dict[str, Any]:
    conn = self._connect()
    try:
        run = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return {"ok": False, "error": f"找不到 Agent 运行：{run_id}"}
        assessment = conn.execute(
            "SELECT * FROM agent_candidate_assessments WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone()
        panel = conn.execute(
            "SELECT * FROM agent_review_panels WHERE run_id=? LIMIT 1", (run_id,)
        ).fetchone()
        payload = _row(run)
        payload["ok"] = payload["status"] not in {"failed"}
        if assessment:
            payload["assessment"] = self._assessment_payload(assessment)
        if panel:
            payload["panel"] = self._panel_payload(conn, panel)
        return payload
    finally:
        conn.close()


def get_candidate_state(self, job_candidate_id: int) -> dict[str, Any]:
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    snapshot_hash = self._snapshot_key(context)
    conn = self._connect()
    try:
        assessment = conn.execute(
            """
            SELECT * FROM agent_candidate_assessments
            WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone()
        latest_run = conn.execute(
            """
            SELECT run_id,status,error,trigger,created_at,updated_at,
                   CAST(strftime('%s','now','localtime')-
                        strftime('%s',COALESCE(updated_at,created_at)) AS INTEGER) AS age_seconds
            FROM agent_runs
            WHERE kind='candidate_assessment' AND context_type='job_candidate' AND context_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone()
        latest_run_payload = _row(latest_run) if latest_run else None
        recent_failure = bool(
            latest_run_payload
            and latest_run_payload.get("status") in {"failed", "interrupted", "stale"}
            and int(latest_run_payload.get("age_seconds") or 0) < 600
        )
        active_run = bool(
            latest_run_payload and latest_run_payload.get("status") in {"queued", "running"}
        )
        panel_row = conn.execute(
            """
            SELECT * FROM agent_review_panels
            WHERE job_candidate_id=?
            ORDER BY is_current DESC,id DESC LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone()
        verification_task = None
        task_columns = _table_columns(conn, "followup_tasks")
        if {"id", "job_candidate_id", "task_type", "status"}.issubset(task_columns):
            task_row = conn.execute(
                """
                SELECT * FROM followup_tasks
                WHERE job_candidate_id=? AND task_type='agent_verification'
                  AND COALESCE(status,'open')='open'
                ORDER BY COALESCE(priority,2) ASC,id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            if task_row:
                verification_task = _row(task_row)
                verification_task["questions"] = (
                    self._assessment_payload(assessment).get("verification_questions", [])
                    if assessment
                    else []
                )
        latest_verification = None
        if _table_exists(conn, "candidate_events"):
            verification_row = conn.execute(
                """
                SELECT id,event_status,event_time,summary,raw_json
                FROM candidate_events
                WHERE job_candidate_id=? AND event_type='agent_verification_completed'
                ORDER BY id DESC LIMIT 1
                """,
                (int(job_candidate_id),),
            ).fetchone()
            if verification_row:
                latest_verification = _row(verification_row)
                latest_verification["result"] = _loads(
                    latest_verification.pop("raw_json", "{}"), {}
                )
        return {
            "ok": True,
            "job_candidate_id": int(job_candidate_id),
            "stale": not assessment or assessment["snapshot_hash"] != snapshot_hash,
            "stopped": is_stopped(context),
            "assessment": self._assessment_payload(assessment) if assessment else None,
            "latest_run": latest_run_payload,
            "auto_assess_allowed": not recent_failure and not active_run,
            "panel_stale": not panel_row or panel_row["snapshot_hash"] != snapshot_hash,
            "panel": self._panel_payload(conn, panel_row) if panel_row else None,
            "verification_task": verification_task,
            "latest_verification": latest_verification,
            "artifacts": self._candidate_agent_artifacts(conn, int(job_candidate_id), context),
            "actions": {
                name: action_decision(name, context)
                for name in ["assess", "save_draft", "create_task", "complete_task", "resume_review", "outreach", "candidate_merge"]
            },
        }
    finally:
        conn.close()


def _candidate_agent_artifacts(self, conn: sqlite3.Connection, job_candidate_id: int, context: dict[str, Any]) -> list[dict[str, Any]]:
    relation = context.get("relation") or {}
    candidate_ids = {
        str(value) for value in (
            job_candidate_id,
            relation.get("job_candidate_id"),
            relation.get("source_candidate_id"),
            relation.get("person_id"),
        )
        if value not in (None, "")
    }
    rows = conn.execute(
        """
        SELECT * FROM agent_artifacts
        WHERE artifact_type IN ('recommendation_report','matching_report','external_action_receipt','outreach_draft_batch')
        ORDER BY id DESC LIMIT 80
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _row(row)
        metadata = _loads(item.pop("metadata_json"), {})
        workflow_context = {}
        if not metadata.get("job_candidate_id"):
            ctx_row = conn.execute(
                "SELECT context_json FROM agent_workflow_context WHERE workflow_id=? ORDER BY id DESC LIMIT 1",
                (item.get("workflow_id"),),
            ).fetchone()
            workflow_context = _loads(ctx_row["context_json"], {}) if ctx_row else {}
        metadata_values = {
            str(metadata.get("job_candidate_id") or ""),
            str(metadata.get("candidate_id") or ""),
            str(metadata.get("person_id") or ""),
            str(workflow_context.get("id") or "") if workflow_context.get("type") == "candidate" else "",
        }
        if not (candidate_ids & {value for value in metadata_values if value}):
            continue
        item["metadata"] = metadata
        if item.get("content") and len(str(item["content"])) > 800:
            item["content"] = str(item["content"])[-800:]
        result.append(item)
        if len(result) >= 12:
            break
    return result


def ensure_verification_task(self, job_candidate_id: int, assessment: dict[str, Any]) -> int | None:
    conn = self._connect()
    try:
        existing = conn.execute(
            """
            SELECT id FROM followup_tasks
            WHERE job_candidate_id=? AND task_type='agent_verification'
              AND COALESCE(status,'open')='open'
            ORDER BY id DESC LIMIT 1
            """,
            (int(job_candidate_id),),
        ).fetchone() if _table_exists(conn, "followup_tasks") else None
        if existing:
            return int(existing["id"])
    finally:
        conn.close()
    questions = [str(item).strip() for item in assessment.get("verification_questions") or [] if str(item).strip()]
    reason = "；".join(questions[:5]) or str(assessment.get("next_action") or "补充关键证据后由 ASA 重新评估")
    candidate = build_candidate_context(self.db_path, int(job_candidate_id))
    return self.capability_runtime._followup(
        candidate,
        "agent_verification",
        reason,
        {"priority": 2, "step_id": "assessment_verify_first"},
        days=2,
    )


def batch_assess(
    self,
    job_candidate_ids: list[int] | None = None,
    *,
    limit: int = 5,
    trigger: str = "agent_workbench_batch",
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 5), 8))
    ids = []
    for value in job_candidate_ids or []:
        candidate_id = int(value)
        if candidate_id > 0 and candidate_id not in ids:
            ids.append(candidate_id)
    if not ids:
        ids = [
            int(item["job_candidate_id"])
            for item in self.get_workbench(limit=50)["items"]
            if item["kind"] in {"unassessed", "stale", "failed"}
        ]
    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for job_candidate_id in ids[:limit]:
        state = self.get_candidate_state(job_candidate_id)
        if state.get("stopped"):
            skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前关系已人工停止"})
            continue
        if not state.get("stale"):
            skipped.append({"job_candidate_id": job_candidate_id, "reason": "当前判断仍有效"})
            continue
        if not state.get("auto_assess_allowed"):
            skipped.append({"job_candidate_id": job_candidate_id, "reason": "运行中或处于失败冷却"})
            continue
        started.append(self.submit_assessment(job_candidate_id, trigger=trigger))
    return {"ok": True, "started": started, "skipped": skipped, "limit": limit}


def auto_assess_all(
    self,
    *,
    limit: int = 50,
    trigger: str = "overview_auto_queue",
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 50))
    items = self.get_workbench(limit=limit)["items"]
    candidate_ids = [
        int(item["job_candidate_id"])
        for item in items
        if item["kind"] in {"unassessed", "stale"}
    ]
    started = []
    for job_candidate_id in candidate_ids:
        result = self.submit_assessment(job_candidate_id, trigger=trigger)
        started.append({"job_candidate_id": job_candidate_id, **result})
    return {
        "ok": True,
        "queued_total": len(started),
        "started": started,
        "limit": limit,
    }
