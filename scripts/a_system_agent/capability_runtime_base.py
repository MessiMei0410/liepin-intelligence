from __future__ import annotations

import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from . import query_builders

if TYPE_CHECKING:
    from .service import AgentService

MULTICHANNEL = Path("/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py")
LIEPIN_SEARCH = Path(__file__).resolve().parents[1] / "run_published_position_search.py"
RESUME_BACKFILL = Path(__file__).resolve().parents[1] / "backfill_pooled_resume_details.py"
XSAAS_SEARCH = Path(__file__).resolve().parents[1] / "xsaas_candidate_search.py"
OPENCLI_SHADOW = Path(__file__).resolve().parents[1] / "opencli_sourcing_shadow.py"
LIEPIN_OUTREACH = Path("/Users/messi/.codex/skills/liepin-cdp-search/scripts/liepin_im_followup.py")
LIEPIN_PUBLISH = Path("/Users/messi/.codex/skills/liepin-job-publish/scripts/liepin_publish_job.py")
MATCHING_REPORT = Path("/Users/messi/.codex/skills/candidate-matching-report/scripts/report_template.py")
JIASHI_REPORT = Path("/Users/messi/.codex/skills/jiashi-recommendation-report/scripts/fill_docx_template.py")
JIASHI_AUDIT = Path("/Users/messi/.codex/skills/jiashi-recommendation-report/scripts/audit_generated_report.py")
SALARY_REPORT = Path("/Users/messi/.codex/skills/candidate-salary-report/scripts/build_salary_report.py")
JIASHI_TEMPLATE = Path("/Users/messi/Desktop/嘉驰推荐报告/2026-06散落归档/嘉驰国际+客户名称--岗位名称--人选姓名（嘉驰模板）.docx")

DEFAULT_SOURCING_CELL_BATCH_SIZE = 8
MAX_SOURCING_CELL_BATCH_SIZE = 20
DEFAULT_PAGINATION_CONTINUATION_HEADROOM = 20
MAX_SOURCING_CONTINUATION_BATCHES = 256

# 注册在服务层（workflow_handler._execute_workflow_capability）实现、没有 run_* 确定性 Runner 的能力。
# 必须与 workflow_handler 的 locally_specialized 集合保持一致；注册期不变量
# assert_workflow_capabilities_resolvable 会在 AgentService 启动时校验漂移。
SERVICE_HANDLED_CAPABILITY_IDS: frozenset[str] = frozenset({
    "talent_pool_search",
    "candidate_batch_assessment",
    "candidate_pool_filter",
    "outreach_queue",
    "pool_gap_advice",
    "reply_triage",
    "communication_draft_batch",
})

# execute_external（后台渠道执行）当前支持的能力；只读/内部能力一律不允许后台渠道执行。
EXTERNAL_EXECUTION_CAPABILITY_IDS: frozenset[str] = frozenset({"multi_channel_sourcing"})


def assert_workflow_capabilities_resolvable(
    workflow_capabilities: list[Any],
    deterministic_runner_ids: set[str] | frozenset[str],
    service_handled_ids: set[str] | frozenset[str] = SERVICE_HANDLED_CAPABILITY_IDS,
) -> None:
    """注册期不变量：每个注册的工作流能力必须落到确定性 Runner（run_*）或服务层处理器。"""
    missing = sorted({
        str(item[0])
        for item in workflow_capabilities
        if str(item[0]) not in deterministic_runner_ids and str(item[0]) not in service_handled_ids
    })
    if missing:
        raise RuntimeError(
            "已注册工作流能力缺少执行实现（既无 run_* 确定性 Runner，也不在服务层处理器集合）："
            + "、".join(missing)
        )


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row else {}


ZERO_RESULT_ATTRIBUTIONS = (
    "no_results",
    "session_expired",
    "compliance_wall",
    "parse_failure",
    "page_structure_changed",
    "loading_incomplete",
    "query_build_error",
    "pool_saturated",
    "unknown",
)

