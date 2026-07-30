"""渠道查询方言层（S4-3c-2 / N1）：策略关键词组 → 逐渠道可执行查询。

位置：策略对象 ``step4_keyword_groups`` / ``channels`` 与渠道 runner 之间。
同一组画像关键词，按各渠道搜索语法分别构造查询；本层只产查询列表，
逐条执行与合并去重是渠道 runner 的既有语义（rounds + dedup），不在本层。

顾问规则链（2026-07-23，#154 第 5/6/7 轮实证）：
1. X-SaaS 系统本身识别不了多关键词组合，多词查询直接 0 结果
   （round5 第 1 组"多相控制器 DrMOS POL TME FAE"五词 0 条实证）；
2. 猎聘同理：候选人简历不一定包含全部关键词，往往只有一两个，
   多词 AND 会把好人选过滤掉——猎聘按 ≤2 词短查询构造；
3. 两个公司名组合语义必错（一人不可能同时在两家公司，round7 实证），
   公司词永不两两成对——需要公司词表识别查询里的公司 token；
4. X-SaaS 更严格：公司词不与任何词组合，每个公司词一条独立查询；
   2026-07-27 实测裸 TME 会大量命中腾讯音乐，POL/AE/FAE 也存在明显歧义，
   因此只保留公司词和高辨识度技术原子词。

渠道方言规则表：

| 规则 | 猎聘 build_liepin_queries | X-SaaS build_xsaas_queries |
| --- | --- | --- |
| 公司词 × 职能/技术词 | 可组合（公司 + 首个非公司词） | 不组合，公司词独立查询 |
| 公司词 × 公司词 | 永不两两成对 | 永不（天然满足：公司词均独立） |
| 非公司词多词组 | 锚定对，每组 ≤2 词 | 高辨识度原子词 |
| 单查询公司名上限 | <2（契约断言） | <2（契约断言） |
| 查询总量上限 | 6（LIEPIN_QUERY_MAX_COUNT） | 8（XSAAS_QUERY_MAX_COUNT） |

输入兼容：策略产出的查询项可能是字符串，也可能是 {query, purpose, evidence, round}
字典（LLM 策略步骤与 MULTICHANNEL fallback 两种形态并存，round6 实证 repr 残片
事故）——字典取 query 字段，其余跳过；空/异常输入一律降级为空列表（不抛异常）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import knowledge_base, strategy_v2

LIEPIN_QUERY_MAX_TERMS = 2
LIEPIN_QUERY_MAX_COUNT = 6
XSAAS_QUERY_MAX_TERMS = 2
XSAAS_QUERY_MAX_COUNT = 8
XSAAS_AMBIGUOUS_ATOMIC_TERMS = {"ae", "fae", "pc", "pol", "tme"}
XSAAS_GENERIC_ATOMIC_TERMS = {"产品", "工程师", "总监", "技术", "电源", "经理"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def query_plan_hash(plan: dict[str, Any]) -> str:
    """Return the content identity of a query plan, excluding its self-declared hash."""
    payload = {key: value for key, value in plan.items() if key != "plan_hash"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def validate_query_plan_v1(value: Any) -> tuple[bool, list[str]]:
    """Validate the immutable executable plan before approval or channel execution."""
    if not isinstance(value, dict):
        return False, ["query_plan_v1 必须是对象"]
    errors: list[str] = []
    if value.get("schema_version") != "query_plan_v1":
        errors.append("schema_version 必须是 query_plan_v1")
    cells = value.get("cells")
    if not isinstance(cells, list) or not cells:
        errors.append("cells 必须是非空数组")
        cells = []
    if value.get("cell_count") != len(cells):
        errors.append("cell_count 与 cells 数量不一致")
    declared_hash = str(value.get("plan_hash") or "")
    if not declared_hash or declared_hash != query_plan_hash(value):
        errors.append("plan_hash 与查询计划内容不一致")
    seen_ids: set[str] = set()
    channels: set[str] = set()
    for index, cell in enumerate(cells, 1):
        if not isinstance(cell, dict):
            errors.append(f"cells[{index}] 必须是对象")
            continue
        cell_id = str(cell.get("cell_id") or "")
        if not cell_id or cell_id in seen_ids:
            errors.append(f"cells[{index}].cell_id 缺失或重复")
        seen_ids.add(cell_id)
        channel = str(cell.get("channel") or "")
        if channel not in {"liepin", "xsaas"}:
            errors.append(f"cells[{index}].channel 非法")
        else:
            channels.add(channel)
        if not str(cell.get("query") or "").strip():
            errors.append(f"cells[{index}].query 为空")
        if not isinstance(cell.get("provenance"), list) or not cell.get("provenance"):
            errors.append(f"cells[{index}].provenance 为空")
    for channel in ("liepin", "xsaas"):
        if channel not in channels:
            errors.append(f"缺少 {channel} 查询单元")
    return not errors, errors


def query_plan_channel_queries(plan: dict[str, Any], channel: str) -> list[str]:
    """Extract exactly the approved query sequence for one channel."""
    return [
        str(cell["query"])
        for cell in plan.get("cells") or []
        if isinstance(cell, dict) and cell.get("channel") == channel and str(cell.get("query") or "").strip()
    ]


def query_plan_channel_entries(plan: dict[str, Any], channel: str) -> list[Any]:
    """Build the execution envelope without mutating the immutable approved plan."""
    entries: list[Any] = []
    for cell in plan.get("cells") or []:
        if not isinstance(cell, dict) or cell.get("channel") != channel:
            continue
        query = str(cell.get("query") or "").strip()
        if not query:
            continue
        cursor = cell.get("execution_cursor")
        progress = cell.get("execution_progress") if isinstance(cell.get("execution_progress"), dict) else {}
        entry = {
            "cell_id": str(cell.get("cell_id") or ""),
            "query": query,
            "evaluation_constraints": cell.get("evaluation_constraints") or {
                "locations": cell.get("locations") or [],
                "levels": cell.get("levels") or [],
                "scenarios": cell.get("scenarios") or [],
            },
            "execution_filters": cell.get("execution_filters") or {},
        }
        if isinstance(cursor, dict) and int(cursor.get("page") or 0) > 1:
            entries.append({
                **entry,
                "cursor": {"page": int(cursor["page"])},
                "collected_before": max(0, int(progress.get("unique_count") or progress.get("extracted_count") or 0)),
                "seen_candidate_keys": [
                    str(key) for key in progress.get("seen_candidate_keys") or [] if str(key).strip()
                ],
            })
        else:
            entries.append(entry)
    return entries


def query_plan_company_vocabulary(plan: dict[str, Any]) -> set[str]:
    names = {
        str(ref.get("company") or "").strip()
        for cell in plan.get("cells") or []
        if isinstance(cell, dict)
        for ref in cell.get("provenance") or []
        if isinstance(ref, dict) and str(ref.get("company") or "").strip()
    }
    return {normalized for name in names if (normalized := knowledge_base.normalize_client_name(name))}


def schedule_query_plan_v1(
    plan: dict[str, Any],
    metrics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rank approved-plan candidates by marginal unique yield while reserving exploration."""
    metric_by_query = {
        (str(item.get("channel") or ""), " ".join(str(item.get("query") or "").split()).casefold()): item
        for item in metrics
        if isinstance(item, dict)
    }
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for raw_cell in plan.get("cells") or []:
        if not isinstance(raw_cell, dict):
            continue
        cell = json.loads(json.dumps(raw_cell, ensure_ascii=False))
        base_priority = int(cell.get("priority") or 0)
        metric = metric_by_query.get((
            str(cell.get("channel") or ""),
            " ".join(str(cell.get("query") or "").split()).casefold(),
        ))
        if metric:
            runs = max(0, int(metric.get("runs") or 0))
            unique_yield = max(0.0, float(metric.get("unique_yield_per_run") or 0))
            overlap_rate = max(0.0, min(float(metric.get("overlap_rate") or 0), 1.0))
            business_score = float(metric.get("business_score") or 0)
            marginal_score = unique_yield * (1 - overlap_rate) + business_score * 2
            scheduling = {
                "policy": "marginal_unique_yield_v1",
                "mode": "learned",
                "historical_runs": runs,
                "unique_yield_per_run": round(unique_yield, 4),
                "overlap_rate": round(overlap_rate, 4),
                "business_score": round(business_score, 4),
                "marginal_score": round(marginal_score, 4),
            }
        else:
            marginal_score = 3.0
            scheduling = {
                "policy": "marginal_unique_yield_v1",
                "mode": "exploration",
                "historical_runs": 0,
                "unique_yield_per_run": 0.0,
                "overlap_rate": 0.0,
                "business_score": 0.0,
                "marginal_score": marginal_score,
            }
        cell["base_priority"] = base_priority
        cell["scheduling"] = scheduling
        ranked.append((marginal_score, base_priority, cell))
    ranked.sort(key=lambda item: (-item[0], item[1], str(item[2].get("cell_id") or "")))
    cells: list[dict[str, Any]] = []
    for priority, (_, _base_priority, cell) in enumerate(ranked, 1):
        cell["priority"] = priority
        cells.append(cell)
    scheduled = {
        **{key: value for key, value in plan.items() if key not in {"plan_hash", "cells", "cell_count", "scheduler"}},
        "scheduler": {
            "policy": "marginal_unique_yield_v1",
            "historical_metric_count": len(metric_by_query),
            "exploration_cells": sum(cell["scheduling"]["mode"] == "exploration" for cell in cells),
        },
        "cell_count": len(cells),
        "cells": cells,
    }
    return {**scheduled, "plan_hash": query_plan_hash(scheduled)}


