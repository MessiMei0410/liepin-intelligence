from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .context import build_candidate_context
from .policy import is_stopped
from . import knowledge_base, negative_rules, query_builders, strategy_v2

if TYPE_CHECKING:
    from .service import AgentService


MULTICHANNEL = Path("/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py")
LIEPIN_SEARCH = Path(__file__).resolve().parents[1] / "run_published_position_search.py"
XSAAS_SEARCH = Path(__file__).resolve().parents[1] / "xsaas_candidate_search.py"
OPENCLI_SHADOW = Path(__file__).resolve().parents[1] / "opencli_sourcing_shadow.py"
LIEPIN_OUTREACH = Path("/Users/messi/.codex/skills/liepin-cdp-search/scripts/liepin_im_followup.py")
LIEPIN_PUBLISH = Path("/Users/messi/.codex/skills/liepin-job-publish/scripts/liepin_publish_job.py")
MATCHING_REPORT = Path("/Users/messi/.codex/skills/candidate-matching-report/scripts/report_template.py")
JIASHI_REPORT = Path("/Users/messi/.codex/skills/jiashi-recommendation-report/scripts/fill_docx_template.py")
JIASHI_AUDIT = Path("/Users/messi/.codex/skills/jiashi-recommendation-report/scripts/audit_generated_report.py")
SALARY_REPORT = Path("/Users/messi/.codex/skills/candidate-salary-report/scripts/build_salary_report.py")
JIASHI_TEMPLATE = Path("/Users/messi/Desktop/嘉驰推荐报告/2026-06散落归档/嘉驰国际+客户名称--岗位名称--人选姓名（嘉驰模板）.docx")


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
    "loading_incomplete": "页面加载未完成或查询未生效",
    "page_structure_changed": "页面结构变化，解析器需要适配",
    "parse_failure": "平台有结果但解析抓取失败",
    "no_results": "该渠道真实无匹配结果",
    "query_build_error": "查询构造异常",
    "pool_saturated": "本地池枯竭（排重率过高）",
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
    if "加载超时" in error or "未加载" in error:
        return "loading_incomplete"
    rounds = [entry for entry in result.get("rounds") or [] if isinstance(entry, dict)]
    if rounds:
        if any(str(entry.get("reason") or "") == "search_controls_missing" for entry in rounds):
            return "page_structure_changed"
        if any(str(entry.get("status") or "") == "stale_query" for entry in rounds):
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


