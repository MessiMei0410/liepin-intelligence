"""S5-3：Mapping 直挖评测指标聚合 —— GET /api/v1/mapping-tasks/metrics 的事实源。

口径来源：PRD docs/ASA_PRD_S5_mapping_direct_sourcing_2026-07-23.md §5（名单质量指标）/§8（度量）。
只读聚合，数据不足的分组如实返回 null（计数仍给出），不编造比率。

四项指标口径（决策表，与本模块实现一一对应）：

① clue_effective_rate 线索有效率 = confirmed_plus_total / clues_total
   - 分子：全部 mapping_task artifact 中状态 ∈ {confirmed, contacted, replied, intaken} 的候选数
     （与 mapping_task._sync_stats 的 confirmed 口径一致：曾被确认即计入；按 candidates 数组实数，
     不取 stats.confirmed 缓存值）；
   - 分母：Σ stats.clues（采集线索总数，含被合并/被过滤的）；分母为 0 → rate=null。

② confirm_to_intake_rate 确认→入库转化率 = intaken_total / confirmed_plus_total
   - intaken = 状态 intaken（入库只能经 intake 动作到达，口径唯一）；
   - 分母同 ① 分子；分母为 0 → rate=null。

③ mapping_coverage Mapping 覆盖率 = 有 mapping_task 的池枯竭工作流数 / 池枯竭工作流总数
   - 池枯竭判定与 S4 N3 信号同口径（strategy_review.POOL_SATURATED 判定）：
     agent_sourcing_funnel 按 workflow_id 聚合，dedupe_rate = Σdedupe_count/Σextracted_count
     > DEFAULT_POOL_SATURATED_DEDUPE_RATE（0.8），extracted=0 的工作流不计入分母；
   - "有 mapping_task" = agent_artifacts 存在 artifact_type='mapping_task' 且 workflow_id 相同；
   - 池枯竭工作流为 0 → exhausted=0、with_mapping=0、coverage=null。

④ high_score_by_source 高分率对照（验证"直挖质量更高"假设）
   - 分组：job_candidates 经 CAST(source_candidate_id) 关联 candidates.source；
     source 含 xsaas/x-saas → "xsaas"；等于 mapping → "mapping"；其余一律归 "liepin"（简历库）；
   - 评估口径：agent_candidate_assessments is_current=1 的 fit_score（多人次取 MAX），
     高分线 = strategy_review.HIGH_SCORE_FLOOR（75，与复盘 score_75_plus 同口径）；
   - 组内已评估数 < MIN_GROUP_ASSESSED → high_rate=null（assessed/high 计数如实给出）；
   - comparison：resume 组 = liepin+xsaas 合并；delta = mapping.high_rate - resume.high_rate，
     任一方为 null 则 delta=null。

红线：本模块只读；候选人名/费率/话术红线/restricted 层不进任何输出（只有聚合计数与比率）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import mapping_task, strategy_review

# 池枯竭阈值与高分线直接复用 S4 复盘常量（口径唯一事实源，防漂移）
POOL_SATURATED_DEDUPE_RATE = strategy_review.DEFAULT_POOL_SATURATED_DEDUPE_RATE
HIGH_SCORE_FLOOR = strategy_review.HIGH_SCORE_FLOOR

# 高分率分组的最小已评估样本量：低于该值比率返回 null（计数仍如实给出）
MIN_GROUP_ASSESSED = 3

_SOURCE_GROUPS = ("mapping", "liepin", "xsaas")


def _loads(value: Any, default: Any) -> Any:
    import json

    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _source_bucket(source: Any) -> str:
    """candidates.source 归组（与 asa_core/database.py 迁移 4 的渠道 CASE 同口径，mapping 单列）。"""
    text = str(source or "").strip().lower()
    if text == "mapping":
        return "mapping"
    if "xsaas" in text or "x-saas" in text:
        return "xsaas"
    return "liepin"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:  # noqa: BLE001 表不存在按无数据处理（null 降级）
        return False


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _clue_and_conversion_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT metadata_json FROM agent_artifacts WHERE artifact_type=?
        """,
        (mapping_task.ARTIFACT_TYPE,),
    ).fetchall()
    clues_total = 0
    confirmed_plus_total = 0
    intaken_total = 0
    for row in rows:
        doc = _loads(row[0], {})
        if not isinstance(doc, dict):
            continue
        stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
        clues_total += max(0, int(stats.get("clues") or 0))
        for candidate in doc.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            status = str(candidate.get("status") or "")
            if status in ("confirmed", "contacted", "replied", "intaken"):
                confirmed_plus_total += 1
            if status == "intaken":
                intaken_total += 1
    return {
        "artifacts": len(rows),
        "clues_total": clues_total,
        "confirmed_plus_total": confirmed_plus_total,
        "intaken_total": intaken_total,
        "clue_effective_rate": _ratio(confirmed_plus_total, clues_total),
        "confirm_to_intake_rate": _ratio(intaken_total, confirmed_plus_total),
    }


