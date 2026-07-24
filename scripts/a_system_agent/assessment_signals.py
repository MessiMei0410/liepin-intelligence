"""S6-2：判人评估器两维的确定性原料 —— 水平分位参照系 + 动机信号。

口径：docs/ASA_PRD_S6_判人评估器_2026-07-23.md §2（水平分位/动机与时机）+ §5 S6-2 行。

红线落法：
- 分位落位 100% 确定性：参照池抽取、年限过滤、分位 rank、band 判定全部由本模块按固定规则算，
  LLM 只把算好的 band/分布读成顾问口径 verdict，永不可能改 band（candidate_assessment 强制写入）。
- 动机只用工况级公开信号：a) 简历工况（在职时长 vs 历史平均任期、近一年简历更新）；
  b) 公司近况公开信号（只读采集官网/公开页，每条带来源 URL 与 as_of，失败记 stats 不静默）；
  c) 无信号时调用方如实写"未见明显变动信号" + confidence=inferred。不推断任何个人隐私。
- 敏感属性零因子：本模块不产出任何年龄/性别/婚育/户籍相关字段。
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import urllib.parse
from datetime import date, datetime
from typing import Any, Callable

from . import mapping_task

# ---------------------------------------------------------------------------
# 水平分位：参照系抽取 + 确定性落位
# ---------------------------------------------------------------------------

MIN_REFERENCE_N = 8  # 参照样本量阈值：N < 8 → confidence=inferred 并注明样本不足
YEARS_WINDOW = 3  # 年限相近口径：±3 年

BANDS = ("top10", "top25", "median", "below")
# band 切点（累计分位 rank = (below + 0.5·equal) / N）：≥0.90 前10%；≥0.75 前25%；≥0.25 中位区间；其余靠后。
BAND_TOP10 = 0.90
BAND_TOP25 = 0.75
BAND_MEDIAN = 0.25

# 职能方向词典（有序，先命中先用；同方向人群互为参照）。无命中 → 归一化岗位名（只与自己同向）。
_DIRECTION_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("技术市场", ("技术市场",)),
    ("失效分析", ("失效分析",)),
    ("自动化软件", ("自动化软件",)),
    ("FAE", ("fae", "现场应用")),
    ("机械", ("机械", "结构设计")),
    ("硬件", ("硬件",)),
    ("软件", ("软件", "嵌入式")),
    ("工艺整合", ("工艺整合", "整合工程")),
    ("工艺", ("工艺",)),
    ("设备", ("设备",)),
    ("质量", ("质量", "pqe", "sqe", "cqe", "qe")),
    ("测试", ("测试",)),
    ("验证", ("验证",)),
    ("设计", ("设计", "design")),
    ("销售", ("销售",)),
    ("市场", ("市场", "marketing")),
    ("采购", ("采购", "sourcing")),
    ("生产", ("生产", "制造")),
)

_TIER_SCORE = {"T1": 90, "T2": 72, "T3": 48, "unknown": 62}
_PACE_DELTA = {"fast": 8, "normal": 0, "slow": -8, "unknown": 0}
_EVOLUTION_DELTA = {"rising": 6, "lateral": 0, "stagnant": -6, "unknown": 0}
_DIRECTION_DELTA = {"up": 8, "lateral": 0, "down": -8}


def parse_experience_years(text: Any) -> int | None:
    """people.experience（"14年"/"10年以上"）→ 年限整数；解析不到返回 None。"""
    match = re.search(r"(\d{1,2})\s*年", str(text or ""))
    return int(match.group(1)) if match else None


def direction_key(job_title: Any) -> str:
    """岗位职能方向键：词典首个命中；无命中用归一化岗位名（参照池≈同岗位）。"""
    title = str(job_title or "").strip()
    lowered = title.lower()
    for direction, tokens in _DIRECTION_LEXICON:
        for token in tokens:
            if token in lowered:
                return direction
    return re.sub(r"\s+", "", lowered) or "未知方向"


def load_reference_pool(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    job_title: str,
    target_years: int | None,
    exclude_job_candidate_id: int,
    years_window: int = YEARS_WINDOW,
) -> dict[str, Any]:
    """参照池：同职能方向 + 年限相近（±window 年）的历史人选既有评估（fit_score）。

    只读 agent_candidate_assessments（is_current=1）× job_candidates × people × jobs；
    排除目标本人；目标年限未知时不做年限过滤（note 注明）。全部过滤在 Python 侧确定性执行。
    """
    direction = direction_key(job_title)
    rows = conn.execute(
        """
        SELECT a.job_candidate_id AS job_candidate_id,a.job_id AS job_id,a.fit_score AS fit_score,
               p.experience AS experience,j.title AS job_title
          FROM agent_candidate_assessments a
          JOIN job_candidates jc ON jc.id=a.job_candidate_id
          JOIN people p ON p.id=a.person_id
          JOIN jobs j ON j.id=a.job_id
         WHERE a.is_current=1
        """,
    ).fetchall()
    members: list[dict[str, Any]] = []
    seen: set[int] = set()
    skipped_direction = 0
    skipped_years = 0
    for row in rows:
        jid = int(row["job_candidate_id"])
        if jid == int(exclude_job_candidate_id) or jid in seen:
            continue
        if direction_key(row["job_title"]) != direction:
            skipped_direction += 1
            continue
        years = parse_experience_years(row["experience"])
        if target_years is not None:
            if years is None or abs(years - target_years) > years_window:
                skipped_years += 1
                continue
        seen.add(jid)
        members.append(
            {
                "job_candidate_id": jid,
                "job_id": int(row["job_id"] or 0),
                "fit_score": int(row["fit_score"]),
                "years": years,
            }
        )
    return {
        "direction": direction,
        "years_window": years_window if target_years is not None else None,
        "target_years": target_years,
        "members": members,
        "skipped_direction": skipped_direction,
        "skipped_years": skipped_years,
    }


def load_target_fit_score(conn: sqlite3.Connection, job_candidate_id: int) -> int | None:
    """目标本人既有评估 fit_score（is_current=1，最新一条）；没有 → None（走轨迹特征 rubric）。"""
    row = conn.execute(
        """
        SELECT fit_score FROM agent_candidate_assessments
         WHERE job_candidate_id=? AND is_current=1
         ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        (int(job_candidate_id),),
    ).fetchone()
    return int(row["fit_score"]) if row and row["fit_score"] is not None else None


