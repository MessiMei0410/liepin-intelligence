"""S4-3：策略复盘器 v1（规则版）—— 每轮寻访收尾后生成结构化复盘。

口径来源：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §5（复盘器，v1 规则版，不上模型）。

输入 = 工作流（含 strategy_v2 artifact）+ 当轮 agent_sourcing_funnel 行 + 评估结果
（agent_candidate_assessments 派生的 sourcing_target_stats）。

判定分支决策表（按优先级从上到下，首个命中即定论；阈值均可配置，括号内为缺省）：

| 序 | 条件 | verdict | 含义 |
| -- | ---- | ------- | ---- |
| 1 | 无 strategy_v2 策略对象 / 无漏斗行 | insufficient_data | 数据不足，不硬判（附可得证据说明） |
| 2 | 任一渠道 zero_attribution ∈ {session_expired, page_structure_changed,
    loading_incomplete}，或聚合 detail_failed 占比 ≥ 阈值（0.30） | execution_channel_issue | 执行/渠道问题，不改策略 |
| 3 | 召回总量 < step5_expectation 总量 × 阈值（0.50，预期为 0 时跳过） | strategy_too_narrow | 策略问题：关键词/目标池太窄 |
| 4 | 入库/评估正常但高分率 < 阈值（0.15） | quality_gap | 画像偏差（策略）或评分偏差（转评估问题单） |
| 5 | 以上均未命中 | healthy | 本轮策略与执行均在预期内 |

序 2 优先于序 3：渠道阻塞/页面结构变化本身会造成召回短收，此时低召回是执行问题的
结果而非策略问题，不误判策略（PRD §5“执行/渠道，不改策略”口径）。

输出：{verdict, per_channel_findings, revision_diff}（+ evidence/escalation/thresholds）。
revision_diff 为 strategy_v2 的 diff 形式，每条带 reason 且可逐项采纳/拒绝
（status: pending → accepted/rejected，结构上支持）：
- {"step":"step2_target_pool","op":"add","tier":"T2","companies":["X","Y"],"reason":"..."}
- {"step":"step4_keyword_groups","op":"replace","group":"...","terms":["..."],"reason":"..."}
- {"step":"step1_job_essence","op":"review","reason":"..."}（quality_gap 的画像复核建议）

持久化：artifact_type='strategy_review' 落 agent_artifacts；同一工作流重算幂等覆盖
（更新 content/metadata，version 自增，历次判定摘要收入 history，上限 10 条）。
触发点：a) workflow 终局 _finish() 后对寻访类工作流自动生成；b) 按需 rebuild
（存量终局工作流补生成，终局=completed/blocked/failed）。

S4-3c：顾问逐项采纳/拒绝（apply_diff_decisions）—— status 写回 revision_diff
（upsert 可重复覆盖，不 bump version），同一写动作追加 strategy_v2.consultant_edits
（strategy_v2 缺失时降级跳过）并写 explicit_corrections 学习信号（strategy_corrections 表）。

S4-3c-3（N3）：池枯竭信号 + 扩池决策树。
- 轮次级 dedupe_rate = Σdedupe_count / Σextracted_count（聚合当轮全部漏斗行，extracted=0 不计）。
  dedupe_rate > 阈值（0.80，可配置）触发 pool_saturated 信号：写入 review.signals 数组，
  并在 verdict_reason 追加决策依据。信号与四判定分支正交——verdict 仍按原分支产出
  （healthy/quality_gap/strategy_too_narrow/execution_channel_issue 均可同时携带该信号），
  信号触发时强制产出扩池决策树。
- 口径分层：本信号是复盘器轮次级聚合（>80%），区别于 capability_runtime 的渠道级 0 归因
  zero_attribution=pool_saturated（>90%，写 agent_sourcing_funnel 行）；两者并存、语义不同层。
- 扩池决策树 expansion_decision_tree：固定 5 步有序输出
  （swap_keywords 换关键词组 → expand_pool 扩 T2/T3 池 → relax_condition 放宽年限/职级/地点
  → rebalance_channel 渠道再平衡 → escalate_mapping 转 Mapping/与客户校准）。
  每步 {step_id, order, action_type, title, detail, params, status:pending}，可逐项采纳。
  params 只取 strategy_v2 / 漏斗行 / 岗位原型（archetype）的真实值；取不到留空并在 notes 说明，
  禁止编造。与 revision_diff 的分工：diff 是对 strategy_v2 的逐条修订（点状），树是池枯竭后的
  有序行动路径（面状），两者可并存于同一复盘。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import knowledge_base, strategy_v2

REVIEW_SCHEMA_VERSION = "strategy_review_v1"
ARTIFACT_TYPE = "strategy_review"
GENERATOR = "rule_v1"

DEFAULT_RECALL_SHORTFALL_RATIO = 0.5
DEFAULT_DETAIL_FAILED_RATIO = 0.3
DEFAULT_HIGH_SCORE_THRESHOLD = 0.15
# N3 池枯竭信号阈值：轮次级 dedupe_rate = Σdedupe_count/Σextracted_count 超过即触发
# （F3 实证：猎聘 119/120=99% 排重仍原样重搜，系统无信号）。阈值可配置。
DEFAULT_POOL_SATURATED_DEDUPE_RATE = 0.8
POOL_SATURATED_SIGNAL = "pool_saturated"

# 扩池决策树：固定 5 步 action_type（顺序即执行优先级）
EXPANSION_ACTION_TYPES = ("swap_keywords", "expand_pool", "relax_condition", "rebalance_channel", "escalate_mapping")
# 层级 → 原型 target_company_pool 键 / 中文名（与 strategy_v2._kb_pool 的映射口径一致）
_TIER_POOL_KEYS = {"T1": "T1_competitor_device", "T2": "T2_customer_OEM", "T3": "T3_adjacent_unconfirmed"}
_TIER_LABELS = {"T1": "T1 同层友商", "T2": "T2 客户整机厂", "T3": "T3 相邻池"}

# 执行/渠道类 0 召回归因（PRD §5：命中即执行问题，不改策略）
EXECUTION_ZERO_ATTRIBUTIONS = ("session_expired", "page_structure_changed", "loading_incomplete")

VERDICTS = ("strategy_too_narrow", "execution_channel_issue", "quality_gap", "insufficient_data", "healthy")
VERDICT_LABELS = {
    "strategy_too_narrow": "策略问题：关键词/目标池太窄",
    "execution_channel_issue": "执行/渠道问题（不改策略）",
    "quality_gap": "高分率偏低：画像偏差（策略）或评分偏差（评估）",
    "insufficient_data": "数据不足，不硬判",
    "healthy": "本轮策略与执行均在预期内",
}

# 复盘只覆盖寻访类工作流的终局状态
TERMINAL_STATUSES = ("completed", "blocked", "failed")

_HISTORY_LIMIT = 10


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _expected_recall_total(strategy_doc: dict[str, Any]) -> int:
    expected = (strategy_doc.get("step5_expectation") or {}).get("expected_recall_per_tier")
    if not isinstance(expected, dict):
        return 0
    return sum(_int(value) for value in expected.values())


# ---------------------------------------------------------------------------
# S4-3c-3（N3）：扩池决策树（纯函数；params 只取真实值，取不到留空 + notes）
# ---------------------------------------------------------------------------

def _tree_step(order: int, action_type: str, title: str, detail: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "step_id": f"exp-{order}",
        "order": order,
        "action_type": action_type,
        "title": title,
        "detail": detail,
        "params": params,
        "status": "pending",
    }


def _group_view(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "group": str(group.get("group") or ""),
        "targets": str(group.get("targets") or ""),
        "terms": [str(term) for term in group.get("terms") or [] if str(term or "").strip()],
    }


def build_expansion_decision_tree(
    *,
    strategy_doc: dict[str, Any] | None,
    funnel_rows: list[dict[str, Any]],
    archetype: dict[str, Any] | None = None,
    dedupe_rate: float,
    dedupe_total: int,
    extracted_total: int,
    threshold: float = DEFAULT_POOL_SATURATED_DEDUPE_RATE,
) -> tuple[list[dict[str, Any]], list[str]]:
    """池枯竭后的有序行动路径（固定 5 步，order 即执行优先级）。

    params 来源（只取真实值，取不到留空并由返回的 notes 说明，禁止编造）：
    1 swap_keywords     —— strategy_v2.step4_keyword_groups（当前组）+ 原型 keyword_groups（候选组）
    2 expand_pool       —— strategy_v2.step2_target_pool（已用层）+ 原型 target_company_pool（下一层公司）
    3 relax_condition   —— step3_level_mapping（职级）/ 原型 location_policy（地点）；年限无来源留空
    4 rebalance_channel —— 当轮漏斗行（入库/去重转化率）
    5 escalate_mapping  —— 升级项（转 Mapping 直挖 / 与客户校准方向），仅需排重证据
    """
    notes: list[str] = []
    has_strategy = isinstance(strategy_doc, dict) and bool(strategy_doc)
    strategy_doc = strategy_doc if has_strategy else {}
    archetype = archetype if isinstance(archetype, dict) else {}
    rate_text = f"排重率 {dedupe_rate:.0%}（{dedupe_total}/{extracted_total}）> 阈值 {threshold:.0%}"

    # ---- 1. 换关键词组（同池不同词）----
    current_groups = [
        _group_view(group)
        for group in strategy_doc.get("step4_keyword_groups") or []
        if isinstance(group, dict) and (group.get("terms") or [])
    ]
    current_names = {group["group"] for group in current_groups}
    candidate_groups = [
        _group_view(group)
        for group in archetype.get("keyword_groups") or []
        if isinstance(group, dict)
        and (group.get("terms") or [])
        and str(group.get("group") or "") not in current_names
    ]
    if not current_groups:
        notes.append("换词步：strategy_v2 无 step4 关键词组，当前组留空，待顾问补充")
    if not candidate_groups:
        notes.append("换词步：知识库原型无更多候选关键词组，候选组留空，待顾问补充")
    step1 = _tree_step(
        1, "swap_keywords", "换关键词组（同池不同词）",
        f"{rate_text}：当前词组覆盖的人选已见完。在同一目标池内轮换词组视角（技术词↔公司词↔职能词），"
        f"当前 {len(current_groups)} 组、候选 {len(candidate_groups)} 组，逐组替换重搜。",
        {"current_groups": current_groups, "candidate_groups": candidate_groups,
         "rotation": "技术词↔公司词↔职能词（同池不同词）"},
    )

    # ---- 2. 扩池（T1→T2 客户整机厂→T3 相邻池）----
    step2_entries = [entry for entry in strategy_doc.get("step2_target_pool") or [] if isinstance(entry, dict)]
    tiers_in_use = [
        tier for tier in dict.fromkeys(str(entry.get("tier") or "") for entry in step2_entries) if tier
    ]
    existing_names = {
        str(company.get("name") or "")
        for entry in step2_entries
        for company in entry.get("companies") or []
        if isinstance(company, dict)
    }
    next_tier = next((tier for tier in ("T1", "T2", "T3") if tier not in tiers_in_use), None)
    pool = archetype.get("target_company_pool") or {}
    block = pool.get(_TIER_POOL_KEYS.get(next_tier or "", ""), {}) if next_tier else {}
    block = block if isinstance(block, dict) else {}
    companies = [
        name
        for company in block.get("companies") or []
        if isinstance(company, dict)
        for name in [str(company.get("name") or "").strip()]
        if name and name not in existing_names
    ]
    source_archetype = ""
    if archetype:
        source_archetype = str(archetype.get("archetype_id") or "")
        if str(archetype.get("source_file") or ""):
            source_archetype = f"{source_archetype}（{archetype.get('source_file')}）"
    if next_tier is None:
        notes.append("扩池步：T1/T2/T3 均已入池，无下一层可扩，待知识资产更新或顾问指定新池")
    elif not companies:
        notes.append(f"扩池步：原型 {_TIER_LABELS.get(next_tier, next_tier)} 无可用新公司（缺失或均已在池），公司列表留空，待顾问补充")
    step2 = _tree_step(
        2, "expand_pool", f"扩池：向 {_TIER_LABELS.get(next_tier, '下一层')} 扩展" if next_tier else "扩池：T1/T2/T3 均已入池",
        (
            f"按 T1→T2（客户整机厂）→T3（相邻池）顺序扩池。当前已用层：{'、'.join(tiers_in_use) or '（无）'}；"
            f"下一层 {_TIER_LABELS.get(next_tier, next_tier)}，来源原型 {source_archetype or '（无）'}，"
            f"可新增公司 {len(companies)} 家。"
            if next_tier else
            "T1/T2/T3 三层均已入池，池内扩展已尽，优先执行后续步骤或补充新知识资产。"
        ),
        {"current_tiers": tiers_in_use, "next_tier": next_tier,
         "tier_label": _TIER_LABELS.get(next_tier, "") if next_tier else "",
         "companies": companies, "rationale": str(block.get("rationale") or ""),
         "source_archetype": source_archetype},
    )

    # ---- 3. 放宽条件（年限/职级/地点，逐项 + 代价）----
    step3_mapping = strategy_doc.get("step3_level_mapping") or {}
    levels = [str(level) for level in step3_mapping.get("accepted_levels") or [] if str(level or "").strip()]
    location_policy = str(archetype.get("location_policy") or "")
    items = [
        {
            "field": "年限",
            "current": None,
            "proposal": "",
            "cost": "",
            "source": "none",
            "note": "strategy_v2 未记录年限门槛，当前值取不到；放宽幅度由顾问定",
        },
        {
            "field": "职级",
            "current": levels or None,
            "proposal": "在 accepted_levels 基础上放宽一档（纳入相邻职级）" if levels else "",
            "cost": "层级偏低人选增多，定档口径须同步复核，评估筛选成本上升" if levels else "",
            "source": "step3_level_mapping" if levels else "none",
            "note": "" if levels else "strategy_v2 无 step3 定档记录，当前职级取不到",
        },
        {
            "field": "地点",
            "current": location_policy or None,
            "proposal": "从地点优先策略放宽为全国/周边城市" if location_policy else "",
            "cost": "人选迁移意愿与到岗率下降，offer 谈判周期变长" if location_policy else "",
            "source": "archetype.location_policy" if location_policy else "none",
            "note": "" if location_policy else "原型未定义地点策略，当前值取不到",
        },
    ]
    step3 = _tree_step(
        3, "relax_condition", "放宽条件（年限/职级/地点，逐项记录代价）",
        "逐项放宽准入条件并记录代价，顾问逐项确认。边界：只放宽年限/职级/地点，"
        "不涉及禁挖名单/竞业限制等 restricted 约束，负向规则不放宽。",
        {"items": items, "boundary": "不涉及禁挖名单/竞业等 restricted 约束，负向规则不放宽"},
    )

    # ---- 4. 渠道再平衡（高效渠道倾斜，引用漏斗转化率）----
    channel_stats: list[dict[str, Any]] = []
    for row in funnel_rows:
        unique = _int(row.get("unique_count"))
        intake = _int(row.get("intake_new_count"))
        channel_stats.append(
            {
                "channel": str(row.get("channel") or ""),
                "recall_count": _int(row.get("recall_count")),
                "unique_count": unique,
                "intake_new_count": intake,
                "intake_conversion": round(intake / unique, 4) if unique else None,
            }
        )
    best = max(
        (stat for stat in channel_stats if stat["intake_new_count"] > 0),
        key=lambda stat: stat["intake_conversion"] or 0,
        default=None,
    )
    if best is None:
        notes.append("渠道再平衡步：本轮各渠道均无入库转化，无可倾斜的高效渠道，优先执行前 3 步")
    stats_text = "；".join(
        f"{stat['channel']} 入库/去重 {stat['intake_new_count']}/{stat['unique_count']}"
        + (f"（{stat['intake_conversion']:.0%}）" if stat["intake_conversion"] is not None else "")
        for stat in channel_stats
    ) or "（无漏斗行）"
    step4 = _tree_step(
        4, "rebalance_channel", "渠道再平衡（向高效渠道倾斜）",
        f"按本轮漏斗转化率倾斜查询配额：{stats_text}。"
        + (f"建议向 {best['channel']} 倾斜。" if best else "暂无可倾斜渠道，先执行前 3 步。"),
        {"channel_stats": channel_stats,
         "recommended_channel": best["channel"] if best else None,
         "basis": "intake_new_count/unique_count（本轮漏斗转化率）"},
    )

    # ---- 5. 转 Mapping 直挖 / 与客户校准方向（升级项）----
    step5 = _tree_step(
        5, "escalate_mapping", "转 Mapping 直挖 / 与客户校准方向（升级项）",
        f"{rate_text}：本地池与渠道池均已尽，继续原样重搜无增量。建议转 Mapping 直挖"
        "（按目标公司组织架构定点挖人），或与客户校准寻访方向（岗位本质/目标池是否需重新定义）。"
        "本步为升级项，须顾问决策后执行。",
        {"actions": ["mapping_direct_sourcing", "client_direction_calibration"],
         "reason": f"{rate_text}，本地池+渠道池已尽"},
    )

    return [step1, step2, step3, step4, step5], notes


def build_strategy_review(
    *,
    workflow_id: str,
    strategy_doc: dict[str, Any] | None,
    funnel_rows: list[dict[str, Any]],
    assessment: dict[str, Any] | None = None,
    pool_candidates: list[str] | None = None,
    keyword_candidates: list[str] | None = None,
    archetype: dict[str, Any] | None = None,
    recall_shortfall_ratio: float = DEFAULT_RECALL_SHORTFALL_RATIO,
    detail_failed_ratio_threshold: float = DEFAULT_DETAIL_FAILED_RATIO,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    dedupe_rate_threshold: float = DEFAULT_POOL_SATURATED_DEDUPE_RATE,
) -> dict[str, Any]:
    """规则版复盘判定（纯函数，不触碰 DB/KB）。返回复盘对象（不含 version/history）。"""
    thresholds = {
        "recall_shortfall_ratio": recall_shortfall_ratio,
        "detail_failed_ratio": detail_failed_ratio_threshold,
        "high_score_rate": high_score_threshold,
        "pool_saturated_dedupe_rate": dedupe_rate_threshold,
    }
    pool_candidates = [str(name) for name in pool_candidates or [] if str(name or "").strip()]
    keyword_candidates = [str(term) for term in keyword_candidates or [] if str(term or "").strip()]

    # ---- 证据汇总 ----
    has_strategy = isinstance(strategy_doc, dict) and bool(strategy_doc)
    expected_total = _expected_recall_total(strategy_doc) if has_strategy else 0
    recall_total = sum(_int(row.get("recall_count")) for row in funnel_rows)
    detail_complete = sum(_int(row.get("detail_complete")) for row in funnel_rows)
    detail_partial = sum(_int(row.get("detail_partial")) for row in funnel_rows)
    detail_failed = sum(_int(row.get("detail_failed")) for row in funnel_rows)
    detail_total = detail_complete + detail_partial + detail_failed
    intake_new_total = sum(_int(row.get("intake_new_count")) for row in funnel_rows)
    assessed_total = sum(_int(row.get("assessed_count")) for row in funnel_rows)
    high_total = sum(_int(row.get("high_score_count")) for row in funnel_rows)
    assessment_source = "funnel"
    if assessed_total == 0 and isinstance(assessment, dict) and _int(assessment.get("assessed")) > 0:
        assessed_total = _int(assessment.get("assessed"))
        high_total = _int(assessment.get("score_75_plus"))
        assessment_source = "assessment_table"
    high_rate = round(high_total / assessed_total, 4) if assessed_total else None
    detail_failed_ratio = round(detail_failed / detail_total, 4) if detail_total else None

    # ---- N3 池枯竭信号（轮次级）：dedupe_rate = Σdedupe_count/Σextracted_count，extracted=0 不计 ----
    extracted_total = sum(_int(row.get("extracted_count")) for row in funnel_rows)
    dedupe_total = sum(_int(row.get("dedupe_count")) for row in funnel_rows)
    dedupe_rate = round(dedupe_total / extracted_total, 4) if extracted_total > 0 else None
    pool_saturated = dedupe_rate is not None and dedupe_rate > dedupe_rate_threshold

    # ---- 逐渠道发现 ----
    per_channel: list[dict[str, Any]] = []
    execution_channels: list[str] = []
    for row in funnel_rows:
        channel = str(row.get("channel") or "")
        recall = _int(row.get("recall_count"))
        assessed = _int(row.get("assessed_count"))
        high = _int(row.get("high_score_count"))
        dc, dp, df = _int(row.get("detail_complete")), _int(row.get("detail_partial")), _int(row.get("detail_failed"))
        row_detail_total = dc + dp + df
        row_failed_ratio = round(df / row_detail_total, 4) if row_detail_total else None
        attribution = str(row.get("zero_attribution") or "") or None
        finding = "ok"
        note = "渠道各环节未见异常"
        if attribution in EXECUTION_ZERO_ATTRIBUTIONS:
            finding = "execution_issue"
            note = f"0 召回归因 {attribution}（执行/渠道类）"
            execution_channels.append(channel)
        elif row_failed_ratio is not None and row_failed_ratio >= detail_failed_ratio_threshold:
            finding = "execution_issue"
            note = f"详情失败占比 {row_failed_ratio:.0%}（≥ 阈值 {detail_failed_ratio_threshold:.0%}）"
            execution_channels.append(channel)
        elif str(row.get("status") or "") in {"blocked", "failed"}:
            finding = "execution_issue"
            note = f"渠道状态 {row.get('status')}（未正常完成）"
            execution_channels.append(channel)
        elif recall == 0:
            finding = "zero_recall"
            note = "渠道 0 召回且非执行类归因"
        elif assessed >= 5 and high / assessed < high_score_threshold:
            finding = "low_high_rate"
            note = f"渠道高分率 {high}/{assessed} 低于阈值 {high_score_threshold:.0%}"
        per_channel.append(
            {
                "channel": channel,
                "status": str(row.get("status") or ""),
                "recall_count": recall,
                "unique_count": _int(row.get("unique_count")),
                "intake_new_count": _int(row.get("intake_new_count")),
                "assessed_count": assessed,
                "high_score_count": high,
                "detail_complete": dc,
                "detail_partial": dp,
                "detail_failed": df,
                "detail_failed_ratio": row_failed_ratio,
                "zero_attribution": attribution,
                "finding": finding,
                "note": note,
            }
        )
    aggregate_detail_issue = (
        detail_failed_ratio is not None and detail_failed_ratio >= detail_failed_ratio_threshold
    )

    evidence = {
        "has_strategy_v2": has_strategy,
        "funnel_channels": len(funnel_rows),
        "expected_recall_total": expected_total,
        "recall_total": recall_total,
        "detail_total": detail_total,
        "detail_failed_total": detail_failed,
        "detail_failed_ratio": detail_failed_ratio,
        "intake_new_total": intake_new_total,
        "assessed_total": assessed_total,
        "high_score_total": high_total,
        "high_score_rate": high_rate,
        "assessment_source": assessment_source,
        "extracted_total": extracted_total,
        "dedupe_total": dedupe_total,
        "dedupe_rate": dedupe_rate,
    }

    # ---- 判定（分支决策表见模块 docstring）----
    notes: list[str] = []
    degraded = False
    if not has_strategy:
        verdict = "insufficient_data"
        verdict_reason = "无 strategy_v2 策略对象，无法对照预期与产出修订建议，不硬判"
        degraded = bool(funnel_rows)
        if funnel_rows:
            notes.append("存在渠道漏斗行但缺策略对象，仅保留执行证据不做策略判定")
    elif not funnel_rows:
        verdict = "insufficient_data"
        verdict_reason = "该轮未记录渠道漏斗明细，无法判定策略/执行归因，不硬判"
        if isinstance(assessment, dict) and _int(assessment.get("assessed")) > 0:
            degraded = True
            notes.append(
                f"可得证据（评估表）：评估 {assessed_total} 人、高分 {high_total} 人"
                "；仅作参考，不足以支撑策略/执行归因"
            )
    elif execution_channels:
        verdict = "execution_channel_issue"
        reasons = [item["note"] for item in per_channel if item["finding"] == "execution_issue"]
        if aggregate_detail_issue and not any("详情失败" in reason for reason in reasons):
            reasons.append(f"聚合详情失败占比 {detail_failed_ratio:.0%}")
        verdict_reason = "；".join(dict.fromkeys(reasons)) + "；按 PRD §5 判定为执行/渠道问题，不改策略"
        notes.append("策略对象保持不变，revision_diff 为空；请先恢复渠道后重跑再复盘")
    elif expected_total > 0 and recall_total < expected_total * recall_shortfall_ratio:
        verdict = "strategy_too_narrow"
        verdict_reason = (
            f"本轮总召回 {recall_total} < step5 预期总量 {expected_total} 的 {recall_shortfall_ratio:.0%}"
            f"（{round(expected_total * recall_shortfall_ratio, 1)}），判定策略问题：关键词/目标池太窄"
        )
    elif expected_total == 0:
        notes.append("策略 step5 未定义召回预期数值，跳过召回量判定")
        verdict = ""
        verdict_reason = ""
    else:
        verdict = ""
        verdict_reason = ""

    if verdict == "":
        if assessed_total > 0 and high_rate is not None and high_rate < high_score_threshold:
            verdict = "quality_gap"
            verdict_reason = (
                f"入库/评估正常（入库 {intake_new_total}、评估 {assessed_total}），"
                f"但高分率 {high_total}/{assessed_total}={high_rate:.0%} < 阈值 {high_score_threshold:.0%}"
                "；疑似画像偏差（策略）或评分偏差（评估，转评估问题单）"
            )
        else:
            verdict = "healthy"
            verdict_reason = "召回、详情、入库与高分率均在预期内，无需修订"

    # ---- N3 池枯竭信号与扩池决策树（与 verdict 正交：信号触发即强制产出决策树）----
    signals: list[dict[str, Any]] = []
    expansion_decision_tree: list[dict[str, Any]] = []
    if pool_saturated:
        saturated_channels = [
            {
                "channel": str(row.get("channel") or ""),
                "extracted_count": _int(row.get("extracted_count")),
                "dedupe_count": _int(row.get("dedupe_count")),
                "dedupe_rate": (
                    round(_int(row.get("dedupe_count")) / _int(row.get("extracted_count")), 4)
                    if _int(row.get("extracted_count")) > 0
                    else None
                ),
            }
            for row in funnel_rows
            if _int(row.get("extracted_count")) > 0
        ]
        signals.append(
            {
                "signal": POOL_SATURATED_SIGNAL,
                "label": "池枯竭（排重率过高）",
                "scope": "round",
                "dedupe_rate": dedupe_rate,
                "dedupe_count": dedupe_total,
                "extracted_count": extracted_total,
                "threshold": dedupe_rate_threshold,
                "channels": saturated_channels,
                "detail": (
                    f"本轮抽取 {extracted_total} 条、排重 {dedupe_total} 条，轮次排重率 {dedupe_rate:.1%}"
                    f" > 阈值 {dedupe_rate_threshold:.0%}：本地候选池已枯竭，须换打法而非原样重搜"
                ),
                "semantics": "轮次级复盘信号（>80%，可配置）；区别于渠道级 0 归因 zero_attribution=pool_saturated（>90%），两者并存、语义分层",
            }
        )
        verdict_reason += (
            f"；轮次信号 {POOL_SATURATED_SIGNAL}：排重率 {dedupe_rate:.0%}（{dedupe_total}/{extracted_total}）"
            f" > 阈值 {dedupe_rate_threshold:.0%}，本地池已枯竭，已生成扩池决策树（信号与判定正交）"
        )
        expansion_decision_tree, tree_notes = build_expansion_decision_tree(
            strategy_doc=strategy_doc,
            funnel_rows=funnel_rows,
            archetype=archetype,
            dedupe_rate=dedupe_rate,
            dedupe_total=dedupe_total,
            extracted_total=extracted_total,
            threshold=dedupe_rate_threshold,
        )
        notes.extend(tree_notes)

    # ---- 修订建议（strategy_v2 diff，逐项可采纳/拒绝）----
    revision_diff: list[dict[str, Any]] = []
    escalation: dict[str, Any] | None = None

    def _diff(step: str, op: str, reason: str, **payload: Any) -> dict[str, Any]:
        return {
            "diff_id": f"diff-{len(revision_diff) + 1}",
            "step": step,
            "op": op,
            **payload,
            "reason": reason,
            "status": "pending",
        }

    if verdict == "strategy_too_narrow" and has_strategy:
        existing_names = {
            str(company.get("name") or "")
            for entry in strategy_doc.get("step2_target_pool") or []
            for company in (entry.get("companies") or [])
            if isinstance(company, dict)
        }
        new_companies = [name for name in dict.fromkeys(pool_candidates) if name not in existing_names][:6]
        if new_companies:
            tiers = {str(entry.get("tier") or "") for entry in strategy_doc.get("step2_target_pool") or []}
            tier = "T2" if "T2" not in tiers else "T3"
            revision_diff.append(
                _diff(
                    "step2_target_pool", "add",
                    f"召回 {recall_total} 不及预期 50%（{expected_total}），按 fallback_plan 放宽目标池：增列 {tier} 公司",
                    tier=tier, companies=new_companies,
                )
            )
        else:
            notes.append("知识库暂无可增补的目标池公司，step2 修订待顾问补充")
        groups = strategy_doc.get("step4_keyword_groups") or []
        existing_terms = {
            str(term)
            for group in groups
            for term in (group.get("terms") or [])
        }
        new_terms = [term for term in dict.fromkeys(keyword_candidates) if term not in existing_terms][:10]
        if groups and new_terms:
            target = groups[0]
            revision_diff.append(
                _diff(
                    "step4_keyword_groups", "replace",
                    f"关键词组“{target.get('group')}”召回不足，建议替换为更宽的知识库锚定词组",
                    group=str(target.get("group") or ""),
                    terms=new_terms,
                )
            )
        elif groups:
            notes.append("知识库暂无可替换的关键词候选，step4 修订待顾问补充")
        if not revision_diff:
            notes.append("策略过窄但知识库无可落地修订候选，请顾问人工放宽池/词")
    elif verdict == "quality_gap" and has_strategy:
        revision_diff.append(
            _diff(
                "step1_job_essence", "review",
                f"高分率 {high_rate:.0%} 低于阈值 {high_score_threshold:.0%}：建议复核岗位本质与 step3 定档口径"
                "（画像偏差方向），评分口径问题已转评估问题单",
            )
        )
        escalation = {
            "kind": "evaluation_issue_ticket",
            "target": "evaluation",
            "reason": f"入库正常但高分率 {high_total}/{assessed_total} 低于阈值 {high_score_threshold:.0%}，"
            "可能为评分偏差，转评估问题单复核评分口径",
            "status": "open",
        }

    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "generator": GENERATOR,
        "workflow_id": workflow_id,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "verdict_reason": verdict_reason,
        "degraded": degraded,
        "thresholds": thresholds,
        "evidence": evidence,
        "signals": signals,
        "expansion_decision_tree": expansion_decision_tree,
        "per_channel_findings": per_channel,
        "revision_diff": revision_diff,
        "escalation": escalation,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# DB 装配与持久化（全部只读输入 + 单表 upsert）
# ---------------------------------------------------------------------------

def _round_index(conn: Any, workflow_id: str, job_id: int) -> int:
    """轮次编号：按岗位工作流时间线（取消/被取代不计），与 Copilot 轮次口径一致。"""
    rows = conn.execute(
        """
        SELECT w.workflow_id FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
        WHERE g.context_type='job' AND g.context_id=? AND w.status NOT IN ('cancelled','superseded')
        ORDER BY w.created_at ASC, w.id ASC
        """,
        (job_id,),
    ).fetchall()
    for index, row in enumerate(rows, 1):
        if str(row["workflow_id"]) == workflow_id:
            return index
    return len(rows)


def _kb_revision_candidates(
    strategy_doc: dict[str, Any] | None, *, client: str
) -> tuple[list[str], list[str], list[str], dict[str, Any] | None]:
    """从 KB 推导 step2 公司/step4 关键词修订候选 + 命中原型（只读；异常一律降级为空并留痕）。

    返回 (pool_candidates, keyword_candidates, trace, archetype)；archetype 供 N3 扩池决策树
    取 T2/T3 池、候选关键词组、地点策略等真实值，推导失败为 None。
    """
    trace: list[str] = []
    if not isinstance(strategy_doc, dict) or not strategy_doc:
        return [], [], trace, None
    pool_candidates: list[str] = []
    keyword_candidates: list[str] = []
    archetype: dict[str, Any] | None = None
    try:
        archetype_id = str(strategy_doc.get("archetype_id") or "")
        archetypes, _ = strategy_v2.load_job_archetypes()
        archetype = next((item for item in archetypes if item.get("archetype_id") == archetype_id), None)
        if archetype is None and archetypes:
            archetype = archetypes[0]
        if archetype:
            pool = archetype.get("target_company_pool") or {}
            for block in pool.values():
                for company in (block or {}).get("companies") or []:
                    name = str(company.get("name") or "").strip()
                    if name:
                        pool_candidates.append(name)
            for group in archetype.get("keyword_groups") or []:
                for term in group.get("terms") or []:
                    if str(term or "").strip():
                        keyword_candidates.append(str(term))
        graph, _ = knowledge_base.load_company_graph()
        if graph:
            query_text = " ".join(
                part
                for part in (
                    str((strategy_doc.get("step1_job_essence") or {}).get("statement") or ""),
                    " ".join(
                        str(term)
                        for group in (strategy_doc.get("step4_keyword_groups") or [])[:3]
                        for term in (group.get("terms") or [])[:6]
                    ),
                )
                if part
            )
            client_norm = knowledge_base.normalize_client_name(client)
            for hit in knowledge_base.search_companies(graph, query_text=query_text, limit=6):
                name = str(hit.get("name") or "").strip()
                if name and not knowledge_base.name_match_rule(client, client_norm, name)[0]:
                    pool_candidates.append(name)
        if pool_candidates or keyword_candidates:
            trace.append(
                f"KB 修订候选：公司 {len(dict.fromkeys(pool_candidates))} 家、关键词 {len(dict.fromkeys(keyword_candidates))} 个"
            )
    except Exception as exc:  # KB 缺失/异常不阻塞复盘
        trace.append(f"KB 修订候选推导失败（{exc.__class__.__name__}），按无候选处理")
        pool_candidates, keyword_candidates, archetype = [], [], None
    return list(dict.fromkeys(pool_candidates)), list(dict.fromkeys(keyword_candidates)), trace, archetype


def generate_for_workflow(
    conn: Any,
    workflow_id: str,
    *,
    recall_shortfall_ratio: float = DEFAULT_RECALL_SHORTFALL_RATIO,
    detail_failed_ratio_threshold: float = DEFAULT_DETAIL_FAILED_RATIO,
    high_score_threshold: float = DEFAULT_HIGH_SCORE_THRESHOLD,
    dedupe_rate_threshold: float = DEFAULT_POOL_SATURATED_DEDUPE_RATE,
) -> dict[str, Any]:
    """从库中装配输入并生成复盘对象。工作流不存在抛 LookupError。"""
    row = conn.execute(
        """
        SELECT w.workflow_id,w.goal_id,w.status,w.business_outcome,
               g.objective,g.context_json,g.business_outcome AS goal_outcome
        FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
        WHERE w.workflow_id=?
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"工作流不存在：{workflow_id}")
    context = _loads(row["context_json"], {})
    job_id = _int(context.get("id")) if str(context.get("type") or "") == "job" else 0

    artifact = conn.execute(
        """
        SELECT metadata_json FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type='search_strategy' ORDER BY id DESC LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    strategy_doc = strategy_v2.extract_strategy_v2(artifact["metadata_json"]) if artifact else None

    funnel_rows = [
        {key: row[key] for key in row.keys()}
        for row in conn.execute(
            """
            SELECT channel,status,query_count,recall_count,extracted_count,dedupe_count,
                   unique_count,detail_complete,detail_partial,detail_failed,
                   intake_duplicate_count,intake_new_count,assessed_count,high_score_count,
                   zero_attribution,error
            FROM agent_sourcing_funnel WHERE workflow_id=? ORDER BY channel ASC, id ASC
            """,
            (workflow_id,),
        ).fetchall()
    ]

    # 评估结果（懒加载避免与 workflow 模块的循环引用）
    assessment: dict[str, Any] | None = None
    business_outcome = str(row["business_outcome"] or row["goal_outcome"] or "") or None
    try:
        from .workflow import classify_business_outcome, sourcing_target_stats

        assessment = sourcing_target_stats(conn, row["objective"], context, workflow_id)
        if not business_outcome:
            business_outcome = classify_business_outcome(conn, workflow_id)
    except Exception:
        assessment = None

    client, job = "", ""
    if funnel_rows:
        client = str(funnel_rows[0].get("client") or "")
        job = str(funnel_rows[0].get("job") or "")
    if not client and job_id:
        named = conn.execute(
            "SELECT c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if named:
            client, job = str(named["client"] or ""), str(named["job"] or "")

    pool_candidates, keyword_candidates, candidate_trace, archetype = _kb_revision_candidates(strategy_doc, client=client)

    review = build_strategy_review(
        workflow_id=workflow_id,
        strategy_doc=strategy_doc,
        funnel_rows=funnel_rows,
        assessment=assessment,
        pool_candidates=pool_candidates,
        keyword_candidates=keyword_candidates,
        archetype=archetype,
        recall_shortfall_ratio=recall_shortfall_ratio,
        detail_failed_ratio_threshold=detail_failed_ratio_threshold,
        high_score_threshold=high_score_threshold,
        dedupe_rate_threshold=dedupe_rate_threshold,
    )
    review.update(
        {
            "goal_id": str(row["goal_id"] or ""),
            "round_index": _round_index(conn, workflow_id, job_id) if job_id else 1,
            "job_id": job_id,
            "client": client,
            "job": job,
            "business_outcome": business_outcome,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    if candidate_trace:
        review["notes"] = [*(review.get("notes") or []), *candidate_trace]
    return review


def _review_content(review: dict[str, Any]) -> str:
    lines = [
        f"# 策略复盘（{review.get('generator')}）：{review.get('verdict_label')}",
        "",
        f"- 工作流：{review.get('workflow_id')}（第 {review.get('round_index')} 轮，{review.get('client')}｜{review.get('job')}）",
        f"- 判定：{review.get('verdict_reason')}",
    ]
    for finding in review.get("per_channel_findings") or []:
        lines.append(f"- 渠道 {finding.get('channel')}：{finding.get('note')}")
    for signal in review.get("signals") or []:
        lines.append(f"- 信号（{signal.get('signal')}）：{signal.get('detail')}")
    tree = review.get("expansion_decision_tree") or []
    if tree:
        lines.append("- 扩池决策树（按序执行，逐项可采纳/拒绝）：")
        for step in tree:
            lines.append(
                f"  {step.get('order')}. [{step.get('action_type')}] {step.get('title')}：{step.get('detail')}"
            )
    for diff in review.get("revision_diff") or []:
        lines.append(f"- 修订建议（{diff.get('diff_id')}，{diff.get('step')}/{diff.get('op')}）：{diff.get('reason')}")
    if review.get("escalation"):
        lines.append(f"- 转评估问题单：{review['escalation'].get('reason')}")
    lines.extend(["", "```json", json.dumps(review, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines)


def upsert_strategy_review(conn: Any, review: dict[str, Any]) -> str:
    """幂等 upsert：同工作流重算覆盖（version 自增 + 历次判定摘要入 history，上限 10 条）。"""
    workflow_id = str(review.get("workflow_id") or "")
    existing = conn.execute(
        """
        SELECT artifact_id,metadata_json FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1
        """,
        (workflow_id, ARTIFACT_TYPE),
    ).fetchone()
    if existing:
        previous = _loads(existing["metadata_json"], {})
        history = list(previous.get("history") or [])
        history.append(
            {
                "version": _int(previous.get("version")) or 1,
                "verdict": previous.get("verdict"),
                "verdict_reason": previous.get("verdict_reason"),
                "generated_at": previous.get("generated_at"),
            }
        )
        review["version"] = (_int(previous.get("version")) or 1) + 1
        review["history"] = history[-_HISTORY_LIMIT:]
        artifact_id = str(existing["artifact_id"])
        conn.execute(
            """
            UPDATE agent_artifacts SET content=?,metadata_json=?,validation_status='passed',title=?
            WHERE artifact_id=?
            """,
            (
                _review_content(review),
                _dumps(review),
                f"策略复盘 v{review['version']}：{review.get('verdict_label')}",
                artifact_id,
            ),
        )
        return artifact_id
    review["version"] = 1
    review["history"] = []
    artifact_id = f"strategy_review_{workflow_id}"
    conn.execute(
        """
        INSERT INTO agent_artifacts
        (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            str(review.get("goal_id") or ""),
            workflow_id,
            None,
            ARTIFACT_TYPE,
            f"策略复盘 v1：{review.get('verdict_label')}",
            "text/markdown",
            None,
            _review_content(review),
            _dumps(review),
            "passed",
        ),
    )
    return artifact_id


