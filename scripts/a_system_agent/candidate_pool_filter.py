"""候选池确定性过滤（candidate_pool_filter capability）。

轻量分级过滤：优先复用当前有效的 Agent 人岗评估；无评估时再按岗位职能域的
硬性证据（简历原文 candidate_profiles）命中计数。输出 A/B/C 分级名单，并自动
排除禁挖名单（restricted 层 banned_companies）。

与 candidate_batch_assessment 的区别：本能力是纯确定性规则、不调 LLM、
毫秒级返回分级名单；批量评估是逐人 LLM 深评。二者互补：先过滤出 A/B 名单，
再对 A/B 逐人深评。

当前支持机械、软件、电力电子三个规则域。未知职能域会 fail-closed，不得套用机械
默认词库。Agent 评估等级保留原语义：A -> A-强、B -> B-中、C -> C-需确认、
D -> D-暂缓；D-暂缓不是“无证据”，不能据此自动停止推进。
"""

from __future__ import annotations
import json
import re
import sqlite3
from typing import Any

# 简历原文硬性证据关键词（按岗位类型可扩展；机械/精密设备岗默认集）
HARD_KEYS_DEFAULT = [
    "微米", "亚微米", "精密设备", "精密机械", "精密运动", "光刻", "晶圆", "半导体设备",
    "有限元", "ansys", "abaqus", "模态", "振动", "热变形", "热分析", "紧固件",
    "直线电机", "光栅尺", "丝杠", "导轨", "轴承", "结构刚性", "刚度", "整机",
    "封测", "测试机", "分选机", "探针台", "键合", "固晶", "贴片", "CMP", "清洗设备",
]
# 精密 / 半导体 / 仿真 / 运动部件 分组
PRECISION_KEYS = ["微米", "亚微米", "精密机械", "精密运动", "精密设备"]
SEMI_KEYS = ["光刻", "晶圆", "半导体设备", "封测", "测试机", "分选机", "探针台", "键合", "贴片", "固晶", "CMP", "清洗设备"]
FEA_KEYS = ["有限元", "ansys", "abaqus", "模态", "振动", "热变形", "热分析", "刚度"]
MOTION_KEYS = ["直线电机", "光栅尺", "丝杠", "导轨", "轴承"]

# 简历原职位强排除词（非机械岗）
EXCL_TITLE = [
    "电气", "软件", "嵌入式", "硬件", "测试", "销售", "市场", "投资", "采购",
    "品质", "质量", "客户", "专利", "IT", "行政", "人力", "运营", "供应链",
    "工艺工程师", "封装设计", "设备工程师", "机器学习", "产品经理", "技术支持",
    "财务", "法务", "售后",
    # 管理层级/职能明显不匹配，或资历明显超出“高级工程师”层级
    "副总", "总经理", "总监", "部长", "主任", "装配", "报价", "经理",
    "manager", "ceo", "director",
]


# 自动化/设备控制软件岗的硬证据词与排除词。
SOFTWARE_HARD_KEYS = [
    "软件", "C++", "C/C++", "C#", "Python", "Java", "嵌入式", "运动控制", "伺服",
    "EtherCAT", "PLC", "RTOS", "实时", "多线程", "上位机", "算法", "控制", "驱动",
    "固件", "通信", "总线", "CAN", "Ethernet", "开发", "编程", "自动化", "设备软件",
    "控制系统", "视觉", "Linux", "Windows", "数据库", "界面", "Qt", "工控",
]
SOFTWARE_EXCL_TITLE = [
    "机械", "结构", "电气", "硬件", "工艺", "质量", "品质", "销售", "市场", "采购",
    "财务", "法务", "行政", "人力", "售后", "客户", "产品经理", "技术支持", "设备工程师",
    "测试", "投资", "运营", "供应链", "专利", "机器学习",
    "副总", "总经理", "总监", "部长", "主任", "经理", "manager", "ceo", "director",
]


