"""S6-1：判人评估器 —— candidate_assessment artifact（职业轨迹 + 跳槽质量史）。

口径来源：docs/TASKCARD_S6-1_判人评估器_轨迹与跳槽史_20260724.md + PRD §2/§3/§5。

架构（LLM 角色 vs 确定性校验）：
- LLM 只做"资深顾问式"判断（verdict/segments/moves/summary），输入为简历原文 + strategy_v2
  + 确定性预匹配的图谱命中（graph_hits）。
- 写入前必过确定性校验层（不过闸不落库）：
  1) 证据强约束：type=简历 的 ref 必须逐字存在于该候选人语料（原文包含校验，失败剥离）；
     type=图谱 的 ref 必须解析到本评估实际命中的图谱条目（公司名规范化匹配，失败剥离）；
     本期无知识库/公开信息证据源，其他 type 一律剥离。某维 evidence 归零 → confidence 强制 inferred。
  2) 敏感属性负向扫描：verdict/summary/segments/moves 等 LLM 生成文本命中年龄/性别/婚育/户籍
     词表 → 整条拒写（ValueError）并记扫描日志（candidate_events）；简历逐字引用命中 → 剥离该条。
  3) 决策字眼拦截："建议淘汰/不建议推荐"类 → 拒写。
  4) 图谱未命中的公司 tier_source 一律强制 inferred（不瞎编）。

红线：评估只辅助不决策；评估数据不出本机；同人同岗幂等（重复生成更新原行，as_of 刷新）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from . import knowledge_base
from .llm import BaseLLM, LLMError

ARTIFACT_TYPE = "candidate_assessment"
SCHEMA_VERSION = "assessment_v1"
ASSESSOR_VERSION = "s6-trajectory-v1"

DIMENSIONS_IMPLEMENTED = ("trajectory", "move_history")
DIMENSIONS_PLACEHOLDER = ("percentile", "motivation", "risks")

_CONFIDENCE = {"certain", "inferred"}
_TIER = {"T1", "T2", "T3", "unknown"}
_TIER_SOURCE = {"graph", "inferred"}
_PACE = {"fast", "normal", "slow", "unknown"}
_EVOLUTION = {"rising", "lateral", "stagnant", "unknown"}
_DIRECTION = {"up", "lateral", "down"}
_MOVE_UNKNOWN = {"up", "lateral", "down", "unknown"}
_EVIDENCE_TYPES = {"简历", "图谱"}

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
    """收集 artifact 中全部 LLM 生成文本（verdict/summary/segments/moves 描述字段）。"""
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
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """校验一组 evidence；返回 (保留, 剥离)，剥离含原因，全部留痕。"""
    kept: list[dict[str, str]] = []
    stripped: list[dict[str, str]] = []
    items = evidence if isinstance(evidence, list) else []
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
        else:
            stripped.append({"type": etype or "未标注", "ref": ref[:120], "reason": "本期证据类型仅支持 简历/图谱"})
    return kept, stripped


# ---------------------------------------------------------------------------
# 图谱命中（确定性）：候选人雇主公司 → 图谱条目
# ---------------------------------------------------------------------------

def match_graph_hits(companies: list[str], graph: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把简历里的雇主公司名规范化后命中图谱 key；命中即真实条目（含名称/赛道/主营业务/分类）。"""
    hits: list[dict[str, Any]] = []
    used: set[str] = set()
    for company in companies:
        raw = " ".join(str(company or "").split())
        norm = knowledge_base.normalize_client_name(company)
        if not norm:
            continue
        for name, info in (graph or {}).items():
            if name in used:
                continue
            rule, _reason = knowledge_base.name_match_rule(raw, norm, name)
            if rule:
                hits.append(
                    {
                        "company": raw,
                        "graph_name": name,
                        "track": str(info.get("track") or ""),
                        "business": str(info.get("business") or ""),
                        "categories": list(info.get("categories") or []),
                    }
                )
                used.add(name)
                break
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

    for name in DIMENSIONS_PLACEHOLDER:
        dimensions[name] = None  # S6-2/3 填充，本期留空占位

    summary = " ".join(str(raw.get("consultant_summary") or "").split())[:600]
    return dimensions, summary, stats


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
        elif not evidence and dim.get("confidence") == "certain":
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
    for name in DIMENSIONS_PLACEHOLDER:
        if dimensions.get(name) is not None:
            errors.append(f"dimensions.{name} 本期必须为 null 占位")
    if doc.get("advisor_action") not in ("pending", "accepted", "modified", "rejected"):
        errors.append("advisor_action 必须是 pending|accepted|modified|rejected")
    return errors


def build_llm_payload(
    *,
    candidate: dict[str, Any],
    job: dict[str, Any],
    strategy_doc: dict[str, Any] | None,
    graph_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """组装 LLM 输入：简历原文 + 岗位/策略要点 + 确定性图谱命中（只读事实）。"""
    strategy: dict[str, Any] = {}
    if strategy_doc:
        step1 = strategy_doc.get("step1_job_essence") if isinstance(strategy_doc.get("step1_job_essence"), dict) else {}
        step3 = strategy_doc.get("step3_level_mapping") if isinstance(strategy_doc.get("step3_level_mapping"), dict) else {}
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
            "target_pool": pool[:20],
        }
    return {
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
    summary = (
        f"生成判人评估（职业轨迹/跳槽质量史）：置信度 "
        f"{trajectory.get('confidence')}/{move_history.get('confidence')}，"
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
) -> dict[str, Any]:
    """生成判人评估（不落库）：返回 doc；敏感扫描命中抛 ValueError（含 scan_blocked 标记）。

    LookupError：candidate/job 不存在或不匹配；ValueError：无简历语料 / 扫描命中；
    LLMError：模型不可用或输出非法（调用方映射 409）。
    """
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
    employers = extract_employers(corpus)
    current_company = str(candidate.get("current_company") or "").strip()
    if current_company and current_company not in employers:
        employers.insert(0, current_company)
    graph_hits = match_graph_hits(employers, graph)

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
    )
    raw = llm.assess_trajectory(payload)
    if not isinstance(raw, dict) or not (raw.get("trajectory") or raw.get("move_history")):
        raise LLMError("判人评估模型未返回有效结构")

    dimensions, summary, stats = normalize_llm_result(raw, corpus=corpus, graph_hits=graph_hits)

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
