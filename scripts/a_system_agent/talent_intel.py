"""人才情报（薪酬对标 + 人才地图）只读查询。

数据源为生产库（A_SYSTEM_DB 环境变量优先，缺省 talent_system_v3 生产库），
一律 mode=ro 只读连接；库缺失/无表/读取失败一律降级返回 None，不抛错、
不影响主流程（与 company_kb 同一降级口径）。

- salary_benchmark：按方向关键词在 agent_candidate_recalls 召回简历的
  profile_text 前 800 字内解析月薪（k），输出分位数统计与样本公司分布。
- talent_map：先按关键词在 company_knowledge 反查目标公司（行业/技术栈/
  产品线/主营业务 LIKE），再用召回简历按公司名归集，统计每家公司的人数、
  经验中位、学历分布、城市分布与月薪范围。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .company_kb import _db_path  # 库路径解析单一来源：显式参数 > A_SYSTEM_DB > 缺省生产库

_RECALLS_TABLE = "agent_candidate_recalls"
_KNOWLEDGE_TABLE = "company_knowledge"

# 月薪解析规则（单位 k，按优先级依次尝试，命中即停）
_SALARY_RULE_MONTH_K = re.compile(r"(\d+(?:\.\d+)?)\s*k\s*[·×*]\s*(\d+)薪", re.I)  # 1) "50k · 15薪" → 月薪 k
_SALARY_RULE_RANGE_K = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*k", re.I)  # 2) "25-35k" → 中值
_SALARY_RULE_PLAIN_K = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)  # 3) "40k"
_SALARY_RULE_ANNUAL_WAN = re.compile(r"年薪[：: ]*(\d+(?:\.\d+)?)\s*万")  # 4) "年薪：60万" → *10/12
_SALARY_RULE_COMMA = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?)")  # 5) "35,000" → /1000

_SALARY_MAX_K = 150.0  # 丢弃超过 150k 的异常月薪
_PROFILE_TEXT_WINDOW = 800  # 只在 profile_text 前 800 字内找薪资（头部摘要区）


def _connect_ro(path: Path, table: str) -> sqlite3.Connection | None:
    """只读连接（mode=ro）；指定表不存在或连接失败返回 None（降级不报错）。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not has_table:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def _loads_json(raw: str) -> dict[str, Any]:
    """raw_json 容错解析；非 dict/解析失败返回空 dict。"""
    try:
        value = json.loads(str(raw or ""))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_salary_k(profile_text: str) -> float | None:
    """从 profile_text 前 800 字解析月薪（k）；解析失败或 >150k 返回 None。"""
    text = str(profile_text or "")[:_PROFILE_TEXT_WINDOW]
    if not text:
        return None
    value: float | None = None
    match = _SALARY_RULE_MONTH_K.search(text)
    if match:
        value = float(match.group(1))
    if value is None:
        match = _SALARY_RULE_RANGE_K.search(text)
        if match:
            value = (float(match.group(1)) + float(match.group(2))) / 2
    if value is None:
        match = _SALARY_RULE_PLAIN_K.search(text)
        if match:
            value = float(match.group(1))
    if value is None:
        match = _SALARY_RULE_ANNUAL_WAN.search(text)
        if match:
            value = float(match.group(1)) * 10 / 12
    if value is None:
        match = _SALARY_RULE_COMMA.search(text)
        if match:
            value = float(match.group(1).replace(",", "")) / 1000
    if value is None or value <= 0 or value > _SALARY_MAX_K:
        return None
    return value


def _percentile(sorted_values: list[float], pct: float) -> float:
    """有序列表的线性插值分位数。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * pct / 100
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return float(sorted_values[low] * (1 - frac) + sorted_values[high] * frac)


def _parse_experience_years(raw_value: Any) -> float | None:
    """经验字段（如 '20年' / '1年以下'）→ 年限数字；无法解析返回 None。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*年", str(raw_value or ""))
    return float(match.group(1)) if match else None


