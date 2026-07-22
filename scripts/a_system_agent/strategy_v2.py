"""S4-1：寻访策略输入分级（L1/L2/L3）、岗位原型匹配与 strategy_v2 schema。

口径来源（事实源，运行时只读）：
- docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md §1（输入分级/L3 提问规则）与 §2（strategy_v2 schema）
- docs/ASA_sourcing_strategy_capability_2026-07-23.md §0.1（四锚点）与 §1.1-1.2
- knowledge_base/seed_silan_tme_v1.json（岗位原型 tme_computing_power）

边界：本模块只读取知识库 JSON 的 job_archetype/校准字段；S4-2 的客户画像/公司图谱/
restricted 层统一由 knowledge_base 模块读取（restricted 仅白名单出库），以参数形式传入
build_strategy_v2 组装，本模块不直接触碰 restricted 文件。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


STRATEGY_V2_VERSION = "strategy_v2"

# 规格 §0.1 四锚点（键 → 中文名）
ANCHOR_KEYS = ("customer_of_customer", "product_tech_line", "competitive_landscape", "scenario_track")
ANCHOR_LABELS = {
    "customer_of_customer": "客户的客户",
    "product_tech_line": "产品/技术线",
    "competitive_landscape": "竞争格局（对标友商）",
    "scenario_track": "场景/赛道",
}

STRATEGY_V2_REQUIRED_KEYS = (
    "schema_version",
    "input_level",
    "step1_job_essence",
    "step2_target_pool",
    "step3_level_mapping",
    "step4_keyword_groups",
    "step5_expectation",
    "negative_rules",
    "consultant_edits",
)

_POOL_SOURCES = {"client_doc", "kb_graph", "kb_profile", "llm_inferred"}
_POOL_PATHS = {"same_layer", "reverse", "adjacent"}
_POOL_TIERS = {"T1", "T2", "T3"}
_CONFIDENCES = {"high", "medium", "low"}

_DEFAULT_KB_DIR = Path("/Users/messi/Documents/ASA/knowledge_base")

# 场景/赛道信号词（用于从 JD/画像文本中识别 scenario_track 锚点）
_SCENARIO_TOKENS = (
    "服务器", "PC", "ADAS", "车载", "汽车", "车企", "半导体", "消费电子", "医疗",
    "工业", "新能源", "AI", "算力", "通信", "光伏", "储能", "机器人",
)

# 顾问“直接搜/先搜”类确认（PRD §1：顾问说“直接搜”才允许带 inferred 标记继续）
_OVERRIDE_PATTERN = re.compile(
    r"^(?:可以|确认|现在|那就|那)?(?:直接|先|按推断|就按推断|照常)?(?:开始|继续|重新|执行)?"
    r"(?:搜索|搜|寻访|找人|找吧|找)(?:吧|了|就行|可以|哈)?$",
    re.I,
)

# 顾问回复看上去像锚点回答（而非新指令）的启发信号
_ANSWER_HINTS = (
    "客户", "终端", "友商", "对标", "目标公司", "产品", "工艺", "产线", "代际",
    "禁挖", "竞业", "限制", "学历", "场景", "赛道", "方向", "名单", "只看", "排除",
)


def knowledge_base_dir() -> Path:
    """知识库目录：环境变量优先，缺省为 ASA 仓的 knowledge_base。运行时只读。"""
    raw = os.environ.get("ASA_KNOWLEDGE_BASE_DIR", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_KB_DIR


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def load_job_archetypes(kb_dir: str | Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """读取知识库 seed_*.json 的 job_archetype。

    文件缺失/解析失败一律按“无原型”处理并留痕（不抛异常、不写文件）。
    只提取策略生成所需字段，不触碰 restricted 层。
    """
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    trace: list[str] = []
    archetypes: list[dict[str, Any]] = []
    if not directory.is_dir():
        return [], [f"知识库目录不存在：{directory}，按无岗位原型处理"]
    for path in sorted(directory.glob("seed_*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            trace.append(f"{path.name} 解析失败（{exc.__class__.__name__}），按无岗位原型处理")
            continue
        raw = doc.get("job_archetype") if isinstance(doc, dict) else None
        if not isinstance(raw, dict) or not raw.get("archetype_id"):
            continue
        archetypes.append(
            {
                "archetype_id": str(raw.get("archetype_id") or ""),
                "title": str(raw.get("title") or ""),
                "client": str(raw.get("client") or ""),
                "essence": str(raw.get("essence") or ""),
                "directions": raw.get("directions") if isinstance(raw.get("directions"), list) else [],
                "target_functions": raw.get("target_functions") if isinstance(raw.get("target_functions"), list) else [],
                "location_policy": str(raw.get("location_policy") or ""),
                "level_mapping": doc.get("level_mapping") if isinstance(doc.get("level_mapping"), dict) else {},
                "keyword_groups": doc.get("keyword_groups") if isinstance(doc.get("keyword_groups"), list) else [],
                "negative_rules": doc.get("negative_rules") if isinstance(doc.get("negative_rules"), list) else [],
                "target_company_pool": doc.get("target_company_pool") if isinstance(doc.get("target_company_pool"), dict) else {},
                "source_file": path.name,
            }
        )
        trace.append(f"已加载岗位原型 {raw.get('archetype_id')}（{path.name}）")
    if not archetypes and not trace:
        trace.append(f"知识库目录 {directory} 无 seed_*.json 岗位原型，按无原型处理")
    return archetypes, trace


# 原型匹配规则（S4-1 最小实现，可解释；S4-2 扩展）。标题命中任一关键词即视为命中原型。
_ARCHETYPE_TITLE_TOKENS: dict[str, tuple[str, ...]] = {
    "tme_computing_power": ("技术市场", "tme", "technical marketing"),
}


def match_job_archetype(
    client: str,
    title: str,
    archetypes: list[dict[str, Any]] | None = None,
    *,
    kb_dir: str | Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """可解释的岗位原型匹配：返回 (命中的原型或 None, 留痕)。

    规则：
    1. 标题命中原型职能关键词（如 技术市场/TME/technical marketing，大小写不敏感）→ 命中；
    2. 客户与原型客户一致且标题含“市场”→ 命中（客户内市场岗兜底）。
    """
    if archetypes is None:
        archetypes, load_trace = load_job_archetypes(kb_dir)
    else:
        load_trace = []
    trace = list(load_trace)
    normalized_title = str(title or "").lower()
    normalized_client = " ".join(str(client or "").split())
    for archetype in archetypes:
        archetype_id = str(archetype.get("archetype_id") or "")
        tokens = _ARCHETYPE_TITLE_TOKENS.get(archetype_id, ())
        hit_token = next((token for token in tokens if token.lower() in normalized_title), "")
        if hit_token:
            trace.append(f"岗位标题“{title}”命中原型 {archetype_id} 职能关键词“{hit_token}”")
            return {**archetype, "matched_by": "title_token", "match_reason": f"标题命中原型职能关键词：{hit_token}"}, trace
        archetype_client = str(archetype.get("client") or "")
        if archetype_client and normalized_client == archetype_client and "市场" in str(title or ""):
            trace.append(f"客户“{client}”+市场职能命中原型 {archetype_id}")
            return {**archetype, "matched_by": "client_function", "match_reason": "客户一致且为市场岗"}, trace
    trace.append(f"岗位“{normalized_client}{title}”未命中任何岗位原型")
    return None, trace


def _split_terms(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[、；;，,/｜|\n]+", str(value or ""))
    return [str(item).strip() for item in items if str(item or "").strip()]


def _extract_labeled(text: str, labels: tuple[str, ...]) -> list[str]:
    """从文本中抽取“标签：值”结构的值（如 主要客户：…／目标友商：…）。"""
    values: list[str] = []
    for label in labels:
        for match in re.finditer(rf"{label}[：:]\s*([^\n；;。]+)", text):
            values.extend(_split_terms(match.group(1)))
    return list(dict.fromkeys(values))


def classify_strategy_input(
    job: dict[str, Any],
    *,
    archetype: dict[str, Any] | None = None,
    consultant_answers: str = "",
) -> dict[str, Any]:
    """策略生成前的输入定级（L1/L2/L3）与四锚点盘点，全部留痕。

    判定口径（PRD §1 / 方案 §1.1）：
    - L1：有客户一手锚点资料（岗位工作簿/需求梳理，如 source_layer 命中 workbook/需求梳理，
      或岗位详情中存在“主要客户/目标友商/重点产品”类客户原文标签）；
    - L2：结构化 JD + 顾问补充（无客户一手资料，但有 position_profiles 顾问画像或顾问当场补充）；
    - L3：只有 JD。
    """
    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    trace: list[str] = []

    source_layer = str(job.get("source_layer") or "")
    jd_text = "\n".join(
        str(job.get(key) or "")
        for key in ("title", "summary", "hard_requirements", "ability_keywords", "target_companies", "exclusions", "responsibilities", "requirements")
    )
    client_doc_labels = ("主要客户", "目标客户", "客户名", "目标友商", "友商", "重点产品")
    has_client_doc_labels = any(f"{label}：" in jd_text or f"{label}:" in jd_text for label in client_doc_labels)
    workbook_source = bool(re.search(r"workbook|需求梳理|梳理表|项目管理表", source_layer, re.I))
    client_doc = workbook_source or has_client_doc_labels
    has_profile = bool(profile) and any(
        _loads(profile.get(key), [])
        for key in ("hard_requirements_json", "ability_keywords_json", "target_companies_json", "search_keywords_json")
    )
    consultant_supplement = bool(str(consultant_answers or "").strip())

    if client_doc:
        input_level = "L1"
        trace.append(f"检测到客户一手锚点资料（source_layer={source_layer or '岗位详情客户原文标签'}）→ 定级 L1")
    elif has_profile or consultant_supplement:
        input_level = "L2"
        basis = "岗位画像（顾问整理补充）" if has_profile else "顾问当场补充"
        trace.append(f"无客户一手资料，存在{basis} → 定级 L2")
    else:
        input_level = "L3"
        trace.append("只有 JD，无客户一手资料与顾问补充 → 定级 L3")

    answers_text = str(consultant_answers or "")

    def from_answers(labels: tuple[str, ...]) -> list[str]:
        return _extract_labeled(answers_text, labels) if answers_text else []

    anchors: dict[str, dict[str, Any]] = {}

    # 锚点 1：客户的客户
    customers = _extract_labeled(jd_text, ("主要客户", "目标客户", "客户名", "终端客户"))
    customer_source = "client_doc" if customers else ""
    if not customers and answers_text:
        customers = from_answers(("客户的客户", "客户", "终端"))
        customer_source = "consultant" if customers else ""
    anchors["customer_of_customer"] = {
        "present": bool(customers), "values": customers[:12],
        "source": customer_source or "missing", "inferred": False, "confidence": "",
    }

    # 锚点 2：产品/技术线
    product_terms = _split_terms(job.get("ability_keywords")) + _extract_labeled(jd_text, ("重点产品", "产品", "技术"))
    product_source = ""
    if _extract_labeled(jd_text, ("重点产品",)):
        product_source = "client_doc"
    elif product_terms:
        product_source = "jd"
    if not product_terms:
        product_terms = _loads(profile.get("ability_keywords_json"), [])
        product_source = "consultant" if product_terms else ""
    if not product_terms and answers_text:
        product_terms = from_answers(("产品", "技术", "产线", "工艺"))
        product_source = "consultant" if product_terms else ""
    anchors["product_tech_line"] = {
        "present": bool(product_terms), "values": list(dict.fromkeys(product_terms))[:16],
        "source": product_source or "missing", "inferred": False, "confidence": "",
    }

    # 锚点 3：竞争格局（对标友商/目标公司池）
    competitors = _extract_labeled(jd_text, ("目标友商", "友商", "对标"))
    competitor_source = "client_doc" if competitors else ""
    if not competitors:
        competitors = _split_terms(job.get("target_companies")) + _loads(profile.get("target_companies_json"), [])
        if competitors:
            competitor_source = "jd" if _split_terms(job.get("target_companies")) else "consultant"
    if not competitors and answers_text:
        competitors = from_answers(("目标友商", "友商", "对标", "目标公司"))
        competitor_source = "consultant" if competitors else ""
    anchors["competitive_landscape"] = {
        "present": bool(competitors), "values": list(dict.fromkeys(competitors))[:16],
        "source": competitor_source or "missing", "inferred": False, "confidence": "",
    }

    # 锚点 4：场景/赛道
    corpus = jd_text + "\n" + answers_text
    scenario_hits = [token for token in _SCENARIO_TOKENS if token in corpus]
    soft_prefs = _split_terms(_loads(profile.get("soft_preferences_json"), []))
    scenario_values = list(dict.fromkeys([*scenario_hits, *soft_prefs]))
    scenario_source = ""
    if scenario_hits:
        scenario_source = "client_doc" if client_doc else "jd"
    elif scenario_values:
        scenario_source = "consultant"
    anchors["scenario_track"] = {
        "present": bool(scenario_values), "values": scenario_values[:10],
        "source": scenario_source or "missing", "inferred": False, "confidence": "",
    }

    # 知识库原型补齐：命中原型的锚点按 inferred 标记填入（PRD §1 L3 规则）
    if archetype:
        kb_fill = {
            "customer_of_customer": [
                str(customer)
                for direction in archetype.get("directions") or []
                for customer in (direction.get("customers") or [])
            ],
            "product_tech_line": [
                str(product)
                for direction in archetype.get("directions") or []
                for product in (direction.get("products") or [])
            ],
            "competitive_landscape": [
                str(competitor)
                for direction in archetype.get("directions") or []
                for competitor in (direction.get("competitors") or [])
            ],
            "scenario_track": [str(direction.get("name") or "") for direction in archetype.get("directions") or []],
        }
        for key, values in kb_fill.items():
            values = [value for value in dict.fromkeys(values) if value]
            if values and not anchors[key]["present"]:
                anchors[key] = {
                    "present": True, "values": values[:16],
                    "source": "kb_archetype", "inferred": True, "confidence": "medium",
                }
                trace.append(f"锚点“{ANCHOR_LABELS[key]}”由岗位原型 {archetype.get('archetype_id')} 推断补齐（inferred, confidence=medium）")

    missing = [key for key in ANCHOR_KEYS if not anchors[key]["present"]]
    for key in ANCHOR_KEYS:
        state = "有" if anchors[key]["present"] else "缺"
        trace.append(f"锚点“{ANCHOR_LABELS[key]}”：{state}（来源 {anchors[key]['source']}）")
    trace.append(f"四锚点缺失 {len(missing)} 项" + (f"：{'、'.join(ANCHOR_LABELS[key] for key in missing)}" if missing else ""))

    return {
        "input_level": input_level,
        "anchors": anchors,
        "missing_anchors": missing,
        "has_client_doc": client_doc,
        "has_consultant_supplement": consultant_supplement,
        "archetype_id": str(archetype.get("archetype_id") or "") if archetype else "",
        "trace": trace,
    }


# PRD §1 四问模板（按四锚点 + 禁挖/竞业），按缺失锚点实例化；禁挖/竞业顾问必答。
def build_anchor_questions(job: dict[str, Any], classification: dict[str, Any]) -> list[str]:
    client = str(job.get("client") or "该客户")
    title = str(job.get("title") or "该岗位")
    missing = set(classification.get("missing_anchors") or [])
    questions: list[str] = []
    if "customer_of_customer" in missing:
        questions.append(f"这个岗位“客户的客户”是谁？（{client}{title} 服务什么客户/终端？）")
    if "competitive_landscape" in missing:
        questions.append("对标友商/目标公司您有名单吗？")
    if "product_tech_line" in missing or "scenario_track" in missing:
        questions.append("产品/产线/工艺代际有没有硬过滤？（如“只看12寸”、限定某场景赛道）")
    questions.append("有没有禁挖名单/竞业限制/背景限制？")
    return questions


def build_clarification_answer(job: dict[str, Any], classification: dict[str, Any], *, floating_compact: bool = False) -> str:
    """L3 提问清单文案（PRD §1：不得直接执行外部寻访，先出提问清单）。"""
    questions = build_anchor_questions(job, classification)
    missing_labels = "、".join(ANCHOR_LABELS[key] for key in classification.get("missing_anchors") or [])
    known = [
        f"{ANCHOR_LABELS[key]}={'、'.join((classification['anchors'][key].get('values') or [])[:4])}"
        for key in ANCHOR_KEYS
        if classification.get("anchors", {}).get(key, {}).get("present")
    ]
    question_lines = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    if floating_compact:
        answer = (
            f"结论：该岗位四锚点缺失 {len(classification.get('missing_anchors') or [])} 项（{missing_labels}），"
            "知识库无对应岗位原型，暂不创建寻访工作流。\n\n"
            f"下一步：请回答以下 {len(questions)} 个问题，或回复“直接搜”按推断先行搜索（推断项会标记待确认）：\n{question_lines}"
        )
    else:
        answer = (
            f"结论：当前岗位输入定级为 {classification.get('input_level')}，"
            f"四锚点缺失 {len(classification.get('missing_anchors') or [])} 项（{missing_labels}），"
            "且知识库无对应岗位原型。按寻访策略规范，此时不创建寻访工作流、不执行任何外部搜索。\n\n"
            f"下一步：请补充以下锚点信息；也可以回复“直接搜”，ASA 会按知识库推断先行生成策略，"
            f"所有推断项标记“待确认”并记录顾问放行：\n{question_lines}"
        )
    if known:
        answer += f"\n\n已掌握锚点（无需重复）：{'；'.join(known)}。"
    return answer


def is_direct_search_override(message: str) -> bool:
    """顾问“直接搜/先搜”类放行确认。"""
    return bool(_OVERRIDE_PATTERN.match(" ".join(str(message or "").split())))


def looks_like_anchor_answer(message: str) -> bool:
    """启发判断：回复像锚点补充（含“锚点标签：值”结构或多个锚点信号词），而非新指令/闲聊。"""
    text = " ".join(str(message or "").split())
    if len(text) < 4:
        return False
    if re.search(r"(客户的客户|终端|友商|对标|目标公司|产品|产线|工艺|代际|禁挖|竞业|场景|赛道|名单)[：:]", text):
        return True
    hits = sum(1 for token in _ANSWER_HINTS if token in text)
    return hits >= 2


def _pool_entry(path: str, tier: str, companies: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
    return {"path": path, "tier": tier, "companies": companies, "rationale": rationale}


def _kb_pool(archetype: dict[str, Any]) -> list[dict[str, Any]]:
    """从命中的岗位原型构建 step2 目标池（来源 kb_profile，顾问校准置信度）。"""
    pool = archetype.get("target_company_pool") or {}
    entries: list[dict[str, Any]] = []
    mapping = (
        ("T1_competitor_device", "same_layer", "T1", "high"),
        ("T2_customer_OEM", "reverse", "T2", "high"),
        ("T3_adjacent_unconfirmed", "adjacent", "T3", "medium"),
    )
    for key, path, tier, confidence in mapping:
        block = pool.get(key) or {}
        companies = [
            {"name": str(company.get("name") or ""), "source": "kb_profile", "confidence": confidence}
            for company in block.get("companies") or []
            if str(company.get("name") or "").strip()
        ]
        if companies:
            entries.append(_pool_entry(path, tier, companies, str(block.get("rationale") or "")))
    return entries


def build_strategy_v2(
    plan: dict[str, Any],
    classification: dict[str, Any],
    *,
    archetype: dict[str, Any] | None = None,
    consultant: dict[str, Any] | None = None,
    llm_fragment: dict[str, Any] | None = None,
    profile_match: dict[str, Any] | None = None,
    graph_pool: list[dict[str, Any]] | None = None,
    restricted_rules: list[dict[str, Any]] | None = None,
    negative_checklist: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装 strategy_v2：LLM 填充各步，运行时兜底必备键并强制版本号/定级/顾问留痕。

    consultant_override=True 时，所有推断锚点保持 inferred:true + confidence（PRD §1）。
    S4-2：profile_match（客户画像挂载留痕）、graph_pool（kb_graph 公司，只用于召回排序）、
    restricted_rules（禁挖/竞业白名单，source=restricted_client）由运行时传入并合。
    S4-3：negative_checklist（排除规则引擎五类清单，negative_rules 模块输出，逐项含
    applicable/rule/basis/source）由运行时传入，强制逐类留痕并入 negative_rules。
    """
    consultant = consultant or {}
    fragment = llm_fragment if isinstance(llm_fragment, dict) else {}
    override = bool(consultant.get("consultant_override"))
    assembly_trace: list[str] = []

    fallback_essence = str(
        (archetype or {}).get("essence") or plan.get("strategy_summary") or "围绕岗位硬门槛、应用场景与目标公司分层寻访。"
    )
    step1 = fragment.get("step1_job_essence") if isinstance(fragment.get("step1_job_essence"), dict) else {}
    step1 = {
        "statement": str(step1.get("statement") or fallback_essence),
        "value_chain_role": str(step1.get("value_chain_role") or (archetype or {}).get("title") or ""),
        "confirmed_by": str(step1.get("confirmed_by") or ("consultant" if archetype else "inferred")),
    }

    step2_raw = fragment.get("step2_target_pool")
    step2: list[dict[str, Any]] = []
    if isinstance(step2_raw, list):
        for entry in step2_raw:
            if not isinstance(entry, dict):
                continue
            companies = [
                {
                    "name": str(company.get("name") or ""),
                    "source": str(company.get("source") or "llm_inferred"),
                    "confidence": str(company.get("confidence") or "low"),
                }
                for company in entry.get("companies") or []
                if isinstance(company, dict) and str(company.get("name") or "").strip()
            ]
            if companies:
                rationale = str(entry.get("rationale") or "")
                # llm_inferred 公司必须始终标“待确认”（PRD §3.2）
                if any(company["source"] == "llm_inferred" for company in companies) and "待确认" not in rationale:
                    rationale = f"{rationale}（含 llm_inferred 公司，待确认）" if rationale else "含 llm_inferred 公司，待确认"
                step2.append(
                    _pool_entry(
                        str(entry.get("path") or "same_layer"), str(entry.get("tier") or "T2"),
                        companies, rationale,
                    )
                )
    if not step2 and archetype:
        step2 = _kb_pool(archetype)
    if not step2:
        companies = [
            {"name": name, "source": "llm_inferred", "confidence": "low"}
            for name in dict.fromkeys(_split_terms(plan.get("target_companies")))
        ]
        if companies:
            step2 = [
                _pool_entry(
                    "same_layer", "T2", companies,
                    "由模型/岗位画像推导的目标池，全部按待确认处理" + ("（顾问放行直接搜）" if override else ""),
                )
            ]

    # S4-2：公司图谱池并入 step2（source=kb_graph + confidence，按公司名去重）。
    # governance：图谱命中只用于召回与排序，必须回候选人详情核验本人证据。
    graph_companies = [
        {"name": str(company.get("name") or ""), "source": "kb_graph", "confidence": str(company.get("confidence") or "low")}
        for company in graph_pool or []
        if isinstance(company, dict) and str(company.get("name") or "").strip()
    ]
    if graph_companies:
        existing_names = {company["name"] for entry in step2 for company in entry["companies"]}
        new_companies = [company for company in graph_companies if company["name"] not in existing_names]
        if new_companies:
            step2.append(
                _pool_entry(
                    "same_layer", "T2", new_companies,
                    "知识库公司图谱按赛道/主营业务召回；只用于召回与排序，须回候选人详情核验本人证据",
                )
            )
            assembly_trace.append(f"step2 并入图谱公司 {len(new_companies)} 家（source=kb_graph）")
        else:
            assembly_trace.append("图谱召回公司已在池内，step2 不重复并入")

    step3_raw = fragment.get("step3_level_mapping") if isinstance(fragment.get("step3_level_mapping"), dict) else {}
    archetype_levels = ((archetype or {}).get("level_mapping") or {})
    accepted_levels = step3_raw.get("accepted_levels") or archetype_levels.get("accepted_candidate_levels") or []
    step3 = {
        "accepted_levels": [str(level) for level in accepted_levels],
        "calibration_rule": str(
            step3_raw.get("calibration_rule") or archetype_levels.get("note") or "按岗位职责范围而非 title 定档，待顾问确认"
        ),
    }

    step4_raw = fragment.get("step4_keyword_groups")
    step4: list[dict[str, Any]] = []
    source_groups = step4_raw if isinstance(step4_raw, list) else (archetype or {}).get("keyword_groups") or []
    for group in source_groups:
        if not isinstance(group, dict):
            continue
        terms = [str(term) for term in group.get("terms") or [] if str(term or "").strip()]
        if terms:
            step4.append(
                {
                    "group": str(group.get("group") or f"group_{len(step4) + 1}"),
                    "targets": str(group.get("targets") or ""),
                    "terms": terms[:20],
                }
            )
    if not step4:
        channels = plan.get("channels") if isinstance(plan.get("channels"), dict) else {}
        for channel, queries in channels.items():
            terms = [str(item.get("query") or "") for item in queries or [] if isinstance(item, dict) and str(item.get("query") or "").strip()]
            if terms:
                step4.append({"group": f"{channel}_queries", "targets": "渠道查询组（由执行计划回填）", "terms": terms[:20]})

    step5_raw = fragment.get("step5_expectation") if isinstance(fragment.get("step5_expectation"), dict) else {}
    expected = step5_raw.get("expected_recall_per_tier") if isinstance(step5_raw.get("expected_recall_per_tier"), dict) else {}
    step5 = {
        "expected_recall_per_tier": {str(k): v for k, v in expected.items()},
        "fallback_plan": str(step5_raw.get("fallback_plan") or "若 T1 召回不足预期 50%，按相邻池与关键词组顺序放宽"),
    }

    negative_rules: list[dict[str, Any]] = []
    raw_rules = fragment.get("negative_rules")
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if isinstance(rule, dict) and str(rule.get("rule") or "").strip():
                negative_rules.append(
                    {
                        "type": str(rule.get("type") or "未分类"),
                        "rule": str(rule.get("rule") or ""),
                        "source": str(rule.get("source") or "llm_inferred"),
                    }
                )
    if not negative_rules and archetype:
        negative_rules = [
            {"type": "客户校准负向规则", "rule": str(rule), "source": "kb_profile"}
            for rule in archetype.get("negative_rules") or []
            if str(rule or "").strip()
        ]
    # S4-2：restricted 层白名单约束（禁挖名单/竞业限制）并入 negative_rules，
    # source=restricted_client；费率/手机号/offer 金额/话术红线永远不进入此对象。
    existing_rule_texts = {str(rule.get("rule") or "") for rule in negative_rules}
    for rule in restricted_rules or []:
        text = str(rule.get("rule") or "").strip() if isinstance(rule, dict) else ""
        if text and text not in existing_rule_texts:
            negative_rules.append(
                {
                    "type": str(rule.get("type") or "restricted约束"),
                    "rule": text,
                    "source": "restricted_client",
                }
            )
            existing_rule_texts.add(text)
            assembly_trace.append(f"negative_rules 并入 restricted 约束：{str(rule.get('type') or '')}")

    # S4-3：排除规则引擎五类清单强制逐类留痕（PRD §4，第 4 步之后必过）。
    # 每项 {type, applicable, rule, basis, source}；不适用的类同样留痕（applicable=false + 理由）。
    # 与既有 negative_rules 条目按 (type, rule) 去重，五类清单条目始终保留。
    existing_pairs = {(str(rule.get("type") or ""), str(rule.get("rule") or "")) for rule in negative_rules}
    for item in negative_checklist or []:
        if not isinstance(item, dict):
            continue
        entry = {
            "type": str(item.get("type") or "未分类"),
            "applicable": bool(item.get("applicable")),
            "rule": str(item.get("rule") or ""),
            "basis": str(item.get("basis") or ""),
            "source": str(item.get("source") or "none"),
        }
        if (entry["type"], entry["rule"]) in existing_pairs:
            continue
        negative_rules.append(entry)
        existing_pairs.add((entry["type"], entry["rule"]))
        state = "适用" if entry["applicable"] else "不适用"
        assembly_trace.append(f"排除规则五类清单[{entry['type']}]：{state}（{entry['basis']}）")

    edits = fragment.get("consultant_edits")
    consultant_edits = [dict(item) for item in edits if isinstance(item, dict)] if isinstance(edits, list) else []

    # 池内公司来源分布留痕（PRD §3.2）
    source_distribution: dict[str, int] = {}
    for entry in step2:
        for company in entry["companies"]:
            source_distribution[company["source"]] = source_distribution.get(company["source"], 0) + 1
    if source_distribution:
        assembly_trace.append(
            "step2 公司池来源分布：" + "、".join(f"{source}={count}" for source, count in sorted(source_distribution.items()))
        )

    matched = profile_match if isinstance(profile_match, dict) else {}
    profile_name = str(matched.get("name") or "")
    if profile_name:
        assembly_trace.append(
            f"客户画像已挂载：{profile_name}（{matched.get('rule')}）"
            + ("，模糊匹配需人工确认" if matched.get("needs_confirmation") else "")
        )
    else:
        assembly_trace.append("客户画像未挂载（无命中）")

    v2 = {
        "schema_version": STRATEGY_V2_VERSION,
        "input_level": str(classification.get("input_level") or "L3"),
        "step1_job_essence": step1,
        "step2_target_pool": step2,
        "step3_level_mapping": step3,
        "step4_keyword_groups": step4,
        "step5_expectation": step5,
        "negative_rules": negative_rules,
        "consultant_edits": consultant_edits,
        "consultant_override": override,
        "anchors": classification.get("anchors") or {},
        "missing_anchors": list(classification.get("missing_anchors") or []),
        "classification_trace": [*list(classification.get("trace") or []), *assembly_trace],
        "archetype_id": str(classification.get("archetype_id") or ""),
        "profile_matched": {
            "name": profile_name,
            "rule": str(matched.get("rule") or "none"),
            "needs_confirmation": bool(matched.get("needs_confirmation")),
        },
        "step2_source_distribution": source_distribution,
    }
    if consultant.get("consultant_answers"):
        v2["consultant_answers"] = str(consultant["consultant_answers"])[:800]
    return v2


