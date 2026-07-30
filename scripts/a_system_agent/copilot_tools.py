"""Phase 2: ASA Copilot 工具系统。提供 Agent 可调用的数据库查询和业务操作能力。

每个工具由两部分组成：
1. OpenAI function calling schema（name/description/parameters）
2. 执行函数 execute_<name>(self, db_path, **kwargs) -> dict
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 工具 Schema 定义（OpenAI function calling 格式）
# ---------------------------------------------------------------------------

COPILOT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_candidate",
            "description": "查询单个候选人的详细信息，包括当前阶段、匹配评分、风险点等。用于了解某个具体候选人的状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "integer",
                        "description": "候选人关系ID（job_candidate_id）",
                    },
                },
                "required": ["candidate_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_job",
            "description": "查询岗位详情，包括候选池大小、当前状态、岗位要求等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "岗位ID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_candidates",
            "description": "搜索候选人池。可按客户、岗位、阶段、姓名关键词搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "client": {
                        "type": "string",
                        "description": "客户名称（模糊匹配），如 '士兰微'",
                    },
                    "job_title": {
                        "type": "string",
                        "description": "岗位名称关键词（模糊匹配），如 'AE'",
                    },
                    "name": {
                        "type": "string",
                        "description": "候选人姓名关键词（模糊匹配）",
                    },
                    "stage": {
                        "type": "string",
                        "description": "阶段过滤，如 'S1 待复核'、'S2 待触达'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回数量，默认10，最大20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dashboard",
            "description": "获取驾驶舱概览：活跃岗位数、候选人总数、待处理数量和异常列表。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candidate_pipeline",
            "description": "获取某个岗位的候选人管道：列出该岗位下所有候选人及其阶段分布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "岗位ID",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "搜索ASA已保存的客户知识、岗位画像和经验记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                    "knowledge_type": {
                        "type": "string",
                        "description": "知识类型：client（客户知识）、job_profile（岗位画像）、experience（经验）、all（全部）",
                        "enum": ["client", "job_profile", "experience", "all"],
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 工具执行函数
# ---------------------------------------------------------------------------

def _safe_query(conn, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """安全查询：返回 dict 列表。"""
    cur = conn.execute(sql, params)
    cols = [desc[0] for desc in cur.description] if cur.description else []
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def execute_query_candidate(db_path: str, candidate_id: int) -> dict[str, Any]:
    """查询候选人详情。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT jc.id, jc.clean_stage, jc.stopped_at, jc.stop_reason,
                      p.display_name as candidate_name,
                      j.title as job_title, cl.name as client_name
               FROM job_candidates jc
               LEFT JOIN people p ON p.id = jc.person_id
               LEFT JOIN jobs j ON j.id = jc.job_id
               LEFT JOIN clients cl ON cl.id = j.client_id
               WHERE jc.id = ?""",
            (candidate_id,),
        ).fetchone()
        if not row:
            return {"success": False, "error": f"未找到候选人 #{candidate_id}"}
        return {
            "success": True,
            "data": {
                "id": row["id"],
                "name": row["candidate_name"] or "未知",
                "stage": row["clean_stage"] or "未知",
                "job": row["job_title"] or "未分配",
                "client": row["client_name"] or "未知",
                "stopped": bool(row["stopped_at"]),
                "stop_reason": row["stop_reason"] or "",
            },
        }
    finally:
        conn.close()


def execute_query_job(db_path: str, job_id: int) -> dict[str, Any]:
    """查询岗位详情。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT j.id, j.title, j.status, j.summary, j.lifecycle_stage,
                      cl.name as client_name,
                      COUNT(jc.id) as candidate_count,
                      SUM(CASE WHEN jc.clean_stage LIKE '%触达%' OR jc.clean_stage LIKE '%面试%' OR jc.clean_stage LIKE '%offer%' THEN 1 ELSE 0 END) as active_count
               FROM jobs j
               LEFT JOIN clients cl ON cl.id = j.client_id
               LEFT JOIN job_candidates jc ON jc.job_id = j.id
               WHERE j.id = ?
               GROUP BY j.id""",
            (job_id,),
        ).fetchone()
        if not row:
            return {"success": False, "error": f"未找到岗位 #{job_id}"}
        return {
            "success": True,
            "data": {
                "id": row["id"],
                "title": row["title"] or "未知",
                "client": row["client_name"] or "未知",
                "status": row["status"] or "未知",
                "lifecycle_stage": row["lifecycle_stage"] or "未知",
                "summary": (row["summary"] or "")[:500],
                "candidate_count": row["candidate_count"],
                "active_count": row["active_count"],
                "need_sourcing": row["active_count"] < 5,
            },
        }
    finally:
        conn.close()


def execute_search_candidates(
    db_path: str,
    client: str = "",
    job_title: str = "",
    name: str = "",
    stage: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """搜索候选人池。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conditions = []
        params: list[Any] = []
        if client:
            conditions.append("cl.name LIKE ?")
            params.append(f"%{client}%")
        if job_title:
            conditions.append("j.title LIKE ?")
            params.append(f"%{job_title}%")
        if name:
            conditions.append("c.name LIKE ?")
            params.append(f"%{name}%")
        if stage:
            conditions.append("jc.clean_stage = ?")
            params.append(stage)
        where = " AND ".join(conditions) if conditions else "1=1"
        limit = min(max(1, limit), 20)
        rows = conn.execute(
            f"""SELECT jc.id, p.display_name as candidate_name, jc.clean_stage,
                       j.title as job_title, cl.name as client_name
                FROM job_candidates jc
                LEFT JOIN people p ON p.id = jc.person_id
                LEFT JOIN jobs j ON j.id = jc.job_id
                LEFT JOIN clients cl ON cl.id = j.client_id
                WHERE {where}
                ORDER BY jc.id DESC
                LIMIT ?""",
            params + [limit],
        ).fetchall()
        results = [
            {
                "id": row["id"],
                "name": row["candidate_name"] or "未知",
                "stage": row["clean_stage"] or "未知",
                "job": row["job_title"] or "未分配",
                "client": row["client_name"] or "未知",
            }
            for row in rows
        ]
        return {
            "success": True,
            "data": {
                "total": len(results),
                "candidates": results,
            },
        }
    finally:
        conn.close()