# VPD/VRM/多相 Buck 模块电源岗。刻意不加入“微米、有限元、导轨、振动”等机械词，
# 防止精密机械履历仅凭通用仿真词被评成电源岗位 A/B。
POWER_HARD_KEYS = [
    "VPD", "Vertical Power Delivery", "垂直供电", "VRM", "多相Buck", "多相 Buck",
    "multiphase", "TLVR", "DrMOS", "SPS", "Power Stage", "模块电源", "服务器电源",
    "CPU供电", "GPU供电", "ASIC供电", "xPU供电", "DC/DC", "DC-DC", "DCDC",
    "负载瞬态", "load transient", "均流", "current sharing", "环路稳定", "COT",
    "磁集成", "TLVR电感", "一体成型电感", "VR电感", "封装寄生", "SIMPLIS",
    "LTspice", "电力电子", "EVT", "DVT", "PVT",
]
POWER_CORE_KEYS = [
    "VPD", "Vertical Power Delivery", "垂直供电", "VRM", "多相Buck", "多相 Buck",
    "multiphase", "TLVR", "模块电源", "服务器电源", "CPU供电", "GPU供电",
    "ASIC供电", "xPU供电",
]
POWER_CONTROL_KEYS = [
    "多相Buck", "多相 Buck", "multiphase", "TLVR", "负载瞬态", "load transient",
    "均流", "current sharing", "环路稳定", "COT", "SIMPLIS", "LTspice",
]
POWER_COMPONENT_KEYS = [
    "DrMOS", "SPS", "Power Stage", "磁集成", "TLVR电感", "一体成型电感",
    "VR电感", "封装寄生",
]
POWER_EXCL_TITLE = [
    "机械", "结构", "软件", "算法", "销售", "市场", "采购", "质量", "品质",
    "财务", "法务", "行政", "人力", "运营", "供应链", "产品经理", "技术支持",
    "机器学习", "模拟IC", "FAE",
]


SUPPORTED_DOMAINS = frozenset({"mechanical", "software", "power"})


def _contains_any(text: str, tokens: tuple[str, ...] | list[str]) -> bool:
    lowered = str(text or "").lower()
    return any(token.lower() in lowered for token in tokens)


def job_filter_domain(job_title: str, job_context: str = "") -> str | None:
    """按岗位标题与 JD 判定可自动分级的职能域；无法可靠识别时返回 None。"""
    title = str(job_title or "")
    context = str(job_context or "")
    software_tokens = ("软件", "嵌入式", "算法", "C++", "C/C++", "Python", "Java", "上位机", "固件")
    power_tokens = (
        "电源", "电力电子", "功率电子", "VPD", "Vertical Power Delivery", "VRM",
        "多相Buck", "多相 Buck", "TLVR", "DrMOS", "模块电源", "垂直供电",
    )
    mechanical_tokens = ("机械", "结构", "精密")
    unsupported_title_tokens = ("电气", "失效分析", "质量", "销售", "市场", "采购", "财务", "人力")

    # 标题是更强的职能信号。“电源软件工程师”仍归软件；“电源结构工程师”归机械。
    if _contains_any(title, software_tokens):
        return "software"
    if _contains_any(title, mechanical_tokens):
        return "mechanical"
    if _contains_any(title, power_tokens):
        return "power"
    if _contains_any(title, unsupported_title_tokens):
        return None

    # 通用标题（如“研发专家”）才用 JD 补判。电源域要求至少一个高特异性词；
    # 软件/机械则沿用明确职能词，避免“开发”这种通用动作误判软件。
    if _contains_any(context, power_tokens):
        return "power"
    if _contains_any(context, software_tokens):
        return "software"
    if _contains_any(context, mechanical_tokens):
        return "mechanical"
    return None


_INTAKE_MISMATCH_TOKENS: dict[str, tuple[str, ...]] = {
    # 只放最明确的职能不符词；管理层级/资历过高、装配、报价、设备工程师、测试等
    # 边界情形一律留给人工复核，避免入库阶段误杀。
    "mechanical": (
        "电气", "软件", "嵌入式", "硬件", "测试", "销售", "市场", "采购", "质量", "品质",
        "财务", "法务", "行政", "人力", "售后", "客户", "产品经理", "技术支持",
        "工艺工程师", "封装设计", "机器学习", "专利", "投资", "运营", "供应链",
    ),
    "software": (
        "机械", "结构", "电气", "硬件", "工艺", "质量", "品质", "销售", "市场", "采购",
        "财务", "法务", "行政", "人力", "售后", "客户", "产品经理", "技术支持",
        "专利", "投资", "运营", "供应链",
    ),
}


