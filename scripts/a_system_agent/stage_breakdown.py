"""评估队列的阶段分解归因（「为什么这批人还没动」口径）。

叶子模块（无包内依赖），供 assessment 读取/执行路径共同使用：
把评估队列人选按当前阶段（clean_stage/raw_status）分解为互斥归因桶，
已 H5 停止/淘汰的人选显式计入「已分流」而不是「没动」。
"""

from __future__ import annotations
from typing import Any

# 阶段停止/触达口径与名单卡（copilot_intent._STOP_TOKENS）、周报漏斗
# （asa_core.job_weekly_report._is_stopped/_CONTACTED_TOKENS）逐词一致：
# 「为什么没动」类归因直接复用同一套计数，不另造口径。
_STAGE_STOP_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")
_STAGE_STOP_STATUSES = {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
_STAGE_CONTACTED_TOKENS = ("已触达", "已联系", "已沟通", "已推荐", "面试", "Offer")

# 归因桶（互斥，按优先级归桶）：已分流停止 / 触达待核验 / 已触达待回 / 待复核 / 其他在途。
STAGE_BREAKDOWN_LABELS = {
    "stopped": "已分流停止",
    "verification": "触达待核验",
    "contacted": "已触达待回",
    "pending_review": "待复核",
    "other_active": "其他在途",
}


def assessed_stage_breakdown(assessed_items: list[dict[str, Any]]) -> dict[str, int]:
    """把评估队列人选按当前阶段（clean_stage/raw_status）分解为互斥归因桶。

    已 H5 停止/淘汰的人选计入 stopped（已分流），不算「没动」；
    桶计数之和恒等于 len(assessed_items)，与名单/漏斗同口径。
    """
    breakdown = {key: 0 for key in STAGE_BREAKDOWN_LABELS}
    for item in assessed_items or []:
        stage = str(item.get("current_stage") or item.get("clean_stage") or "")
        raw_status = str(item.get("raw_status") or "").strip().lower()
        recommendation = str(item.get("recommendation") or "")
        if any(token in stage for token in _STAGE_STOP_TOKENS) or raw_status in _STAGE_STOP_STATUSES:
            breakdown["stopped"] += 1
        elif "待核验" in stage or recommendation == "verify_first":
            breakdown["verification"] += 1
        elif any(token in stage for token in _STAGE_CONTACTED_TOKENS):
            breakdown["contacted"] += 1
        elif stage.startswith(("H1 ", "X1 ", "S1 ")) or "待复核" in stage or "待筛" in stage:
            breakdown["pending_review"] += 1
        else:
            breakdown["other_active"] += 1
    return breakdown


def assessed_stage_summary(assessed_items: list[dict[str, Any]]) -> str:
    """阶段分解的一句话摘要，供「为什么这批人还没动」类归因直接引用。"""
    items = list(assessed_items or [])
    if not items:
        return ""
    breakdown = assessed_stage_breakdown(items)
    parts = [
        f"{STAGE_BREAKDOWN_LABELS[key]} {breakdown[key]}"
        for key in ("stopped", "verification", "contacted", "pending_review", "other_active")
        if breakdown[key]
    ]
    summary = f"阶段分布（共 {len(items)} 人）：{' · '.join(parts)}。"
    if breakdown["stopped"]:
        summary += "已分流停止的人选是已淘汰/已停止，回答「为什么没动」时应计为已分流，而非未推进。"
    return summary