def _dimension_values(strategy: dict[str, Any], key: str) -> list[str]:
    anchors = strategy.get("anchors") if isinstance(strategy.get("anchors"), dict) else {}
    anchor = anchors.get(key) if isinstance(anchors.get(key), dict) else {}
    return list(dict.fromkeys(str(value).strip() for value in anchor.get("values") or [] if str(value).strip()))


def compile_query_plan_v1(strategy: dict[str, Any]) -> dict[str, Any]:
    """Compile every strategy pool/group into deterministic, channel-specific query cells."""
    declared_constraints = (
        strategy.get("evaluation_constraints")
        if isinstance(strategy.get("evaluation_constraints"), dict)
        else {}
    )
    locations = list(dict.fromkeys(
        str(value).strip()
        for value in declared_constraints.get("locations") or _dimension_values(strategy, "location")
        if str(value).strip()
    ))
    scenarios = list(dict.fromkeys(
        str(value).strip()
        for value in (
            declared_constraints.get("scenarios")
            or _dimension_values(strategy, "scenario_track")
            or _dimension_values(strategy, "scenario")
        )
        if str(value).strip()
    ))
    level_mapping = strategy.get("step3_level_mapping") if isinstance(strategy.get("step3_level_mapping"), dict) else {}
    levels = list(dict.fromkeys(
        str(value).strip()
        for value in declared_constraints.get("levels") or level_mapping.get("accepted_levels") or []
        if str(value).strip()
    ))
    evaluation_constraints = {"locations": locations, "levels": levels, "scenarios": scenarios}

    companies: list[dict[str, str]] = []
    for pool in strategy.get("step2_target_pool") or []:
        if not isinstance(pool, dict):
            continue
        for company in pool.get("companies") or []:
            if not isinstance(company, dict) or not str(company.get("name") or "").strip():
                continue
            companies.append({
                "name": str(company["name"]).strip(),
                "tier": str(pool.get("tier") or "T3"),
                "path": str(pool.get("path") or "adjacent"),
                "source": str(company.get("source") or "llm_inferred"),
                "confidence": str(company.get("confidence") or "low"),
            })
    groups = [
        {
            "group": str(group.get("group") or "").strip(),
            "targets": str(group.get("targets") or "").strip(),
            "terms": [str(term).strip() for term in group.get("terms") or [] if str(term).strip()],
        }
        for group in strategy.get("step4_keyword_groups") or []
        if isinstance(group, dict) and str(group.get("group") or "").strip()
    ]
    groups = [group for group in groups if group["terms"]]
    vocab = {knowledge_base.normalize_client_name(company["name"]) for company in companies}
    vocab.discard("")

    cells_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    def add(channel: str, query: str, priority: int, provenance: dict[str, Any]) -> None:
        normalized_query = " ".join(str(query or "").split())
        if not normalized_query:
            return
        key = (channel, normalized_query.casefold())
        if key not in cells_by_key:
            identity = {
                "channel": channel,
                "query": normalized_query,
                "locations": locations,
                "levels": levels,
                "scenarios": scenarios,
            }
            cells_by_key[key] = {
                "cell_id": "qpc_" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:20],
                **identity,
                "evaluation_constraints": evaluation_constraints,
                "execution_filters": {},
                "priority": priority,
                "provenance": [],
            }
        cell = cells_by_key[key]
        cell["priority"] = min(int(cell["priority"]), priority)
        if provenance not in cell["provenance"]:
            cell["provenance"].append(provenance)

    builders = {"liepin": build_liepin_queries, "xsaas": build_xsaas_queries}
    tier_priority = {"T1": 10, "T2": 20, "T3": 30}
    for company in companies:
        provenance = {
            "kind": "company_pool", "company": company["name"], "tier": company["tier"],
            "path": company["path"], "source": company["source"], "confidence": company["confidence"],
        }
        for channel, builder in builders.items():
            for query in builder([company["name"]], company_terms=vocab):
                add(channel, query, tier_priority.get(company["tier"], 30) + 10, provenance)

    for group_index, group in enumerate(groups):
        group_query = " ".join(group["terms"])
        provenance = {
            "kind": "keyword_group", "group": group["group"],
            "targets": group["targets"], "terms": group["terms"],
        }
        for channel, builder in builders.items():
            for query in builder([group_query], company_terms=vocab):
                add(channel, query, 100 + group_index, provenance)

        for company in companies:
            combined = f"{company['name']} {group_query}"
            combined_provenance = {
                "kind": "company_keyword", "company": company["name"], "tier": company["tier"],
                "path": company["path"], "group": group["group"], "targets": group["targets"],
            }
            for channel, builder in builders.items():
                for query in builder([combined], company_terms=vocab):
                    add(
                        channel, query,
                        tier_priority.get(company["tier"], 30) + group_index,
                        combined_provenance,
                    )

    cells = sorted(
        cells_by_key.values(),
        key=lambda cell: (int(cell["priority"]), str(cell["channel"]), str(cell["query"]).casefold()),
    )
    plan: dict[str, Any] = {
        "schema_version": "query_plan_v1",
        "source_strategy_version": str(strategy.get("schema_version") or ""),
        "dimensions": {"locations": locations, "levels": levels, "scenarios": scenarios},
        "execution_semantics": {
            "retrieval_axes": ["channel", "query"],
            "platform_filters": [],
            "evaluation_constraints": {
                "locations": "post_recall_soft_score",
                "levels": "post_recall_assessment",
                "scenarios": "post_recall_assessment_context",
            },
        },
        "cell_count": len(cells),
        "cells": cells,
    }
    return {**plan, "plan_hash": query_plan_hash(plan)}