def intake_mismatch_verdict(job_title: str, candidate_title: str) -> dict[str, Any] | None:
    """寻访入库预筛：候选当前职位与岗位职能明确不符时返回停止裁定，否则 None。

    返回裁定时表示应把这条关系直接写成 H5 初筛不通过（方向不符），而不是
    干净进入待复核。仅做保守的“方向不符”，不做资历/薪资/地点判断。
    """
    domain = job_filter_domain(job_title)
    title = str(candidate_title or "").strip()
    if not domain or not title:
        return None
    hits = [token for token in _INTAKE_MISMATCH_TOKENS.get(domain, ()) if token in title]
    if not hits:
        return None
    return {
        "stage": "H5 最近寻访/初筛不通过",
        "flow_bucket": "最近寻访",
        "stop_reason": "direction_mismatch",
        "reason": f"寻访入库预筛：方向不符（{'、'.join(hits)}）",
    }


def _exp_years(exp: Any) -> int | None:
    if not exp:
        return None
    m = re.search(r"(\d+)\s*年", str(exp))
    return int(m.group(1)) if m else None


def _parse_expectation(summary: str) -> tuple[int | None, str | None]:
    """从 profile_summary 的"求职意向"区段解析期望薪资上限与期望城市。

    格式形如 "求职意向 | 职位 | 15-20k×14薪 | 成都 | 行业"（竖线或换行分隔）。
    薪资段取数字上限（"15-20k"→20，"20k"→20）；城市段取逗号/顿号分隔的第一个。
    找不到"求职意向"区段或对应段时返回 (None, None)。
    """
    if not summary:
        return None, None
    text = str(summary)
    idx = text.find("求职意向")
    if idx < 0:
        return None, None
    segs = [s.strip() for s in re.split(r"[|\n]", text[idx:]) if s.strip()]
    salary_k_max: int | None = None
    salary_idx = -1
    for i, seg in enumerate(segs):
        m = re.search(r"(\d{1,4})\s*[-~～—至]+\s*(\d{1,4})\s*[kK]", seg) or \
            re.search(r"(\d{1,4})\s*[kK]", seg)
        if m:
            salary_k_max = int(m.group(2)) if m.lastindex == 2 else int(m.group(1))
            salary_idx = i
            break
    city: str | None = None
    if salary_idx >= 0 and salary_idx + 1 < len(segs):
        cand = re.split(r"[、,，/]", segs[salary_idx + 1], maxsplit=1)[0].strip()
        if cand and cand not in ("全部行业", "行业", "不限", "全国各地"):
            city = cand
    return salary_k_max, city


def _candidate_profiles(db_path: str) -> dict[int, dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT candidate_id, candidate_name, candidate_company, position, "
            "education_level, seniority, profile_summary FROM candidate_profiles"
        ).fetchall()
        return {int(r["candidate_id"]): dict(r) for r in rows}
    finally:
        conn.close()


