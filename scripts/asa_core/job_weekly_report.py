"""岗位自动周报（三期驾驶舱缺口）：按岗位 × 按周的确定性组装报告。

不依赖 LLM：全部内容来自库内真实数据的聚合与保守规则阈值。
产物落 agent_artifacts（artifact_type=job_weekly_report），同周幂等 upsert
同一 artifact（version 自增 + history 留痕，上限 10 条），markdown 正文 +
结构化 metadata；跨周生成新 artifact，以上一期周报漏斗作为对比基线，
无基线时如实标注「无上期对比」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

SCHEMA_VERSION = "job_weekly_report_v1"
ARTIFACT_TYPE = "job_weekly_report"
HISTORY_LIMIT = 10

# 停止口径与 asa_core.service 保持一致（避免循环 import 在此复写同一组词表）。
_STOP_STAGES = ("初筛不通过", "停止推进", "已停止", "淘汰", "关闭")
_STOP_STATUSES = {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
_CONTACTED_TOKENS = ("已触达", "已联系", "已沟通", "已推荐", "面试", "Offer")
_RECOMMENDED_TOKENS = ("已推荐", "客户", "面试", "Offer")

# 触达/回复事件口径：来自 candidate_events 真实 event_type 分布。
_OUTREACH_EVENTS = ("liepin_outreach", "candidate_outreach")
_REPLY_EVENTS = ("candidate_message_received",)
_FEEDBACK_TYPES = ("approved", "interview", "rejected", "hold", "other")
FEEDBACK_TYPE_LABELS = {
    "approved": "客户认可",
    "interview": "安排面试",
    "rejected": "客户否决",
    "hold": "暂缓推进",
    "other": "其他反馈",
}

# 规则阈值（保守：只基于真实数据计数，不外推）。
OUTREACH_NO_REPLY_MIN = 5  # 本周触达 >= N 且 0 回复才提示
NEW_CANDIDATE_EVENTS = ("search_shortlisted", "xsaas_search_shortlisted", "candidate_intake", "mapping_task_created")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except Exception:  # noqa: BLE001 元数据损坏按缺省处理
        return default


def _table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _is_stopped(stage: Any, raw_status: Any) -> bool:
    stage_text = str(stage or "")
    return any(token in stage_text for token in _STOP_STAGES) or str(raw_status or "").lower() in _STOP_STATUSES


def week_window(now: datetime) -> tuple[datetime, datetime]:
    """自然周窗口 [周一 00:00, 下周一 00:00)。"""
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def _dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _current_funnel(conn: Any, job_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT clean_stage,raw_status FROM job_candidates WHERE job_id=?",
        (int(job_id),),
    ).fetchall()
    stopped = sum(_is_stopped(row["clean_stage"], row["raw_status"]) for row in rows)
    active = len(rows) - stopped
    return {
        "total": len(rows),
        "active": active,
        "stopped": stopped,
        "contacted": sum(
            any(token in str(row["clean_stage"] or "") for token in _CONTACTED_TOKENS)
            for row in rows if not _is_stopped(row["clean_stage"], row["raw_status"])
        ),
        "recommended": sum(
            any(token in str(row["clean_stage"] or "") for token in _RECOMMENDED_TOKENS)
            for row in rows if not _is_stopped(row["clean_stage"], row["raw_status"])
        ),
    }


def _assessed_count(conn: Any, job_id: int) -> int:
    if not _table_exists(conn, "agent_candidate_assessments"):
        return 0
    return int(
        conn.execute(
            """SELECT COUNT(DISTINCT a.job_candidate_id)
                 FROM agent_candidate_assessments a
                 JOIN agent_runs r ON r.run_id=a.run_id
                 JOIN job_candidates jc ON jc.id=a.job_candidate_id
                WHERE a.is_current=1 AND r.status='completed' AND jc.job_id=?""",
            (int(job_id),),
        ).fetchone()[0]
    )


def _previous_funnel(conn: Any, job_id: int, week_start: str) -> dict[str, int] | None:
    """上一期周报（同岗位、周起始早于本周）的漏斗快照，作为对比基线。"""
    rows = conn.execute(
        """
        SELECT metadata_json FROM agent_artifacts
        WHERE artifact_type=? AND artifact_id LIKE ?
        ORDER BY id DESC LIMIT 20
        """,
        (ARTIFACT_TYPE, f"job_weekly_{int(job_id)}_%"),
    ).fetchall()
    for row in rows:
        doc = _loads(row["metadata_json"], {})
        if not isinstance(doc, dict):
            continue
        if str(doc.get("week_start") or "") >= week_start:
            continue
        funnel = doc.get("funnel")
        current = funnel.get("current") if isinstance(funnel, dict) else None
        if isinstance(current, dict) and all(isinstance(current.get(key), int) for key in ("total", "active", "contacted", "recommended", "stopped")):
            return current
    return None


def build_job_weekly_report(conn: Any, job_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """组装岗位周报结构化文档；岗位不存在抛 LookupError。"""
    now = now or datetime.now()
    start, end = week_window(now)
    week_start, week_end = _dt(start), _dt(end)
    base = conn.execute(
        "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
        (int(job_id),),
    ).fetchone()
    if not base:
        raise LookupError("job not found")
    job_title = str(base["title"] or "")
    client = str(base["client"] or "")

    funnel_now = _current_funnel(conn, job_id)
    funnel_prev = _previous_funnel(conn, job_id, week_start[:10])
    new_candidates = int(
        conn.execute(
            """SELECT COUNT(DISTINCT job_candidate_id) FROM candidate_events
                WHERE job_id=? AND event_type IN ({}) AND COALESCE(event_time,'')>=? AND COALESCE(event_time,'')<?""".format(
                ",".join("?" * len(NEW_CANDIDATE_EVENTS))
            ),
            (int(job_id), *NEW_CANDIDATE_EVENTS, week_start, week_end),
        ).fetchone()[0]
    )

    assessed = _assessed_count(conn, job_id)
    confirmed_total = 0
    confirmed_week = 0
    if _table_exists(conn, "consultant_confirmed_recommendations"):
        confirmed_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM consultant_confirmed_recommendations WHERE job_id=?",
                (int(job_id),),
            ).fetchone()[0]
        )
        confirmed_week = int(
            conn.execute(
                """SELECT COUNT(*) FROM consultant_confirmed_recommendations
                    WHERE job_id=? AND COALESCE(confirmed_at,'')>=? AND COALESCE(confirmed_at,'')<?""",
                (int(job_id), week_start, week_end),
            ).fetchone()[0]
        )
    feedback = {key: 0 for key in _FEEDBACK_TYPES}
    if _table_exists(conn, "recommendation_package_feedback"):
        for row in conn.execute(
            """SELECT feedback_type,COUNT(*) AS n FROM recommendation_package_feedback
                WHERE job_id=? AND COALESCE(feedback_time,'')>=? AND COALESCE(feedback_time,'')<?
                GROUP BY feedback_type""",
            (int(job_id), week_start, week_end),
        ).fetchall():
            key = str(row["feedback_type"] or "other")
            feedback[key if key in feedback else "other"] += int(row["n"])

    channels: list[dict[str, Any]] = []
    if _table_exists(conn, "agent_sourcing_funnel"):
        for row in conn.execute(
            """SELECT channel,COUNT(*) AS runs,SUM(recall_count) AS recall,SUM(extracted_count) AS extracted,
                      SUM(assessed_count) AS assessed,SUM(high_score_count) AS high_score
                 FROM agent_sourcing_funnel
                WHERE job_id=? AND COALESCE(created_at,'')>=? AND COALESCE(created_at,'')<?
                GROUP BY channel ORDER BY recall DESC, channel ASC""",
            (int(job_id), week_start, week_end),
        ).fetchall():
            assessed_n = int(row["assessed"] or 0)
            high = int(row["high_score"] or 0)
            channels.append(
                {
                    "channel": str(row["channel"] or "unknown"),
                    "runs": int(row["runs"] or 0),
                    "recall_count": int(row["recall"] or 0),
                    "extracted_count": int(row["extracted"] or 0),
                    "assessed_count": assessed_n,
                    "high_score_count": high,
                    "high_score_rate": round(high / assessed_n, 4) if assessed_n else None,
                }
            )

    # 风险（全部从现有数据推导，样本只列前 3 条）。
    risks: list[dict[str, Any]] = []
    overdue_rows = conn.execute(
        """SELECT candidate_name,task_type,due_at FROM followup_tasks
            WHERE client=? AND position=? AND COALESCE(status,'open') NOT IN ('closed','completed','done')
              AND due_at IS NOT NULL AND due_at<>'' AND due_at<?
            ORDER BY due_at ASC LIMIT 50""",
        (client, job_title, _dt(now)),
    ).fetchall()
    if overdue_rows:
        risks.append(
            {
                "code": "overdue_followups",
                "label": "逾期待办",
                "count": len(overdue_rows),
                "detail": "；".join(
                    f"{str(row['candidate_name'] or row['task_type'] or '待办')}（截止 {str(row['due_at'])[:10]}）"
                    for row in overdue_rows[:3]
                ),
            }
        )
    backlog = funnel_now["active"] - assessed
    if backlog > 0:
        risks.append(
            {
                "code": "assessment_backlog",
                "label": "待评估积压",
                "count": backlog,
                "detail": f"活跃人选 {funnel_now['active']} 人中 {backlog} 人尚无当前有效判人评估",
            }
        )
    outreach_count = int(
        conn.execute(
            """SELECT COUNT(*) FROM candidate_events
                WHERE job_id=? AND event_type IN ({}) AND COALESCE(event_time,'')>=? AND COALESCE(event_time,'')<?""".format(
                ",".join("?" * len(_OUTREACH_EVENTS))
            ),
            (int(job_id), *_OUTREACH_EVENTS, week_start, week_end),
        ).fetchone()[0]
    )
    reply_count = int(
        conn.execute(
            """SELECT COUNT(*) FROM candidate_events
                WHERE job_id=? AND event_type IN ({}) AND COALESCE(event_time,'')>=? AND COALESCE(event_time,'')<?""".format(
                ",".join("?" * len(_REPLY_EVENTS))
            ),
            (int(job_id), *_REPLY_EVENTS, week_start, week_end),
        ).fetchone()[0]
    )
    if outreach_count >= OUTREACH_NO_REPLY_MIN and reply_count == 0:
        risks.append(
            {
                "code": "outreach_no_reply",
                "label": "触达无回复",
                "count": outreach_count,
                "detail": f"本周触达 {outreach_count} 人次，0 回复",
            }
        )

    # 建议（保守规则：只由上面的真实计数触发，无触发即如实说明）。
    suggestions: list[str] = []
    if outreach_count >= OUTREACH_NO_REPLY_MIN and reply_count == 0:
        suggestions.append(f"本周触达 {outreach_count} 人次 0 回复，建议调整触达话术或更换触达渠道后小批量验证。")
    if overdue_rows:
        suggestions.append(f"有 {len(overdue_rows)} 条待办已逾期，建议优先处理，避免人选流失。")
    if backlog > 0:
        suggestions.append(f"{backlog} 名活跃人选尚未完成判人评估，建议先完成评估再安排客户推荐。")
    for channel in channels:
        if channel["recall_count"] > 0 and channel["extracted_count"] == 0:
            suggestions.append(
                f"渠道 {channel['channel']} 本周召回 {channel['recall_count']} 但入库 0，建议检查该渠道关键词或抓取质量。"
            )
    if feedback["rejected"] > feedback["approved"] and feedback["rejected"] >= 1:
        suggestions.append("本周客户否决多于认可，建议复核推荐口径与人岗匹配标准。")
    week_events = int(
        conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE job_id=? AND COALESCE(event_time,'')>=? AND COALESCE(event_time,'')<?",
            (int(job_id), week_start, week_end),
        ).fetchone()[0]
    )
    if week_events == 0:
        suggestions.append("本周岗位无任何业务动态，建议确认岗位状态或补充寻访动作。")
    if not suggestions:
        suggestions.append("本周数据未触发任何规则建议，保持当前推进节奏。")

    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": int(job_id),
        "job_title": job_title,
        "client": client,
        "week_start": week_start[:10],
        "week_end": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "generated_at": _dt(now),
        "funnel": {
            "current": funnel_now,
            "previous": funnel_prev,
            "new_candidates_this_week": new_candidates,
            "comparison": "available" if funnel_prev else "no_baseline",
        },
        "recommendations": {
            "confirmed_this_week": confirmed_week,
            "confirmed_total": confirmed_total,
            "assessed_candidates": assessed,
            "rate": round(confirmed_total / assessed, 4) if assessed else None,
            "feedback": feedback,
        },
        "channels": channels,
        "outreach": {"outreach_count": outreach_count, "reply_count": reply_count},
        "risks": risks,
        "suggestions": suggestions,
    }


def _delta(current: int, previous: dict[str, int] | None, key: str) -> str:
    if not previous:
        return "无上期对比"
    diff = current - int(previous.get(key) or 0)
    return f"+{diff}" if diff > 0 else str(diff)


def render_job_weekly_markdown(doc: dict[str, Any]) -> str:
    funnel = doc["funnel"]
    current = funnel["current"]
    previous = funnel.get("previous")
    recommendations = doc["recommendations"]
    feedback = recommendations["feedback"]
    lines = [
        f"# 岗位周报 · {doc['client']} {doc['job_title']}",
        "",
        f"> 统计窗口 {doc['week_start']} ~ {doc['week_end']} · 生成于 {doc['generated_at']} · v{doc.get('version', 1)}",
        "",
        "## 一、漏斗概览",
        "",
        "| 指标 | 本周 | 较上周 |",
        "| --- | --- | --- |",
    ]
    for key, label in (("total", "全部人选"), ("active", "活跃推进"), ("contacted", "已触达"), ("recommended", "已推荐"), ("stopped", "已停止")):
        lines.append(f"| {label} | {current[key]} | {_delta(current[key], previous, key)} |")
    lines.append("")
    if not previous:
        lines.append("本期为首期周报（或无可用上期数据），上期对比缺失，如实标注「无上期对比」。")
        lines.append("")
    lines.append(f"本周新增人选 {funnel['new_candidates_this_week']} 人（按新增/入库类事件计）。")
    lines += [
        "",
        "## 二、有效推荐",
        "",
        f"- 本周顾问确认推荐 {recommendations['confirmed_this_week']} 人；累计确认 {recommendations['confirmed_total']} 人。",
        "- 有效推荐率："
        + (
            f"{round(recommendations['rate'] * 100)}%（累计确认 {recommendations['confirmed_total']} / 已完成评估 {recommendations['assessed_candidates']}）。"
            if recommendations["rate"] is not None
            else "数据不足（尚无完成的判人评估）。"
        ),
        "- 本周推荐包反馈："
        + " · ".join(f"{FEEDBACK_TYPE_LABELS[key]} {feedback[key]}" for key in _FEEDBACK_TYPES)
        + "。",
        "",
        "## 三、渠道质量",
        "",
    ]
    if doc["channels"]:
        lines += ["| 渠道 | 召回 | 入库 | 已评估 | 高分 | 高分率 |", "| --- | --- | --- | --- | --- | --- |"]
        for channel in doc["channels"]:
            rate = f"{round(channel['high_score_rate'] * 100)}%" if channel["high_score_rate"] is not None else "数据不足"
            lines.append(
                f"| {channel['channel']} | {channel['recall_count']} | {channel['extracted_count']} | "
                f"{channel['assessed_count']} | {channel['high_score_count']} | {rate} |"
            )
    else:
        lines.append("本周无寻访执行记录（agent_sourcing_funnel 无本周渠道行）。")
    lines += ["", "## 四、风险", ""]
    if doc["risks"]:
        for risk in doc["risks"]:
            lines.append(f"- **{risk['label']}**（{risk['count']}）：{risk['detail']}")
    else:
        lines.append("本周未识别到风险项。")
    lines += ["", "## 五、建议", ""]
    for index, suggestion in enumerate(doc["suggestions"], start=1):
        lines.append(f"{index}. {suggestion}")
    lines += [
        "",
        "## 口径说明",
        "",
        "- 漏斗：job_candidates 当前阶段快照；「较上周」取上一期岗位周报漏斗快照，无基线时如实标注。",
        "- 有效推荐：顾问确认推荐（consultant_confirmed_recommendations）/ 已完成判人评估人数；推荐包反馈按 feedback_time 落在本周计数。",
        "- 渠道质量：agent_sourcing_funnel 本周各渠道召回/入库/评估/高分聚合，高分率=高分/已评估。",
        "- 风险：逾期待办（followup_tasks）、待评估积压（活跃-已评估）、触达无回复（本周触达事件≥5 且回复 0）。",
        "- 本报告为确定性规则组装，未使用模型生成；建议仅基于上述真实数据阈值。",
        "",
    ]
    return "\n".join(lines)


def upsert_job_weekly_report(conn: Any, doc: dict[str, Any]) -> str:
    """同周幂等 upsert：artifact_id=job_weekly_<job_id>_<week_start>，version 自增 + history 留痕。"""
    artifact_id = f"job_weekly_{int(doc['job_id'])}_{doc['week_start']}"
    existing = conn.execute(
        "SELECT artifact_id,metadata_json FROM agent_artifacts WHERE artifact_id=? AND artifact_type=? LIMIT 1",
        (artifact_id, ARTIFACT_TYPE),
    ).fetchone()
    title = f"岗位周报 {doc['client']} {doc['job_title']} {doc['week_start']}"
    if existing:
        previous = _loads(existing["metadata_json"], {})
        history = list(previous.get("history") or [])
        history.append({"version": int(previous.get("version") or 1), "generated_at": previous.get("generated_at")})
        doc["version"] = int(previous.get("version") or 1) + 1
        doc["history"] = history[-HISTORY_LIMIT:]
        conn.execute(
            "UPDATE agent_artifacts SET title=?,content=?,metadata_json=?,validation_status='passed' WHERE artifact_id=?",
            (f"{title} v{doc['version']}", render_job_weekly_markdown(doc), _dumps(doc), artifact_id),
        )
        return artifact_id
    doc["version"] = 1
    doc["history"] = []
    conn.execute(
        """
        INSERT INTO agent_artifacts
        (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            f"job_weekly_{int(doc['job_id'])}",
            f"job_weekly_{int(doc['job_id'])}",
            None,
            ARTIFACT_TYPE,
            f"{title} v1",
            "text/markdown",
            None,
            render_job_weekly_markdown(doc),
            _dumps(doc),
            "passed",
        ),
    )
    return artifact_id


