"""停止原因标准化（PRD 阶段 4 R10）。

统一枚举以 X-SaaS 候选人助手扩展契约为基准
（xsaas-candidate-assistant-extension/content.js STOP_REASON_OPTIONS），
中文标签按 PRD 定稿。Core commit 与 legacy talent-action 两条写路径共用本模块，
保证落库值一致。
"""

from __future__ import annotations

from typing import Any

STOP_REASON_LABELS: dict[str, str] = {
    "too_senior": "资历过高",
    "salary_mismatch": "薪资不符",
    "direction_mismatch": "方向不符",
    "experience_mismatch": "经验不符",
    "location_mismatch": "地点不符",
    "low_intent": "意向不足",
    "duplicate_candidate": "重复人选",
    "other": "其他",
}

# 历史数据（stop_reason 列为 NULL）在统计口径里的单独标签。
UNLABELED_STOP_REASON_LABEL = "未标注"

# 兼容别名：A 系统行内复核（build_talent_workbench 快捷原因）旧码 → 统一枚举。
# 该表面的下拉对齐由后续任务完成；对齐后别名保留无副作用。
STOP_REASON_ALIASES: dict[str, str] = {
    "salary_high": "salary_mismatch",
    "duplicate": "duplicate_candidate",
}


def normalize_stop_reason(reason: Any, note: str) -> tuple[str, str]:
    """把调用方给的停止原因归一到 8 枚举。

    - reason 命中枚举（大小写/首尾空白不敏感，含旧码别名）→ 存枚举值，备注原样返回；
    - reason 缺失、未知值或自由文本 → 存 'other'，原文并入备注保留（不报错阻断）。
    """
    text = str(reason or "").strip()
    code = STOP_REASON_ALIASES.get(text.lower(), text.lower())
    if code in STOP_REASON_LABELS:
        return code, note
    if text:
        merged = f"停止原因：{text}"
        note = f"{note}｜{merged}" if note else merged
    return "other", note
