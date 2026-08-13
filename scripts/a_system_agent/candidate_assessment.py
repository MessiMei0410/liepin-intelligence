"""S6-1/S6-2/S6-3：判人评估器 —— candidate_assessment artifact（轨迹/跳槽史/分位/动机/风险点）。

口径来源：docs/TASKCARD_S6-1_判人评估器_轨迹与跳槽史_20260724.md + PRD §2/§3/§5（S6-2/S6-3 行）。

架构（LLM 角色 vs 确定性校验）：
- LLM 只做"资深顾问式"判断（verdict/segments/moves/summary），输入为简历原文 + strategy_v2
  + 确定性预匹配的图谱命中（graph_hits）；S6-2 两维 LLM 只把确定性算好的 band/信号读成顾问口径。
- S6-2 水平分位：参照池抽取/年限过滤/分位 rank/band 全部由 assessment_signals 确定性计算并
  强制写入 artifact，模型永不可能改 band；N < 8（默认阈值）→ confidence=inferred 注明样本不足。
- S6-2 动机时机：信号确定性产出——a) 简历工况（在职时长 vs 历史平均任期、近一年简历更新）；
  b) 公司近况公开信号（只读采集，每条带来源 URL 与 as_of，失败记 stats）；c) 无信号如实
  "未见明显变动信号" + inferred。不推断个人隐私。
- S6-3 风险点（呈现口径"需要核实的问题"）：gap 空窗/频繁跳动/时间线冲突/硬条件差距由
  assessment_signals 确定性检出（阈值固定）；title 通胀/过度包装两类语义项由 LLM 第三调
  assess_risks 补充，证据过同一道逐字闸，证据归零的 LLM 项整条丢弃；第三调失败降级为
  纯确定性检出（记 signal_stats），不阻断。无风险项时 items=[] + "未见需核实的问题"。
- 写入前必过确定性校验层（不过闸不落库）：
  1) 证据强约束：type=简历 的 ref 必须逐字存在于该候选人语料（原文包含校验，失败剥离）；
     type=图谱 的 ref 必须解析到本评估实际命中的图谱条目（公司名规范化匹配，失败剥离）；
     type=知识库 的 ref 必须等于本评估实际生成的参照系摘要串（白名单，防编造）；
     type=公开信息 的 ref 必须等于本评估实际采集到的来源 URL（白名单，防编造）。
     某维 evidence 归零 → confidence 强制 inferred。
  2) 敏感属性负向扫描：verdict/summary/segments/moves/risk 等 LLM 生成文本命中年龄/性别/婚育/户籍
     词表 → 整条拒写（ValueError）并记扫描日志（candidate_events）；简历逐字引用命中 → 剥离该条；
     公开信号摘要命中 → 丢弃该条信号（公开页内容不可控，宁可不采）。
  3) 决策字眼拦截："建议淘汰/不建议推荐"类 → 拒写。
  4) 图谱未命中的公司 tier_source 一律强制 inferred（不瞎编）。

红线：评估只辅助不决策；风险以"需要核实的问题"呈现（不做风险定罪）；评估数据不出本机；
同人同岗幂等（重复生成更新原行，as_of 刷新）。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Callable

from . import assessment_calibration, assessment_signals, company_kb, knowledge_base
from .llm import BaseLLM, LLMError

ARTIFACT_TYPE = "candidate_assessment"
SCHEMA_VERSION = "assessment_v1"
ASSESSOR_VERSION = "s6-3-v1"

DIMENSIONS_IMPLEMENTED = ("trajectory", "move_history", "percentile", "motivation", "risks")

# 顾问动作（采纳/改判/否决）：经 PATCH advisor-action 写回 artifact，version 不 bump。
ADVISOR_ACTIONS = ("pending", "accepted", "modified", "rejected")
ADVISOR_ACTION_LABELS = {"pending": "待处理", "accepted": "已采纳", "modified": "已改判", "rejected": "已否决"}

_CONFIDENCE = {"certain", "inferred"}
_TIER = {"T1", "T2", "T3", "unknown"}
_TIER_SOURCE = {"graph", "inferred"}
_PACE = {"fast", "normal", "slow", "unknown"}
_EVOLUTION = {"rising", "lateral", "stagnant", "unknown"}
_DIRECTION = {"up", "lateral", "down"}
_MOVE_UNKNOWN = {"up", "lateral", "down", "unknown"}
_BANDS = set(assessment_signals.BANDS)
_PERCENTILE_BASIS = {"fit_score", "trajectory_features"}
_EVIDENCE_TYPES = {"简历", "图谱", "知识库", "公开信息", "雷达信号"}
_SEVERITY = {"high", "medium", "low"}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# S6-3 风险点五类：前三类半 + 硬条件差距确定性检出；title 通胀/过度包装由 LLM 语义补充。
_RISK_KINDS = {"gap", "frequent_hop", "title_inflation", "over_packaging", "hard_requirement"}
_RISK_LLM_KINDS = {"title_inflation", "over_packaging"}
RISK_ITEM_MAX = 8  # risks.items 上限（确定性每类 ≤3 + LLM ≤4，双保险）

# 决策类禁语（评估只辅助不决策）：出现在生成文本里 → 拒写。
_BANNED_DECISION_PATTERNS = [
    re.compile(r"建议淘汰|不建议推荐|予以淘汰|直接淘汰|不推荐此人|不推进此人|建议不推进|建议放弃"),
]

# 敏感属性词表：年龄/性别/婚育/户籍。作为判断因子出现即违规（正负向都不许）。
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("年龄", re.compile(r"\d{2}\s*岁|年龄|年纪|年富力强|岁数|偏大|偏年轻|太年轻|老了")),
    ("性别", re.compile(r"男性|女性|男士|女士|性别")),
    ("婚育", re.compile(r"已婚|未婚|已育|未育|婚育|备孕|怀孕|产假|哺乳|孩子|二胎|三胎|家庭稳定|家庭负担")),
    ("户籍", re.compile(r"户籍|户口|本地人|外地人|籍贯")),
]

_CORPUS_FIELDS = ("full_text", "work_text", "project_text", "education_text", "profile_text")

# UX-1 业务语言映射（导出/展示口径；schema 内部值保持英文枚举）。
LABELS = {
    "trajectory": "职业轨迹",
    "move_history": "跳槽质量史",
    "percentile": "在同龄人里的位置",
    "motivation": "动机与时机",
    "risks": "需要核实的问题",
    "inferred": "推测",
    "certain": "确定",
    "up": "上升",
    "lateral": "平移",
    "down": "下行",
    "unknown": "无法判断",
    "fast": "偏快",
    "normal": "正常",
    "slow": "偏慢",
    "rising": "上升",
    "stagnant": "吃老本",
    "T1": "头部",
    "T2": "腰部",
    "T3": "长尾",
    "top10": "前 10%",
    "top25": "前 25%",
    "median": "中位区间",
    "below": "相对靠后",
    "fit_score": "既有评估得分",
    "trajectory_features": "轨迹特征分",
    "high": "高",
    "medium": "中",
    "low": "低",
    "gap": "空窗",
    "frequent_hop": "频繁跳动",
    "title_inflation": "title 通胀",
    "over_packaging": "过度包装信号",
    "hard_requirement": "硬条件差距",
}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


# ---------------------------------------------------------------------------
# 敏感属性负向扫描 + 决策禁语（硬闸）
# ---------------------------------------------------------------------------

def scan_sensitive(texts: list[str]) -> list[dict[str, str]]:
    """扫描生成文本中的敏感属性因子（年龄/性别/婚育/户籍）。

    返回命中列表 [{category, hit}]；空列表 = 通过。任何命中 → 调用方拒写并记扫描日志。
    """
    hits: list[dict[str, str]] = []
    for text in texts:
        body = str(text or "")
        if not body:
            continue
        for category, pattern in _SENSITIVE_PATTERNS:
            match = pattern.search(body)
            if match:
                hits.append({"category": category, "hit": match.group(0), "text": body[:80]})
    return hits


def scan_banned_decision(texts: list[str]) -> list[str]:
    """扫描"建议淘汰/不建议推荐"类决策字眼；命中列表非空 → 拒写。"""
    hits: list[str] = []
    for text in texts:
        body = str(text or "")
        for pattern in _BANNED_DECISION_PATTERNS:
            match = pattern.search(body)
            if match:
                hits.append(match.group(0))
    return hits


def generated_texts(doc: dict[str, Any]) -> list[str]:
    """收集 artifact 中全部 LLM 生成文本（verdict/summary/segments/moves/risk 描述字段）。

    注：motivation.signals 的 summary 是确定性数据（工况计算/公开页原文摘录），不是 LLM 文本，
    不进本扫描；公开信号摘要在采集侧单独过敏感词丢弃（见 run_assessment）。
    risks.items 的 risk 文本（含确定性模板与 LLM 语义项）全部进扫描——确定性模板本就不会命中，
    LLM 项命中即拒写，口径与其他维一致。
    """
    texts: list[str] = []
    dimensions = doc.get("dimensions") if isinstance(doc.get("dimensions"), dict) else {}
    for name in DIMENSIONS_IMPLEMENTED:
        dim = dimensions.get(name) if isinstance(dimensions.get(name), dict) else {}
        texts.append(str(dim.get("verdict") or ""))
        for segment in dim.get("segments") or []:
            if isinstance(segment, dict):
                texts.extend(str(segment.get(key) or "") for key in ("team", "report_line", "note"))
        for move in dim.get("moves") or []:
            if isinstance(move, dict):
                texts.append(str(move.get("reason") or ""))
        for risk_item in dim.get("items") or []:
            if isinstance(risk_item, dict):
                texts.append(str(risk_item.get("risk") or ""))
    texts.append(str(doc.get("consultant_summary") or ""))
    return [text for text in texts if text]


# ---------------------------------------------------------------------------
# 证据校验层（硬）：简历逐字 / 图谱真实条目
# ---------------------------------------------------------------------------

def build_corpus(resume: dict[str, Any]) -> str:
    """候选人语料：简历各段原文拼接，供逐字包含校验。"""
    parts = [str(resume.get(field) or "") for field in _CORPUS_FIELDS]
    return "\n".join(part for part in parts if part)


def _verbatim_hit(ref: str, corpus: str) -> bool:
    """逐字包含校验：ref 必须是语料的连续子串（仅容忍首尾空白差异）。"""
    needle = str(ref or "").strip()
    if len(needle) < 4:  # 太短无法作为可核验证据，按不合格剥离
        return False
    return needle in corpus


def _graph_hit_name(ref: str, graph_names: list[str]) -> str:
    """图谱 ref 解析：ref 规范化后与某命中条目命中（精确/别名）→ 返回条目名，否则 ''。"""
    target_raw = " ".join(str(ref or "").split())
    target_norm = knowledge_base.normalize_client_name(ref)
    if not target_norm:
        return ""
    for name in graph_names:
        rule, _reason = knowledge_base.name_match_rule(target_raw, target_norm, name)
        if rule:
            return name
    return ""


def verify_evidence(
    evidence: Any,
    *,
    corpus: str,
    graph_names: list[str],
    kb_refs: list[str] | None = None,
    url_refs: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """校验一组 evidence；返回 (保留, 剥离)，剥离含原因，全部留痕。

    知识库 ref 必须逐字等于本评估实际生成的参照系摘要串；公开信息 ref 必须逐字等于
    本评估实际采集到的来源 URL——两者都是白名单精确匹配，模型编造的引用过不了闸。
    """
    kept: list[dict[str, str]] = []
    stripped: list[dict[str, str]] = []
    items = evidence if isinstance(evidence, list) else []
    kb_whitelist = {str(ref) for ref in (kb_refs or []) if str(ref or "").strip()}
    url_whitelist = {str(ref) for ref in (url_refs or []) if str(ref or "").strip()}
    for item in items:
        if not isinstance(item, dict):
            stripped.append({"type": "", "ref": str(item)[:80], "reason": "evidence 结构非法"})
            continue
        etype = str(item.get("type") or "").strip()
        ref = str(item.get("ref") or "").strip()
        if etype == "简历":
            if _verbatim_hit(ref, corpus):
                kept.append({"type": etype, "ref": ref})
            else:
                stripped.append({"type": etype, "ref": ref[:120], "reason": "简历引用非候选人语料逐字片段"})
        elif etype == "图谱":
            matched = _graph_hit_name(ref, graph_names)
            if matched:
                kept.append({"type": etype, "ref": matched})
            else:
                stripped.append({"type": etype, "ref": ref[:120], "reason": "图谱引用未解析到本评估命中的真实条目"})
        elif etype == "知识库":
            if ref in kb_whitelist:
                kept.append({"type": etype, "ref": ref})
            else:
                stripped.append({"type": etype, "ref": ref[:120], "reason": "知识库引用非本评估实际生成的参照系摘要"})
        elif etype == "公开信息":
            if ref in url_whitelist:
                kept.append({"type": etype, "ref": ref})
            else:
                stripped.append({"type": etype, "ref": ref[:120], "reason": "公开信息引用非本评估实际采集的来源 URL"})
        else:
            stripped.append({"type": etype or "未标注", "ref": ref[:120], "reason": "证据类型仅支持 简历/图谱/知识库/公开信息"})
    return kept, stripped


# ---------------------------------------------------------------------------
# 图谱命中（确定性）：候选人雇主公司 → 图谱条目
# ---------------------------------------------------------------------------

def _conn_db_path(conn: sqlite3.Connection) -> str:
    """从连接解析主库文件路径（供校准覆盖层读库）；内存库/读取失败返回空串。

    空串走 load_calibration_overlay 的缺省口径（回退 A_SYSTEM_DB 环境变量）。
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return ""
    return str(row[2] or "") if row else ""