def rebuild_for_workflow(conn: Any, workflow_id: str, **thresholds: Any) -> tuple[str, dict[str, Any]]:
    """按需重算（存量终局工作流补生成）：终局=completed/blocked/failed，否则抛 ValueError。"""
    row = conn.execute("SELECT status FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    if row is None:
        raise LookupError(f"工作流不存在：{workflow_id}")
    if str(row["status"] or "") not in TERMINAL_STATUSES:
        raise ValueError(f"工作流状态 {row['status']} 非终局（completed/blocked/failed），不能生成策略复盘")
    review = generate_for_workflow(conn, workflow_id, **thresholds)
    artifact_id = upsert_strategy_review(conn, review)
    return artifact_id, review


def get_strategy_review(conn: Any, workflow_id: str) -> dict[str, Any] | None:
    """读取最新复盘（artifact + 结构化 review）；无复盘返回 None。"""
    row = conn.execute(
        """
        SELECT artifact_id,title,content,metadata_json,created_at FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1
        """,
        (workflow_id, ARTIFACT_TYPE),
    ).fetchone()
    if row is None:
        return None
    return {
        "artifact_id": str(row["artifact_id"]),
        "workflow_id": workflow_id,
        "title": str(row["title"] or ""),
        "content": str(row["content"] or ""),
        "review": _loads(row["metadata_json"], {}),
        "created_at": str(row["created_at"] or ""),
    }


# ---------------------------------------------------------------------------
# S4-3c：顾问逐项采纳/拒绝落库（PATCH diffs）
# ---------------------------------------------------------------------------

DECISION_STATUSES = ("accepted", "rejected")
_DECISION_LABELS = {"accepted": "采纳", "rejected": "拒绝"}

# explicit_corrections 学习信号的落点表（读取点：capability_runtime._strategy_learning_context
# 的 explicit_corrections）。DDL 与 scripts/generate_strategy_corrections.py 保持一致：
# 既有库可能尚未建该表（批量脚本按需建），此处 CREATE IF NOT EXISTS 兜底。
_STRATEGY_CORRECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS strategy_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT,
    position TEXT,
    promote_keywords_json TEXT DEFAULT '[]',
    suppress_keywords_json TEXT DEFAULT '[]',
    target_tags_json TEXT DEFAULT '[]',
    blocker_tags_json TEXT DEFAULT '[]',
    evidence_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(client, position)
)
"""


def _append_consultant_edits(
    conn: Any,
    workflow_id: str,
    diffs_by_id: dict[str, dict[str, Any]],
    normalized: list[tuple[str, str]],
    decided_at: str,
) -> int:
    """把决策追加进该工作流 strategy_v2 的 consultant_edits（同 diff_id 覆盖而非重复追加）。

    strategy_v2 缺失（无 search_strategy artifact / v1 旧格式）时降级返回 0，不报错。
    """
    artifact = conn.execute(
        """
        SELECT artifact_id,metadata_json FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type='search_strategy' ORDER BY id DESC LIMIT 1
        """,
        (workflow_id,),
    ).fetchone()
    if artifact is None:
        return 0
    metadata = _loads(artifact["metadata_json"], {})
    strategy_doc = strategy_v2.extract_strategy_v2(metadata)
    if not isinstance(strategy_doc, dict):
        return 0
    edits = [dict(item) for item in strategy_doc.get("consultant_edits") or [] if isinstance(item, dict)]
    by_diff_id = {str(item.get("diff_id") or ""): item for item in edits}
    order = [str(item.get("diff_id") or "") for item in edits]
    for diff_id, status in normalized:
        diff = diffs_by_id[diff_id]
        entry = {
            "diff_id": diff_id,
            "step": str(diff.get("step") or ""),
            "op": str(diff.get("op") or ""),
            "status": status,
            "reason": str(diff.get("reason") or ""),
            "decided_at": decided_at,
        }
        if diff_id in by_diff_id:
            by_diff_id[diff_id].update(entry)
        else:
            by_diff_id[diff_id] = entry
            order.append(diff_id)
    strategy_doc["consultant_edits"] = [by_diff_id[key] for key in order]
    metadata["strategy_v2"] = strategy_doc
    conn.execute(
        "UPDATE agent_artifacts SET metadata_json=? WHERE artifact_id=?",
        (_dumps(metadata), str(artifact["artifact_id"])),
    )
    return len(normalized)


def _merged_signal_list(existing: Any, column: str, new_items: list[str], cap: int, drop: set[str] | None = None) -> str:
    """合并既有 JSON 数组列与新信号：新信号在前（最新优先），去重后截断，口径同批量脚本。

    drop 中的条目从既有列表剔除：顾问翻转决策（采纳↔拒绝）时撤回对侧旧信号，避免同一
    关键词/公司同时挂在 promote 与 suppress 两列。
    """
    base = _loads(existing[column], []) if existing else []
    if not isinstance(base, list):
        base = []
    discard = drop or set()
    merged = [
        item
        for item in dict.fromkeys([*new_items, *(str(x) for x in base)])
        if str(item).strip() and item not in discard
    ]
    return json.dumps(merged[:cap], ensure_ascii=False)


def _record_explicit_correction(
    conn: Any,
    review: dict[str, Any],
    diffs_by_id: dict[str, dict[str, Any]],
    normalized: list[tuple[str, str]],
    decided_at: str,
) -> bool:
    """写 explicit_corrections 学习信号（strategy_corrections 表，按 client+position 合并 upsert）。

    语义映射：采纳 step4 词组→promote、拒绝 step4 词组→suppress、采纳 step2 公司→target、
    拒绝 step2 公司→blocker；每条决策落 evidence 留痕（只含 diff_id/step/op，不含 restricted）。
    缺 client/job 锚点时降级返回 False，不报错。
    """
    client = str(review.get("client") or "").strip()
    position = str(review.get("job") or "").strip()
    if not client or not position:
        return False
    promote: list[str] = []
    suppress: list[str] = []
    target: list[str] = []
    blocker: list[str] = []
    evidence: list[str] = []
    for diff_id, status in normalized:
        diff = diffs_by_id[diff_id]
        step, op = str(diff.get("step") or ""), str(diff.get("op") or "")
        if step == "step4_keyword_groups":
            terms = [str(term) for term in diff.get("terms") or [] if str(term or "").strip()]
            (promote if status == "accepted" else suppress).extend(terms)
        elif step == "step2_target_pool":
            companies = [str(name) for name in diff.get("companies") or [] if str(name or "").strip()]
            (target if status == "accepted" else blocker).extend(companies)
        evidence.append(f"复盘修订{_DECISION_LABELS[status]}：{diff_id} {step}/{op}（{decided_at}）")
    conn.execute(_STRATEGY_CORRECTIONS_DDL)
    existing = conn.execute(
        "SELECT * FROM strategy_corrections WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
        (client, position),
    ).fetchone()
    new_by_column = {
        "promote_keywords_json": promote,
        "suppress_keywords_json": suppress,
        "target_tags_json": target,
        "blocker_tags_json": blocker,
        "evidence_json": evidence,
    }
    opposite = {
        "promote_keywords_json": "suppress_keywords_json",
        "suppress_keywords_json": "promote_keywords_json",
        "target_tags_json": "blocker_tags_json",
        "blocker_tags_json": "target_tags_json",
    }
    columns = (
        ("promote_keywords_json", 20),
        ("suppress_keywords_json", 20),
        ("target_tags_json", 12),
        ("blocker_tags_json", 20),
        ("evidence_json", 30),
    )
    values = {
        column: _merged_signal_list(
            existing, column, new_by_column[column], cap,
            drop=set(new_by_column[opposite[column]]) if column in opposite else None,
        )
        for column, cap in columns
    }
    if existing:
        conn.execute(
            """
            UPDATE strategy_corrections SET promote_keywords_json=?,suppress_keywords_json=?,
                   target_tags_json=?,blocker_tags_json=?,evidence_json=?,updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (
                values["promote_keywords_json"], values["suppress_keywords_json"],
                values["target_tags_json"], values["blocker_tags_json"], values["evidence_json"],
                existing["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO strategy_corrections
            (client,position,promote_keywords_json,suppress_keywords_json,target_tags_json,blocker_tags_json,evidence_json,updated_at)
            VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))
            """,
            (
                client, position,
                values["promote_keywords_json"], values["suppress_keywords_json"],
                values["target_tags_json"], values["blocker_tags_json"], values["evidence_json"],
            ),
        )
    return True


def apply_diff_decisions(conn: Any, workflow_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """顾问逐项采纳/拒绝：status 写回 artifact 的 revision_diff（upsert 可重复覆盖），并同步
    strategy_v2.consultant_edits 与 explicit_corrections 学习信号。

    全部决策先校验后落库（任一非法则整批不写入）：工作流不存在/无复盘抛 LookupError（404）；
    diff_id 未知或 status 非 accepted/rejected 抛 ValueError（409）。
    """
    exists = conn.execute("SELECT 1 FROM agent_workflows WHERE workflow_id=?", (workflow_id,)).fetchone()
    if exists is None:
        raise LookupError(f"工作流不存在：{workflow_id}")
    row = conn.execute(
        """
        SELECT artifact_id,metadata_json FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1
        """,
        (workflow_id, ARTIFACT_TYPE),
    ).fetchone()
    if row is None:
        raise LookupError(f"该工作流暂无策略复盘：{workflow_id}")
    review = _loads(row["metadata_json"], {})
    revision_diff = [item for item in review.get("revision_diff") or [] if isinstance(item, dict)]
    diffs_by_id = {str(item.get("diff_id") or ""): item for item in revision_diff}
    # 校验 + 归一化（同一 diff_id 重复出现时后者覆盖前者）
    final: dict[str, str] = {}
    for item in decisions:
        if not isinstance(item, dict):
            raise ValueError("决策条目必须是对象：{diff_id, status}")
        diff_id = str(item.get("diff_id") or "")
        status = str(item.get("status") or "")
        if status not in DECISION_STATUSES:
            raise ValueError(f"非法决策状态：{status or '(空)'}（仅支持 accepted/rejected）")
        if diff_id not in diffs_by_id:
            raise ValueError(f"复盘中不存在该修订项：{diff_id or '(空 diff_id)'}")
        final[diff_id] = status
    normalized = list(final.items())
    decided_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for diff_id, status in normalized:
        diffs_by_id[diff_id]["status"] = status
        diffs_by_id[diff_id]["decided_at"] = decided_at
    artifact_id = str(row["artifact_id"])
    conn.execute(
        "UPDATE agent_artifacts SET content=?,metadata_json=? WHERE artifact_id=?",
        (_review_content(review), _dumps(review), artifact_id),
    )
    edits_appended = _append_consultant_edits(conn, workflow_id, diffs_by_id, normalized, decided_at)
    signal_recorded = _record_explicit_correction(conn, review, diffs_by_id, normalized, decided_at)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "artifact_id": artifact_id,
        "updated": len(normalized),
        "revision_diff": revision_diff,
        "consultant_edits_appended": edits_appended,
        "learning_signal_recorded": signal_recorded,
    }