def salary_benchmark(
    kw: str,
    limit: int = 3000,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """方向关键词 → 月薪分位数统计；无样本/库不可用返回 None。

    返回 {n, min, p25, p50, p75, max, annual_万, companies: [(name, cnt)]}，
    annual_万 为中位月薪 ×12 的年化（万），companies 为样本数最多的前 10 家公司。
    """
    keyword = " ".join(str(kw or "").split())
    if not keyword:
        return None
    path = _db_path(db_path)
    if path is None:
        return None
    conn = _connect_ro(path, _RECALLS_TABLE)
    if conn is None:
        return None
    try:
        like = f"%{keyword}%"
        rows = conn.execute(
            """
            SELECT company, title, raw_json FROM agent_candidate_recalls
            WHERE (title LIKE ? OR company LIKE ?) AND raw_json LIKE '%profile_text%'
            LIMIT ?
            """,
            (like, like, max(1, int(limit))),
        ).fetchall()
        salaries: list[float] = []
        company_counts: dict[str, int] = {}
        for row in rows:
            raw = _loads_json(str(row["raw_json"] or ""))
            value = _parse_salary_k(str(raw.get("profile_text") or ""))
            if value is None:
                continue
            salaries.append(value)
            company = str(row["company"] or raw.get("company") or "").strip()
            if company:
                company_counts[company] = company_counts.get(company, 0) + 1
        if not salaries:
            return None
        salaries.sort()
        p50 = _percentile(salaries, 50)
        return {
            "n": len(salaries),
            "min": round(salaries[0], 1),
            "p25": round(_percentile(salaries, 25), 1),
            "p50": round(p50, 1),
            "p75": round(_percentile(salaries, 75), 1),
            "max": round(salaries[-1], 1),
            "annual_万": round(p50 * 12 / 10, 1),
            "companies": sorted(company_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10],
        }
    except (sqlite3.Error, ValueError):
        return None
    finally:
        conn.close()


def _map_target_companies(conn: sqlite3.Connection, keyword: str, limit: int = 40) -> dict[str, set[str]]:
    """CKB 反查目标公司：返回 {展示名: {匹配名集合}}（company_key/name 都去重保留）。"""
    like = f"%{keyword}%"
    try:
        rows = conn.execute(
            """
            SELECT company_key, name FROM company_knowledge
            WHERE industry LIKE ? OR tech_stack_json LIKE ?
               OR product_lines_json LIKE ? OR business_desc LIKE ?
            ORDER BY confidence DESC, evidence_count DESC
            LIMIT ?
            """,
            (like, like, like, like, max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error:
        return {}
    targets: dict[str, set[str]] = {}
    for row in rows:
        key = str(row["company_key"] or "").strip()
        name = str(row["name"] or "").strip()
        display = name or key
        if not display:
            continue
        tokens = targets.setdefault(display, set())
        for token in (key, name):
            if len(token) >= 2:
                tokens.add(token)
    return targets


def talent_map(kw: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """方向关键词 → 人才地图：目标公司列表 + 各公司人才统计；无目标公司返回 None。

    返回 {"keyword", "companies": [{name, n, exp_median, edu, city, salary_min, salary_max}],
    "total": 总人数}。匹配方式为内存双向包含（CKB key/name 与召回公司名互相包含）。
    """
    keyword = " ".join(str(kw or "").split())
    if not keyword:
        return None
    path = _db_path(db_path)
    if path is None:
        return None
    conn = _connect_ro(path, _KNOWLEDGE_TABLE)
    if conn is None:
        return None
    try:
        targets = _map_target_companies(conn, keyword)
        if not targets:
            return None
        # 召回表可能缺（老库）：降级为只返回 CKB 目标公司、人数为 0
        has_recalls = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_RECALLS_TABLE,),
        ).fetchone()
        matched_ids: dict[str, list[int]] = {display: [] for display in targets}
        if has_recalls:
            # 两阶段匹配：先只取 (id, company) 在内存里双向包含匹配公司名，
            # 命中后再按 id 回捞 raw_json，避免全量搬运大字段。
            for row in conn.execute("SELECT id, company FROM agent_candidate_recalls"):
                comp = " ".join(str(row["company"] or "").split())
                if len(comp) < 2:
                    continue
                for display, tokens in targets.items():
                    if any(token in comp or comp in token for token in tokens):
                        matched_ids[display].append(int(row["id"]))
                        break
        companies: list[dict[str, Any]] = []
        total = 0
        for display, ids in matched_ids.items():
            if not ids:
                continue
            exps: list[float] = []
            salaries: list[float] = []
            edu_counts: dict[str, int] = {}
            city_counts: dict[str, int] = {}
            # 分批回捞命中行的 raw_json，解析经验/学历/城市/薪资
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                try:
                    rows = conn.execute(
                        "SELECT raw_json FROM agent_candidate_recalls WHERE id IN (%s)"
                        % ",".join("?" * len(chunk)),
                        chunk,
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    raw = _loads_json(str(row["raw_json"] or ""))
                    exp = _parse_experience_years(raw.get("experience"))
                    if exp is not None:
                        exps.append(exp)
                    edu = str(raw.get("education") or "").strip()
                    if edu:
                        edu_counts[edu] = edu_counts.get(edu, 0) + 1
                    city = str(raw.get("city") or "").split("-")[0].split("·")[0].strip()
                    # city 字段偶有脏数据（如“2026年毕业”/学历词），只保留纯中文 2-4 字
                    if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", city) and city not in (
                        "高中", "大专", "本科", "硕士", "博士", "毕业",
                    ):
                        city_counts[city] = city_counts.get(city, 0) + 1
                    salary = _parse_salary_k(str(raw.get("profile_text") or ""))
                    if salary is not None:
                        salaries.append(salary)
            exps.sort()
            salaries.sort()
            total += len(ids)
            companies.append(
                {
                    "name": display,
                    "n": len(ids),
                    "exp_median": round(_percentile(exps, 50), 1) if exps else None,
                    "edu": sorted(edu_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3],
                    "city": sorted(city_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3],
                    "salary_min": round(salaries[0], 1) if salaries else None,
                    "salary_max": round(salaries[-1], 1) if salaries else None,
                }
            )
        companies.sort(key=lambda item: (-item["n"], item["name"]))
        return {"keyword": keyword, "companies": companies, "total": total}
    except (sqlite3.Error, ValueError):
        return None
    finally:
        conn.close()
