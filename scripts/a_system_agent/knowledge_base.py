"""S4-2：知识库消费 —— 客户画像挂载、公司图谱查询、restricted 层边界。

口径来源（事实源，运行时只读；目录可用 ASA_KNOWLEDGE_BASE_DIR 覆盖，
缺省 /Users/messi/Documents/ASA/knowledge_base）：
- kb_client_profiles_v1.json：客户画像（233 家），命中后把（赛道/卖点/面试流程/用人偏好/
  目标池/注意事项）注入策略生成上下文与岗位详情上下文（PRD §3.1）；
- kb_company_graph_jsj_v1.json：589 家公司图谱（赛道/主营业务/四分类标签），供 strategy_v2
  step2 公司池推导，命中公司带 source=kb_graph + confidence（PRD §3.2）；
- cases/case_*.json 的 restricted 字段：仅禁挖名单/竞业限制进入策略约束；
  费率/顾问手机号/offer 金额/话术红线永远不进任何生成上下文与对外输出（PRD §3.3，P0 边界）。

图谱 governance（PRD §3.2/§8）：公司命中只用于召回和排序，必须回候选人详情核验本人证据；
赛道分类是公开信息归类，不作为候选人行业证据。
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from .strategy_v2 import knowledge_base_dir

CLIENT_PROFILES_FILE = "kb_client_profiles_v1.json"
COMPANY_GRAPH_FILE = "kb_company_graph_jsj_v1.json"

# 画像注入白名单（PRD §3.1：赛道/卖点/面试流程/用人偏好/目标池/注意事项）。
# rate（费率）在 kb meta 中已声明 restricted；website/schedule/comp_benefits 等一律不注入。
PROFILE_CONTEXT_FIELDS = (
    "track",
    "selling_points",
    "interview_process",
    "hiring_preferences",
    "target_pool_hint",
    "notes",
)

# restricted 层白名单：仅禁挖名单与竞业限制可进入策略约束（source=restricted_client）；
# 其余键（费率/手机号/offer 金额/话术红线/pii 备注等）永远不得出库进入任何上下文。
_RESTRICTED_ALLOWED_KEYS = ("banned_companies", "banned_rule")
_RESTRICTED_NON_COMPETE_PATTERN = re.compile(r"竞业|non_?compete", re.I)

# 公司名规范化时剥离的尾部公司后缀（长后缀在前，循环剥离）
_CORP_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团", "公司")
_BRACKET_PATTERN = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^]]*\]")

# 图谱打分时的高噪 bigram（不具备区分度）
_GRAPH_STOPGRAMS = {
    "设备", "公司", "有限", "科技", "股份", "集团", "制造", "技术", "研发", "生产",
    "上海", "苏州", "杭州", "北京", "深圳", "无锡", "常州", "嘉兴", "宁波", "南京",
}


def _read_json(path: Path) -> tuple[Any, str]:
    """读取 JSON 文件；返回 (文档, 错误信息)。运行时只读，绝不写文件。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return None, f"{path.name} 解析失败（{exc.__class__.__name__}）"


# ---------------------------------------------------------------------------
# 3.1 客户画像挂载
# ---------------------------------------------------------------------------