class RecruitingCapabilityRuntime:
    """Deterministic implementations behind the ASA capability registry."""

    def __init__(self, service: AgentService) -> None:
        self.service = service
        self.python = self._resolve_python()
        self.output_dir = service.db_path.parent / "asa_artifacts"
        self.output_dir.mkdir(parents=True, exist_ok=True)

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

    def execute(self, capability_id: str, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"run_{capability_id}", None)
        if handler is None:
            raise ValueError(f"能力尚未实现确定性 Runner：{capability_id}")
        return handler(context, inputs)

    def execute_external(self, capability_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if capability_id != "multi_channel_sourcing":
            raise ValueError(f"能力不支持后台渠道执行：{capability_id}")
        client, job = str(request.get("client") or ""), str(request.get("job") or "")
        if not client or not job:
            raise ValueError("寻访任务缺少客户或岗位")
        target = max(1, min(int(request.get("target_count") or 10), 50))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidates_path = self.output_dir / "sourcing" / f"{_slug(client)}-{_slug(job)}-{stamp}.json"
        liepin_path = candidates_path.with_name(candidates_path.stem + "-liepin.json")
        xsaas_path = candidates_path.with_name(candidates_path.stem + "-xsaas.json")
        liepin_queries_path = candidates_path.with_name(candidates_path.stem + "-liepin-queries.json")
        xsaas_queries_path = candidates_path.with_name(candidates_path.stem + "-xsaas-queries.json")
        candidates_path.parent.mkdir(parents=True, exist_ok=True)
        strategy = request.get("strategy") if isinstance(request.get("strategy"), dict) else {}
        channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
        liepin_queries = channels.get("liepin") if isinstance(channels.get("liepin"), list) else []
        xsaas_queries = channels.get("xsaas") if isinstance(channels.get("xsaas"), list) else []
        if not liepin_queries or not xsaas_queries:
            fallback = self._run_json([self.python, str(MULTICHANNEL), "plan", "--db", str(self.service.db_path), "--client", client, "--job", job, "--max-queries", "6"], 90)
            fallback_channels = fallback.get("channels") or {}
            liepin_queries = liepin_queries or fallback_channels.get("liepin") or []
            xsaas_queries = xsaas_queries or fallback_channels.get("xsaas") or []
        # 渠道查询方言层（S4-3c-2 / N1，顾问规则 2026-07-23）：
        # 猎聘维持组合查询（公司 + 职能/技术词可组合，≤2 词/≤6 组）；
        # X-SaaS 公司词独立查询（不与任何词组合），职能/技术词锚定对 ≤2 词/≤8 组；
        # 公司词永不两两成对（一人不可能同时在两家公司）。
        company_terms = query_builders.company_vocabulary(strategy)
        liepin_queries = query_builders.build_liepin_queries(liepin_queries, company_terms=company_terms)
        xsaas_queries = query_builders.build_xsaas_queries(xsaas_queries, company_terms=company_terms)
        liepin_queries_path.write_text(json.dumps({"queries": liepin_queries}, ensure_ascii=False, indent=2), encoding="utf-8")
        xsaas_queries_path.write_text(json.dumps({"queries": xsaas_queries}, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            self.python, str(LIEPIN_SEARCH), "--client", client, "--position", job,
            "--db", str(self.service.db_path), "--output-dir", str(candidates_path.parent),
            "--port", str(int(request.get("cdp_port") or 9223)), "--rounds", "6",
            "--max-cards", str(max(12, target * 2)), "--min-score", "55", "--recommend-score", "65",
            "--capture-links", "--capture-details", "--detail-limit", str(max(12, target * 2)),
            "--no-open-links", "--dry-run", "--json-output", str(liepin_path),
            "--queries-json", str(liepin_queries_path),
        ]
        try:
            search = self._run_json(command, 900)
        except Exception as exc:
            self._record_sourcing_funnel_failure(
                run_id=f"asa-source-{stamp}",
                workflow_id=str(request.get("workflow_id") or ""),
                client=client,
                job=job,
                channel="liepin",
                error=_trim_error(exc),
            )
            raise
        try:
            xsaas = self._run_json([self.python, str(XSAAS_SEARCH), "--queries", str(xsaas_queries_path), "--output", str(xsaas_path), "--port", str(int(request.get("cdp_port") or 9223)), "--max-rows", str(max(12, target * 2))], 300)
        except Exception as exc:
            xsaas = {"ok": False, "status": "blocked", "error": _trim_error(exc)}
            xsaas_path.write_text("[]", encoding="utf-8")
        opencli_shadow = self._run_opencli_shadow(
            request=request,
            client=client,
            job=job,
            port=int(request.get("cdp_port") or 9223),
            limit=max(12, target * 2),
            liepin_queries=liepin_queries,
            xsaas_queries=xsaas_queries,
            liepin_path=liepin_path,
            xsaas_path=xsaas_path,
            artifact_path=candidates_path.with_name(candidates_path.stem + "-opencli-shadow.json"),
        )
        liepin_candidates = _loads(liepin_path.read_text(encoding="utf-8"), [])
        xsaas_candidates = _loads(xsaas_path.read_text(encoding="utf-8"), [])
        combined = liepin_candidates + xsaas_candidates
        candidates_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        dry = self._run_json([self.python, str(MULTICHANNEL), "intake", "--db", str(self.service.db_path), "--client", client, "--job", job, "--input", str(candidates_path)], 120)
        applied = self._run_json([self.python, str(MULTICHANNEL), "intake", "--db", str(self.service.db_path), "--client", client, "--job", job, "--input", str(candidates_path), "--apply"], 180)
        workflow_id = str(request.get("workflow_id") or "")
        attributions = self._persist_sourcing_attributions(
            applied, request.get("strategy") if isinstance(request.get("strategy"), dict) else {},
            workflow_id, client, job,
        )
        channel_runs = [
            {"channel": "liepin", "status": "completed", "result": search},
            {"channel": "xsaas", "status": "completed" if xsaas.get("ok") else "blocked", "result": xsaas},
        ]
        try:
            funnel = self._persist_sourcing_funnel(
                run_id=f"asa-source-{stamp}",
                workflow_id=workflow_id,
                client=client,
                job=job,
                channel_runs=channel_runs,
                channel_candidates={"liepin": liepin_candidates, "xsaas": xsaas_candidates},
                applied=applied,
                attributions=attributions,
                company_vocab=company_terms,
            )
        except Exception as exc:
            funnel = {"ok": False, "stored": 0, "error": str(exc)[:500]}
        sync_script = Path("/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py")
        sync = self._run([self.python, str(sync_script), "--client", client, "--job", job, "--no-open"], 300)
        learning = self._capture_search_learning(client, job, [*liepin_queries, *xsaas_queries])
        return {
            "verified": True,
            "run_id": f"asa-source-{stamp}",
            "channel_runs": channel_runs,
            "opencli_shadow": opencli_shadow,
            "intake": {"dry_run": dry, "applied": applied, "source_file": str(candidates_path)},
            "attributions": attributions,
            "sourcing_funnel": funnel,
            "audit": {"ok": sync.returncode == 0, "summary": (sync.stdout or "")[-4000:]},
            "learning": learning,
        }

    @staticmethod
    def _query_text(entries: list[Any]) -> str:
        if not entries:
            return ""
        first = entries[0]
        value = first.get("query") if isinstance(first, dict) else first
        return " ".join(str(value or "").split())

    def _run_opencli_shadow(
        self,
        *,
        request: dict[str, Any],
        client: str,
        job: str,
        port: int,
        limit: int,
        liepin_queries: list[Any],
        xsaas_queries: list[Any],
        liepin_path: Path,
        xsaas_path: Path,
        artifact_path: Path,
    ) -> dict[str, Any]:
        configured = request.get("opencli_shadow", os.environ.get("ASA_OPENCLI_SHADOW", "1"))
        enabled = str(configured).strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return {"enabled": False, "mode": "read_only_shadow", "affects_intake": False, "channels": []}
        channels = []
        for channel, entries, baseline in (
            ("liepin", liepin_queries, liepin_path),
            ("xsaas", xsaas_queries, xsaas_path),
        ):
            query = self._query_text(entries)
            if not query:
                channels.append({"channel": channel, "status": "skipped", "reason": "missing_query"})
                continue
            channel_limit = min(limit, 24 if channel == "liepin" else 100)
            command = [
                self.python,
                str(OPENCLI_SHADOW),
                "--channel", channel,
                "--query", query,
                "--baseline", str(baseline),
                "--client", client,
                "--job", job,
                "--db", str(self.service.db_path),
                "--port", str(port),
                "--limit", str(channel_limit),
            ]
            try:
                result = self._run_json(command, 600)
                channels.append({"channel": channel, "status": "completed", **result})
            except Exception as exc:
                channels.append({
                    "channel": channel,
                    "status": "blocked",
                    "query": query,
                    "error": _trim_error(exc),
                    "affects_intake": False,
                })
        payload = {
            "enabled": True,
            "mode": "read_only_shadow",
            "affects_intake": False,
            "affects_outreach": False,
            "sample_policy": "first_query_per_channel",
            "channels": channels,
            "artifact": str(artifact_path),
        }
        artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        history_path = artifact_path.parent / "opencli-shadow-history.jsonl"
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "client": client,
                "job": job,
                "workflow_id": str(request.get("workflow_id") or ""),
                "channels": channels,
            }, ensure_ascii=False) + "\n")
        payload["history"] = str(history_path)
        return payload

    def _persist_sourcing_attributions(
        self, applied: dict[str, Any], strategy: dict[str, Any], workflow_id: str, client: str, job: str,
    ) -> dict[str, Any]:
        staged = applied.get("staged") if isinstance(applied.get("staged"), dict) else {}
        accepted = staged.get("accepted") if isinstance(staged.get("accepted"), list) else []
        intake = applied.get("intake") if isinstance(applied.get("intake"), dict) else {}
        receipts = intake.get("receipts") if isinstance(intake.get("receipts"), list) else []
        strategy_channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
        query_meta: dict[tuple[str, str], dict[str, Any]] = {}
        for channel, entries in strategy_channels.items():
            for entry in entries if isinstance(entries, list) else []:
                item = entry if isinstance(entry, dict) else {"query": entry}
                query = " ".join(str(item.get("query") or "").split())
                if query:
                    query_meta[(str(channel), query)] = item
        strategy_hash = hashlib.sha256(json.dumps(strategy, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() if strategy else ""
        strategy_model = str((strategy.get("generation") or {}).get("model") or "") if isinstance(strategy.get("generation"), dict) else ""
        stored = 0
        channel_new: dict[str, int] = {}
        conn = self.service._connect()
        try:
            for index, candidate in enumerate(accepted):
                item = candidate if isinstance(candidate, dict) else {}
                receipt = next(
                    (value for value in receipts if isinstance(value, dict) and value.get("name") == item.get("name") and value.get("status") == "inserted"),
                    receipts[index] if index < len(receipts) and isinstance(receipts[index], dict) else {},
                )
                job_candidate_id = int(receipt.get("job_candidate_id") or 0)
                if not job_candidate_id:
                    continue
                channel = str(item.get("channel") or item.get("source") or "unknown").lower()
                query = " ".join(str(item.get("source_query") or "").split()) or "未记录关键词"
                meta = query_meta.get((channel, query), {})
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_attributions
                    (job_candidate_id,candidate_id,job_id,workflow_id,strategy_hash,strategy_model,
                     channel,source_query,source_round,source_purpose)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_candidate_id,channel,source_query) DO UPDATE SET
                      workflow_id=COALESCE(excluded.workflow_id,workflow_id),
                      strategy_hash=COALESCE(NULLIF(excluded.strategy_hash,''),strategy_hash),
                      strategy_model=COALESCE(NULLIF(excluded.strategy_model,''),strategy_model),
                      source_round=COALESCE(NULLIF(excluded.source_round,''),source_round),
                      source_purpose=COALESCE(NULLIF(excluded.source_purpose,''),source_purpose),
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        job_candidate_id, int(receipt.get("candidate_id") or 0) or None,
                        self._job_id(client, job), workflow_id or None, strategy_hash or None, strategy_model or None,
                        channel, query, str(meta.get("round") or ""), str(meta.get("purpose") or ""),
                    ),
                )
                stored += 1
                channel_new[channel] = channel_new.get(channel, 0) + 1
            conn.commit()
        finally:
            conn.close()
        return {"stored": stored, "strategy_hash": strategy_hash, "workflow_id": workflow_id, "channel_new": channel_new}

    def _persist_sourcing_funnel(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        channel_runs: list[dict[str, Any]],
        channel_candidates: dict[str, list[Any]],
        applied: dict[str, Any],
        attributions: dict[str, Any],
        company_vocab: set[str] | None = None,
    ) -> dict[str, Any]:
        """每个 run×channel 落一行寻访漏斗，并把 0 结果归因回写到 channel_runs 条目上。"""
        staged = applied.get("staged") if isinstance(applied.get("staged"), dict) else {}
        intake_dups: dict[str, int] = {}
        for key in ("existing", "batch_duplicates"):
            entries = staged.get(key) if isinstance(staged.get(key), list) else []
            for entry in entries:
                item = entry if isinstance(entry, dict) else {}
                dup_channel = str(item.get("channel") or item.get("source") or "unknown").lower()
                intake_dups[dup_channel] = intake_dups.get(dup_channel, 0) + 1
        channel_new = attributions.get("channel_new") if isinstance(attributions.get("channel_new"), dict) else {}
        job_id = self._job_id(client, job)
        rows: list[dict[str, Any]] = []
        for run in channel_runs:
            channel = str(run.get("channel") or "unknown").lower()
            status = str(run.get("status") or "completed")
            result = run.get("result") if isinstance(run.get("result"), dict) else {}
            candidates = [item for item in channel_candidates.get(channel) or [] if isinstance(item, dict)]
            rounds = [entry for entry in result.get("rounds") or [] if isinstance(entry, dict)]
            detail = result.get("detail_capture") if isinstance(result.get("detail_capture"), dict) else {}
            recall = sum(_round_int(entry, "result_count") for entry in rounds)
            extracted = sum(_round_int(entry, "extracted_count") for entry in rounds)
            if extracted <= 0 and not rounds:
                extracted = len(candidates)
            scored = [item for item in candidates if item.get("fit_score") is not None]
            high_score = 0
            for item in scored:
                try:
                    if int(item.get("fit_score") or 0) >= 65:
                        high_score += 1
                except (TypeError, ValueError):
                    continue
            zero_attribution = ""
            if not candidates:
                zero_attribution = classify_zero_result(
                    channel,
                    status,
                    result,
                    dedupe_count=max(0, extracted - len(candidates)),
                    company_vocab=company_vocab,
                )
                run["zero_attribution"] = zero_attribution
            rows.append(
                {
                    "run_id": run_id,
                    "workflow_id": workflow_id or None,
                    "job_id": job_id,
                    "client": client,
                    "job": job,
                    "channel": channel,
                    "status": status,
                    "query_count": len(rounds),
                    "queries_json": json.dumps(rounds, ensure_ascii=False),
                    "recall_count": recall,
                    "extracted_count": extracted,
                    "dedupe_count": max(0, extracted - len(candidates)),
                    "unique_count": len(candidates),
                    "detail_complete": _round_int(detail, "complete"),
                    "detail_partial": _round_int(detail, "partial"),
                    "detail_failed": _round_int(detail, "failed"),
                    "intake_duplicate_count": int(intake_dups.get(channel, 0)),
                    "intake_new_count": int(channel_new.get(channel, 0) or 0),
                    "assessed_count": len(scored),
                    "high_score_count": high_score,
                    "zero_attribution": zero_attribution or None,
                    "error": _trim_error(result.get("error")) or None,
                }
            )
        conn = self.service._connect()
        try:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_funnel
                    (run_id,workflow_id,job_id,client,job,channel,status,query_count,queries_json,
                     recall_count,extracted_count,dedupe_count,unique_count,
                     detail_complete,detail_partial,detail_failed,
                     intake_duplicate_count,intake_new_count,assessed_count,high_score_count,
                     zero_attribution,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,channel) DO UPDATE SET
                      workflow_id=COALESCE(excluded.workflow_id,workflow_id),
                      job_id=excluded.job_id,
                      status=excluded.status,
                      query_count=excluded.query_count,
                      queries_json=excluded.queries_json,
                      recall_count=excluded.recall_count,
                      extracted_count=excluded.extracted_count,
                      dedupe_count=excluded.dedupe_count,
                      unique_count=excluded.unique_count,
                      detail_complete=excluded.detail_complete,
                      detail_partial=excluded.detail_partial,
                      detail_failed=excluded.detail_failed,
                      intake_duplicate_count=excluded.intake_duplicate_count,
                      intake_new_count=excluded.intake_new_count,
                      assessed_count=excluded.assessed_count,
                      high_score_count=excluded.high_score_count,
                      zero_attribution=excluded.zero_attribution,
                      error=excluded.error,
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        row["run_id"], row["workflow_id"], row["job_id"], row["client"], row["job"],
                        row["channel"], row["status"], row["query_count"], row["queries_json"],
                        row["recall_count"], row["extracted_count"], row["dedupe_count"], row["unique_count"],
                        row["detail_complete"], row["detail_partial"], row["detail_failed"],
                        row["intake_duplicate_count"], row["intake_new_count"],
                        row["assessed_count"], row["high_score_count"],
                        row["zero_attribution"], row["error"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "stored": len(rows), "run_id": run_id}

    def _record_sourcing_funnel_failure(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        channel: str,
        error: str,
    ) -> None:
        """渠道 runner 在合并前直接失败时，尽力留下一行失败漏斗（绝不掩盖原始异常）。"""
        try:
            conn = self.service._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_funnel
                    (run_id,workflow_id,job_id,client,job,channel,status,zero_attribution,error)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,channel) DO UPDATE SET
                      status=excluded.status,
                      zero_attribution=excluded.zero_attribution,
                      error=excluded.error,
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        run_id, workflow_id or None, self._job_id(client, job), client, job,
                        channel, "failed", classify_zero_result(channel, "failed", {"error": _trim_error(error)}), _trim_error(error),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _run(self, command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or f"命令退出码 {proc.returncode}").strip()
            raise RuntimeError(_trim_error(message, 2000))
        return proc

    def _run_json(self, command: list[str], timeout: int = 300) -> dict[str, Any]:
        proc = self._run(command, timeout)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("能力脚本未返回合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("能力脚本必须返回 JSON 对象")
        return payload

    def _job(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("type") != "job" or not context.get("id"):
            raise ValueError("该能力需要明确岗位上下文")
        conn = self.service._connect()
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
            position_join = ""
            position_fields = ""
            if columns:
                position_join = "LEFT JOIN positions p ON p.client=c.name AND p.title=j.title"
                position_fields = ",p.id AS position_id,p.location AS position_location,p.salary,p.education,p.experience,p.responsibilities,p.requirements,p.headcount,p.deadline,p.liepin_status"
            row = conn.execute(
                f"""
                SELECT j.*,c.name AS client{position_fields}
                FROM jobs j JOIN clients c ON c.id=j.client_id {position_join}
                WHERE j.id=? ORDER BY COALESCE(p.id,0) DESC LIMIT 1
                """ if columns else "SELECT j.*,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (int(context["id"]),),
            ).fetchone()
            if row is None:
                raise ValueError(f"找不到岗位：{context['id']}")
            result = _row(row)
            profile = conn.execute(
                "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
                (result.get("client"), result.get("title")),
            ).fetchone() if self._table(conn, "position_profiles") else None
            result["profile"] = _row(profile)
            return result
        finally:
            conn.close()

    @staticmethod
    def _table(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)).fetchone() is not None

    def _candidate(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("type") != "candidate" or not context.get("id"):
            raise ValueError("该能力需要明确人选上下文")
        return build_candidate_context(self.service.db_path, int(context["id"]))

    def _latest_assessment(self, job_candidate_id: int) -> dict[str, Any]:
        conn = self.service._connect()
        try:
            row = conn.execute(
                "SELECT * FROM agent_candidate_assessments WHERE job_candidate_id=? AND is_current=1 ORDER BY id DESC LIMIT 1",
                (int(job_candidate_id),),
            ).fetchone()
            if row is None:
                result = self.service.submit_assessment(int(job_candidate_id), wait=True)
                return result.get("assessment") or {}
            item = _row(row)
            for key, target, default in (
                ("criteria_json", "criteria", {}), ("strengths_json", "strengths", []),
                ("gaps_json", "gaps", []), ("risks_json", "risks", []),
                ("verification_questions_json", "verification_questions", []),
                ("citations_json", "citations", []),
            ):
                item[target] = _loads(item.get(key), default)
            return item
        finally:
            conn.close()

    def _path(self, category: str, title: str, suffix: str) -> Path:
        folder = self.output_dir / category
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{_slug(title)}-{datetime.now():%Y%m%d-%H%M%S}.{suffix.lstrip('.')}"

    @staticmethod
    def _artifact(kind: str, title: str, *, content: str = "", file_path: Path | None = None,
                  mime_type: str = "text/markdown", validation: str = "passed", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": kind, "title": title, "content": content,
            "file_path": str(file_path) if file_path else "", "mime_type": mime_type,
            "validation_status": validation, "metadata": metadata or {},
        }

    @staticmethod
    def _blocked(summary: str, missing: list[str], references: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"summary": summary, "blocked": True, "missing_inputs": missing, "references": references or []}

    def _job_reference(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "job", "id": job.get("id"), "label": job.get("title"), "subtitle": job.get("client")}]

    def _candidate_reference(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        identity, position, relation = candidate["identity"], candidate["position"], candidate["relation"]
        return [{"type": "candidate", "id": relation["job_candidate_id"], "label": identity.get("name"), "subtitle": f"{position.get('client','')} / {position.get('job','')}"}]

    def _candidate_event(self, candidate: dict[str, Any], event_type: str, status: str, summary: str, raw: dict[str, Any]) -> int:
        relation = candidate["relation"]
        conn = self.service._connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO candidate_events
                (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
                VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,?,?)
                """,
                (relation["job_candidate_id"], relation["person_id"], relation.get("job_id"), event_type, status,
                 summary[:1000], json.dumps(raw, ensure_ascii=False), "agent_workflow", str(raw.get("workflow_id") or "")),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def _followup(self, candidate: dict[str, Any], task_type: str, reason: str, inputs: dict[str, Any], days: int = 2) -> int | None:
        conn = self.service._connect()
        try:
            if not self._table(conn, "followup_tasks"):
                return None
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(followup_tasks)").fetchall()}
            next_id = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM followup_tasks").fetchone()[0])
            identity, position, relation = candidate["identity"], candidate["position"], candidate["relation"]
            values = {
                "id": next_id, "candidate_id": relation.get("source_candidate_id"), "candidate_name": identity.get("name"),
                "candidate_company": identity.get("company"), "client": position.get("client"), "position": position.get("job"),
                "task_type": task_type, "priority": int(inputs.get("priority") or 2),
                "due_at": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                "status": "open", "reason": reason[:1000], "source_table": "agent_workflow",
                "source_id": inputs.get("step_id"), "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "job_candidate_id": relation["job_candidate_id"],
            }
            keys = [key for key in values if key in columns]
            conn.execute(
                f"INSERT INTO followup_tasks ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [values[key] for key in keys],
            )
            conn.commit()
            return next_id
        finally:
            conn.close()

    def run_job_intake(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        content = "\n".join([
            f"# 岗位接入 · {job['client']} / {job['title']}", "",
            f"- 状态：{job.get('status') or '未设置'}", f"- 地点：{job.get('position_location') or job.get('location') or '待补充'}",
            f"- 薪资：{job.get('salary') or '待补充'}", f"- 编制：{job.get('headcount') or '待补充'}",
            "", "## 岗位摘要", str(job.get("summary") or job.get("profile", {}).get("jd_analysis_summary") or "待补充"),
        ])
        return {"summary": "岗位接入信息已从 v3 事实源整理。", "references": self._job_reference(job),
                "artifacts": [self._artifact("job_brief", "岗位接入简报", content=content)]}

    def run_jd_calibration(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        profile = job.get("profile") or {}
        facts = {
            "客户": job.get("client"), "岗位": job.get("title"),
            "地点": job.get("position_location") or job.get("location"), "薪资": job.get("salary"),
            "学历": job.get("education") or profile.get("education_requirement"),
            "经验": job.get("experience") or profile.get("experience_requirement"),
            "硬门槛": _loads(profile.get("hard_requirements_json"), []) or _loads(job.get("hard_requirements"), []),
            "核心能力": _loads(profile.get("ability_keywords_json"), []) or _loads(job.get("ability_keywords"), []),
        }
        missing = [key for key in ("岗位", "地点", "硬门槛", "核心能力") if not facts.get(key)]
        content = "# JD 校准\n\n" + "\n".join(f"- {key}：{json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value or '待补充'}" for key, value in facts.items())
        return {"summary": f"JD 校准完成；{len(missing)} 项待补充。", "missing_inputs": missing,
                "references": self._job_reference(job), "artifacts": [self._artifact("jd_calibration", "JD 校准结果", content=content, validation="needs_input" if missing else "passed")]}

    def run_job_library_update(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        objective = str(inputs.get("objective") or "")
        directions = self._job_update_directions(objective, inputs)
        conn = self.service._connect()
        try:
            client = self._job_update_client(conn, context, objective, inputs)
            if not client:
                return self._blocked("岗位库更新缺少客户名，未写入数据库。", ["client"])
            profiles = self._job_update_profiles(conn, client, objective, directions)
            if not profiles:
                return self._blocked("没有找到可用于更新岗位库的岗位画像，未写入数据库。", ["position_profiles"])
            client_row = conn.execute("SELECT id,name FROM clients WHERE name=?", (client,)).fetchone()
            if client_row is None:
                cursor = conn.execute("INSERT INTO clients(name) VALUES (?)", (client,))
                client_id = int(cursor.lastrowid)
            else:
                client_id = int(client_row["id"])

            changes: list[dict[str, Any]] = []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for profile in profiles:
                change = self._upsert_profile_job(conn, client_id, client, _row(profile), now)
                changes.append(change)
            archived = []
            if self._should_archive_legacy_update(objective, directions):
                archived = self._archive_legacy_split_jobs(conn, client, [item["title"] for item in changes], now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        sync_result: dict[str, Any] = {"skipped": True}
        if not inputs.get("skip_sync"):
            sync_result = self._sync_job_library(client)
        receipt = {
            "client": client,
            "directions": directions,
            "changes": changes,
            "archived_legacy": archived,
            "sync": sync_result,
        }
        content = "# 岗位库更新回执\n\n```json\n" + json.dumps(receipt, ensure_ascii=False, indent=2) + "\n```"
        return {
            "summary": f"岗位库已更新：{client} 新增/更新 {len(changes)} 个岗位方向。",
            "references": [{"type": "job", "id": item["job_id"], "label": item["title"], "subtitle": client} for item in changes],
            "artifacts": [self._artifact("job_library_update_receipt", "岗位库更新回执", content=content, metadata=receipt)],
            "job_library_update": receipt,
        }

    def _job_update_client(self, conn: sqlite3.Connection, context: dict[str, Any], objective: str, inputs: dict[str, Any]) -> str:
        explicit = str(inputs.get("client") or "").strip()
        if explicit:
            return explicit
        if context.get("type") == "job" and context.get("id"):
            row = conn.execute(
                "SELECT c.name FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                (int(context["id"]),),
            ).fetchone()
            if row:
                return str(row["name"] or "")
        rows = conn.execute("SELECT name FROM clients ORDER BY LENGTH(name) DESC").fetchall()
        for row in rows:
            name = str(row["name"] or "")
            if name and name in objective:
                return name
        return ""

    @staticmethod
    def _job_update_directions(objective: str, inputs: dict[str, Any]) -> list[str]:
        raw = inputs.get("directions")
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            if values:
                return values
        mapping = (("PC", ("PC", "pc", "电脑")), ("服务器", ("服务器", "server", "Server")), ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")))
        directions = [label for label, tokens in mapping if any(token in objective for token in tokens)]
        return directions

    def _job_update_profiles(self, conn: sqlite3.Connection, client: str, objective: str, directions: list[str]) -> list[sqlite3.Row]:
        if not self._table(conn, "position_profiles"):
            return []
        clauses = ["client=?"]
        params: list[Any] = [client]
        if "技术市场" in objective:
            clauses.append("position LIKE '%技术市场%'")
        if directions:
            direction_clauses = []
            for direction in directions:
                if direction == "PC":
                    direction_clauses.append("(position LIKE '%PC%' OR position LIKE '%电脑%')")
                elif direction == "服务器":
                    direction_clauses.append("position LIKE '%服务器%'")
                elif direction == "ADAS":
                    direction_clauses.append("position LIKE '%ADAS%'")
            if direction_clauses:
                clauses.append("(" + " OR ".join(direction_clauses) + ")")
        rows = conn.execute(
            f"SELECT * FROM position_profiles WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        ).fetchall()
        if directions:
            rows = [row for row in rows if "服务器或PC市场" not in str(row["position"] or "")]
        return rows

    def _upsert_profile_job(self, conn: sqlite3.Connection, client_id: int, client: str, profile: dict[str, Any], now: str) -> dict[str, Any]:
        title = str(profile.get("position") or "").strip()
        if not title:
            raise ValueError("岗位画像缺少 position")
        position = conn.execute(
            "SELECT * FROM positions WHERE client=? AND title=? ORDER BY id DESC LIMIT 1",
            (client, title),
        ).fetchone() if self._table(conn, "positions") else None
        position_data = _row(position)
        hard = _list_text(profile.get("hard_requirements_json")) or str(position_data.get("hard_requirements") or "")
        ability = _list_text(profile.get("ability_keywords_json")) or str(position_data.get("ability_keywords") or "")
        targets = _list_text(profile.get("target_companies_json")) or str(position_data.get("target_companies") or "")
        exclusions = _list_text(profile.get("exclusion_tags_json")) or str(position_data.get("exclusions") or "")
        search_words = _list_text(profile.get("search_keywords_json")) or str(position_data.get("search_words") or title)
        summary = str(profile.get("jd_analysis_summary") or position_data.get("summary") or f"{client}/{title} 岗位画像已按方向拆分。")
        status = str(position_data.get("status") or "待启动")
        existing = conn.execute("SELECT * FROM jobs WHERE client_id=? AND title=?", (client_id, title)).fetchone()
        if existing:
            job_id = int(existing["id"])
            lifecycle = str(existing["lifecycle_stage"] or "sourcing")
            if lifecycle in {"archived", "closed", "cancelled"}:
                lifecycle = "sourcing"
            conn.execute(
                """
                UPDATE jobs
                   SET status=?,lifecycle_stage=?,hard_requirements=?,ability_keywords=?,
                       target_companies=?,exclusions=?,search_words=?,summary=?,
                       source_layer='v3_position_profile',updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (status, lifecycle, hard, ability, targets, exclusions, search_words, summary, job_id),
            )
            action = "updated"
        else:
            cursor = conn.execute(
                """
                INSERT INTO jobs
                (client_id,title,status,lifecycle_stage,source_layer,hard_requirements,ability_keywords,
                 target_companies,exclusions,search_words,summary,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),datetime('now','localtime'))
                """,
                (client_id, title, status, "sourcing", "v3_position_profile", hard, ability, targets, exclusions, search_words, summary),
            )
            job_id = int(cursor.lastrowid)
            action = "created"
        if position_data:
            conn.execute(
                """
                UPDATE positions
                   SET hard_requirements=?,ability_keywords=?,target_companies=?,exclusions=?,
                       search_words=?,summary=?,updated_at=datetime('now','localtime')
                 WHERE client=? AND title=?
                """,
                (hard, ability, targets, exclusions, search_words, summary, client, title),
            )
        self._upsert_job_metrics(conn, job_id, profile, now)
        return {"action": action, "job_id": job_id, "title": title, "position_id": position_data.get("id")}

    def _upsert_job_metrics(self, conn: sqlite3.Connection, job_id: int, profile: dict[str, Any], now: str) -> None:
        payload = {
            "metric_date": now[:10],
            "priority": "P0-最急｜用户指定最高优先级",
            "risk": _list_text(profile.get("risk_points_json")),
            "next_keywords_json": profile.get("search_keywords_json") or "[]",
            "target_companies_json": profile.get("target_companies_json") or "[]",
            "exclude_terms_json": profile.get("exclusion_tags_json") or "[]",
        }
        row = conn.execute("SELECT id FROM job_pipeline_metrics WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if row:
            conn.execute(
                """
                UPDATE job_pipeline_metrics
                   SET metric_date=?,priority=?,risk=?,next_keywords_json=?,
                       target_companies_json=?,exclude_terms_json=?,data_gap=0
                 WHERE id=?
                """,
                (payload["metric_date"], payload["priority"], payload["risk"], payload["next_keywords_json"],
                 payload["target_companies_json"], payload["exclude_terms_json"], int(row["id"])),
            )
        else:
            conn.execute(
                """
                INSERT INTO job_pipeline_metrics
                (job_id,metric_date,a_count,b_count,p0_count,p1_count,published_count,under_review_count,
                 contacted_count,pending_followup_count,priority,risk,next_keywords_json,target_companies_json,
                 exclude_terms_json,data_gap)
                VALUES (?,?,0,0,0,0,0,0,0,0,?,?,?,?,?,0)
                """,
                (job_id, payload["metric_date"], payload["priority"], payload["risk"], payload["next_keywords_json"],
                 payload["target_companies_json"], payload["exclude_terms_json"]),
            )

    @staticmethod
    def _should_archive_legacy_update(objective: str, directions: list[str]) -> bool:
        return bool(directions) and any(token in objective for token in ("拆", "分成", "三个", "分别", "独立"))

    def _archive_legacy_split_jobs(self, conn: sqlite3.Connection, client: str, active_titles: list[str], now: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT j.id,j.title
            FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE c.name=? AND j.title LIKE '%技术市场%' AND j.title LIKE '%服务器或PC市场%'
            """,
            (client,),
        ).fetchall()
        archived = []
        for row in rows:
            if row["title"] in active_titles:
                continue
            conn.execute(
                """
                UPDATE jobs
                   SET status='已拆分为PC/服务器/ADAS-保留历史',
                       lifecycle_stage='archived',
                       closed_reason='岗位画像已拆分为独立方向，历史人选关系保留',
                       closed_at=datetime('now','localtime'),
                       updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (int(row["id"]),),
            )
            archived.append({"job_id": int(row["id"]), "title": row["title"]})
        if self._table(conn, "positions"):
            conn.execute(
                """
                UPDATE positions
                   SET status='已拆分为PC/服务器/ADAS-保留历史',
                       updated_at=datetime('now','localtime')
                 WHERE client=? AND title LIKE '%技术市场%' AND title LIKE '%服务器或PC市场%'
                """,
                (client,),
            )
        return archived

    def _sync_job_library(self, client: str) -> dict[str, Any]:
        command = [
            self.python,
            "/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py",
            "--client",
            client,
            "--db",
            str(self.service.db_path),
            "--no-open",
            "--no-backup",
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-2000:],
        }

    def _strategy_learning_context(self, job: dict[str, Any]) -> dict[str, Any]:
        conn = self.service._connect()
        try:
            experiments = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT channel,query,result_count,viewed_count,extracted_count,
                           recommended_count,reply_count,positive_reply_count,noise_notes,status,updated_at
                    FROM search_experiments
                    WHERE client=? AND position=?
                    ORDER BY datetime(COALESCE(updated_at,run_time,created_at)) DESC,id DESC LIMIT 24
                    """,
                    (job["client"], job["title"]),
                ).fetchall()
            ] if self._table(conn, "search_experiments") else []
            correction = _row(conn.execute(
                "SELECT * FROM strategy_corrections WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
                (job["client"], job["title"]),
            ).fetchone()) if self._table(conn, "strategy_corrections") else {}
            business_outcomes = [
                _row(row)
                for row in conn.execute(
                    """
                    SELECT sa.channel,sa.source_query,
                           COUNT(DISTINCT sa.job_candidate_id) AS attributed_candidates,
                           COUNT(sf.id) AS signal_count,ROUND(COALESCE(SUM(sf.weight),0),2) AS experience_score,
                           SUM(sf.signal_type='review_pass') AS review_pass,
                           SUM(sf.signal_type='contacted') AS contacted,
                           SUM(sf.signal_type='recommended') AS recommended,
                           SUM(sf.signal_type='stopped') AS stopped,
                           SUM(sf.signal_type IN ('client_approved','client_interview','client_offer','client_hired')) AS client_positive,
                           SUM(sf.signal_type='client_rejected') AS client_rejected
                    FROM agent_sourcing_attributions sa
                    LEFT JOIN agent_sourcing_feedback sf ON sf.attribution_id=sa.id
                    WHERE sa.job_id=?
                    GROUP BY sa.channel,sa.source_query
                    ORDER BY experience_score DESC,signal_count DESC,sa.id DESC LIMIT 30
                    """,
                    (job["id"],),
                ).fetchall()
            ] if self._table(conn, "agent_sourcing_attributions") and self._table(conn, "agent_sourcing_feedback") else []
        finally:
            conn.close()
        memories = self.service.search_memories(
            f"{job['client']} {job['title']} 寻访关键词 搜索效果",
            context_type="job", context_id=job["id"], client=str(job["client"]), job=str(job["title"]), limit=8,
        )
        return {
            "historical_experiments": experiments,
            "explicit_corrections": correction,
            "business_outcomes": business_outcomes,
            "approved_memories": memories.get("memories") or [] if memories.get("mode") == "active" else [],
            "memory_mode": memories.get("mode") or "off",
            "memory_hits": len(memories.get("memories") or []),
        }

    @staticmethod
    def _normalize_strategy_entries(values: Any) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for index, value in enumerate(values if isinstance(values, list) else []):
            item = value if isinstance(value, dict) else {"query": value}
            query = " ".join(str(item.get("query") or "").split())
            if not query:
                continue
            rows.append({
                "round": str(item.get("round") or f"model-{index + 1}"),
                "query": query,
                "purpose": str(item.get("purpose") or "大模型生成的岗位寻访查询"),
                "evidence": str(item.get("evidence") or "岗位事实"),
            })
        return rows

    @staticmethod
    def _strategy_term_grounded(term: str, canonical_text: str) -> bool:
        canonical = re.sub(r"\s+", "", canonical_text.lower())
        normalized = re.sub(r"\s+", "", term.lower())
        if normalized and normalized in canonical:
            return True
        parts = [value for value in re.split(r"[\s/、,，;；]+", term) if value]
        if len(parts) > 1:
            return all(RecruitingCapabilityRuntime._strategy_term_grounded(value, canonical_text) for value in parts)
        ascii_tokens = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", term)
        if ascii_tokens and not all(token.lower() in canonical for token in ascii_tokens):
            return False
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", term))
        if not chinese:
            return bool(ascii_tokens)
        if len(chinese) <= 2:
            return chinese.lower() in canonical
        bigrams = [chinese[index:index + 2] for index in range(len(chinese) - 1)]
        matched = sum(value.lower() in canonical for value in bigrams)
        return matched / max(1, len(bigrams)) >= 0.5

    def _validate_model_strategy(
        self, raw: dict[str, Any], fallback: dict[str, Any], job: dict[str, Any], learning: dict[str, Any], max_queries: int,
    ) -> dict[str, Any]:
        profile = job.get("profile") or {}
        canonical_text = " ".join(
            str(value or "")
            for value in (job.get("title"), job.get("responsibilities"), job.get("requirements"), job.get("hard_requirements"), job.get("exclusions"))
        ).lower()
        legacy_terms = [
            str(value).strip()
            for key in ("ability_keywords_json", "search_keywords_json")
            for value in _loads(profile.get(key), [])
            if str(value).strip()
        ]
        unsupported = {
            term for term in legacy_terms
            if len(re.sub(r"\W+", "", term)) >= 3 and not self._strategy_term_grounded(term, canonical_text)
        }
        raw_channels = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
        fallback_channels = fallback.get("channels") if isinstance(fallback.get("channels"), dict) else {}
        removed: list[str] = []
        channels: dict[str, list[dict[str, str]]] = {}
        for channel in ("liepin", "xsaas"):
            accepted: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in [*self._normalize_strategy_entries(raw_channels.get(channel)), *self._normalize_strategy_entries(fallback_channels.get(channel))]:
                normalized = re.sub(r"\s+", "", item["query"].lower())
                bad = next((term for term in unsupported if re.sub(r"\s+", "", term.lower()) in normalized), "")
                if bad:
                    removed.append(f"{item['query']}（无岗位依据：{bad}）")
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                accepted.append(item)
                if len(accepted) >= max_queries:
                    break
            channels[channel] = accepted
        model_used = any(self._normalize_strategy_entries(raw_channels.get(channel)) for channel in ("liepin", "xsaas"))
        return {
            **{key: value for key, value in fallback.items() if key not in {"channels"}},
            "channels": channels,
            "target_companies": raw.get("target_companies") or fallback.get("target_companies") or [],
            "strategy_summary": str(raw.get("strategy_summary") or "围绕岗位硬门槛、应用场景与目标公司分层寻访。"),
            "learning_notes": raw.get("learning_notes") if isinstance(raw.get("learning_notes"), list) else [],
            "generation": {
                "mode": "llm" if model_used else "deterministic_fallback",
                "model": self.service.llm.model,
                "memory_mode": learning["memory_mode"],
                "memory_hits": learning["memory_hits"],
                "experiment_count": len(learning["historical_experiments"]),
                "removed_unsupported_queries": removed,
            },
        }

    def _capture_search_learning(self, client: str, job: str, queries: list[Any]) -> dict[str, Any]:
        query_values = [
            " ".join(str((item or {}).get("query") if isinstance(item, dict) else item or "").split())
            for item in queries
        ]
        query_values = list(dict.fromkeys(value for value in query_values if value))
        if not query_values:
            return {"stored_memories": 0, "queries": 0}
        conn = self.service._connect()
        try:
            placeholders = ",".join("?" for _ in query_values)
            rows = conn.execute(
                f"""
                SELECT channel,query,result_count,viewed_count,recommended_count,positive_reply_count,noise_notes
                FROM search_experiments
                WHERE client=? AND position=? AND query IN ({placeholders})
                ORDER BY datetime(COALESCE(updated_at,run_time,created_at)) DESC,id DESC
                """,
                [client, job, *query_values],
            ).fetchall()
        finally:
            conn.close()
        stored = 0
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["channel"] or ""), str(row["query"] or ""))
            if key in seen:
                continue
            seen.add(key)
            recommended = int(row["recommended_count"] or 0)
            viewed = int(row["viewed_count"] or 0)
            outcome = "有效" if recommended > 0 else "待降权"
            content = (
                f"{client}/{job} 搜索经验：{key[0] or '未知渠道'} 关键词“{key[1]}”{outcome}；"
                f"查看 {viewed}，推荐 {recommended}，正向回复 {int(row['positive_reply_count'] or 0)}。"
            )
            if row["noise_notes"]:
                content += f" 噪音：{str(row['noise_notes'])[:160]}"
            self.service.store_memory(
                scope_type="job", scope_id=str(self._job_id(client, job)), memory_type="search_outcome",
                content=content, source_type="search_experiment", source_id=f"{key[0]}:{key[1]}",
                confidence=0.9 if recommended > 0 else 0.72,
            )
            stored += 1
        return {"stored_memories": stored, "queries": len(query_values)}

    def _job_id(self, client: str, job: str) -> int:
        conn = self.service._connect()
        try:
            row = conn.execute(
                "SELECT j.id FROM jobs j JOIN clients c ON c.id=j.client_id WHERE c.name=? AND j.title=? ORDER BY j.id LIMIT 1",
                (client, job),
            ).fetchone()
            return int(row["id"]) if row else 0
        finally:
            conn.close()

    def run_search_strategy(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        max_queries = min(12, int(inputs.get("max_queries") or 6))
        command = [self.python, str(MULTICHANNEL), "plan", "--db", str(self.service.db_path), "--client", str(job["client"]), "--job", str(job["title"]), "--max-queries", str(max_queries)]
        try:
            fallback = self._run_json(command, 60)
        except Exception:
            profile = job.get("profile") or {}
            fallback = {
                "client": job["client"], "job": job["title"],
                "queries": _loads(profile.get("search_keywords_json"), [])[:6],
                "target_companies": _loads(profile.get("target_companies_json"), [])[:20],
                "exclusions": _loads(profile.get("exclusion_tags_json"), [])[:20],
                "source": "v3_fallback",
            }
        learning = self._strategy_learning_context(job)
        # S4-1：策略生成前先定级（L1/L2/L3）并盘点四锚点；顾问在 Copilot 侧的
        # 放行/锚点回复经工作流上下文 strategy_clarification 传入（ consultant_override /
        # consultant_answers ），推断项按 PRD §1 保持 inferred:true + confidence。
        clarification = context.get("strategy_clarification") if isinstance(context.get("strategy_clarification"), dict) else {}
        consultant = {
            "consultant_override": bool(clarification.get("consultant_override")),
            "consultant_answers": str(clarification.get("consultant_answers") or ""),
        }
        archetype, archetype_trace = strategy_v2.match_job_archetype(job.get("client"), job.get("title"))
        classification = strategy_v2.classify_strategy_input(
            job, archetype=archetype, consultant_answers=consultant["consultant_answers"]
        )
        classification["trace"] = [*archetype_trace, *classification["trace"]]
        # S4-2：知识库消费 —— 客户画像挂载（精确/别名/模糊需确认）、公司图谱召回、
        # restricted 白名单约束。画像与图谱进 LLM 输入；restricted 键值不进任何生成
        # 上下文，仅由运行时并入 negative_rules（source=restricted_client）。
        profile_match, profile_trace = knowledge_base.match_client_profile(job.get("client"))
        restricted_info, restricted_trace = knowledge_base.load_restricted_constraints(job.get("client"))
        graph, graph_trace = knowledge_base.load_company_graph()
        graph_query = " ".join(
            part
            for part in (
                str(job.get("title") or ""),
                str(job.get("ability_keywords") or ""),
                knowledge_base.profile_context(profile_match["profile"]).get("track", "") if profile_match else "",
            )
            if part
        )
        graph_pool, graph_pool_trace = knowledge_base.derive_graph_pool(graph, query_text=graph_query)
        # 目标池不得含客户本公司（图谱按赛道召回时本公司常高分命中）
        client_raw = " ".join(str(job.get("client") or "").split())
        client_norm = knowledge_base.normalize_client_name(client_raw)
        before = len(graph_pool)
        graph_pool = [
            company
            for company in graph_pool
            if not knowledge_base.name_match_rule(client_raw, client_norm, company["name"])[0]
        ]
        if len(graph_pool) < before:
            graph_pool_trace.append(f"已从图谱池剔除客户本公司 {before - len(graph_pool)} 家")
        # S4-3：排除规则引擎 —— 第 4 步之后强制过五类检查清单（PRD §4），
        # 逐类输出 适用/不适用+依据；禁挖名单/竞业从 restricted 层按客户继承。
        # 只在运行时并入 negative_rules，绝不进入 LLM 输入。
        negative_checklist, checklist_trace = negative_rules.build_negative_rule_checklist(
            job,
            restricted_info=restricted_info,
            archetype=archetype,
            consultant_answers=consultant["consultant_answers"],
        )
        classification["trace"] = [
            *classification["trace"], *profile_trace, *restricted_trace, *graph_trace, *graph_pool_trace, *checklist_trace,
        ]
        client_profile_payload: dict[str, Any] = {"matched": False}
        if profile_match:
            client_profile_payload = {
                "matched": True,
                "name": profile_match["name"],
                "rule": profile_match["rule"],
                "needs_confirmation": profile_match["needs_confirmation"],
                "context": knowledge_base.profile_context(profile_match["profile"]),
            }
        payload = {
            "canonical_position": {
                "client": job["client"], "job": job["title"],
                "requirements": job.get("requirements") or job.get("hard_requirements") or "",
                "responsibilities": job.get("responsibilities") or "",
                "education": job.get("education") or "", "experience": job.get("experience") or "",
                "hard_requirements": job.get("hard_requirements") or "",
                "exclusions": job.get("exclusions") or "", "location": job.get("position_location") or job.get("location") or "",
                "objective": inputs.get("objective") or "",
            },
            "legacy_profile_suggestions": {
                "ability_keywords": _loads((job.get("profile") or {}).get("ability_keywords_json"), []),
                "search_keywords": _loads((job.get("profile") or {}).get("search_keywords_json"), []),
                "target_companies": _loads((job.get("profile") or {}).get("target_companies_json"), []),
            },
            "input_classification": {
                "input_level": classification["input_level"],
                "anchors": classification["anchors"],
                "missing_anchors": classification["missing_anchors"],
            },
            "job_archetype": {
                key: archetype[key]
                for key in ("archetype_id", "title", "client", "essence", "directions", "target_functions", "level_mapping", "keyword_groups")
            } if archetype else {},
            "consultant_input": consultant,
            "client_profile": client_profile_payload,
            "kb_graph_candidates": graph_pool,
            **learning,
            "deterministic_fallback": fallback,
        }
        try:
            generated = self.service.llm.generate_search_strategy(payload)
        except Exception:
            generated = {}
        llm_v2_fragment = generated.pop("strategy_v2", None) if isinstance(generated, dict) else None
        plan = self._validate_model_strategy(generated, fallback, job, learning, max_queries)
        plan["generation"]["input_level"] = classification["input_level"]
        v2 = strategy_v2.build_strategy_v2(
            plan, classification, archetype=archetype, consultant=consultant, llm_fragment=llm_v2_fragment,
            profile_match=knowledge_base.profile_matched_info(profile_match),
            graph_pool=graph_pool,
            restricted_rules=knowledge_base.restricted_negative_rules(restricted_info),
            negative_checklist=negative_checklist,
        )
        v2_ok, v2_errors = strategy_v2.validate_strategy_v2(v2)
        result: dict[str, Any] = {
            "summary": "已由大模型基于岗位事实、历史实验和长期记忆生成寻访策略，并完成无依据关键词校验。",
            "strategy": plan,
            "input_level": classification["input_level"],
            "references": self._job_reference(job),
        }
        if v2_ok:
            result["strategy_v2"] = v2
            content = "# 多渠道寻访策略（strategy_v2）\n\n```json\n" + json.dumps(v2, ensure_ascii=False, indent=2) + "\n```"
            result["artifacts"] = [
                self._artifact(
                    "search_strategy", "多渠道寻访策略", content=content,
                    metadata={"plan": plan, "strategy_v2": v2, "schema_version": strategy_v2.STRATEGY_V2_VERSION},
                )
            ]
        else:
            # 硬性约束：缺必备键/版本号错误时不写库，留 error 供排查。
            result["strategy_v2_error"] = {"errors": v2_errors, "trace": classification["trace"][-12:]}
        return result

    def run_multi_channel_sourcing(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        strategy = self._workflow_strategy(inputs)
        try:
            preflight = self._run_json([self.python, str(MULTICHANNEL), "preflight", "--db", str(self.service.db_path), "--client", str(job["client"]), "--job", str(job["title"]), "--port", str(int(inputs.get("cdp_port") or 9223))], 90)
        except Exception as exc:
            preflight = {"ok": False, "status": "preflight_unavailable", "error": str(exc)[:1000]}
        channels = preflight.get("channels") or preflight.get("preflight") or {}
        ticket = {
            "client": job["client"], "job": job["title"], "preflight": preflight,
            "workflow_id": str(inputs.get("workflow_id") or ""),
            "target_count": int(inputs.get("target_count") or self._target_count(inputs.get("objective")) or 10),
            "cdp_port": int(inputs.get("cdp_port") or 9223),
            "strategy": strategy,
            "required_result": {"verified": True, "channel_runs": [], "intake": {}, "audit": {}},
        }
        return {
            "summary": "渠道预检完成，已生成受约束寻访任务；渠道执行完成并读回前不会进入评估。",
            "references": self._job_reference(job), "external_action_executed": False,
            "external_request": ticket,
            "auto_execute_request": ticket if preflight.get("ok") is not False else None,
            "artifacts": [self._artifact("sourcing_ticket", "多渠道寻访执行任务", content=json.dumps(ticket, ensure_ascii=False, indent=2), validation="pending_execution", metadata={"channels": channels})],
        }

    def _workflow_strategy(self, inputs: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(inputs.get("workflow_id") or "")
        if not workflow_id:
            return {}
        conn = self.service._connect()
        try:
            row = conn.execute(
                "SELECT output_json FROM agent_workflow_steps WHERE workflow_id=? AND capability_id='search_strategy' AND status='completed' ORDER BY sequence DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            output = _loads(row["output_json"], {}) if row else {}
            return output.get("strategy") if isinstance(output.get("strategy"), dict) else {}
        finally:
            conn.close()

    @staticmethod
    def _target_count(objective: Any) -> int:
        match = re.search(r"(\d+)\s*(?:位|个|人)", str(objective or ""))
        return min(100, int(match.group(1))) if match else 0

    def run_job_publish_prepare(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        job = self._job(context)
        overrides = inputs.get("publish_fields") if isinstance(inputs.get("publish_fields"), dict) else {}
        draft = {
            "client_company": overrides.get("client_company") or job.get("client"),
            "job_title": overrides.get("job_title") or job.get("title"),
            "city_keyword": overrides.get("city_keyword") or job.get("position_location") or job.get("location"),
            "city_choice": overrides.get("city_choice") or job.get("position_location") or job.get("location"),
            "salary_low_k": overrides.get("salary_low_k"), "salary_high_k": overrides.get("salary_high_k"),
            "salary_months": overrides.get("salary_months") or 12,
            "job_category_keyword": overrides.get("job_category_keyword"), "job_category_choice": overrides.get("job_category_choice"),
            "industry_keyword": overrides.get("industry_keyword"), "industry_choice": overrides.get("industry_choice"),
            "work_year_keyword": overrides.get("work_year_keyword") or job.get("experience"),
            "work_year_choice": overrides.get("work_year_choice"), "work_year_low": overrides.get("work_year_low"), "work_year_high": overrides.get("work_year_high"),
            "education_choice": overrides.get("education_choice") or job.get("education"), "education_tongzhao": bool(overrides.get("education_tongzhao", False)),
            "private_job": bool(overrides.get("private_job", False)), "recruit_count": int(overrides.get("recruit_count") or job.get("headcount") or 1),
            "close_date": overrides.get("close_date") or job.get("deadline"),
            "description": overrides.get("description") or "\n".join(filter(None, [str(job.get("responsibilities") or ""), str(job.get("requirements") or job.get("summary") or "")])),
        }
        salary = str(job.get("salary") or "")
        numbers = [int(value) for value in re.findall(r"\d+", salary)]
        if not draft["salary_low_k"] and numbers:
            draft["salary_low_k"] = numbers[0]
        if not draft["salary_high_k"] and len(numbers) > 1:
            draft["salary_high_k"] = numbers[1]
        required = ["client_company", "job_title", "city_keyword", "salary_low_k", "salary_high_k", "job_category_choice", "industry_choice", "description", "close_date"]
        missing = [key for key in required if draft.get(key) in (None, "")]
        path = self._path("job_publish", f"{job['client']}-{job['title']}-draft", "json")
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        artifacts = [
            self._artifact(
                "job_publish_draft", "猎聘岗位发布草稿", file_path=path, mime_type="application/json",
                content=json.dumps(draft, ensure_ascii=False, indent=2),
                validation="needs_input" if missing else "passed",
                metadata={"client": job.get("client"), "job": job.get("title"), "job_id": job.get("id")},
            )
        ]
        result = {"summary": "岗位发布草稿已生成。" if not missing else "岗位发布草稿缺少关键字段，已阻塞正式发布。",
                  "references": self._job_reference(job), "draft": draft, "missing_inputs": missing,
                  "artifacts": artifacts}
        if missing:
            result["blocked"] = True
            return result
        prepare_log = self._path("job_publish", "liepin-publish-prepare-readback", "json")
        payload = self._run_json([
            self.python, str(LIEPIN_PUBLISH), "--mode", "prepare",
            "--port", str(int(inputs.get("cdp_port") or 9223)), "--draft", str(path), "--log", str(prepare_log),
        ], 180)
        failed = payload.get("ok") is False or str(payload.get("status") or "").lower() in {"blocked", "failed", "error"}
        artifacts.append(self._artifact(
            "job_publish_prepare_readback", "猎聘岗位发布预检读回", file_path=prepare_log,
            mime_type="application/json", content=json.dumps(payload, ensure_ascii=False, indent=2),
            validation="blocked" if failed else "passed",
            metadata={"draft_path": str(path), "client": job.get("client"), "job": job.get("title"), "job_id": job.get("id")},
        ))
        result["prepare_readback"] = payload
        result["summary"] = "岗位发布草稿已填入猎聘发布表单并完成读回预检。" if not failed else "猎聘岗位发布预检未通过，已阻塞正式发布。"
        if failed:
            result["blocked"] = True
            result["missing_inputs"] = ["修正猎聘发布预检问题"]
        return result

    def run_job_publish_execute(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        draft = self._dependency_file(inputs, "job_publish_draft")
        readback = self._dependency_file(inputs, "job_publish_prepare_readback")
        if not draft or not readback:
            return self._blocked("没有通过校验和读回的岗位发布草稿。", ["job_publish_draft", "job_publish_prepare_readback"])
        log = self._path("job_publish", "liepin-publish-readback", "json")
        payload = self._run_json([self.python, str(LIEPIN_PUBLISH), "--mode", "publish", "--confirm", "PUBLISH", "--port", str(int(inputs.get("cdp_port") or 9223)), "--draft", str(draft), "--log", str(log)], 180)
        verified = bool(payload.get("verified") or payload.get("status") in {"published", "submitted", "auditing"})
        if not verified:
            raise RuntimeError("猎聘发布动作未通过结果页或职位列表读回验证")
        job = self._job(context)
        conn = self.service._connect()
        try:
            if self._table(conn, "positions"):
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
                assignments = ["status='已发布'", "updated_at=datetime('now','localtime')"]
                if "liepin_status" in columns:
                    assignments.append("liepin_status='已发布/已验证'")
                if "liepin_published_at" in columns:
                    assignments.append("liepin_published_at=datetime('now','localtime')")
                if "liepin_verify_log" in columns:
                    assignments.append("liepin_verify_log=?")
                    conn.execute(f"UPDATE positions SET {','.join(assignments)} WHERE client=? AND title=?", (str(log), job["client"], job["title"]))
                else:
                    conn.execute(f"UPDATE positions SET {','.join(assignments)} WHERE client=? AND title=?", (job["client"], job["title"]))
                conn.commit()
        finally:
            conn.close()
        return {"summary": "猎聘岗位已发布并完成页面读回验证。", "external_action_executed": True,
                "external_result": payload, "artifacts": [self._artifact("external_action_receipt", "猎聘岗位发布回执", file_path=log, mime_type="application/json", content=json.dumps(payload, ensure_ascii=False, indent=2))]}

    def _dependency_file(self, inputs: dict[str, Any], artifact_type: str) -> Path | None:
        workflow_id = str(inputs.get("workflow_id") or "")
        if not workflow_id:
            return None
        conn = self.service._connect()
        try:
            row = conn.execute("SELECT file_path,validation_status FROM agent_artifacts WHERE workflow_id=? AND artifact_type=? ORDER BY id DESC LIMIT 1", (workflow_id, artifact_type)).fetchone()
            if row and row["file_path"] and row["validation_status"] == "passed" and Path(row["file_path"]).exists():
                return Path(row["file_path"])
            return None
        finally:
            conn.close()

    def run_resume_export(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        from docx import Document
        identity, position = candidate["identity"], candidate["position"]
        doc = Document()
        doc.add_heading(str(identity.get("name") or "候选人简历"), 0)
        for label, value in (("当前公司", identity.get("company")), ("当前职位", identity.get("title")), ("城市", identity.get("city")), ("学历", identity.get("education")), ("经验", identity.get("experience")), ("目标岗位", f"{position.get('client','')} / {position.get('job','')}")):
            doc.add_paragraph(f"{label}：{value or '不详'}")
        doc.add_heading("履历原始证据", level=1)
        profiles = candidate.get("source_profiles") or []
        if not profiles:
            doc.add_paragraph("暂无来源简历正文。")
        for profile in profiles:
            raw = _loads(profile.get("raw_json"), {})
            text = raw.get("profile_text") or raw.get("resume_text") or raw.get("text") or ""
            if text:
                doc.add_paragraph(str(text))
        path = self._path("resumes", f"{identity.get('name')}-{position.get('job')}-结构化简历", "docx")
        doc.save(path)
        return {"summary": "结构化简历 DOCX 已生成。", "references": self._candidate_reference(candidate),
                "artifacts": [self._artifact("resume_document", "结构化简历", file_path=path, mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")]}

    def run_matching_report(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        relation, identity, position = candidate["relation"], candidate["identity"], candidate["position"]
        assessment = self._latest_assessment(int(relation["job_candidate_id"]))
        criteria = assessment.get("criteria") or {}
        status_icon = {"met": "通过", "partial": "部分", "not_met": "不通过", "unknown": "待核验"}
        hard = [[item.get("criterion"), "；".join(item.get("evidence") or []) or item.get("reason") or "无证据", status_icon.get(item.get("status"), "待核验")] for item in criteria.get("hard_requirements") or []]
        star_map = {"met": 5, "partial": 3, "not_met": 1, "unknown": 2}
        matches = [{"duty": item.get("criterion"), "evidence": "；".join(item.get("evidence") or []) or "待核验", "stars": star_map.get(item.get("status"), 2)} for item in criteria.get("core_abilities") or []]
        risks = [{"level": "高" if "关键" in str(value) else "中", "title": str(value), "description": str(value), "verify": "在下一次沟通中核验并记录证据"} for value in assessment.get("risks") or assessment.get("gaps") or []]
        data = {
            "candidate": identity.get("name"), "company": position.get("client"), "position": position.get("job"),
            "hard_gates": hard, "responsibility_matches": matches,
            "bonus_items": [[value, "来自 ASA 当前评估", "通过"] for value in (assessment.get("strengths") or [])],
            "risks": risks, "scores": {"综合匹配": int(assessment.get("fit_score") or 0)},
            "total_score": int(assessment.get("fit_score") or 0),
            "interview_suggestions": [["证据核验", value] for value in (assessment.get("verification_questions") or [])],
            "verdict": str(assessment.get("recommendation") or "待复核"), "conclusion_summary": str(assessment.get("next_action") or "人工复核"),
        }
        data_path = self._path("reports", f"{identity.get('name')}-matching-data", "json")
        output = self._path("reports", f"人岗匹配-{position.get('client')}-{position.get('job')}-{identity.get('name')}", "docx")
        data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._run([self.python, str(MATCHING_REPORT), "--data", f"@{data_path}", "--output", str(output)], 120)
        return {"summary": "人岗匹配分析报告已生成。", "references": self._candidate_reference(candidate),
                "artifacts": [self._artifact(
                    "matching_report", "人岗匹配分析报告", file_path=output,
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    metadata={
                        "assessment_id": assessment.get("id"), "job_candidate_id": relation.get("job_candidate_id"),
                        "person_id": relation.get("person_id"), "candidate_id": relation.get("source_candidate_id"),
                        "job_id": relation.get("job_id"), "client": position.get("client"), "job": position.get("job"),
                    },
                )]}

    def run_recommendation_report(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        relation, identity, position = candidate["relation"], candidate["identity"], candidate["position"]
        assessment = self._latest_assessment(int(relation["job_candidate_id"]))
        if float(assessment.get("evidence_coverage") or 0) < 0.75:
            return self._blocked("证据覆盖不足，不能生成可发送推荐报告。", ["核验问题完成", "证据覆盖率>=0.75"], self._candidate_reference(candidate))
        if not JIASHI_TEMPLATE.exists():
            return self._blocked("嘉驰标准模板不存在。", [str(JIASHI_TEMPLATE)], self._candidate_reference(candidate))
        profile_summary = str((candidate.get("candidate_profile") or {}).get("profile_summary") or "履历职责待进一步结构化核验")
        data = {
            "customer": position.get("client"), "position": position.get("job"), "name": identity.get("name"),
            "current_location": identity.get("city") or "不详", "expected_location": "不详",
            "consultant_comments": (assessment.get("strengths") or [])[:6],
            "education": [str(identity.get("education") or "不详")],
            "work_experience": [f"时间不详 {identity.get('company') or '公司待核验'}\n担任职位：{identity.get('title') or '职位待核验'}\n工作职责：{profile_summary}"],
            "project_experience": ["暂无明确项目经历"], "motivation": "待核验", "leaving_reason": "待核验",
        }
        data_path = self._path("reports", f"{identity.get('name')}-jiashi-data", "json")
        output = self._path("reports", f"嘉驰国际-{position.get('client')}-{position.get('job')}-{identity.get('name')}", "docx")
        data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._run([self.python, str(JIASHI_REPORT), "--template", str(JIASHI_TEMPLATE), "--data", str(data_path), "--output", str(output)], 180)
        self._sanitize_docx_privacy(output)
        audit = self._run([self.python, str(JIASHI_AUDIT), str(output)], 120)
        return {"summary": "嘉驰推荐报告草稿已生成并通过模板审计。", "references": self._candidate_reference(candidate),
                "artifacts": [self._artifact(
                    "recommendation_report", "嘉驰推荐报告", file_path=output,
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    content=audit.stdout[-4000:],
                    metadata={
                        "assessment_id": assessment.get("id"), "job_candidate_id": relation.get("job_candidate_id"),
                        "person_id": relation.get("person_id"), "candidate_id": relation.get("source_candidate_id"),
                        "job_id": relation.get("job_id"), "client": position.get("client"), "job": position.get("job"),
                        "attached_to_candidate": True, "external_submitted": False,
                    },
                )]}

    @staticmethod
    def _sanitize_docx_privacy(path: Path) -> None:
        from docx import Document
        doc = Document(path)
        private = re.compile(r"(?:手机号|手机号码|联系电话|电话|微信|WeChat|微信号|邮箱|Email|E-mail)|(?:\+?86[-\s]?)?1[3-9]\d{9}|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)
        for paragraph in doc.paragraphs:
            if private.search(paragraph.text or ""):
                paragraph.text = ""
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        if private.search(paragraph.text or ""):
                            paragraph.text = ""
        doc.save(path)

    def run_interview_followup(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return self._lifecycle_note(context, inputs, "interview_followup", "面试跟进", "interview_followup", "interview_note")

    def run_salary_negotiation(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return self._lifecycle_note(context, inputs, "salary_negotiation", "谈薪跟进", "salary_negotiation", "salary_negotiation_note")

    def run_decision_coaching(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return self._lifecycle_note(context, inputs, "decision_coaching", "候选人决策辅导", "decision_coaching", "decision_coaching")

    def run_onboarding_followup(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        return self._lifecycle_note(context, inputs, "onboarding_followup", "入职跟进", "onboarding_followup", "onboarding_note", days=7)

    def _lifecycle_note(self, context: dict[str, Any], inputs: dict[str, Any], event_type: str, label: str, task_type: str, artifact_type: str, days: int = 2) -> dict[str, Any]:
        candidate = self._candidate(context)
        objective = str(inputs.get("objective") or "").strip()
        if not objective:
            return self._blocked(f"{label}缺少业务事实。", ["objective"], self._candidate_reference(candidate))
        event_id = self._candidate_event(candidate, event_type, "recorded", objective, inputs)
        task_id = self._followup(candidate, task_type, objective, inputs, days)
        content = f"# {label}\n\n- 记录：{objective}\n- 事件 ID：{event_id}\n- 跟进任务 ID：{task_id or '未创建'}\n"
        return {"summary": f"{label}事实已记录，并创建后续任务。", "references": self._candidate_reference(candidate),
                "artifacts": [self._artifact(artifact_type, label, content=content, metadata={"event_id": event_id, "task_id": task_id})]}

    def run_salary_verification(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        data = inputs.get("salary_data") if isinstance(inputs.get("salary_data"), dict) else None
        if not data or not data.get("records"):
            return self._blocked("薪资核验需要结构化流水证据，未找到时不会生成虚假报告。", ["salary_data.records"], self._candidate_reference(candidate))
        data = {**data, "candidate_name": data.get("candidate_name") or candidate["identity"].get("name"), "report_date": data.get("report_date") or datetime.now().strftime("%Y-%m-%d")}
        data_path = self._path("salary", f"{candidate['identity'].get('name')}-salary-data", "json")
        out_dir = self.output_dir / "salary"
        data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        proc = self._run([self.python, str(SALARY_REPORT), "--input", str(data_path), "--output-dir", str(out_dir)], 180)
        files = [Path(line.strip()) for line in proc.stdout.splitlines() if line.strip().endswith(".docx")]
        return {"summary": f"薪资证据报告已生成，共 {len(files)} 个文件。", "references": self._candidate_reference(candidate),
                "artifacts": [self._artifact("salary_report", path.stem, file_path=path, mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document") for path in files]}

    @staticmethod
    def _message_hash(job_candidate_id: Any, message: str) -> str:
        raw = f"{job_candidate_id}|{message}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _outreach_message(self, candidate: dict[str, Any], inputs: dict[str, Any]) -> str:
        explicit = " ".join(str(inputs.get("message") or "").split())
        if explicit:
            return explicit
        identity, position = candidate["identity"], candidate["position"]
        return (
            f"{identity.get('name') or '你好'}，你好。我这边有一个{position.get('client') or ''}"
            f"{position.get('job') or '岗位'}机会，和你{identity.get('company') or ''}"
            f"{identity.get('title') or ''}经历比较相关，方便了解一下吗？"
        )

    def _outreach_targets(self, context: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, Any]]:
        context_type = str(context.get("type") or "")
        if context_type == "candidate":
            return [self._candidate(context)]
        filters = context.get("filters") if isinstance(context.get("filters"), dict) else {}
        queue = str(inputs.get("queue") or filters.get("queue") or "待联系")
        limit = max(1, min(int(inputs.get("limit") or filters.get("limit") or 20), 20))
        inbox = self.service.get_flow_inbox(queue=queue, limit=limit)
        targets: list[dict[str, Any]] = []
        for item in inbox.get("items") or []:
            job_candidate_id = item.get("job_candidate_id")
            if not job_candidate_id:
                continue
            try:
                targets.append(build_candidate_context(self.service.db_path, int(job_candidate_id)))
            except Exception:
                continue
        return targets

    def run_outreach_prepare(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        targets = self._outreach_targets(context, inputs)
        if not targets:
            return self._blocked("当前目标或队列没有可触达的人选。", ["选择当前目标、当前队列或具体人选"])
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate in targets[:20]:
            relation, identity, position = candidate["relation"], candidate["identity"], candidate["position"]
            if is_stopped(candidate):
                skipped.append({"job_candidate_id": relation["job_candidate_id"], "candidate": identity.get("name"), "reason": "关系已停止"})
                continue
            message = self._outreach_message(candidate, inputs)
            message_hash = self._message_hash(relation["job_candidate_id"], message)
            items.append({
                "job_candidate_id": relation["job_candidate_id"], "person_id": relation.get("person_id"),
                "job_id": relation.get("job_id"), "candidate": identity.get("name"),
                "company": identity.get("company"), "title": identity.get("title"),
                "client": position.get("client"), "job": position.get("job"), "channel": "猎聘职聊",
                "message": message, "message_hash": message_hash,
                "before": relation.get("clean_stage") or relation.get("raw_status") or "未触达",
                "after": "猎聘消息发送并读回后进入已触达/待回复",
                "status": "pending",
            })
        if not items:
            return self._blocked("本批人选均不可触达。", ["选择未停止且有明确人岗关系的人选"], [])
        payload = {
            "version": "1.0", "prepared_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "batch_limit": 20, "item_count": len(items), "items": items, "skipped": skipped,
        }
        path = self._path("outreach", "猎聘触达锁定草稿", "json")
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        references = [
            {"type": "candidate", "id": item["job_candidate_id"], "label": item["candidate"], "subtitle": f"{item['client']} / {item['job']}"}
            for item in items[:8]
        ]
        return {
            "summary": f"已锁定 {len(items)} 条猎聘触达草稿，正式发送前需要批量确认。",
            "references": references,
            "outreach_draft_batch": payload,
            "artifacts": [self._artifact(
                "outreach_draft_batch", "猎聘触达锁定草稿", file_path=path, mime_type="application/json",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"item_count": len(items), "batch_limit": 20},
            )],
        }

    def _sent_message_exists(self, job_candidate_id: int, message_hash: str) -> bool:
        conn = self.service._connect()
        try:
            row = conn.execute(
                """
                SELECT 1 FROM candidate_events
                WHERE job_candidate_id=? AND event_type='candidate_outreach'
                  AND event_status='sent_verified' AND raw_json LIKE ?
                LIMIT 1
                """,
                (int(job_candidate_id), f"%{message_hash}%"),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _load_outreach_batch(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        path = self._dependency_file(inputs, "outreach_draft_batch")
        if not path:
            return None
        return _loads(path.read_text(encoding="utf-8"), {})

    def run_outreach_execute(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        batch = self._load_outreach_batch(inputs)
        if not batch:
            return self._blocked("没有已锁定并通过审批的触达草稿。", ["outreach_draft_batch"])
        results: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        for item in (batch.get("items") or [])[:20]:
            job_candidate_id = int(item.get("job_candidate_id") or 0)
            message = str(item.get("message") or "").strip()
            message_hash = str(item.get("message_hash") or self._message_hash(job_candidate_id, message))
            if not job_candidate_id or not message:
                results.append({**item, "status": "failed", "error": "缺少人岗关系或文案"})
                continue
            if self._sent_message_exists(job_candidate_id, message_hash):
                results.append({**item, "status": "skipped", "reason": "同一锁定文案已发送并验证"})
                continue
            candidate = build_candidate_context(self.service.db_path, job_candidate_id)
            if is_stopped(candidate):
                results.append({**item, "status": "skipped", "reason": "关系已停止"})
                continue
            identity, position = candidate["identity"], candidate["position"]
            base = [
                self.python, str(LIEPIN_OUTREACH), "--port", str(int(inputs.get("cdp_port") or 9223)),
                "--candidate", str(identity.get("name")), "--message", message, "--check", str(position.get("job")),
            ]
            try:
                dry = self._run_json(base, 90)
                if dry.get("status") != "dry_run_ok":
                    raise RuntimeError("猎聘触达预检未通过")
                sent = self._run_json(base + ["--send"], 120)
                if sent.get("status") != "sent_verified":
                    raise RuntimeError("猎聘消息点击后未通过会话读回验证")
                event_id = self._candidate_event(
                    candidate, "candidate_outreach", "sent_verified", f"猎聘触达已验证：{message[:80]}",
                    {**inputs, "message_hash": message_hash, "locked_message": message, "channel_result": sent},
                )
                conn = self.service._connect()
                try:
                    conn.execute(
                        "UPDATE job_candidates SET raw_status='contacted',clean_stage='已触达',flow_bucket='已触达/待回复',updated_at=datetime('now','localtime') WHERE id=?",
                        (job_candidate_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                result_item = {**item, "status": "sent_verified", "event_id": event_id, "dry_run": dry, "receipt": sent}
            except Exception as exc:
                result_item = {**item, "status": "failed", "error": str(exc)[:1000]}
            results.append(result_item)
            references.append({"type": "candidate", "id": job_candidate_id, "label": item.get("candidate"), "subtitle": f"{item.get('client','')} / {item.get('job','')}"})
        success = [item for item in results if item.get("status") in {"sent_verified", "skipped"}]
        failed = [item for item in results if item.get("status") == "failed"]
        if not success and failed:
            raise RuntimeError("本批猎聘触达全部失败：" + "；".join(str(item.get("error") or "") for item in failed[:3]))
        payload = {
            "verified": bool(success), "batch_status": "partial_failed" if failed else "completed",
            "sent_count": len([item for item in results if item.get("status") == "sent_verified"]),
            "skipped_count": len([item for item in results if item.get("status") == "skipped"]),
            "failed_count": len(failed), "items": results,
        }
        return {
            "summary": f"猎聘触达完成：发送 {payload['sent_count']} 人，跳过 {payload['skipped_count']} 人，失败 {payload['failed_count']} 人。",
            "references": references[:8], "external_action_executed": True, "external_result": payload,
            "artifacts": [self._artifact(
                "external_action_receipt", "猎聘触达批量回执", content=json.dumps(payload, ensure_ascii=False, indent=2),
                metadata={"sent_count": payload["sent_count"], "failed_count": payload["failed_count"]},
            )],
        }

    def run_client_recommendation(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        report = self._dependency_file(inputs, "recommendation_report")
        if not report:
            return self._blocked("客户推荐缺少通过审计的推荐报告。", ["recommendation_report"], self._candidate_reference(candidate))
        request = {"candidate": candidate["identity"].get("name"), "client": candidate["position"].get("client"), "job": candidate["position"].get("job"), "report": str(report), "channel": inputs.get("channel") or "manual_client_channel"}
        return {"summary": "客户推荐材料已锁定，等待指定客户渠道完成发送并读回。", "references": self._candidate_reference(candidate),
                "external_action_executed": False, "external_request": request,
                "artifacts": [self._artifact("external_action_ticket", "客户推荐执行任务", content=json.dumps(request, ensure_ascii=False, indent=2), validation="pending_execution")]}

    def run_offer_confirmation(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        terms = inputs.get("offer_terms") if isinstance(inputs.get("offer_terms"), dict) else None
        if not terms:
            return self._blocked("Offer 确认缺少明确条件，不改变候选人阶段。", ["offer_terms"], self._candidate_reference(candidate))
        event_id = self._candidate_event(candidate, "offer_confirmation", "confirmed", "Offer 条件已人工确认", {**inputs, "offer_terms": terms})
        return {"summary": "Offer 条件已记录；未代表候选人已经接受。", "references": self._candidate_reference(candidate), "external_action_executed": True,
                "artifacts": [self._artifact("offer_confirmation", "Offer 条件确认", content=json.dumps(terms, ensure_ascii=False, indent=2), metadata={"event_id": event_id})]}

    def run_project_retrospective(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        if context.get("type") == "candidate":
            candidate = self._candidate(context)
            refs = self._candidate_reference(candidate)
            relation_id = candidate["relation"]["job_candidate_id"]
            conn = self.service._connect()
            try:
                events = [_row(row) for row in conn.execute("SELECT event_type,event_status,event_time,summary FROM candidate_events WHERE job_candidate_id=? ORDER BY event_time,id", (relation_id,)).fetchall()]
            finally:
                conn.close()
            content = "# 项目复盘\n\n" + "\n".join(f"- {item.get('event_time') or ''} · {item.get('event_type')} · {item.get('summary') or ''}" for item in events)
        else:
            job = self._job(context)
            refs = self._job_reference(job)
            conn = self.service._connect()
            try:
                counts = {row["clean_stage"] or "未整理": int(row["total"]) for row in conn.execute("SELECT clean_stage,COUNT(*) total FROM job_candidates WHERE job_id=? GROUP BY clean_stage", (job["id"],)).fetchall()}
            finally:
                conn.close()
            content = "# 项目复盘\n\n```json\n" + json.dumps(counts, ensure_ascii=False, indent=2) + "\n```"
        return {"summary": "已基于 v3 事件与漏斗生成项目复盘。", "references": refs,
                "artifacts": [self._artifact("project_retrospective", "项目复盘", content=content)]}

    def run_memory_capture(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        content = str(inputs.get("confirmed_memory") or "").strip()
        if not content:
            return self._blocked("长期记忆只沉淀明确确认的信息。", ["confirmed_memory"])
        scope = context.get("type") if context.get("type") in {"job", "candidate"} else "global"
        result = self.service.store_memory(scope, str(context.get("id") or ""), "workflow_outcome", content, "agent_workflow", str(inputs.get("workflow_id") or ""), 1.0)
        return {"summary": "经确认的业务经验已写入 ASA 长期记忆。", "memory": result}

    def run_identity_merge_preflight(self, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(context)
        other_id = inputs.get("other_job_candidate_id")
        if not other_id:
            return self._blocked("身份合并预检需要另一条候选人关系。", ["other_job_candidate_id"], self._candidate_reference(candidate))
        other = build_candidate_context(self.service.db_path, int(other_id))
        comparison = {"left": candidate["identity"], "right": other["identity"], "same_name": candidate["identity"].get("name") == other["identity"].get("name"), "same_company": candidate["identity"].get("company") == other["identity"].get("company"), "same_title": candidate["identity"].get("title") == other["identity"].get("title")}
        comparison["allowed"] = comparison["same_name"] and (comparison["same_company"] or comparison["same_title"])
        return {"summary": "身份对比完成；该步骤不会执行合并。", "comparison": comparison,
                "references": self._candidate_reference(candidate) + self._candidate_reference(other),
                "artifacts": [self._artifact("identity_comparison", "候选人身份对比", content=json.dumps(comparison, ensure_ascii=False, indent=2), validation="passed" if comparison["allowed"] else "blocked")]}
