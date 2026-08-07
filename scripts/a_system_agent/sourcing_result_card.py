"""寻访计划结果卡：在工作流完成后生成可展示的结果摘要。

数据协议 consumed by asa-web:
- action_card.type = "sourcing_result"
- summary 包含本轮评估统计、推荐等级分布、Top 候选人、下一步操作。
"""

from __future__ import annotations
import json
from typing import Any


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _mask_name(value: str) -> str:
    """对外列表只暴露遮罩姓名。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "*" in text or "某" in text or text.endswith(("先生", "女士", "老师")):
        return text
    return text[:1] + "**"


def _round_number_from_title(title: str) -> int | None:
    """从工作流标题里解析轮次，例如'第3轮寻访'->3。"""
    import re
    match = re.search(r"第\s*(\d+)\s*轮", str(title or ""))
    if match:
        return int(match.group(1))
    return None


def _job_context(conn: Any, workflow_id: str) -> dict[str, Any]:
    """读取 workflow 关联的岗位与客户信息。"""
    row = conn.execute(
        """
        SELECT g.context_json, g.objective, g.title, w.business_outcome, w.status
        FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
        WHERE w.workflow_id=?
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        return {}
    context = _loads(row["context_json"], {})
    job_id = int(context.get("id") or 0) if context.get("type") == "job" else 0
    job_row = None
    if job_id:
        job_row = conn.execute(
            """
            SELECT j.title AS job, c.name AS client
            FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE j.id=?
            """,
            (job_id,),
        ).fetchone()
    return {
        "workflow_id": workflow_id,
        "objective": row["objective"] or "",
        "title": row["title"] or "",
        "status": row["status"] or "",
        "business_outcome": row["business_outcome"],
        "job_id": job_id,
        "client": job_row["client"] if job_row else context.get("client", ""),
        "job": job_row["job"] if job_row else context.get("job", ""),
        "round": _round_number_from_title(row["title"] or ""),
    }


def _assessment_step_result(conn: Any, workflow_id: str) -> dict[str, Any]:
    """读取 candidate_batch_assessment 步骤的最新输出。"""
    row = conn.execute(
        """
        SELECT output_json FROM agent_workflow_steps
        WHERE workflow_id=? AND capability_id='candidate_batch_assessment'
        ORDER BY sequence DESC LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    output = _loads(row["output_json"] if row else None, {})
    queue = output.get("assessment_queue") if isinstance(output, dict) else {}
    if not isinstance(queue, dict):
        queue = {}
    return queue


def _total_current_assessments(conn: Any, job_id: int) -> int:
    """岗位当前有效评估总数。"""
    if not job_id:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM agent_candidate_assessments a
        JOIN agent_runs r ON r.run_id=a.run_id
        JOIN job_candidates jc ON jc.id=a.job_candidate_id
        WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
        """,
        (job_id,),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def _top_candidates(conn: Any, job_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """取岗位当前有效评估中 fit_score 最高的候选人。"""
    if not job_id:
        return []
    rows = conn.execute(
        """
        SELECT
            jc.id AS job_candidate_id,
            p.display_name,
            p.current_company,
            p.current_title,
            a.fit_score,
            a.fit_level,
            a.recommendation
        FROM agent_candidate_assessments a
        JOIN agent_runs r ON r.run_id=a.run_id
        JOIN job_candidates jc ON jc.id=a.job_candidate_id
        JOIN people p ON p.id=jc.person_id
        WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
        ORDER BY a.fit_score DESC, a.created_at DESC
        LIMIT ?
        """,
        (job_id, limit),
    ).fetchall()
    return [
        {
            "job_candidate_id": row["job_candidate_id"],
            "name": _mask_name(row["display_name"]),
            "current_company": row["current_company"] or "",
            "current_title": row["current_title"] or "",
            "fit_score": row["fit_score"],
            "fit_level": row["fit_level"] or "",
            "recommendation": row["recommendation"] or "",
        }
        for row in rows
    ]


def _recommendation_breakdown(conn: Any, job_id: int) -> dict[str, int]:
    """岗位当前有效评估的推荐等级分布。"""
    breakdown: dict[str, int] = {"recommended": 0, "verify_first": 0, "not_recommended": 0}
    if not job_id:
        return breakdown
    rows = conn.execute(
        """
        SELECT a.recommendation, COUNT(*) AS cnt
        FROM agent_candidate_assessments a
        JOIN agent_runs r ON r.run_id=a.run_id
        JOIN job_candidates jc ON jc.id=a.job_candidate_id
        WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
        GROUP BY a.recommendation
        """,
        (job_id,),
    ).fetchall()
    for row in rows:
        key = str(row["recommendation"] or "").strip()
        if key in breakdown:
            breakdown[key] = int(row["cnt"])
    return breakdown


def build_sourcing_result_card(conn: Any, workflow_id: str) -> dict[str, Any] | None:
    """为已完成的寻访工作流生成结果卡数据。

    返回 None 表示该工作流不是寻访类目标或没有岗位上下文。
    """
    import re
    context = _job_context(conn, workflow_id)
    objective = str(context.get("objective") or "")
    if not any(token in objective for token in ("补充", "补池", "寻访", "找人", "搜索", "找更多", "找到更多")):
        return None
    job_id = int(context.get("job_id") or 0)
    if not job_id:
        return None

    queue = _assessment_step_result(conn, workflow_id)
    completed_items = queue.get("completed_items") if isinstance(queue, dict) else []
    failed_items = queue.get("failed") if isinstance(queue, dict) else []
    if not isinstance(completed_items, list):
        completed_items = []
    if not isinstance(failed_items, list):
        failed_items = []

    # 如果本轮没有评估数据，仍然生成一张空结果卡（方便前端展示完成状态）。
    breakdown = _recommendation_breakdown(conn, job_id)
    total_in_job = _total_current_assessments(conn, job_id)

    # 区分本轮新增 vs 岗位累计：本轮成功数用 completed_items 长度，失败用 failed。
    assessed_count = len(completed_items) + len(failed_items)
    successful_count = len(completed_items)
    failed_count = len(failed_items)

    # 下一步操作根据业务终态调整。
    business_outcome = context.get("business_outcome")
    next_actions: list[dict[str, str]] = [
        {"type": "review_candidates", "label": "复核现有人选"},
        {"type": "discuss_strategy", "label": "调整寻访策略"},
    ]
    if business_outcome in ("completed_target_met",):
        next_actions.append({"type": "archive", "label": "结束本轮"})
    elif business_outcome in ("completed_needs_review", "completed_pool_insufficient"):
        next_actions.append({"type": "continue_sourcing", "label": "继续补池"})
    else:
        next_actions.append({"type": "archive", "label": "结束本轮"})

    title_parts = [p for p in (context.get("client"), context.get("job")) if p]
    if context.get("round"):
        title = f"寻访结果：{' · '.join(title_parts)} · 第{context['round']}轮"
    else:
        title = f"寻访结果：{' · '.join(title_parts)}"

    return {
        "type": "sourcing_result",
        "title": title,
        "context": {"type": "workflow", "id": workflow_id},
        "summary": {
            "workflow_id": workflow_id,
            "round": context.get("round"),
            "client": context.get("client") or "",
            "job": context.get("job") or "",
            "status": context.get("status") or "",
            "business_outcome": business_outcome,
            "assessed_count": assessed_count,
            "successful_count": successful_count,
            "failed_count": failed_count,
            "total_assessed_in_job": total_in_job,
            "recommendation_breakdown": breakdown,
            "top_candidates": _top_candidates(conn, job_id, limit=5),
            "next_actions": next_actions,
        },
    }
