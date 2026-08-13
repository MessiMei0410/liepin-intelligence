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

_POOL_SOURCES = {
    "client_doc",
    "kb_graph",
    "kb_profile",
    "legacy_profile_suggestions",
    "llm_inferred",
    "consultant_calibrated",
    "consultant_confirmed",
    "company_kb",
}
_POOL_PATHS = {"same_layer", "reverse", "adjacent"}
_POOL_TIERS = {"T1", "T2", "T3"}
_CONFIDENCES = {"high", "medium", "low"}
_COMPANY_SUFFIXES = ("股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团", "公司")
_COMPANY_BRACKETS = re.compile(r"（[^）]*）|\([^)]*\)|【[^】]*】|\[[^]]*\]")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_KB_CANDIDATES = (
    _REPO_ROOT / "asa-web" / "knowledge_base",
    _REPO_ROOT / "knowledge_base",
    Path("/Users/messi/Documents/ASA/knowledge_base"),
)

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
    raw = (
        os.environ.get("ASA_KNOWLEDGE_BASE_DIR", "").strip()
        or os.environ.get("A_SYSTEM_KNOWLEDGE_BASE_DIR", "").strip()
    )
    if raw:
        return Path(raw).expanduser()
    return next((path for path in _DEFAULT_KB_CANDIDATES if path.is_dir()), _DEFAULT_KB_CANDIDATES[0])