def execute_get_dashboard(db_path: str) -> dict[str, Any]:
    """获取驾驶舱概览。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        active_jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('closed','archived')"
        ).fetchone()[0]
        total_candidates = conn.execute(
            "SELECT COUNT(*) FROM job_candidates"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM job_candidates WHERE clean_stage IN ('S1 待复核','S2 待触达')"
        ).fetchone()[0]
        interviews = conn.execute(
            "SELECT COUNT(*) FROM job_candidates WHERE clean_stage LIKE '%面试%'"
        ).fetchone()[0]
        # 候选池不足的岗位（活跃候选 < 5）
        low_pool = conn.execute(
            """SELECT COUNT(*) FROM (
                SELECT j.id FROM jobs j
                LEFT JOIN job_candidates jc ON jc.job_id = j.id
                WHERE j.status NOT IN ('closed','archived')
                GROUP BY j.id HAVING COUNT(jc.id) < 5
            )"""
        ).fetchone()[0]
        return {
            "success": True,
            "data": {
                "active_jobs": active_jobs,
                "total_candidates": total_candidates,
                "pending_review_or_outreach": pending,
                "in_interview": interviews,
                "jobs_with_low_pool": low_pool,
            },
        }
    finally:
        conn.close()


def execute_get_candidate_pipeline(db_path: str, job_id: int) -> dict[str, Any]:
    """获取岗位候选人管道分布。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT jc.id, p.display_name as candidate_name, jc.clean_stage,
                      jc.updated_at
               FROM job_candidates jc
               LEFT JOIN people p ON p.id = jc.person_id
               WHERE jc.job_id = ?
               ORDER BY jc.clean_stage, jc.updated_at DESC""",
            (job_id,),
        ).fetchall()
        # 按阶段分组
        stages: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            stage = row["clean_stage"] or "未分类"
            stages.setdefault(stage, []).append({
                "id": row["id"],
                "name": row["candidate_name"] or "未知",
            })
        return {
            "success": True,
            "data": {
                "job_id": job_id,
                "total": len(rows),
                "stages": {k: len(v) for k, v in stages.items()},
                "pipeline": stages,
            },
        }
    finally:
        conn.close()


def execute_search_knowledge(
    db_path: str, query: str, knowledge_type: str = "all"
) -> dict[str, Any]:
    """搜索知识库。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        results: list[dict[str, Any]] = []
        # 从 agent_memories 表搜索
        try:
            type_filter = ""
            type_params: list[Any] = []
            if knowledge_type != "all":
                type_filter = "AND memory_type = ?"
                type_params = [knowledge_type]
            rows = conn.execute(
                f"""SELECT id, memory_type, content, client, job, created_at
                    FROM agent_memories
                    WHERE content LIKE ? {type_filter}
                    ORDER BY created_at DESC LIMIT 5""",
                [f"%{query}%"] + type_params,
            ).fetchall()
            for row in rows:
                results.append({
                    "id": row["id"],
                    "type": row["memory_type"] or "unknown",
                    "content": (row["content"] or "")[:300],
                    "client": row["client"] or "",
                    "job": row["job"] or "",
                })
        except sqlite3.OperationalError:
            pass  # 表可能还不存在
        return {
            "success": True,
            "data": {
                "query": query,
                "total": len(results),
                "results": results,
            },
        }
    finally:
        conn.close()