def _brief(row: Any) -> dict[str, Any]:
    doc = _loads(row["metadata_json"], {})
    funnel = doc.get("funnel") if isinstance(doc, dict) else {}
    current = funnel.get("current") if isinstance(funnel, dict) else {}
    recommendations = doc.get("recommendations") if isinstance(doc, dict) else None
    recommendations = recommendations if isinstance(recommendations, dict) else {}
    risks = doc.get("risks") if isinstance(doc, dict) else []
    suggestions = doc.get("suggestions") if isinstance(doc, dict) else []
    return {
        "artifact_id": str(row["artifact_id"] or ""),
        "title": str(row["title"] or ""),
        "version": int(doc.get("version") or 1),
        "week_start": str(doc.get("week_start") or ""),
        "week_end": str(doc.get("week_end") or ""),
        "generated_at": str(doc.get("generated_at") or ""),
        "created_at": str(row["created_at"] or ""),
        "validation_status": str(row["validation_status"] or ""),
        "summary": {
            "total": (current or {}).get("total"),
            "active": (current or {}).get("active"),
            "recommended": (current or {}).get("recommended"),
            "confirmed_this_week": recommendations.get("confirmed_this_week"),
            "comparison": funnel.get("comparison"),
            "risk_count": len(risks) if isinstance(risks, list) else 0,
            "suggestion_count": len(suggestions) if isinstance(suggestions, list) else 0,
        },
    }


def list_job_weekly_reports(conn: Any, job_id: int, *, limit: int = 12) -> dict[str, Any]:
    """岗位周报历史列表（新→旧）；岗位不存在抛 LookupError。"""
    if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (int(job_id),)).fetchone():
        raise LookupError("job not found")
    rows = conn.execute(
        """
        SELECT artifact_id,title,metadata_json,validation_status,created_at
        FROM agent_artifacts
        WHERE artifact_type=? AND artifact_id LIKE ?
        ORDER BY id DESC LIMIT ?
        """,
        (ARTIFACT_TYPE, f"job_weekly_{int(job_id)}_%", int(limit)),
    ).fetchall()
    items = [_brief(row) for row in rows]
    return {"ok": True, "job_id": int(job_id), "latest": items[0] if items else None, "items": items}