def trajectory_feature_score(trajectory: dict[str, Any], move_history: dict[str, Any]) -> int:
    """无既有 fit_score 时的确定性轨迹特征分（固定 rubric，0-100，与 fit_score 同量级）。

    base = 各段平台 tier 加权（最近段 0.5，其余均分 0.5；无段 60）
         + 晋升速度修正（fast +8 / slow -8）+ 技术栈演进修正（rising +6 / stagnant -6）
         + 跳槽方向均值修正（up +8 / down -8，无 move 记 0）。clamp 0-100。
    """
    segments = [item for item in (trajectory.get("segments") or []) if isinstance(item, dict)]
    if len(segments) == 1:
        base = float(_TIER_SCORE.get(str(segments[0].get("tier") or "unknown"), 62))
    elif segments:
        weights = [0.5] + [0.5 / (len(segments) - 1)] * (len(segments) - 1)
        base = sum(_TIER_SCORE.get(str(seg.get("tier") or "unknown"), 62) * w for seg, w in zip(segments, weights))
    else:
        base = 60.0
    score = base
    score += _PACE_DELTA.get(str(trajectory.get("promotion_pace") or "unknown"), 0)
    score += _EVOLUTION_DELTA.get(str(trajectory.get("tech_evolution") or "unknown"), 0)
    moves = [item for item in (move_history.get("moves") or []) if isinstance(item, dict)]
    if moves:
        score += sum(_DIRECTION_DELTA.get(str(move.get("direction") or "lateral"), 0) for move in moves) / len(moves)
    return max(0, min(100, round(score)))


def compute_placement(score: int, reference_scores: list[int], *, min_n: int = MIN_REFERENCE_N) -> dict[str, Any]:
    """确定性落位：rank=(below+0.5·equal)/N → band；N=0 时 band=None（无法落位，如实）。"""
    scores = [int(value) for value in reference_scores]
    n = len(scores)
    placement: dict[str, Any] = {
        "n": n,
        "sample_sufficient": n >= min_n,
        "min_n": min_n,
        "score": int(score),
        "percentile_rank": None,
        "band": None,
        "median": None,
        "q25": None,
        "q75": None,
        "min": None,
        "max": None,
    }
    if n == 0:
        return placement
    ordered = sorted(scores)
    placement["min"] = ordered[0]
    placement["max"] = ordered[-1]
    placement["median"] = round(statistics.median(ordered), 1)
    if n >= 2:
        quartiles = statistics.quantiles(ordered, n=4, method="inclusive")
        placement["q25"] = round(quartiles[0], 1)
        placement["q75"] = round(quartiles[2], 1)
    else:
        placement["q25"] = placement["q75"] = placement["median"]
    below = sum(1 for value in scores if value < score)
    equal = sum(1 for value in scores if value == score)
    rank = (below + 0.5 * equal) / n
    placement["percentile_rank"] = round(rank, 4)
    if rank >= BAND_TOP10:
        placement["band"] = "top10"
    elif rank >= BAND_TOP25:
        placement["band"] = "top25"
    elif rank >= BAND_MEDIAN:
        placement["band"] = "median"
    else:
        placement["band"] = "below"
    return placement