def company_vocabulary(strategy: dict[str, Any]) -> set[str]:
    """公司词表：策略 step2 目标池公司名 + 公司图谱全名 + 种子池公司名（运行时只读）。

    顾问规则（2026-07-23）：两个公司名组合查询语义必错（一人不可能同时在两家公司），
    需要词表识别查询里的公司 token。任何来源异常都降级为空集（不影响查询生成）。
    """
    names: set[str] = set()
    v2 = strategy.get("strategy_v2") if isinstance(strategy, dict) else None
    if not isinstance(v2, dict):
        v2 = (strategy.get("metadata") or {}).get("strategy_v2") if isinstance(strategy, dict) else None
    for path in (v2 or {}).get("step2_target_pool") or []:
        for comp in (path or {}).get("companies") or []:
            name = str((comp or {}).get("name") or "").strip()
            if name:
                names.add(name)
    # 种子原型池（round8 实证：execute_external 的 strategy 嵌套不含 step2 时，
    # 仅靠图谱词表会漏 MPS/矽力杰/杰华特，公司词成对漏网）
    try:
        archetypes, _arch_trace = strategy_v2.load_job_archetypes()
        for arch in archetypes:
            pool = (arch or {}).get("target_company_pool") or {}
            for group in pool.values():
                for comp in (group or {}).get("companies") or []:
                    name = str((comp or {}).get("name") or "").strip()
                    if name:
                        names.add(name)
    except Exception:
        pass
    try:
        graph, _trace = knowledge_base.load_company_graph()
        names.update(graph.keys())
    except Exception:
        pass
    return {norm for name in names if (norm := knowledge_base.normalize_client_name(name))}