# 0 召回归因的中文解释（与 classify_zero_result 的枚举一一对应，单一来源）。
# 前端映射在仓外，含义必须与这里保持一致。
ZERO_RESULT_ATTRIBUTION_LABELS = {
    "session_expired": "登录态失效，需重新登录该渠道",
    "compliance_wall": "命中平台合规墙（需在浏览器里确认承诺函后重试）",
    "loading_incomplete": "页面加载未完成或查询未生效",
    "page_structure_changed": "页面结构变化，解析器需要适配",
    "parse_failure": "平台有结果但解析抓取失败",
    "no_results": "该渠道真实无匹配结果",
    "query_build_error": "查询构造异常",
    "pool_saturated": "本地人才库基本找遍了（重复率太高）",
    "unknown": "原因待排查",
}


def _round_int(entry: Any, key: str) -> int:
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _trim_error(text: Any, limit: int = 1000) -> str:
    """错误文本截断：超长时保留尾部。

    traceback 的诊断行（异常类型 + 消息）在末尾，头部截断会把 ``X-SAAS_LOGIN_REQUIRED``
    这类归因信号切掉，使 classify_zero_result 系统性落到 unknown（T3 实战发现）。
    """
    value = str(text or "").strip()
    return value if len(value) <= limit else value[-limit:]


def _revision_consultant_evidence(context: dict[str, Any]) -> str:
    """Return only the consultant evidence embedded in a workflow revision instruction."""
    instruction = " ".join(str(context.get("revision_instruction") or "").split())
    if not instruction:
        return ""
    marker = "顾问已确认的原始条件："
    evidence = instruction.split(marker, 1)[1] if marker in instruction else instruction
    for suffix in ("。生成前必须逐项读取", "生成前必须逐项读取"):
        if suffix in evidence:
            evidence = evidence.split(suffix, 1)[0]
    return evidence.strip(" 。；;")


def _consultant_constraint_items(evidence: str) -> list[dict[str, str]]:
    """Extract modal constraints without asking the model to reinterpret their strength."""
    text = " ".join(str(evidence or "").split())
    if not text:
        return []
    candidates: list[tuple[str, str]] = []
    candidates.extend(("hard_requirement", value) for value in re.findall(r"必须[^，,；;。]+", text))
    for clause in re.split(r"[；;。]+", text):
        cleaned = clause.strip(" ，,")
        if not cleaned:
            continue
        if "优先" in cleaned or "更好" in cleaned:
            candidates.append(("preference", cleaned))
        if "可看" in cleaned or "可以看" in cleaned:
            candidates.append(("conditional_acceptance", cleaned))
        if any(token in cleaned for token in ("纠正", "术语", "不是", "不等于")):
            candidates.append(("consultant_wording", cleaned))
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for constraint_type, rule in candidates:
        normalized = rule.strip(" ：:，,；;。")
        key = (constraint_type, normalized)
        if not normalized or key in seen:
            continue
        seen.add(key)
        rows.append({"type": constraint_type, "rule": normalized, "source": "consultant_revision"})
    return rows