def reference_summary_text(placement: dict[str, Any], *, direction: str, years_window: int | None) -> str:
    """参照系确定性摘要串：作为 type=知识库 证据 ref 的白名单唯一合法值（模型编造过不了校验）。"""
    window = f"±{years_window}年" if years_window is not None else "不限年限"
    if int(placement.get("n") or 0) == 0:
        return f"历史人选库参照系：同方向（{direction}）{window} 样本N=0，无法构成参照分布"
    return (
        f"历史人选库参照系：同方向（{direction}）{window} 样本N={placement['n']}"
        f"，既有评估中位分{placement.get('median')}（P25={placement.get('q25')}，P75={placement.get('q75')}）"
    )


# ---------------------------------------------------------------------------
# 动机与时机：a) 简历工况信号（确定性）
# ---------------------------------------------------------------------------

_PERIOD_PATTERN = re.compile(
    r"(\d{4})\s*[./年]\s*(\d{1,2})\s*月?\s*(?:-|–|—|~|～|至)\s*"
    r"(至今|现在|今|(\d{4})\s*[./年]\s*(\d{1,2})\s*月?)"
)

# 当前任职显著偏离其历史节奏才成信号（阈值固定，可解释）
TENURE_OVER_RATIO = 1.2  # 当前任期 ≥ 历史平均 ×1.2 → 到了他 historically 会动的时点
TENURE_UNDER_RATIO = 0.6  # 当前任期 ≤ 历史平均 ×0.6 → 比他自己节奏还早
RESUME_FRESH_DAYS = 90  # 近 3 个月有简历更新 → 活跃信号
RESUME_YEAR_DAYS = 365  # 近一年内有更新 → 弱信号；超过一年无更新 → 不出信号


