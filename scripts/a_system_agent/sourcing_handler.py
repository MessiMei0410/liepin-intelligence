"""Handler extracted from service.py — Sourcing signals, attribution, channel analytics, dashboard.

All functions receive 'self' (AgentService instance) as first parameter.
"""

from __future__ import annotations
import hashlib, json, re, sqlite3
from datetime import datetime
from typing import Any

from ._shared import (
    _dumps, _loads, _row, _channel_key, _table_exists, _table_columns,
    SOURCING_SIGNAL_WEIGHTS, SOURCING_SIGNAL_LABELS, DECISION_LABELS,
)
from .evaluation import compute_evaluation


def _channel_analytics(self, conn: sqlite3.Connection) -> dict[str, Any]:
    labels = {
        "liepin": "猎聘",
        "xsaas": "X-SaaS",
        "talent_pool": "历史人才库",
        "other": "其他来源",
        "unknown": "未归因",
    }
    profile_sources: dict[int, set[str]] = {}
    inventory: dict[str, set[str]] = {}
    if _table_exists(conn, "source_profiles"):
        for row in conn.execute(
            "SELECT person_id,source_type FROM source_profiles WHERE person_id IS NOT NULL"
        ).fetchall():
            person_id = int(row["person_id"])
            source = str(row["source_type"] or "")
            profile_sources.setdefault(person_id, set()).add(source)
            channel = _channel_key(source)
            inventory.setdefault(channel, set()).add(f"p:{person_id}")

    candidate_sources: dict[int, str] = {}
    if "source" in _table_columns(conn, "candidates"):
        candidate_rows = conn.execute(
            """
            SELECT c.id,c.source,MIN(jc.person_id) AS person_id
            FROM candidates c
            LEFT JOIN job_candidates jc ON CAST(jc.source_candidate_id AS INTEGER)=c.id
            GROUP BY c.id,c.source
            """
        ).fetchall()
        for row in candidate_rows:
            if row["id"] is None:
                continue
            candidate_id = int(row["id"])
            source = str(row["source"] or "")
            candidate_sources[candidate_id] = source
            channel = _channel_key(source)
            identity = (
                f"p:{int(row['person_id'])}"
                if row["person_id"] is not None
                else f"c:{candidate_id}"
            )
            inventory.setdefault(channel, set()).add(identity)

    def fallback_channel(person_id: int) -> str:
        channels = {_channel_key(source) for source in profile_sources.get(person_id, set())}
        for key in ("xsaas", "liepin", "talent_pool", "other"):
            if key in channels:
                return key
        return "unknown"

    event_flags: dict[int, list[tuple[str, str]]] = {}
    if _table_exists(conn, "candidate_events"):
        for row in conn.execute(
            "SELECT job_candidate_id,event_type,event_status FROM candidate_events WHERE job_candidate_id IS NOT NULL"
        ).fetchall():
            status = str(row["event_status"] or "").strip().lower()
            if status in {"undone", "void", "invalid", "retracted"}:
                continue
            event_flags.setdefault(int(row["job_candidate_id"]), []).append(
                (str(row["event_type"] or "").lower(), status)
            )

    metrics: dict[str, dict[str, int]] = {}
    relation_rows = conn.execute(
        """
        SELECT id,person_id,source_candidate_id,clean_stage,raw_stage,raw_status
        FROM job_candidates
        """
    ).fetchall()
    for row in relation_rows:
        candidate_id: int | None = None
        try:
            candidate_id = int(str(row["source_candidate_id"] or ""))
        except ValueError:
            pass
        channel = _channel_key(candidate_sources.get(candidate_id or -1, ""))
        if channel == "unknown":
            channel = fallback_channel(int(row["person_id"]))
        values = metrics.setdefault(
            channel,
            {
                "intake": 0,
                "valid": 0,
                "reviewed": 0,
                "review_passed": 0,
                "contacted": 0,
                "replied": 0,
                "recommended": 0,
                "interview": 0,
            },
        )
        values["intake"] += 1
        stage = " ".join(
            str(row[key] or "") for key in ("clean_stage", "raw_stage", "raw_status")
        ).lower()
        events = event_flags.get(int(row["id"]), [])
        stopped = any(
            token in stage
            for token in ("h5 ", "停止", "淘汰", "拒绝", "screen_rejected", "rejected", "xsaas_review_stop")
        )
        review_results = {
            status
            for event_type, status in events
            if event_type == "resume_review_completed" and status in {"continue", "stop"}
        }
        reviewed = bool(review_results) or stopped or any(
            token in stage for token in ("复核通过", "已触达", "已回复", "推荐", "面试", "offer", "谈薪", "入职")
        )
        review_passed = "continue" in review_results or (
            not stopped
            and any(token in stage for token in ("复核通过", "已触达", "已回复", "推荐", "面试", "offer", "谈薪", "入职"))
        )
        contacted = any(
            token in stage for token in ("已触达", "已联系", "微信", "回复", "推荐", "面试", "offer", "谈薪", "入职")
        ) or any(
            event_type
            in {
                "liepin_outreach",
                "candidate_outreach",
                "candidate_contact_update",
                "outreach_status_backfill",
                "candidate_message_received",
            }
            for event_type, _ in events
        )
        replied = "回复" in stage or any(
            event_type == "candidate_message_received" for event_type, _ in events
        )
        recommended = any(token in stage for token in ("推荐", "面试", "offer", "谈薪", "入职")) or any(
            event_type in {"candidate_recommended", "client_recommendation"}
            for event_type, _ in events
        )
        interview = any(token in stage for token in ("面试", "offer", "谈薪", "入职")) or any(
            event_type in {"interview", "interview_scheduled", "offer", "hired"}
            for event_type, _ in events
        )
        values["valid"] += int(not stopped)
        values["reviewed"] += int(reviewed)
        values["review_passed"] += int(review_passed)
        values["contacted"] += int(contacted)
        values["replied"] += int(replied)
        values["recommended"] += int(recommended)
        values["interview"] += int(interview)
        inventory.setdefault(channel, set()).add(f"p:{int(row['person_id'])}")

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    rows: list[dict[str, Any]] = []
    for channel in set(inventory) | set(metrics):
        values = metrics.get(
            channel,
            {key: 0 for key in ("intake", "valid", "reviewed", "review_passed", "contacted", "replied", "recommended", "interview")},
        )
        intake = int(values["intake"])
        valid = int(values["valid"])
        reviewed = int(values["reviewed"])
        contacted = int(values["contacted"])
        maturity = "可比较" if intake >= 10 else "样本较少" if intake else "待激活"
        rows.append(
            {
                "channel": channel,
                "label": labels.get(channel, channel),
                "inventory": len(inventory.get(channel, set())),
                **values,
                "valid_rate": rate(valid, intake),
                "review_pass_rate": rate(int(values["review_passed"]), reviewed),
                "contact_rate": rate(contacted, intake),
                "reply_rate": rate(int(values["replied"]), contacted),
                "downstream_rate": rate(max(int(values["recommended"]), int(values["interview"])), intake),
                "maturity": maturity,
            }
        )
    order = {"liepin": 0, "xsaas": 1, "talent_pool": 2, "other": 3, "unknown": 4}
    rows.sort(key=lambda item: (-int(item["intake"]), order.get(str(item["channel"]), 9)))
    total_intake = sum(int(item["intake"]) for item in rows)
    attributed = sum(int(item["intake"]) for item in rows if item["channel"] != "unknown")
    main_channel = max(rows, key=lambda item: int(item["intake"]), default={})
    untapped = max(rows, key=lambda item: int(item["inventory"]) - int(item["intake"]), default={})
    return {
        "summary": {
            "total_intake": total_intake,
            "attributed": attributed,
            "coverage_rate": rate(attributed, total_intake),
            "main_channel": main_channel.get("label", "暂无"),
            "untapped_channel": untapped.get("label", "暂无"),
            "untapped_inventory": max(0, int(untapped.get("inventory", 0)) - int(untapped.get("intake", 0))),
        },
        "rows": rows,
    }