def match_graph_hits(companies: list[str], graph: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把简历里的雇主公司名规范化后命中图谱 key；命中即真实条目（含名称/赛道/主营业务/分类）。

    公司知识库（CKB）补充：图谱未命中的雇主公司，若 CKB 有画像记录，则追加
    source=company_kb 的补充命中（track=行业、business=主营业务、profile_summary=
    中文画像摘要），随 graph_hits 一并注入评估上下文；CKB 库缺失/无表/不可用时
    静默跳过，输出与现状完全一致。证据校验逻辑不变（CKB 命中同为真实知识库条目）。
    """
    hits: list[dict[str, Any]] = []
    used: set[str] = set()
    unmatched: list[str] = []
    for company in companies:
        raw = " ".join(str(company or "").split())
        norm = knowledge_base.normalize_client_name(company)
        if not norm:
            continue
        matched = False
        for name, info in (graph or {}).items():
            if name in used:
                continue
            rule, _reason = knowledge_base.name_match_rule(raw, norm, name)
            if rule:
                hit: dict[str, Any] = {
                    "company": raw,
                    "graph_name": name,
                    "track": str(info.get("track") or ""),
                    "business": str(info.get("business") or ""),
                    "categories": list(info.get("categories") or []),
                }
                # 校准覆盖层合并后的条目带 source=consultant_calibrated；原始图谱条目
                # 无 source 键（不补默认值），无覆盖层时输出与现状逐字节一致。
                if info.get("source"):
                    hit["source"] = str(info["source"])
                hits.append(hit)
                used.add(name)
                matched = True
                break
        if not matched:
            unmatched.append(raw)
    # CKB 补充：图谱未命中的雇主公司查公司知识库画像（只读；库不可用静默跳过）。
    # 规范化名（去掉公司后缀）更短，LIKE 命中率更高，raw 查不到再用 norm 兜底。
    if unmatched:
        seen_ckb: set[str] = set()
        for raw in unmatched:
            norm = knowledge_base.normalize_client_name(raw)
            profile = company_kb.get_profile(raw) or (
                company_kb.get_profile(norm) if norm and norm != raw else None
            )
            if not profile:
                continue
            kb_name = str(profile.get("name") or profile.get("company_key") or raw)
            if kb_name in seen_ckb:
                continue
            seen_ckb.add(kb_name)
            hits.append(
                {
                    "company": raw,
                    "graph_name": kb_name,
                    "track": str(profile.get("industry") or ""),
                    "business": str(profile.get("business_desc") or ""),
                    "categories": [],
                    "source": "company_kb",
                    "profile_summary": company_kb.profile_summary(profile),
                }
            )
    return hits


def extract_employers(text: str) -> list[str]:
    """从简历文本粗提雇主公司名（含 公司/集团/中心 后缀或 · 分隔的行首词），供图谱预匹配。

    宁多勿漏：多提的名字图谱匹配不上只是少一次命中，不会编造。
    """
    companies: list[str] = []
    for match in re.finditer(r"[一-龥A-Za-z0-9（）()]{2,30}(?:公司|集团|研究院|研究所|事务所|工厂|银行|医院)", text):
        name = match.group(0)
        if name not in companies:
            companies.append(name)
    for match in re.finditer(r"(?:^|[\s，。；;|｜、])([一-龥A-Za-z0-9（）()]{2,20})\s*[·•]\s*", text):
        name = match.group(1)
        if name not in companies and not name.endswith(("岁", "年", "月")):
            companies.append(name)
    return companies[:30]


# ---------------------------------------------------------------------------
# LLM 结果清洗 + 确定性校验（写入前过闸）
# ---------------------------------------------------------------------------

def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _tier(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text if text in _TIER else "unknown"


def _normalize_segments(raw: Any, graph_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    graph_names = {hit["graph_name"] for hit in graph_hits}
    resume_companies = {hit["company"] for hit in graph_hits}
    segments: list[dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else [])[:12]:
        if not isinstance(item, dict):
            continue
        company = _clean_text(item.get("company"), 60)
        tier_source = _enum(item.get("tier_source"), _TIER_SOURCE, "inferred")
        # 确定性闸：公司未命中图谱 → tier_source 强制 inferred，不许伪装 graph 结论。
        if tier_source == "graph" and not any(
            company and (company in resume_companies or knowledge_base.name_match_rule(company, knowledge_base.normalize_client_name(company), name)[0])
            for name in graph_names
        ):
            tier_source = "inferred"
        segments.append(
            {
                "company": company,
                "title": _clean_text(item.get("title"), 60),
                "period": _clean_text(item.get("period"), 40),
                "tier": _tier(item.get("tier")),
                "tier_source": tier_source,
                "team": _clean_text(item.get("team"), 80),
                "report_line": _clean_text(item.get("report_line"), 80),
                "note": _clean_text(item.get("note"), 200),
            }
        )
    return segments


def _normalize_moves(raw: Any) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else [])[:12]:
        if not isinstance(item, dict):
            continue
        moves.append(
            {
                "from": _clean_text(item.get("from"), 60),
                "to": _clean_text(item.get("to"), 60),
                "direction": _enum(item.get("direction"), _DIRECTION, "lateral"),
                "platform": _enum(item.get("platform"), _DIRECTION, "lateral"),
                "title_direction": _enum(item.get("title_direction"), _DIRECTION, "lateral"),
                "responsibility_direction": _enum(item.get("responsibility_direction"), _DIRECTION, "lateral"),
                "reason": _clean_text(item.get("reason"), 200),
            }
        )
    return moves


def normalize_llm_result(
    raw: dict[str, Any],
    *,
    corpus: str,
    graph_hits: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """LLM 输出 → assessment dimensions + summary；确定性证据校验 + 降级留痕。

    返回 (dimensions, consultant_summary, evidence_stats)。
    证据归零的维 confidence 强制 inferred；被剥离的证据全部记入 stats。
    """
    graph_names = [hit["graph_name"] for hit in graph_hits]
    stats: dict[str, Any] = {"kept": 0, "stripped": 0, "stripped_detail": []}
    dimensions: dict[str, Any] = {}

    raw_trajectory = raw.get("trajectory") if isinstance(raw.get("trajectory"), dict) else {}
    kept, dropped = verify_evidence(raw_trajectory.get("evidence"), corpus=corpus, graph_names=graph_names)
    stats["kept"] += len(kept)
    stats["stripped"] += len(dropped)
    stats["stripped_detail"].extend(dropped)
    confidence = _enum(raw_trajectory.get("confidence"), _CONFIDENCE, "inferred")
    if not kept:
        confidence = "inferred"
    dimensions["trajectory"] = {
        "verdict": _clean_text(raw_trajectory.get("verdict"), 300),
        "evidence": kept,
        "confidence": confidence,
        "segments": _normalize_segments(raw_trajectory.get("segments"), graph_hits),
        "promotion_pace": _enum(raw_trajectory.get("promotion_pace"), _PACE, "unknown"),
        "tech_evolution": _enum(raw_trajectory.get("tech_evolution"), _EVOLUTION, "unknown"),
    }

    raw_moves = raw.get("move_history") if isinstance(raw.get("move_history"), dict) else {}
    kept, dropped = verify_evidence(raw_moves.get("evidence"), corpus=corpus, graph_names=graph_names)
    stats["kept"] += len(kept)
    stats["stripped"] += len(dropped)
    stats["stripped_detail"].extend(dropped)
    confidence = _enum(raw_moves.get("confidence"), _CONFIDENCE, "inferred")
    if not kept:
        confidence = "inferred"
    dimensions["move_history"] = {
        "verdict": _clean_text(raw_moves.get("verdict"), 300),
        "evidence": kept,
        "confidence": confidence,
        "moves": _normalize_moves(raw_moves.get("moves")),
        "current_move": _enum(raw_moves.get("current_move"), _MOVE_UNKNOWN, "unknown"),
    }

    dimensions["percentile"] = None  # S6-2：由 build_s62_dimensions 填充
    dimensions["motivation"] = None  # S6-2：由 build_s62_dimensions 填充
    dimensions["risks"] = None  # S6-3：由 build_risks_dimension 填充

    summary = " ".join(str(raw.get("consultant_summary") or "").split())[:600]
    return dimensions, summary, stats


# ---------------------------------------------------------------------------
# S6-2：水平分位 + 动机时机（落位/信号确定性，LLM 只读成顾问口径 verdict）
# ---------------------------------------------------------------------------

def _percentile_template_verdict(placement: dict[str, Any], *, direction: str, years_window: int | None) -> str:
    """LLM 未给 verdict 时的确定性模板（band 本身就是确定性计算结果，话术同样可模板化）。"""
    if placement.get("band") is None:
        return "历史参照样本为空，无法给出在同龄人里的位置判断，建议结合面试实地判断。"
    window = f"±{years_window}年" if years_window is not None else "不限年限"
    base = f"同方向（{direction}）{window}参照人群 N={placement['n']}（既有评估中位分 {placement.get('median')}）"
    insufficient = "" if placement.get("sample_sufficient") else f"参照样本不足（N<{placement.get('min_n', 8)}），谨慎参考："
    if placement["band"] == "top10":
        return f"{insufficient}{base}，该人选落位前 10%，属于同龄人里的第一梯队。"
    if placement["band"] == "top25":
        return f"{insufficient}{base}，该人选落位前 25%，高于多数同方向同龄人。"
    if placement["band"] == "median":
        return f"{insufficient}{base}，该人选落位中位区间，处于同方向同龄人的中间位置。"
    return f"{insufficient}{base}，该人选落位相对靠后，建议结合面试再核实真实水平。"


def _motivation_template_verdict(signals: list[dict[str, Any]]) -> str:
    """无信号时如实"未见明显变动信号"（红线 c）；有信号时列信号、诉求留面谈。"""
    if not signals:
        return (
            "未见明显变动信号：在职工况与其历史节奏未见显著偏离，也未采集到公司近况的公开变动信号；"
            "动机与时机需面谈核实。"
        )
    parts = [str(sig.get("summary") or "") for sig in signals[:2] if str(sig.get("summary") or "").strip()]
    return "变动信号：" + "；".join(parts) + "。动的可能性与真实诉求需结合面谈核实。"


def _merge_evidence(*groups: list[dict[str, str]], limit: int = 8) -> list[dict[str, str]]:
    """确定性证据 + LLM 校验后证据合并去重（按 type+ref），限量。"""
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group:
            key = (str(item.get("type") or ""), str(item.get("ref") or ""))
            if key in seen or not key[0] or not key[1]:
                continue
            seen.add(key)
            merged.append({"type": key[0], "ref": key[1]})
        if len(merged) >= limit:
            break
    return merged[:limit]


def build_s62_dimensions(
    raw_pm: dict[str, Any] | None,
    *,
    corpus: str,
    placement: dict[str, Any],
    basis: str,
    direction: str,
    years_window: int | None,
    signals: list[dict[str, Any]],
    stats: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """组装 percentile/motivation 两维；band/signals 由确定性侧强制写入，LLM 只提供 verdict。

    raw_pm=None（模型不可用/输出非法）→ 两维 verdict 用确定性模板，并在 stats 记 fallback。
    证据：percentile 挂知识库参照系摘要（白名单唯一合法值）；motivation 挂公开信号 URL +
    工况信号所在简历行（逐字校验）。LLM 返回的证据过同一道 verify_evidence 闸。
    """
    raw_pm = raw_pm if isinstance(raw_pm, dict) else {}
    kb_ref = assessment_signals.reference_summary_text(placement, direction=direction, years_window=years_window)
    url_refs = [str(sig.get("url")) for sig in signals if sig.get("source") == "公开信息" and sig.get("url")]

    raw_percentile = raw_pm.get("percentile") if isinstance(raw_pm.get("percentile"), dict) else {}
    kept, dropped = verify_evidence(
        raw_percentile.get("evidence"), corpus=corpus, graph_names=[], kb_refs=[kb_ref], url_refs=url_refs
    )
    stats["kept"] += len(kept)
    stats["stripped"] += len(dropped)
    stats["stripped_detail"].extend(dropped)
    evidence = _merge_evidence([{"type": "知识库", "ref": kb_ref}], kept)
    confidence = _enum(raw_percentile.get("confidence"), _CONFIDENCE, "inferred")
    # 确定性闸：无法落位 / 参照样本不足 → 强制 inferred 并注明样本不足
    if placement.get("band") is None or not placement.get("sample_sufficient"):
        confidence = "inferred"
    if not evidence:
        confidence = "inferred"
    percentile = {
        "verdict": _clean_text(raw_percentile.get("verdict"), 300)
        or _percentile_template_verdict(placement, direction=direction, years_window=years_window),
        "band": placement.get("band"),
        "basis": basis,
        "score": placement.get("score"),
        "percentile_rank": placement.get("percentile_rank"),
        "reference": {
            "n": placement.get("n"),
            "direction": direction,
            "years_window": years_window,
            "median": placement.get("median"),
            "q25": placement.get("q25"),
            "q75": placement.get("q75"),
            "min": placement.get("min"),
            "max": placement.get("max"),
            "sample_sufficient": bool(placement.get("sample_sufficient")),
            "min_n": placement.get("min_n"),
            "note": "" if placement.get("sample_sufficient") else "参照样本不足，结论按推测口径",
        },
        "evidence": evidence,
        "confidence": confidence,
    }

    raw_motivation = raw_pm.get("motivation") if isinstance(raw_pm.get("motivation"), dict) else {}
    kept, dropped = verify_evidence(
        raw_motivation.get("evidence"), corpus=corpus, graph_names=[], kb_refs=[kb_ref], url_refs=url_refs
    )
    stats["kept"] += len(kept)
    stats["stripped"] += len(dropped)
    stats["stripped_detail"].extend(dropped)
    deterministic_evidence: list[dict[str, str]] = []
    for sig in signals:
        if sig.get("source") == "公开信息" and sig.get("url"):
            deterministic_evidence.append({"type": "公开信息", "ref": str(sig["url"])})
        elif sig.get("evidence_line") and _verbatim_hit(str(sig["evidence_line"]), corpus):
            deterministic_evidence.append({"type": "简历", "ref": str(sig["evidence_line"])})
    evidence = _merge_evidence(deterministic_evidence, kept)
    confidence = _enum(raw_motivation.get("confidence"), _CONFIDENCE, "inferred")
    # 确定性闸：无信号 → 强制 inferred；证据归零 → 强制 inferred
    if not signals or not evidence:
        confidence = "inferred"
    motivation = {
        "verdict": _clean_text(raw_motivation.get("verdict"), 300) or _motivation_template_verdict(signals),
        "signals": signals,
        "evidence": evidence,
        "confidence": confidence,
    }
    return percentile, motivation


# ---------------------------------------------------------------------------
# S6-3：风险点（需要核实的问题）—— 确定性检出 + LLM 语义项，证据同一道闸
# ---------------------------------------------------------------------------

def _risks_template_verdict(items: list[dict[str, Any]], checks_run: list[str]) -> str:
    """risks verdict 确定性模板（口径：需要核实的问题，不定罪）。"""
    if not items:
        if checks_run:
            return "未见需核实的问题（已按简历时间线、任期节奏与岗位硬条件逐项核对）。"
        return "简历信息不足，未能完成风险核对，建议补充简历后再评估。"
    counts = {"high": 0, "medium": 0, "low": 0}
    for item in items:
        counts[str(item.get("severity") or "low")] = counts.get(str(item.get("severity") or "low"), 0) + 1
    parts = [f"{LABELS[level]} {counts[level]} 项" for level in ("high", "medium", "low") if counts[level]]
    return f"共 {len(items)} 项需要核实的问题（{'，'.join(parts)}），逐项附证据，请逐条核实后再下判断。"


def build_risks_dimension(
    raw_risks: dict[str, Any] | None,
    *,
    deterministic_items: list[dict[str, Any]],
    checks_run: list[str],
    corpus: str,
    graph_hits: list[dict[str, Any]],
    stats: dict[str, Any],
) -> dict[str, Any]:
    """组装 risks 维：确定性 items（gap/频繁跳动/时间线冲突/硬条件差距）+ LLM 语义 items（title 通胀/过度包装）。

    - 确定性 items 由 assessment_signals 产出，evidence 已是简历逐字行，直接采信（仍过一遍闸留痕）。
    - LLM items：kind 只允许 title_inflation/over_packaging（其余视为越权，丢弃）；severity 非法值压 low；
      evidence 过 verify_evidence 同一道闸，证据归零的 LLM 项整条丢弃（记 stripped_detail，防编造）。
    - confidence：items 非空且每条都有证据 → certain；任一 item 无证据（如技能缺项这类" absence 型"）
      → inferred；items 为空时按 checks_run（确实核对了 → certain；无数据可核对 → inferred）。
    raw_risks=None（模型不可用/输出非法）→ 仅确定性 items，调用方在 signal_stats 记 fallback。
    """
    graph_names = [hit["graph_name"] for hit in graph_hits]
    items: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    llm_dropped = 0

    def _append_item(kind: str, risk: Any, severity: Any, kept: list[dict[str, str]]) -> None:
        text = _clean_text(risk, 200)
        if not text or text in seen_texts or len(items) >= RISK_ITEM_MAX:
            return
        seen_texts.add(text)
        items.append(
            {
                "kind": kind if kind in _RISK_KINDS else "over_packaging",
                "risk": text,
                "severity": _enum(severity, _SEVERITY, "low"),
                "evidence": kept,
            }
        )

    for entry in deterministic_items:
        if not isinstance(entry, dict):
            continue
        kept, dropped = verify_evidence(entry.get("evidence"), corpus=corpus, graph_names=graph_names)
        stats["kept"] += len(kept)
        stats["stripped"] += len(dropped)
        stats["stripped_detail"].extend(dropped)
        _append_item(str(entry.get("kind") or ""), entry.get("risk"), entry.get("severity"), kept)

    raw_risks = raw_risks if isinstance(raw_risks, dict) else {}
    for entry in (raw_risks.get("items") if isinstance(raw_risks.get("items"), list) else [])[:4]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        if kind not in _RISK_LLM_KINDS:
            llm_dropped += 1
            stats["stripped_detail"].append(
                {"type": "风险项", "ref": _clean_text(entry.get("risk"), 80), "reason": "LLM 风险项 kind 越权（仅允许 title_inflation/over_packaging），丢弃"}
            )
            continue
        kept, dropped = verify_evidence(entry.get("evidence"), corpus=corpus, graph_names=graph_names)
        stats["kept"] += len(kept)
        stats["stripped"] += len(dropped)
        stats["stripped_detail"].extend(dropped)
        if not kept:
            llm_dropped += 1
            stats["stripped_detail"].append(
                {"type": "风险项", "ref": _clean_text(entry.get("risk"), 80), "reason": "LLM 风险项证据全部未过逐字校验，整条丢弃"}
            )
            continue
        _append_item(kind, entry.get("risk"), entry.get("severity"), kept)

    items.sort(key=lambda item: _SEVERITY_RANK.get(str(item.get("severity") or "low"), 2))
    evidence = _merge_evidence(*[item["evidence"] for item in items])
    if items:
        confidence = "certain" if all(item["evidence"] for item in items) else "inferred"
    else:
        confidence = "certain" if checks_run else "inferred"
    return {
        "verdict": _risks_template_verdict(items, checks_run),
        "items": items,
        "evidence": evidence,
        "confidence": confidence,
        "stats": {
            "deterministic_items": len(deterministic_items),
            "llm_items_kept": len(items) - len([i for i in items if i["kind"] not in _RISK_LLM_KINDS]),
            "llm_items_dropped": llm_dropped,
            "checks_run": list(checks_run),
        },
    }


def refresh_risks_dimension(risks: dict[str, Any]) -> None:
    """敏感剥离后重算 risks 维的 verdict / dim 级 evidence / confidence（item 级剥离在 run_assessment）。"""
    items = [item for item in (risks.get("items") or []) if isinstance(item, dict)]
    risks["items"] = items
    checks = (risks.get("stats") or {}).get("checks_run") or []
    risks["verdict"] = _risks_template_verdict(items, checks)
    risks["evidence"] = _merge_evidence(*[item.get("evidence") or [] for item in items])
    if items:
        risks["confidence"] = "certain" if all(item.get("evidence") for item in items) else "inferred"
    else:
        risks["confidence"] = "certain" if checks else "inferred"


# ---------------------------------------------------------------------------
# artifact 校验 / 构建 / 幂等 upsert / 读取
# ---------------------------------------------------------------------------

def validate_assessment(doc: Any) -> list[str]:
    """schema 校验：必备键 / 版本 / 两维结构 / 占位维度 null / 枚举值域。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["assessment 必须是 JSON 对象"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {SCHEMA_VERSION}")
    for key in ("candidate_id", "job_id", "as_of"):
        if doc.get(key) in (None, ""):
            errors.append(f"缺必备键 {key}")
    dimensions = doc.get("dimensions")
    if not isinstance(dimensions, dict):
        errors.append("dimensions 必须是对象")
        return errors
    for name in DIMENSIONS_IMPLEMENTED:
        dim = dimensions.get(name)
        if not isinstance(dim, dict):
            errors.append(f"dimensions.{name} 必须是对象")
            continue
        if not str(dim.get("verdict") or "").strip():
            errors.append(f"dimensions.{name}.verdict 不能为空")
        if dim.get("confidence") not in _CONFIDENCE:
            errors.append(f"dimensions.{name}.confidence 必须是 certain|inferred")
        evidence = dim.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"dimensions.{name}.evidence 必须是数组")
        elif not evidence and dim.get("confidence") == "certain" and name != "risks":
            # risks 空态（未见需核实的问题）本身无证据，certain 与否由 risks 专项规则按 checks_run 判定
            errors.append(f"dimensions.{name} 无证据时 confidence 不得为 certain")
        for item in evidence or []:
            if not isinstance(item, dict) or item.get("type") not in _EVIDENCE_TYPES or not str(item.get("ref") or "").strip():
                errors.append(f"dimensions.{name}.evidence 存在非法条目")
    trajectory = dimensions.get("trajectory") if isinstance(dimensions.get("trajectory"), dict) else {}
    if trajectory:
        if trajectory.get("promotion_pace") not in _PACE:
            errors.append("trajectory.promotion_pace 枚举非法")
        if trajectory.get("tech_evolution") not in _EVOLUTION:
            errors.append("trajectory.tech_evolution 枚举非法")
        if not isinstance(trajectory.get("segments"), list):
            errors.append("trajectory.segments 必须是数组")
    move_history = dimensions.get("move_history") if isinstance(dimensions.get("move_history"), dict) else {}
    if move_history:
        if move_history.get("current_move") not in _MOVE_UNKNOWN:
            errors.append("move_history.current_move 枚举非法")
        if not isinstance(move_history.get("moves"), list):
            errors.append("move_history.moves 必须是数组")
        for move in move_history.get("moves") or []:
            if isinstance(move, dict):
                for key in ("direction", "platform", "title_direction", "responsibility_direction"):
                    if move.get(key) not in _DIRECTION:
                        errors.append(f"move_history.moves.{key} 枚举非法")
    percentile = dimensions.get("percentile") if isinstance(dimensions.get("percentile"), dict) else {}
    if percentile:
        band = percentile.get("band")
        reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
        ref_n = reference.get("n")
        if band is not None and band not in _BANDS:
            errors.append("percentile.band 必须是 top10|top25|median|below（null 仅限参照样本为空）")
        if band is None and ref_n != 0:
            errors.append("percentile.band 为 null 仅限参照样本 N=0")
        if not isinstance(ref_n, int) or ref_n < 0:
            errors.append("percentile.reference.n 必须是非负整数")
        elif ref_n < assessment_signals.MIN_REFERENCE_N and percentile.get("confidence") != "inferred":
            errors.append("percentile 参照样本不足时 confidence 必须为 inferred")
        if percentile.get("basis") not in _PERCENTILE_BASIS:
            errors.append("percentile.basis 必须是 fit_score|trajectory_features")
        rank = percentile.get("percentile_rank")
        if rank is not None and not (isinstance(rank, (int, float)) and 0.0 <= float(rank) <= 1.0):
            errors.append("percentile.percentile_rank 必须在 0-1 之间")
    motivation = dimensions.get("motivation") if isinstance(dimensions.get("motivation"), dict) else {}
    if motivation:
        signals = motivation.get("signals")
        if not isinstance(signals, list):
            errors.append("motivation.signals 必须是数组")
            signals = []
        for signal in signals or []:
            if not isinstance(signal, dict) or not str(signal.get("kind") or "").strip() or not str(signal.get("summary") or "").strip():
                errors.append("motivation.signals 存在非法条目（缺 kind/summary）")
                continue
            if signal.get("source") == "公开信息" and (
                not str(signal.get("url") or "").strip() or not str(signal.get("as_of") or "").strip()
            ):
                errors.append("motivation 公开信息信号必须带来源 URL 与 as_of")
        if not signals and motivation.get("confidence") != "inferred":
            errors.append("motivation 无信号时 confidence 必须为 inferred（如实『未见明显变动信号』）")
    risks = dimensions.get("risks") if isinstance(dimensions.get("risks"), dict) else {}
    if risks:
        risk_items = risks.get("items")
        if not isinstance(risk_items, list):
            errors.append("risks.items 必须是数组")
            risk_items = []
        if len(risk_items) > RISK_ITEM_MAX:
            errors.append(f"risks.items 最多 {RISK_ITEM_MAX} 条")
        for item in risk_items:
            if not isinstance(item, dict):
                errors.append("risks.items 存在非法条目（必须是对象）")
                continue
            if not str(item.get("risk") or "").strip():
                errors.append("risks.items 存在非法条目（risk 为空）")
            if item.get("severity") not in _SEVERITY:
                errors.append("risks.items.severity 必须是 high|medium|low")
            kind = item.get("kind")
            if kind is not None and kind not in _RISK_KINDS:
                errors.append("risks.items.kind 枚举非法")
            item_evidence = item.get("evidence")
            if not isinstance(item_evidence, list):
                errors.append("risks.items.evidence 必须是数组")
                continue
            for ev in item_evidence:
                if not isinstance(ev, dict) or ev.get("type") not in _EVIDENCE_TYPES or not str(ev.get("ref") or "").strip():
                    errors.append("risks.items.evidence 存在非法条目")
        if risk_items and any(not (item.get("evidence") or []) for item in risk_items if isinstance(item, dict)):
            if risks.get("confidence") != "inferred":
                errors.append("risks 存在无证据条目时 confidence 必须为 inferred（如技能缺项类 absence 证据）")
        if not risk_items:
            if not str(risks.get("verdict") or "").strip():
                errors.append("risks 无条目时 verdict 也必须如实说明（如『未见需核实的问题』）")
            risk_stats = risks.get("stats") if isinstance(risks.get("stats"), dict) else {}
            if risks.get("confidence") == "certain" and not (risk_stats.get("checks_run") or []):
                errors.append("risks 空态且未实际执行核对（checks_run 为空）时 confidence 必须为 inferred")
    if doc.get("advisor_action") not in ADVISOR_ACTIONS:
        errors.append("advisor_action 必须是 pending|accepted|modified|rejected")
    return errors


def build_archetype_reference(
    archetype: dict[str, Any] | None,
    *,
    skill_ontology: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """岗位原型评估对照上下文（source=kb_archetype）：硬门槛 + 典型证据形式。

    知识飞轮二期：评估直接消费岗位原型。命中原型时返回对照块（注入 LLM payload）：
    - hard_gates：原型 negative_rules（客户校准硬门槛/负向规则）；
    - typical_evidence_forms：原型 skills_ontology_nodes（canonical 词）在技能本体里的
      典型证据形式，供技能证据归一比对；
    只做对照提示，不改变评分门槛；无命中/无 id 返回 None，调用方完全不注入（保持现状）。
    """
    if not isinstance(archetype, dict) or not str(archetype.get("archetype_id") or "").strip():
        return None
    level_mapping = archetype.get("level_mapping") if isinstance(archetype.get("level_mapping"), dict) else {}
    hard_gates = [str(rule).strip() for rule in archetype.get("negative_rules") or [] if str(rule or "").strip()][:8]
    typical_evidence: list[str] = []
    ontology_skills = (skill_ontology or {}).get("skills") if isinstance(skill_ontology, dict) else None
    if isinstance(ontology_skills, dict):
        for node in archetype.get("skills_ontology_nodes") or []:
            skill = ontology_skills.get(str(node or "").strip()) or {}
            for form in skill.get("evidence") or []:
                text = str(form or "").strip()
                if text and text not in typical_evidence:
                    typical_evidence.append(text)
    return {
        "source": "kb_archetype",
        "archetype_id": str(archetype.get("archetype_id") or ""),
        "archetype_title": str(archetype.get("title") or ""),
        "matched_by": str(archetype.get("matched_by") or ""),
        "match_reason": str(archetype.get("match_reason") or ""),
        "essence": str(archetype.get("essence") or ""),
        "hard_gates": hard_gates,
        "accepted_levels": [str(item) for item in level_mapping.get("accepted_candidate_levels") or []],
        "level_note": str(level_mapping.get("note") or ""),
        "location_policy": str(archetype.get("location_policy") or ""),
        "typical_evidence_forms": typical_evidence[:12],
        "usage_note": "评估对照上下文：用于硬门槛核验与技能证据形式比对，不改变评分门槛；"
        "只命中技能标签无项目证据的人选必须标记待核验（沿用原型负向规则口径）",
    }


def build_llm_payload(
    *,
    candidate: dict[str, Any],
    job: dict[str, Any],
    strategy_doc: dict[str, Any] | None,
    graph_hits: list[dict[str, Any]],
    skill_ontology: dict[str, Any] | None = None,
    archetype: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 LLM 输入：简历原文 + 岗位/策略要点 + 确定性图谱命中（只读事实）。

    知识飞轮二期：skill_ontology 非空时，把策略 step4 关键词经本体别名归一后的
     canonical 技能词（source=kb_skill）一并注入，供技能证据归一比对；保守接入，
    只做提示，不改变评分门槛；无本体或无命中时该块整个不出现。
    知识飞轮二期（原型直消）：archetype 非空（strategy_v2.match_job_archetype 同一套
    可解释匹配命中）时注入 archetype_reference 对照块（source=kb_archetype：硬门槛 +
    典型证据形式）；无命中时该键整个不出现，行为与之前完全一致。
    """
    strategy: dict[str, Any] = {}
    if strategy_doc:
        step1 = strategy_doc.get("step1_job_essence") if isinstance(strategy_doc.get("step1_job_essence"), dict) else {}
        step3 = strategy_doc.get("step3_level_mapping") if isinstance(strategy_doc.get("step3_level_mapping"), dict) else {}
        evaluation_constraints = (
            strategy_doc.get("evaluation_constraints")
            if isinstance(strategy_doc.get("evaluation_constraints"), dict)
            else {}
        )
        pool: list[dict[str, str]] = []
        for entry in strategy_doc.get("step2_target_pool") or []:
            if not isinstance(entry, dict):
                continue
            for company in entry.get("companies") or []:
                if isinstance(company, dict):
                    pool.append(
                        {
                            "name": str(company.get("name") or ""),
                            "tier": str(entry.get("tier") or ""),
                            "path": str(entry.get("path") or ""),
                        }
                    )
        strategy = {
            "job_essence": str(step1.get("statement") or ""),
            "accepted_levels": [str(item) for item in step3.get("accepted_levels") or []],
            "evaluation_constraints": {
                "locations": [str(item) for item in evaluation_constraints.get("locations") or []],
                "levels": [str(item) for item in evaluation_constraints.get("levels") or []],
                "scenarios": [str(item) for item in evaluation_constraints.get("scenarios") or []],
            },
            "target_pool": pool[:20],
        }
        ontology = skill_ontology if isinstance(skill_ontology, dict) and skill_ontology.get("skills") else None
        if ontology:
            normalized_terms: list[dict[str, str]] = []
            seen_canonical: set[str] = set()
            for group in strategy_doc.get("step4_keyword_groups") or []:
                if not isinstance(group, dict):
                    continue
                for term in group.get("terms") or []:
                    info = knowledge_base.normalize_skill(term, ontology)
                    if info["matched"] and info["canonical"] not in seen_canonical:
                        seen_canonical.add(info["canonical"])
                        normalized_terms.append(
                            {
                                "raw": str(term or "").strip(),
                                "canonical": info["canonical"],
                                "family": info["family"],
                                "source": "kb_skill",
                            }
                        )
            if normalized_terms:
                strategy["skill_terms_normalized"] = normalized_terms[:30]
    payload: dict[str, Any] = {
        "task": "判人评估：职业轨迹 + 跳槽质量史（S6-1）",
        "candidate": {
            "current_company": str(candidate.get("current_company") or ""),
            "current_title": str(candidate.get("current_title") or ""),
            "education": str(candidate.get("education") or ""),
            "experience": str(candidate.get("experience") or ""),
            "resume_full_text": str(candidate.get("full_text") or "")[:6000],
            "resume_work_text": str(candidate.get("work_text") or "")[:4000],
            "resume_project_text": str(candidate.get("project_text") or "")[:3000],
            "resume_education_text": str(candidate.get("education_text") or "")[:1500],
        },
        "job": {
            "client": str(job.get("client") or ""),
            "title": str(job.get("title") or ""),
            "summary": _clean_text(job.get("summary"), 400),
            "hard_requirements": _clean_text(job.get("hard_requirements"), 400),
        },
        "strategy_v2": strategy,
        "graph_hits": graph_hits,
    }
    # 知识飞轮二期：岗位原型对照块（source=kb_archetype）；无命中完全不注入，保持现状。
    archetype_reference = build_archetype_reference(archetype, skill_ontology=skill_ontology)
    if archetype_reference:
        payload["archetype_reference"] = archetype_reference
    return payload


def _artifact_markdown(doc: dict[str, Any]) -> str:
    """artifact content（内部 markdown 视图，业务语言）。"""
    lines = [
        f"# 判人评估：{doc.get('candidate_name_masked') or ''} × {doc.get('job_title') or ''}",
        "",
        f"- 生成时间：{doc.get('as_of')}｜评估器版本：{doc.get('assessor_version')}｜模型：{doc.get('model')}",
        f"- 顾问动作：{doc.get('advisor_action')}（评估只辅助判断，不构成任何决策建议）",
        "",
        "## 职业轨迹",
        "",
    ]
    trajectory = (doc.get("dimensions") or {}).get("trajectory") or {}
    lines.append(f"**结论**：{trajectory.get('verdict') or ''}（置信度：{LABELS.get(trajectory.get('confidence'), trajectory.get('confidence'))}）")
    lines.append(
        f"- 晋升速度：{LABELS.get(trajectory.get('promotion_pace'), '无法判断')}｜"
        f"技术栈演进：{LABELS.get(trajectory.get('tech_evolution'), '无法判断')}"
    )
    for segment in trajectory.get("segments") or []:
        tier = LABELS.get(segment.get("tier"), "无法判断")
        source = "图谱" if segment.get("tier_source") == "graph" else "推测"
        lines.append(
            f"- {segment.get('period') or ''} {segment.get('company') or ''}｜{segment.get('title') or ''}"
            f"｜平台：{tier}（{source}）｜{segment.get('note') or ''}"
        )
    lines.extend(["", "## 跳槽质量史", ""])
    move_history = (doc.get("dimensions") or {}).get("move_history") or {}
    lines.append(f"**结论**：{move_history.get('verdict') or ''}（置信度：{LABELS.get(move_history.get('confidence'), move_history.get('confidence'))}）")
    for move in move_history.get("moves") or []:
        lines.append(
            f"- {move.get('from') or ''} → {move.get('to') or ''}：{LABELS.get(move.get('direction'), '平移')}"
            f"（平台 {LABELS.get(move.get('platform'), '平移')}/title {LABELS.get(move.get('title_direction'), '平移')}"
            f"/职责 {LABELS.get(move.get('responsibility_direction'), '平移')}）— {move.get('reason') or ''}"
        )
    lines.append(f"- 当前这一单对他：{LABELS.get(move_history.get('current_move'), '无法判断')}")
    percentile = (doc.get("dimensions") or {}).get("percentile") or {}
    if percentile:
        lines.extend(["", "## 在同龄人里的位置", ""])
        reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
        window = f"±{reference.get('years_window')}年" if reference.get("years_window") is not None else "不限年限"
        band_label = LABELS.get(percentile.get("band"), "无法落位") if percentile.get("band") else "无法落位"
        lines.append(
            f"**结论**：{percentile.get('verdict') or ''}"
            f"（置信度：{LABELS.get(percentile.get('confidence'), percentile.get('confidence'))}）"
        )
        lines.append(
            f"- 落位：{band_label}｜得分 {percentile.get('score')}（{LABELS.get(percentile.get('basis'), percentile.get('basis'))}）"
        )
        lines.append(
            f"- 参照系：同方向（{reference.get('direction') or ''}）{window}｜样本 N={reference.get('n')}"
            f"｜中位分 {reference.get('median')}（P25={reference.get('q25')}，P75={reference.get('q75')}）"
            + (f"｜{reference.get('note')}" if reference.get("note") else "")
        )
    motivation = (doc.get("dimensions") or {}).get("motivation") or {}
    if motivation:
        lines.extend(["", "## 动机与时机", ""])
        lines.append(
            f"**结论**：{motivation.get('verdict') or ''}"
            f"（置信度：{LABELS.get(motivation.get('confidence'), motivation.get('confidence'))}）"
        )
        for signal in motivation.get("signals") or []:
            if signal.get("url"):
                suffix = f"（来源：{signal.get('url')}，{signal.get('as_of')}）"
            elif signal.get("as_of"):
                suffix = f"（{signal.get('as_of')}）"
            else:
                suffix = ""
            lines.append(f"- [{signal.get('source') or ''}] {signal.get('summary') or ''}{suffix}")
        if not (motivation.get("signals") or []):
            lines.append("- 未见明显变动信号")
    risks = (doc.get("dimensions") or {}).get("risks") or {}
    if risks:
        lines.extend(["", "## 需要核实的问题", ""])
        lines.append(
            f"**结论**：{risks.get('verdict') or ''}"
            f"（置信度：{LABELS.get(risks.get('confidence'), risks.get('confidence'))}）"
        )
        for item in risks.get("items") or []:
            lines.append(
                f"- 【{LABELS.get(item.get('severity'), item.get('severity'))}｜{LABELS.get(item.get('kind'), item.get('kind'))}】"
                f"{item.get('risk') or ''}"
            )
            for ev in item.get("evidence") or []:
                lines.append(f"  - 证据（{ev.get('type')}）：{ev.get('ref')}")
        if not (risks.get("items") or []):
            lines.append("- 未见需核实的问题")
        lines.append("")
        lines.append("（以上为需要核实的问题清单，只辅助顾问判断，不构成任何决策建议）")
    lines.extend(["", "## 证据", ""])
    for name in DIMENSIONS_IMPLEMENTED:
        dim = (doc.get("dimensions") or {}).get(name) or {}
        for item in dim.get("evidence") or []:
            lines.append(f"- [{LABELS.get(name, name)}｜{item.get('type')}] {item.get('ref')}")
    lines.extend(["", "## 顾问口径摘要", "", str(doc.get("consultant_summary") or ""), ""])
    return "\n".join(lines)


def upsert_assessment(conn: sqlite3.Connection, doc: dict[str, Any]) -> str:
    """校验 + 幂等 upsert：同人同岗（candidate_id×job_id）更新原行，as_of 刷新，version 自增。

    校验不过抛 ValueError，整条拒写。
    """
    errors = validate_assessment(doc)
    if errors:
        raise ValueError("candidate_assessment 校验失败，拒绝写入：" + "；".join(errors))
    candidate_id = int(doc["candidate_id"])
    job_id = int(doc["job_id"])
    artifact_id = f"candidate_assessment_{candidate_id}_{job_id}"
    title = f"判人评估：{doc.get('candidate_name_masked') or candidate_id} × {doc.get('job_title') or job_id}"
    existing = conn.execute(
        "SELECT artifact_id,metadata_json FROM agent_artifacts WHERE artifact_id=? AND artifact_type=?",
        (artifact_id, ARTIFACT_TYPE),
    ).fetchone()
    if existing:
        previous = _loads(existing["metadata_json"], {})
        history = list(previous.get("history") or [])
        history.append(
            {
                "version": int(previous.get("version") or 1),
                "as_of": previous.get("as_of"),
                "model": previous.get("model"),
            }
        )
        doc["version"] = int(previous.get("version") or 1) + 1
        doc["history"] = history[-10:]
        conn.execute(
            """
            UPDATE agent_artifacts SET title=?,content=?,metadata_json=?,validation_status='passed'
            WHERE artifact_id=?
            """,
            (f"{title} v{doc['version']}", _artifact_markdown(doc), _dumps(doc), artifact_id),
        )
        return artifact_id
    doc["version"] = 1
    doc["history"] = []
    conn.execute(
        """
        INSERT INTO agent_artifacts
        (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            str(doc.get("goal_id") or f"candidate_{candidate_id}"),
            str(doc.get("workflow_id") or f"assessment_{candidate_id}_{job_id}"),
            None,
            ARTIFACT_TYPE,
            f"{title} v1",
            "text/markdown",
            None,
            _artifact_markdown(doc),
            _dumps(doc),
            "passed",
        ),
    )
    return artifact_id


def get_assessment(conn: sqlite3.Connection, candidate_id: int, job_id: int) -> dict[str, Any] | None:
    """按 candidate_id×job_id 读取 assessment；不存在返回 None。"""
    artifact_id = f"candidate_assessment_{int(candidate_id)}_{int(job_id)}"
    row = conn.execute(
        """
        SELECT artifact_id,title,content,metadata_json,created_at
        FROM agent_artifacts WHERE artifact_id=? AND artifact_type=?
        """,
        (artifact_id, ARTIFACT_TYPE),
    ).fetchone()
    if row is None:
        return None
    doc = _loads(row["metadata_json"], {})
    return {
        "artifact_id": row["artifact_id"],
        "title": row["title"],
        "content": row["content"],
        "assessment": doc,
        "created_at": row["created_at"],
    }


def report_reference_block(assessment: dict[str, Any]) -> dict[str, Any]:
    """S6-3：推荐报告强制引用块 —— trajectory 结论 / 分位 band / 需核实问题 top 3 / 顾问口径摘要。

    返回 lines（可直接进报告"顾问评语"板块的行）+ 结构化字段（报告产物 metadata 留痕用）。
    旧版评估（risks 为 null 占位）如实标注 risks_pending，由顾问重新评估后报告自动带上风险维。
    呈现语义：需要核实的问题；只辅助判断，不构成决策建议。
    """
    assessment = assessment if isinstance(assessment, dict) else {}
    dimensions = assessment.get("dimensions") if isinstance(assessment.get("dimensions"), dict) else {}
    trajectory = dimensions.get("trajectory") if isinstance(dimensions.get("trajectory"), dict) else {}
    percentile = dimensions.get("percentile") if isinstance(dimensions.get("percentile"), dict) else {}
    risks = dimensions.get("risks") if isinstance(dimensions.get("risks"), dict) else None
    reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
    band = percentile.get("band")
    band_label = LABELS.get(band, "无法落位") if band else "无法落位"
    window = f"±{reference.get('years_window')}年" if reference.get("years_window") is not None else "不限年限"
    summary = " ".join(str(assessment.get("consultant_summary") or "").split())
    lines = [
        f"判人评估（{assessment.get('as_of') or ''}）顾问口径：{summary or '（未生成摘要）'}",
        f"职业轨迹：{trajectory.get('verdict') or '未评估'}",
        f"在同龄人里的位置：{band_label}（同方向 {window}参照 N={reference.get('n') or 0}）",
    ]
    top_risks: list[dict[str, Any]] = []
    if risks is None:
        lines.append("需要核实的问题：当前评估为旧版（未含该维度），重新评估后报告将自动引用")
    elif not (risks.get("items") or []):
        lines.append("需要核实的问题：未见需核实的问题")
    else:
        ordered = sorted(
            [item for item in risks.get("items") or [] if isinstance(item, dict)],
            key=lambda item: _SEVERITY_RANK.get(str(item.get("severity") or "low"), 2),
        )
        top_risks = ordered[:3]
        lines.append("需要核实的问题：" + "；".join(str(item.get("risk") or "") for item in top_risks))
    lines.append("（以上摘自判人评估，只辅助顾问判断，不构成任何决策建议）")
    return {
        "lines": lines,
        "trajectory_verdict": str(trajectory.get("verdict") or ""),
        "percentile_band": band,
        "percentile_band_label": band_label,
        "reference_n": reference.get("n"),
        "top_risks": [
            {"risk": str(item.get("risk") or ""), "severity": str(item.get("severity") or ""), "kind": str(item.get("kind") or "")}
            for item in top_risks
        ],
        "risks_pending": risks is None,
        "consultant_summary": summary,
        "assessor_version": str(assessment.get("assessor_version") or ""),
        "as_of": str(assessment.get("as_of") or ""),
    }


def apply_advisor_action(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    job_id: int,
    action: str,
    note: str = "",
) -> dict[str, Any]:
    """S6-1b：顾问动作写回（采纳/改判/否决）——只更新 advisor_action/advisor_note/updated_at。

    version 不 bump、as_of 不动（评估内容未变，只是顾问结论变化）；artifact markdown 同步重渲。
    LookupError：人选/岗位不存在或不匹配、尚无评估；ValueError：非法 action。
    已 action 的可再次写回（顾问改主意是正常业务流）；不 commit（调用方决定事务）。
    """
    relation = conn.execute(
        "SELECT id,job_id,person_id FROM job_candidates WHERE id=?", (int(candidate_id),)
    ).fetchone()
    if relation is None:
        raise LookupError(f"人选不存在：{candidate_id}")
    if int(relation["job_id"] or 0) != int(job_id):
        raise LookupError(f"人选 {candidate_id} 不属于岗位 {job_id}")
    payload = get_assessment(conn, int(candidate_id), int(job_id))
    if payload is None:
        raise LookupError(f"人选 {candidate_id} 在岗位 {job_id} 下还没有判人评估，请先 POST 生成")
    action_value = str(action or "").strip()
    if action_value not in ADVISOR_ACTIONS:
        raise ValueError(f"action 必须是 {'/'.join(ADVISOR_ACTIONS)}，收到：{action_value or '空'}")

    doc = payload["assessment"]
    doc["advisor_action"] = action_value
    doc["advisor_note"] = _clean_text(note, 600)
    doc["updated_at"] = _now()
    artifact_id = str(payload["artifact_id"])
    conn.execute(
        "UPDATE agent_artifacts SET content=?,metadata_json=? WHERE artifact_id=?",
        (_artifact_markdown(doc), _dumps(doc), artifact_id),
    )
    label = ADVISOR_ACTION_LABELS.get(action_value, action_value)
    summary = f"判人评估顾问动作：{label}"
    if doc["advisor_note"]:
        summary += f"（备注：{doc['advisor_note'][:80]}）"
    conn.execute(
        """
        INSERT INTO candidate_events
        (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
        VALUES (?,?,?,'candidate_assessment_advisor_action','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
        """,
        (
            int(candidate_id),
            int(relation["person_id"]) if relation["person_id"] else None,
            int(job_id),
            summary,
            _dumps({"artifact_id": artifact_id, "advisor_action": action_value, "advisor_note": doc["advisor_note"]}),
            artifact_id,
        ),
    )
    # S6-4：改判/否决回流校准样例库（敏感因子拒入只拦样例，不阻断动作写回）
    from . import assessment_calibration

    calibration = assessment_calibration.sync_calibration_sample(conn, artifact_id=artifact_id, doc=doc)
    return {
        "ok": True,
        "candidate_id": int(candidate_id),
        "job_id": int(job_id),
        "artifact_id": artifact_id,
        "advisor_action": action_value,
        "advisor_note": doc["advisor_note"],
        "updated_at": doc["updated_at"],
        "calibration": calibration,
        "assessment": doc,
    }


# ---------------------------------------------------------------------------
# 评估主流程：取数 → LLM → 确定性校验 → 敏感扫描 → 落库
# ---------------------------------------------------------------------------

def load_candidate_resume(conn: sqlite3.Connection, candidate_id: int) -> dict[str, Any] | None:
    """读取 job_candidate + people + 最长简历语料（source_profiles / candidate_events / legacy）。"""
    base = conn.execute(
        """
        SELECT jc.id AS candidate_id,jc.job_id,jc.person_id,
               p.display_name,p.current_company,p.current_title,p.city,p.education,p.experience,
               legacy.skills AS legacy_profile_text
          FROM job_candidates jc
          JOIN people p ON p.id=jc.person_id
          LEFT JOIN candidates legacy ON CAST(legacy.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
         WHERE jc.id=?
        """,
        (int(candidate_id),),
    ).fetchone()
    if base is None:
        return None
    item = dict(base)
    resume: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT raw_json FROM source_profiles WHERE person_id=? ORDER BY COALESCE(source_date,''),id DESC",
        (item["person_id"],),
    ).fetchall()
    for row in rows:
        raw = _loads(row["raw_json"], {})
        if not isinstance(raw, dict):
            continue
        if len(str(raw.get("full_text") or raw.get("profile_text") or "")) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
            resume = raw
    event_rows = conn.execute(
        """
        SELECT raw_json FROM candidate_events
         WHERE job_candidate_id=? OR person_id=?
         ORDER BY COALESCE(event_time,'') DESC,id DESC
        """,
        (int(candidate_id), item["person_id"]),
    ).fetchall()
    for row in event_rows:
        raw = _loads(row["raw_json"], {})
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("full_text") or raw.get("profile_text") or raw.get("candidate_profile_text") or raw.get("content") or "")
        if len(text) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
            resume = {**raw, "full_text": raw.get("full_text") or text, "profile_text": raw.get("profile_text") or text}
    legacy_text = str(item.get("legacy_profile_text") or "").strip()
    if len(legacy_text) > len(str(resume.get("full_text") or resume.get("profile_text") or "")):
        resume = {"full_text": legacy_text, "profile_text": legacy_text}
    item["resume"] = resume
    return item


