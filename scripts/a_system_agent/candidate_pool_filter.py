"""候选池确定性过滤（candidate_pool_filter capability）。

轻量分级过滤：按岗位硬性证据（简历原文 candidate_profiles）命中计数，
输出 A/B/C 分级名单，自动排除禁挖名单（restricted 层 banned_companies）。

与 candidate_batch_assessment 的区别：本能力是纯确定性规则、不调 LLM、
毫秒级返回分级名单；批量评估是逐人 LLM 深评。二者互补：先过滤出 A/B 名单，
再对 A/B 逐人深评。

机械/软件分级口径：
- A-核心：精密证据≥1 且 (半导体 或 运动部件) ≥1 且 仿真≥1，且本科+/经验≥7
- A-强：硬证据≥4 且 本科+/经验≥7
- B-中：硬证据≥2
- C-弱：硬证据 1
- D-无证据：0
- X-排除：原职位/当前职位非机械岗，或管理层级明显超出高级工程师（电气/软件/测试/销售/
  质量/产品经理/副总/总监/经理/部长/主任/装配/报价等）
- 禁挖：命中 restricted.banned_companies 的公司一律剔除（source=restricted_client）

电源岗使用独立证据模型：A/B 必须含 VPD/VRM/TLVR/DrMOS/多相 Buck 等
直接证据；通用“电源/硬件/Ansys”只能作为弱辅助证据，不能单独进入 A/B。
未支持的职能域失败关闭，绝不静默回落到机械规则。
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


POWER_DIRECT_KEYS = [
    "VPD", "Vertical Power Delivery", "VRM", "TLVR", "多相Buck", "多相 Buck",
    "multiphase buck", "DrMOS", "Smart Power Stage", "Power Stage", "SPS",
    "CPU供电", "GPU供电", "ASIC供电", "xPU供电", "CPU/GPU供电",
]
POWER_SUPPORT_KEYS = [
    "模块电源", "服务器电源", "负载瞬态", "load transient", "均流", "current sharing",
    "环路稳定", "loop stability", "磁集成", "TLVR电感", "VR电感", "封装寄生",
    "一体成型电感", "SIMPLIS", "LTspice", "EVT", "DVT", "PVT",
]
POWER_ADJACENT_KEYS = [
    "电力电子", "Buck", "降压", "电源", "硬件", "磁件", "电感", "热设计",
    "可靠性", "Ansys",
]
POWER_EXCL_TITLE = [
    "机械", "结构", "软件", "测试", "销售", "市场", "采购", "品质", "质量",
    "财务", "法务", "行政", "人力", "运营", "供应链", "产品经理", "项目经理",
    "技术支持", "技术市场", "sales", "marketing", "hr",
]
POWER_REVIEW_ONLY_TITLE = ("FAE", "现场应用", "应用工程师")

SUPPORTED_FILTER_DOMAINS = {"mechanical", "software", "power"}


class UnsupportedFilterDomainError(ValueError):
    """Raised when a job has no auditable deterministic filter model."""


def job_filter_domain(job_title: str) -> str | None:
    """按岗位名判定可自动分级/批量停止的职能域；不支持返回 None。"""
    title = str(job_title or "").strip()
    normalized = title.lower()
    if any(token in title for token in ("技术市场", "市场经理", "销售")):
        return None
    if any(token in title for token in ("软件", "嵌入式", "算法", "C++", "C/C++", "开发", "Python", "Java")):
        return "software"
    if any(token in title for token in ("机械", "结构", "精密")):
        return "mechanical"
    if title == "电源专家" or any(token in normalized for token in ("vpd", "vrm", "tlvr", "drmos")):
        return "power"
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

    domain 为 "mechanical"、"software" 或 "power"；缺省时按 jobs.title 自动识别。
    max_salary_k: 期望月薪上限(K)，候选人期望薪资上限超过该值 → D-期望超限。
    cities: 期望城市关键词，命中任一即保留；不命中 → D-城市不符。
    两者默认 None 均不过滤，向后兼容。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    profiles = _candidate_profiles(db_path)
    banned = _banned_companies(db_path, client) if client else []
    try:
        if not domain:
            job_row = conn.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()
            domain = job_filter_domain(str(job_row["title"] or "") if job_row else "")
        if domain not in SUPPORTED_FILTER_DOMAINS:
            raise UnsupportedFilterDomainError(
                f"岗位 {job_id} 暂无受支持的确定性筛选模型，已拒绝套用其他岗位规则"
            )
        if domain == "software":
            keys = hard_keys or SOFTWARE_HARD_KEYS
            excl_tokens = SOFTWARE_EXCL_TITLE
        elif domain == "power":
            keys = hard_keys or [*POWER_DIRECT_KEYS, *POWER_SUPPORT_KEYS, *POWER_ADJACENT_KEYS]
            excl_tokens = POWER_EXCL_TITLE
        else:
            keys = hard_keys or HARD_KEYS_DEFAULT
            excl_tokens = EXCL_TITLE
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
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for r in rows:
        stage = r["clean_stage"] or ""
        if "初筛不通过" in stage or "已触达" in stage or "已申请" in stage:
            continue
        co = r["current_company"] or ""
        # 禁挖排除
        if banned and any(b in co for b in banned):
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "禁挖", "score": 0, "hard_hits": [], "reason": f"禁挖名单:{'、'.join(banned)}",
            })
            continue
        prof = profiles.get(int(r["source_candidate_id"])) if r["source_candidate_id"] else None
        if not prof:
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "U-待补画像", "score": 0, "hard_hits": [],
                "reason": "无 candidate_profiles 画像，证据不足，禁止自动淘汰",
            })
            continue
        salary_k_max, exp_city = _parse_expectation(str(prof.get("profile_summary") or ""))
        if max_salary_k and salary_k_max is not None and salary_k_max > max_salary_k:
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "D-期望超限", "score": 0, "hard_hits": [],
                "reason": f"期望 {salary_k_max}K 超预算上限 {max_salary_k}K",
            })
            continue
        if cities and exp_city and not any(kw in exp_city or exp_city in kw for kw in cities):
            results.append({
                "id": r["id"], "name": r["display_name"], "company": co, "title": r["current_title"] or "",
                "city": r["city"] or "", "education": r["education"] or "", "experience": r["experience"] or "",
                "stage": stage, "grade": "D-城市不符", "score": 0, "hard_hits": [],
                "reason": f"期望城市 {exp_city} 不在允许列表 [{', '.join(cities)}]",
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
        power_direct = [k for k in POWER_DIRECT_KEYS if k.lower() in txt] if domain == "power" else []
        power_support = [k for k in POWER_SUPPORT_KEYS if k.lower() in txt] if domain == "power" else []
        power_adjacent = [k for k in POWER_ADJACENT_KEYS if k.lower() in txt] if domain == "power" else []
        score = (
            len(power_direct) * 20 + len(power_support) * 8 + len(power_adjacent) * 2
            if domain == "power"
            else len(hard_hits) * 10
        )
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

        if excl_title:
            grade = "X-排除"
            reason = f"当前/原职位不匹配:{'、'.join(excl_title)}"
        elif domain == "power" and any(token.lower() in title_text for token in POWER_REVIEW_ONLY_TITLE):
            grade = "C-弱"
            reason = "应用/FAE 角色需核验是否承担模块电源设计责任"
        elif domain == "power" and (
            len(power_direct) >= 2 or (power_direct and len(power_support) >= 3)
        ):
            grade = "A-核心"
            reason = f"电源直接证据{len(power_direct)}项、支撑证据{len(power_support)}项"
        elif domain == "power" and power_direct and power_support:
            grade = "A-强"
            reason = f"电源直接证据{len(power_direct)}项并有项目支撑"
        elif domain == "power" and power_direct:
            grade = "B-中"
            reason = f"电源直接证据{len(power_direct)}项，需补充项目闭环证据"
        elif domain == "power" and (len(power_support) >= 2 or len(power_adjacent) >= 3):
            grade = "C-弱"
            reason = "仅有相邻电源证据，需核验 VPD/VRM/TLVR/DrMOS 实际项目"
        elif domain == "mechanical" and prec and (semi or mot) and fea:
            grade = "A-核心"
            reason = "精密设备+仿真+半导体/运动部件全占"
        elif domain == "software" and len(hard_hits) >= 5 and score >= 60:
            grade = "A-核心"
            reason = f"软件硬证据{len(hard_hits)}项且学历/经验充分"
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
            "stage": stage, "grade": grade, "score": score, "hard_hits": hard_hits[:10], "reason": reason,
        })

    results.sort(key=lambda x: -x["score"])
    return {"job_id": job_id, "client": client, "total": len(results), "candidates": results[:max_candidates]}


def format_grade_list(result: dict[str, Any]) -> str:
    """把过滤结果格式化成可读名单文本，区分优先复核、证据不足和明确排除。"""
    candidates = list(result.get("candidates") or [])
    review_grades = ("A-核心", "A-强", "B-中", "C-弱")
    evidence_pending_grades = ("D-无证据", "U-待补画像")
    stop_grades = ("X-排除", "禁挖", "D-期望超限", "D-城市不符")
    review_count = sum(1 for c in candidates if c.get("grade") in review_grades)
    evidence_pending_count = sum(1 for c in candidates if c.get("grade") in evidence_pending_grades)
    stop_count = sum(1 for c in candidates if c.get("grade") in stop_grades)
    lines = [f"## 候选池分级名单（共 {result['total']} 人）"]
    lines.append(
        f"建议优先复核 {review_count} 人；证据不足待补充 {evidence_pending_count} 人；"
        f"有明确排除证据 {stop_count} 人。\n"
    )

    def _grade_block(grade: str, group: list[dict[str, Any]]) -> None:
        if not group:
            return
        lines.append(f"### {grade}（{len(group)} 人）")
        for c in group:
            ev = "、".join(c["hard_hits"][:6]) if c["hard_hits"] else c["reason"]
            lines.append(
                f"- {c['name']} | {c['company'][:20]} | {c['title'][:16]} "
                f"| {c['city'][:6]} | {c['education']}/{c['experience'][:6]} | 证据:{ev}"
            )

    lines.append("## 建议优先复核")
    for grade in review_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    lines.append("\n## 证据不足，暂不自动裁决")
    for grade in evidence_pending_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    lines.append("\n## 有明确排除证据")
    for grade in stop_grades:
        _grade_block(grade, [c for c in candidates if c.get("grade") == grade])
    return "\n".join(lines)
