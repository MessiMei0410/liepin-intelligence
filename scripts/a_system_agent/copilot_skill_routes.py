"""Sourcing strategy gate, client mention routing and skill route table (split from copilot_routing.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
from typing import Any

from ._shared import _contains_any
from . import strategy_v2
from .copilot_evidence import _client_aliases


def _sourcing_strategy_gate(
    self, goal_request: str, goal_context: dict[str, Any], *, floating_compact: bool = False
) -> dict[str, Any]:
    """S4-1 L3 提问门控（PRD §1 最高优先单点）：四锚点缺失 ≥2 且知识库无对应
    岗位原型时，不创建寻访工作流，改为输出四锚点提问清单。仅作用于寻访类目标。"""
    text = str(goal_request or "").lower()
    sourcing_like = any(token in text for token in ("补充", "补池", "寻访", "找人", "搜索", "搜人", "再找", "继续找", "多找")) or any(
        token in text for token in ("人选", "候选人")
    )
    if not sourcing_like or goal_context.get("type") != "job" or not goal_context.get("id"):
        return {"action": "proceed"}
    try:
        job = self.capability_runtime._job(goal_context)
    except ValueError:
        return {"action": "proceed"}
    archetype, match_trace = strategy_v2.match_job_archetype(job.get("client"), job.get("title"))
    classification = strategy_v2.classify_strategy_input(job, archetype=archetype)
    classification["trace"] = [*match_trace, *classification["trace"]]
    if archetype or len(classification.get("missing_anchors") or []) < 2:
        return {"action": "proceed"}
    answer = strategy_v2.build_clarification_answer(job, classification, floating_compact=floating_compact)
    pending = {
        "status": "pending",
        "job_id": int(goal_context["id"]),
        "client": str(job.get("client") or ""),
        "job": str(job.get("title") or ""),
        "original_objective": " ".join(str(goal_request or "").split()),
        "input_level": str(classification.get("input_level") or "L3"),
        "missing_anchors": list(classification.get("missing_anchors") or []),
        "questions": strategy_v2.build_anchor_questions(job, classification),
        "trace": list(classification.get("trace") or [])[-12:],
    }
    return {"action": "ask", "answer": answer, "pending": pending}


def _mentioned_client_names(self, message: str) -> list[str]:
    text = " ".join(str(message or "").split())
    if not text:
        return []
    conn = self._connect()
    try:
        rows = conn.execute("SELECT name FROM clients ORDER BY length(name) DESC, id").fetchall()
    finally:
        conn.close()
    return [
        str(row["name"])
        for row in rows
        if any(alias in text for alias in _client_aliases(str(row["name"] or "")))
    ]


def _route_copilot_skills(self, message: str, context: dict[str, Any]) -> list[str]:
    routes: list[str] = []
    normalized = message.lower()

    def add(skill_id: str) -> None:
        spec = self.skills.get(skill_id)
        if spec and context["type"] in spec.supported_contexts and skill_id not in routes:
            routes.append(skill_id)

    if "opencli" in normalized:
        add("opencli_usage")
        if any(token in message for token in ("当前页面", "浏览器", "网页", "Chrome", "chrome", "页面状态")):
            add("opencli_browser_read")
    if (
        context["type"] == "candidate" and "猎聘" in message
        and any(token in message for token in ("抓取", "补全", "补充", "读取", "简历"))
    ):
        return ["liepin_resume_capture"]

    direct_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("job_intake", ("岗位接入", "录入岗位", "接入岗位", "需求接入")),
        ("jd_calibration", ("jd校准", "JD校准", "岗位校准", "校准岗位", "硬门槛", "岗位需求", "分析岗位", "分析JD", "JD分析", "看看这个JD", "岗位要求", "JD")),
        ("job_library_update", ("更新岗位", "拆分岗位", "新建岗位", "建立岗位", "岗位库更新")),
        ("job_diagnosis", ("岗位诊断", "岗位风险", "岗位漏斗", "风险", "漏斗", "诊断", "驾驶舱", "看板")),
        ("talent_pool_search", ("人才库", "历史人才", "存量人选", "库里", "搜库", "检索人才")),
        ("search_strategy", ("寻访策略", "搜索策略", "怎么找", "搜人策略", "目标公司", "关键词")),
        ("job_publish_prepare", ("发布准备", "岗位发布准备", "准备发布", "发布草稿", "上架准备")),
        ("candidate_assessment", ("评估", "匹配", "判断", "合不合适", "适配", "推荐吗")),
        ("verification_plan", ("核验", "验证", "缺什么", "待核验", "核实", "问题清单")),
        ("communication_draft", ("草稿", "怎么联系", "沟通话术", "怎么聊", "私聊话术")),
        ("resume_export", ("导出简历", "简历导出", "结构化简历", "简历文档")),
        ("candidate_batch_assessment", ("批量评估", "批量判断", "批量匹配", "评估这一批")),
        ("candidate_pool_filter", ("过滤", "筛选", "名单", "分级", "重新过滤", "输出名单", "给名单", "把名单", "期望", "薪资上限", "只要.*万", "只要.*k", "江浙沪", "城市")),
        ("outreach_queue", ("转成触达队列", "触达队列", "排触达", "触达优先级", "P0队列", "按P0", "按P1")),
        ("pool_gap_advice", ("补池", "去哪补", "缺口", "目标公司", "补人", "还差哪些公司")),
        ("matching_report", ("匹配报告", "人岗匹配报告", "匹配分析", "人岗分析")),
        ("recommendation_report", ("推荐报告", "嘉驰推荐", "推荐材料", "候选人报告")),
        ("reply_triage", ("回复识别", "回复分流", "回复待办", "回复处理", "回复 triage")),
        ("communication_draft_batch", ("批量草稿", "批量话术", "批量沟通", "草稿这一批")),
        ("outreach_prepare", ("触达准备", "准备触达", "锁定触达", "触达草稿", "外呼准备")),
        ("interview_followup", ("面试反馈", "面试跟进", "面试纪要", "客户反馈")),
        ("salary_verification", ("薪资核验", "薪资验证", "薪资报告", "薪资证明")),
        ("salary_negotiation", ("谈薪", "薪资谈判", "谈薪风险", "薪资风险")),
        ("decision_coaching", ("决策辅导", "候选人决策", "决策建议", "offer决策")),
        ("onboarding_followup", ("入职跟进", "onboarding", "入职计划", "入职事项")),
        ("project_retrospective", ("项目复盘", "复盘", "结案总结", "项目总结")),
    )
    for skill_id, tokens in direct_rules:
        if _contains_any(message, tokens):
            add(skill_id)
    return routes[: max(1, int(self.config["runtime"]["copilot_max_skills"]))]