def assessment_input_hash(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    job_id: int,
    llm: BaseLLM,
    today: date | None = None,
) -> str:
    """Fingerprint every local input that can materially change an assessment.

    The date deliberately participates so public-company signals are refreshed at
    least daily, while repeated clicks against unchanged evidence avoid three model calls.
    """
    candidate = load_candidate_resume(conn, int(candidate_id))
    if candidate is None:
        raise LookupError(f"人选不存在：{candidate_id}")
    if int(candidate.get("job_id") or 0) != int(job_id):
        raise LookupError(f"人选 {candidate_id} 不属于岗位 {job_id}")
    job = conn.execute(
        """SELECT j.id,j.title,j.summary,j.hard_requirements,c.name AS client
             FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?""",
        (int(job_id),),
    ).fetchone()
    if job is None:
        raise LookupError(f"岗位不存在：{job_id}")
    strategy = conn.execute(
        """SELECT a.artifact_id,a.metadata_json
             FROM agent_workflows w
             JOIN agent_goals g ON g.goal_id=w.goal_id
             JOIN agent_artifacts a ON a.workflow_id=w.workflow_id AND a.artifact_type='search_strategy'
            WHERE g.context_type='job' AND g.context_id=?
            ORDER BY a.id DESC LIMIT 1""",
        (int(job_id),),
    ).fetchone()
    calibration_rows = conn.execute(
        """SELECT id,created_at FROM assessment_calibration_samples
            WHERE client=? OR job_type=? ORDER BY id DESC LIMIT 5""",
        (
            str(job["client"] or ""),
            assessment_calibration.normalize_job_type(job["title"]),
        ),
    ).fetchall()
    payload = {
        "candidate": candidate,
        "job": dict(job),
        "strategy": dict(strategy) if strategy is not None else None,
        "calibration": [dict(row) for row in calibration_rows],
        "assessor_version": ASSESSOR_VERSION,
        "model": str(getattr(llm, "model", "unknown")),
        "refresh_date": (today or date.today()).isoformat(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def log_scan_block(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    job_id: int,
    person_id: int | None,
    hits: list[dict[str, str]],
) -> None:
    """敏感扫描命中 → 拒写时的扫描日志（candidate_events 业务时间线，可审计）。"""
    conn.execute(
        """
        INSERT INTO candidate_events
        (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
        VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'candidate_assessment',NULL)
        """,
        (
            int(candidate_id),
            int(person_id) if person_id else None,
            int(job_id),
            "assessment_sensitive_scan_blocked",
            "blocked",
            f"判人评估拒写：敏感属性/决策禁语扫描命中 {len(hits)} 处，已拦截未落库",
            _dumps({"hits": hits[:20], "assessor_version": ASSESSOR_VERSION}),
        ),
    )


def persist_assessment(conn: sqlite3.Connection, doc: dict[str, Any]) -> str:
    """落库通道（路由与回放共用）：upsert artifact + 写岗位时间线；不 commit（调用方决定事务）。"""
    artifact_id = upsert_assessment(conn, doc)
    stats = doc.get("evidence_stats") or {}
    trajectory = (doc.get("dimensions") or {}).get("trajectory") or {}
    move_history = (doc.get("dimensions") or {}).get("move_history") or {}
    percentile = (doc.get("dimensions") or {}).get("percentile") or {}
    motivation = (doc.get("dimensions") or {}).get("motivation") or {}
    risks = (doc.get("dimensions") or {}).get("risks") or {}
    risk_items = risks.get("items") or []
    summary = (
        f"生成判人评估（职业轨迹/跳槽质量史/在同龄人里的位置/动机与时机/需要核实的问题）：置信度 "
        f"{trajectory.get('confidence')}/{move_history.get('confidence')}/"
        f"{percentile.get('confidence')}/{motivation.get('confidence')}/{risks.get('confidence')}，"
        f"分位 {percentile.get('band') or '无法落位'}（参照 N={(percentile.get('reference') or {}).get('n')}），"
        f"动机信号 {len(motivation.get('signals') or [])} 条，"
        f"需要核实的问题 {len(risk_items)} 项（高 {sum(1 for i in risk_items if i.get('severity') == 'high')} 项），"
        f"证据 {stats.get('kept', 0)} 条（剥离 {stats.get('stripped', 0)} 条）。"
        "评估只辅助顾问判断，不构成决策建议。"
    )
    conn.execute(
        """
        INSERT INTO candidate_events
        (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
        VALUES (?,?,?,'candidate_assessment_generated','completed',datetime('now','localtime'),?,?,'agent_artifacts',?)
        """,
        (
            int(doc["candidate_id"]),
            None,
            int(doc["job_id"]),
            summary,
            _dumps(
                {
                    "artifact_id": artifact_id,
                    "as_of": doc.get("as_of"),
                    "model": doc.get("model"),
                    "evidence_kept": stats.get("kept", 0),
                    "evidence_stripped": stats.get("stripped", 0),
                }
            ),
            artifact_id,
        ),
    )
    return artifact_id


def run_assessment(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    job_id: int,
    llm: BaseLLM,
    kb_dir: str | None = None,
    mask_name: Any = None,
    signal_fetcher: Callable[[str, float], tuple[int, str, str]] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """生成判人评估（不落库）：返回 doc；敏感扫描命中抛 ValueError（含 scan_blocked 标记）。

    LookupError：candidate/job 不存在或不匹配；ValueError：无简历语料 / 扫描命中；
    LLMError：模型不可用或输出非法（调用方映射 409）。
    S6-2：分位落位与动机信号全部确定性计算（assessment_signals），LLM 第二调只读成
    顾问口径 verdict；第二调失败降级为确定性模板 verdict（记 signal_stats），不阻断。
    signal_fetcher/today 仅供测试与回测注入（默认真实采集 / 真实当天）。
    S6-4：同客户/同岗位类型改判样例 ≤5 条注入三次 LLM 调用的 calibration 块
    （无样例不注入）；样例内容只进 prompt，不进 artifact 正文与任何对外文本。
    知识飞轮二期：命中岗位原型时把原型硬门槛/典型证据形式作为对照上下文注入首调
    payload（source=kb_archetype，signal_stats.archetype_reference 留痕）；无命中不注入。
    """
    today = today or date.today()
    candidate = load_candidate_resume(conn, candidate_id)
    if candidate is None:
        raise LookupError(f"人选不存在：{candidate_id}")
    if int(candidate.get("job_id") or 0) != int(job_id):
        raise LookupError(f"人选 {candidate_id} 不属于岗位 {job_id}")
    job = conn.execute(
        """
        SELECT j.id,j.title,j.summary,j.hard_requirements,c.name AS client
          FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
        """,
        (int(job_id),),
    ).fetchone()
    if job is None:
        raise LookupError(f"岗位不存在：{job_id}")
    job_item = dict(job)
    resume = candidate.get("resume") if isinstance(candidate.get("resume"), dict) else {}
    corpus = build_corpus(resume)
    if len(corpus.strip()) < 50:
        raise ValueError(f"人选 {candidate_id} 缺少可评估的简历数据，无法生成判人评估")

    strategy_doc = None
    strategy_ref = ""
    strategy = conn.execute(
        """
        SELECT w.workflow_id,g.goal_id,a.artifact_id,a.metadata_json
        FROM agent_workflows w
        JOIN agent_goals g ON g.goal_id=w.goal_id
        JOIN agent_artifacts a ON a.workflow_id=w.workflow_id AND a.artifact_type='search_strategy'
        WHERE g.context_type='job' AND g.context_id=?
        ORDER BY a.id DESC LIMIT 1
        """,
        (int(job_id),),
    ).fetchone()
    if strategy is not None:
        from . import strategy_v2

        strategy_doc = strategy_v2.extract_strategy_v2(strategy["metadata_json"])
        strategy_ref = str(strategy["artifact_id"])

    graph, _trace = knowledge_base.load_company_graph(kb_dir)
    # 知识飞轮二期：公司校准覆盖层（company_calibrations 表）合并后再做 graph_hits
    # 匹配 —— 只增强命中信息（校准 track/categories + source=consultant_calibrated），
    # 不改任何评分；覆盖层为空/DB 不可用时图谱保持原样，行为与现状一致。
    calibration_overlay, _overlay_trace = knowledge_base.load_calibration_overlay(_conn_db_path(conn))
    if calibration_overlay:
        graph, _apply_trace = knowledge_base.apply_calibration_overlay(graph, calibration_overlay)
    # 知识飞轮二期：技能本体（source=kb_skill）供技能证据归一比对；缺文件降级为空本体。
    skill_ontology, _skill_trace = knowledge_base.load_skill_ontology(kb_dir)
    # 知识飞轮二期（原型直消）：评估直接消费岗位原型 —— 用 strategy_v2 同一套可解释
    # 匹配，命中后把原型硬门槛/典型证据形式作为对照上下文注入 payload（source=kb_archetype，
    # 见 build_archetype_reference）；无命中时 archetype_hit=None，完全不注入，保持现状。
    from . import strategy_v2

    archetype_hit, _archetype_trace = strategy_v2.match_job_archetype(
        str(job_item.get("client") or ""), str(job_item.get("title") or ""), kb_dir=kb_dir
    )
    employers = extract_employers(corpus)
    current_company = str(candidate.get("current_company") or "").strip()
    if current_company and current_company not in employers:
        employers.insert(0, current_company)
    graph_hits = match_graph_hits(employers, graph)

    # ------------------------------------------------------------------
    # S6-4 校准注入：同客户或同岗位类型最近 ≤5 条改判样例 → few-shot 块。
    # 只进三次 LLM 调用的 payload（键名 calibration；无样例时该键整个不出现）。
    # 红线：样例内容不落 artifact 正文/markdown/推荐报告，UI 永不渲染；样例只影响
    # 判断口径，证据强约束不放宽（约束写进注入 instruction）。
    # ------------------------------------------------------------------
    from . import assessment_calibration

    calibration_examples = assessment_calibration.retrieve_examples(
        conn,
        client=str(job_item.get("client") or ""),
        job_type=assessment_calibration.normalize_job_type(job_item.get("title")),
    )
    calibration_block = assessment_calibration.build_prompt_block(calibration_examples) if calibration_examples else None

    payload = build_llm_payload(
        candidate={
            "current_company": current_company,
            "current_title": str(candidate.get("current_title") or ""),
            "education": str(candidate.get("education") or ""),
            "experience": str(candidate.get("experience") or ""),
            "full_text": resume.get("full_text") or resume.get("profile_text") or "",
            "work_text": resume.get("work_text") or "",
            "project_text": resume.get("project_text") or "",
            "education_text": resume.get("education_text") or "",
        },
        job=job_item,
        strategy_doc=strategy_doc,
        graph_hits=graph_hits,
        skill_ontology=skill_ontology,
        archetype=archetype_hit,
    )
    if calibration_block:
        payload["calibration"] = calibration_block
    raw = llm.assess_trajectory(payload)
    if not isinstance(raw, dict) or not (raw.get("trajectory") or raw.get("move_history")):
        raise LLMError("判人评估模型未返回有效结构")

    dimensions, summary, stats = normalize_llm_result(raw, corpus=corpus, graph_hits=graph_hits)

    # ------------------------------------------------------------------
    # S6-2 水平分位：参照池抽取 + 确定性落位（band 由数据算，不许模型拍）
    # ------------------------------------------------------------------
    target_years = assessment_signals.parse_experience_years(candidate.get("experience"))
    pool = assessment_signals.load_reference_pool(
        conn,
        job_id=int(job_id),
        job_title=str(job_item.get("title") or ""),
        target_years=target_years,
        exclude_job_candidate_id=int(candidate_id),
    )
    fit_score = assessment_signals.load_target_fit_score(conn, int(candidate_id))
    if fit_score is not None:
        score, basis = fit_score, "fit_score"
    else:
        score = assessment_signals.trajectory_feature_score(dimensions["trajectory"], dimensions["move_history"])
        basis = "trajectory_features"
    placement = assessment_signals.compute_placement(score, [m["fit_score"] for m in pool["members"]])

    # ------------------------------------------------------------------
    # S6-2 动机与时机：a) 简历工况信号 b) 公司近况公开信号（带来源 URL）
    # ------------------------------------------------------------------
    latest_source = conn.execute(
        "SELECT MAX(source_date) AS latest FROM source_profiles WHERE person_id=?",
        (candidate.get("person_id"),),
    ).fetchone()
    work_text = str(resume.get("work_text") or "") or corpus
    emp_signals, emp_facts = assessment_signals.employment_signals(
        work_text, latest_source_date=(latest_source["latest"] if latest_source else None), today=today
    )
    company_signals, company_stats = assessment_signals.collect_company_signals(
        current_company, fetcher=signal_fetcher, today=today
    )
    # 公开页内容不可控：信号摘要命中敏感词 → 丢弃该条信号（宁可不采，不进 artifact）
    signals: list[dict[str, Any]] = list(emp_signals)
    dropped_sensitive_signals = 0
    for signal in company_signals:
        if scan_sensitive([str(signal.get("summary") or "")]):
            dropped_sensitive_signals += 1
            continue
        signals.append(signal)
    signal_stats: dict[str, Any] = {
        "company_collection": company_stats,
        "employment_facts": {key: value for key, value in emp_facts.items() if key != "segments"},
        "dropped_sensitive_signals": dropped_sensitive_signals,
        "pm_llm": "ok",
    }
    if archetype_hit:
        # 知识飞轮二期：原型对照块注入留痕（source=kb_archetype）；无命中不记，保持现状。
        signal_stats["archetype_reference"] = {
            "matched": True,
            "source": "kb_archetype",
            "archetype_id": str(archetype_hit.get("archetype_id") or ""),
            "matched_by": str(archetype_hit.get("matched_by") or ""),
            "match_reason": str(archetype_hit.get("match_reason") or ""),
        }

    # LLM 第二调：只把 band/信号读成顾问口径 verdict；失败降级确定性模板（不阻断）
    pm_payload = {
        "task": "判人评估：在同龄人里的位置 + 动机与时机（S6-2）",
        "candidate": {
            "current_company": current_company,
            "current_title": str(candidate.get("current_title") or ""),
            "experience": str(candidate.get("experience") or ""),
        },
        "job": {"client": str(job_item.get("client") or ""), "title": str(job_item.get("title") or "")},
        "trajectory_brief": {
            "promotion_pace": dimensions["trajectory"].get("promotion_pace"),
            "tech_evolution": dimensions["trajectory"].get("tech_evolution"),
            "moves": [str(move.get("direction") or "") for move in dimensions["move_history"].get("moves") or []],
        },
        "percentile": {
            "band": placement.get("band"),
            "score": placement.get("score"),
            "basis": basis,
            "percentile_rank": placement.get("percentile_rank"),
            "reference": {
                "direction": pool["direction"],
                "years_window": pool["years_window"],
                "n": placement.get("n"),
                "median": placement.get("median"),
                "q25": placement.get("q25"),
                "q75": placement.get("q75"),
                "sample_sufficient": placement.get("sample_sufficient"),
            },
        },
        "employment_facts": signal_stats["employment_facts"],
        "signals": signals,
    }
    if calibration_block:
        pm_payload["calibration"] = calibration_block
    try:
        raw_pm = llm.assess_percentile_motivation(pm_payload)
        if not isinstance(raw_pm, dict):
            raise LLMError("分位/动机模型输出非 JSON 对象")
    except LLMError:
        llm.mark_last_call_fallback()
        raw_pm = None
        signal_stats["pm_llm"] = "fallback_template"
    percentile_dim, motivation_dim = build_s62_dimensions(
        raw_pm,
        corpus=corpus,
        placement=placement,
        basis=basis,
        direction=pool["direction"],
        years_window=pool["years_window"],
        signals=signals,
        stats=stats,
    )
    dimensions["percentile"] = percentile_dim
    dimensions["motivation"] = motivation_dim

    # ------------------------------------------------------------------
    # S7-2 雷达联动：候选人现职公司在最新雷达榜单有未过期信号 → 追加 motivation 证据
    #（type="雷达信号"，带 as_of + 来源链接，文案标注"推测"；confidence 封顶 inferred）。
    # 读取侧只追加一次雷达信号查询调用，不改既有生成逻辑；无榜单/无信号时行为与之前一致。
    # ------------------------------------------------------------------
    from . import radar_scan

    radar_signals = radar_scan.radar_signals_for_company(conn, current_company, today=today)
    radar_injected = 0
    motivation_dim = dimensions["motivation"]
    if radar_signals and isinstance(motivation_dim, dict):
        for signal in radar_signals[:2]:
            summary = str(signal.get("summary") or "").strip()
            # 与公开信号同口径：摘要命中敏感词的整条不采（宁可不注，不进 artifact）
            if not summary or scan_sensitive([summary]):
                continue
            urls = "；".join(str(url) for url in signal.get("source_urls") or [] if str(url or "").strip())
            if not urls:
                continue  # 红线：雷达信号作证据必须带来源链接
            ref = f"{summary}（{signal.get('as_of')}，推测）｜来源：{urls}"
            merged = _merge_evidence(motivation_dim.get("evidence") or [], [{"type": "雷达信号", "ref": ref}], limit=10)
            motivation_dim["evidence"] = merged
            radar_injected += 1
        if radar_injected:
            # 雷达信号是公开信息推测：注入后 confidence 封顶 inferred（无论信号自报置信度）
            motivation_dim["confidence"] = "inferred"
    signal_stats["radar_signals"] = {"matched": len(radar_signals), "injected": radar_injected}

    # ------------------------------------------------------------------
    # S6-3 风险点（需要核实的问题）：确定性检出（gap/频繁跳动/时间线冲突/硬条件差距）
    # + LLM 第三调只补 title 通胀/过度包装两类语义项（证据过同一道逐字闸）
    # ------------------------------------------------------------------
    risk_facts = assessment_signals.collect_risk_facts(
        work_text,
        corpus=corpus,
        hard_text=str(job_item.get("hard_requirements") or ""),
        education_text=" ".join(
            part for part in (str(candidate.get("education") or ""), str(resume.get("education_text") or "")) if part
        ),
        experience_text=str(candidate.get("experience") or ""),
        today=today,
    )
    risks_payload = {
        "task": "判人评估：需要核实的问题（S6-3，仅 title 通胀/过度包装两类语义项）",
        "candidate": {
            "current_company": current_company,
            "current_title": str(candidate.get("current_title") or ""),
            "experience": str(candidate.get("experience") or ""),
        },
        "job": {
            "client": str(job_item.get("client") or ""),
            "title": str(job_item.get("title") or ""),
            "hard_requirements": _clean_text(job_item.get("hard_requirements"), 400),
        },
        "resume_full_text": str(resume.get("full_text") or resume.get("profile_text") or "")[:6000],
        "resume_work_text": str(resume.get("work_text") or "")[:4000],
        "deterministic_findings": [item["risk"] for item in risk_facts["items"]],
    }
    if calibration_block:
        risks_payload["calibration"] = calibration_block
    try:
        raw_risks = llm.assess_risks(risks_payload)
        if not isinstance(raw_risks, dict):
            raise LLMError("风险点模型输出非 JSON 对象")
        signal_stats["risks_llm"] = "ok"
    except LLMError:
        llm.mark_last_call_fallback()
        raw_risks = None
        signal_stats["risks_llm"] = "fallback_deterministic"
    dimensions["risks"] = build_risks_dimension(
        raw_risks,
        deterministic_items=risk_facts["items"],
        checks_run=risk_facts["checks_run"],
        corpus=corpus,
        graph_hits=graph_hits,
        stats=stats,
    )
    signal_stats["risk_facts"] = risk_facts["facts"]

    masked = mask_name(candidate.get("display_name")) if callable(mask_name) else str(candidate.get("display_name") or "")
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": int(candidate_id),
        "job_id": int(job_id),
        "candidate_name_masked": masked,
        "job_title": str(job_item.get("title") or ""),
        "client": str(job_item.get("client") or ""),
        "strategy_ref": strategy_ref,
        "as_of": _now(),
        "assessor_version": ASSESSOR_VERSION,
        "model": str(getattr(llm, "model", "unknown")),
        "dimensions": dimensions,
        "consultant_summary": summary,
        "advisor_action": "pending",
        "advisor_note": "",
        "evidence_stats": {
            "kept": stats["kept"],
            "stripped": stats["stripped"],
            "stripped_detail": stats["stripped_detail"],
        },
        "signal_stats": signal_stats,
        # S6-4：只记注入计数与样例 id（内部留痕）；样例内容（改判 note/机器原判）
        # 永不进 artifact 正文/markdown/推荐报告，UI 无内容可渲染（红线：注入不外泄）。
        "calibration_stats": {
            "samples_injected": len(calibration_examples),
            "sample_ids": [int(example["sample_id"]) for example in calibration_examples],
        },
    }

    # 硬闸 1：决策禁语（拒写 + 扫描日志）
    texts = generated_texts(doc)
    banned = scan_banned_decision(texts)
    # 硬闸 2：敏感属性负向扫描（生成文本命中 → 拒写 + 扫描日志）
    sensitive_hits = scan_sensitive(texts)
    # 简历逐字引用里的敏感词：引用即事实原文，但作为评估输出仍须剥离，不算拒写因子。
    kept_sensitives: list[dict[str, str]] = []
    for name in DIMENSIONS_IMPLEMENTED:
        dim = doc["dimensions"][name]
        if name == "risks":
            # 风险点为 item 级证据：逐 item 剥离敏感简历引用；LLM 语义项证据归零 → 整条丢弃
            # （确定性 absence 型条目本就没证据，保留）；随后重算 verdict/evidence/confidence。
            surviving_items: list[dict[str, Any]] = []
            for risk_item in dim.get("items") or []:
                if not isinstance(risk_item, dict):
                    continue
                kept_item_evidence: list[dict[str, str]] = []
                for item in risk_item.get("evidence") or []:
                    if item.get("type") == "简历" and scan_sensitive([item.get("ref") or ""]):
                        doc["evidence_stats"]["stripped"] += 1
                        doc["evidence_stats"]["stripped_detail"].append(
                            {"type": "简历", "ref": str(item.get("ref") or "")[:120], "reason": "简历引用含敏感属性表述，剥离"}
                        )
                        kept_sensitives.append(item)
                        continue
                    kept_item_evidence.append(item)
                risk_item["evidence"] = kept_item_evidence
                if not kept_item_evidence and risk_item.get("kind") in _RISK_LLM_KINDS:
                    doc["evidence_stats"]["stripped_detail"].append(
                        {"type": "风险项", "ref": str(risk_item.get("risk") or "")[:80], "reason": "风险项证据因敏感剥离归零，整条丢弃"}
                    )
                    continue
                surviving_items.append(risk_item)
            dim["items"] = surviving_items
            refresh_risks_dimension(dim)
            continue
        kept: list[dict[str, str]] = []
        for item in dim.get("evidence") or []:
            if item.get("type") == "简历" and scan_sensitive([item.get("ref") or ""]):
                doc["evidence_stats"]["stripped"] += 1
                doc["evidence_stats"]["stripped_detail"].append(
                    {"type": "简历", "ref": str(item.get("ref") or "")[:120], "reason": "简历引用含敏感属性表述，剥离"}
                )
                kept_sensitives.append(item)
                continue
            kept.append(item)
        dim["evidence"] = kept
        if not kept:
            dim["confidence"] = "inferred"
    if banned or sensitive_hits:
        hits = [{"category": "决策禁语", "hit": hit, "text": ""} for hit in banned] + sensitive_hits
        log_scan_block(
            conn,
            candidate_id=candidate_id,
            job_id=job_id,
            person_id=candidate.get("person_id"),
            hits=hits,
        )
        # 扫描日志是拒写留痕，必须独立于评估写入提交（artifact 本身不落库）。
        conn.commit()
        categories = "、".join(sorted({hit["category"] for hit in hits}))
        raise ValueError(f"判人评估输出命中{categories}类敏感表述/决策禁语，已拒写并记扫描日志")
    doc["evidence_stats"]["kept"] = sum(len(doc["dimensions"][name]["evidence"]) for name in DIMENSIONS_IMPLEMENTED)
    return doc