def is_company_token(token: str, vocab: set[str]) -> bool:
    norm = knowledge_base.normalize_client_name(token)
    if not norm:
        return False
    # 短别名（MPS/TI 2 字符）只许精确等值；≥3 字符允许双向包含（矽力杰 ∈ 杭州矽力杰半导体）
    if len(norm) <= 2:
        return norm in vocab
    return any(norm in entry or entry in norm for entry in vocab)


def _query_text(raw: Any) -> str:
    """查询项归一化：字典取 query/q/keyword 字段，其余按字符串；异常归空串。"""
    if isinstance(raw, dict):
        raw = raw.get("query") or raw.get("q") or raw.get("keyword") or ""
    return str(raw or "")


def _query_terms(raw: Any) -> list[str]:
    return [term for term in re.split(r"[\s/、，,；;|]+", _query_text(raw).strip()) if term]


def _normalized_vocab(company_terms: set[str] | None) -> set[str]:
    vocab = {knowledge_base.normalize_client_name(term) for term in (company_terms or set())}
    vocab.discard("")
    return vocab


def _anchor_pairs(terms: list[str], max_terms: int) -> list[str]:
    """非公司词组：≤max_terms 原样保留；超密按"锚定词 + 逐个剩余词"二字对展开。"""
    if len(terms) <= max_terms:
        return [" ".join(terms)]
    anchor = terms[0]
    return [f"{anchor} {term}" for term in terms[1:]]


