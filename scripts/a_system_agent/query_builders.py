"""渠道查询方言层（S4-3c-2 / N1）：策略关键词组 → 逐渠道可执行查询。

位置：策略对象 ``step4_keyword_groups`` / ``channels`` 与渠道 runner 之间。
同一组画像关键词，按各渠道搜索语法分别构造查询；本层只产查询列表，
逐条执行与合并去重是渠道 runner 的既有语义（rounds + dedup），不在本层。

顾问规则链（2026-07-23，#154 第 5/6/7 轮实证）：
1. X-SaaS 系统本身识别不了多关键词组合，多词查询直接 0 结果
   （round5 第 1 组"多相控制器 DrMOS POL TME FAE"五词 0 条实证）；
2. 猎聘同理：候选人简历不一定包含全部关键词，往往只有一两个，
   多词 AND 会把好人选过滤掉——两渠道统一按 ≤2 词短查询构造；
3. 两个公司名组合语义必错（一人不可能同时在两家公司，round7 实证），
   公司词永不两两成对——需要公司词表识别查询里的公司 token；
4. X-SaaS 更严格：公司词不与任何词组合，每个公司词一条独立查询，
   职能/技术词按"锚定词 + 逐词"二字对展开。

渠道方言规则表：

| 规则 | 猎聘 build_liepin_queries | X-SaaS build_xsaas_queries |
| --- | --- | --- |
| 公司词 × 职能/技术词 | 可组合（公司 + 首个非公司词） | 不组合，公司词独立查询 |
| 公司词 × 公司词 | 永不两两成对 | 永不（天然满足：公司词均独立） |
| 非公司词多词组 | 锚定对，每组 ≤2 词 | 锚定对，每组 ≤2 词 |
| 单查询公司名上限 | <2（契约断言） | <2（契约断言） |
| 查询总量上限 | 6（LIEPIN_QUERY_MAX_COUNT） | 8（XSAAS_QUERY_MAX_COUNT） |

输入兼容：策略产出的查询项可能是字符串，也可能是 {query, purpose, evidence, round}
字典（LLM 策略步骤与 MULTICHANNEL fallback 两种形态并存，round6 实证 repr 残片
事故）——字典取 query 字段，其余跳过；空/异常输入一律降级为空列表（不抛异常）。
"""

from __future__ import annotations

from typing import Any

from . import knowledge_base

LIEPIN_QUERY_MAX_TERMS = 2
LIEPIN_QUERY_MAX_COUNT = 6
XSAAS_QUERY_MAX_TERMS = 2
XSAAS_QUERY_MAX_COUNT = 8


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
    """X-SaaS 方言：公司词独立查询，职能/技术词锚定对 ≤2 词，总量 ≤8 组。

    X-SaaS 搜索语义不支持多关键词空格拼接（round5/7 实证 0 召回），因此：
    - 每个公司词单独一条查询，不与任何词组合（含其他公司词）——
      单查询天然不可能含 ≥2 个公司名（契约断言的不变量）；
    - 非公司词 ≤XSAAS_QUERY_MAX_TERMS 原样保留，超密按锚定对展开；
    - 逐条执行后由 runner 合并去重（runner 既有 rounds+dedup 语义不在本层）。
    """
    vocab = _normalized_vocab(company_terms)
    adapted: list[str] = []
    for raw in queries:
        terms = _query_text(raw).split()
        if not terms:
            continue
        companies = [t for t in terms if is_company_token(t, vocab)]
        others = [t for t in terms if not is_company_token(t, vocab)]
        # 公司词独立查询：优先级高于锚定对（公司定向是 T1/T2 池主战术）
        adapted.extend(companies)
        if others:
            adapted.extend(_anchor_pairs(others, XSAAS_QUERY_MAX_TERMS))
    return _dedupe_cap(adapted, XSAAS_QUERY_MAX_COUNT)