def _lock_consultant_constraints(
    plan: dict[str, Any], strategy: dict[str, Any], evidence: str,
    locked_items: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Make revision semantics auditable and prevent model-generated constraint weakening."""
    constraints = _consultant_constraint_items(evidence)
    kind_map = {
        "must": "hard_requirement", "prefer": "preference",
        "allow": "conditional_acceptance", "exclude": "exclusion",
        "target_count": "target_count", "other": "consultant_wording",
    }
    seen = {(item["type"], item["rule"]) for item in constraints}
    for item in locked_items or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("quote") or item.get("rule") or "").strip(" ：:，,；;。")
        constraint_type = kind_map.get(str(item.get("kind") or "other"), "consultant_wording")
        key = (constraint_type, rule)
        if not rule or key in seen:
            continue
        seen.add(key)
        constraints.append({"type": constraint_type, "rule": rule, "source": "copilot_verbatim"})
    if not constraints:
        return []
    strategy["consultant_constraints"] = constraints
    plan["consultant_constraints"] = constraints

    rules = [item["rule"] for item in constraints]
    essence = strategy.get("step1_job_essence")
    if isinstance(essence, dict):
        statement = str(essence.get("statement") or "").strip()
        locked = "顾问确认：" + "；".join(rules) + "。"
        essence["statement"] = f"{locked}{statement}" if locked not in statement else statement
        essence["confirmed_by"] = "consultant"

    hard_rules = [item["rule"] for item in constraints if item["type"] == "hard_requirement"]
    expectation = strategy.get("step5_expectation")
    if hard_rules and isinstance(expectation, dict):
        expectation["fallback_plan"] = (
            f"不得放宽顾问硬约束：{'；'.join(hard_rules)}；"
            "召回不足时只调整同义词、渠道或目标公司池，不降低门槛。"
        )

    trace = strategy.get("classification_trace")
    if isinstance(trace, list):
        trace.append(f"顾问修订约束已锁定 {len(constraints)} 项，模型不得弱化")
    summary = str(plan.get("strategy_summary") or "").strip()
    locked_summary = "顾问约束：" + "；".join(rules)
    plan["strategy_summary"] = f"{locked_summary}。{summary}" if locked_summary not in summary else summary
    return constraints


def _locked_constraint_conflicts(
    plan: dict[str, Any], strategy: dict[str, Any], constraints: list[dict[str, str]],
) -> list[str]:
    """Reject model output that changes the meaning of a locked consultant phrase."""
    if not constraints:
        return []
    serialized = json.dumps({"plan": plan, "strategy_v2": strategy}, ensure_ascii=False)
    errors: list[str] = []
    for item in constraints:
        rule = str(item.get("rule") or "").strip()
        if rule and rule not in serialized:
            errors.append(f"顾问原话约束未逐字保留：{rule}")
        if "三次电源" in rule and re.search(r"(?:三|3)次(?:以上|及以上|完整)", serialized):
            errors.append("领域术语“三次电源”被误解为次数条件")
    return list(dict.fromkeys(errors))


class CommandExecutionError(RuntimeError):
    def __init__(self, message: str, detail: dict[str, Any]):
        super().__init__(message)
        self.detail = detail


class ExternalPhaseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        phase: str,
        partial_result: dict[str, Any],
        detail: dict[str, Any],
    ):
        super().__init__(message)
        self.phase = phase
        self.partial_result = partial_result
        self.detail = detail


class ExternalExecutionCancelled(RuntimeError):
    """Raised after the workflow is stopped while a channel subprocess is active."""


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _command_failure_summary(stdout: str, stderr: str, returncode: int) -> tuple[str, dict[str, Any]]:
    outer = _json_object(stdout) or _json_object(stderr)
    nested = _json_object(outer.get("stdout")) if outer else {}
    payload = nested or outer
    failed_checks = [
        check for check in payload.get("checks", [])
        if isinstance(check, dict) and check.get("ok") is False
    ] if isinstance(payload.get("checks"), list) else []
    labels = [
        str(check.get("message") or check.get("check") or "审计检查未通过").strip()
        for check in failed_checks[:3]
    ]
    if labels:
        summary = "；".join(dict.fromkeys(label for label in labels if label))
    else:
        raw = stderr or stdout or f"命令退出码 {returncode}"
        summary = _trim_error(raw, 600)
    detail = {
        "returncode": returncode,
        "command": outer.get("cmd") if outer else None,
        "failed_checks": [
            {
                "check": check.get("check"),
                "message": check.get("message"),
                "client": check.get("client"),
                "rows": check.get("rows", [])[:5] if isinstance(check.get("rows"), list) else [],
            }
            for check in failed_checks
        ],
        "stdout_tail": _trim_error(stdout, 2000),
        "stderr_tail": _trim_error(stderr, 2000),
    }
    return summary, detail


# 渠道查询方言层已模块化（S4-3c-2 / N1）：实现在 a_system_agent.query_builders。
# 以下常量与 adapt_channel_queries 是兼容别名/委托（既有测试与调用方签名不变），
# 新接线直接用 query_builders.build_liepin_queries / build_xsaas_queries。
XSAAS_QUERY_MAX_TERMS = query_builders.XSAAS_QUERY_MAX_TERMS
XSAAS_QUERY_MAX_COUNT = query_builders.XSAAS_QUERY_MAX_COUNT
LIEPIN_QUERY_MAX_TERMS = query_builders.LIEPIN_QUERY_MAX_TERMS
LIEPIN_QUERY_MAX_COUNT = query_builders.LIEPIN_QUERY_MAX_COUNT


def adapt_channel_queries(queries: list[Any], *, max_terms: int, max_count: int, company_terms: set[str] | None = None) -> list[str]:
    """兼容委托：实现已迁入 query_builders.adapt_queries（组合式方言，猎聘语法）。"""
    return query_builders.adapt_queries(queries, max_terms=max_terms, max_count=max_count, company_terms=company_terms)


# pool_saturated 阈值：dedupe_count/extracted_count 超过该比例视为本地池枯竭
# （F3 实证：猎聘 119/120=99% 排重仍原样重搜，系统无信号）。
POOL_SATURATED_DEDUPE_RATE = 0.9


def _has_query_build_error(rounds: list[dict[str, Any]], vocab: set[str]) -> bool:
    """查询构造错误信号（HTTP 成功但查询本身错了；任一 query 命中即成立）。

    a) "关键字："嵌套拼接（同一查询出现 ≥2 次，round7 真实形态：
       ``MPS 矽力杰 关键字：MPS 杰华特 关键字：MPS TME``），或查询间条件累加
       （后一条页面回显查询完整包含前一条且更长——条件未重置时 X-SaaS 的 selected_query 形态）；
    b) 单查询含 ≥2 个公司名（一人不可能同时在两家公司，组合语义必错；词表为空时该信号不启用）；
    c) 空查询，或 repr 残片（round6 真实形态：字典被 str() 当查询词，``{'evidence': ...`` 开头）。
    """
    selected_queries: list[str] = []
    for entry in rounds:
        query = str(entry.get("query") or "").strip()
        if "query" in entry and not query:
            return True
        selected = str(entry.get("selected_query") or "").strip()
        if selected:
            selected_queries.append(selected)
        for text in (query, selected):
            if not text:
                continue
            if text.count("关键字：") >= 2 or text.startswith("{'"):
                return True
            if vocab and sum(1 for token in text.split() if query_builders.is_company_token(token, vocab)) >= 2:
                return True
    return any(
        current != previous and current.startswith(previous)
        for previous, current in zip(selected_queries, selected_queries[1:])
    )


def classify_zero_result(
    channel: str,
    status: str,
    result: dict[str, Any],
    *,
    dedupe_count: int | None = None,
    company_vocab: set[str] | None = None,
) -> str:
    """用 runner 输出里的既有信号为 0 候选的渠道结果归因。

    信号来源（不改浏览器 runner 逻辑）：
    - 失败/阻断时 error 文本：X-SaaS 的 ``X-SAAS_LOGIN_REQUIRED``、猎聘的“登录已过期/登录态失效”
      → session_expired；“加载超时/未加载” → loading_incomplete。
    - per-query rounds：search_controls_missing（X-SaaS SEARCH_JS 返回的 reason）→ page_structure_changed；
      stale_query（查询未生效）→ loading_incomplete；平台有结果数但抓取 0 条 → parse_failure；
      全部查询平台返回 0 条，或抓到了但全部被评分门槛/排重过滤 → no_results。
    - query_build_error（S4-3c-1）：查询构造错误但 HTTP 成功——"关键字："嵌套拼接/查询间条件累加、
      单查询 ≥2 个公司名（company_vocab 判定）、空查询/repr 残片；任一 query 命中即归类该渠道。
    - pool_saturated（S4-3c-1）：召回正常（recall_count > 0）但排重率
      dedupe_count/extracted_count > 90%（调用方传入漏斗口径 dedupe_count，extracted 为 0 不计）。
    判定顺序：执行类（session/loading/结构）> query_build_error > pool_saturated
    > rounds 常规判定（parse_failure/no_results）> unknown；信号不足一律 unknown。
    """
    error = str(result.get("error") or "")
    if "LOGIN_REQUIRED" in error or "登录已过期" in error or "登录态失效" in error:
        return "session_expired"
    if "合规墙" in error or "合规承诺" in error or "compliancecommitment" in error.lower():
        return "compliance_wall"
    if "加载超时" in error or "未加载" in error:
        return "loading_incomplete"
    rounds = [entry for entry in result.get("rounds") or [] if isinstance(entry, dict)]
    if rounds:
        if any(str(entry.get("reason") or "") == "search_controls_missing" for entry in rounds):
            return "page_structure_changed"
        if any(str(entry.get("status") or "") == "stale_query" for entry in rounds):
            return "loading_incomplete"
        if any(str(entry.get("reason") or "") == "settle_timeout" for entry in rounds):
            return "loading_incomplete"
        if _has_query_build_error(rounds, company_vocab or set()):
            return "query_build_error"
        recall = sum(_round_int(entry, "result_count") for entry in rounds)
        extracted = sum(_round_int(entry, "extracted_count") for entry in rounds)
        if dedupe_count is not None and recall > 0 and extracted > 0 and dedupe_count / extracted > POOL_SATURATED_DEDUPE_RATE:
            return "pool_saturated"
        if recall > 0 and extracted <= 0:
            return "parse_failure"
        return "no_results"
    if status == "completed" and result.get("ok") is True and not error:
        # 查询成功但没有 per-query 明细（旧版 runner 输出），无法进一步区分。
        return "unknown"
    return "unknown"


def _slug(value: Any) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", str(value or "").strip())
    return text.strip("-.")[:80] or "artifact"


def _list_text(value: Any) -> str:
    items = _loads(value, [])
    if not isinstance(items, list):
        return str(value or "")
    return "；".join(str(item).strip() for item in items if str(item).strip())


__all__ = [
    'MULTICHANNEL',
    'LIEPIN_SEARCH',
    'RESUME_BACKFILL',
    'XSAAS_SEARCH',
    'OPENCLI_SHADOW',
    'LIEPIN_OUTREACH',
    'LIEPIN_PUBLISH',
    'MATCHING_REPORT',
    'JIASHI_REPORT',
    'JIASHI_AUDIT',
    'SALARY_REPORT',
    'JIASHI_TEMPLATE',
    'DEFAULT_SOURCING_CELL_BATCH_SIZE',
    'MAX_SOURCING_CELL_BATCH_SIZE',
    'DEFAULT_PAGINATION_CONTINUATION_HEADROOM',
    'MAX_SOURCING_CONTINUATION_BATCHES',
    'SERVICE_HANDLED_CAPABILITY_IDS',
    'EXTERNAL_EXECUTION_CAPABILITY_IDS',
    'assert_workflow_capabilities_resolvable',
    '_loads',
    '_row',
    'ZERO_RESULT_ATTRIBUTIONS',
    'ZERO_RESULT_ATTRIBUTION_LABELS',
    '_round_int',
    '_trim_error',
    '_revision_consultant_evidence',
    '_consultant_constraint_items',
    '_lock_consultant_constraints',
    '_locked_constraint_conflicts',
    'CommandExecutionError',
    'ExternalPhaseError',
    'ExternalExecutionCancelled',
    '_json_object',
    '_command_failure_summary',
    'XSAAS_QUERY_MAX_TERMS',
    'XSAAS_QUERY_MAX_COUNT',
    'LIEPIN_QUERY_MAX_TERMS',
    'LIEPIN_QUERY_MAX_COUNT',
    'adapt_channel_queries',
    'POOL_SATURATED_DEDUPE_RATE',
    '_has_query_build_error',
    'classify_zero_result',
    '_slug',
    '_list_text',
    'RunnerBaseMixin',
]


class RunnerBaseMixin:
    """生命周期、runner 执行基元与渠道执行参数（不含 OpenCLI 簇）。"""

    def __init__(self, service: AgentService) -> None:
        self.service = service
        self.python = self._resolve_python()
        self.output_dir = service.db_path.parent / "asa_artifacts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def deterministic_runner_ids(cls) -> frozenset[str]:
        """当前 run_* 确定性 Runner 覆盖的能力集合。"""
        return frozenset(
            name[len("run_"):]
            for name in dir(cls)
            if name.startswith("run_") and callable(getattr(cls, name, None))
        )

    def availability(self, capability_id: str | None = None) -> dict[str, Any]:
        """能力可用性与调用语义元数据：确定性 Runner / 服务层处理器 / 后台渠道执行支持。"""
        runner_ids = self.deterministic_runner_ids()
        known = sorted(runner_ids | set(SERVICE_HANDLED_CAPABILITY_IDS))
        selected = [str(capability_id)] if capability_id else known
        rows: list[dict[str, Any]] = []
        for cid in selected:
            spec = self.service.skills.get(cid) if hasattr(self.service, "skills") else None
            deterministic = cid in runner_ids
            service_handled = cid in SERVICE_HANDLED_CAPABILITY_IDS
            rows.append({
                "capability_id": cid,
                "registered": spec is not None,
                "deterministic_runner": deterministic,
                "service_handler": service_handled,
                "external_execution_supported": cid in EXTERNAL_EXECUTION_CAPABILITY_IDS,
                "execution_path": (
                    "deterministic_runner" if deterministic
                    else "service_handler" if service_handled
                    else "unsupported"
                ),
            })
        return {"ok": True, "capabilities": rows[0] if capability_id and len(rows) == 1 else rows}

    @staticmethod
    def _resolve_python() -> str:
        candidates = [
            os.environ.get("A_SYSTEM_PYTHON", ""),
            sys.executable,
            shutil.which("python3") or "",
            "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen or not Path(candidate).exists():
                continue
            seen.add(candidate)
            probe = subprocess.run(
                [candidate, "-c", "import openpyxl, docx"],
                capture_output=True, text=True, timeout=15,
            )
            if probe.returncode == 0:
                return candidate
        raise RuntimeError("ASA 找不到具备 openpyxl 和 python-docx 的 Python 运行环境")

    @staticmethod
    def _sourcing_score_thresholds(job: str, ability_terms: set[str]) -> tuple[int, int]:
        text = " ".join([str(job or ""), *(str(term or "") for term in ability_terms)]).casefold()
        markers = ("vpd", "vrm", "tlvr", "多相buck", "模块电源", "垂直供电")
        return (70, 80) if sum(marker.casefold() in text for marker in markers) >= 2 else (55, 65)

    @staticmethod
    def _channel_risk_stop_reason(result: dict[str, Any]) -> str:
        text = json.dumps(result, ensure_ascii=False).casefold()
        markers = (
            "安全风险", "风险提示", "安全合规", "合规墙", "compliancecommitment",
            "captcha", "人机验证", "操作频繁", "访问频繁", "账号风险",
        )
        return next((marker for marker in markers if marker.casefold() in text), "")

    @staticmethod
    def _channel_page_budget(request: dict[str, Any]) -> int:
        try:
            requested = int(request.get("max_pages_per_query") or 3)
        except (TypeError, ValueError):
            requested = 3
        return max(1, min(requested, 10))

    @staticmethod
    def _platform_capped_continuation_limit(request: dict[str, Any]) -> int:
        """Bound pagination retries so capped queries cannot keep re-entering the queue."""
        raw = request.get("max_platform_capped_retries")
        if raw not in (None, ""):
            try:
                return max(0, min(int(raw), 3))
            except (TypeError, ValueError):
                pass
        mode = str(request.get("liepin_risk_mode") or os.environ.get("ASA_LIEPIN_RISK_MODE", "low")).strip().lower()
        return 1 if mode in {"fast", "balanced_fast"} else 0

    @staticmethod
    def _liepin_detail_capture_options(request: dict[str, Any], target: int) -> tuple[int, list[str]]:
        """Low-risk fast mode: keep recall broad, throttle only resume detail pages."""
        mode = str(request.get("liepin_risk_mode") or os.environ.get("ASA_LIEPIN_RISK_MODE", "low")).strip().lower()
        if mode in {"fast", "balanced_fast"}:
            defaults = {"min": 1.8, "max": 4.0, "burst": 8, "cooldown": 8.0}
        elif mode in {"very_low", "safe", "conservative"}:
            defaults = {"min": 4.0, "max": 8.0, "burst": 4, "cooldown": 25.0}
        else:
            defaults = {"min": 2.8, "max": 6.2, "burst": 5, "cooldown": 16.0}

        def _int_value(key: str, fallback: int, minimum: int, maximum: int) -> int:
            raw = request.get(key) or os.environ.get(f"ASA_{key.upper()}")
            try:
                value = int(raw) if raw not in (None, "") else fallback
            except (TypeError, ValueError):
                value = fallback
            return max(minimum, min(value, maximum))

        def _float_value(key: str, fallback: float, minimum: float, maximum: float) -> float:
            raw = request.get(key) or os.environ.get(f"ASA_{key.upper()}")
            try:
                value = float(raw) if raw not in (None, "") else fallback
            except (TypeError, ValueError):
                value = fallback
            return max(minimum, min(value, maximum))

        fallback_limit = min(max(6, target), 12)
        detail_limit = _int_value("liepin_detail_limit", fallback_limit, 0, 40)
        min_delay = _float_value("liepin_detail_min_delay", float(defaults["min"]), 0.0, 30.0)
        max_delay = _float_value("liepin_detail_max_delay", float(defaults["max"]), min_delay, 45.0)
        burst_size = _int_value("liepin_detail_burst_size", int(defaults["burst"]), 1, 50)
        cooldown = _float_value("liepin_detail_burst_cooldown", float(defaults["cooldown"]), 0.0, 120.0)
        return detail_limit, [
            "--detail-min-delay", str(min_delay),
            "--detail-max-delay", str(max_delay),
            "--detail-burst-size", str(burst_size),
            "--detail-burst-cooldown", str(cooldown),
            "--stop-on-risk-page",
        ]

    def _external_request_cancelled(self, request: dict[str, Any]) -> bool:
        try:
            step_id = int(request.get("_workflow_step_id") or 0)
        except (TypeError, ValueError):
            return False
        execution_token = str(request.get("_workflow_execution_token") or "")
        return step_id > 0 and not self.service.workflow_engine.external_step_is_active(step_id, execution_token)

    def _ensure_external_request_active(self, request: dict[str, Any]) -> None:
        if self._external_request_cancelled(request):
            raise ExternalExecutionCancelled("工作流已停止，当前渠道执行已终止")

    @staticmethod
    def _run_command(
        command: list[str],
        timeout: int = 300,
        *,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.25,
    ) -> subprocess.CompletedProcess[str]:
        if cancel_check is None:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            while True:
                if cancel_check():
                    raise ExternalExecutionCancelled("工作流已停止，当前渠道执行已终止")
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = proc.communicate(timeout=min(max(0.01, poll_interval), remaining))
                    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        except (ExternalExecutionCancelled, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.communicate(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
            raise

    def _run(
        self,
        command: list[str],
        timeout: int = 300,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        proc = self._run_command(command, timeout, cancel_check=cancel_check)
        if proc.returncode != 0:
            message, detail = _command_failure_summary(proc.stdout, proc.stderr, proc.returncode)
            detail["command"] = command
            raise CommandExecutionError(message, detail)
        return proc

    def _run_external(
        self,
        command: list[str],
        timeout: int,
        *,
        cancel_check: Callable[[], bool] | None,
    ) -> subprocess.CompletedProcess[str]:
        if cancel_check is None:
            return self._run(command, timeout)
        return self._run(command, timeout, cancel_check=cancel_check)

    def _run_json(
        self,
        command: list[str],
        timeout: int = 300,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        proc = self._run(command, timeout, cancel_check=cancel_check)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("能力脚本未返回合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("能力脚本必须返回 JSON 对象")
        return payload

    def _run_external_json(
        self,
        command: list[str],
        timeout: int,
        *,
        cancel_check: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        if cancel_check is None:
            return self._run_json(command, timeout)
        return self._run_json(command, timeout, cancel_check=cancel_check)