def _months_between(start: tuple[int, int], end: tuple[int, int]) -> int:
    return max(1, (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1)


def parse_work_segments(text: str, *, today: date | None = None) -> list[dict[str, Any]]:
    """从简历文本解析工作时间段：YYYY.MM-至今 / YYYY年M月-YYYY年M月。

    返回按出现顺序的段列表：{start,end("至今"→None),months,line(所在行原文,供逐字证据)}。
    """
    today = today or date.today()
    lines = str(text or "").splitlines()
    segments: list[dict[str, Any]] = []
    for line in lines:
        for match in _PERIOD_PATTERN.finditer(line):
            start = (int(match.group(1)), int(match.group(2)))
            if match.group(4):
                end: tuple[int, int] | None = (int(match.group(4)), int(match.group(5)))
            else:
                end = None  # 至今
            months = _months_between(start, (today.year, today.month)) if end is None else _months_between(start, end)
            segments.append(
                {
                    "start": f"{start[0]}.{start[1]:02d}",
                    "end": "至今" if end is None else f"{end[0]}.{end[1]:02d}",
                    "is_current": end is None,
                    "months": months,
                    "line": line.strip(),
                }
            )
    return segments


def _parse_source_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def employment_signals(
    work_text: str,
    *,
    latest_source_date: Any = None,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """a) 简历工况信号：在职时长 vs 历史平均任期 + 近一年简历更新。

    返回 (signals, facts)。signal={kind,source:"简历工况",summary,as_of,evidence_line?}；
    facts 记录全部计算值（供 LLM payload / 模板 verdict 落地），无信号时 facts 也为空 dict 以外的真实数字。
    """
    today = today or date.today()
    as_of = today.strftime("%Y-%m-%d")
    segments = parse_work_segments(work_text, today=today)
    signals: list[dict[str, Any]] = []
    facts: dict[str, Any] = {"segments": segments}
    current = next((seg for seg in segments if seg["is_current"]), None)
    if current is None and segments:
        current = max(segments, key=lambda seg: (seg["end"], seg["start"]))
    previous = [seg for seg in segments if seg is not current]
    if current:
        facts["current_tenure_months"] = current["months"]
        facts["current_segment_line"] = current["line"]
    if current and previous:
        avg_prev = round(sum(seg["months"] for seg in previous) / len(previous), 1)
        facts["avg_prev_tenure_months"] = avg_prev
        if current["months"] >= avg_prev * TENURE_OVER_RATIO:
            signals.append(
                {
                    "kind": "tenure_over_avg",
                    "source": "简历工况",
                    "summary": f"当前任职已 {current['months']} 个月，明显超过其历史平均任期 {avg_prev} 个月（按其自身节奏到了可能动的时点）",
                    "as_of": as_of,
                    "evidence_line": current["line"],
                }
            )
        elif current["months"] <= avg_prev * TENURE_UNDER_RATIO:
            signals.append(
                {
                    "kind": "tenure_under_avg",
                    "source": "简历工况",
                    "summary": f"当前任职 {current['months']} 个月，明显短于其历史平均任期 {avg_prev} 个月（入职不久，按其自身节奏尚早）",
                    "as_of": as_of,
                    "evidence_line": current["line"],
                }
            )
    updated = _parse_source_date(latest_source_date)
    if updated:
        days = (today - updated).days
        facts["latest_resume_update"] = updated.strftime("%Y-%m-%d")
        facts["days_since_update"] = days
        if 0 <= days <= RESUME_FRESH_DAYS:
            signals.append(
                {
                    "kind": "resume_recently_updated",
                    "source": "简历工况",
                    "summary": f"近 3 个月内有简历更新记录（{updated.strftime('%Y-%m-%d')}，距今 {days} 天）",
                    "as_of": as_of,
                }
            )
        elif RESUME_FRESH_DAYS < days <= RESUME_YEAR_DAYS:
            signals.append(
                {
                    "kind": "resume_updated_within_year",
                    "source": "简历工况",
                    "summary": f"近一年内有简历更新记录（{updated.strftime('%Y-%m-%d')}，距今 {days} 天）",
                    "as_of": as_of,
                }
            )
    return signals, facts


# ---------------------------------------------------------------------------
# 动机与时机：b) 公司近况公开信号（只读采集，每条带来源 URL 与 as_of）
# ---------------------------------------------------------------------------

# 信号关键词（PRD §2：裁员/业务收缩/融资/上市节点；只用工况级公开信号，不碰个人隐私）。
_SIGNAL_KEYWORD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("layoff", ("裁员", "人员优化", "组织优化", "结构性优化", "破产", "停产", "关停")),
    ("contraction", ("业务收缩", "收缩业务", "关闭产线", "业务调整", "战略收缩")),
    ("funding", ("融资", "领投", "跟投", "战略投资", "增资扩股")),
    ("ipo", ("上市", "IPO", "挂牌", "过会", "招股")),
    ("ma", ("并购", "重组", "被收购", "要约收购")),
)
_SIGNAL_KIND_LABELS = {"layoff": "裁员/人员调整", "contraction": "业务收缩", "funding": "融资", "ipo": "上市节点", "ma": "并购重组"}

# 新闻/投资者类入口链接识别（官网首页导航里的近况入口）
_NEWS_LINK_PATTERN = re.compile(r"(?:news|press|ir|investor|media|关于|新闻|资讯|动态|投资者)", re.I)
_LINK_HREF_PATTERN = re.compile(r"""href=["']([^"'#]+)["']""", re.I)

MAX_COMPANY_SIGNALS = 4
MAX_SIGNAL_PAGES = 2  # 每公司页面数小上限（采集器硬边界）


def _company_site_hint(company: str, hints: dict[str, dict[str, str]]) -> str:
    """公司名 → 官网线索：精确键 / 线索键是公司名子串（"杰华特" ⊂ "杰华特微电子股份有限公司"）。"""
    name = str(company or "").strip()
    if not name:
        return ""
    if name in hints:
        return str(hints[name].get("site") or "")
    for key, hint in hints.items():
        if key and key in name:
            return str(hint.get("site") or "")
    return ""


def _news_links(base_url: str, html: str, *, limit: int) -> list[str]:
    base_host = urllib.parse.urlparse(base_url).netloc.lower()
    links: list[str] = []
    for match in _LINK_HREF_PATTERN.finditer(html):
        url = urllib.parse.urljoin(base_url, match.group(1))
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != base_host:
            continue
        if _NEWS_LINK_PATTERN.search(urllib.parse.unquote(url)):
            if url not in links:
                links.append(url)
        if len(links) >= limit:
            break
    return links


def _scan_signal_sentences(text: str) -> list[tuple[str, str]]:
    """可见文本按句扫描信号关键词；返回 [(kind, sentence)]，句序保持、去重。"""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sentence in re.split(r"[。！!？?\n]", str(text or "")):
        body = " ".join(sentence.split()).strip("，,；;：: ")
        if len(body) < 6:
            continue
        for kind, keywords in _SIGNAL_KEYWORD_GROUPS:
            if any(keyword in body for keyword in keywords):
                if body not in seen:
                    seen.add(body)
                    hits.append((kind, body[:120]))
                break
    return hits


def collect_company_signals(
    company: str,
    *,
    fetcher: Callable[[str, float], tuple[int, str, str]] | None = None,
    hints: dict[str, dict[str, str]] | None = None,
    today: date | None = None,
    max_pages: int = MAX_SIGNAL_PAGES,
    timeout: float = mapping_task.DEFAULT_TIMEOUT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """公司近况公开信号：官网首页 + 新闻/投资者入口页只读采集，按句命中关键词。

    每条信号 {kind,source:"公开信息",summary,url,as_of}；失败分类记 stats（timeout/http_404/
    blocked/network_error/js_shell），无官网线索记 skip，绝不静默、绝不编造。
    """
    today = today or date.today()
    as_of = today.strftime("%Y-%m-%d")
    fetcher = fetcher or mapping_task.urllib_fetcher
    hints = mapping_task.COMPANY_SOURCE_HINTS if hints is None else hints
    stats: dict[str, Any] = {"company": str(company or ""), "pages_fetched": 0, "failures": [], "note": ""}
    site = _company_site_hint(company, hints)
    if not site:
        stats["note"] = "无官网线索，未采集（不编造公开信号）"
        return [], stats
    urls = [site]
    status, body, category = fetcher(site, timeout)
    stats["pages_fetched"] += 1
    if status != 200 or not body:
        stats["failures"].append({"url": site, "category": category or f"http_{status}"})
        return [], stats
    if mapping_task.looks_like_js_shell(body):
        stats["failures"].append({"url": site, "category": "js_shell"})
        return [], stats
    urls.extend(_news_links(site, body, limit=max(0, max_pages - 1)))
    signals: list[dict[str, Any]] = []
    seen_sentences: set[str] = set()
    for url in urls:
        if len(signals) >= MAX_COMPANY_SIGNALS:
            break
        if url != site:
            status, body, category = fetcher(url, timeout)
            stats["pages_fetched"] += 1
            if status != 200 or not body:
                stats["failures"].append({"url": url, "category": category or f"http_{status}"})
                continue
        for kind, sentence in _scan_signal_sentences(mapping_task._visible_text(body)):
            if len(signals) >= MAX_COMPANY_SIGNALS:
                break
            if sentence in seen_sentences:
                continue
            seen_sentences.add(sentence)
            signals.append(
                {
                    "kind": kind,
                    "kind_label": _SIGNAL_KIND_LABELS.get(kind, kind),
                    "source": "公开信息",
                    "summary": f"{company}：{sentence}",
                    "url": url,
                    "as_of": as_of,
                }
            )
    if not signals:
        stats["note"] = "已采集公开页但未见明显变动信号关键词"
    return signals, stats


# ---------------------------------------------------------------------------
# S6-3 风险点（呈现口径：需要核实的问题，不是风险定罪）：确定性检出原料
#
# 五类检出中三类半确定性（gap 空窗 / 频繁跳动 / 时间线冲突 / strategy_v2 硬条件差距），
# title 通胀与过度包装的语义判断由 LLM 补充（candidate_assessment 过同一道证据闸）。
# 阈值全部固定、可解释；每条 item 带 kind/severity/evidence（简历逐字行优先）；
# 呈现语义永远是"需要核实的问题"，不出现任何定罪/淘汰表述。
# ---------------------------------------------------------------------------

RISK_GAP_MONTHS = 6  # 相邻两段经历之间空窗 >6 个月 → 需核实
RISK_GAP_HIGH_MONTHS = 12  # 空窗 >12 个月 → high
RISK_SHORT_TENURE_MONTHS = 12  # 已结束段任期 <12 个月计一次短任期
RISK_SHORT_TENURE_MEDIUM = 2  # 短任期 ≥2 段 → medium
RISK_SHORT_TENURE_HIGH = 3  # 短任期 ≥3 段 → high
RISK_RECENT_WINDOW_MONTHS = 60  # 近 5 年窗口内段数过多也算频繁跳动
RISK_RECENT_MOVES = 3  # 近 5 年 ≥3 段经历 → low
RISK_OVERLAP_MONTHS = 6  # 两段时间重叠 >6 个月 → 时间线冲突（过度包装信号类）
RISK_MAX_PER_KIND = 3  # 同类确定性 item 上限（防刷屏）
RISK_SKILL_TERM_MAX = 12  # 技能缺项核对的词数上限
RISK_SKILL_MISSING_MAX = 3  # 技能缺项 item 最多列 3 个缺项词

# 学历等级（硬性比对用；只比岗位要求 vs 简历自述，不推测）
_EDU_LEVELS: tuple[tuple[str, int], ...] = (
    ("博士", 4),
    ("硕士", 3),
    ("研究生", 3),
    ("mba", 3),
    ("MBA", 3),
    ("本科", 2),
    ("学士", 2),
    ("大专", 1),
    ("专科", 1),
)

_YEARS_PATTERN = re.compile(r"(\d{1,2})\s*年")
_SKILL_VERB_PREFIXES = ("熟悉", "掌握", "精通", "具备", "具有", "了解")
# 学历段（"浙江大学 · 本科 2009.09-2013.06"）不是工作经历：gap/任期/冲突检出前剔除
_DEGREE_LINE_PATTERN = re.compile(r"本科|大专|专科|硕士|博士|学士|研究生|MBA|mba")
_SKILL_STOP_TOKENS = {
    "经验", "以上", "以下", "优先", "能力", "工作", "相关", "及", "或", "和", "与",
    "学历", "年", "年以上", "者", "人选", "岗位", "职责", "要求", "背景",
    "设计", "开发", "研发", "工程师", "良好", "较好", "优秀",
}


def _ym(value: Any) -> tuple[int, int] | None:
    """"YYYY.MM" 段端点 → (year, month)；"至今" 等 → None。"""
    match = re.match(r"(\d{4})\.(\d{1,2})", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _risk_item(kind: str, risk: str, severity: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
    return {"kind": kind, "risk": risk, "severity": severity, "evidence": evidence}


def _line_evidence(*lines: str) -> list[dict[str, str]]:
    # 只去首尾空白：内部空白必须保持原样，否则过不了简历逐字包含校验（verify_evidence）
    evidence: list[dict[str, str]] = []
    for line in lines:
        body = str(line or "").strip()
        if body and {"type": "简历", "ref": body} not in evidence:
            evidence.append({"type": "简历", "ref": body})
    return evidence


def detect_resume_gaps(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """空窗检出：按开始时间排序后，相邻两段（前段有结束月）间隔 >6 个月 → 需核实。

    空窗月数 = 两段端点之间完整的间隔月（2020.05 结束 → 2021.03 开始 = 9 个月）。
    >12 个月 high，否则 medium。证据 = 前后两段所在行原文（逐字）。
    """
    items: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    ordered = sorted(
        (seg for seg in segments if _ym(seg.get("start"))), key=lambda seg: _ym(seg["start"]) or (0, 0)
    )
    for earlier, later in zip(ordered, ordered[1:]):
        end = _ym(earlier.get("end"))
        start = _ym(later.get("start"))
        if end is None or start is None:  # 前段"至今"不可能是较早段；防御
            continue
        gap_months = (start[0] - end[0]) * 12 + (start[1] - end[1]) - 1
        if gap_months <= RISK_GAP_MONTHS:
            continue
        gaps.append({"after": earlier["end"], "before": later["start"], "months": gap_months})
        if len(items) >= RISK_MAX_PER_KIND:
            continue
        severity = "high" if gap_months > RISK_GAP_HIGH_MONTHS else "medium"
        items.append(
            _risk_item(
                "gap",
                f"{earlier['end']} 至 {later['start']} 之间有约 {gap_months} 个月简历空窗，需要核实该期间的经历安排",
                severity,
                _line_evidence(earlier.get("line") or "", later.get("line") or ""),
            )
        )
    return items, {"gaps": gaps}


def detect_frequent_hops(
    segments: list[dict[str, Any]], *, today: date | None = None, stability_sensitive: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """频繁跳动：已结束段任期 <12 个月 ≥2 段（≥3 段升 high）；或近 5 年 ≥3 段经历（low）。

    岗位硬条件强调"稳定"（stability_sensitive）→ severity 升一档（low→medium→high）。
    """
    today = today or date.today()
    ended = [seg for seg in segments if not seg.get("is_current")]
    short = [seg for seg in ended if int(seg.get("months") or 0) < RISK_SHORT_TENURE_MONTHS]
    current_ym = (today.year, today.month)
    recent = [
        seg for seg in segments
        if _ym(seg.get("start")) and (current_ym[0] - _ym(seg["start"])[0]) * 12 + (current_ym[1] - _ym(seg["start"])[1]) <= RISK_RECENT_WINDOW_MONTHS
    ]
    facts: dict[str, Any] = {
        "short_tenure_segments": [{"period": f"{seg['start']}-{seg['end']}", "months": seg["months"]} for seg in short],
        "recent_window_segments": len(recent),
        "stability_sensitive": stability_sensitive,
    }
    items: list[dict[str, Any]] = []
    if len(short) >= RISK_SHORT_TENURE_MEDIUM:
        severity = "high" if len(short) >= RISK_SHORT_TENURE_HIGH else "medium"
        if stability_sensitive and severity == "medium":
            severity = "high"
        examples = "、".join(f"{seg['start']}-{seg['end']}（{seg['months']} 个月）" for seg in short[:3])
        items.append(
            _risk_item(
                "frequent_hop",
                f"有 {len(short)} 段经历任期不足 12 个月（{examples}），需要核实换工作频率与稳定性匹配情况",
                severity,
                _line_evidence(*[seg.get("line") or "" for seg in short[:3]]),
            )
        )
    elif len(recent) >= RISK_RECENT_MOVES:
        severity = "medium" if stability_sensitive else "low"
        items.append(
            _risk_item(
                "frequent_hop",
                f"近 5 年内有 {len(recent)} 段经历，需要核实换工作的频率与动因",
                severity,
                _line_evidence(*[seg.get("line") or "" for seg in recent[:3]]),
            )
        )
    return items, facts


def detect_timeline_conflicts(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """时间线冲突（过度包装信号）：两段时间重叠 >6 个月 → 需核实（兼任/简历笔误都可能，不定罪）。"""
    items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    ordered = sorted(
        (seg for seg in segments if _ym(seg.get("start"))), key=lambda seg: _ym(seg["start"]) or (0, 0)
    )
    for index, earlier in enumerate(ordered):
        earlier_end = _ym(earlier.get("end"))
        if earlier_end is None:
            continue
        for later in ordered[index + 1:]:
            later_start = _ym(later.get("start"))
            if later_start is None or later_start >= earlier_end:
                continue
            overlap = (earlier_end[0] - later_start[0]) * 12 + (earlier_end[1] - later_start[1]) + 1
            if overlap <= RISK_OVERLAP_MONTHS:
                continue
            conflicts.append({"first": f"{earlier['start']}-{earlier['end']}", "second": f"{later['start']}-{later['end']}", "overlap_months": overlap})
            if len(items) >= 2:
                continue
            items.append(
                _risk_item(
                    "over_packaging",
                    f"两段经历时间线重叠约 {overlap} 个月（{earlier['start']}-{earlier['end']} 与 {later['start']}-{later['end']}），需要核实时间线是否准确",
                    "medium",
                    _line_evidence(earlier.get("line") or "", later.get("line") or ""),
                )
            )
    return items, {"timeline_conflicts": conflicts}


def parse_hard_requirements(hard_text: Any) -> dict[str, Any]:
    """岗位硬条件确定性解析：学历要求 / 年限要求 / 技能关键词 / 是否强调稳定。"""
    text = " ".join(str(hard_text or "").split())
    edu_required: tuple[str, int] | None = None
    for label, level in _EDU_LEVELS:
        if label in text and (edu_required is None or level > edu_required[1]):
            edu_required = (label, level)
    years_values = [int(match.group(1)) for match in _YEARS_PATTERN.finditer(text)]
    # 年限硬条件取最低值（"4年以上，优先8年以上"的硬线是 4；取 max 会把"优先"误当硬线）
    years_required = min(years_values) if years_values else None
    scrubbed = _YEARS_PATTERN.sub(" ", text)
    for label, _level in _EDU_LEVELS:
        scrubbed = scrubbed.replace(label, " ")
    # 连接性话术不是技能词：先整体剥离再切词（"8年以上电源芯片经验" → "电源芯片"）
    for filler in ("及以上", "以上", "学历", "学位"):
        scrubbed = scrubbed.replace(filler, " ")
    tokens: list[str] = []
    for token in re.split(r"[、，,；;。/|\s]+", scrubbed):
        body = token.strip()
        for prefix in _SKILL_VERB_PREFIXES:
            if body.startswith(prefix):
                body = body[len(prefix):]
        body = body.strip("（）()【】 ")
        if body.endswith("经验") or body.endswith("优先") or body.endswith("方向"):
            body = body[:-2].strip()
        # "稳定性好"类是态度要求不是技能词（稳定性已单独用于 severity 加权）
        if len(body) < 2 or body in _SKILL_STOP_TOKENS or "稳定" in body:
            continue
        if body not in tokens:
            tokens.append(body)
    return {
        "edu_required": edu_required,
        "years_required": years_required,
        "skill_terms": tokens[:RISK_SKILL_TERM_MAX],
        "stability_sensitive": "稳定" in text,
    }


def _education_level(text: Any) -> tuple[str, int] | None:
    found: tuple[str, int] | None = None
    body = str(text or "")
    for label, level in _EDU_LEVELS:
        if label in body and (found is None or level > found[1]):
            found = (label, level)
    return found


def _corpus_line_with(corpus: str, keyword: str) -> str:
    for line in str(corpus or "").splitlines():
        if keyword and keyword in line:
            # 只去首尾空白，保持内部原样（逐字校验口径同上）
            return line.strip()
    return ""


def detect_hard_gaps(
    *,
    hard_text: Any,
    education_text: Any,
    experience_text: Any,
    segments: list[dict[str, Any]],
    corpus: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """与岗位硬条件（jobs.hard_requirements，即 strategy_v2 的硬条件输入）的差距：学历 / 年限 / 必备技能缺项。

    只比对明确写出的硬条件 vs 简历自述；双方任一缺失 → 不检（不编造）。
    技能词只取硬条件文本解析结果——strategy_v2 step4 是寻访关键词（含目标池公司名），
    不是对人要求，误用会把"人选不是来自友商"错报成缺项（S6-3 真实验证 #564 暴露，已修正）。
    学历/年限差距 = high（硬条件）；技能缺项 = low 且 evidence 为空（缺项本身无逐字证据，
    调用方据此把 risks 维 confidence 压为 inferred）。
    """
    parsed = parse_hard_requirements(hard_text)
    items: list[dict[str, Any]] = []
    facts: dict[str, Any] = {"hard_requirement_parsed": parsed}
    edu_required = parsed["edu_required"]
    edu_candidate = _education_level(education_text) or _education_level(corpus)
    facts["edu_candidate"] = edu_candidate
    if edu_required and edu_candidate and edu_required[1] > edu_candidate[1]:
        line = _corpus_line_with(corpus, edu_candidate[0])
        items.append(
            _risk_item(
                "hard_requirement",
                f"岗位要求{edu_required[0]}及以上学历，简历显示最高学历为{edu_candidate[0]}，需要核实是否满足学历硬条件",
                "high",
                _line_evidence(line),
            )
        )
    years_required = parsed["years_required"]
    candidate_years = parse_experience_years(experience_text)
    if candidate_years is None and segments:
        candidate_years = sum(int(seg.get("months") or 0) for seg in segments) // 12
    facts["years_candidate"] = candidate_years
    if years_required is not None and candidate_years is not None and candidate_years < years_required:
        earliest = min((seg for seg in segments if _ym(seg.get("start"))), key=lambda seg: _ym(seg["start"]), default=None)
        items.append(
            _risk_item(
                "hard_requirement",
                f"岗位要求 {years_required} 年以上相关经验，简历推算约 {candidate_years} 年，需要核实年限是否达标",
                "high",
                _line_evidence((earliest or {}).get("line") or ""),
            )
        )
    terms = parsed["skill_terms"]
    missing = [term for term in terms if term not in str(corpus or "")]
    facts["skill_terms_checked"] = terms
    facts["skill_terms_missing"] = missing
    if missing:
        shown = "、".join(missing[:RISK_SKILL_MISSING_MAX])
        items.append(
            _risk_item(
                "hard_requirement",
                f"岗位关键词「{shown}」在简历中未见，需要核实是否具备相关经验",
                "low",
                [],
            )
        )
    return items, facts


def collect_risk_facts(
    work_text: str,
    *,
    corpus: str,
    hard_text: Any = "",
    education_text: Any = "",
    experience_text: Any = "",
    today: date | None = None,
) -> dict[str, Any]:
    """S6-3 确定性风险检出汇总：gap / 频繁跳动 / 时间线冲突 / 硬条件差距。

    返回 {items, facts, checks_run}；checks_run 记录实际执行了哪些核对（空态置信度用）：
    有时间段 → gap/跳动/冲突三项都跑了；有硬条件文本 → 硬条件核对跑了。
    """
    today = today or date.today()
    segments = [
        seg for seg in parse_work_segments(work_text, today=today)
        if not _DEGREE_LINE_PATTERN.search(str(seg.get("line") or ""))
    ]
    items: list[dict[str, Any]] = []
    facts: dict[str, Any] = {}
    checks: list[str] = []
    if segments:
        gap_items, gap_facts = detect_resume_gaps(segments)
        hop_items, hop_facts = detect_frequent_hops(
            segments, today=today, stability_sensitive="稳定" in str(hard_text or "")
        )
        conflict_items, conflict_facts = detect_timeline_conflicts(segments)
        items.extend(gap_items + hop_items + conflict_items)
        facts.update({**gap_facts, **hop_facts, **conflict_facts})
        checks.extend(["gap", "frequent_hop", "over_packaging"])
    if str(hard_text or "").strip():
        hard_items, hard_facts = detect_hard_gaps(
            hard_text=hard_text,
            education_text=education_text,
            experience_text=experience_text,
            segments=segments,
            corpus=corpus,
        )
        items.extend(hard_items)
        facts.update(hard_facts)
        checks.append("hard_requirement")
    facts["segments_count"] = len(segments)
    return {"items": items, "facts": facts, "checks_run": checks}
