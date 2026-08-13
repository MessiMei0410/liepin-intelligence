"""S5-1：Mapping 直挖 —— 目标团队定位 + 名单生成（mapping_task artifact，schema_version=mapping_v1）。

口径来源（事实源）：
- 任务卡 docs/TASKCARD_S5-1_mapping直挖_20260723.md（范围/红线/验收）
- PRD docs/ASA_PRD_S5_mapping_direct_sourcing_2026-07-23.md §2（数据模型）/§3（采集边界）/§7（硬性约束）

红线（写死，违反即返工）：
- 不自动触达、不自动发消息；本模块只产名单和任务卡数据，行动全部由顾问本人执行。
- 人名必须有公开来源 URL：candidates[].source_urls 为空 → 整条拒绝写入（防编造硬约束，
  写入校验 + 名单组装双层拦截，拒写计数进 stats.rejected_no_source）。
- 禁挖名单照常生效：禁挖公司的人不进名单（过滤计数进 stats.banned_filtered）；
  restricted 层只经 knowledge_base.load_restricted_constraints 白名单出库，费率/手机号/
  offer/话术红线永不进任何输出。
- 猎聘/X-SaaS 不用于 Mapping 采集（避免与 N1 方言层重复）。
- 入库不开旁路：status=intaken 的后续路径指向现有 preflight/commit（S5-2 落地），
  本模块不写 job_candidates。
- 知识库 JSON 运行时只读（load_company_graph / load_job_archetypes / restricted 均只读）。

采集器边界（PRD §3）：urllib/标准库只读公网页面，超时 ≤10s，每公司页面数设小上限；
JS 渲染页/反爬/页面变动/超时一律记入 stats.failures（原因分类），不静默、不硬编数据。
专利源为 Google Patents XHR；论文源为 OpenAlex works API（单位标注含公司的作者才取）；
全局共享源不可达时熔断（首失败留痕 + 后续公司记 skipped_after_failure，防重复打满超时）。
人脉/姓名提取保守（宁缺毋滥）：只取结构化字段（专利发明人/论文作者单位标注）或显式
"联系人/作者"标注，每条候选必须能回指具体来源 URL。脉脉本期只做接口预留（MaimaiCollector 不接实现）。

S5-2（任务卡 docs/TASKCARD_S5-2_任务卡视图与破冰素材_20260723.md）：
- 候选状态机 PATCH（第 6 节）：七态枚举，pending→confirmed→contacted→replied→intaken 主链，
  parked 可恢复、rejected 软删终态、intaken 终态不倒退；intaken 只能经入库动作到达。
  状态/备注/破冰/入库回执全部原位更新（version 不 bump、history 不动），整批先校验后落库。
- 破冰素材（icebreaker）：规则版生成（不依赖 LLM），hooks 必须引用该候选真实线索实词
  （论文题/单位/团队名/职务）；反模板硬约束——全部 hooks 不含任何线索关键词判不合格并拒绝写入；
  费率/话术红线/手机号永不进 hooks。素材只存不送，发送动作永远由顾问本人执行。
- 入库（intake）：仅 confirmed 可入库；复用 MULTICHANNEL intake 的写入口径（candidates/people/
  source_profiles/entity_source_links/job_candidates/candidate_events 同一事务，不写第二条
  job_candidates）；遮罩名合并按 §6.4 既有口径（遮罩名须公司+职位双匹配才可互证）；禁挖名单
  入库前再校验一次；已停止推进的关系不重复入库。
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape
from typing import Any, Callable

from . import knowledge_base, strategy_v2
from .candidate_pool_filter import intake_mismatch_verdict
from .workflow import _mask_candidate_name

ARTIFACT_TYPE = "mapping_task"
SCHEMA_VERSION = "mapping_v1"

TRIGGERS = ("decision_tree_exhausted", "manual", "radar")
TRIGGER_LABELS = {
    "decision_tree_exhausted": "扩池决策树末端（池已尽）",
    "manual": "顾问手动发起",
    "radar": "人才流动雷达榜单发起（公司近况信号联动）",
}
CANDIDATE_STATUSES = ("pending", "confirmed", "contacted", "replied", "intaken", "parked", "rejected")
# PRD §2 evidence.type 枚举（图谱/官网/公众号/招聘JD/脉脉）；专利/论文证据只进候选 source_urls，
# 团队证据沿用 PRD 枚举，不另造类型。
EVIDENCE_TYPES = ("图谱", "官网", "公众号", "招聘JD", "脉脉")
CONFIDENCES = ("high", "medium", "low")

REQUIRED_KEYS = ("schema_version", "trigger", "job_id", "strategy_ref", "target_teams", "candidates", "stats")
_TEAM_REQUIRED_KEYS = ("company", "team", "location", "evidence", "confidence")
_CANDIDATE_REQUIRED_KEYS = (
    "name", "current_role", "team_ref", "source_urls", "confidence", "reason", "status", "consultant_note",
)
_STATS_REQUIRED_KEYS = ("teams", "candidates", "confirmed", "intaken")

# 采集器硬边界（任务卡：urllib/标准库、超时 ≤10s、每公司页面数小上限）
DEFAULT_TIMEOUT = 8.0
DEFAULT_MAX_PAGES_PER_COMPANY = 4
MAX_CANDIDATE_SOURCE_URLS = 5
MAX_FAILURES_KEPT = 30
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 公司采集线索提示（公开信息，顾问可校准；不是候选人数据，仅定位入口页/专利申请人口径）。
# 缺提示的公司：官网步跳过（留痕），专利步按公司名直接检索。
COMPANY_SOURCE_HINTS: dict[str, dict[str, str]] = {
    "MPS": {"site": "https://www.monolithicpower.com", "patent_key": "Monolithic Power Systems", "paper_key": "Monolithic Power Systems"},
    "矽力杰": {"site": "https://www.silergy.com", "patent_key": "矽力杰", "paper_key": "Silergy"},
    "杰华特": {"site": "https://www.joulwatt.com", "patent_key": "杰华特", "paper_key": "JoulWatt"},
    "晶丰明源": {"site": "https://www.bpsemi.com", "patent_key": "晶丰明源", "paper_key": "Bright Power Semiconductor"},
}

# JS 壳页/页面变动识别：可见文本极少且带前端渲染标记（或几乎无文本）→ 记 js_shell 失败
_JS_SHELL_MARKERS = ("enable javascript", "javascript is required", "__next_data__", "window.__INITIAL_STATE__", "ng-version")
_CONTACT_PATTERN = re.compile(r"(?:联系人|作者|撰文)[：:\s]{0,3}([一-龥]{2,4})(?=[\s，,。；;、（(]|$)")
_LOCATION_PATTERN = re.compile(r"(?:工作地点|工作城市|base(?:地)?)[：:\s]{0,3}([一-龥]{2,4}?)(?=[\s，,。；;、（(]|$)", re.I)
_LINK_PATTERN = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
# 招聘/团队页链接识别：英文按路径段匹配（防 synchronous/achron 之类子串误命中），中文按子串
_CAREER_PATH_PATTERN = re.compile(r"(?:^|[/\_.?=&\-])(join|careers?|jobs?|recruit|zhaopin)(?:[/\_.?=&\-]|$)", re.I)
_CAREER_TEXT_TOKENS = ("招聘", "加入", "人才", "职位")
_SCRIPT_PATTERN = re.compile(r"<script[\s\S]*?</script>", re.I)
_STYLE_PATTERN = re.compile(r"<style[\s\S]*?</style>", re.I)
_TAG_PATTERN = re.compile(r"<[^>]+>")

# 全局共享源（专利/论文检索）熔断原因：超时/网络错/被拦 → 后续公司跳过该源，防重复打满超时
_DEAD_SOURCE_REASONS = {"timeout", "network_error", "blocked"}

# 人脉通道接口预留（本期不接实现；PRD §3：WebBridge 登录态只读、限速、不自动发消息）
RESERVED_CHANNELS = ("maimai",)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. schema 校验（写入硬约束：缺必备键 / 版本不符 / source_urls 为空一律拒写）
# ---------------------------------------------------------------------------

def validate_mapping_task(doc: Any) -> list[str]:
    """校验 mapping_v1 文档；返回错误列表（空=通过）。任何错误都拒绝写入。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["mapping_task 必须是对象"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {SCHEMA_VERSION}（实际：{doc.get('schema_version')}）")
    for key in REQUIRED_KEYS:
        if key not in doc:
            errors.append(f"缺必备键 {key}")
    if errors:
        return errors
    if doc.get("trigger") not in TRIGGERS:
        errors.append(f"trigger 必须是 {'/'.join(TRIGGERS)}")
    if not isinstance(doc.get("job_id"), int) or int(doc.get("job_id") or 0) <= 0:
        errors.append("job_id 必须是正整数")
    if not str(doc.get("strategy_ref") or "").strip() and doc.get("trigger") != "radar":
        # S7-2：trigger=radar 允许 strategy_ref 为空（岗位暂无 strategy_v2 时按 null 处理，
        # 由 artifact.stats.radar_context 注明）；其余 trigger 仍强制指向 strategy_v2。
        errors.append("strategy_ref 必须非空（指向 strategy_v2 artifact）")

    teams = doc.get("target_teams")
    if not isinstance(teams, list):
        errors.append("target_teams 必须是数组")
    else:
        for index, team in enumerate(teams):
            if not isinstance(team, dict):
                errors.append(f"target_teams[{index}] 必须是对象")
                continue
            for key in _TEAM_REQUIRED_KEYS:
                if key not in team:
                    errors.append(f"target_teams[{index}] 缺键 {key}")
            if not str(team.get("company") or "").strip():
                errors.append(f"target_teams[{index}].company 必须非空")
            if not str(team.get("team") or "").strip():
                errors.append(f"target_teams[{index}].team 必须非空")
            if team.get("confidence") not in CONFIDENCES:
                errors.append(f"target_teams[{index}].confidence 必须是 high/medium/low")
            evidence = team.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                errors.append(f"target_teams[{index}].evidence 必须是非空数组（每条团队定位必须带证据）")
            else:
                for item in evidence:
                    if not isinstance(item, dict) or item.get("type") not in EVIDENCE_TYPES:
                        errors.append(f"target_teams[{index}] 存在非法 evidence.type：{item}")
                        continue
                    if not str(item.get("ref") or "").strip() or not str(item.get("as_of") or "").strip():
                        errors.append(f"target_teams[{index}] evidence 缺 ref/as_of")

    candidates = doc.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates 必须是数组")
    else:
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                errors.append(f"candidates[{index}] 必须是对象")
                continue
            for key in _CANDIDATE_REQUIRED_KEYS:
                if key not in candidate:
                    errors.append(f"candidates[{index}] 缺键 {key}")
            if not str(candidate.get("name") or "").strip():
                errors.append(f"candidates[{index}].name 必须非空")
            urls = candidate.get("source_urls")
            # 防编造硬约束：无公开来源 URL 的人名一律非法（写入侧拒写锚点）
            if not isinstance(urls, list) or not [u for u in urls if str(u or "").strip()]:
                errors.append(f"candidates[{index}].source_urls 必须是非空 URL 数组（无来源人名拒绝写入）")
            if not isinstance(candidate.get("team_ref"), int) or int(candidate.get("team_ref") or 0) < 0:
                errors.append(f"candidates[{index}].team_ref 必须是非负整数")
            if candidate.get("confidence") not in CONFIDENCES:
                errors.append(f"candidates[{index}].confidence 必须是 high/medium/low")
            if candidate.get("status") not in CANDIDATE_STATUSES:
                errors.append(f"candidates[{index}].status 非法：{candidate.get('status')}")
            icebreaker = candidate.get("icebreaker")
            # S5-2：icebreaker 为可选键；一旦存在必须是任务卡 ② 结构（hooks≤3/angle 枚举/时间戳/线索回指）
            if icebreaker is not None:
                if not isinstance(icebreaker, dict):
                    errors.append(f"candidates[{index}].icebreaker 必须是对象")
                else:
                    hooks = icebreaker.get("hooks")
                    if (
                        not isinstance(hooks, list)
                        or not 1 <= len(hooks) <= 3
                        or not all(isinstance(hook, str) and hook.strip() for hook in hooks)
                    ):
                        errors.append(f"candidates[{index}].icebreaker.hooks 必须是 1-3 条非空口播句")
                    if icebreaker.get("angle") not in ICEBREAKER_ANGLES:
                        errors.append(
                            f"candidates[{index}].icebreaker.angle 必须是 {'/'.join(ICEBREAKER_ANGLES)}"
                        )
                    for key in ("generated_at", "source_ref"):
                        if not str(icebreaker.get(key) or "").strip():
                            errors.append(f"candidates[{index}].icebreaker.{key} 必须非空")

    stats = doc.get("stats")
    if not isinstance(stats, dict):
        errors.append("stats 必须是对象")
    else:
        for key in _STATS_REQUIRED_KEYS:
            if not isinstance(stats.get(key), int):
                errors.append(f"stats.{key} 必须是整数")
    return errors


# ---------------------------------------------------------------------------
# 2. 目标团队定位器（策略 v2 T1/T2 公司池 → 每家公司目标团队，证据标注；图谱优先）
# ---------------------------------------------------------------------------

def _graph_hit(graph: dict[str, dict[str, Any]], company: str) -> tuple[str, dict[str, Any]] | None:
    """图谱精确/别名命中（复用 knowledge_base 规范化口径，宁可 miss 不可错配）。"""
    target_raw = " ".join(str(company or "").split())
    target_norm = knowledge_base.normalize_client_name(company)
    if not target_norm:
        return None
    for name, info in (graph or {}).items():
        rule, _reason = knowledge_base.name_match_rule(target_raw, target_norm, name)
        if rule:
            return name, info
    return None


def _seed_directions(archetype: dict[str, Any] | None, company: str) -> tuple[list[str], str]:
    """种子原型 target_company_pool 中该公司的方向标注（如 MPS → PC/服务器/ADAS）。"""
    if not isinstance(archetype, dict):
        return [], ""
    pool = archetype.get("target_company_pool") if isinstance(archetype.get("target_company_pool"), dict) else {}
    target_norm = knowledge_base.normalize_client_name(company)
    for group in pool.values():
        if not isinstance(group, dict):
            continue
        for entry in group.get("companies") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            rule, _reason = knowledge_base.name_match_rule(" ".join(company.split()), target_norm, name)
            if rule:
                directions = [str(item) for item in entry.get("directions") or [] if str(item or "").strip()]
                return directions, name
    return [], ""


def locate_target_teams(
    strategy_doc: dict[str, Any],
    *,
    graph: dict[str, dict[str, Any]] | None = None,
    archetype: dict[str, Any] | None = None,
    tiers: tuple[str, ...] = ("T1", "T2"),
    as_of: str = "",
    radar_context: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """目标团队定位器：策略 v2 step2_target_pool 的 T1/T2 公司 → 每家公司的目标团队。

    证据优先级：公司图谱已有信息（track/business/categories）→ 种子原型方向标注；
    二者都缺的公司仍进列表（confidence=low，团队名取策略 step1 岗位本质方向），
    缺失信息交采集器补充并在留痕说明，不编造。

    S7-2：radar_context（雷达未过期信号，key=规范化公司名）只作团队定位的上下文输入——
    有新鲜信号的公司优先排在目标团队列表前面（窗口期公司先挖），信号正文/链接
    不进 target_teams 任何对外字段，只以计数形式进 trace。
    """
    as_of = as_of or _today()
    strategy_doc = strategy_doc if isinstance(strategy_doc, dict) else {}
    step1 = strategy_doc.get("step1_job_essence") if isinstance(strategy_doc.get("step1_job_essence"), dict) else {}
    essence = str(step1.get("statement") or step1.get("value_chain_role") or "")
    functions = []
    if isinstance(archetype, dict):
        functions = [str(item) for item in archetype.get("target_functions") or [] if str(item or "").strip()]
    function_label = "/".join(functions[:3]) or "目标职能"
    seed_ref = str((archetype or {}).get("source_file") or "")

    teams: list[dict[str, Any]] = []
    trace: list[str] = []
    seen: set[str] = set()
    for entry in strategy_doc.get("step2_target_pool") or []:
        if not isinstance(entry, dict) or str(entry.get("tier") or "") not in tiers:
            continue
        for company in entry.get("companies") or []:
            if not isinstance(company, dict):
                continue
            name = str(company.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            evidence: list[dict[str, str]] = []
            notes: list[str] = []
            hit = _graph_hit(graph or {}, name)
            graph_info: dict[str, Any] = {}
            if hit:
                graph_name, graph_info = hit
                evidence.append(
                    {"type": "图谱", "ref": f"kb_company_graph:{graph_name}", "as_of": as_of}
                )
            directions, seed_name = _seed_directions(archetype, name)
            if directions:
                evidence.append(
                    {"type": "图谱", "ref": f"{seed_ref or 'job_archetype'}#target_company_pool:{seed_name}", "as_of": as_of}
                )
            if directions:
                team_label = f"{'/'.join(directions[:3])} 方向 {function_label} 团队"
                confidence = "medium"
            elif graph_info:
                track = str(graph_info.get("track") or "")
                business = str(graph_info.get("business") or "")
                base = track or business or essence
                team_label = f"{base} 相关 {function_label} 团队" if base else f"{function_label} 团队"
                confidence = "low"
                notes.append("图谱仅有赛道/主营业务，团队结构待采集器补充")
            else:
                team_label = f"{essence or '目标方向'} {function_label} 团队" if essence else f"{function_label} 团队"
                confidence = "low"
                notes.append("图谱与种子原型均未覆盖该公司，团队定位待采集器补充")
            teams.append(
                {
                    "company": name,
                    "team": team_label.strip(),
                    "location": "",
                    "tier": str(entry.get("tier") or ""),
                    "evidence": evidence or [{"type": "图谱", "ref": "strategy_v2:step2_target_pool", "as_of": as_of}],
                    "confidence": confidence,
                    "notes": notes,
                }
            )
    graph_hits = sum(1 for team in teams if any(item["type"] == "图谱" and item["ref"].startswith("kb_company_graph:") for item in team["evidence"]))
    trace.append(
        f"团队定位：策略 T1/T2 公司 {len(teams)} 家（图谱直接命中 {graph_hits} 家，其余交采集器补充证据）"
    )
    # S7-2：雷达信号上下文参与定位——窗口期公司（有未过期信号）排到目标团队列表前面。
    # 只用"有无信号+条数"，信号正文/来源链接不进 target_teams 对外字段。
    if radar_context:
        signaled = [
            team for team in teams
            if _radar_context_signals(radar_context, str(team.get("company") or ""))
        ]
        if signaled:
            order = {id(team): index for index, team in enumerate(teams)}
            teams.sort(
                key=lambda team: (
                    0 if _radar_context_signals(radar_context, str(team.get("company") or "")) else 1,
                    order[id(team)],
                )
            )
            total_signals = sum(
                len(_radar_context_signals(radar_context, str(team.get("company") or ""))) for team in signaled
            )
            trace.append(
                f"雷达联动：{len(signaled)} 家目标公司有未过期雷达信号（共 {total_signals} 条），"
                "已作为定位上下文优先排前；信号内容不进任务卡对外字段"
            )
    return teams, trace


def _radar_context_signals(radar_context: dict[str, list[dict[str, Any]]], company: str) -> list[dict[str, Any]]:
    """按规范化公司名取雷达上下文信号；key 不匹配时退回别名包含匹配（宁可 miss 不可错配）。"""
    if not radar_context or not company:
        return []
    norm = knowledge_base.normalize_client_name(company)
    if not norm:
        return []
    direct = radar_context.get(norm)
    if direct:
        return list(direct)
    raw = " ".join(str(company).split())
    for key, signals in radar_context.items():
        if not key:
            continue
        shorter, longer = sorted((norm, str(key)), key=len)
        if len(shorter) >= 3 and shorter in longer:
            return list(signals)
        rule, _reason = knowledge_base.name_match_rule(raw, norm, str(key))
        if rule:
            return list(signals)
    return []


# ---------------------------------------------------------------------------
# 3. 只读线索采集器（官网团队/招聘页 + 专利公开检索；失败记 stats 不静默）
# ---------------------------------------------------------------------------

FetchResult = tuple[int, str, str]  # (http_status, body, error_category)
Fetcher = Callable[[str, float], FetchResult]


def _ssl_context() -> Any:
    """python.org 版 Python 不带系统 CA；优先 certifi，退化系统默认。只读公网页面。"""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 certifi 缺失时用默认（失败由 fetcher 分类留痕）
        return ssl.create_default_context()


def urllib_fetcher(url: str, timeout: float) -> FetchResult:
    """标准库只读抓取；失败原因分类：timeout / http_404 / blocked / http_error / network_error。"""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
            body = response.read(2_000_000).decode("utf-8", "replace")
            return int(response.status), body, ""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            category = "http_404"
        elif exc.code in (401, 403, 429):
            category = "blocked"
        else:
            category = "http_error"
        return int(exc.code), "", category
    except (socket.timeout, TimeoutError):
        return 0, "", "timeout"
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", "") or "").lower()
        return 0, "", "timeout" if "timed out" in reason else "network_error"
    except Exception:  # noqa: BLE001 采集失败一律分类留痕，不外抛
        return 0, "", "network_error"


def _visible_text(html: str) -> str:
    text = _SCRIPT_PATTERN.sub(" ", html)
    text = _STYLE_PATTERN.sub(" ", text)
    text = _TAG_PATTERN.sub(" ", text)
    return " ".join(unescape(text).split())


def looks_like_js_shell(body: str) -> bool:
    """JS 渲染壳页/页面结构变动：可见文本极少且带前端框架标记（或几乎无可见文本）。"""
    visible = _visible_text(body)
    if len(visible) >= 200:
        return False
    lowered = body.lower()
    return len(visible) < 80 or any(marker in lowered for marker in _JS_SHELL_MARKERS)


def _extract_links(base_url: str, body: str) -> list[str]:
    base_host = urllib.parse.urlparse(base_url).netloc.lower()
    base_domain = base_host[4:] if base_host.startswith("www.") else base_host
    links: list[str] = []
    for raw in _LINK_PATTERN.findall(body):
        url = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlparse(url)
        link_host = parsed.netloc.lower()
        same_site = link_host == base_domain or link_host.endswith("." + base_domain)
        if parsed.scheme not in ("http", "https") or not same_site:
            continue  # 只跟同站链接（保守，不跨站）
        path_text = urllib.parse.unquote(url.lower())
        if not (_CAREER_PATH_PATTERN.search(path_text) or any(token in path_text for token in _CAREER_TEXT_TOKENS)):
            continue
        if url not in links:
            links.append(url)
    return links


def _patent_query_url(query: str) -> str:
    return "https://patents.google.com/xhr/query?url=" + urllib.parse.quote(f"q={query}") + "&exp="


def _paper_query_url(company_key: str, term: str = "") -> str:
    """OpenAlex works：raw_affiliation_strings.search 精确过滤单位（保守），可叠加英文技术词缩小范围。"""
    url = "https://api.openalex.org/works?filter=raw_affiliation_strings.search:" + urllib.parse.quote(company_key)
    if term:
        url += "&search=" + urllib.parse.quote(term)
    return url + "&per-page=5&select=id,display_name,authorships,doi"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def parse_patent_results(body: str, *, company_frag: str, limit: int = 3) -> list[dict[str, Any]]:
    """解析 Google Patents XHR JSON；单位对不上公司的一律不取（保守）。"""
    doc = json.loads(body)
    results = ((doc.get("results") or {}).get("cluster") or []) if isinstance(doc, dict) else []
    frag = str(company_frag or "").strip().lower()
    patents: list[dict[str, Any]] = []
    for cluster in results:
        for item in (cluster or {}).get("result") or []:
            patent = (item or {}).get("patent") or {}
            publication = str(patent.get("publication_number") or "").strip()
            title = " ".join(_TAG_PATTERN.sub(" ", str(patent.get("title") or "")).split())
            assignees = _as_list(patent.get("assignee")) + _as_list(patent.get("assignee_original"))
            inventors = _as_list(patent.get("inventor"))
            if not publication or not inventors:
                continue
            if frag and not any(frag in assignee.lower() for assignee in assignees):
                continue  # 申请人单位与公司口径不符，宁缺毋滥
            patents.append(
                {
                    "publication_number": publication,
                    "title": title,
                    "assignees": assignees,
                    "inventors": inventors[:5],
                    "url": f"https://patents.google.com/patent/{publication}/zh",
                }
            )
            if len(patents) >= limit:
                return patents
    return patents


def parse_paper_results(body: str, *, company_frag: str, limit: int = 3) -> list[dict[str, Any]]:
    """解析 OpenAlex works JSON；只取作者单位标注含公司口径的（保守，单位对不上不取）。"""
    doc = json.loads(body)
    results = (doc.get("results") or []) if isinstance(doc, dict) else []
    frag = str(company_frag or "").strip().lower()
    papers: list[dict[str, Any]] = []
    for work in results:
        if not isinstance(work, dict):
            continue
        title = str(work.get("display_name") or "").strip()
        url = str(work.get("doi") or work.get("id") or "").strip()
        authors: list[dict[str, str]] = []
        for authorship in work.get("authorships") or []:
            if not isinstance(authorship, dict):
                continue
            name = str((authorship.get("author") or {}).get("display_name") or "").strip()
            affiliations = _as_list(authorship.get("raw_affiliation_strings"))
            if not name:
                continue
            if frag and not any(frag in affiliation.lower() for affiliation in affiliations):
                continue  # 单位标注与公司不符，宁缺毋滥
            authors.append({"name": name, "affiliation": affiliations[0] if affiliations else ""})
        if not url or not authors:
            continue
        papers.append({"title": title, "url": url, "authors": authors[:5]})
        if len(papers) >= limit:
            break
    return papers


class MappingCollector:
    """只读线索采集器：官网团队/招聘页 + 专利公开检索。

    边界：超时 ≤10s、每公司页面数小上限、失败（超时/404/反爬/JS 壳页/解析失败）
    全部记入 failures（原因分类），不静默；不硬编任何人名数据。
    fetcher 可注入（测试用本地 fixture，绝不打外网）。
    """

    def __init__(
        self,
        *,
        fetcher: Fetcher | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_pages: int = DEFAULT_MAX_PAGES_PER_COMPANY,
    ) -> None:
        self.fetcher = fetcher or urllib_fetcher
        self.timeout = min(float(timeout), 10.0)
        self.max_pages = max(1, int(max_pages))
        # 全局共享源熔断（专利/论文检索）：超时/网络错/被拦后置死，后续公司跳过，
        # 防同一不可达源在每家公司重复打满超时；熔断与跳过都记 failures 留痕。
        self._dead_sources: set[str] = set()

    def _fetch(self, url: str, failures: list[dict[str, str]], *, source: str) -> tuple[str | None, str]:
        status, body, error = self.fetcher(url, self.timeout)
        if error or status != 200 or not body:
            reason = error or f"http_{status}"
            failures.append(
                {
                    "source": source,
                    "url": url,
                    "reason": reason,
                    "note": f"抓取失败（HTTP {status}）" if not error else "",
                }
            )
            return None, reason
        return body, ""

    def _dead(self, source: str, failures: list[dict[str, str]], *, company: str) -> bool:
        """共享源熔断检查；已置死则记一条跳过留痕并返回 True。"""
        if source not in self._dead_sources:
            return False
        failures.append(
            {
                "source": source,
                "url": "",
                "reason": "skipped_after_failure",
                "note": f"{source}源此前已不可达，跳过 {company} 的后续请求（防重复打满超时）",
            }
        )
        return True

    def collect_company(
        self,
        company: str,
        *,
        keywords: list[str] | tuple[str, ...] = (),
        site_hint: str = "",
        patent_key: str = "",
        paper_key: str = "",
        as_of: str = "",
    ) -> dict[str, Any]:
        """采集一家公司的公开线索。返回 {evidence, clues, failures, pages_fetched, location}。"""
        as_of = as_of or _today()
        evidence: list[dict[str, str]] = []
        clues: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        location = ""
        pages = 0

        # ---- 官网团队/招聘页（同站、页面数上限内）----
        if site_hint:
            body, _reason = self._fetch(site_hint, failures, source="官网")
            if body is not None:
                pages += 1
                if looks_like_js_shell(body):
                    failures.append(
                        {
                            "source": "官网",
                            "url": site_hint,
                            "reason": "js_shell",
                            "note": "页面需 JS 渲染或结构已变动，按失败留痕不猜测",
                        }
                    )
                else:
                    evidence.append({"type": "官网", "ref": site_hint, "as_of": as_of})
                    if not location:
                        location = self._extract_location(body)
                    clues.extend(self._extract_contacts(company, site_hint, body, kind="官网"))
                    for link in _extract_links(site_hint, body):
                        if pages >= self.max_pages:
                            break
                        page, _page_reason = self._fetch(link, failures, source="招聘JD")
                        if page is None:
                            continue
                        pages += 1
                        if looks_like_js_shell(page):
                            failures.append(
                                {"source": "招聘JD", "url": link, "reason": "js_shell", "note": "页面需 JS 渲染"}
                            )
                            continue
                        evidence.append({"type": "招聘JD", "ref": link, "as_of": as_of})
                        if not location:
                            location = self._extract_location(page)
                        clues.extend(self._extract_contacts(company, link, page, kind="招聘JD"))
        else:
            failures.append({"source": "官网", "url": "", "reason": "no_site_hint", "note": "无官网线索提示，跳过官网步"})

        # ---- 专利公开检索（公司 + 技术词 → 发明人；只取姓名+单位+方向）----
        terms = [str(term).strip() for term in keywords if str(term or "").strip()][:2] or [""]
        if not self._dead("专利", failures, company=company):
            for term in terms:
                query = " ".join(part for part in (patent_key or company, term) if part)
                url = _patent_query_url(query)
                body, reason = self._fetch(url, failures, source="专利")
                if body is None:
                    if reason in _DEAD_SOURCE_REASONS:
                        self._dead_sources.add("专利")
                        break
                    continue
                try:
                    patents = parse_patent_results(body, company_frag=patent_key or company, limit=3)
                except ValueError:
                    failures.append({"source": "专利", "url": url, "reason": "parse_error", "note": "检索返回结构已变动"})
                    continue
                for patent in patents:
                    for inventor in patent["inventors"]:
                        clues.append(
                            {
                                "kind": "专利",
                                "company": company,
                                "name": inventor,
                                "current_role": f"{company} 研发（专利发明人）",
                                "source_url": patent["url"],
                                "confidence": "medium",
                                "reason": f"{company} {term or '技术'}方向公开专利《{patent['title']}》（{patent['publication_number']}）发明人",
                            }
                        )

        # ---- 论文公开检索（OpenAlex 单位精确过滤；只取单位标注含公司的作者）----
        if not self._dead("论文", failures, company=company):
            company_key = paper_key or patent_key or company
            ascii_term = next((t for t in terms if t and t.isascii()), "")
            papers: list[dict[str, Any]] = []
            for attempt_term in (ascii_term, "") if ascii_term else ("",):
                url = _paper_query_url(company_key, attempt_term)
                body, reason = self._fetch(url, failures, source="论文")
                if body is None:
                    if reason in _DEAD_SOURCE_REASONS:
                        self._dead_sources.add("论文")
                    break
                try:
                    papers = parse_paper_results(body, company_frag=company_key, limit=3)
                except ValueError:
                    papers = []
                    failures.append({"source": "论文", "url": url, "reason": "parse_error", "note": "检索返回结构已变动"})
                    break
                if papers or not attempt_term:
                    break  # 有结果或已是兜底（仅公司口径）查询
            for paper in papers:
                for author in paper["authors"]:
                    affiliation = author.get("affiliation") or company
                    clues.append(
                        {
                            "kind": "论文",
                            "company": company,
                            "name": author["name"],
                            "current_role": f"{company} 技术论文作者",
                            "source_url": paper["url"],
                            "confidence": "medium",
                            "reason": f"{company} 相关公开论文《{paper['title']}》作者（单位标注：{affiliation}）",
                        }
                    )

        return {
            "evidence": evidence,
            "clues": clues,
            "failures": failures,
            "pages_fetched": pages,
            "location": location,
        }

    @staticmethod
    def _extract_location(body: str) -> str:
        match = _LOCATION_PATTERN.search(_visible_text(body))
        return match.group(1) if match else ""

    @staticmethod
    def _extract_contacts(company: str, url: str, body: str, *, kind: str) -> list[dict[str, Any]]:
        """保守人脉提取：只取页面显式标注的“联系人/作者/撰文”，无标注不猜。"""
        text = _visible_text(body)
        clues: list[dict[str, Any]] = []
        for match in _CONTACT_PATTERN.finditer(text):
            name = match.group(1)
            clues.append(
                {
                    "kind": kind,
                    "company": company,
                    "name": name,
                    "current_role": f"{company} 页面公开联系人",
                    "source_url": url,
                    "confidence": "low",
                    "reason": f"{company}{kind}公开页显式标注的联系人/作者",
                }
            )
        return clues[:3]


class MaimaiCollector:
    """脉脉通道：本期只做接口预留（PRD §3：WebBridge 登录态只读、限速、不自动发消息）。

    S5-1 不接实现；调用返回空线索 + 预留说明，绝不触发登录/翻页/发消息。
    """

    channel = "maimai"

    def collect_company(self, company: str, **_: Any) -> dict[str, Any]:
        return {
            "evidence": [],
            "clues": [],
            "failures": [],
            "pages_fetched": 0,
            "location": "",
            "note": "脉脉采集为接口预留，本期未启用",
        }


# ---------------------------------------------------------------------------
# 4. 名单生成（线索 → 候选目标人；禁挖过滤；无来源拒写）
# ---------------------------------------------------------------------------

def _is_banned(company: str, banned: list[str] | tuple[str, ...]) -> bool:
    target_raw = " ".join(str(company or "").split())
    target_norm = knowledge_base.normalize_client_name(company)
    if not target_norm:
        return False
    for item in banned:
        rule, _reason = knowledge_base.name_match_rule(target_raw, target_norm, str(item or ""))
        if rule:
            return True
    return False


def build_candidates(
    clues: list[dict[str, Any]],
    *,
    banned: list[str] | tuple[str, ...] = (),
    mask_names: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """线索 → 候选目标人（两步走的第二步）。

    - 禁挖公司的人不进名单（stats_delta.banned_filtered）；
    - source_url 为空的线索整条拒收（stats_delta.rejected_no_source，防编造硬约束）；
    - 同名同公司去重合并来源 URL（上限 MAX_CANDIDATE_SOURCE_URLS）；
    - 人名按 workflow._mask_candidate_name 口径遮罩存储（全名可由顾问沿 source_urls 回溯）。
    """
    stats = {"banned_filtered": 0, "rejected_no_source": 0}
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for clue in clues:
        if not isinstance(clue, dict):
            continue
        company = str(clue.get("company") or "").strip()
        name = str(clue.get("name") or "").strip()
        if not company or not name:
            stats["rejected_no_source"] += 1
            continue
        if _is_banned(company, banned):
            stats["banned_filtered"] += 1
            continue
        url = str(clue.get("source_url") or "").strip()
        if not url:
            stats["rejected_no_source"] += 1
            continue  # 硬约束：无来源人名不进名单
        key = (knowledge_base.normalize_client_name(company), name)
        if key not in merged:
            merged[key] = {
                "name": _mask_candidate_name(name) if mask_names else name,
                "current_role": str(clue.get("current_role") or ""),
                "team_ref": int(clue.get("team_ref") or 0),
                "source_urls": [url],
                "confidence": str(clue.get("confidence") or "low"),
                "reason": str(clue.get("reason") or ""),
                "status": "pending",
                "consultant_note": "",
                "_company": company,
            }
            order.append(key)
        else:
            urls = merged[key]["source_urls"]
            if url not in urls and len(urls) < MAX_CANDIDATE_SOURCE_URLS:
                urls.append(url)
            if clue.get("confidence") == "high":
                merged[key]["confidence"] = "high"
    candidates = []
    for key in order:
        candidate = merged.pop(key)
        candidate.pop("_company", None)
        candidates.append(candidate)
    return candidates, stats


# ---------------------------------------------------------------------------
# 5. 组装 + 持久化（复用 agent_artifacts，幂等 upsert）
# ---------------------------------------------------------------------------

def build_mapping_task(
    *,
    job_id: int,
    trigger: str,
    strategy_ref: str,
    strategy_doc: dict[str, Any] | None,
    client: str = "",
    job_title: str = "",
    graph: dict[str, dict[str, Any]] | None = None,
    archetype: dict[str, Any] | None = None,
    collector: MappingCollector | None = None,
    banned: list[str] | tuple[str, ...] | None = None,
    kb_dir: str | None = None,
    as_of: str = "",
    radar_context: dict[str, list[dict[str, Any]]] | None = None,
    radar_company: str = "",
    radar_scan_ref: str = "",
) -> dict[str, Any]:
    """组装 mapping_v1 文档：团队定位 → 线索采集 → 名单生成 → stats。

    banned=None 时按客户读 restricted 白名单（load_restricted_constraints）；
    显式传 [] 表示无禁挖（测试注入）。
    S7-2：trigger=radar 时 radar_context（未过期雷达信号）注入团队定位上下文，
    radar_company/radar_scan_ref 只以标记形式进 stats.radar_context（不含信号正文/链接）。
    """
    as_of = as_of or _today()
    if banned is None:
        restricted, restricted_trace = knowledge_base.load_restricted_constraints(client, kb_dir=kb_dir)
        constraints = (restricted or {}).get("constraints") if isinstance(restricted, dict) else {}
        banned = [str(item) for item in (constraints or {}).get("banned_companies") or [] if str(item or "").strip()]
    else:
        restricted_trace = []
    banned = [str(item) for item in banned if str(item or "").strip()]

    teams, trace = locate_target_teams(strategy_doc or {}, graph=graph, archetype=archetype, as_of=as_of, radar_context=radar_context)
    trace.extend(restricted_trace)

    collector = collector or MappingCollector()
    keywords = _tech_keywords(strategy_doc or {}, archetype)
    failures: list[dict[str, str]] = []
    clues: list[dict[str, Any]] = []
    pages_fetched = 0
    for index, team in enumerate(teams):
        company = team["company"]
        hints = COMPANY_SOURCE_HINTS.get(company, {})
        result = collector.collect_company(
            company,
            keywords=keywords,
            site_hint=str(hints.get("site") or ""),
            patent_key=str(hints.get("patent_key") or ""),
            paper_key=str(hints.get("paper_key") or ""),
            as_of=as_of,
        )
        pages_fetched += int(result.get("pages_fetched") or 0)
        failures.extend(result.get("failures") or [])
        # 采集证据并入团队证据（按 ref 去重）；地域缺失时由 JD 证据回填
        existing_refs = {item.get("ref") for item in team["evidence"]}
        for item in result.get("evidence") or []:
            if item.get("ref") not in existing_refs:
                team["evidence"].append(item)
                existing_refs.add(item.get("ref"))
        if not team["location"] and result.get("location"):
            team["location"] = str(result["location"])
        if any(item["type"] in ("官网", "招聘JD") for item in team["evidence"]):
            team["confidence"] = "high"
        for clue in result.get("clues") or []:
            clue["team_ref"] = index
            clues.append(clue)

    candidates, candidate_stats = build_candidates(clues, banned=banned)

    # 失败留痕压缩：共享源熔断后的逐公司 skip 记录汇总为一条（防淹没真实失败明细）
    compressed_failures: list[dict[str, str]] = []
    skip_counts: dict[str, int] = {}
    for failure in failures:
        if failure.get("reason") == "skipped_after_failure":
            skip_counts[failure.get("source", "")] = skip_counts.get(failure.get("source", ""), 0) + 1
        else:
            compressed_failures.append(failure)
    for source, count in sorted(skip_counts.items()):
        compressed_failures.append(
            {
                "source": source,
                "url": "",
                "reason": "skipped_after_failure",
                "note": f"{source}源不可达已熔断，{count} 家公司的后续请求已跳过（防重复打满超时）",
            }
        )
    failures = compressed_failures

    source_counts: dict[str, int] = {}
    for team in teams:
        for item in team["evidence"]:
            source_counts[item["type"]] = source_counts.get(item["type"], 0) + 1
    patent_clues = sum(1 for clue in clues if clue.get("kind") == "专利")
    if patent_clues:
        source_counts["专利"] = patent_clues
    paper_clues = sum(1 for clue in clues if clue.get("kind") == "论文")
    if paper_clues:
        source_counts["论文"] = paper_clues

    doc = {
        "schema_version": SCHEMA_VERSION,
        "trigger": trigger,
        "job_id": int(job_id),
        "strategy_ref": str(strategy_ref),
        "client": str(client or ""),
        "job_title": str(job_title or ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_teams": teams,
        "candidates": candidates,
        "stats": {
            "teams": len(teams),
            "candidates": len(candidates),
            "confirmed": 0,
            "intaken": 0,
            "clues": len(clues),
            "banned_filtered": candidate_stats["banned_filtered"],
            "rejected_no_source": candidate_stats["rejected_no_source"],
            "pages_fetched": pages_fetched,
            "failures_count": len(failures),
            "failures": failures[:MAX_FAILURES_KEPT],
            "sources": source_counts,
        },
        "trace": trace,
        "red_lines": [
            "不自动触达：名单仅供顾问本人决策与行动",
            "无公开来源 URL 的人名一律不进名单（已拒收计数见 stats.rejected_no_source）",
            "禁挖名单照常生效（已过滤计数见 stats.banned_filtered）",
            "后续确认入库走现有 preflight/commit 链路，Mapping 不写第二条 job_candidates",
        ],
    }
    if trigger == "radar":
        # S7-2 雷达联动标记（验收锚点）：只记公司名/计数/来源榜单 id，信号正文与链接不进 artifact。
        pool_companies = [str(team.get("company") or "") for team in teams]
        signaled = [
            name for name in pool_companies if radar_context and _radar_context_signals(radar_context, name)
        ]
        radar_marker: dict[str, Any] = {
            "applied": bool(radar_context),
            "company": str(radar_company or ""),
            "company_in_pool": any(
                radar_company and _radar_context_signals({knowledge_base.normalize_client_name(radar_company): [{}]}, name)
                for name in pool_companies
            ) if radar_company else False,
            "pool_companies_with_signals": len(signaled),
            "signals_used": sum(len(_radar_context_signals(radar_context or {}, name)) for name in signaled),
            "scan_artifact": str(radar_scan_ref or ""),
            "note": "雷达信号只作团队定位上下文，信号内容不进任务卡对外字段",
        }
        if not str(strategy_ref or "").strip():
            radar_marker["strategy_ref_missing"] = "该岗位暂无 strategy_v2，strategy_ref 按 null 处理"
        doc["stats"]["radar_context"] = radar_marker
        doc["red_lines"].append("雷达联动：信号仅作定位上下文， Mapping 名单红线不变（无来源不进名单、禁挖过滤、不自动触达）")
    return doc


def _tech_keywords(strategy_doc: dict[str, Any], archetype: dict[str, Any] | None) -> list[str]:
    """采集用技术词：策略 step4 词组优先（剔除公司名与职能词），其次原型方向产品词；取不到留空。"""
    company_names = {
        str(company.get("name") or "").strip()
        for entry in strategy_doc.get("step2_target_pool") or []
        if isinstance(entry, dict)
        for company in entry.get("companies") or []
        if isinstance(company, dict) and str(company.get("name") or "").strip()
    }
    function_words = {"TME", "FAE", "AE", "电源工程师", "技术市场", "产品定义", "客户技术推广", "design-in", "design-win"}
    terms: list[str] = []
    for group in strategy_doc.get("step4_keyword_groups") or []:
        if not isinstance(group, dict):
            continue
        for term in group.get("terms") or []:
            text = str(term or "").strip()
            if text and text not in company_names and text not in function_words and text not in terms:
                terms.append(text)
    if not terms and isinstance(archetype, dict):
        for direction in archetype.get("directions") or []:
            if not isinstance(direction, dict):
                continue
            for product in direction.get("products") or []:
                text = str(product or "").strip()
                if text and text not in terms:
                    terms.append(text)
    return terms[:4]


def _task_content(doc: dict[str, Any]) -> str:
    """artifact content（markdown，业务语言，UX-1；不裸露技术枚举码）。"""
    stats = doc.get("stats") or {}
    lines = [
        f"# Mapping 直挖任务卡：{doc.get('client')} {doc.get('job_title')}",
        "",
        f"- 发起方式：{TRIGGER_LABELS.get(str(doc.get('trigger')), doc.get('trigger'))}",
        f"- 生成时间：{doc.get('generated_at')}",
        f"- 目标团队 {stats.get('teams', 0)} 个；候选目标人 {stats.get('candidates', 0)} 位"
        f"（已确认 {stats.get('confirmed', 0)}、已入库 {stats.get('intaken', 0)}；"
        f"线索 {stats.get('clues', 0)} 条，禁挖过滤 {stats.get('banned_filtered', 0)} 条，"
        f"无来源拒收 {stats.get('rejected_no_source', 0)} 条）",
        f"- 采集留痕：抓取页面 {stats.get('pages_fetched', 0)} 个，失败 {stats.get('failures_count', 0)} 次（明细见 stats.failures）",
        "",
        "## 目标团队",
    ]
    for team in doc.get("target_teams") or []:
        refs = "；".join(f"{item.get('type')}:{item.get('ref')}" for item in team.get("evidence") or [])
        location = f"（{team['location']}）" if team.get("location") else ""
        lines.append(f"- {team.get('company')}{location}｜{team.get('team')}｜证据：{refs}")
    lines.append("")
    lines.append("## 候选目标人（姓名遮罩，全名沿来源链接回溯；行动由顾问本人执行）")
    for candidate in doc.get("candidates") or []:
        team_ref = int(candidate.get("team_ref") or 0)
        company = ""
        teams = doc.get("target_teams") or []
        if 0 <= team_ref < len(teams):
            company = str(teams[team_ref].get("company") or "")
        status = str(candidate.get("status") or "")
        status_label = CANDIDATE_STATUS_LABELS.get(status, status)
        line = (
            f"- {candidate.get('name')}｜{company}｜{candidate.get('current_role')}｜状态：{status_label}｜"
            f"来源：{'；'.join(candidate.get('source_urls') or [])}｜{candidate.get('reason')}"
        )
        note = str(candidate.get("consultant_note") or "").strip()
        if note:
            line += f"｜备注：{note}"
        lines.append(line)
        icebreaker = candidate.get("icebreaker")
        # S5-2：开场白要点只展示不发送（发送动作永远由顾问本人执行）
        if isinstance(icebreaker, dict):
            lines.append(f"  - 开场白要点（{icebreaker.get('angle')}，只读不发送）：")
            for hook in icebreaker.get("hooks") or []:
                lines.append(f"    - {hook}")
        intake = candidate.get("intake")
        if isinstance(intake, dict) and intake.get("job_candidate_id"):
            lines.append(f"  - 已入库：job_candidate_id={intake.get('job_candidate_id')}（{intake.get('intaken_at')}）")
    lines.extend(["", "```json", json.dumps(doc, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines)


def upsert_mapping_task(conn: Any, doc: dict[str, Any]) -> str:
    """校验 + 幂等 upsert（同工作流重算覆盖，version 自增 + history，上限 10 条）。

    校验不过（含任何候选缺 source_urls）抛 ValueError，整条拒写。
    """
    errors = validate_mapping_task(doc)
    if errors:
        raise ValueError("mapping_task 校验失败，拒绝写入：" + "；".join(errors))
    workflow_id = str(doc.get("workflow_id") or "")
    existing = conn.execute(
        """
        SELECT artifact_id,metadata_json FROM agent_artifacts
        WHERE workflow_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1
        """,
        (workflow_id, ARTIFACT_TYPE),
    ).fetchone()
    title = f"Mapping 直挖任务卡：{doc.get('client')} {doc.get('job_title')}".strip("： ")
    if existing:
        previous = _loads(existing["metadata_json"], {})
        history = list(previous.get("history") or [])
        history.append(
            {
                "version": int(previous.get("version") or 1),
                "teams": (previous.get("stats") or {}).get("teams"),
                "candidates": (previous.get("stats") or {}).get("candidates"),
                "generated_at": previous.get("generated_at"),
            }
        )
        doc["version"] = int(previous.get("version") or 1) + 1
        doc["history"] = history[-10:]
        artifact_id = str(existing["artifact_id"])
        conn.execute(
            """
            UPDATE agent_artifacts SET content=?,metadata_json=?,validation_status='passed',title=?
            WHERE artifact_id=?
            """,
            (_task_content(doc), _dumps(doc), f"{title} v{doc['version']}", artifact_id),
        )
        return artifact_id
    doc["version"] = 1
    doc["history"] = []
    artifact_id = f"mapping_task_{workflow_id}"
    conn.execute(
        """
        INSERT INTO agent_artifacts
        (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            artifact_id,
            str(doc.get("goal_id") or ""),
            workflow_id,
            None,
            ARTIFACT_TYPE,
            f"{title} v1",
            "text/markdown",
            None,
            _task_content(doc),
            _dumps(doc),
            "passed",
        ),
    )
    return artifact_id


def get_mapping_task(conn: Any, artifact_id: str) -> dict[str, Any] | None:
    """按 artifact_id 读取 mapping_task；不存在返回 None。"""
    row = conn.execute(
        """
        SELECT artifact_id,goal_id,workflow_id,title,content,metadata_json,created_at
        FROM agent_artifacts WHERE artifact_id=? AND artifact_type=?
        """,
        (str(artifact_id), ARTIFACT_TYPE),
    ).fetchone()
    if row is None:
        return None
    doc = _loads(row["metadata_json"], {})
    return {
        "artifact_id": str(row["artifact_id"]),
        "goal_id": str(row["goal_id"] or ""),
        "workflow_id": str(row["workflow_id"] or ""),
        "title": str(row["title"] or ""),
        "content": str(row["content"] or ""),
        "mapping_task": doc,
        "created_at": str(row["created_at"] or ""),
    }


# ---------------------------------------------------------------------------
# 6. S5-2：候选状态机 PATCH + 逐人破冰素材 + 入库动作
# ---------------------------------------------------------------------------

# 七态业务标签（前端契约：业务语言，UX-1）
CANDIDATE_STATUS_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "contacted": "已接触",
    "replied": "已回复",
    "intaken": "已入库",
    "parked": "已搁置",
    "rejected": "已淘汰",
}

# 状态机迁移表（PATCH 允许的目标态集合）：
# - 主链只许前进：pending→confirmed→contacted→replied；intaken 只能经入库动作到达（不开旁路）；
# - intaken/rejected 为终态：intaken 禁止倒退，rejected 是软删终态，均不再变更；
# - parked 是旁路：主链各态可搁置，搁置后只能恢复 pending 或淘汰；
# - 同态 PATCH 视为幂等无操作（放行，便于前端重试）。
STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("confirmed", "parked", "rejected"),
    "confirmed": ("contacted", "parked", "rejected"),
    "contacted": ("replied", "parked", "rejected"),
    "replied": ("parked", "rejected"),
    "intaken": (),
    "parked": ("pending", "rejected"),
    "rejected": (),
}
_TERMINAL_STATUSES = ("intaken", "rejected")


def validate_status_transition(current: Any, target: Any) -> str | None:
    """校验一次状态迁移；合法返回 None，非法返回中文错误（人话，供 409 透出）。"""
    current_text = str(current or "")
    target_text = str(target or "").strip()
    if target_text not in CANDIDATE_STATUSES:
        return f"未知状态：{target_text}（七态：{'/'.join(CANDIDATE_STATUSES)}）"
    if target_text == current_text:
        return None  # 同态幂等无操作
    if target_text == "intaken":
        return "intaken 只能经入库动作（intake）到达，PATCH 不支持直接置入库"
    if current_text in _TERMINAL_STATUSES:
        return f"{current_text} 是终态，禁止状态变更（intaken 不倒退，rejected 为软删终态）"
    allowed = STATUS_TRANSITIONS.get(current_text, ())
    if target_text not in allowed:
        return f"非法状态迁移：{current_text} → {target_text}（允许：{'/'.join(allowed) or '无'}）"
    return None


def _sync_stats(doc: dict[str, Any]) -> None:
    """状态变更后同步 stats：confirmed=曾被确认（confirmed/contacted/replied/intaken），intaken=已入库。"""
    stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
    candidates = doc.get("candidates") or []
    stats["candidates"] = len(candidates)
    stats["confirmed"] = sum(
        1 for item in candidates if item.get("status") in ("confirmed", "contacted", "replied", "intaken")
    )
    stats["intaken"] = sum(1 for item in candidates if item.get("status") == "intaken")
    doc["stats"] = stats


def save_mapping_task_in_place(conn: Any, artifact_id: str, doc: dict[str, Any]) -> None:
    """状态机/备注/破冰/入库回执的原位更新：整批先校验后落库；version 不 bump、history 不动。"""
    errors = validate_mapping_task(doc)
    if errors:
        raise ValueError("mapping_task 校验失败，拒绝写入：" + "；".join(errors))
    conn.execute(
        """
        UPDATE agent_artifacts SET content=?,metadata_json=?,validation_status='passed'
        WHERE artifact_id=? AND artifact_type=?
        """,
        (_task_content(doc), _dumps(doc), str(artifact_id), ARTIFACT_TYPE),
    )


def _candidate_team(doc: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    teams = doc.get("target_teams") or []
    team_ref = candidate.get("team_ref")
    if isinstance(team_ref, int) and 0 <= team_ref < len(teams) and isinstance(teams[team_ref], dict):
        return teams[team_ref]
    return {}


# ---- 破冰素材（规则版：不依赖 LLM；hooks 必须引用真实线索实词，反模板硬约束）----

ICEBREAKER_ANGLES = ("技术共鸣", "职业发展", "地域", "客户平台")
MAX_ICEBREAKER_HOOKS = 3

# 话术红线（restricted 边界）：费率/红线/承诺类措辞与手机号永不进 hooks
_ICEBREAKER_FORBIDDEN_TOKENS = ("费率", "话术红线", "红线", "保底", "承诺", "代您")
_PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")

_ICE_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.#/-]{1,}")
_ICE_CHINESE_RUN = re.compile(r"[一-龥]{2,}")
# 英文虚词/泛词不进关键词（防模板判定被 "of/the/company" 之类稀释）
_ICE_ASCII_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "at", "by", "with", "from",
    "into", "using", "based", "via", "new", "their", "its", "this", "that", "without", "between",
    "need", "needs", "inc", "ltd", "corp", "corporation", "company", "co", "llc", "gmbh",
    "university", "college", "institute", "china", "usa",
    "analysis", "analytical", "study", "design", "method", "methods", "model", "modeling", "approach",
}
# 中文泛词不进关键词（行业通称谁都能说，不构成"该候选的真实线索"）
_ICE_CHINESE_GENERIC = {
    "相关", "公开", "论文", "作者", "单位", "标注", "方向", "团队", "发明人", "专利", "联系人",
    "页面", "公司", "有限", "股份", "半导体", "微电子", "技术", "研发", "工程师", "一种",
}
_ICE_TEAM_FILLER = ("方向", "团队", "相关")


def _reason_clues(reason: Any) -> dict[str, str]:
    """从候选 reason 抽取结构化线索：论文/专利题《》、单位标注、专利公开号、线索类型。"""
    text = str(reason or "")
    clues = {"paper_title": "", "affiliation": "", "patent_no": "", "clue_kind": ""}
    title = re.search(r"《([^》]+)》", text)
    if title:
        clues["paper_title"] = title.group(1).strip()
    affiliation = re.search(r"单位标注[：:]\s*([^）)]+)", text)
    if affiliation:
        clues["affiliation"] = affiliation.group(1).strip()
    patent = re.search(r"（([A-Z]{2}\d{4,}[A-Z0-9]*)）", text)
    if patent:
        clues["patent_no"] = patent.group(1)
    # 线索类型：带公开号或文本含"专利" → 专利；否则按论文（官网联系人线索无《》则为空）
    if clues["paper_title"]:
        clues["clue_kind"] = "专利" if (clues["patent_no"] or "专利" in text) else "论文"
    return clues


def _icebreaker_clues(candidate: dict[str, Any], team: dict[str, Any]) -> dict[str, Any]:
    """汇集该候选可用于破冰的真实线索与关键词集合（论文题/单位/团队名/职务实词）。"""
    team = team if isinstance(team, dict) else {}
    company = str(team.get("company") or "").strip()
    team_label = str(team.get("team") or "").strip()
    location = str(team.get("location") or "").strip()
    role = str(candidate.get("current_role") or "").strip()
    reason_clues = _reason_clues(candidate.get("reason"))

    keywords: list[str] = []

    def add(value: str) -> None:
        word = str(value or "").strip()
        if word and word not in keywords:
            keywords.append(word)

    add(company)
    if location:
        add(location)
    for token in re.split(r"[/、\s（()）]+", team_label):
        token = token.strip()
        if token and token not in _ICE_TEAM_FILLER:
            add(token)
    paper_text = f"{reason_clues['paper_title']} {reason_clues['affiliation']} {role}"
    for word in _ICE_ASCII_TOKEN.findall(paper_text):
        if len(word) >= 3 and word.lower() not in _ICE_ASCII_STOPWORDS:
            add(word)
        elif word.upper() in ("PC", "AE", "DC", "AC", "IC", "3D"):
            add(word)
    for run in _ICE_CHINESE_RUN.findall(f"{paper_text} {team_label}"):
        run = re.sub(r"^一种", "", run)
        if len(run) >= 2 and run not in _ICE_CHINESE_GENERIC:
            add(run)

    # 论文题专属关键词（hook 1 选用：优先词中/词尾带大写、连字符或数字的实词，其次最长词）
    paper_keywords: list[str] = []
    for word in _ICE_ASCII_TOKEN.findall(reason_clues["paper_title"]):
        if len(word) >= 3 and word.lower() not in _ICE_ASCII_STOPWORDS:
            paper_keywords.append(word)
    for run in _ICE_CHINESE_RUN.findall(reason_clues["paper_title"]):
        run = re.sub(r"^一种", "", run)
        if len(run) >= 2 and run not in _ICE_CHINESE_GENERIC:
            paper_keywords.append(run)
    paper_keywords.sort(
        key=lambda item: (not (re.search(r"[A-Z]", item[1:]) or re.search(r"[0-9-]", item)), -len(item))
    )

    return {
        "company": company,
        "team_label": team_label,
        "location": location,
        "role": role,
        "paper_title": reason_clues["paper_title"],
        "affiliation": reason_clues["affiliation"],
        "patent_no": reason_clues["patent_no"],
        "clue_kind": reason_clues["clue_kind"],
        "keywords": keywords,
        "paper_keywords": paper_keywords,
    }


def _hook_cites_clue(hook: str, keywords: list[str]) -> bool:
    lowered = hook.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def icebreaker_quality_errors(
    icebreaker: Any, candidate: dict[str, Any], team: dict[str, Any]
) -> list[str]:
    """破冰素材质量门禁（反模板硬约束）。返回错误列表（空=合格）。

    - 结构：hooks 1-3 条非空口播句、angle 四选一、generated_at/source_ref 非空；
    - 反模板：全部 hooks 不含该候选任何线索关键词（论文题/单位/团队实词）→ 判不合格；
    - 红线：费率/红线/承诺类措辞、手机号 → 判不合格。
    """
    if not isinstance(icebreaker, dict):
        return ["icebreaker 必须是对象"]
    errors: list[str] = []
    hooks = icebreaker.get("hooks")
    hooks_ok = (
        isinstance(hooks, list)
        and 1 <= len(hooks) <= MAX_ICEBREAKER_HOOKS
        and all(isinstance(hook, str) and hook.strip() for hook in hooks)
    )
    if not hooks_ok:
        errors.append(f"hooks 必须是 1-{MAX_ICEBREAKER_HOOKS} 条非空口播句")
        hooks = []
    if icebreaker.get("angle") not in ICEBREAKER_ANGLES:
        errors.append(f"angle 必须是 {'/'.join(ICEBREAKER_ANGLES)}")
    if not str(icebreaker.get("generated_at") or "").strip():
        errors.append("generated_at 必须非空")
    if not str(icebreaker.get("source_ref") or "").strip():
        errors.append("source_ref 必须非空（回指所用线索）")
    keywords = _icebreaker_clues(candidate, team)["keywords"]
    if not keywords:
        errors.append("该候选没有可引用的真实线索关键词（论文题/单位/团队/职务缺失）")
    if hooks and keywords and not any(_hook_cites_clue(hook, keywords) for hook in hooks):
        errors.append("hooks 不含该候选任何线索关键词（论文题/单位/团队实词），判为泛泛模板，拒绝写入")
    for hook in hooks:
        if any(token in hook for token in _ICEBREAKER_FORBIDDEN_TOKENS) or _PHONE_PATTERN.search(hook):
            errors.append("hooks 触碰话术红线（费率/红线/承诺/电话），拒绝写入")
            break
    return errors


def build_icebreaker(
    candidate: dict[str, Any], team: dict[str, Any], *, client: str = "", now: str = ""
) -> dict[str, Any]:
    """规则版破冰素材：hooks 全部由该候选真实线索拼装（口语句，顾问可直接念）。

    无线索关键词或质量门禁不过 → 抛 ValueError（判不合格并拒绝写入）。
    只产素材不触达：发送动作永远由顾问本人执行。
    """
    clues = _icebreaker_clues(candidate, team)
    if not clues["keywords"]:
        raise ValueError("该候选没有可引用的真实线索（论文题/单位/团队/职务关键词缺失），无法生成开场白要点")
    hooks: list[str] = []
    company = clues["company"]
    if clues["paper_title"] and clues["clue_kind"] == "专利":
        tech = clues["paper_keywords"][0] if clues["paper_keywords"] else ""
        direction = f"{tech}这个方向" if tech else "这个方向"
        hooks.append(f"注意到您名下的公开专利《{clues['paper_title']}》，{direction}跟我们客户在做的贴得很近，想跟您请教两句")
    elif clues["paper_title"]:
        tech = clues["paper_keywords"][0] if clues["paper_keywords"] else ""
        direction = f"{tech}这个方向" if tech else "这个方向"
        hooks.append(f"看到您发的《{clues['paper_title']}》，{direction}我们客户这边正好在重点投入，想跟您请教两句")
    if company:
        team_tokens = [
            token
            for token in re.split(r"[/、\s（()）]+", clues["team_label"])
            if token.strip() and token.strip() not in _ICE_TEAM_FILLER
        ]
        team_core = f"{'/'.join(team_tokens[:3])}团队" if team_tokens else "团队"
        hooks.append(f"您现在在{company} {team_core}这块，到我们客户这个平台是平移还是往上走，想听听您的判断")
    if clues["location"]:
        hooks.append(f"看您base在{clues['location']}，客户团队也在{clues['location']}，真有机会动起来同城聊也方便")
    if clues["patent_no"] and clues["clue_kind"] != "专利" and len(hooks) < MAX_ICEBREAKER_HOOKS:
        hooks.append(f"注意到您名下{clues['patent_no']}这篇公开专利，跟我们客户在做的方向贴得很近")
    deduped: list[str] = []
    for hook in hooks:
        if hook not in deduped:
            deduped.append(hook)
    hooks = deduped[:MAX_ICEBREAKER_HOOKS]
    if not hooks:
        raise ValueError("该候选没有可拼装的真实线索，无法生成开场白要点")
    if clues["paper_title"] or clues["patent_no"]:
        angle = "技术共鸣"
    elif clues["location"]:
        angle = "地域"
    else:
        angle = "职业发展"
    source_urls = [str(url).strip() for url in candidate.get("source_urls") or [] if str(url or "").strip()]
    source_ref = source_urls[0] if source_urls else ""
    if clues["paper_title"]:
        source_ref += f"（{clues['clue_kind'] or '线索'}《{clues['paper_title']}》）"
    elif clues["patent_no"]:
        source_ref += f"（专利 {clues['patent_no']}）"
    icebreaker = {
        "hooks": hooks,
        "angle": angle,
        "generated_at": now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_ref": source_ref,
    }
    errors = icebreaker_quality_errors(icebreaker, candidate, team)
    if errors:
        raise ValueError("破冰素材质量不合格，拒绝写入：" + "；".join(errors))
    return icebreaker


# ---- 状态机 PATCH（整批先校验后落库；confirmed 触发自动生成破冰素材）----

def apply_candidate_update(
    conn: Any,
    artifact_id: str,
    index: int,
    *,
    status: str | None = None,
    consultant_note: str | None = None,
) -> dict[str, Any]:
    """PATCH 候选状态/备注。artifact 不存在或 index 越界抛 LookupError（404）；
    未知态/非法迁移/终态变更/直接置 intaken 抛 ValueError（409）。
    confirmed 迁移成功时自动生成破冰素材；素材质量不合格不阻断状态变更，
    但不写入 icebreaker（错误进响应 icebreaker_errors，由前端提示顾问）。
    """
    if status is None and consultant_note is None:
        raise ValueError("PATCH 至少需要 status 或 consultant_note 之一")
    payload = get_mapping_task(conn, artifact_id)
    if payload is None:
        raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
    doc = payload["mapping_task"]
    candidates = doc.get("candidates") or []
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        raise LookupError(f"候选不存在：index={index}")
    candidate = dict(candidates[index])
    current = str(candidate.get("status") or "")
    icebreaker_generated = False
    icebreaker_errors: list[str] = []
    if status is not None:
        error = validate_status_transition(current, status)
        if error:
            raise ValueError(error)
        candidate["status"] = str(status).strip()
        if candidate["status"] == "confirmed" and current != "confirmed":
            team = _candidate_team(doc, candidate)
            try:
                candidate["icebreaker"] = build_icebreaker(candidate, team, client=str(doc.get("client") or ""))
                icebreaker_generated = True
            except ValueError as exc:
                icebreaker_errors.append(str(exc))
    if consultant_note is not None:
        candidate["consultant_note"] = str(consultant_note)
    candidates[index] = candidate
    _sync_stats(doc)
    save_mapping_task_in_place(conn, artifact_id, doc)
    return {
        "ok": True,
        "artifact_id": str(artifact_id),
        "index": index,
        "candidate": candidate,
        "status": candidate["status"],
        "status_label": CANDIDATE_STATUS_LABELS.get(candidate["status"], candidate["status"]),
        "stats": doc["stats"],
        "icebreaker_generated": icebreaker_generated,
        "icebreaker_errors": icebreaker_errors,
    }


def regenerate_icebreaker(conn: Any, artifact_id: str, index: int) -> dict[str, Any]:
    """重新生成破冰素材（人选卡"重新生成"按钮）。仅已确认及之后状态可用；
    质量不合格抛 ValueError（409），不写入。"""
    payload = get_mapping_task(conn, artifact_id)
    if payload is None:
        raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
    doc = payload["mapping_task"]
    candidates = doc.get("candidates") or []
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        raise LookupError(f"候选不存在：index={index}")
    candidate = dict(candidates[index])
    if candidate.get("status") not in ("confirmed", "contacted", "replied", "intaken"):
        raise ValueError(f"仅已确认及之后状态的人选可生成开场白要点，当前状态：{candidate.get('status')}")
    team = _candidate_team(doc, candidate)
    icebreaker = build_icebreaker(candidate, team, client=str(doc.get("client") or ""))
    candidate["icebreaker"] = icebreaker
    candidates[index] = candidate
    save_mapping_task_in_place(conn, artifact_id, doc)
    return {
        "ok": True,
        "artifact_id": str(artifact_id),
        "index": index,
        "icebreaker": icebreaker,
        "candidate": candidate,
    }


# ---- 入库动作（复用 MULTICHANNEL intake 写入口径；不写第二条 job_candidates）----

def _table_columns(conn: Any, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001 表不存在按空列集处理（跳过该面写入）
        return set()


def _table_exists(conn: Any, table: str) -> bool:
    return bool(_table_columns(conn, table))


def _next_id(conn: Any, table: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()
    return int(row[0])


def _insert_dynamic(conn: Any, table: str, values: dict[str, Any]) -> Any:
    """按表实际列动态插入（与 MULTICHANNEL apply_intake 同口径）。"""
    allowed = _table_columns(conn, table)
    payload = {key: value for key, value in values.items() if key in allowed}
    if not payload:
        raise ValueError(f"表 {table} 没有可写字段")
    columns = list(payload)
    placeholders = ",".join("?" for _ in columns)
    return conn.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        [payload[column] for column in columns],
    )


def _normalize_text(value: Any) -> str:
    """与 MULTICHANNEL _normalize_text 同口径（小写/全角括号归一/去空白）。"""
    text = str(value or "").strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", text)


def _identity_key(name: Any, company: Any, title: Any) -> str:
    values = [_normalize_text(name), _normalize_text(company), _normalize_text(title)]
    if not all(values):
        return ""
    return "|".join(values)


# 遮罩名合并口径（§6.4，与 liepin_workbench_server 既有函数同规则）：
# 遮罩名（含*/某/先生/女士/老师）与全名互证须同姓；合并还须公司+职位双匹配。

def _normalize_person_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text.replace("老师", "").strip()


def _is_masked_name(value: Any) -> bool:
    text = _normalize_person_name(value)
    return bool(text) and (
        "*" in text or "某" in text or text.endswith(("先生", "女士", "老师"))
    )


def _names_can_correspond(left: Any, right: Any) -> bool:
    left_text = _normalize_person_name(left).replace("先生", "").replace("女士", "").strip()
    right_text = _normalize_person_name(right).replace("先生", "").replace("女士", "").strip()
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if (_is_masked_name(left) or _is_masked_name(right)) and left_text[:1] and left_text[:1] == right_text[:1]:
        return True
    return False


def _identity_text_matches(left: Any, right: Any) -> bool:
    left_text = " ".join(str(left or "").split())
    right_text = " ".join(str(right or "").split())
    return bool(left_text and right_text and (left_text in right_text or right_text in left_text))


def _person_fingerprint(name: Any, company: Any, title: Any) -> str:
    """与 MULTICHANNEL apply_intake 同口径：name|company|normalize(title)。"""
    return f"{name}|{company}|{_normalize_text(title)}"


def _relation_is_stopped(clean_stage: Any, raw_status: Any) -> bool:
    """与 MULTICHANNEL load_exclusion_set 的 stopped 判定同口径（H5/拒绝态不重复入库）。"""
    stage = str(clean_stage or "")
    status = str(raw_status or "").strip().lower()
    return stage.startswith("H5") or status in {"screen_rejected", "rejected", "client_rejected", "stopped"}


def _find_person(conn: Any, *, name: str, company: str, title: str) -> int | None:
    """people 去重：fingerprint 精确 → §6.4 遮罩互证（同姓遮罩名 + 公司 + 职位双匹配）。"""
    if not _table_exists(conn, "people"):
        return None
    columns = _table_columns(conn, "people")
    if "fingerprint" in columns:
        row = conn.execute(
            "SELECT id FROM people WHERE fingerprint=? ORDER BY id LIMIT 1",
            (_person_fingerprint(name, company, title),),
        ).fetchone()
        if row is not None:
            return int(row[0])
    rows = conn.execute(
        """
        SELECT id,display_name,current_company,current_title FROM people
        WHERE (instr(COALESCE(current_company,''), ?) > 0 AND ? <> '')
           OR (instr(?, COALESCE(current_company,'')) > 0 AND COALESCE(current_company,'') <> '')
        """,
        (company, company, company),
    ).fetchall()
    for row in rows:
        # §6.4：遮罩名互证必须公司+职位双匹配（不放松）
        if (
            _names_can_correspond(name, row[1])
            and _identity_text_matches(company, row[2])
            and _identity_text_matches(title, row[3])
        ):
            return int(row[0])
    return None


def _find_candidate_row(
    conn: Any, *, client: str, position: str, name: str, company: str, title: str
) -> int | None:
    """candidates 去重（client+position 作用域，与 MULTICHANNEL _existing_candidate_id 同口径，
    叠加 §6.4 遮罩互证：遮罩名须公司+职位双匹配才可复用已有行）。"""
    if not _table_exists(conn, "candidates"):
        return None
    rows = conn.execute(
        "SELECT id,name,company,title FROM candidates WHERE client=? AND position=?",
        (client, position),
    ).fetchall()
    target_identity = _identity_key(name, company, title)
    for row in rows:
        if target_identity and target_identity == _identity_key(row[1], row[2], row[3]):
            return int(row[0])
    for row in rows:
        if (
            _names_can_correspond(name, row[1])
            and _identity_text_matches(company, row[2])
            and _identity_text_matches(title, row[3])
        ):
            return int(row[0])
    return None


def intake_candidate(
    conn: Any,
    artifact_id: str,
    index: int,
    *,
    banned: list[str] | tuple[str, ...] | None = None,
    kb_dir: str | None = None,
) -> dict[str, Any]:
    """Mapping 候选入库：仅 confirmed 可用；复用现有 intake 写入口径（同一事务写
    candidates/candidate_clients/candidate_profiles/candidate_intelligence/people/
    source_profiles/entity_source_links/job_candidates/candidate_events）。

    红线：
    - 不写第二条 job_candidates（job_id+person_id+raw_position 已存在 → 复用原 id）；
    - 禁挖名单入库前再校验（restricted 白名单出库，费率/话术红线不出库）；
    - 无公开来源 URL 的人名不得入库（防编造硬约束，与写入口径一致）；
    - 已停止推进（H5/拒绝态）的关系不重复入库；
    - 遮罩名原样存储，合并按 §6.4 既有规则（不放松）。
    """
    payload = get_mapping_task(conn, artifact_id)
    if payload is None:
        raise LookupError(f"Mapping 任务卡不存在：{artifact_id}")
    doc = payload["mapping_task"]
    candidates = doc.get("candidates") or []
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        raise LookupError(f"候选不存在：index={index}")
    candidate = dict(candidates[index])
    status = str(candidate.get("status") or "")
    if status == "intaken":
        receipt = candidate.get("intake") if isinstance(candidate.get("intake"), dict) else None
        if receipt and receipt.get("job_candidate_id"):
            return {
                "ok": True,
                "artifact_id": str(artifact_id),
                "index": index,
                "status": "intaken",
                "already_intaken": True,
                "relation_existed": True,
                "job_candidate_id": int(receipt["job_candidate_id"]),
                "candidate_id": int(receipt.get("candidate_id") or 0),
                "person_id": int(receipt.get("person_id") or 0),
                "intaken_at": str(receipt.get("intaken_at") or ""),
            }
        raise ValueError("该人选已标记入库（缺入库回执），不重复写入")
    if status != "confirmed":
        raise ValueError(f"仅已确认（confirmed）人选可入库，当前状态：{status}")
    source_urls = [str(url).strip() for url in candidate.get("source_urls") or [] if str(url or "").strip()]
    if not source_urls:
        raise ValueError("无公开来源 URL 的人名不得入库（防编造硬约束）")

    team = _candidate_team(doc, candidate)
    company = str(team.get("company") or "").strip()
    client = str(doc.get("client") or "").strip()
    job_title = str(doc.get("job_title") or "").strip()
    job_id = int(doc.get("job_id") or 0)
    name = str(candidate.get("name") or "").strip()
    title = str(candidate.get("current_role") or "").strip()
    location = str(team.get("location") or "").strip()

    verdict = intake_mismatch_verdict(job_title, title)
    if verdict:
        stage = verdict["stage"]
        flow_bucket = verdict["flow_bucket"]
        clean_reason = verdict["reason"]
        stop_reason = verdict["stop_reason"]
        raw_status = "screen_rejected"
        event_type = "resume_review_completed"
        event_status = "stop"
        event_summary = f"{verdict['reason']}：{name}｜{company}｜{title}（任务卡 {artifact_id}）"
        candidate_status = "screen_rejected"
        candidate_notes = f"{stage}｜{verdict['reason']}｜mapping={artifact_id}"
        intelligence_next = verdict["reason"]
        intelligence_decision = "screen_rejected"
    else:
        stage = "S1 新增寻访/待复核"
        flow_bucket = "待复核"
        clean_reason = "Mapping 直挖入库，待完整简历复核"
        stop_reason = None
        raw_status = "mapping_intake"
        event_type = "mapping_intake"
        event_status = "pending_review"
        event_summary = f"Mapping 直挖入库：{name}｜{company}｜{title}（任务卡 {artifact_id}）"
        candidate_status = "new"
        candidate_notes = f"S1 新增寻访/待复核｜mapping={artifact_id}"
        intelligence_next = "沿来源链接打开公开资料，按岗位硬门槛人工复核"
        intelligence_decision = "pending_review"

    # 禁挖名单入库前再校验一次（restricted 只白名单出库）
    if banned is None:
        restricted, _restricted_trace = knowledge_base.load_restricted_constraints(client, kb_dir=kb_dir)
        constraints = (restricted or {}).get("constraints") if isinstance(restricted, dict) else {}
        banned = [str(item) for item in (constraints or {}).get("banned_companies") or [] if str(item or "").strip()]
    if _is_banned(company, banned):
        raise ValueError(f"{company} 在禁挖名单内，该人选不得入库")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = now[:10]

    # people（fingerprint 精确 → §6.4 遮罩互证）
    person_id = _find_person(conn, name=name, company=company, title=title)
    person_existed = person_id is not None
    if person_id is None:
        cursor = _insert_dynamic(
            conn,
            "people",
            {
                "display_name": name,
                "current_company": company,
                "current_title": title,
                "city": location,
                "education": "",
                "experience": "",
                "fingerprint": _person_fingerprint(name, company, title),
                "created_at": now,
            },
        )
        person_id = int(cursor.lastrowid)

    # candidates（client+position 作用域去重）
    candidate_id = _find_candidate_row(
        conn, client=client, position=job_title, name=name, company=company, title=title
    )
    candidate_existed = candidate_id is not None
    if candidate_id is None:
        iteration_row = conn.execute(
            "SELECT COALESCE(MAX(iteration),0)+1 FROM candidates WHERE client=? AND position=?",
            (client, job_title),
        ).fetchone()
        candidate_id = _next_id(conn, "candidates")
        _insert_dynamic(
            conn,
            "candidates",
            {
                "id": candidate_id,
                "name": name,
                "company": company,
                "title": title,
                "education": "",
                "experience": "",
                "skills": str(candidate.get("reason") or "")[:1200],
                "level": "未分层",
                "city": location,
                "client": client,
                "position": job_title,
                "search_date": today,
                "status": candidate_status,
                "notes": candidate_notes,
                "iteration": int(iteration_row[0]) if iteration_row else 1,
                "created_at": now,
                "updated_at": now,
                "source": "mapping",
                "xsaas_id": "",
            },
        )

    # 辅助面（与 apply_intake 同一事务口径；表存在才写）
    if _table_exists(conn, "candidate_clients"):
        _insert_dynamic(
            conn,
            "candidate_clients",
            {
                "id": _next_id(conn, "candidate_clients"),
                "candidate_name": name,
                "candidate_company": company,
                "client": client,
                "source": "mapping",
                "position_tag": job_title,
                "created_at": now,
            },
        )
    if _table_exists(conn, "candidate_profiles"):
        _insert_dynamic(
            conn,
            "candidate_profiles",
            {
                "id": _next_id(conn, "candidate_profiles"),
                "candidate_id": candidate_id,
                "candidate_name": name,
                "candidate_company": company,
                "client": client,
                "position": job_title,
                "industry_tags_json": "[]",
                "function_tags_json": "[]",
                "risk_tags_json": "[]",
                "profile_summary": str(candidate.get("reason") or "")[:1200],
                "updated_at": now,
            },
        )
    if _table_exists(conn, "candidate_intelligence"):
        _insert_dynamic(
            conn,
            "candidate_intelligence",
            {
                "id": _next_id(conn, "candidate_intelligence"),
                "candidate_id": candidate_id,
                "candidate_name": name,
                "candidate_company": company,
                "client": client,
                "position": job_title,
                "fit_score": 0,
                "fit_level": "unrated",
                "evidence_json": "{}",
                "risk_json": "{}",
                "next_action": intelligence_next,
                "last_evaluated_at": now,
                "model_version": "mapping-task-s5",
                "created_at": now,
                "updated_at": now,
                "strong_matches_json": "[]",
                "weak_matches_json": "[]",
                "verification_questions_json": "[]",
                "recommendation_decision": intelligence_decision,
            },
        )
    if _table_exists(conn, "source_profiles"):
        existing_profile = conn.execute(
            """
            SELECT id FROM source_profiles
            WHERE person_id=? AND lower(COALESCE(source_type,''))='mapping'
            ORDER BY id LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        if existing_profile is None:
            _insert_dynamic(
                conn,
                "source_profiles",
                {
                    "person_id": person_id,
                    "source_type": "mapping",
                    "source_candidate_id": None,
                    "source_date": today,
                    "raw_status": "mapping_intake",
                    "raw_client": client,
                    "raw_position": job_title,
                    "raw_json": _dumps(
                        {
                            "name": name,
                            "company": company,
                            "title": title,
                            "source_url": source_urls[0],
                            "source_urls": source_urls,
                            "reason": str(candidate.get("reason") or ""),
                            "mapping_artifact": str(artifact_id),
                        }
                    ),
                },
            )
    if _table_exists(conn, "entity_source_links"):
        for url in source_urls:
            conn.execute(
                """
                INSERT INTO entity_source_links
                    (canonical_type,canonical_id,source_system,source_entity_type,
                     source_entity_id,source_url,metadata_json,updated_at)
                VALUES ('person',?,?,?,?,?,?,?)
                ON CONFLICT(source_system,source_entity_type,source_entity_id,
                            canonical_type,canonical_id)
                DO UPDATE SET source_url=excluded.source_url,
                              metadata_json=excluded.metadata_json,
                              updated_at=excluded.updated_at
                """,
                (
                    str(person_id),
                    "mapping",
                    "external_profile",
                    url,
                    url,
                    _dumps({"backfilled_from": "mapping_task_intake", "mapping_artifact": str(artifact_id)}),
                    now,
                ),
            )

    # job_candidates：同人选同岗位已存在关系 → 复用原 id，绝不写第二条
    relation = conn.execute(
        """
        SELECT id,clean_stage,raw_status FROM job_candidates
        WHERE job_id=? AND person_id=? AND raw_position=?
        ORDER BY id LIMIT 1
        """,
        (job_id, person_id, job_title),
    ).fetchone()
    relation_existed = relation is not None
    if relation_existed:
        if _relation_is_stopped(relation[1], relation[2]):
            raise ValueError("该人选在此岗位的关系已停止推进，不重复入库（如需重启请走人工状态纠正）")
        job_candidate_id = int(relation[0])
    else:
        cursor = _insert_dynamic(
            conn,
            "job_candidates",
            {
                "job_id": job_id,
                "person_id": person_id,
                "raw_client": client,
                "raw_position": job_title,
                "raw_status": raw_status,
                "raw_stage": stage,
                "clean_stage": stage,
                "flow_bucket": flow_bucket,
                "clean_reason": clean_reason,
                "stop_reason": stop_reason,
                "recent_hunting": 1,
                "search_date": today,
                "updated_at": now,
                "source_candidate_id": str(candidate_id),
            },
        )
        job_candidate_id = int(cursor.lastrowid)

    # 业务时间线（与 intake 同口径：新增事件 pending_review，source_table 标 mapping_task）
    _insert_dynamic(
        conn,
        "candidate_events",
        {
            "job_candidate_id": job_candidate_id,
            "person_id": person_id,
            "job_id": job_id,
            "event_type": event_type,
            "event_status": event_status,
            "event_time": now,
            "summary": event_summary,
            "raw_json": _dumps(
                {
                    "mapping_artifact": str(artifact_id),
                    "candidate_index": index,
                    "source_urls": source_urls,
                    "reason": str(candidate.get("reason") or ""),
                    "relation_existed": relation_existed,
                    "actor": "user",
                }
            ),
            "source_table": "mapping_task",
            "source_id": str(artifact_id),
        },
    )

    # 任务卡回写：status→intaken + 入库回执（整批校验后原位落库，version 不 bump）
    candidate["status"] = "intaken"
    candidate["intake"] = {
        "job_candidate_id": job_candidate_id,
        "candidate_id": candidate_id,
        "person_id": person_id,
        "intaken_at": now,
        "relation_existed": relation_existed,
    }
    candidates[index] = candidate
    _sync_stats(doc)
    save_mapping_task_in_place(conn, artifact_id, doc)

    return {
        "ok": True,
        "artifact_id": str(artifact_id),
        "index": index,
        "status": "intaken",
        "already_intaken": False,
        "relation_existed": relation_existed,
        "job_candidate_id": job_candidate_id,
        "candidate_id": candidate_id,
        "person_id": person_id,
        "candidate_existed": candidate_existed,
        "person_existed": person_existed,
        "intaken_at": now,
        "stats": doc["stats"],
    }
