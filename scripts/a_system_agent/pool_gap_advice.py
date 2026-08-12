"""缺口补池建议（pool_gap_advice capability）。

基于候选池分级过滤结果，对照精密设备目标公司清单，给出定向寻访的
缺口补池建议：对现有候选池中未出现的目标公司输出 suggested 建议，
若 A 级（A-核心 + A-强）总量不足则追加一条 priority 优先级提示，
并遵循禁挖名单（banned 关键词命中的目标公司一律不输出）。
"""

from __future__ import annotations
from typing import Any

# 精密设备 Tier1 目标公司清单（name=公司关键词, reason=建议补挖理由）
DEFAULT_TARGET_COMPANIES: list[dict[str, str]] = [
    {"name": "华卓精科", "reason": "精密运动台/气浮平台定位200nm级, 光刻机核心部件"},
    {"name": "上海微电子", "reason": "光刻机整机机械设计"},
    {"name": "北方华创", "reason": "刻蚀/薄膜/清洗设备整机"},
    {"name": "中微", "reason": "刻蚀设备整机机械"},
    {"name": "华海清科", "reason": "CMP设备机械设计"},
    {"name": "芯源微", "reason": "涂胶显影设备精密运动"},
    {"name": "盛美", "reason": "清洗设备机械设计"},
    {"name": "屹唐", "reason": "去胶/退火设备结构"},
    {"name": "京仪", "reason": "光伏/半导体设备机械"},
    {"name": "富创精密", "reason": "半导体设备精密零部件"},
    {"name": "雅科贝思", "reason": "直线电机/精密运动部件"},
    {"name": "海德汉", "reason": "光栅尺/编码器精密部件"},
    {"name": "THK", "reason": "导轨/丝杠精密部件"},
    {"name": "上银", "reason": "导轨/丝杠精密部件"},
    {"name": "NSK", "reason": "轴承/丝杠精密部件"},
]

# A 级（A-核心 + A-强）充足下限
MIN_A_COUNT = 30


def suggest_pool_gap(
    result: dict[str, Any],
    banned: list[str] | None = None,
    target_companies: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """对照目标公司清单分析候选池缺口，返回补池建议列表。

    参数:
        result: 分级过滤结果 dict，须含 candidates 列表（每项有 company/grade 字段）。
        banned: 禁挖公司关键词列表，命中的目标公司不输出建议。
        target_companies: 目标公司清单，默认用 DEFAULT_TARGET_COMPANIES。
    """
    banned = [str(b or "").strip() for b in (banned or []) if str(b or "").strip()]
    targets = list(target_companies) if target_companies else DEFAULT_TARGET_COMPANIES

    seen = {str(c.get("company") or "").strip() for c in (result.get("candidates") or [])}
    a_count = sum(
        1
        for c in (result.get("candidates") or [])
        if str(c.get("grade") or "").startswith("A-")
    )

    advice: list[dict[str, str]] = []
    for t in targets:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        if any(b in name for b in banned):
            continue
        if any(name in co for co in seen):
            continue
        advice.append({
            "company": name,
            "reason": str(t.get("reason") or ""),
            "status": "suggested",
        })

    if a_count < MIN_A_COUNT:
        advice.append({
            "company": "",
            "reason": f"当前A级仅{a_count}人,建议优先从精密设备Tier1定向寻访",
            "status": "priority",
        })

    return advice