# 工具执行路由表
TOOL_EXECUTORS = {
    "query_candidate": execute_query_candidate,
    "query_job": execute_query_job,
    "search_candidates": execute_search_candidates,
    "get_dashboard": execute_get_dashboard,
    "get_candidate_pipeline": execute_get_candidate_pipeline,
    "search_knowledge": execute_search_knowledge,
}


# ---- Phase 3: 主动建议引擎 ----

def generate_proactive_suggestions(db_path: str) -> list[dict[str, Any]]:
    """扫描业务状态，生成主动建议列表。返回去重后的建议卡片。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    suggestions: list[dict[str, Any]] = []
    try:
        # 1. 候选池不足的岗位（活跃候选 < 5）
        low_pool_jobs = conn.execute(
            """SELECT j.id, j.title, cl.name as client, COUNT(jc.id) as cnt
               FROM jobs j
               JOIN clients cl ON cl.id = j.client_id
               LEFT JOIN job_candidates jc ON jc.job_id = j.id
               WHERE j.status NOT IN ('closed','archived')
               GROUP BY j.id HAVING cnt < 5
               ORDER BY cnt ASC LIMIT 3"""
        ).fetchall()
        for job in low_pool_jobs:
            suggestions.append({
                "id": f"source_{job['id']}",
                "type": "source_candidates",
                "priority": "high",
                "message": f"{job['client']} {job['title']} 候选池仅 {job['cnt']} 人，需要寻访补充",
                "action": {"type": "start_sourcing", "job_id": job["id"], "label": "开始寻访"},
            })

        # 2. 长时间待触达的候选人（> 7 天）
        stale = conn.execute(
            """SELECT jc.id, p.display_name as name, jc.clean_stage, jc.updated_at,
                      j.title as job_title, cl.name as client
               FROM job_candidates jc
               LEFT JOIN people p ON p.id = jc.person_id
               LEFT JOIN jobs j ON j.id = jc.job_id
               LEFT JOIN clients cl ON cl.id = j.client_id
               WHERE jc.clean_stage = 'S2 待触达'
                 AND jc.updated_at < datetime('now', '-7 days', 'localtime')
               ORDER BY jc.updated_at ASC LIMIT 3"""
        ).fetchall()
        for row in stale:
            suggestions.append({
                "id": f"outreach_{row['id']}",
                "type": "outreach_reminder",
                "priority": "medium",
                "message": f"{row['name']}（{row['client']} {row['job_title']}）已待触达超7天",
                "action": {"type": "open_candidate", "id": row["id"], "label": "查看人选"},
            })

        # 3. S1 待复核积压
        pending_review = conn.execute(
            "SELECT COUNT(*) as cnt FROM job_candidates WHERE clean_stage = 'S1 待复核'"
        ).fetchone()
        if pending_review and pending_review["cnt"] >= 5:
            suggestions.append({
                "id": "review_backlog",
                "type": "review_backlog",
                "priority": "high",
                "message": f"待复核候选人有 {pending_review['cnt']} 人，建议集中处理",
                "action": {"type": "open_queue", "label": "打开待复核队列"},
            })

        # 4. 最近7天无活动的活跃岗位
        inactive = conn.execute(
            """SELECT j.id, j.title, cl.name as client
               FROM jobs j
               JOIN clients cl ON cl.id = j.client_id
               WHERE j.status NOT IN ('closed','archived')
                 AND j.id NOT IN (
                   SELECT DISTINCT jc.job_id FROM job_candidates jc
                   WHERE jc.updated_at > datetime('now', '-7 days', 'localtime')
                 )
               LIMIT 2"""
        ).fetchall()
        for job in inactive:
            suggestions.append({
                "id": f"inactive_{job['id']}",
                "type": "job_inactive",
                "priority": "low",
                "message": f"{job['client']} {job['title']} 近7天无活动",
                "action": {"type": "open_job", "id": job["id"], "label": "打开岗位"},
            })

    finally:
        conn.close()

    # 去重：同类型只保留最高优先级
    seen_types: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for s in sorted(suggestions, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x["priority"], 3)):
        if s["type"] not in seen_types:
            seen_types.add(s["type"])
            deduped.append(s)
    return deduped[:5]  # 最多5条