def load_client_profiles(kb_dir: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 kb_client_profiles_v1.json；缺失/坏 JSON 一律降级为空画像库并留痕（不抛异常）。"""
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    path = directory / CLIENT_PROFILES_FILE
    if not path.is_file():
        return [], [f"客户画像库 {CLIENT_PROFILES_FILE} 缺失（{directory}），按无画像处理"]
    doc, error = _read_json(path)
    if error:
        return [], [f"客户画像库{error}，按无画像处理"]
    profiles = doc.get("profiles") if isinstance(doc, dict) else None
    if not isinstance(profiles, list):
        return [], [f"客户画像库 {CLIENT_PROFILES_FILE} 结构异常（缺 profiles 数组），按无画像处理"]
    valid = [profile for profile in profiles if isinstance(profile, dict) and str(profile.get("client") or "").strip()]
    return valid, [f"已加载客户画像 {len(valid)} 家（{CLIENT_PROFILES_FILE}）"]


def normalize_client_name(name: Any) -> str:
    """客户名规范化：去括号及其内容、去空白、去尾部公司后缀、小写。"""
    text = _BRACKET_PATTERN.sub("", str(name or ""))
    text = "".join(text.split()).lower()
    changed = True
    while changed and text:
        changed = False
        for suffix in _CORP_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
    return text


def name_match_rule(target_raw: str, target_norm: str, candidate: str) -> tuple[str, str]:
    """确定性匹配（精确/别名）：返回 (rule, reason)；不匹配返回 ("", "")。"""
    candidate_raw = " ".join(str(candidate or "").split())
    candidate_norm = normalize_client_name(candidate)
    if not target_raw or not candidate_raw or not target_norm or not candidate_norm:
        return "", ""
    if target_raw == candidate_raw:
        return "exact", "客户名精确一致"
    if target_norm == candidate_norm:
        return "alias", f"去括号/规范化后一致：{target_norm}"
    shorter, longer = sorted((target_norm, candidate_norm), key=len)
    if len(shorter) >= 3 and shorter in longer:
        return "alias", f"规范化别名包含关系：{shorter} ⊆ {longer}"
    return "", ""


def match_client_profile(
    client: Any,
    profiles: list[dict[str, Any]] | None = None,
    *,
    kb_dir: str | Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """客户画像三级匹配：精确 → 去括号/规范化别名 → 模糊（必须标记需人工确认）。

    返回 (命中信息或 None, 留痕)。命中信息：{name, rule, needs_confirmation, reason, profile}。
    模糊匹配 needs_confirmation=True，绝不静默命中；无命中返回 None。
    """
    if profiles is None:
        profiles, trace = load_client_profiles(kb_dir)
    else:
        trace = []
    target_raw = " ".join(str(client or "").split())
    target_norm = normalize_client_name(client)
    if not target_norm:
        trace.append("客户名为空，按未挂载画像处理")
        return None, trace

    best_fuzzy: tuple[float, dict[str, Any]] | None = None
    for profile in profiles:
        name = str(profile.get("client") or "")
        rule, reason = name_match_rule(target_raw, target_norm, name)
        if rule:
            trace.append(f"客户“{client}”命中画像“{name}”（{rule}：{reason}）")
            return {"name": name, "rule": rule, "needs_confirmation": False, "reason": reason, "profile": profile}, trace
        candidate_norm = normalize_client_name(name)
        ratio = difflib.SequenceMatcher(None, target_norm, candidate_norm).ratio()
        if ratio >= 0.6 and (best_fuzzy is None or ratio > best_fuzzy[0]):
            best_fuzzy = (ratio, profile)

    if best_fuzzy is not None:
        ratio, profile = best_fuzzy
        name = str(profile.get("client") or "")
        trace.append(
            f"客户“{client}”与画像“{name}”为模糊匹配（相似度 {ratio:.2f}），已标记需人工确认，不静默命中"
        )
        return {
            "name": name, "rule": "fuzzy", "needs_confirmation": True,
            "reason": f"模糊匹配（相似度 {ratio:.2f}），需人工确认", "profile": profile,
        }, trace
    trace.append(f"客户“{client}”未命中任何客户画像")
    return None, trace


def profile_context(profile: dict[str, Any]) -> dict[str, str]:
    """画像注入上下文（白名单六类：赛道/卖点/面试流程/用人偏好/目标池/注意事项）。

    绝不包含 rate/website/schedule/comp_benefits 等字段（费率为 restricted）。
    """
    track = "、".join(
        part
        for part in (
            str(profile.get("track") or "").strip(),
            str(profile.get("sub_track") or "").strip(),
            str(profile.get("process_track") or "").strip(),
        )
        if part
    )
    context = {
        "track": track,
        "selling_points": str(profile.get("selling_points") or "").strip(),
        "interview_process": str(profile.get("interview_process") or "").strip(),
        "hiring_preferences": str(profile.get("interviewer_style") or "").strip(),
        "target_pool_hint": str(profile.get("competitor_target") or "").strip(),
        "notes": str(profile.get("rec_notes") or "").strip(),
    }
    return {key: value for key, value in context.items() if value}


def profile_matched_info(match: dict[str, Any] | None) -> dict[str, Any]:
    """策略对象留痕：profile_matched{name, rule, needs_confirmation}（挂/未挂都留）。"""
    if not match:
        return {"name": "", "rule": "none", "needs_confirmation": False}
    return {
        "name": str(match.get("name") or ""),
        "rule": str(match.get("rule") or ""),
        "needs_confirmation": bool(match.get("needs_confirmation")),
    }


# ---------------------------------------------------------------------------
# 3.2 公司图谱查询
# ---------------------------------------------------------------------------

def load_company_graph(kb_dir: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读取 kb_company_graph_jsj_v1.json；缺失/坏 JSON 降级为空图谱并留痕（不崩）。"""
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    path = directory / COMPANY_GRAPH_FILE
    if not path.is_file():
        return {}, [f"公司图谱 {COMPANY_GRAPH_FILE} 缺失（{directory}），降级为空图谱"]
    doc, error = _read_json(path)
    if error:
        return {}, [f"公司图谱{error}，降级为空图谱"]
    companies = doc.get("companies") if isinstance(doc, dict) else None
    if not isinstance(companies, dict):
        return {}, [f"公司图谱 {COMPANY_GRAPH_FILE} 结构异常（缺 companies 对象），降级为空图谱"]
    graph: dict[str, dict[str, Any]] = {}
    for name, info in companies.items():
        if not isinstance(info, dict) or not str(name or "").strip():
            continue
        categories = info.get("categories")
        graph[str(name)] = {
            "track": str(info.get("track") or ""),
            "business": str(info.get("business") or ""),
            "categories": [str(item) for item in categories if str(item or "").strip()] if isinstance(categories, list) else [],
        }
    return graph, [f"已加载公司图谱 {len(graph)} 家（{COMPANY_GRAPH_FILE}）"]


def _bigrams(text: str) -> set[str]:
    """中文二元切分（免分词），剥离高噪 bigram；用于赛道/主营业务的可解释匹配。"""
    compact = re.sub(r"[\s｜|/、，,；;（）()【】\[\]：:·\-]+", "", str(text or ""))
    return {compact[index : index + 2] for index in range(len(compact) - 1)} - _GRAPH_STOPGRAMS


def search_companies(
    graph: dict[str, dict[str, Any]],
    *,
    query_text: str = "",
    categories: list[str] | tuple[str, ...] = (),
    limit: int = 25,
) -> list[dict[str, Any]]:
    """按赛道/主营业务/四分类标签检索公司。

    打分可解释：查询文本与（赛道+主营业务）的 bigram 重合数 + 分类标签命中加分。
    confidence：score>=4 → high；2-3 → medium；1 → low；0 不召回。
    """
    query_grams = _bigrams(query_text)
    wanted_categories = [str(item) for item in categories if str(item or "").strip()]
    hits: list[dict[str, Any]] = []
    if not query_grams and not wanted_categories:
        return []
    for name, info in (graph or {}).items():
        matched = sorted(query_grams & _bigrams(f"{info.get('track', '')} {info.get('business', '')}"))
        matched_categories = [item for item in wanted_categories if item in (info.get("categories") or [])]
        if wanted_categories and not matched_categories:
            continue
        score = len(matched) + 2 * len(matched_categories)
        if score <= 0:
            continue
        confidence = "high" if score >= 4 else "medium" if score >= 2 else "low"
        hits.append(
            {
                "name": name,
                "track": info.get("track", ""),
                "business": info.get("business", ""),
                "categories": list(info.get("categories") or []),
                "score": score,
                "confidence": confidence,
                "matched_tokens": matched,
            }
        )
    hits.sort(key=lambda item: (-item["score"], item["name"]))
    return hits[: max(1, int(limit))]


def derive_graph_pool(
    graph: dict[str, dict[str, Any]],
    *,
    query_text: str = "",
    categories: list[str] | tuple[str, ...] = (),
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[str]]:
    """为 strategy_v2 step2 推导图谱公司：source=kb_graph + confidence，全部留痕。

    governance：命中只用于召回与排序，必须回候选人详情核验本人证据。
    """
    hits = search_companies(graph, query_text=query_text, categories=categories, limit=limit)
    pool = [
        {"name": hit["name"], "source": "kb_graph", "confidence": hit["confidence"]}
        for hit in hits
    ]
    if pool:
        brief = "、".join(f"{hit['name']}({hit['confidence']}:{'/'.join(hit['matched_tokens'][:4])})" for hit in hits[:5])
        trace = [
            f"公司图谱按赛道/主营业务召回 {len(pool)} 家：{brief}"
            "；只用于召回与排序，须回候选人详情核验本人证据"
        ]
    else:
        trace = ["公司图谱未召回到相关公司（或图谱为空），step2 不使用 kb_graph 来源"]
    return pool, trace


# ---------------------------------------------------------------------------
# 3.3 restricted 层（白名单读取，P0 边界）
# ---------------------------------------------------------------------------

def load_restricted_constraints(
    client: Any,
    *,
    kb_dir: str | Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """按客户读取 cases/case_*.json 的 restricted 白名单约束。

    白名单：仅禁挖名单（banned_companies/banned_rule）与竞业限制类键进入返回结果；
    费率/顾问手机号/offer 金额/话术红线等键只把【键名】记入 skipped_keys 留痕，
    键值永远不出库。客户匹配只用精确/别名（restricted 宁可 miss 不可错配，不用模糊匹配）。
    """
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    cases_dir = directory / "cases"
    trace: list[str] = []
    if not cases_dir.is_dir():
        return None, [f"restricted 层目录缺失（{cases_dir}），按无客户约束处理"]
    target_raw = " ".join(str(client or "").split())
    target_norm = normalize_client_name(client)
    if not target_norm:
        return None, ["客户名为空，不读取 restricted 层"]
    for path in sorted(cases_dir.glob("case_*.json")):
        doc, error = _read_json(path)
        if error:
            trace.append(f"restricted 层{error}，跳过该 case")
            continue
        restricted = doc.get("restricted") if isinstance(doc, dict) else None
        if not isinstance(restricted, dict) or not restricted:
            continue
        client_profile = doc.get("client_profile") if isinstance(doc.get("client_profile"), dict) else {}
        case_client = str(client_profile.get("name") or client_profile.get("client") or "")
        rule, reason = name_match_rule(target_raw, target_norm, case_client)
        if not rule:
            continue
        allowed: dict[str, Any] = {}
        for key in _RESTRICTED_ALLOWED_KEYS:
            if restricted.get(key) not in (None, "", []):
                allowed[key] = restricted[key]
        for key, value in restricted.items():
            if key not in allowed and _RESTRICTED_NON_COMPETE_PATTERN.search(str(key)) and value not in (None, "", []):
                allowed[str(key)] = value
        skipped = sorted(str(key) for key in restricted if key not in allowed)
        trace.append(
            f"客户“{client}”命中 restricted 层 {path.name}（{rule}：{reason}）；"
            f"白名单键 {sorted(allowed)} 进入策略约束，受限键 {skipped} 已拦截（键值不出库）"
        )
        return {
            "client": case_client,
            "matched_by": rule,
            "constraints": allowed,
            "skipped_keys": skipped,
            "source_file": path.name,
        }, trace
    trace.append(f"客户“{client}”无 restricted 层约束")
    return None, trace


def restricted_negative_rules(info: dict[str, Any] | None) -> list[dict[str, str]]:
    """把 restricted 白名单约束转成 strategy_v2 negative_rules（source=restricted_client）。

    只消费 load_restricted_constraints 的白名单输出；禁挖名单按在职保护口径表述。
    """
    if not info:
        return []
    constraints = info.get("constraints") if isinstance(info.get("constraints"), dict) else {}
    rules: list[dict[str, str]] = []
    banned = [str(item).strip() for item in constraints.get("banned_companies") or [] if str(item or "").strip()]
    if banned:
        banned_rule = str(constraints.get("banned_rule") or "").strip()
        text = f"禁挖名单（在职保护）：{'、'.join(banned)}"
        if banned_rule:
            text += f"（{banned_rule}）"
        rules.append({"type": "禁挖名单", "rule": text, "source": "restricted_client"})
    for key, value in constraints.items():
        if key in _RESTRICTED_ALLOWED_KEYS:
            continue
        if isinstance(value, list):
            value = "、".join(str(item) for item in value if str(item or "").strip())
        text = str(value or "").strip()
        if text:
            rules.append({"type": "竞业限制", "rule": text, "source": "restricted_client"})
    return rules
