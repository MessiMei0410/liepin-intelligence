"""S4-2：知识库消费 —— 客户画像挂载、公司图谱查询、restricted 层边界。

口径来源（事实源，运行时只读；目录可用 ASA_KNOWLEDGE_BASE_DIR 覆盖，
缺省 /Users/messi/Documents/ASA/knowledge_base）：
- kb_client_profiles_v1.json：客户画像（233 家），命中后把（赛道/卖点/面试流程/用人偏好/
  目标池/注意事项）注入策略生成上下文与岗位详情上下文（PRD §3.1）；
- kb_company_graph_jsj_v1.json：589 家公司图谱（赛道/主营业务/四分类标签），供 strategy_v2
  step2 公司池推导，命中公司带 source=kb_graph + confidence（PRD §3.2）；
- cases/case_*.json 的 restricted 字段：仅禁挖名单/竞业限制进入策略约束；
  费率/顾问手机号/offer 金额/话术红线永远不进任何生成上下文与对外输出（PRD §3.3，P0 边界）；
- kb_skill_ontology_semiconductor_v1.json：半导体/硬件技能本体（技能族→技能→别名/相关技能/
  证据形式），策略生成做关键词别名归一与相关词提示（source=kb_skill）、简历评估做技能证据
  归一比对；命中别名单独不构成技能证据；
- kb_level_mapping_v1.json：职级映射库（职级带 + 互联网 P/M、半导体原厂、设备厂序列对照），
  策略 step3 职级映射优先查本库（source=kb_level），查不到再走 LLM/原型路径；
- kb_agent_confirmed_rules_v1.json：知识增补提案经顾问确认后的规则库（negative_rule/
  skill_alias/level_mapping 三类，proposed_by=consultant_confirmed，写入方 asa_core.
  knowledge_proposals）。消费侧"有则增强、无则现状"：negative_rule 由 negative_rules
  模块追加进五类清单（source=consultant_confirmed）；skill_alias 并入 normalize_skill
  别名归一；level_mapping 在 map_level 中优先于内置库。文件缺失/坏 JSON 一律降级为空。

图谱 governance（PRD §3.2/§8）：公司命中只用于召回和排序，必须回候选人详情核验本人证据；
赛道分类是公开信息归类，不作为候选人行业证据。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .strategy_v2 import knowledge_base_dir

CLIENT_PROFILES_FILE = "kb_client_profiles_v1.json"
COMPANY_GRAPH_FILE = "kb_company_graph_jsj_v1.json"
SKILL_ONTOLOGY_FILE = "kb_skill_ontology_semiconductor_v1.json"
LEVEL_MAPPING_FILE = "kb_level_mapping_v1.json"

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
                # 校准覆盖层合并后条目带 source=consultant_calibrated；原始图谱条目缺省 kb_graph。
                "source": str(info.get("source") or "kb_graph"),
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
        {"name": hit["name"], "source": hit["source"], "confidence": hit["confidence"]}
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


# ---------------------------------------------------------------------------
# 技能本体（kb_skill_ontology_semiconductor_v1.json，source=kb_skill）
# ---------------------------------------------------------------------------

def _normalize_skill_key(text: Any) -> str:
    """技能词归一键：去全部空白、连字符/下划线归一、casefold（中英文同义词共用）。"""
    compact = re.sub(r"[\s\-_／/]+", "", str(text or ""))
    return compact.casefold()


def load_skill_ontology(kb_dir: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """读取 kb_skill_ontology_semiconductor_v1.json；缺失/坏 JSON 降级为空本体并留痕（不崩）。

    返回 (ontology, trace)。ontology = {
        "skills": {canonical_name: {"family": ..., "label": ..., "aliases": [...],
                                    "related": [...], "evidence": [...]}},
        "aliases": {归一键: canonical_name},   # 覆盖技能本名 + 全部别名
        "families": {family_id: label},
    }；空本体为 {"skills": {}, "aliases": {}, "families": {}}（布尔值为 False）。
    """
    empty: dict[str, Any] = {"skills": {}, "aliases": {}, "families": {}}
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    path = directory / SKILL_ONTOLOGY_FILE
    if not path.is_file():
        return empty, [f"技能本体 {SKILL_ONTOLOGY_FILE} 缺失（{directory}），降级为空本体"]
    doc, error = _read_json(path)
    if error:
        return empty, [f"技能本体{error}，降级为空本体"]
    families = doc.get("families") if isinstance(doc, dict) else None
    if not isinstance(families, list):
        return empty, [f"技能本体 {SKILL_ONTOLOGY_FILE} 结构异常（缺 families 数组），降级为空本体"]
    ontology: dict[str, Any] = {"skills": {}, "aliases": {}, "families": {}}
    for family in families:
        if not isinstance(family, dict):
            continue
        family_id = str(family.get("family_id") or "").strip()
        label = str(family.get("label") or "").strip()
        if family_id:
            ontology["families"][family_id] = label
        skills = family.get("skills")
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            name = str(skill.get("name") or "").strip()
            if not name or not _normalize_skill_key(name):
                continue
            aliases = [str(item).strip() for item in skill.get("aliases") or [] if str(item or "").strip()]
            related = [str(item).strip() for item in skill.get("related") or [] if str(item or "").strip()]
            evidence = [str(item).strip() for item in skill.get("evidence") or [] if str(item or "").strip()]
            ontology["skills"][name] = {
                "family": family_id,
                "label": label,
                "aliases": aliases,
                "related": related,
                "evidence": evidence,
            }
            for alias in [name, *aliases]:
                key = _normalize_skill_key(alias)
                if key:
                    ontology["aliases"].setdefault(key, name)
    return ontology, [f"已加载技能本体 {len(ontology['skills'])} 技能 / {len(ontology['families'])} 族（{SKILL_ONTOLOGY_FILE}）"]


def normalize_skill(
    term: Any,
    ontology: dict[str, Any] | None = None,
    *,
    kb_dir: str | Path | None = None,
) -> dict[str, Any]:
    """技能别名归一：命中返回 canonical/family，未命中原样留痕（source=none）。

    命中来源两级：顾问确认别名（kb_agent_confirmed_rules_v1.json，
    source=consultant_confirmed）优先，其次本体内置别名（source=kb_skill）。
    归一只做比对与提示：命中别名单独不构成技能证据（沿用本体 governance 口径）。
    """
    raw = str(term or "").strip()
    key = _normalize_skill_key(raw)
    result = {"raw": raw, "normalized": key, "canonical": "", "family": "", "matched": False, "source": "none"}
    if not key:
        return result
    if ontology is None:
        ontology, _trace = load_skill_ontology(kb_dir)
    if not isinstance(ontology, dict):
        ontology = {"skills": {}, "aliases": {}, "families": {}}
    # 顾问确认的技能别名（kb_agent_confirmed_rules_v1.json）优先于本体内置别名；
    # 确认规则为空/文件缺失/坏 JSON 时降级为空映射，行为与现状一致。
    confirmed_aliases, _confirmed_trace = load_confirmed_skill_aliases(kb_dir)
    canonical = confirmed_aliases.get(key, "")
    source = "consultant_confirmed"
    if not canonical:
        canonical = (ontology.get("aliases") or {}).get(key, "")
        source = "kb_skill"
    if not canonical:
        return result
    skill = (ontology.get("skills") or {}).get(canonical) or {}
    result.update(
        {
            "canonical": canonical,
            "family": str(skill.get("family") or ""),
            "matched": True,
            "source": source,
        }
    )
    return result


def related_skills(
    term: Any,
    ontology: dict[str, Any] | None = None,
    *,
    kb_dir: str | Path | None = None,
    limit: int = 8,
) -> list[str]:
    """相关技能提示：term 命中本体（本名或别名）时返回其 related 列表；未命中返回空。"""
    info = normalize_skill(term, ontology, kb_dir=kb_dir)
    if not info["matched"]:
        return []
    if ontology is None:
        ontology, _trace = load_skill_ontology(kb_dir)
    skill = (ontology.get("skills") or {}).get(info["canonical"]) or {}
    related = [str(item) for item in skill.get("related") or [] if str(item or "").strip()]
    return related[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# 职级映射（kb_level_mapping_v1.json，source=kb_level）
# ---------------------------------------------------------------------------

def load_level_mapping(kb_dir: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """读取 kb_level_mapping_v1.json；缺失/坏 JSON 降级为空映射并留痕（不崩）。

    返回 (mapping, trace)。mapping = {"bands": [...], "systems": [...]}；
    空映射为 {"bands": [], "systems": []}（bands 为空即视为无映射）。
    """
    empty: dict[str, Any] = {"bands": [], "systems": []}
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    path = directory / LEVEL_MAPPING_FILE
    if not path.is_file():
        return empty, [f"职级映射库 {LEVEL_MAPPING_FILE} 缺失（{directory}），降级为空映射"]
    doc, error = _read_json(path)
    if error:
        return empty, [f"职级映射库{error}，降级为空映射"]
    if not isinstance(doc, dict):
        return empty, [f"职级映射库 {LEVEL_MAPPING_FILE} 结构异常（非对象），降级为空映射"]
    bands: list[dict[str, Any]] = []
    for band in doc.get("level_bands") or []:
        if not isinstance(band, dict) or not str(band.get("band") or "").strip():
            continue
        bands.append(
            {
                "band": str(band.get("band") or "").strip(),
                "label": str(band.get("label") or "").strip(),
                "aliases": [str(item).strip() for item in band.get("aliases") or [] if str(item or "").strip()],
                "accepted": [str(item).strip() for item in band.get("accepted") or [] if str(item or "").strip()],
                "years_hint": str(band.get("years_hint") or "").strip(),
                "basis": str(band.get("basis") or "").strip(),
            }
        )
    systems = [item for item in doc.get("systems") or [] if isinstance(item, dict)]
    mapping = {"bands": bands, "systems": systems}
    return mapping, [f"已加载职级映射 {len(bands)} 职级带 / {len(systems)} 体系（{LEVEL_MAPPING_FILE}）"]


def map_level(
    title: Any,
    mapping: dict[str, Any] | None = None,
    *,
    kb_dir: str | Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """按 title 命中职级带：别名最长包含匹配（"高级工程师"优先于"工程师"）。

    命中返回 (hit, trace)：hit = {band, label, matched_alias, accepted_levels, years_hint,
    basis, source}；source=consultant_confirmed 表示命中顾问确认规则（kb_agent_confirmed_
    rules_v1.json，优先于内置库），source=kb_level 表示命中内置映射库。
    未命中/空映射返回 (None, trace)，调用方走 LLM/原型路径。
    """
    text = _normalize_skill_key(title)
    if mapping is None:
        mapping, load_trace = load_level_mapping(kb_dir)
    else:
        load_trace = []
    # 顾问确认的职级映射优先于内置映射库；文件缺失/坏 JSON/无命中时继续走内置库。
    if text:
        confirmed, _confirmed_trace = load_confirmed_level_mappings(kb_dir)
        best_confirmed: tuple[int, dict[str, Any], str] | None = None
        for entry in confirmed:
            for alias in entry.get("aliases") or []:
                key = _normalize_skill_key(alias)
                if key and key in text and (best_confirmed is None or len(key) > best_confirmed[0]):
                    best_confirmed = (len(key), entry, alias)
        if best_confirmed is not None:
            _length, entry, alias = best_confirmed
            hit = {
                "band": entry["band"],
                "label": str(entry.get("label") or ""),
                "matched_alias": alias,
                "accepted_levels": list(entry.get("accepted") or []),
                "years_hint": str(entry.get("years_hint") or ""),
                "basis": str(entry.get("basis") or ""),
                "source": "consultant_confirmed",
                "proposal_id": str(entry.get("proposal_id") or ""),
            }
            trace = [
                *load_trace,
                f"职级映射命中（source=consultant_confirmed）：title“{str(title or '').strip()}”按确认规则别名"
                f"“{alias}”落入职级带 {entry['band']}（{hit['label'] or '未标注'}，提案 {hit['proposal_id'] or '未知'}）",
            ]
            return hit, trace
    bands = mapping.get("bands") if isinstance(mapping, dict) else []
    if not bands:
        return None, [*load_trace, f"职级映射为空，title“{str(title or '').strip()}”走 LLM/原型路径"]
    best: tuple[int, dict[str, Any], str] | None = None
    for band in bands:
        for alias in band.get("aliases") or []:
            key = _normalize_skill_key(alias)
            if key and key in text and (best is None or len(key) > best[0]):
                best = (len(key), band, alias)
    if best is None:
        return None, [*load_trace, f"职级映射未命中 title“{str(title or '').strip()}”，走 LLM/原型路径"]
    _length, band, alias = best
    hit = {
        "band": band["band"],
        "label": band["label"],
        "matched_alias": alias,
        "accepted_levels": list(band.get("accepted") or []),
        "years_hint": band.get("years_hint") or "",
        "basis": band.get("basis") or "",
        "source": "kb_level",
    }
    trace = [
        *load_trace,
        f"职级映射命中（source=kb_level）：title“{str(title or '').strip()}”按别名“{alias}”落入"
        f"职级带 {band['band']}（{band['label']}），对标依据：{band.get('basis') or '未标注'}",
    ]
    return hit, trace


# ---------------------------------------------------------------------------
# 3.2b 公司校准覆盖层（company_calibrations 表，二期知识飞轮）
# ---------------------------------------------------------------------------
#
# 图谱 JSON 保持"原始名单"不改；顾问逐公司校准（行业/产品线/技能标签/职级体系/
# 禁挖竞业标记/备注）持久化在 asa_core 的 company_calibrations 表。消费侧经本节的
# 合并钩子叠加：DB 有 status='calibrated' 记录时优先用校准值并标注
# source=consultant_calibrated；DB 不可用/无记录时完全保持现状（空覆盖层 + 留痕）。
# 策略/评估消费路径不默认调用本节函数，行为不变；调用方显式传入 db_path 才启用。

CALIBRATION_OVERLAY_TABLE = "company_calibrations"


def load_calibration_overlay(db_path: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """读取公司校准覆盖层；返回 (overlay, trace)。overlay 以 company_key（规范化公司名）为键。

    db_path 为空时回退 A_SYSTEM_DB 环境变量；DB 缺失/无表/读取失败一律降级为
    空覆盖层并留痕（不抛异常）。只有 status='calibrated' 的记录进入覆盖层；
    rejected/needs_review 不覆盖原始图谱。
    """
    empty: dict[str, dict[str, Any]] = {}
    raw = str(db_path or "").strip() or os.environ.get("A_SYSTEM_DB", "").strip()
    if not raw:
        return empty, ["未指定校准库（db_path/A_SYSTEM_DB 均为空），校准覆盖层为空，按原始图谱处理"]
    path = Path(raw).expanduser()
    if not path.is_file():
        return empty, [f"校准库缺失（{path}），校准覆盖层为空，按原始图谱处理"]
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (CALIBRATION_OVERLAY_TABLE,),
            ).fetchone()
            if not has_table:
                return empty, [f"校准库无 {CALIBRATION_OVERLAY_TABLE} 表，校准覆盖层为空，按原始图谱处理"]
            rows = conn.execute(
                """SELECT company_key,company_name,track,product_lines_json,skill_tags_json,
                          level_system,no_poach,non_compete,note,calibrated_by,calibrated_at,version
                     FROM company_calibrations WHERE status='calibrated'"""
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return empty, [f"校准库读取失败（{exc.__class__.__name__}），校准覆盖层为空，按原始图谱处理"]

    overlay: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["company_key"] or "").strip()
        if not key:
            continue
        try:
            product_lines = json.loads(str(row["product_lines_json"] or "[]"))
        except ValueError:
            product_lines = []
        try:
            skill_tags = json.loads(str(row["skill_tags_json"] or "[]"))
        except ValueError:
            skill_tags = []
        overlay[key] = {
            "company_name": str(row["company_name"] or ""),
            "track": str(row["track"] or ""),
            "product_lines": [str(item) for item in product_lines if str(item or "").strip()] if isinstance(product_lines, list) else [],
            "skill_tags": [str(item) for item in skill_tags if str(item or "").strip()] if isinstance(skill_tags, list) else [],
            "level_system": str(row["level_system"] or ""),
            "no_poach": bool(row["no_poach"]),
            "non_compete": bool(row["non_compete"]),
            "note": str(row["note"] or ""),
            "calibrated_by": str(row["calibrated_by"] or ""),
            "calibrated_at": str(row["calibrated_at"] or ""),
            "version": int(row["version"] or 1),
        }
    trace = [f"已加载公司校准覆盖层 {len(overlay)} 家（{CALIBRATION_OVERLAY_TABLE}，仅 status=calibrated）"] if overlay else [
        "校准库无 calibrated 记录，校准覆盖层为空，按原始图谱处理"
    ]
    return overlay, trace


def apply_calibration_overlay(
    graph: dict[str, dict[str, Any]],
    overlay: dict[str, dict[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """把校准覆盖层合并进公司图谱（返回新 dict，不改入参）。

    合并口径（命中 status='calibrated' 的图谱条目）：
    - track（行业）：校准值非空 → 覆盖原始 track；
    - categories：校准 skill_tags 非空 → 优先用校准技能标签替换四分类标签；
    - product_lines / level_system / no_poach / non_compete / note：作为校准附加键并入；
    - 条目标注 source=consultant_calibrated + calibration{level_system,no_poach,
      non_compete,note,calibrated_by,calibrated_at,version} 留痕。
    覆盖层为空/无命中 → 返回原图谱（内容不变）+ 留痕；校准记录不在图谱中 → 留痕不新建条目。
    """
    if not overlay:
        return dict(graph or {}), ["校准覆盖层为空，公司图谱按原始名单处理"]
    merged: dict[str, dict[str, Any]] = {}
    applied: list[str] = []
    for name, info in (graph or {}).items():
        record = overlay.get(normalize_client_name(name))
        if record is None:
            merged[name] = dict(info)
            continue
        entry = dict(info)
        if record.get("track"):
            entry["track"] = str(record["track"])
        if record.get("skill_tags"):
            entry["categories"] = list(record["skill_tags"])
        if record.get("product_lines"):
            entry["product_lines"] = list(record["product_lines"])
        entry["source"] = "consultant_calibrated"
        entry["calibration"] = {
            "level_system": str(record.get("level_system") or ""),
            "no_poach": bool(record.get("no_poach")),
            "non_compete": bool(record.get("non_compete")),
            "note": str(record.get("note") or ""),
            "calibrated_by": str(record.get("calibrated_by") or ""),
            "calibrated_at": str(record.get("calibrated_at") or ""),
            "version": int(record.get("version") or 1),
        }
        merged[name] = entry
        applied.append(name)
    graph_keys = {normalize_client_name(name) for name in (graph or {})}
    orphan = sorted(
        str(record.get("company_name") or key)
        for key, record in overlay.items()
        if key not in graph_keys
    )
    trace: list[str] = []
    if applied:
        trace.append(
            f"公司图谱应用顾问校准覆盖 {len(applied)} 家（source=consultant_calibrated）："
            f"{'、'.join(applied[:5])}{'等' if len(applied) > 5 else ''}"
        )
    else:
        trace.append("校准覆盖层与图谱无交集，公司图谱按原始名单处理")
    for name in orphan:
        trace.append(f"校准记录「{name}」不在公司图谱中，未应用（校准是覆盖层，不新建图谱条目）")
    return merged, trace


# ---------------------------------------------------------------------------
# 顾问确认规则（kb_agent_confirmed_rules_v1.json，二期知识飞轮消费侧）
# ---------------------------------------------------------------------------
#
# 写入方是 asa_core.knowledge_proposals：知识增补提案 accept 后把 negative_rule /
# skill_alias / level_mapping 三类提案追加进该文件（meta + rules 数组，条目带
# proposed_by=consultant_confirmed / proposal_id / evidence 快照）。本节是消费侧：
# 文件缺失/坏 JSON/结构异常一律降级为空规则集并留痕（不抛异常、不写文件）；
# 有规则则增强（确认优先于内置库），无规则完全保持现状。

CONFIRMED_RULES_FILE = "kb_agent_confirmed_rules_v1.json"
CONFIRMED_RULE_TYPES = ("negative_rule", "skill_alias", "level_mapping")

# 条目显式标注了 status 时只认这些有效值；未标注视为有效（写入方只写 accepted 提案）。
_CONFIRMED_VALID_STATUSES = ("", "accepted", "confirmed", "active")


def load_confirmed_rules(
    kb_dir: str | Path | None = None,
    *,
    rule_type: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """读取 kb_agent_confirmed_rules_v1.json；返回 (entries, trace)。

    只保留 rule_type ∈ CONFIRMED_RULE_TYPES、content 为 dict、status 有效（见
    _CONFIRMED_VALID_STATUSES）的条目；rule_type 参数可按类型过滤。
    文件缺失/坏 JSON/结构异常 → 空列表 + 留痕（不炸）。
    """
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    path = directory / CONFIRMED_RULES_FILE
    if not path.is_file():
        return [], [f"顾问确认规则 {CONFIRMED_RULES_FILE} 缺失（{directory}），按无确认规则处理"]
    doc, error = _read_json(path)
    if error:
        return [], [f"顾问确认规则{error}，按无确认规则处理"]
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, list):
        return [], [f"顾问确认规则 {CONFIRMED_RULES_FILE} 结构异常（缺 rules 数组），按无确认规则处理"]
    wanted = str(rule_type or "").strip()
    entries: list[dict[str, Any]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        entry_type = str(item.get("rule_type") or "").strip()
        if entry_type not in CONFIRMED_RULE_TYPES:
            continue
        if str(item.get("status") or "").strip().lower() not in _CONFIRMED_VALID_STATUSES:
            continue
        if not isinstance(item.get("content"), dict):
            continue
        if wanted and entry_type != wanted:
            continue
        entries.append(item)
    scope = f"（类型 {wanted}）" if wanted else ""
    trace = [f"已加载顾问确认规则 {len(entries)} 条{scope}（{CONFIRMED_RULES_FILE}）"] if entries else [
        f"顾问确认规则无可用条目{scope}（{CONFIRMED_RULES_FILE}），按无确认规则处理"
    ]
    return entries, trace


def _content_aliases(content: dict[str, Any]) -> list[str]:
    """从确认规则 content 提取别名列表（兼容 alias/aliases/title 三种键）。"""
    aliases = content.get("aliases")
    if isinstance(aliases, list):
        result = [str(item).strip() for item in aliases if str(item or "").strip()]
        if result:
            return result
    single = str(content.get("alias") or content.get("title") or "").strip()
    return [single] if single else []


def load_confirmed_skill_aliases(kb_dir: str | Path | None = None) -> tuple[dict[str, str], list[str]]:
    """skill_alias 类确认规则 → {归一别名键: canonical} 映射。

    content 口径：{"alias"|"aliases": ..., "canonical"|"skill"|"name": ...}；
    缺 canonical 或别名的条目跳过。文件缺失/坏 JSON → 空映射（不炸）。
    """
    entries, trace = load_confirmed_rules(kb_dir, rule_type="skill_alias")
    aliases: dict[str, str] = {}
    for entry in entries:
        content = entry["content"]
        canonical = str(
            content.get("canonical") or content.get("skill") or content.get("name") or ""
        ).strip()
        if not canonical or not _normalize_skill_key(canonical):
            continue
        for alias in [canonical, *_content_aliases(content)]:
            key = _normalize_skill_key(alias)
            if key:
                aliases[key] = canonical
    return aliases, trace


def load_confirmed_level_mappings(kb_dir: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """level_mapping 类确认规则 → [{band, label, aliases, accepted, years_hint, basis, proposal_id}]。

    content 口径：{"alias"|"aliases"|"title": ..., "band"|"level": ...}，
    可选 label/accepted(或 accepted_levels)/years_hint/basis；缺 band 或别名的条目跳过。
    文件缺失/坏 JSON → 空列表（不炸）。
    """
    entries, trace = load_confirmed_rules(kb_dir, rule_type="level_mapping")
    mappings: list[dict[str, Any]] = []
    for entry in entries:
        content = entry["content"]
        band = str(content.get("band") or content.get("level") or "").strip()
        aliases = _content_aliases(content)
        if not band or not aliases:
            continue
        accepted = content.get("accepted") if isinstance(content.get("accepted"), list) else content.get("accepted_levels")
        mappings.append(
            {
                "band": band,
                "label": str(content.get("label") or "").strip(),
                "aliases": aliases,
                "accepted": [str(item).strip() for item in accepted if str(item or "").strip()] if isinstance(accepted, list) else [],
                "years_hint": str(content.get("years_hint") or "").strip(),
                "basis": str(content.get("basis") or "").strip(),
                "proposal_id": str(entry.get("proposal_id") or "").strip(),
            }
        )
    return mappings, trace