def _coverage_metric(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "agent_sourcing_funnel"):
        return {
            "exhausted_workflows": 0,
            "with_mapping": 0,
            "coverage": None,
            "dedupe_rate_threshold": POOL_SATURATED_DEDUPE_RATE,
        }
    rows = conn.execute(
        """
        SELECT workflow_id,
               SUM(COALESCE(extracted_count,0)) AS extracted_total,
               SUM(COALESCE(dedupe_count,0)) AS dedupe_total
          FROM agent_sourcing_funnel
         WHERE COALESCE(workflow_id,'') <> ''
         GROUP BY workflow_id
        """
    ).fetchall()
    exhausted = {
        str(row[0])
        for row in rows
        if int(row[1] or 0) > 0 and int(row[2] or 0) / int(row[1]) > POOL_SATURATED_DEDUPE_RATE
    }
    mapping_workflows = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT workflow_id FROM agent_artifacts
             WHERE artifact_type=? AND COALESCE(workflow_id,'') <> ''
            """,
            (mapping_task.ARTIFACT_TYPE,),
        ).fetchall()
    }
    with_mapping = len(exhausted & mapping_workflows)
    return {
        "exhausted_workflows": len(exhausted),
        "with_mapping": with_mapping,
        "coverage": _ratio(with_mapping, len(exhausted)),
        "dedupe_rate_threshold": POOL_SATURATED_DEDUPE_RATE,
    }


def _high_score_by_source(conn: sqlite3.Connection) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {
        name: {"assessed": 0, "high": 0, "high_rate": None} for name in _SOURCE_GROUPS
    }
    if _table_exists(conn, "job_candidates") and _table_exists(conn, "candidates") and _table_exists(
        conn, "agent_candidate_assessments"
    ):
        rows = conn.execute(
            """
            SELECT c.source AS source,
                   MAX(a.fit_score) AS fit_score
              FROM job_candidates jc
              JOIN candidates c ON CAST(c.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
              JOIN agent_candidate_assessments a
                ON a.job_candidate_id=jc.id AND a.is_current=1
             GROUP BY jc.id
            """
        ).fetchall()
        for row in rows:
            bucket = groups[_source_bucket(row[0])]
            bucket["assessed"] += 1
            if row[1] is not None and int(row[1]) >= HIGH_SCORE_FLOOR:
                bucket["high"] += 1
    for bucket in groups.values():
        if bucket["assessed"] >= MIN_GROUP_ASSESSED:
            bucket["high_rate"] = round(bucket["high"] / bucket["assessed"], 4)
    resume_assessed = groups["liepin"]["assessed"] + groups["xsaas"]["assessed"]
    resume_high = groups["liepin"]["high"] + groups["xsaas"]["high"]
    resume_rate = round(resume_high / resume_assessed, 4) if resume_assessed >= MIN_GROUP_ASSESSED else None
    mapping_rate = groups["mapping"]["high_rate"]
    return {
        "groups": groups,
        "high_score_floor": HIGH_SCORE_FLOOR,
        "min_assessed": MIN_GROUP_ASSESSED,
        "comparison": {
            "resume_assessed": resume_assessed,
            "resume_high": resume_high,
            "resume_high_rate": resume_rate,
            "mapping_high_rate": mapping_rate,
            "delta_mapping_vs_resume": (
                round(mapping_rate - resume_rate, 4)
                if mapping_rate is not None and resume_rate is not None
                else None
            ),
        },
    }


def compute_mapping_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """聚合全部 mapping_task artifact + 渠道漏斗 + 评估表，产出 §8 四项指标（只读）。

    conn 只需可读；任何表缺失按 null 降级（不抛异常）。指标口径见模块 docstring。
    """
    funnel = _clue_and_conversion_metrics(conn)
    return {
        "artifacts_aggregated": funnel["artifacts"],
        "clue_effectiveness": {
            "clues_total": funnel["clues_total"],
            "confirmed_plus_total": funnel["confirmed_plus_total"],
            "rate": funnel["clue_effective_rate"],
        },
        "confirm_to_intake": {
            "confirmed_plus_total": funnel["confirmed_plus_total"],
            "intaken_total": funnel["intaken_total"],
            "rate": funnel["confirm_to_intake_rate"],
        },
        "mapping_coverage": _coverage_metric(conn),
        "high_score_by_source": _high_score_by_source(conn),
    }