def _current_assessments(conn: sqlite3.Connection, job_id: int) -> dict[int, dict[str, Any]]:
    """读取完成态且 is_current=1 的人岗评估；旧库缺表时安全退化为空。"""
    try:
        rows = conn.execute(
            """
            SELECT a.job_candidate_id,a.fit_score,a.fit_level,a.recommendation,
                   a.confidence,a.evidence_coverage,a.strengths_json,a.gaps_json,
                   a.created_at
            FROM agent_candidate_assessments a
            JOIN agent_runs r ON r.run_id=a.run_id
            JOIN job_candidates jc ON jc.id=a.job_candidate_id
            WHERE jc.job_id=? AND a.is_current=1 AND r.status='completed'
            ORDER BY a.id DESC
            """,
            (job_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    assessments: dict[int, dict[str, Any]] = {}
    for row in rows:
        relation_id = int(row["job_candidate_id"])
        assessments.setdefault(relation_id, dict(row))
    return assessments


def _json_text_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    values: list[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
        elif isinstance(item, dict):
            text = next(
                (str(item.get(key) or "").strip() for key in ("evidence", "reason", "text", "criterion") if item.get(key)),
                "",
            )
            if text:
                values.append(text)
    return values


def _assessment_grade(fit_level: Any, fit_score: Any) -> str:
    level = str(fit_level or "").strip().upper()
    if level.startswith("A"):
        return "A-强"
    if level.startswith("B"):
        return "B-中"
    if level.startswith("C"):
        return "C-需确认"
    if level.startswith("D"):
        return "D-暂缓"
    try:
        score = int(fit_score)
    except (TypeError, ValueError):
        score = 0
    if score >= 85:
        return "A-强"
    if score >= 70:
        return "B-中"
    if score >= 50:
        return "C-需确认"
    return "D-暂缓"


def _banned_companies(db_path: str, client: str, kb_dir: str | None = None) -> list[str]:
    """读取 restricted 层禁挖名单（banned_companies 白名单）。"""
    try:
        from . import knowledge_base
        info, _trace = knowledge_base.load_restricted_constraints(client, kb_dir=kb_dir)
        if not info:
            return []
        return [str(x).strip() for x in (info.get("constraints") or {}).get("banned_companies") or [] if str(x or "").strip()]
    except Exception:
        return []


def filter_job_candidates(
    db_path: str,
    job_id: int,
    *,
    client: str = "",
    hard_keys: list[str] | None = None,
    domain: str | None = None,
    max_candidates: int = 2000,
    max_salary_k: int | None = None,
    cities: list[str] | None = None,
) -> dict[str, Any]:
    """按岗位职能域的硬证据过滤候选池，返回分级名单。

    domain 为 "mechanical"、"software" 或 "power"；缺省时按岗位标题与 JD 自动识别。
    未识别域会抛出 ValueError，绝不回落机械默认词库。
    max_salary_k: 期望月薪上限(K)，候选人期望薪资上限超过该值 → D-期望超限。
    cities: 期望城市关键词，命中任一即保留；不命中 → D-城市不符。
    两者默认 None 均不过滤，向后兼容。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job_row is None:
            raise LookupError(f"岗位不存在: {job_id}")
        job_title = str(job_row["title"] or "")
        job_columns = set(job_row.keys())
        job_context = " ".join(
            str(job_row[key] or "")
            for key in ("summary", "hard_requirements", "ability_keywords", "search_words", "exclusions")
            if key in job_columns
        )
        domain_aliases = {"power_electronics": "power", "power-electronics": "power"}
        if domain:
            domain = domain_aliases.get(str(domain).strip().lower(), str(domain).strip().lower())
        else:
            domain = job_filter_domain(job_title, job_context)
        if domain not in SUPPORTED_DOMAINS:
            raise ValueError(f"未识别或不支持岗位职能域，已停止自动分级: {job_title or job_id}")
        if domain == "software":
            keys = hard_keys or SOFTWARE_HARD_KEYS
            excl_tokens = SOFTWARE_EXCL_TITLE
        elif domain == "mechanical":
            keys = hard_keys or HARD_KEYS_DEFAULT
            excl_tokens = EXCL_TITLE
        else:
            keys = hard_keys or POWER_HARD_KEYS
            excl_tokens = POWER_EXCL_TITLE
        rows = conn.execute(
            """
            SELECT jc.id, jc.person_id, jc.clean_stage, jc.flow_bucket, jc.source_candidate_id,
                   jc.updated_at, p.display_name, p.current_company, p.current_title,
                   p.city, p.education, p.experience
            FROM job_candidates jc JOIN people p ON p.id=jc.person_id
            WHERE jc.job_id=?
            ORDER BY jc.updated_at DESC, jc.id DESC
            """,
            (job_id,),
        ).fetchall()
        assessments = _current_assessments(conn, job_id)
    finally:
        conn.close()

    profiles = _candidate_profiles(db_path)
    banned = _banned_companies(db_path, client) if client else []

    results: list[dict[str, Any]] = []
    skipped_stage_total = 0
    for r in rows:
        stage = r["clean_stage"] or ""
        if "初筛不通过" in stage or "已触达" in stage or "已申请" in stage:
            skipped_stage_total += 1
            continue
        co = r["current_company"] or ""
        # 禁挖排除
        if banned and any(b in co for b in banned):
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "禁挖", "score": 0, "hard_hits": [],
                "grade_source": "restricted_client", "reason": f"禁挖名单:{'、'.join(banned)}",
            })
            continue
        prof = profiles.get(int(r["source_candidate_id"])) if r["source_candidate_id"] else None
        salary_k_max, exp_city = _parse_expectation(str((prof or {}).get("profile_summary") or ""))
        if max_salary_k and salary_k_max is not None and salary_k_max > max_salary_k:
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "D-期望超限", "score": 0, "hard_hits": [], "grade_source": "constraint",
                "reason": f"期望 {salary_k_max}K 超预算上限 {max_salary_k}K",
            })
            continue
        if cities and exp_city and not any(kw in exp_city or exp_city in kw for kw in cities):
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "D-城市不符", "score": 0, "hard_hits": [], "grade_source": "constraint",
                "reason": f"期望城市 {exp_city} 不在允许列表 [{', '.join(cities)}]",
            })
            continue

        assessment = assessments.get(int(r["id"]))
        if assessment is not None:
            score = int(assessment.get("fit_score") or 0)
            fit_level = str(assessment.get("fit_level") or "")
            strengths = _json_text_list(assessment.get("strengths_json"))
            gaps = _json_text_list(assessment.get("gaps_json"))
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": _assessment_grade(fit_level, score), "score": score,
                "hard_hits": strengths[:10], "reason": f"当前有效 Agent 评估：{score} 分 / {fit_level or '未标级'}",
                "grade_source": "agent_assessment", "assessment_fit_score": score,
                "assessment_fit_level": fit_level, "assessment_recommendation": assessment.get("recommendation") or "",
                "assessment_confidence": assessment.get("confidence"),
                "assessment_evidence_coverage": assessment.get("evidence_coverage"),
                "assessment_strengths": strengths, "assessment_gaps": gaps,
                "assessment_created_at": assessment.get("created_at") or "",
            })
            continue

        if not prof:
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "D-无画像", "score": 0, "hard_hits": [],
                "grade_source": "deterministic_rule", "reason": "无 candidate_profiles 画像",
            })
            continue
        cur_title = str(r["current_title"] or "").strip()
        # 画像 position 字段在部分入库批次里被统一写成目标岗位名（机械岗写“机械高级
        # 工程师”、软件岗写“自动化软件高级工程师”），既不能用于排除，也不能用于证据。
        # 排除与证据只信任 people.current_title + 简历摘要 + 当前公司。
        title_text = cur_title.lower()
        txt = " ".join([cur_title, str(prof.get("profile_summary") or ""), co]).lower()
        hard_hits = [k for k in keys if k.lower() in txt]
        excl_title = [k for k in excl_tokens if k.lower() in title_text]
        exp_n = _exp_years(r["experience"]) or _exp_years(prof.get("seniority"))
        edu = r["education"] or ""
        score = len(hard_hits) * 10
        if edu in ("硕士", "博士"):
            score += 8
        elif edu == "本科":
            score += 5
        if exp_n is not None and exp_n >= 7:
            score += 8
        elif exp_n is not None and exp_n >= 4:
            score += 3

        prec = [k for k in PRECISION_KEYS if k in hard_hits]
        semi = [k for k in SEMI_KEYS if k in hard_hits]
        fea = [k for k in FEA_KEYS if k in hard_hits]
        mot = [k for k in MOTION_KEYS if k in hard_hits]
        hit_set = {hit.lower() for hit in hard_hits}
        power_core = [k for k in POWER_CORE_KEYS if k.lower() in hit_set]
        power_control = [k for k in POWER_CONTROL_KEYS if k.lower() in hit_set]
        power_component = [k for k in POWER_COMPONENT_KEYS if k.lower() in hit_set]

        if excl_title:
            grade = "X-排除"
            reason = f"当前/原职位不匹配:{'、'.join(excl_title)}"
        elif domain == "mechanical" and prec and (semi or mot) and fea:
            grade = "A-核心"
            reason = "精密设备+仿真+半导体/运动部件全占"
        elif domain == "software" and len(hard_hits) >= 5 and score >= 60:
            grade = "A-核心"
            reason = f"软件硬证据{len(hard_hits)}项且学历/经验充分"
        elif domain == "power" and power_core and len(hard_hits) >= 5 and power_control and power_component:
            grade = "A-核心"
            reason = "电源核心架构+控制/建模+器件/磁件证据完整"
        elif domain == "power" and power_core and len(hard_hits) >= 4:
            grade = "A-强"
            reason = f"电力电子核心证据{len(hard_hits)}项"
        elif domain == "power" and power_core and len(hard_hits) >= 2:
            grade = "B-中"
            reason = f"电力电子核心证据{len(hard_hits)}项，关键闭环待核验"
        elif domain == "power" and hard_hits:
            grade = "C-弱"
            reason = f"仅命中电力电子辅助证据{len(hard_hits)}项"
        elif domain == "power":
            grade = "D-无证据"
            reason = "简历无 VPD/VRM/多相 Buck 等岗位硬证据"
        elif len(hard_hits) >= 4 and score >= 50:
            grade = "A-强"
            reason = f"硬证据{len(hard_hits)}项"
        elif len(hard_hits) >= 2:
            grade = "B-中"
            reason = f"硬证据{len(hard_hits)}项"
        elif hard_hits:
            grade = "C-弱"
            reason = f"硬证据{len(hard_hits)}项"
        else:
            grade = "D-无证据"
            reason = "简历无岗位硬证据"
        results.append({
            "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
            "city": r["city"] or "", "education": edu, "experience": r["experience"] or "",
            "stage": stage, "grade": grade, "score": score, "hard_hits": hard_hits[:10],
            "grade_source": "deterministic_rule", "reason": reason,
        })

    results.sort(key=lambda x: -x["score"])
    limit = max(0, int(max_candidates))
    returned = results[:limit]
    grade_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for item in results:
        grade = str(item.get("grade") or "未知")
        source = str(item.get("grade_source") or "unknown")
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        source_counts[source] = source_counts.get(source, 0) + 1
    coverage_note = (
        f"岗位共有 {len(rows)} 条人岗关系；排除 {skipped_stage_total} 条已停止/已触达/已申请关系后，"
        f"已分析全部 {len(results)} 条待分级关系"
    )
    if len(returned) < len(results):
        coverage_note += f"；受 limit 限制仅返回排序前 {len(returned)} 条明细"
    else:
        coverage_note += f"；已返回全部 {len(returned)} 条明细"
    return {
        "job_id": job_id,
        "client": client,
        "job_title": job_title,
        "domain": domain,
        "supported": True,
        "total": len(results),
        "pool_total": len(rows),
        "eligible_total": len(results),
        "analyzed_total": len(results),
        "returned_total": len(returned),
        "skipped_stage_total": skipped_stage_total,
        "truncated": len(returned) < len(results),
        "coverage_note": coverage_note,
        "grade_counts": grade_counts,
        "source_counts": source_counts,
        "candidates": returned,
    }


def format_grade_list(result: dict[str, Any]) -> str:
    """把过滤结果格式化成可读名单文本，明确区分“可推进”与“建议停止推进”。"""
    candidates = list(result.get("candidates") or [])
    keep_grades = ("A-核心", "A-强", "B-中", "C-弱")
    review_grades = ("C-需确认", "D-暂缓")
    stop_grades = ("X-排除", "禁挖", "D-无证据", "D-无画像", "D-期望超限", "D-城市不符")
    grade_counts = dict(result.get("grade_counts") or {})
    if not grade_counts:
        for candidate in candidates:
            grade = str(candidate.get("grade") or "未知")
            grade_counts[grade] = grade_counts.get(grade, 0) + 1
    keep_count = sum(int(grade_counts.get(grade) or 0) for grade in keep_grades)
    review_count = sum(int(grade_counts.get(grade) or 0) for grade in review_grades)
    stop_count = sum(int(grade_counts.get(grade) or 0) for grade in stop_grades)
    lines = [f"## 候选池分级名单（本次分析 {result['total']} 人）"]
    if result.get("coverage_note"):
        lines.append(str(result["coverage_note"]))
    lines.append(f"可推进 {keep_count} 人；待人工判断 {review_count} 人；建议停止推进 {stop_count} 人。\n")

    def _grade_block(grade: str, group: list[dict[str, Any]]) -> None:
        total = int(grade_counts.get(grade) or len(group))
        if total <= 0:
            return
        if len(group) < total:
            lines.append(f"### {grade}（共 {total} 人，本次展示 {len(group)} 人）")
        else:
            lines.append(f"### {grade}（{total} 人）")
        for c in group:
            evidence = "、".join(c["hard_hits"][:6]) if c["hard_hits"] else ""
            if c.get("grade_source") == "agent_assessment":
                ev = str(c.get("reason") or "当前有效 Agent 评估")
                if evidence:
                    ev += f"；优势:{evidence}"
            else:
                ev = evidence or c["reason"]
            lines.append(
                f"- {c['name']} | {c['company'][:20]} | {c['title'][:16]} "
                f"| {c['city'][:6]} | {c['education']}/{c['experience'][:6]} | 证据:{ev}"
            )

    lines.append("## 可推进")
    for grade in keep_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    lines.append("\n## 待人工判断")
    for grade in review_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    lines.append("\n## 建议停止推进")
    for grade in stop_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    return "\n".join(lines)