def get_dashboard(self) -> dict[str, Any]:
    workbench = self.get_workbench(limit=50)
    conn = self._connect()
    try:
        stage_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段') AS stage,
                   COUNT(*) AS total
            FROM job_candidates
            GROUP BY COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段')
            ORDER BY total DESC
            """
        ).fetchall()
        stage_counts = {str(row["stage"]): int(row["total"]) for row in stage_rows}

        def stage_sum(*tokens: str) -> int:
            return sum(
                total
                for stage, total in stage_counts.items()
                if any(token.lower() in stage.lower() for token in tokens)
            )

        def ratio(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 4) if denominator else 0.0

        total_relations = sum(stage_counts.values())
        closed_relations = stage_sum("H5", "停止", "淘汰", "拒绝")
        effective_relations = total_relations - closed_relations
        contacted_relations = stage_sum("已触达", "X3", "回复", "推荐", "面试", "offer", "谈薪", "入职")
        replied_relations = stage_sum("回复")
        recommended_relations = stage_sum("推荐", "面试", "offer", "谈薪", "入职")
        interview_relations = stage_sum("面试", "offer", "谈薪", "入职")
        offer_relations = stage_sum("offer", "谈薪", "入职")
        funnel_jobs = [
            _row(row)
            for row in conn.execute(
                """
                WITH relations AS (
                    SELECT raw_client AS client,raw_position AS job,
                           COALESCE(NULLIF(clean_stage,''),NULLIF(raw_stage,''),'未分阶段') AS stage
                    FROM job_candidates
                )
                SELECT client,job,COUNT(*) AS total,
                       SUM(CASE WHEN stage LIKE 'H5 %' OR stage LIKE '%停止%' OR stage LIKE '%淘汰%' OR stage LIKE '%拒绝%' THEN 0 ELSE 1 END) AS effective,
                       SUM(CASE WHEN stage LIKE '%已触达%' OR stage LIKE 'X3 %' OR stage LIKE '%回复%' OR stage LIKE '%推荐%' OR stage LIKE '%面试%' OR lower(stage) LIKE '%offer%' OR stage LIKE '%谈薪%' OR stage LIKE '%入职%' THEN 1 ELSE 0 END) AS contacted,
                       SUM(CASE WHEN stage LIKE '%回复%' THEN 1 ELSE 0 END) AS replied,
                       SUM(CASE WHEN stage LIKE 'H5 %' OR stage LIKE '%停止%' OR stage LIKE '%淘汰%' OR stage LIKE '%拒绝%' THEN 1 ELSE 0 END) AS stopped
                FROM relations
                GROUP BY client,job
                ORDER BY total DESC,effective DESC
                LIMIT 10
                """
            ).fetchall()
        ]

        funnel = {
            "total": total_relations,
            "effective": effective_relations,
            "closed": closed_relations,
            "pending_review": stage_sum("待复核", "S1", "X1"),
            "waiting_contact": stage_sum("待联系", "复核通过"),
            "contacted": contacted_relations,
            "replied": replied_relations,
            "recommended": recommended_relations,
            "interview": interview_relations,
            "offer": offer_relations,
            "rates": {
                "effective": ratio(effective_relations, total_relations),
                "contacted": ratio(contacted_relations, effective_relations),
                "replied": ratio(replied_relations, contacted_relations),
                "recommended": ratio(recommended_relations, effective_relations),
            },
            "jobs": funnel_jobs,
            "stages": [{"stage": stage, "total": total} for stage, total in list(stage_counts.items())[:12]],
        }

        channels = self._channel_analytics(conn)

        feedback: dict[str, Any] = {
            "open_tasks": 0,
            "overdue_tasks": 0,
            "reply_events_7d": 0,
            "client_feedback_7d": 0,
            "candidate_reply_avg_hours": None,
            "candidate_reply_samples": 0,
            "client_feedback_avg_hours": None,
            "client_feedback_samples": 0,
            "stalled_jobs": [],
            "stalled_job_count": 0,
        }
        if _table_exists(conn, "followup_tasks"):
            task_row = conn.execute(
                """
                SELECT SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_tasks,
                       SUM(CASE WHEN status='open' AND due_at<>'' AND datetime(due_at)<datetime('now','localtime') THEN 1 ELSE 0 END) AS overdue_tasks
                FROM followup_tasks
                """
            ).fetchone()
            feedback["open_tasks"] = int(task_row["open_tasks"] or 0)
            feedback["overdue_tasks"] = int(task_row["overdue_tasks"] or 0)
        if _table_exists(conn, "candidate_events"):
            event_row = conn.execute(
                """
                SELECT SUM(CASE WHEN event_type='candidate_message_received' THEN 1 ELSE 0 END) AS replies
                FROM candidate_events
                WHERE lower(COALESCE(event_status,'')) NOT IN ('undone','void','invalid','retracted')
                  AND datetime(CASE WHEN event_time LIKE '%Z' THEN datetime(event_time,'localtime') ELSE event_time END)>=datetime('now','-7 days','localtime')
                """
            ).fetchone()
            feedback["reply_events_7d"] = int(event_row["replies"] or 0)
            response_row = conn.execute(
                """
                WITH valid_events AS (
                    SELECT job_candidate_id,event_type,
                           julianday(CASE WHEN event_time LIKE '%Z' THEN datetime(event_time,'localtime') ELSE event_time END) AS event_jd
                    FROM candidate_events
                    WHERE job_candidate_id IS NOT NULL
                      AND lower(COALESCE(event_status,'')) NOT IN ('undone','void','invalid','retracted')
                ), replies AS (
                    SELECT job_candidate_id,MIN(event_jd) AS reply_jd
                    FROM valid_events WHERE event_type='candidate_message_received'
                    GROUP BY job_candidate_id
                ), pairs AS (
                    SELECT r.job_candidate_id,r.reply_jd,MAX(e.event_jd) AS outreach_jd
                    FROM replies r JOIN valid_events e ON e.job_candidate_id=r.job_candidate_id
                    WHERE e.event_type IN ('liepin_outreach','candidate_outreach','candidate_contact_update','outreach_status_backfill')
                      AND e.event_jd<=r.reply_jd
                    GROUP BY r.job_candidate_id,r.reply_jd
                )
                SELECT COUNT(*) AS samples,AVG((reply_jd-outreach_jd)*24.0) AS avg_hours
                FROM pairs WHERE outreach_jd IS NOT NULL AND reply_jd>=outreach_jd
                """
            ).fetchone()
            feedback["candidate_reply_samples"] = int(response_row["samples"] or 0)
            if response_row["avg_hours"] is not None:
                feedback["candidate_reply_avg_hours"] = round(float(response_row["avg_hours"]), 1)
        if _table_exists(conn, "client_feedback_events"):
            client_feedback_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN datetime(COALESCE(feedback_time,created_at))>=datetime('now','-7 days','localtime') THEN 1 ELSE 0 END) AS recent
                FROM client_feedback_events
                """
            ).fetchone()
            feedback["client_feedback_samples"] = int(client_feedback_row["total"] or 0)
            feedback["client_feedback_7d"] = int(client_feedback_row["recent"] or 0)
        if _table_exists(conn, "jobs"):
            stalled_rows = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT j.id AS job_id,c.name AS client,j.title AS job,j.status,j.updated_at,
                           CAST(julianday('now','localtime')-julianday(j.updated_at) AS INTEGER) AS stalled_days
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    WHERE j.status IN ('P0紧急/待启动','已发布/推进中','已搜索/可筛人','已发布','有反馈/待复盘','谈薪中','已触达/跟进中')
                      AND datetime(COALESCE(j.updated_at,''))<datetime('now','-7 days','localtime')
                    ORDER BY stalled_days DESC,j.id
                    LIMIT 10
                    """
                ).fetchall()
            ]
            feedback["stalled_jobs"] = stalled_rows
            feedback["stalled_job_count"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM jobs
                    WHERE status IN ('P0紧急/待启动','已发布/推进中','已搜索/可筛人','已发布','有反馈/待复盘','谈薪中','已触达/跟进中')
                      AND datetime(COALESCE(updated_at,''))<datetime('now','-7 days','localtime')
                    """
                ).fetchone()[0]
            )

        quality = compute_evaluation(conn)
        score_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   AVG(fit_score) AS avg_score,AVG(confidence) AS avg_confidence,
                   AVG(evidence_coverage) AS avg_coverage,
                   SUM(CASE WHEN fit_score>=75 THEN 1 ELSE 0 END) AS high,
                   SUM(CASE WHEN fit_score>=55 AND fit_score<75 THEN 1 ELSE 0 END) AS medium,
                   SUM(CASE WHEN fit_score<55 THEN 1 ELSE 0 END) AS low,
                   SUM(CASE WHEN evidence_coverage>=0.75 THEN 1 ELSE 0 END) AS coverage_high,
                   SUM(CASE WHEN evidence_coverage>=0.50 AND evidence_coverage<0.75 THEN 1 ELSE 0 END) AS coverage_medium,
                   SUM(CASE WHEN evidence_coverage<0.50 THEN 1 ELSE 0 END) AS coverage_low
            FROM agent_candidate_assessments WHERE is_current=1
            """
        ).fetchone()
        failed_runs = int(
            conn.execute("SELECT COUNT(*) FROM agent_runs WHERE status='failed'").fetchone()[0]
        )
        latest_run_row = conn.execute(
            """
            WITH latest AS (
                SELECT context_id,status,
                       ROW_NUMBER() OVER(PARTITION BY context_id ORDER BY id DESC) AS rn
                FROM agent_runs WHERE context_type='job_candidate'
            )
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
            FROM latest WHERE rn=1
            """
        ).fetchone()
        shadow_rows = [
            _row(row)
            for row in conn.execute(
                """
                SELECT sr.*,p.display_name AS candidate,jc.raw_client AS client,jc.raw_position AS job
                FROM agent_stage_recommendations sr
                JOIN job_candidates jc ON jc.id=sr.job_candidate_id
                JOIN people p ON p.id=jc.person_id
                WHERE sr.mode='shadow' AND sr.status='pending'
                ORDER BY sr.id DESC LIMIT 12
                """
            ).fetchall()
        ]
        memory_row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status='revoked' THEN 1 ELSE 0 END) AS revoked,
                   SUM(CASE WHEN hit_count>0 THEN 1 ELSE 0 END) AS used
            FROM agent_memories
            """
        ).fetchone()
        recall_row = conn.execute(
            """
            SELECT COUNT(*) AS recalls,SUM(adopted) AS adopted,SUM(conflict) AS conflicts
            FROM agent_memory_recalls
            """
        ).fetchone()
        rule_rows = {
            str(row["status"]): int(row["total"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS total FROM agent_learning_rules GROUP BY status"
            ).fetchall()
        }
        quality.update(
            {
                "current_total": int(score_row["total"] or 0),
                "avg_score": round(float(score_row["avg_score"] or 0), 1),
                "avg_confidence": round(float(score_row["avg_confidence"] or 0), 4),
                "avg_coverage": round(float(score_row["avg_coverage"] or 0), 4),
                "high_score_total": int(score_row["high"] or 0),
                "medium_score_total": int(score_row["medium"] or 0),
                "low_score_total": int(score_row["low"] or 0),
                "failed_runs": failed_runs,
                "latest_run_total": int(latest_run_row["total"] or 0),
                "latest_failed_runs": int(latest_run_row["failed"] or 0),
                "latest_completed_runs": int(latest_run_row["completed"] or 0),
                "latest_failure_rate": ratio(
                    int(latest_run_row["failed"] or 0), int(latest_run_row["total"] or 0)
                ),
                "score_distribution": [
                    {"label": "75-100", "count": int(score_row["high"] or 0)},
                    {"label": "55-74", "count": int(score_row["medium"] or 0)},
                    {"label": "0-54", "count": int(score_row["low"] or 0)},
                ],
                "coverage_distribution": [
                    {"label": "覆盖充分", "count": int(score_row["coverage_high"] or 0)},
                    {"label": "部分覆盖", "count": int(score_row["coverage_medium"] or 0)},
                    {"label": "证据不足", "count": int(score_row["coverage_low"] or 0)},
                ],
                "shadow_total": len(shadow_rows),
                "memory": {
                    "mode": self.config["memory"]["mode"],
                    "total": int(memory_row["total"] or 0),
                    "active": int(memory_row["active"] or 0),
                    "revoked": int(memory_row["revoked"] or 0),
                    "used": int(memory_row["used"] or 0),
                    "recalls": int(recall_row["recalls"] or 0),
                    "adopted": int(recall_row["adopted"] or 0),
                    "conflicts": int(recall_row["conflicts"] or 0),
                },
                "learning": {
                    "collecting": rule_rows.get("collecting", 0),
                    "pending": rule_rows.get("pending", 0),
                    "active": rule_rows.get("active", 0),
                    "suspended": rule_rows.get("suspended", 0),
                },
            }
        )

        p0_jobs: list[dict[str, Any]] = []
        if _table_exists(conn, "job_pipeline_metrics"):
            p0_jobs = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT j.id AS job_id,c.name AS client,j.title AS job,j.status,
                           m.priority,m.risk,m.a_count,m.b_count,m.p0_count,m.p1_count,
                           m.contacted_count,m.pending_followup_count,m.data_gap,m.stop_condition
                    FROM jobs j JOIN clients c ON c.id=j.client_id
                    JOIN job_pipeline_metrics m ON m.id=(
                        SELECT MAX(m2.id) FROM job_pipeline_metrics m2 WHERE m2.job_id=j.id
                    )
                    WHERE COALESCE(m.priority,'') LIKE 'P0%'
                    ORDER BY COALESCE(m.data_gap,0) DESC,COALESCE(m.pending_followup_count,0) DESC,j.id
                    LIMIT 12
                    """
                ).fetchall()
            ]

        proposals = self.list_proposals(status="pending", limit=20).get("proposals", [])
        items = workbench.get("items", [])
        top_actions = [
            {
                "type": "candidate",
                "id": item["job_candidate_id"],
                "label": f"{item['candidate']} · {item['label']}",
                "project": f"{item['client']} / {item['job']}",
                "reason": item["reason"],
                "priority": item["priority"],
            }
            for item in items[:3]
        ]
        exceptions = [item for item in items if item["kind"] in {"failed", "stale"}][:8]
        return {
            "ok": True,
            "summary": workbench.get("summary", {}),
            "runtime": workbench.get("runtime", {}),
            "top_actions": top_actions,
            "exceptions": exceptions,
            "pending_approvals": proposals,
            "p0_jobs": p0_jobs,
            "shadow_recommendations": shadow_rows,
            "analytics": {
                "funnel": funnel,
                "channels": channels,
                "feedback": feedback,
                "agent_quality": quality,
            },
        }
    finally:
        conn.close()


def _ensure_sourcing_attribution(self, conn: sqlite3.Connection, job_candidate_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_sourcing_attributions WHERE job_candidate_id=? ORDER BY id",
        (int(job_candidate_id),),
    ).fetchall()
    if rows:
        return [_row(row) for row in rows]
    relation = conn.execute(
        """
        SELECT jc.id,jc.job_id,jc.source_candidate_id,c.id AS candidate_id,c.source,c.notes
        FROM job_candidates jc
        LEFT JOIN candidates c ON CAST(c.id AS TEXT)=jc.source_candidate_id
        WHERE jc.id=?
        """,
        (int(job_candidate_id),),
    ).fetchone()
    if relation is None:
        return []
    event = conn.execute(
        """
        SELECT raw_json,source_table FROM candidate_events
        WHERE job_candidate_id=? AND event_type IN ('search_shortlisted','xsaas_search_shortlisted')
        ORDER BY COALESCE(event_time,''),id LIMIT 1
        """,
        (int(job_candidate_id),),
    ).fetchone()
    raw = _loads(event["raw_json"], {}) if event else {}
    notes = str(relation["notes"] or "")
    match = re.search(r"(?:^|[｜|])query=([^｜|]+)", notes)
    query = str(raw.get("source_query") or raw.get("query") or (match.group(1).strip() if match else "") or "未记录关键词")
    channel = str(raw.get("channel") or raw.get("source") or relation["source"] or "unknown").lower()
    source_round = str(raw.get("source_round") or raw.get("round") or "")
    source_purpose = ""
    workflow_id = ""
    strategy_hash = ""
    strategy_model = ""
    strategy_rows = conn.execute(
        """
        SELECT s.workflow_id,s.output_json
        FROM agent_workflow_steps s
        JOIN agent_workflow_context wc ON wc.workflow_id=s.workflow_id
        WHERE s.capability_id='search_strategy' AND s.status='completed'
          AND json_extract(wc.context_json,'$.type')='job'
          AND CAST(json_extract(wc.context_json,'$.id') AS INTEGER)=?
        ORDER BY s.updated_at DESC,s.id DESC LIMIT 20
        """,
        (int(relation["job_id"]),),
    ).fetchall()
    for strategy_row in strategy_rows:
        output = _loads(strategy_row["output_json"], {})
        strategy = output.get("strategy") if isinstance(output.get("strategy"), dict) else {}
        channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
        matched_entry = next(
            (
                entry for entry in channels.get(channel, [])
                if isinstance(entry, dict) and str(entry.get("query") or "").strip() == query
            ),
            None,
        )
        if matched_entry:
            workflow_id = str(strategy_row["workflow_id"] or "")
            strategy_hash = hashlib.sha256(_dumps(strategy).encode("utf-8")).hexdigest()
            generation = strategy.get("generation") if isinstance(strategy.get("generation"), dict) else {}
            strategy_model = str(generation.get("model") or "")
            source_round = str(matched_entry.get("round") or source_round)
            source_purpose = str(matched_entry.get("purpose") or "")
            break
    cursor = conn.execute(
        """
        INSERT INTO agent_sourcing_attributions
        (job_candidate_id,candidate_id,job_id,workflow_id,strategy_hash,strategy_model,
         channel,source_query,source_round,source_purpose)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(job_candidate_id), int(relation["candidate_id"] or 0) or None, int(relation["job_id"]),
            workflow_id or None, strategy_hash or None, strategy_model or None,
            channel, query, source_round, source_purpose,
        ),
    )
    row = conn.execute("SELECT * FROM agent_sourcing_attributions WHERE id=?", (cursor.lastrowid,)).fetchone()
    return [_row(row)] if row else []