def _dedupe_cap(queries: list[str], max_count: int) -> list[str]:
    seen: set[str] = set()
    unique = [query for query in queries if not (query in seen or seen.add(query))]
    return unique[:max_count]


def adapt_queries(
    queries: list[Any],
    *,
    max_terms: int,
    max_count: int,
    company_terms: set[str] | None = None,
) -> list[str]:
    """组合式方言（猎聘语法）：密集多词查询拆成 1-2 个关键词的短查询。

    公司词只与非公司词配对（取首个）或单独成组，公司词永不两两成对；
    非公司词按"锚定词 + 逐个剩余词"组成二字查询。去重保序，总量封顶 max_count。
    """
    vocab = _normalized_vocab(company_terms)
    adapted: list[str] = []
    for raw in queries:
        terms = _query_text(raw).split()
        if not terms:
            continue
        companies = [t for t in terms if is_company_token(t, vocab)]
        others = [t for t in terms if not is_company_token(t, vocab)]
        if not companies:
            adapted.extend(_anchor_pairs(terms, max_terms))
            continue
        for comp in companies:
            adapted.append(f"{comp} {others[0]}" if others else comp)
        if len(others) > 1:
            adapted.extend(_anchor_pairs(others, max_terms))
    return _dedupe_cap(adapted, max_count)


def build_liepin_queries(queries: list[Any], *, company_terms: set[str] | None = None) -> list[str]:
    """猎聘方言：维持组合查询——公司 + 职能/技术词可组合，≤2 词/≤6 组，公司词不两两成对。"""
    return adapt_queries(
        queries,
        max_terms=LIEPIN_QUERY_MAX_TERMS,
        max_count=LIEPIN_QUERY_MAX_COUNT,
        company_terms=company_terms,
    )


def build_xsaas_queries(queries: list[Any], *, company_terms: set[str] | None = None) -> list[str]:
    """X-SaaS 方言：公司词优先独立查询，高辨识度技术词原子查询，总量 ≤8 组。

    X-SaaS 搜索语义不支持多关键词空格拼接（round5/7 和 2026-07-27 PC 电源岗位实证
    均为 0 召回），因此：
    - 每个公司词单独一条查询，不与任何词组合（含其他公司词）——
      单查询天然不可能含 ≥2 个公司名（契约断言的不变量）；
    - 所有输入组先汇总公司词，避免 8 组上限被前面的低价值技术组合占满；
    - 非公司词按原子词查询，并合并 ``PC + 电源`` 为 ``PC电源``；
    - 裸 TME/AE/FAE/POL/PC 歧义过高，分别会命中腾讯音乐、普通英文片段或过宽人群，
      只保留公司定向或带电源语义的高辨识度查询；
    - 逐条执行后由 runner 合并去重（runner 既有 rounds+dedup 语义不在本层）。
    """
    vocab = _normalized_vocab(company_terms)
    companies: list[str] = []
    technical: list[str] = []
    for raw in queries:
        terms = _query_terms(raw)
        if not terms:
            continue
        group_companies = [term for term in terms if is_company_token(term, vocab)]
        others = [term for term in terms if not is_company_token(term, vocab)]
        companies.extend(group_companies)
        folded = {term.casefold() for term in others}
        if "pc" in folded and "电源" in others:
            technical.append("PC电源")
        for term in others:
            key = term.casefold()
            if key in XSAAS_AMBIGUOUS_ATOMIC_TERMS or term in XSAAS_GENERIC_ATOMIC_TERMS:
                continue
            technical.append(term)
    return _dedupe_cap([*companies, *technical], XSAAS_QUERY_MAX_COUNT)