def validate_strategy_v2(value: Any) -> tuple[bool, list[str]]:
    """strategy_v2 落库前校验：缺必备键/类型错误时不写库并留 error。"""
    errors: list[str] = []
    if not isinstance(value, dict):
        return False, ["strategy_v2 必须是 JSON 对象"]
    for key in STRATEGY_V2_REQUIRED_KEYS:
        if key not in value:
            errors.append(f"缺少必备键：{key}")
    if errors:
        return False, errors
    if value.get("schema_version") != STRATEGY_V2_VERSION:
        errors.append(f"schema_version 必须是 {STRATEGY_V2_VERSION}")
    if value.get("input_level") not in {"L1", "L2", "L3"}:
        errors.append("input_level 必须是 L1/L2/L3")
    if not isinstance(value.get("step1_job_essence"), dict):
        errors.append("step1_job_essence 必须是对象")
    if not isinstance(value.get("step2_target_pool"), list):
        errors.append("step2_target_pool 必须是数组")
    else:
        for index, entry in enumerate(value["step2_target_pool"], 1):
            if not isinstance(entry, dict):
                errors.append(f"step2_target_pool[{index}] 必须是对象")
                continue
            if entry.get("path") not in _POOL_PATHS:
                errors.append(f"step2_target_pool[{index}].path 必须是 same_layer/reverse/adjacent")
            if entry.get("tier") not in _POOL_TIERS:
                errors.append(f"step2_target_pool[{index}].tier 必须是 T1/T2/T3")
            companies = entry.get("companies")
            if not isinstance(companies, list) or not companies:
                errors.append(f"step2_target_pool[{index}].companies 必须是非空数组")
                continue
            for company in companies:
                if not isinstance(company, dict) or not str(company.get("name") or "").strip():
                    errors.append(f"step2_target_pool[{index}] 存在缺 name 的公司")
                    break
                if company.get("source") not in _POOL_SOURCES:
                    errors.append(f"公司 {company.get('name')} 的 source 非法：{company.get('source')}")
                if company.get("confidence") not in _CONFIDENCES:
                    errors.append(f"公司 {company.get('name')} 的 confidence 非法：{company.get('confidence')}")
    if not isinstance(value.get("step3_level_mapping"), dict):
        errors.append("step3_level_mapping 必须是对象")
    if not isinstance(value.get("step4_keyword_groups"), list):
        errors.append("step4_keyword_groups 必须是数组")
    else:
        for group in value["step4_keyword_groups"]:
            if not isinstance(group, dict) or not group.get("group") or not isinstance(group.get("terms"), list):
                errors.append("step4_keyword_groups 存在缺 group/terms 的组")
                break
    if not isinstance(value.get("step5_expectation"), dict):
        errors.append("step5_expectation 必须是对象")
    if not isinstance(value.get("negative_rules"), list):
        errors.append("negative_rules 必须是数组（可为空）")
    if not isinstance(value.get("consultant_edits"), list):
        errors.append("consultant_edits 必须是数组（可为空）")
    return not errors, errors


def extract_strategy_v2(metadata: Any) -> dict[str, Any] | None:
    """读取侧兼容：v1 旧 artifact（无 strategy_v2 键）返回 None，不崩。"""
    data = _loads(metadata, {})
    if not isinstance(data, dict):
        return None
    candidate = data.get("strategy_v2")
    return candidate if isinstance(candidate, dict) else None