def record_sourcing_business_signal(
    self, job_candidate_id: int, signal_type: str, *, actor_type: str,
    note: str = "", source_type: str = "business_event", source_id: Any = None,
) -> dict[str, Any]:
    signal_type = str(signal_type or "").strip()
    if signal_type not in SOURCING_SIGNAL_WEIGHTS:
        raise ValueError("未知寻访学习信号")
    dedupe_key = f"{source_type}:{source_id}" if source_id not in (None, "") else f"{job_candidate_id}:{signal_type}:{actor_type}"
    conn = self._connect()
    try:
        attributions = self._ensure_sourcing_attribution(conn, int(job_candidate_id))
        if not attributions:
            conn.commit()
            return {"ok": True, "recorded": False, "reason": "没有可归因的寻访关键词"}
        inserted = 0
        for attribution in attributions:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_sourcing_feedback
                (dedupe_key,attribution_id,job_candidate_id,job_id,signal_type,actor_type,weight,note,source_type,source_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{dedupe_key}:{attribution['id']}", attribution["id"], int(job_candidate_id), attribution["job_id"],
                    signal_type, actor_type, SOURCING_SIGNAL_WEIGHTS[signal_type], str(note or "")[:800],
                    source_type, str(source_id or "") or None,
                ),
            )
            inserted += int(cursor.rowcount > 0)
        updated_memories: list[int] = []
        for attribution in attributions:
            aggregate = conn.execute(
                """
                SELECT COUNT(*) AS signal_count,COUNT(DISTINCT sf.job_candidate_id) AS candidate_count,
                       ROUND(SUM(sf.weight),2) AS score,
                       SUM(sf.signal_type='review_pass') AS review_pass,
                       SUM(sf.signal_type='contacted') AS contacted,
                       SUM(sf.signal_type='recommended') AS recommended,
                       SUM(sf.signal_type='stopped') AS stopped,
                       SUM(sf.signal_type IN ('client_approved','client_interview','client_offer','client_hired')) AS client_positive,
                       SUM(sf.signal_type='client_rejected') AS client_rejected
                FROM agent_sourcing_feedback sf
                JOIN agent_sourcing_attributions sa ON sa.id=sf.attribution_id
                WHERE sa.job_id=? AND sa.channel=? AND sa.source_query=?
                """,
                (attribution["job_id"], attribution["channel"], attribution["source_query"]),
            ).fetchone()
            job_row = conn.execute(
                "SELECT c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (attribution["job_id"],),
            ).fetchone()
            if not job_row:
                continue
            score = float(aggregate["score"] or 0)
            verdict = "优先保留" if score >= 3 else "有正向证据" if score > 0 else "建议降权" if score < 0 else "继续观察"
            content = (
                f"{job_row['client']}/{job_row['job']} 寻访经验：{attribution['channel']} 关键词“{attribution['source_query']}”{verdict}；"
                f"复核通过 {int(aggregate['review_pass'] or 0)}，联系 {int(aggregate['contacted'] or 0)}，"
                f"推荐 {int(aggregate['recommended'] or 0)}，停止 {int(aggregate['stopped'] or 0)}，"
                f"客户正向 {int(aggregate['client_positive'] or 0)}，客户否决 {int(aggregate['client_rejected'] or 0)}，经验分 {score:g}。"
            )
            memory_source_id = f"job:{attribution['job_id']}|{attribution['channel']}|{attribution['source_query']}"
            content_hash = hashlib.sha256(f"job|{attribution['job_id']}|sourcing_performance|{content}".encode("utf-8")).hexdigest()
            existing = conn.execute(
                "SELECT id FROM agent_memories WHERE source_type='sourcing_performance' AND source_id=?",
                (memory_source_id,),
            ).fetchone()
            confidence = min(1.0, 0.58 + 0.07 * int(aggregate["signal_count"] or 0))
            if existing:
                conn.execute(
                    """
                    UPDATE agent_memories SET content=?,confidence=?,content_hash=?,status='active',
                      revoked_at=NULL,updated_at=datetime('now','localtime') WHERE id=?
                    """,
                    (content, confidence, content_hash, existing["id"]),
                )
                memory_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO agent_memories
                    (scope_type,scope_id,memory_type,content,source_type,source_id,confidence,content_hash)
                    VALUES ('job',?,'sourcing_performance',?,'sourcing_performance',?,?,?)
                    """,
                    (str(attribution["job_id"]), content, memory_source_id, confidence, content_hash),
                )
                memory_id = int(cursor.lastrowid)
            updated_memories.append(memory_id)
        conn.commit()
        return {
            "ok": True, "recorded": bool(inserted), "inserted": inserted,
            "signal_type": signal_type, "weight": SOURCING_SIGNAL_WEIGHTS[signal_type],
            "attributions": attributions, "memory_ids": sorted(set(updated_memories)),
        }
    finally:
        conn.close()