def knowledge_base_health(kb_dir: str | Path | None = None) -> dict[str, Any]:
    """Report whether strategy generation has at least one real knowledge anchor."""
    directory = Path(kb_dir) if kb_dir else knowledge_base_dir()
    available = {
        "client_profiles": (directory / "kb_client_profiles_v1.json").is_file(),
        "company_graph": (directory / "kb_company_graph_jsj_v1.json").is_file(),
        "job_archetypes": any(directory.glob("seed_*.json")) if directory.is_dir() else False,
        "skill_ontology": (directory / "kb_skill_ontology_semiconductor_v1.json").is_file(),
        "level_mapping": (directory / "kb_level_mapping_v1.json").is_file(),
    }
    return {
        "ok": bool(directory.is_dir() and any(available.values())),
        "directory": str(directory),
        "available": available,
        "missing": [name for name, present in available.items() if not present],
    }


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
        try:
            golden_replay_min_recall = float(doc.get("golden_replay_min_recall") or 1.0)
        except (TypeError, ValueError):
            golden_replay_min_recall = 1.0
            trace.append(f"{path.name} 的 golden_replay_min_recall 非法，按 1.0 处理")
        archetypes.append(
            {
                "archetype_id": str(raw.get("archetype_id") or ""),
                "title": str(raw.get("title") or ""),
                "client": str(raw.get("client") or ""),
                "client_scoped": bool(raw.get("client_scoped")),
                "essence": str(raw.get("essence") or ""),
                "directions": raw.get("directions") if isinstance(raw.get("directions"), list) else [],
                "target_functions": raw.get("target_functions") if isinstance(raw.get("target_functions"), list) else [],
                "location_policy": str(raw.get("location_policy") or ""),
                "level_mapping": doc.get("level_mapping") if isinstance(doc.get("level_mapping"), dict) else {},
                "keyword_groups": doc.get("keyword_groups") if isinstance(doc.get("keyword_groups"), list) else [],
                "negative_rules": doc.get("negative_rules") if isinstance(doc.get("negative_rules"), list) else [],
                "target_company_pool": doc.get("target_company_pool") if isinstance(doc.get("target_company_pool"), dict) else {},
                "golden_candidates": doc.get("golden_candidates") if isinstance(doc.get("golden_candidates"), list) else [],
                "golden_replay_min_recall": golden_replay_min_recall,
                # 知识飞轮二期：原型技能词（kb_skill_ontology canonical），供评估消费时
                # 取本体典型证据形式；缺失按空列表处理。
                "skills_ontology_nodes": [
                    str(node).strip()
                    for node in doc.get("skills_ontology_nodes") or []
                    if str(node or "").strip()
                ],
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
    "changyue_bonding_motion_control": (
        "自动化软件", "运动控制软件", "控制软件", "motion control software",
    ),
    "changyue_precision_equipment_mechanical": (
        "机械高级", "高级机械", "机械设计", "机械工程师", "结构设计",
    ),
    # 知识飞轮二期扩充原型（seed_*_v1.json，2026-08-05）：电源研发 / fab 工艺·设备·良率·质量 /
    # 长越电气·失效分析 / FPGA·嵌入式硬件。token 选择原则：足够具体，不抢占既有原型
    # （如电源研发不收“电源”裸词，避免抢走「技术市场经理（三次电源）」的 TME 命中）。
    "power_rd_expert_computing": (
        "电源专家", "电源研发", "电源工程师", "电源设计", "电源硬件",
        "vrm", "vpd", "tlvr", "acdc", "ac-dc", "ac/dc", "dc-dc", "dcdc",
    ),
    "fab_td_process_expert": (
        "工艺专家", "工艺工程师", "工艺整合", "制程整合", "制程工程师",
        "pie", "device专家", "器件专家",
    ),
    "fab_equipment_expert": (
        "设备专家", "设备工程师", "量测设备", "设备研发工程师",
    ),
    "fab_yield_expert": (
        "ye技术", "ye工程师", "ye专家", "良率", "缺陷分析", "yield",
    ),
    "fab_quality_reliability": (
        "pqe", "cqe", "sqe", "质量工程师", "质量专家", "品质工程师", "可靠性",
    ),
    "changyue_bonding_electrical": (
        "电气工程师", "电气高级", "电气设计", "电气研发",
    ),
    "changyue_failure_analysis": (
        "失效分析", "fa工程师", "failure analysis",
    ),
    "fpga_embedded_hardware": (
        "fpga", "嵌入式", "硬件工程师", "逻辑设计", "pcb工程师",
    ),
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
        archetype_client = str(archetype.get("client") or "")
        if archetype.get("client_scoped") and normalized_client != archetype_client:
            continue
        tokens = _ARCHETYPE_TITLE_TOKENS.get(archetype_id, ())
        hit_token = next((token for token in tokens if token.lower() in normalized_title), "")
        if hit_token:
            trace.append(f"岗位标题“{title}”命中原型 {archetype_id} 职能关键词“{hit_token}”")
            return {**archetype, "matched_by": "title_token", "match_reason": f"标题命中原型职能关键词：{hit_token}"}, trace
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
    legacy_defaults = {
        "T1_competitor_device": ("same_layer", "T1", "high"),
        "T2_customer_OEM": ("reverse", "T2", "high"),
        "T3_adjacent_unconfirmed": ("adjacent", "T3", "medium"),
    }
    for key, raw_block in pool.items():
        if not isinstance(raw_block, dict) or raw_block.get("enabled") is False:
            continue
        block = raw_block
        tier_match = re.match(r"T[123]", str(key))
        default_path, default_tier, default_confidence = legacy_defaults.get(
            str(key),
            (
                "adjacent" if "adjacent" in str(key).lower() else "reverse" if "reverse" in str(key).lower() else "same_layer",
                tier_match.group(0) if tier_match else "T2",
                "medium",
            ),
        )
        path = str(block.get("path") or default_path)
        tier = str(block.get("tier") or default_tier)
        confidence = str(block.get("confidence") or default_confidence)
        confidence = confidence if confidence in _CONFIDENCES else default_confidence
        companies = [
            {"name": str(company.get("name") or ""), "source": "kb_profile", "confidence": confidence}
            for company in block.get("companies") or []
            if str(company.get("name") or "").strip()
        ]
        if companies:
            entries.append(_pool_entry(path, tier, companies, str(block.get("rationale") or "")))
    return entries


def _graph_company_is_explicitly_excluded(company_name: str, fragment: dict[str, Any]) -> bool:
    """Honor blanket and company-specific exclusions for graph-derived targets."""
    rules = fragment.get("negative_rules")
    if not isinstance(rules, list):
        return False
    normalized_name = _coverage_norm(company_name)
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get("type") or "").lower()
        text = " ".join(
            str(rule.get(key) or "")
            for key in ("type", "rule", "source")
        ).lower()
        graph_signal = "kb_graph" in text or "知识图谱" in text or "图谱公司" in text
        exclusion_signal = "exclusion" in rule_type or "排除" in rule_type or any(
            marker in text
            for marker in (
                "不纳入公司池", "不并入公司池", "从公司池排除", "排除出公司池",
                "排除", "不得搜索", "不得推荐",
            )
        )
        blanket_exclusion = graph_signal and any(
            marker in text
            for marker in ("不纳入公司池", "不并入公司池", "从公司池排除", "排除出公司池")
        )
        named_exclusion = bool(normalized_name and normalized_name in _coverage_norm(text))
        if exclusion_signal and (blanket_exclusion or named_exclusion):
            return True
    return False


def _company_name_is_banned(company_name: str, banned_companies: list[str]) -> bool:
    """Match customer-level do-not-source companies across legal-name aliases."""
    def normalize(value: Any) -> str:
        text = _COMPANY_BRACKETS.sub("", str(value or ""))
        text = "".join(text.split()).lower()
        text = re.sub(r"(?:及其|及)?子公司$", "", text)
        changed = True
        while changed and text:
            changed = False
            for suffix in _COMPANY_SUFFIXES:
                if text.endswith(suffix) and len(text) > len(suffix):
                    text = text[: -len(suffix)]
                    changed = True
        return text

    candidate = normalize(company_name)
    if not candidate:
        return False
    for banned in banned_companies:
        blocked = normalize(banned)
        if not blocked:
            continue
        shorter, longer = sorted((candidate, blocked), key=len)
        if shorter == longer or (len(shorter) >= 3 and shorter in longer):
            return True
    return False


def _brief_items(value: Any, *, limit: int = 6) -> list[str]:
    """把顾问简报里的输入压成短、稳定、可展示的事实片段。"""
    if isinstance(value, str):
        values = _split_terms(value)
    elif isinstance(value, (list, tuple)):
        values = [str(item).strip() for item in value if str(item or "").strip()]
    else:
        values = []
    return list(dict.fromkeys(values))[:limit]


def _infer_role_family(title: str, corpus: str) -> str:
    """岗位族只用于顾问口径，不参与硬筛选，避免 title 被当成能力证据。"""
    text = f"{title}\n{corpus}".lower()
    if any(token in text for token in ("技术市场", "tme", "technical marketing", "fae", "应用工程", "ae")):
        return "技术市场/应用"
    if any(token in text for token in ("总监", "负责人", "主管", "经理", "head", "director", "manager")):
        return "研发管理"
    if any(token in text for token in ("研发", "工程师", "专家", "设计", "开发", "engineer", "expert")):
        return "研发/工程"
    return "待核验岗位族"


def build_consultant_judgement(
    plan: dict[str, Any],
    classification: dict[str, Any],
    *,
    archetype: dict[str, Any] | None = None,
    consultant: dict[str, Any] | None = None,
    profile_context: dict[str, Any] | None = None,
    canonical_position: dict[str, Any] | None = None,
    step1: dict[str, Any] | None = None,
    step2: list[dict[str, Any]] | None = None,
    step3: dict[str, Any] | None = None,
    step4: list[dict[str, Any]] | None = None,
    step5: dict[str, Any] | None = None,
    negative_rules: list[dict[str, Any]] | None = None,
    learning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成资深猎头顾问简报：把岗位事实转成判断、顺序、边界和复盘动作。

    这个对象是策略的解释层，不替代 step1-5，也不改变硬条件。它的价值在于把
    “找什么人”与“为什么先找、什么时候放宽、放宽要付出什么代价”明确记录下来。
    所有事实均来自岗位、画像、原型或已确认的历史反馈；无法确认的内容进入
    must_confirm，而不是伪装成客户偏好。
    """
    canonical = canonical_position or {}
    consultant = consultant or {}
    profile_context = profile_context or {}
    learning = learning or {}
    step1 = step1 or {}
    step2 = step2 or []
    step3 = step3 or {}
    step4 = step4 or []
    step5 = step5 or {}
    negative_rules = negative_rules or []

    title = str(canonical.get("job") or plan.get("job") or "").strip()
    corpus = " ".join(
        str(canonical.get(key) or "")
        for key in (
            "job", "requirements", "responsibilities", "education", "experience",
            "hard_requirements", "exclusions", "location", "objective",
        )
    )
    anchors = classification.get("anchors") if isinstance(classification.get("anchors"), dict) else {}
    product_anchor = _brief_items((anchors.get("product_tech_line") or {}).get("values"), limit=5)
    scenario_anchor = _brief_items((anchors.get("scenario_track") or {}).get("values"), limit=4)
    customer_anchor = _brief_items((anchors.get("customer_of_customer") or {}).get("values"), limit=4)
    hard_requirements = _brief_items(
        "；".join(
            str(canonical.get(key) or "")
            for key in ("hard_requirements", "requirements", "experience", "education")
        ),
        limit=6,
    )
    missing_labels = [ANCHOR_LABELS[key] for key in classification.get("missing_anchors") or [] if key in ANCHOR_LABELS]
    role_family = _infer_role_family(title, corpus)
    product_text = "、".join(product_anchor) or "岗位要求中的核心产品/技术线"
    scenario_text = "、".join(scenario_anchor) or "岗位明确的应用场景"
    levels = _brief_items(step3.get("accepted_levels"), limit=5)
    pool_entries = [entry for entry in step2 if isinstance(entry, dict)]
    path_entries = {
        path: [entry for entry in pool_entries if str(entry.get("path") or "") == path]
        for path in ("same_layer", "reverse", "adjacent")
    }
    pool_count = sum(
        len(entry.get("companies") or [])
        for entry in pool_entries
        if isinstance(entry.get("companies"), list)
    )
    same_layer_count = sum(len(entry.get("companies") or []) for entry in path_entries["same_layer"])
    transfer_paths = [path for path in ("adjacent", "reverse") if path_entries[path]]

    direct_evidence = list(dict.fromkeys([
        *hard_requirements[:4],
        *(f"产品/技术线：{item}" for item in product_anchor[:3]),
        *(f"应用场景：{item}" for item in scenario_anchor[:2]),
        *(f"客户场景：{item}" for item in customer_anchor[:2]),
    ]))[:8]
    transferable_evidence = [
        "相邻产品或相邻客户场景只能证明迁移基础，不能直接等同于岗位硬技能",
        "目标公司和 title 只能作为召回线索，必须回到候选人项目/职责核验本人证据",
    ]
    must_verify = [
        "真实职责边界：独立负责、主导交付，还是协同支持",
        "项目证据：具体产品/技术、应用场景、复杂度与可核验结果",
        "求职动机与可接受条件：地点、薪资、汇报线和到岗节奏",
    ]
    if missing_labels:
        must_verify.insert(0, f"客户尚未明确：{'、'.join(missing_labels)}")

    search_sequence = [
        {
            "round": "R1",
            "name": "核心同层",
            "target": f"T1/T2 同层公司 + {role_family}",
            "purpose": f"先验证 {product_text} 在 {scenario_text} 中的直接项目证据",
            "gate": "硬条件和直接证据不降级",
        },
        {
            "round": "R2",
            "name": "岗位原型变体",
            "target": "相邻职称、产品别名、项目交付物和场景词",
            "purpose": "补齐 title 不一致但职责相同的人选，降低单一关键词偏差",
            "gate": "仍需回到项目/职责核验，不能只看公司或 title",
        },
    ]
    if transfer_paths:
        search_sequence.append(
            {
                "round": "R3",
                "name": "迁移池",
                "target": "、".join(transfer_paths) + " 路径公司/场景",
                "purpose": "核心池不足时补充可迁移人选，明确迁移成本再交顾问复核",
                "gate": "仅在核心池不足或客户允许迁移候选时启用",
            }
        )
    search_sequence.append(
        {
            "round": "R4",
            "name": "复核与再平衡",
            "target": "去重、噪音复盘、渠道/关键词边际产出",
            "purpose": "把客户反馈和真实下游结果转成下一轮策略调整",
            "gate": "先记录原因，再决定扩池、改词或回到客户校准",
        }
    )

    expansion_ladder = [
        {
            "step": 1,
            "direction": "同层目标公司",
            "trigger": "首轮建立直接证据基线",
            "preserve": "全部硬条件、核心场景和职责深度",
            "tradeoff": "池子相对小，但判断最稳",
        },
        {
            "step": 2,
            "direction": "关键词与 title 变体",
            "trigger": "公司池有覆盖但召回不足，或 title 命名不统一",
            "preserve": "岗位本质不变，只扩大表达方式",
            "tradeoff": "需要加强去重和噪音复核",
        },
    ]
    if "adjacent" in transfer_paths:
        expansion_ladder.append(
            {
                "step": len(expansion_ladder) + 1,
                "direction": "相邻产品/场景迁移",
                "trigger": "核心池不足且客户接受可迁移人选",
                "preserve": "保留解决问题的方法论和交付深度",
                "tradeoff": "产品或场景差距必须通过项目追问补证",
            }
        )
    if "reverse" in transfer_paths:
        expansion_ladder.append(
            {
                "step": len(expansion_ladder) + 1,
                "direction": "客户侧/反向人才池",
                "trigger": "同层供给不足，且岗位允许从客户或整机侧迁移",
                "preserve": "必须保留对产品/技术线的真实理解",
                "tradeoff": "角色责任、研发深度和动机需要单独核验",
            }
        )
    expansion_ladder.append(
        {
            "step": len(expansion_ladder) + 1,
            "direction": "地域/职级边界调整",
            "trigger": "前述路径仍不足，并经顾问确认",
            "preserve": "不自动放宽硬门槛",
            "tradeoff": "候选人意愿、薪资和到岗风险上升",
        }
    )

    must_confirm = list(dict.fromkeys([
        *(f"补齐四锚点：{label}" for label in missing_labels),
        "确认哪些条件是一票否决，哪些只是优先项",
        "确认薪资范围、汇报线、团队配置和决策周期",
    ]))[:6]
    if not must_confirm:
        must_confirm = ["确认客户对直接证据、迁移候选和职级跨度的排序"]
    assumptions = []
    if str(classification.get("input_level") or "") == "L3":
        assumptions.append("当前主要依据 JD 和知识库原型，客户口味仍需一轮校准")
    if profile_context.get("notes"):
        assumptions.append("客户注意事项已挂载，仍以本轮顾问确认后的版本为准")
    if (profile_context or {}).get("hiring_preferences"):
        assumptions.append("已参考客户用人偏好，但未把偏好自动升级为硬门槛")
    if str(consultant.get("consultant_answers") or "").strip():
        assumptions.append("已纳入顾问本轮补充，原话约束优先于模型推断")
    if not assumptions:
        assumptions.append("未写明的条件不自动升级为硬门槛")

    positive_signals: list[str] = []
    negative_signals: list[str] = []
    for outcome in learning.get("business_outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        channel = str(outcome.get("channel") or "未知渠道")
        query = str(outcome.get("source_query") or "").strip()
        try:
            score = float(outcome.get("experience_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        positive = int(outcome.get("client_positive") or 0) + int(outcome.get("recommended") or 0)
        negative = int(outcome.get("client_rejected") or 0) + int(outcome.get("stopped") or 0)
        label = f"{channel}「{query}」" if query else channel
        if positive or score > 0:
            positive_signals.append(f"{label}有正向下游信号（经验分 {score:g}）")
        if negative:
            negative_signals.append(f"{label}出现停止/客户否决信号（{negative} 条）")
        noise = str(outcome.get("noise_notes") or "").strip()
        if noise:
            negative_signals.append(f"{label}记录噪音：{noise[:100]}")
    for memory in learning.get("approved_memories") or []:
        if isinstance(memory, dict) and str(memory.get("content") or "").strip() and len(positive_signals) < 4:
            positive_signals.append(str(memory["content"])[:140])
    if not positive_signals and not negative_signals:
        learning_note = ["首轮历史反馈不足，先建立渠道、关键词和客户反馈基线"]
    else:
        learning_note = [
            "优先复用有正向下游信号的渠道/词组，同时保留岗位硬门槛",
            "对持续低产出或高噪音项先降权复核，不直接删除，避免把一次失败误判为永久负向规则",
        ]

    if pool_count == 0:
        difficulty, difficulty_reason = "unknown", "当前没有可核验的目标公司池，市场难度不能靠模型估计"
    elif pool_count < 8:
        difficulty, difficulty_reason = "hard", f"当前可执行公司池仅 {pool_count} 家，供给可能偏窄"
    elif pool_count < 16:
        difficulty, difficulty_reason = "challenging", f"当前可执行公司池 {pool_count} 家，需要分轮扩池"
    else:
        difficulty, difficulty_reason = "normal", f"当前可执行公司池 {pool_count} 家，适合先做核心池验证"

    return {
        "version": "senior_consultant_v1",
        "basis": [
            "岗位事实",
            *(["客户画像"] if profile_context else []),
            *(["岗位原型"] if archetype else []),
            *(["历史实验/业务反馈"] if positive_signals or negative_signals else []),
        ],
        "role_diagnosis": {
            "role_family": role_family,
            "business_mandate": str(step1.get("statement") or "围绕岗位硬条件和真实业务场景建立可执行人才池"),
            "core_differentiator": (
                f"直接证据优先看 {product_text}；场景落地优先看 {scenario_text}"
                + (f"；客户链路关注 { '、'.join(customer_anchor) }" if customer_anchor else "")
            ),
            "candidate_archetype": (
                f"能在{scenario_text}中独立负责{product_text}相关工作，并能讲清项目边界、交付物和结果的人选"
                + (f"；职级参考 { '、'.join(levels) }" if levels else "")
            ),
        },
        "search_sequence": search_sequence,
        "expansion_ladder": expansion_ladder,
        "evidence_standard": {
            "direct_evidence": direct_evidence,
            "transferable_evidence": transferable_evidence,
            "must_verify": must_verify[:8],
        },
        "client_calibration": {
            "must_confirm": must_confirm,
            "assumptions": assumptions,
            "selling_points": _brief_items(profile_context.get("selling_points"), limit=4),
            "hiring_preferences": _brief_items(profile_context.get("hiring_preferences"), limit=4),
        },
        "market_view": {
            "difficulty": difficulty,
            "reason": difficulty_reason,
            "same_layer_company_count": same_layer_count,
            "transfer_paths_available": transfer_paths,
            "pool_risk": (
                "四锚点仍有缺口，初筛容易把相邻经验误判为直接匹配"
                if missing_labels else "需防止目标公司/ title 替代本人项目证据"
            ),
        },
        "learning_application": {
            "positive_signals": positive_signals[:6],
            "negative_signals": negative_signals[:6],
            "applied_adjustments": learning_note,
        },
        "hard_gate_policy": "先守住硬门槛，再按同义词、相邻池、反向池、地域/职级顺序扩展；任何放宽都要记录代价并经顾问确认。",
    }


def refresh_consultant_judgement(value: dict[str, Any]) -> dict[str, Any]:
    """策略按项修订后补建或刷新顾问简报，保留已有画像与反馈结论。"""
    existing = value.get("consultant_judgement")
    if not isinstance(existing, dict):
        existing = {}
    classification = {
        "input_level": str(value.get("input_level") or "L3"),
        "anchors": value.get("anchors") if isinstance(value.get("anchors"), dict) else {},
        "missing_anchors": value.get("missing_anchors") if isinstance(value.get("missing_anchors"), list) else [],
    }
    current_statement = str((value.get("step1_job_essence") or {}).get("statement") or "").strip()
    refreshed = build_consultant_judgement(
        {"strategy_summary": current_statement},
        classification,
        canonical_position={"job": current_statement, "responsibilities": current_statement},
        step1=value.get("step1_job_essence") if isinstance(value.get("step1_job_essence"), dict) else {},
        step2=value.get("step2_target_pool") if isinstance(value.get("step2_target_pool"), list) else [],
        step3=value.get("step3_level_mapping") if isinstance(value.get("step3_level_mapping"), dict) else {},
        step4=value.get("step4_keyword_groups") if isinstance(value.get("step4_keyword_groups"), list) else [],
        step5=value.get("step5_expectation") if isinstance(value.get("step5_expectation"), dict) else {},
        negative_rules=value.get("negative_rules") if isinstance(value.get("negative_rules"), list) else [],
    )
    existing_role = existing.get("role_diagnosis") if isinstance(existing.get("role_diagnosis"), dict) else {}
    refreshed_role = refreshed["role_diagnosis"]
    for key in ("role_family", "core_differentiator"):
        existing_value = str(existing_role.get(key) or "").strip()
        if existing_value and not (key == "role_family" and existing_value == "待核验岗位族"):
            refreshed_role[key] = existing_role[key]
    if current_statement:
        refreshed_role["business_mandate"] = current_statement

    existing_evidence = existing.get("evidence_standard") if isinstance(existing.get("evidence_standard"), dict) else {}
    for key in ("direct_evidence", "must_verify"):
        if isinstance(existing_evidence.get(key), list) and existing_evidence[key]:
            refreshed["evidence_standard"][key] = list(existing_evidence[key])
    for key in ("basis", "client_calibration", "learning_application"):
        if existing.get(key):
            refreshed[key] = json.loads(json.dumps(existing[key], ensure_ascii=False))
    value["consultant_judgement"] = refreshed
    return value


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
    banned_companies: list[str] | None = None,
    negative_checklist: list[dict[str, Any]] | None = None,
    canonical_position: dict[str, Any] | None = None,
    skill_ontology: dict[str, Any] | None = None,
    level_hit: dict[str, Any] | None = None,
    profile_context: dict[str, Any] | None = None,
    learning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 strategy_v2：LLM 填充各步，运行时兜底必备键并强制版本号/定级/顾问留痕。

    consultant_override=True 时，所有推断锚点保持 inferred:true + confidence（PRD §1）。
    S4-2：profile_match（客户画像挂载留痕）、graph_pool（kb_graph 公司，只用于召回排序）、
    restricted_rules（禁挖/竞业白名单，source=restricted_client）由运行时传入并合。
    S4-3：negative_checklist（排除规则引擎五类清单，negative_rules 模块输出，逐项含
    applicable/rule/basis/source）由运行时传入，强制逐类留痕并入 negative_rules。
    知识飞轮二期：skill_ontology（技能本体，kb_skill）用于 step4 关键词别名归一与相关词
    提示（有本体则增强、无本体则现状）；level_hit（kb_level_mapping 职级命中）优先于
    LLM/原型路径填充 step3（source=kb_level），未命中时维持现有路径。
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
    # 知识飞轮二期：校准覆盖层命中的公司保留 source=consultant_calibrated（二期校准覆盖层）。
    # governance：图谱命中只用于召回与排序，必须回候选人详情核验本人证据。
    graph_companies: list[dict[str, str]] = []
    excluded_graph_companies: list[str] = []
    for company in graph_pool or []:
        if not isinstance(company, dict):
            continue
        name = str(company.get("name") or "").strip()
        if not name:
            continue
        if _graph_company_is_explicitly_excluded(name, fragment):
            excluded_graph_companies.append(name)
            continue
        graph_companies.append(
            {
                "name": name,
                "source": str(company.get("source") or "kb_graph"),
                "confidence": str(company.get("confidence") or "low"),
            }
        )
    if excluded_graph_companies:
        assembly_trace.append(f"图谱公司命中显式排除规则，未并入 step2：{len(excluded_graph_companies)} 家")
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
            calibrated = sum(1 for company in new_companies if company["source"] == "consultant_calibrated")
            assembly_trace.append(
                f"step2 并入图谱公司 {len(new_companies)} 家（source=kb_graph"
                + (f"，含顾问校准 {calibrated} 家 source=consultant_calibrated）" if calibrated else "）")
            )
        else:
            assembly_trace.append("图谱召回公司已在池内，step2 不重复并入")

    # 公司知识库（CKB）补充：图谱未命中（仍为 llm_inferred）的目标公司，若 CKB 有画像
    # 记录，则把来源升级为 source=company_kb 留痕（数据结构/键不变，仅 source 升级）；
    # CKB 库缺失/无表/不可用时静默跳过，行为与现状一致。
    from . import company_kb

    ckb_confirmed = 0
    for entry in step2:
        for company in entry.get("companies") or []:
            if not isinstance(company, dict) or company.get("source") != "llm_inferred":
                continue
            if company_kb.get_profile(str(company.get("name") or "")):
                company["source"] = "company_kb"
                ckb_confirmed += 1
    if ckb_confirmed:
        assembly_trace.append(f"step2 公司知识库画像确认 {ckb_confirmed} 家（llm_inferred→company_kb）")

    # 客户级禁挖名单是硬边界，统一过滤所有来源的目标公司，不能只限制图谱来源。
    # 名称匹配支持法定名/简称；仅保留计数留痕，不把受限公司字面量写入策略对象以外的表面。
    banned = [str(name).strip() for name in banned_companies or [] if str(name or "").strip()]
    if banned:
        blocked_count = 0
        filtered_step2: list[dict[str, Any]] = []
        for entry in step2:
            companies = [
                company
                for company in entry["companies"]
                if not _company_name_is_banned(str(company.get("name") or ""), banned)
            ]
            blocked_count += len(entry["companies"]) - len(companies)
            if companies:
                filtered_step2.append({**entry, "companies": companies})
        step2 = filtered_step2
        if blocked_count:
            assembly_trace.append(f"step2 已按客户级禁挖约束剔除 {blocked_count} 家目标公司")

    step3_raw = fragment.get("step3_level_mapping") if isinstance(fragment.get("step3_level_mapping"), dict) else {}
    archetype_levels = ((archetype or {}).get("level_mapping") or {})
    if level_hit and level_hit.get("accepted_levels"):
        # 知识飞轮二期：step3 职级映射优先查职级知识（source=kb_level 内置库 /
        # consultant_confirmed 顾问确认规则），查不到（level_hit=None）才走现有
        # LLM/原型路径；按职责定档口径保留。
        basis = str(level_hit.get("basis") or "").strip()
        years_hint = str(level_hit.get("years_hint") or "").strip()
        level_source = str(level_hit.get("source") or "kb_level")
        step3 = {
            "accepted_levels": [str(level) for level in level_hit.get("accepted_levels") or []],
            "calibration_rule": "；".join(
                part
                for part in (
                    f"职级带 {level_hit.get('band')}（{level_hit.get('label')}）" + (f"，{years_hint}" if years_hint else ""),
                    "按岗位职责范围而非 title 机械定档，待顾问确认",
                )
                if part
            ),
            "level_source": level_source,
            "kb_level_band": str(level_hit.get("band") or ""),
        }
        if basis:
            step3["kb_level_basis"] = basis
        assembly_trace.append(
            f"step3 职级映射采用知识库（source={level_source}）：{level_hit.get('label')}，接受职级 "
            f"{'、'.join(step3['accepted_levels'])}；LLM/原型职级路径未启用"
        )
    else:
        accepted_levels = step3_raw.get("accepted_levels") or archetype_levels.get("accepted_candidate_levels") or []
        step3 = {
            "accepted_levels": [str(level) for level in accepted_levels],
            "calibration_rule": str(
                step3_raw.get("calibration_rule") or archetype_levels.get("note") or "按岗位职责范围而非 title 定档，待顾问确认"
            ),
        }
    canonical_position = canonical_position or {}
    locations = [
        item.strip()
        for item in re.split(r"[/、,，;；\s]+", str(canonical_position.get("location") or ""))
        if item.strip()
    ]
    scenario_anchor = (
        classification.get("anchors", {}).get("scenario_track", {})
        if isinstance(classification.get("anchors"), dict)
        else {}
    )
    scenarios = [
        str(item).strip() for item in scenario_anchor.get("values") or [] if str(item).strip()
    ] if isinstance(scenario_anchor, dict) else []

    step4_raw = fragment.get("step4_keyword_groups")
    step4: list[dict[str, Any]] = []
    source_groups = step4_raw if isinstance(step4_raw, list) else (archetype or {}).get("keyword_groups") or []
    ontology = skill_ontology if isinstance(skill_ontology, dict) and skill_ontology.get("skills") else None
    if ontology:
        from . import knowledge_base  # 局部导入避免环：knowledge_base 依赖本模块的 knowledge_base_dir
    for group in source_groups:
        if not isinstance(group, dict):
            continue
        terms = [str(term) for term in group.get("terms") or [] if str(term or "").strip()]
        if not terms:
            continue
        entry = {
            "group": str(group.get("group") or f"group_{len(step4) + 1}"),
            "targets": str(group.get("targets") or ""),
            "terms": terms[:20],
        }
        if ontology:
            # 知识飞轮二期（source=kb_skill）：别名归一 —— 同一 canonical 的多个写法只保留
            # 首个原词（不破坏召回口径，仅去重归一）；相关技能提示附在组上供顾问参考。
            normalized: list[dict[str, str]] = []
            deduped_terms: list[str] = []
            seen_keys: set[str] = set()
            for term in terms:
                info = knowledge_base.normalize_skill(term, ontology)
                key = info["canonical"] if info["matched"] else info["normalized"]
                if not key or key in seen_keys:
                    if info["matched"]:
                        normalized.append({"raw": term, "canonical": info["canonical"], "family": info["family"]})
                    continue
                seen_keys.add(key)
                deduped_terms.append(term)
                if info["matched"]:
                    normalized.append({"raw": term, "canonical": info["canonical"], "family": info["family"]})
            hints: list[str] = []
            hint_keys = {knowledge_base._normalize_skill_key(term) for term in deduped_terms}
            for item in normalized:
                for related in knowledge_base.related_skills(item["canonical"], ontology):
                    if related not in hints and knowledge_base._normalize_skill_key(related) not in hint_keys:
                        hints.append(related)
            hints = hints[:5]
            if normalized or hints:
                entry["terms"] = deduped_terms[:20]
                entry["skill_ontology"] = {
                    "source": "kb_skill",
                    "normalized": normalized,
                    "related_terms_hint": hints,
                }
                if normalized:
                    assembly_trace.append(
                        f"关键词组[{entry['group']}]技能别名归一（source=kb_skill）："
                        + "、".join(f"{item['raw']}→{item['canonical']}" for item in normalized[:6])
                    )
                if hints:
                    assembly_trace.append(
                        f"关键词组[{entry['group']}]相关技能提示（source=kb_skill，仅供顾问参考，不进查询）：{'、'.join(hints)}"
                    )
        step4.append(entry)
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
        # 顾问确认规则（consultant_confirmed）附提案 ID 留痕；其余条目无此键，输出不变。
        if item.get("proposal_id"):
            entry["proposal_id"] = str(item["proposal_id"])
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
        "evaluation_constraints": {
            "locations": list(dict.fromkeys(locations)),
            "levels": list(dict.fromkeys(step3["accepted_levels"])),
            "scenarios": list(dict.fromkeys(scenarios)),
        },
        "step4_keyword_groups": step4,
        "step5_expectation": step5,
        "negative_rules": negative_rules,
        "consultant_edits": consultant_edits,
        "consultant_override": override,
        "anchors": classification.get("anchors") or {},
        "missing_anchors": list(classification.get("missing_anchors") or []),
        "classification_trace": [*list(classification.get("trace") or []), *assembly_trace],
        "archetype_id": str(classification.get("archetype_id") or ""),
        # 知识飞轮二期：原型命中显式标注（方案验收口径：新 P0 岗位复用同类原型或
        # 明确说明无可用原型）；未命中时 archetype_note 给出原因，coverage_report=None
        # 与该标记互证，不算要素缺失。
        "archetype_matched": bool(archetype),
        "profile_matched": {
            "name": profile_name,
            "rule": str(matched.get("rule") or "none"),
            "needs_confirmation": bool(matched.get("needs_confirmation")),
        },
        "step2_source_distribution": source_distribution,
    }
    v2["consultant_judgement"] = build_consultant_judgement(
        plan,
        classification,
        archetype=archetype,
        consultant=consultant,
        profile_context=profile_context,
        canonical_position=canonical_position,
        step1=step1,
        step2=step2,
        step3=step3,
        step4=step4,
        step5=step5,
        negative_rules=negative_rules,
        learning=learning,
    )
    assembly_trace.append(
        "资深顾问判断已生成：岗位诊断、搜索顺序、扩池阶梯、证据标准、客户校准与复盘应用"
    )
    v2["classification_trace"] = [*list(v2.get("classification_trace") or []), *assembly_trace[-1:]]
    if archetype:
        v2["classification_trace"].append(
            f"岗位原型已命中并消费：{archetype.get('archetype_id')}（source=kb_archetype，{archetype.get('matched_by') or 'match'}）"
        )
    else:
        v2["archetype_note"] = (
            "无可用岗位原型：知识库 seed_*.json 的标题职能词与客户兜底规则均未命中本岗位"
            "（原型匹配留痕见 classification_trace）；策略要素按 JD/顾问输入/LLM 推断生成，"
            "推断项标记待确认。若该岗位族将反复出现，建议复盘后沉淀新原型。"
        )
        v2["classification_trace"].append("原型管理闭环：archetype_matched=false，已显式说明无可用原型及原因")
    if consultant.get("consultant_answers"):
        v2["consultant_answers"] = str(consultant["consultant_answers"])[:800]
    return v2


# S4-3c-4（N6）：策略全要素消费检查 —— 命中原型的种子要素分层标签（顺序固定）。
_COVERAGE_POOL_LAYERS = (
    ("T1_competitor_device", "T1 竞对原厂"),
    ("T2_customer_OEM", "T2 客户整机厂"),
    ("T3_adjacent_unconfirmed", "T3 相邻池（未确认）"),
)


def _coverage_pool_layers(archetype: dict[str, Any]) -> list[tuple[str, str]]:
    pool = archetype.get("target_company_pool") if isinstance(archetype.get("target_company_pool"), dict) else {}
    layers = list(_COVERAGE_POOL_LAYERS)
    known = {key for key, _label in layers}
    for key, block in pool.items():
        if key in known or not isinstance(block, dict):
            continue
        tier_match = re.match(r"T[123]", str(key))
        tier = str(block.get("tier") or (tier_match.group(0) if tier_match else "T2"))
        layers.append((str(key), str(block.get("label") or f"{tier} {key}")))
    return layers


def _coverage_norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _coverage_name_hit(seed_name: str, candidates: list[str]) -> bool:
    """公司名双向包含匹配（“MPS”与“MPS（芯源系统）”视为同一家）。"""
    norm = _coverage_norm(seed_name)
    if not norm:
        return False
    for candidate in candidates:
        normalized = _coverage_norm(candidate)
        if normalized and (norm == normalized or norm in normalized or normalized in norm):
            return True
    return False


def _coverage_text_corpus(v2: dict[str, Any]) -> str:
    """策略对象的可检索文本（step1-5 + negative_rules），用于地点策略消费核对。"""
    parts: list[str] = []
    step1 = v2.get("step1_job_essence") if isinstance(v2.get("step1_job_essence"), dict) else {}
    parts.extend([step1.get("statement"), step1.get("value_chain_role")])
    for entry in v2.get("step2_target_pool") or []:
        if isinstance(entry, dict):
            parts.append(entry.get("rationale"))
    step3 = v2.get("step3_level_mapping") if isinstance(v2.get("step3_level_mapping"), dict) else {}
    parts.append(step3.get("calibration_rule"))
    parts.extend(step3.get("accepted_levels") or [])
    for group in v2.get("step4_keyword_groups") or []:
        if isinstance(group, dict):
            parts.extend([group.get("group"), group.get("targets")])
            parts.extend(group.get("terms") or [])
    step5 = v2.get("step5_expectation") if isinstance(v2.get("step5_expectation"), dict) else {}
    parts.append(step5.get("fallback_plan"))
    for rule in v2.get("negative_rules") or []:
        if isinstance(rule, dict):
            parts.extend([rule.get("type"), rule.get("rule")])
    return _coverage_norm("".join(str(part or "") for part in parts))


def build_coverage_report(archetype: dict[str, Any] | None, v2: dict[str, Any]) -> dict[str, Any] | None:
    """S4-3c-4（N6）：策略全要素消费检查（治 B4，防知识资产静默漏用）。

    对照命中原型的种子要素清单逐项核对 strategy_v2 是否消费：
    - T1/T2/T3 各层公司池：该层种子公司全部进入 step2_target_pool（双向包含匹配）→ 已消费；
      全部未进/仅部分进入 → 未使用并注明缺漏公司；
    - 地点策略 location_policy：策略首分句（如“杭州优先”）出现在策略对象文本
      （step1-5/negative_rules）→ 已消费；strategy_v2 schema 无地点策略落点时如实记为未使用
      （这是生成侧的既有缺陷，N6 只留痕不修复）；
    - 排除规则：种子 negative_rules 逐条，规则文本进入 strategy_v2.negative_rules → 已消费；
    - 有效关键词组：种子 keyword_groups 逐组，同名组进入 step4 或与 step4 词面有交集 → 已消费。

    种子未命中原型（无原型岗位）返回 None：coverage_report=None 留痕，不算缺失。
    要素清单只来自种子原型；restricted 层要素（禁挖名单/竞业约束）永远不进 unused 对外面。
    """
    if not isinstance(archetype, dict) or not str(archetype.get("archetype_id") or "").strip():
        return None
    consumed: list[str] = []
    unused: list[dict[str, str]] = []

    pool_names = [
        str(company.get("name") or "")
        for entry in v2.get("step2_target_pool") or []
        if isinstance(entry, dict)
        for company in entry.get("companies") or []
        if isinstance(company, dict) and str(company.get("name") or "").strip()
    ]
    seed_pool = archetype.get("target_company_pool") if isinstance(archetype.get("target_company_pool"), dict) else {}
    for key, label in _coverage_pool_layers(archetype):
        block = seed_pool.get(key) if isinstance(seed_pool.get(key), dict) else {}
        companies = [
            str(company.get("name") or "").strip()
            for company in block.get("companies") or []
            if isinstance(company, dict) and str(company.get("name") or "").strip()
        ]
        if not companies:
            continue
        missing = [name for name in companies if not _coverage_name_hit(name, pool_names)]
        if not missing:
            consumed.append(label)
        elif len(missing) == len(companies):
            unused.append(
                {
                    "element": label,
                    "reason": f"种子{label} {len(companies)} 家公司均未进入 step2 目标池（{'、'.join(companies[:4])}{'等' if len(companies) > 4 else ''}）",
                }
            )
        else:
            unused.append(
                {
                    "element": label,
                    "reason": f"种子{label}仅部分消费：{len(companies) - len(missing)}/{len(companies)} 进入 step2，缺 {'、'.join(missing[:6])}",
                }
            )

    policy = str(archetype.get("location_policy") or "").strip()
    if policy:
        clause = re.split(r"[；;。，,\n]", policy)[0].strip() or policy
        corpus = _coverage_text_corpus(v2)
        if _coverage_norm(clause) and _coverage_norm(clause) in corpus:
            consumed.append(clause)
        else:
            unused.append(
                {
                    "element": clause,
                    "reason": f"种子地点策略「{policy}」未进入 strategy_v2：schema 无地点策略落点（step1-5/negative_rules 均未消费），生成侧漏消费，N6 如实留痕",
                }
            )

    rule_texts = [
        _coverage_norm(rule.get("rule"))
        for rule in v2.get("negative_rules") or []
        if isinstance(rule, dict) and _coverage_norm(rule.get("rule"))
    ]
    for rule in archetype.get("negative_rules") or []:
        text = str(rule or "").strip()
        if not text:
            continue
        norm = _coverage_norm(text)
        if any(norm in candidate or candidate in norm for candidate in rule_texts):
            consumed.append(f"排除规则：{text}")
        else:
            unused.append(
                {
                    "element": f"排除规则：{text}",
                    "reason": "种子排除规则未进入 negative_rules（策略采用了其他来源的排除规则）",
                }
            )

    step4_groups = [group for group in v2.get("step4_keyword_groups") or [] if isinstance(group, dict)]
    step4_names = {_coverage_norm(group.get("group")) for group in step4_groups}
    step4_terms = [_coverage_norm(term) for group in step4_groups for term in group.get("terms") or [] if _coverage_norm(term)]
    for group in archetype.get("keyword_groups") or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("group") or "").strip()
        terms = [_coverage_norm(term) for term in group.get("terms") or [] if _coverage_norm(term)]
        if not name and not terms:
            continue
        label = f"关键词组 {name}" if name else "关键词组（未命名）"
        name_hit = bool(name) and _coverage_norm(name) in step4_names
        term_hit = any(term in candidate or candidate in term for term in terms for candidate in step4_terms)
        if name_hit or term_hit:
            consumed.append(label)
        else:
            unused.append(
                {
                    "element": label,
                    "reason": f"种子关键词组 {name or '（未命名）'} 未进入 step4：策略关键词组与该组词面无交集",
                }
            )

    total = len(consumed) + len(unused)
    return {
        "version": "n6_coverage_v1",
        "archetype_id": str(archetype.get("archetype_id") or ""),
        "consumed": consumed,
        "unused": unused,
        "coverage_rate": round(len(consumed) / total, 4) if total else 1.0,
        "element_count": total,
        "consumed_count": len(consumed),
    }


def _replay_query_atoms(query: Any) -> list[str]:
    text = " ".join(str(query or "").split()).lower()
    atoms = [text, *re.split(r"[\s、；;，,/｜|+]+", text)]
    return list(dict.fromkeys(_coverage_norm(atom) for atom in atoms if _coverage_norm(atom)))


def build_golden_candidate_replay(
    archetype: dict[str, Any] | None,
    query_plan: dict[str, Any],
) -> dict[str, Any] | None:
    """Replay a query grid against anonymized historically positive profiles."""
    if not isinstance(archetype, dict):
        return None
    candidates = [item for item in archetype.get("golden_candidates") or [] if isinstance(item, dict)]
    if not candidates:
        return None
    cells = [cell for cell in query_plan.get("cells") or [] if isinstance(cell, dict)]
    atom_cells = [(cell, _replay_query_atoms(cell.get("query"))) for cell in cells]
    covered_profiles: list[str] = []
    uncovered_profiles: list[str] = []
    profile_results: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, 1):
        profile_id = str(candidate.get("profile_id") or f"golden_{index}")
        signals = [
            _coverage_norm(signal)
            for signal in candidate.get("search_signals") or []
            if _coverage_norm(signal)
        ]
        matched_cells = [
            str(cell.get("cell_id") or "")
            for cell, atoms in atom_cells
            if any(
                atom == signal or atom in signal or signal in atom
                for atom in atoms
                for signal in signals
            )
        ]
        covered = bool(signals and matched_cells)
        (covered_profiles if covered else uncovered_profiles).append(profile_id)
        profile_results.append(
            {
                "profile_id": profile_id,
                "covered": covered,
                "matched_cell_count": len(matched_cells),
                "matched_cell_ids": matched_cells[:12],
                "outcome_label": str(candidate.get("outcome_label") or "historical_positive"),
            }
        )
    candidate_count = len(candidates)
    recall_rate = round(len(covered_profiles) / candidate_count, 4) if candidate_count else 1.0
    minimum_recall = max(0.0, min(float(archetype.get("golden_replay_min_recall") or 1.0), 1.0))
    return {
        "schema_version": "golden_candidate_replay_v1",
        "archetype_id": str(archetype.get("archetype_id") or ""),
        "evidence_scope": "anonymized_historical_positive_profiles",
        "candidate_count": candidate_count,
        "covered_count": len(covered_profiles),
        "recall_rate": recall_rate,
        "minimum_recall": minimum_recall,
        "passed": recall_rate >= minimum_recall,
        "uncovered_profile_ids": uncovered_profiles,
        "profiles": profile_results,
    }


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
    if value.get("consultant_judgement") is not None and not isinstance(value.get("consultant_judgement"), dict):
        errors.append("consultant_judgement 必须是对象")
    return not errors, errors


def extract_strategy_v2(metadata: Any) -> dict[str, Any] | None:
    """读取侧兼容：v1 旧 artifact（无 strategy_v2 键）返回 None，不崩。"""
    data = _loads(metadata, {})
    if not isinstance(data, dict):
        return None
    candidate = data.get("strategy_v2")
    return candidate if isinstance(candidate, dict) else None
