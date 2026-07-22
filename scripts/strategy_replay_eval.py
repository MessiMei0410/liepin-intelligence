"""S4-4：策略回放评测 —— 两个定稿 case 跑策略生成链（确定性模式），产出三指标并进回归。

口径来源：docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §6（回放评测）。

回放对象（事实源，运行时只读；目录用 ASA_KNOWLEDGE_BASE_DIR 或 --kb-dir 覆盖，
缺省 /Users/messi/Documents/ASA/knowledge_base）：
- cases/seed_silan_tme_v1.json（士兰微 v1.1，L1 校准基准，岗位原型 tme_computing_power）
- cases/case_changyue_equipment_v1.json（长越 v1.2，L3 校准基准；
  该 case 含 4 个岗位，回放固定取 priority_v1.1==1 的「自动化软件高级工程师」——
  它是 case 内唯一带 anchor_analysis（四锚点参考答案）的岗位，且为顾问确认的第一优先级）

回放方式（确定性，可进 CI）：
- 每个 case 按文件内容构造岗位上下文（client/job/JD 语料），写入临时 sqlite 库
  （只用临时库，绝不触碰生产 DB；知识库文件只读）；
- 用 AgentService + FakeLLM（不传 search_strategy）跑 capability_runtime.run_search_strategy，
  LLM 填充步骤走 deterministic_fallback 模式（与 tests/test_strategy_v2_s4.py 的 FakeLLM 模式一致），
  因此全部输出确定可复现。真实 LLM 对照：手动把 service.llm 换成 OpenAICompatibleLLM
  跑同一 run_replay() 即可，其输出不进回归门槛。
- 士兰微 case 按 L1 构造（岗位详情含客户一手锚点标签：主要客户/目标友商/重点产品）；
  长越 case 按其 meta 声明的 L3 构造（仅 JD，无顾问补充）——L3 裸跑下「客户的客户/竞争格局」
  锚点缺失是该 case 的真实基线，不是评测 bug。

三指标口径：
① 目标池重合度（pool_recall / pool_precision）
   - 参考池：case 的 T1+T2 公司（T3 不计入——士兰微 T3 为相邻待确认池、长越 T3 逆向默认关闭）。
     长越 T1 的三个名单（companies_international / companies_domestic_added_v1.1 /
     companies_domestic_databacked_v1.2）合并；泛化条目（见下）剔除并单列；按归一键去重。
   - Agent 池：strategy_v2.step2_target_pool 中 tier ∈ {T1,T2} 的全部公司。
   - recall = 被 Agent 池覆盖的参考公司数 / 参考公司数；
     precision = 命中参考池的 Agent 公司数 / Agent 池公司数。
   - 明细给出未覆盖的 case 公司（改进输入）与 Agent 池多出公司（precision 损耗来源）。
② 关键词有效率（keyword_coverage）
   - 参考：case 标注的有效关键词组（士兰微顶层 keyword_groups；长越岗位的 keyword_groups）。
   - 组级命中：组内被 Agent step4 覆盖的词数 / 组内总词数 ≥ 0.5 记该组命中；
     指标 = 命中组数 / 总组数。词级覆盖 = 压缩归一后相等，或双向包含（短词 ≥2 字符）。
③ 锚点完整率（anchor_completeness）
   - 逐锚点评分：缺失 = 0；present 且与参考答案有任一值重合 = 1；present 但无重合 = 0.5
     （锚定偏差）。指标 = 四锚点得分均值。
   - 参考答案：士兰微取 job_archetype.directions（customers/products/competitors/方向名）；
     长越取岗位 anchor_analysis（customer_of_client/product_tech/competitors/scene），
     其中 competitors 标注「缺失，见 target_company_pool.T1」→ 参考答案用 T1 具体公司池。
   - 值重合 = 压缩归一（去空白/括号内容/小写）后相等或双向包含（短值 ≥2 字符）。

公司名归一规则（①的匹配基础，company_keys/companies_match）：
- 大小写不敏感、去全部空白；全角/半角括号内容作为别名键（如 MPS（芯源系统）→ {mps, 芯源系统}）；
- 循环剥离尾部公司后缀（股份有限公司/有限责任公司/有限公司/集团公司/集团/公司）；
- 「/／、」连接的复合名拆分为独立键（世禹/景焱 → {世禹, 景焱}）；
- 括号别名 <3 字符或为纯地名（上海/浙江/无锡等）时丢弃（防「芯钛科（上海）」误配「上海光键」）；
- 两公司匹配 = 键集合有交集（等长任意、包含关系要求短键 ≥3 字符），
  或经图谱别名归一到同一家图谱公司（各自与 589 家图谱全名做同一套键匹配，
  命中同一家即视为同一公司——图谱无显式 alias 字段，全名键即别名机制）；
- 泛化条目（非具体公司，不计入参考池）：含「其他/等」；或非法定全名（不含「有限公司/股份」）
  且剥后缀后以 公司/设备商/方案商/代理商/平台/岗/市场/实验室/工程师/技术市场/系统商/设备 结尾
  （如「机器人/直线电机平台公司」「其他 die bonder/wire bonder 设备商」）。

基线更新手册（策略生成逻辑改动后）：
1. 跑 `PYTHONPATH=scripts /usr/local/bin/python3 scripts/strategy_replay_eval.py --json` 拿新指标；
2. 与 tests/test_strategy_replay_s4.py 的 REPLAY_BASELINE 常量及
   docs/ASA_strategy_replay_baseline_s4-4_2026-07-23.md 对比，逐项看未命中明细；
3. 确认是能力提升（不是口径漂移/倒退洗白）后，手动更新测试常量（改注释里的日期与口径）
   并同步更新基线文档的数值与明细；指标下降一律视为回归，修生成逻辑而不是改基线。

CLI：默认打印人类可读表格；--json 打印 JSON；--out PATH 把 JSON 报告写文件；
--case 只跑指定 case；--kb-dir 覆盖知识库目录（等价于 ASA_KNOWLEDGE_BASE_DIR）。
case 文件缺失/坏 JSON/结构不符 → 抛 ReplayCaseError（退出码 2），绝不静默全过。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent import knowledge_base, strategy_v2  # noqa: E402

DEFAULT_KB_DIR = Path("/Users/messi/Documents/ASA/knowledge_base")
CASES_SUBDIR = "cases"

CASE_SILAN = "case_silan_tme"
CASE_CHANGYUE = "case_changyue_equipment"

CASE_SPECS: dict[str, dict[str, Any]] = {
    CASE_SILAN: {
        "file": "seed_silan_tme_v1.json",
        "label": "士兰微 技术市场经理/总监（TME，计算电源管理方向）",
        "expected_input_level": "L1",
    },
    CASE_CHANGYUE: {
        "file": "case_changyue_equipment_v1.json",
        "label": "长越 自动化软件高级工程师（priority_v1.1=1 岗位）",
        "expected_input_level": "L3",
    },
}

METRIC_KEYS = ("pool_recall", "pool_precision", "keyword_coverage", "anchor_completeness")

# 归一规则常量（口径见模块 docstring）
_CORP_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团", "公司")
_BRACKET_CONTENT = re.compile(r"（([^）]*)）|\(([^)]*)\)|【([^】]*)】|\[([^]]*)\]")
_BRACKET_STRIP = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^]]*\]")
_GEO_ALIASES = {
    "上海", "北京", "苏州", "无锡", "常州", "嘉兴", "杭州", "浙江", "深圳",
    "宁波", "南京", "广东", "江苏", "江阴", "南通", "天水",
}
_GENERIC_TOKENS = ("其他", "等")
_GENERIC_TAILS = (
    "公司", "设备商", "方案商", "代理商", "平台", "岗", "市场", "实验室",
    "工程师", "技术市场", "系统商", "设备",
)
_MIN_CONTAINMENT = 3  # 包含匹配要求短键长度 ≥3（精确相等不限长度）


class ReplayCaseError(RuntimeError):
    """回放 case 文件缺失/坏 JSON/结构不符——明确报错，绝不静默全过。"""


# ---------------------------------------------------------------------------
# 公司名归一与匹配
# ---------------------------------------------------------------------------

def _compact(text: Any) -> str:
    return "".join(str(text or "").split()).lower()


def _strip_suffixes(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for suffix in _CORP_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
    return text


def company_keys(name: Any) -> set[str]:
    """公司名归一键集合：括号别名 + 斜杠拆分 + 剥后缀主名（口径见模块 docstring）。"""
    raw = str(name or "").strip()
    if not raw:
        return set()
    keys: set[str] = set()
    for match in _BRACKET_CONTENT.finditer(raw):
        alias = _strip_suffixes(_compact(next(group for group in match.groups() if group)))
        if len(alias) >= 3 and alias not in _GEO_ALIASES:
            keys.add(alias)
    base = _BRACKET_STRIP.sub("", raw)
    for part in re.split(r"[/／、;；,，]+", base):
        key = _strip_suffixes(_compact(part))
        if key:
            keys.add(key)
    return keys


def keys_match(keys_a: set[str], keys_b: set[str]) -> bool:
    """键集合匹配：精确相等（任意长度）或双向包含（短键 ≥3 字符）。"""
    if keys_a & keys_b:
        return True
    for key_a in keys_a:
        for key_b in keys_b:
            shorter, longer = (key_a, key_b) if len(key_a) <= len(key_b) else (key_b, key_a)
            if len(shorter) >= _MIN_CONTAINMENT and shorter in longer:
                return True
    return False


def build_graph_keys(kb_dir: Path) -> list[tuple[str, set[str]]]:
    """读取公司图谱，返回 [(图谱全名, 归一键集合)]；图谱缺失/坏 JSON 降级为空并留痕。"""
    graph, _trace = knowledge_base.load_company_graph(kb_dir)
    return [(name, company_keys(name)) for name in graph]


def graph_canonical(name: Any, graph_keys: list[tuple[str, set[str]]]) -> str:
    """把公司名归一到图谱全名（图谱别名机制）；未命中返回空串。"""
    keys = company_keys(name)
    if not keys:
        return ""
    for graph_name, gkeys in graph_keys:
        if keys_match(keys, gkeys):
            return graph_name
    return ""


def companies_match(name_a: Any, name_b: Any, graph_keys: list[tuple[str, set[str]]] | None = None) -> bool:
    """两公司名是否同一家：归一键匹配，或图谱别名归一到同一家。"""
    keys_a, keys_b = company_keys(name_a), company_keys(name_b)
    if not keys_a or not keys_b:
        return False
    if keys_match(keys_a, keys_b):
        return True
    if graph_keys:
        canonical_a, canonical_b = graph_canonical(name_a, graph_keys), graph_canonical(name_b, graph_keys)
        if canonical_a and canonical_a == canonical_b:
            return True
    return False


def is_generic_company(name: Any) -> bool:
    """泛化条目判定（非具体公司）：含 其他/等；或非法定全名且剥后缀后以类目词结尾。"""
    raw = str(name or "").strip()
    if not raw:
        return True
    if any(token in raw for token in _GENERIC_TOKENS):
        return True
    if "有限公司" in raw or "股份" in raw:
        return False
    base = _BRACKET_STRIP.sub("", raw)
    stripped = _strip_suffixes(_compact(base))
    return any(stripped.endswith(tail) for tail in _GENERIC_TAILS)


# ---------------------------------------------------------------------------
# case 加载与参考答案抽取（两个定稿 case 的格式基准，结构不符即报错）
# ---------------------------------------------------------------------------

def resolve_kb_dir(kb_dir: str | Path | None = None) -> Path:
    if kb_dir:
        return Path(kb_dir).expanduser()
    raw = os.environ.get("ASA_KNOWLEDGE_BASE_DIR", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_KB_DIR


def load_case_doc(kb_dir: str | Path | None, case_id: str) -> dict[str, Any]:
    """读取 case 文件；缺失/坏 JSON/非对象一律 ReplayCaseError（不静默）。"""
    spec = CASE_SPECS.get(case_id)
    if spec is None:
        raise ReplayCaseError(f"未知回放 case：{case_id}（可选：{sorted(CASE_SPECS)}）")
    directory = resolve_kb_dir(kb_dir) / CASES_SUBDIR
    path = directory / spec["file"]
    if not path.is_file():
        raise ReplayCaseError(f"回放 case 文件缺失：{path}（case_id={case_id}）")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayCaseError(f"回放 case JSON 解析失败：{path}（{exc.__class__.__name__}: {exc}）") from exc
    if not isinstance(doc, dict):
        raise ReplayCaseError(f"回放 case 结构异常：{path} 顶层必须是 JSON 对象")
    return doc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayCaseError(message)


def _dedup_companies(names: list[str]) -> tuple[list[str], list[str]]:
    """参考池归一去重：返回 (具体公司列表, 泛化剔除条目)。键匹配即视为同一家。"""
    concrete: list[str] = []
    generic: list[str] = []
    for name in names:
        name = " ".join(str(name or "").split())
        if not name:
            continue
        if is_generic_company(name):
            generic.append(name)
            continue
        if any(keys_match(company_keys(name), company_keys(existing)) for existing in concrete):
            continue
        concrete.append(name)
    return concrete, generic


def _clean_anchor_values(text: Any) -> list[str]:
    """从锚点参考文本抽取值：去括号内容、截断「——」后注释、按 /／、；; 拆分。"""
    cleaned = _BRACKET_STRIP.sub("", str(text or ""))
    cleaned = cleaned.split("——")[0].split("--")[0]
    return [part.strip() for part in re.split(r"[/／、;；,，]+", cleaned) if part.strip()]


def extract_reference(case_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """抽取 case 参考答案：目标池（T1/T2 具体公司）、有效关键词组、四锚点。

    结构不符（缺键/类型错）抛 ReplayCaseError——定稿 case 被改坏时回放必须炸出声。
    """
    if case_id == CASE_SILAN:
        archetype = doc.get("job_archetype")
        pool = doc.get("target_company_pool")
        groups = doc.get("keyword_groups")
        _require(isinstance(archetype, dict), f"{CASE_SILAN} 缺 job_archetype 对象")
        _require(isinstance(pool, dict), f"{CASE_SILAN} 缺 target_company_pool 对象")
        _require(isinstance(groups, list) and groups, f"{CASE_SILAN} 缺 keyword_groups 数组")
        directions = archetype.get("directions")
        _require(isinstance(directions, list) and directions, f"{CASE_SILAN} job_archetype.directions 缺失")
        names: list[str] = []
        for tier_key in ("T1_competitor_device", "T2_customer_OEM"):
            block = pool.get(tier_key)
            _require(isinstance(block, dict), f"{CASE_SILAN} target_company_pool.{tier_key} 缺失")
            companies = block.get("companies")
            _require(isinstance(companies, list) and companies, f"{CASE_SILAN} {tier_key}.companies 缺失")
            names.extend(str(company.get("name") or "") for company in companies if isinstance(company, dict))
        reference_anchors = {
            "customer_of_customer": sorted({str(customer) for d in directions for customer in d.get("customers") or []}),
            "product_tech_line": sorted({str(product) for d in directions for product in d.get("products") or []}),
            "competitive_landscape": sorted({str(c) for d in directions for c in d.get("competitors") or []}),
            "scenario_track": [str(d.get("name") or "") for d in directions if str(d.get("name") or "").strip()],
        }
        pool_concrete, pool_generic = _dedup_companies(names)
        return {
            "pool": pool_concrete,
            "pool_generic_excluded": pool_generic,
            "keyword_groups": [
                {"group": str(group.get("group") or ""), "terms": [str(t) for t in group.get("terms") or []]}
                for group in groups if isinstance(group, dict)
            ],
            "anchors": reference_anchors,
        }

    if case_id == CASE_CHANGYUE:
        positions = doc.get("positions")
        _require(isinstance(positions, list) and positions, f"{CASE_CHANGYUE} 缺 positions 数组")
        priority = [p for p in positions if isinstance(p, dict) and p.get("priority_v1.1") == 1]
        position = priority[0] if priority else positions[0]
        _require(isinstance(position.get("title"), str) and position["title"], f"{CASE_CHANGYUE} 回放岗位缺 title")
        analysis = position.get("anchor_analysis")
        pool = position.get("target_company_pool")
        groups = position.get("keyword_groups")
        _require(isinstance(analysis, dict), f"{CASE_CHANGYUE} 回放岗位缺 anchor_analysis（四锚点参考答案）")
        _require(isinstance(pool, dict), f"{CASE_CHANGYUE} 回放岗位缺 target_company_pool")
        _require(isinstance(groups, list) and groups, f"{CASE_CHANGYUE} 回放岗位缺 keyword_groups")
        t1 = pool.get("T1_same_layer")
        t2 = pool.get("T2_adjacent")
        _require(isinstance(t1, dict), f"{CASE_CHANGYUE} 回放岗位缺 target_company_pool.T1_same_layer")
        names = [str(item) for item in t1.get("companies_international") or []]
        for list_key in ("companies_domestic_added_v1.1", "companies_domestic_databacked_v1.2"):
            names.extend(
                str(company.get("name") or "")
                for company in t1.get(list_key) or [] if isinstance(company, dict)
            )
        if isinstance(t2, dict):
            names.extend(str(item) for item in t2.get("companies") or [])
        pool_concrete, pool_generic = _dedup_companies(names)
        competitors_note = str(analysis.get("competitors") or "")
        reference_anchors = {
            "customer_of_customer": _clean_anchor_values(analysis.get("customer_of_client")),
            "product_tech_line": [str(item) for item in analysis.get("product_tech") or []],
            # case 标注「缺失，见 target_company_pool.T1」→ 参考答案用 T1 具体公司池
            "competitive_landscape": pool_concrete if "缺失" in competitors_note else _clean_anchor_values(competitors_note),
            "scenario_track": _clean_anchor_values(analysis.get("scene")),
        }
        return {
            "pool": pool_concrete,
            "pool_generic_excluded": pool_generic,
            "keyword_groups": [
                {"group": str(group.get("group") or ""), "terms": [str(t) for t in group.get("terms") or []]}
                for group in groups if isinstance(group, dict)
            ],
            "anchors": reference_anchors,
            "position_title": position["title"],
        }

    raise ReplayCaseError(f"未知回放 case：{case_id}")


# ---------------------------------------------------------------------------
# 岗位上下文构造（按 case 文件内容；士兰微 L1 / 长越 L3）
# ---------------------------------------------------------------------------

def build_job_spec(case_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    """构造回放岗位（client/title/定级语料全部取自 case 文件）。"""
    if case_id == CASE_SILAN:
        archetype = doc["job_archetype"]
        directions = archetype.get("directions") or []
        customers = sorted({str(c) for d in directions for c in d.get("customers") or []})
        competitors = sorted({str(c) for d in directions for c in d.get("competitors") or []})
        products = sorted({str(p) for d in directions for p in d.get("products") or []})
        direction_names = "、".join(str(d.get("name") or "") for d in directions if d.get("name"))
        # L1：客户一手需求梳理语料（锚点标签为 case 原文内容）
        summary = (
            f"{archetype.get('client_profile', '')}。覆盖 {direction_names} 方向。"
            f"主要客户：{'、'.join(customers)}。目标友商：{'、'.join(competitors)}。"
            f"重点产品：{'、'.join(products)}。"
        )
        return {
            "client": str(archetype.get("client") or ""),
            "title": str(archetype.get("title") or ""),
            "location": "杭州",
            "summary": summary,
            "hard_requirements": "本科及以上；8 年以上电源管理/技术市场经验；熟悉多相控制器、DrMOS、POL 产品线",
            "ability_keywords": "、".join([*products, "三次电源", "板级电源", "VRM"]),
            "target_companies": "、".join(competitors),
        }
    if case_id == CASE_CHANGYUE:
        positions = doc["positions"]
        priority = [p for p in positions if isinstance(p, dict) and p.get("priority_v1.1") == 1]
        position = priority[0] if priority else positions[0]
        # L3：仅 JD（case meta 声明：友商/客户锚点全部缺失），不给任何顾问补充
        return {
            "client": str((doc.get("client_profile") or {}).get("client") or ""),
            "title": str(position.get("title") or ""),
            "location": str(position.get("location") or "杭州"),
            "summary": str(position.get("essence") or ""),
            "hard_requirements": "；".join(str(item) for item in position.get("hard_requirements") or []),
            "ability_keywords": "运动控制、EtherCAT、TwinCAT、Codesys、RTOS、多轴运动学、闭环控制",
            "target_companies": "",
        }
    raise ReplayCaseError(f"未知回放 case：{case_id}")


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def evaluate_pool(
    agent_pool: list[dict[str, Any]],
    reference_pool: list[str],
    graph_keys: list[tuple[str, set[str]]],
) -> dict[str, Any]:
    """指标①：目标池重合度（recall/precision + 未命中明细）。"""
    matched_ref: set[int] = set()
    matched_agent: set[int] = set()
    for index, reference in enumerate(reference_pool):
        for agent_index, agent in enumerate(agent_pool):
            if companies_match(reference, agent["name"], graph_keys):
                matched_ref.add(index)
                matched_agent.add(agent_index)
                break
    for agent_index, agent in enumerate(agent_pool):
        if agent_index in matched_agent:
            continue
        if any(companies_match(agent["name"], reference, graph_keys) for reference in reference_pool):
            matched_agent.add(agent_index)
    total_ref, total_agent = len(reference_pool), len(agent_pool)
    return {
        "recall": round(len(matched_ref) / total_ref, 4) if total_ref else 0.0,
        "precision": round(len(matched_agent) / total_agent, 4) if total_agent else 0.0,
        "reference_size": total_ref,
        "agent_size": total_agent,
        "missed_reference": [reference_pool[i] for i in range(total_ref) if i not in matched_ref],
        "extra_agent": [
            {"name": agent_pool[i]["name"], "source": agent_pool[i].get("source", ""), "tier": agent_pool[i].get("tier", "")}
            for i in range(total_agent) if i not in matched_agent
        ],
    }


def _term_covered(reference_term: str, agent_terms: list[str]) -> bool:
    ref = _compact(reference_term)
    if not ref:
        return False
    for agent_term in agent_terms:
        agent = _compact(agent_term)
        if not agent:
            continue
        if ref == agent:
            return True
        shorter, longer = (ref, agent) if len(ref) <= len(agent) else (agent, ref)
        if len(shorter) >= 2 and shorter in longer:
            return True
    return False


def evaluate_keywords(agent_groups: list[dict[str, Any]], reference_groups: list[dict[str, Any]]) -> dict[str, Any]:
    """指标②：关键词有效率（组级命中：组内词覆盖率 ≥0.5）。"""
    agent_terms = [term for group in agent_groups for term in group.get("terms") or []]
    details: list[dict[str, Any]] = []
    covered_groups = 0
    for group in reference_groups:
        terms = [str(term) for term in group.get("terms") or [] if str(term or "").strip()]
        covered = [term for term in terms if _term_covered(term, agent_terms)]
        hit = bool(terms) and len(covered) / len(terms) >= 0.5
        covered_groups += 1 if hit else 0
        details.append({
            "group": group.get("group", ""),
            "hit": hit,
            "covered_terms": covered,
            "missing_terms": [term for term in terms if term not in covered],
        })
    total = len(reference_groups)
    return {
        "coverage": round(covered_groups / total, 4) if total else 0.0,
        "covered_groups": covered_groups,
        "total_groups": total,
        "groups": details,
    }


def _anchor_value_hit(reference_values: list[str], agent_values: list[str]) -> bool:
    return any(_term_covered(reference, agent_values) for reference in reference_values)


def evaluate_anchors(agent_anchors: dict[str, Any], reference_anchors: dict[str, list[str]]) -> dict[str, Any]:
    """指标③：锚点完整率（缺失 0 / 锚定偏差 0.5 / 正确锚定 1，四锚点均值）。"""
    details: dict[str, Any] = {}
    total_score = 0.0
    for key in strategy_v2.ANCHOR_KEYS:
        agent = agent_anchors.get(key) if isinstance(agent_anchors.get(key), dict) else {}
        reference_values = [str(value) for value in reference_anchors.get(key) or [] if str(value or "").strip()]
        agent_values = [str(value) for value in agent.get("values") or []]
        if not agent.get("present"):
            score = 0.0
            note = "锚点缺失"
        elif reference_values and _anchor_value_hit(reference_values, agent_values):
            score = 1.0
            note = "与参考答案重合"
        elif not reference_values:
            score = 1.0
            note = "case 无参考值，present 即记锚定"
        else:
            score = 0.5
            note = "已锚定但与参考答案无重合（锚定偏差）"
        total_score += score
        details[key] = {
            "label": strategy_v2.ANCHOR_LABELS[key],
            "score": score,
            "note": note,
            "agent_present": bool(agent.get("present")),
            "agent_source": str(agent.get("source") or ""),
            "agent_values": agent_values[:12],
            "reference_values": reference_values[:12],
        }
    return {"score": round(total_score / len(strategy_v2.ANCHOR_KEYS), 4), "anchors": details}


# ---------------------------------------------------------------------------
# 回放执行（临时库 + FakeLLM 确定性模式；知识库只读）
# ---------------------------------------------------------------------------

_REPLAY_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,location TEXT,status TEXT,
  hard_requirements TEXT,ability_keywords TEXT,target_companies TEXT,exclusions TEXT,summary TEXT,updated_at TEXT);
"""


def _seed_replay_db(db_path: Path, job_spec: dict[str, Any]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_REPLAY_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,?)", (job_spec["client"],))
        conn.execute(
            "INSERT INTO jobs VALUES (1,1,?,?,?,?,?,?,?,?,?)",
            (
                job_spec["title"], job_spec["location"], "已发布",
                job_spec["hard_requirements"], job_spec["ability_keywords"],
                job_spec["target_companies"], "", job_spec["summary"], "2026-07-23",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_replay(case_id: str, kb_dir: str | Path | None = None) -> dict[str, Any]:
    """跑单个 case 的回放评测，返回指标与明细（确定性，可复现）。

    kb_dir 解析顺序：参数 > ASA_KNOWLEDGE_BASE_DIR > 缺省目录；解析后临时写入环境变量，
    保证策略链内部的知识库读取与回放同一目录，结束后恢复原环境。
    """
    resolved = resolve_kb_dir(kb_dir)
    doc = load_case_doc(resolved, case_id)
    reference = extract_reference(case_id, doc)
    job_spec = build_job_spec(case_id, doc)
    version = str((doc.get("meta") or {}).get("version") or "")

    old_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
    os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(resolved)
    temp = tempfile.TemporaryDirectory()
    service: AgentService | None = None
    try:
        db_path = Path(temp.name) / "replay.db"
        _seed_replay_db(db_path, job_spec)
        # FakeLLM 不传 search_strategy → generate_search_strategy 走 deterministic_fallback，
        # 策略生成链全部步骤确定可复现（PRD §6 进 CI 门槛的硬性要求）。
        service = AgentService(db_path, FakeLLM({}))
        result = service.capability_runtime.run_search_strategy(
            {"type": "job", "id": 1}, {"objective": "S4-4 回放评测"}
        )
    finally:
        if service is not None:
            service.close()
        temp.cleanup()
        if old_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = old_env

    if "strategy_v2" not in result:
        error = result.get("strategy_v2_error") or {}
        raise ReplayCaseError(
            f"回放 case {case_id} 未产出合法 strategy_v2：{error.get('errors') or result.get('summary')}"
        )
    v2 = result["strategy_v2"]
    ok, errors = strategy_v2.validate_strategy_v2(v2)
    if not ok:
        raise ReplayCaseError(f"回放 case {case_id} 的 strategy_v2 校验失败：{errors}")

    graph_keys = build_graph_keys(resolve_kb_dir(resolved))
    agent_pool = [
        {"name": str(company.get("name") or ""), "source": str(company.get("source") or ""), "tier": str(entry.get("tier") or "")}
        for entry in v2.get("step2_target_pool") or []
        for company in entry.get("companies") or []
        if str(entry.get("tier") or "") in {"T1", "T2"} and isinstance(company, dict)
    ]
    pool_metrics = evaluate_pool(agent_pool, reference["pool"], graph_keys)
    keyword_metrics = evaluate_keywords(v2.get("step4_keyword_groups") or [], reference["keyword_groups"])
    anchor_metrics = evaluate_anchors(v2.get("anchors") or {}, reference["anchors"])

    return {
        "case_id": case_id,
        "case_file": str(resolve_kb_dir(resolved) / CASES_SUBDIR / CASE_SPECS[case_id]["file"]),
        "case_version": version,
        "label": CASE_SPECS[case_id]["label"],
        "job": {"client": job_spec["client"], "title": job_spec["title"]},
        "input_level": v2.get("input_level"),
        "expected_input_level": CASE_SPECS[case_id]["expected_input_level"],
        "generation_mode": str((result.get("strategy") or {}).get("generation", {}).get("mode") or ""),
        "missing_anchors": list(v2.get("missing_anchors") or []),
        "step2_source_distribution": v2.get("step2_source_distribution") or {},
        "metrics": {
            "pool_recall": pool_metrics["recall"],
            "pool_precision": pool_metrics["precision"],
            "keyword_coverage": keyword_metrics["coverage"],
            "anchor_completeness": anchor_metrics["score"],
        },
        "details": {
            "pool": pool_metrics,
            "pool_generic_excluded": reference["pool_generic_excluded"],
            "keywords": keyword_metrics,
            "anchors": anchor_metrics,
        },
    }


def evaluate_all(kb_dir: str | Path | None = None, case_ids: list[str] | None = None) -> dict[str, Any]:
    """跑全部（或指定）case，返回逐 case 结果 + 汇总（指标算术均值）。"""
    ids = case_ids or list(CASE_SPECS)
    cases = [run_replay(case_id, kb_dir) for case_id in ids]
    overall = {
        key: round(sum(case["metrics"][key] for case in cases) / len(cases), 4) if cases else 0.0
        for key in METRIC_KEYS
    }
    return {"kb_dir": str(resolve_kb_dir(kb_dir)), "cases": cases, "overall": overall}


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "=" * 68,
        "策略回放评测（S4-4，确定性模式）",
        f"知识库目录：{report['kb_dir']}",
        "=" * 68,
    ]
    for case in report["cases"]:
        metrics = case["metrics"]
        pool = case["details"]["pool"]
        keywords = case["details"]["keywords"]
        anchors = case["details"]["anchors"]
        lines.append(f"\n■ {case['case_id']}（{case['label']}，case {case['case_version']}）")
        lines.append(
            f"  定级 {case['input_level']}（预期 {case['expected_input_level']}）"
            f"｜生成模式 {case['generation_mode']}｜step2 来源 {case['step2_source_distribution']}"
        )
        lines.append(
            f"  ① 目标池重合度  recall={_fmt(metrics['pool_recall'])}  precision={_fmt(metrics['pool_precision'])}"
            f"（参考池 {pool['reference_size']} 家 / Agent T1T2 池 {pool['agent_size']} 家）"
        )
        lines.append(
            f"  ② 关键词有效率  {_fmt(metrics['keyword_coverage'])}"
            f"（{keywords['covered_groups']}/{keywords['total_groups']} 组命中）"
        )
        lines.append(f"  ③ 锚点完整率    {_fmt(metrics['anchor_completeness'])}（四锚点均值）")
        lines.append("  未命中明细：")
        missed = pool["missed_reference"]
        lines.append(f"    - case 池未覆盖公司（{len(missed)}）：{'、'.join(missed) if missed else '无'}")
        extra = pool["extra_agent"]
        lines.append(
            f"    - Agent 池多出公司（{len(extra)}，拉低 precision）："
            + ("、".join(f"{item['name']}({item['source']})" for item in extra) if extra else "无")
        )
        generic = case["details"]["pool_generic_excluded"]
        if generic:
            lines.append(f"    - 参考池剔除的泛化条目（{len(generic)}）：{'、'.join(generic)}")
        for group in keywords["groups"]:
            if not group["hit"]:
                lines.append(f"    - 关键词组未命中：{group['group']}（缺 {'、'.join(group['missing_terms'][:8])}）")
        for key, anchor in anchors["anchors"].items():
            if anchor["score"] < 1:
                lines.append(
                    f"    - 锚点 {anchor['label']}：{anchor['score']}（{anchor['note']}；"
                    f"Agent={anchor['agent_values'][:4] or '缺失'} 参考={anchor['reference_values'][:4]}）"
                )
    overall = report["overall"]
    lines.append("\n" + "=" * 68)
    lines.append(
        f"汇总（{len(report['cases'])} case 均值）：recall={_fmt(overall['pool_recall'])}"
        f"  precision={_fmt(overall['pool_precision'])}"
        f"  keyword={_fmt(overall['keyword_coverage'])}"
        f"  anchor={_fmt(overall['anchor_completeness'])}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S4-4 策略回放评测（确定性，可进 CI）")
    parser.add_argument("--kb-dir", default=None, help="知识库目录（缺省读 ASA_KNOWLEDGE_BASE_DIR 或 ASA 仓）")
    parser.add_argument("--case", action="append", choices=sorted(CASE_SPECS), help="只跑指定 case（可多次）")
    parser.add_argument("--json", action="store_true", help="打印 JSON 报告")
    parser.add_argument("--out", default=None, help="把 JSON 报告写入该文件")
    args = parser.parse_args(argv)

    try:
        report = evaluate_all(args.kb_dir, args.case)
    except ReplayCaseError as exc:
        print(f"回放评测失败：{exc}", file=sys.stderr)
        return 2

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
