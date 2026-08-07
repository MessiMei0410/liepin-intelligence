#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from run_published_position_search import (  # noqa: E402
    build_db_position_profile,
    capture_resume_details,
    score_candidate_for_profile,
)
from xsaas_candidate_search import (  # noqa: E402
    CDP,
    capture_candidate_details,
    choose_authenticated_tab,
    clone_authenticated_tab,
    load_json,
    wait_for_list,
)

DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OPENCLI = Path("/Users/messi/.hermes/node/bin/opencli")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def section_text(value: Any) -> str:
    return "\n".join(line.strip() for line in str(value or "").replace("\r", "\n").split("\n") if line.strip())


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("baseline file must be an array or an object with a candidates array")
    return [item for item in values if isinstance(item, dict)]


def normalize_candidate(channel: str, item: dict[str, Any]) -> dict[str, Any]:
    candidate_id = clean_text(
        item.get("candidateId") or item.get("xsaas_id") or item.get("candidate_id")
        or item.get("resumeId") or item.get("res_id_encode")
    )
    missing = item.get("resumeCaptureMissing") or item.get("resume_capture_missing") or []
    if not isinstance(missing, list):
        missing = [clean_text(missing)] if clean_text(missing) else []
    return {
        "candidate_id": candidate_id,
        "resume_id": clean_text(item.get("resumeId") or item.get("res_id_encode")),
        "name": clean_text(item.get("name")),
        "company": clean_text(item.get("company") or item.get("currentCompany") or item.get("current_company")),
        "title": clean_text(item.get("title") or item.get("currentTitle") or item.get("current_title")),
        "experience": clean_text(item.get("experience")),
        "education": clean_text(item.get("education")),
        "city": clean_text(item.get("city")),
        "profile_text": section_text(item.get("profileText") or item.get("profile_text") or item.get("raw_text")),
        "full_text": section_text(item.get("fullText") or item.get("full_text")),
        "work_text": section_text(item.get("workText") or item.get("work_text")),
        "project_text": section_text(item.get("projectText") or item.get("project_text")),
        "education_text": section_text(item.get("educationText") or item.get("education_text")),
        "url": clean_text(item.get("url") or item.get("source_url") or item.get("resume_url")),
        "query": clean_text(item.get("query") or item.get("source_query")),
        "data_stage": clean_text(item.get("dataStage") or item.get("data_stage") or "recall"),
        "resume_capture_status": clean_text(
            item.get("resumeCaptureStatus") or item.get("resume_capture_status") or "not_requested"
        ),
        "resume_capture_missing": [clean_text(value) for value in missing if clean_text(value)],
        "resume_capture_error": clean_text(item.get("resumeCaptureError") or item.get("resume_capture_error")),
        "resume_captured_at": clean_text(item.get("resumeCapturedAt") or item.get("resume_captured_at")),
        "score": int(float(item.get("fit_score", item.get("score", 0)))) if item.get("fit_score") is not None or item.get("score") is not None else None,
    }


def candidate_key(channel: str, item: dict[str, Any]) -> str:
    source_id = clean_text(
        item.get("candidate_id") or item.get("resume_id") or item.get("res_id_encode")
        or item.get("xsaas_id")
    )
    if source_id:
        return f"id:{source_id}"
    identity = "|".join(clean_text(item.get(key)).casefold() for key in ("name", "company", "title"))
    return f"identity:{identity}"


def filter_baseline(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    return [item for item in rows if item.get("query") == query]


def select_baseline(channel: str, path: Path, query: str) -> list[dict[str, Any]]:
    rows = [normalize_candidate(channel, item) for item in load_candidates(path)]
    return filter_baseline(rows, query)


def query_text(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("query", "keyword", "text", "q"):
            value = clean_text(entry.get(key))
            if value:
                return value
        return ""
    return clean_text(entry)


def load_queries(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return []
    return [text for text in (query_text(entry) for entry in entries) if text]


def choose_sample_query(
    rows: list[dict[str, Any]], queries: list[str], fallback_query: str
) -> tuple[str, dict[str, Any]]:
    """优先采样基线非空的 query（全空时回退第一词），让跨工作流对比不再是空对空。"""
    counts = [len(filter_baseline(rows, query)) for query in queries]
    for index, count in enumerate(counts):
        if count > 0:
            return queries[index], {
                "sample_policy": "first_nonempty_baseline_else_first",
                "sampled_query_index": index,
                "queries_total": len(queries),
                "baseline_counts_per_query": counts,
                "fallback_query_used": False,
            }
    return (queries[0] if queries else fallback_query), {
        "sample_policy": "first_nonempty_baseline_else_first",
        "sampled_query_index": 0,
        "queries_total": len(queries),
        "baseline_counts_per_query": counts,
        "fallback_query_used": True,
    }


def opencli_endpoint(channel: str, port: int) -> tuple[str, str]:
    tabs = load_json(f"http://127.0.0.1:{port}/json/list")
    if channel == "liepin":
        tab = next(
            (
                item for item in tabs
                if item.get("type") == "page"
                and "h.liepin.com/search/getConditionItem" in str(item.get("url") or "")
            ),
            None,
        )
        if not tab or not tab.get("webSocketDebuggerUrl"):
            raise RuntimeError("LIEPIN_LOGIN_REQUIRED: no signed-in Liepin search tab")
        if not liepin_tab_is_authenticated(tab):
            raise RuntimeError("LIEPIN_LOGIN_REQUIRED: Liepin search page is no longer authenticated")
        return str(tab["webSocketDebuggerUrl"]), ""

    source = choose_authenticated_tab(port)
    target, target_id = clone_authenticated_tab(port, source)
    try:
        wait_for_list(target)
        target_tab = next(
            (item for item in load_json(f"http://127.0.0.1:{port}/json/list") if str(item.get("id")) == target_id),
            None,
        )
        if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
            raise RuntimeError("X-SaaS shadow tab has no CDP endpoint")
        return str(target_tab["webSocketDebuggerUrl"]), target_id
    finally:
        target.close()


def liepin_tab_is_authenticated(tab: dict[str, Any]) -> bool:
    """Reject login pages before OpenCLI can misclassify them as empty results."""
    endpoint = str(tab.get("webSocketDebuggerUrl") or "")
    if not endpoint:
        return False
    cdp = CDP(endpoint)
    try:
        probe = cdp.send(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({href:location.href,title:document.title,body:(document.body?.innerText||'').slice(0,1200)})",
                "returnByValue": True,
            },
        )
    finally:
        cdp.close()
    value = ((probe or {}).get("result") or {}).get("result", {}).get("value", "")
    try:
        state = json.loads(value) if isinstance(value, str) else {}
    except json.JSONDecodeError:
        state = {}
    href = str(state.get("href") or tab.get("url") or "").lower()
    title = str(state.get("title") or tab.get("title") or "")
    body = str(state.get("body") or "")
    login_markers = ("/login", "passport", "登录猎聘", "扫码登录", "密码登录", "手机号登录")
    return not any(marker in href or marker in title or marker in body for marker in login_markers)


def close_target(port: int, target_id: str) -> None:
    if not target_id:
        return
    version = load_json(f"http://127.0.0.1:{port}/json/version")
    browser = CDP(version["webSocketDebuggerUrl"])
    try:
        browser.send("Target.closeTarget", {"targetId": target_id})
    finally:
        browser.close()


def capture_opencli_details(
    channel: str,
    rows: list[dict[str, Any]],
    port: int,
    endpoint: str,
    *,
    detail_min_delay: float = 2.5,
    detail_max_delay: float = 5.5,
    detail_burst_size: int = 6,
    detail_burst_cooldown: float = 15.0,
) -> dict[str, Any]:
    if not rows:
        return {"requested": 0, "complete": 0, "partial": 0, "failed": 0, "status": "completed_empty"}

    for item in rows:
        item["recall_profile_text"] = item.get("profile_text") or ""
        if channel == "liepin":
            item.update({
                "res_id_encode": item.get("resume_id") or item.get("candidate_id") or "",
                "current_company": item.get("company") or "",
                "current_title": item.get("title") or "",
                "raw_text": item.get("profile_text") or "",
                "resume_url": item.get("url") or "",
            })
        else:
            item.update({
                "xsaas_id": item.get("candidate_id") or "",
                "source_url": item.get("url") or "",
            })

    try:
        if channel == "liepin":
            stats: dict[str, Any] = capture_resume_details(
                port,
                rows,
                len(rows),
                min_delay=detail_min_delay,
                max_delay=detail_max_delay,
                burst_size=detail_burst_size,
                burst_cooldown=detail_burst_cooldown,
            )
        else:
            detail_cdp = CDP(endpoint)
            try:
                stats = capture_candidate_details(detail_cdp, rows, True)
            finally:
                detail_cdp.close()
    except Exception as exc:
        for item in rows:
            item.update({
                "resume_capture_status": "failed",
                "resume_capture_missing": ["完整履历"],
                "resume_capture_error": "OpenCLI 详情抓取器不可用",
            })
        return {
            "requested": len(rows), "complete": 0, "partial": 0, "failed": len(rows),
            "status": "failed", "error_type": type(exc).__name__,
        }

    for item in rows:
        item["url"] = clean_text(item.get("resume_url") or item.get("source_url") or item.get("url"))
        item["data_stage"] = "detail" if item.get("resume_capture_status") in {"complete", "partial"} else "recall"
    return {**stats, "status": "completed"}


def run_opencli(
    channel: str, query: str, limit: int, port: int, opencli_bin: Path,
    capture_details: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    endpoint, target_id = opencli_endpoint(channel, port)
    started = time.perf_counter()
    try:
        env = os.environ.copy()
        env["OPENCLI_CDP_ENDPOINT"] = endpoint
        env["PATH"] = str(opencli_bin.parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        proc = subprocess.run(
            [
                str(opencli_bin), channel, "candidate-search", query,
                "--limit", str(limit), "-f", "json",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        duration_ms = round((time.perf_counter() - started) * 1000)
        empty = proc.returncode == 66 or "EMPTY_RESULT" in (proc.stderr or "")
        if proc.returncode != 0 and not empty:
            raise RuntimeError(f"OpenCLI {channel} adapter failed with exit code {proc.returncode}")
        try:
            raw = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else []
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenCLI {channel} adapter returned invalid JSON") from exc
        if not isinstance(raw, list):
            raise RuntimeError("OpenCLI candidate-search did not return a JSON array")
        rows = [normalize_candidate(channel, item) for item in raw if isinstance(item, dict)]
        detail_started = time.perf_counter()
        detail_capture = (
            capture_opencli_details(channel, rows, port, endpoint)
            if capture_details
            else {"requested": 0, "status": "deferred_for_cross_query_dedupe"}
        )
        detail_duration_ms = round((time.perf_counter() - detail_started) * 1000)
        result_count = raw[0].get("resultCount") if raw and isinstance(raw[0], dict) else None
        if rows and not result_count:
            result_count = None
        diagnostics = {
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "adapter_duration_ms": duration_ms,
            "detail_duration_ms": detail_duration_ms,
            "status": "completed_empty" if empty else "completed",
            "result_count": result_count,
            "detail_capture": detail_capture,
        }
        return rows, diagnostics
    finally:
        close_target(port, target_id)


def apply_liepin_score_gate(
    rows: list[dict[str, Any]], db_path: Path, client: str, job: str, min_score: int,
) -> list[dict[str, Any]]:
    profile = None
    accepted = []
    for item in rows:
        score = item.get("score")
        if score is None:
            if profile is None:
                profile = build_db_position_profile(str(db_path), client, job)
            card = {
                "name": item.get("name"),
                "current_company": item.get("company"),
                "current_title": item.get("title"),
                "experience": item.get("experience"),
                "education": item.get("education"),
                "city": item.get("city"),
                "raw_text": item.get("full_text") or item.get("profile_text") or item.get("recall_profile_text"),
                "skills": [],
                "work": [],
            }
            score, _, _, _ = score_candidate_for_profile(card, profile.default_city, profile)
        if int(score) >= min_score:
            accepted.append(item)
    return accepted


apply_position_score_gate = apply_liepin_score_gate


def capture_primary_details(
    channel: str,
    rows: list[dict[str, Any]],
    detail_limit: int,
    port: int,
    *,
    detail_min_delay: float = 2.5,
    detail_max_delay: float = 5.5,
    detail_burst_size: int = 6,
    detail_burst_cooldown: float = 15.0,
) -> dict[str, Any]:
    selected = rows[: max(0, detail_limit)]
    pending = [
        item for item in selected
        if item.get("resume_capture_status") not in {"complete", "partial", "failed"}
    ]
    if pending:
        endpoint, target_id = opencli_endpoint(channel, port)
        try:
            capture_opencli_details(
                channel,
                pending,
                port,
                endpoint,
                detail_min_delay=detail_min_delay,
                detail_max_delay=detail_max_delay,
                detail_burst_size=detail_burst_size,
                detail_burst_cooldown=detail_burst_cooldown,
            )
        finally:
            close_target(port, target_id)
    counts = {
        status: sum(item.get("resume_capture_status") == status for item in selected)
        for status in ("complete", "partial", "failed")
    }
    return {
        "requested": len(selected),
        **counts,
        "skipped_by_limit": max(0, len(rows) - len(selected)),
        "status": "completed" if selected else "completed_empty",
    }


def run_primary_recall(
    channel: str,
    queries: list[str],
    limit: int,
    port: int,
    opencli_bin: Path,
    db: Path,
    client: str,
    job: str,
    min_score: int,
    max_queries: int,
    detail_limit: int,
    detail_min_delay: float = 2.5,
    detail_max_delay: float = 5.5,
    detail_burst_size: int = 6,
    detail_burst_cooldown: float = 15.0,
) -> dict[str, Any]:
    """OpenCLI 默认主渠道召回：多词召回 + 生产同款分数门/详情采集/完整度标准。

    只负责召回与详情采集；入库、去重、归因、审计仍由 ASA 既有链路完成。
    rows 写出到调用方指定文件；返回的摘要只含计数与失败原因，不含候选人明文。
    """
    selected = [query for query in (clean_text(value) for value in queries) if query][: max(1, max_queries)]
    blocked: list[dict[str, str]] = []
    merged: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    succeeded = 0
    for query in selected:
        try:
            rows, diagnostics = run_opencli(channel, query, limit, port, opencli_bin, False)
        except Exception as exc:
            blocked.append({"query": query, "error": str(exc)[-300:]})
            rounds.append({
                "query": query,
                "status": "failed",
                "result_count": None,
                "extracted_count": 0,
                "pages_fetched": 0,
                "terminal_state": "failed",
                "terminal_reason": "opencli_query_failed",
            })
            continue
        succeeded += 1
        for position_index, item in enumerate(rows, 1):
            item["query"] = query
            item["channel"] = channel
            item["page_number"] = 1
            item["position_index"] = position_index
        merged.extend(rows)
        raw_reported = diagnostics.get("result_count") if isinstance(diagnostics, dict) else None
        try:
            reported_total = int(raw_reported) if raw_reported is not None else None
        except (TypeError, ValueError):
            reported_total = None
        if reported_total is None:
            terminal_state, terminal_reason = "platform_capped", "opencli_reported_total_unknown"
        elif len(rows) >= reported_total:
            terminal_state, terminal_reason = "exhausted", "reported_total_exhausted"
        else:
            terminal_state, terminal_reason = "platform_capped", "opencli_limit_below_reported_total"
        rounds.append({
            "query": query,
            "status": "completed",
            "result_count": reported_total,
            "extracted_count": len(rows),
            "unique_count": len({candidate_key(channel, item) for item in rows}),
            "pages_fetched": 1,
            "cursor": {"page": 2} if terminal_state == "platform_capped" and rows else None,
            "terminal_state": terminal_state,
            "terminal_reason": terminal_reason,
        })
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in merged:
        key = candidate_key(channel, item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    detail_capture = capture_primary_details(
        channel,
        deduped,
        detail_limit,
        port,
        detail_min_delay=detail_min_delay,
        detail_max_delay=detail_max_delay,
        detail_burst_size=detail_burst_size,
        detail_burst_cooldown=detail_burst_cooldown,
    )
    gated = apply_position_score_gate(deduped, db, client, job, min_score)
    accepted = gated
    complete = sum(1 for item in accepted if item.get("resume_capture_status") == "complete")
    all_exhausted = bool(rounds) and all(item.get("terminal_state") == "exhausted" for item in rounds)
    return {
        # Keep intake readiness separate from coverage completion. A query can be
        # exhausted even when every recalled card is low-score or detail capture failed.
        "ok": complete > 0 and all_exhausted,
        "coverage_complete": all_exhausted,
        "intake_ready": complete > 0,
        "mode": "opencli_primary_recall",
        "channel": channel,
        "queries_attempted": len(selected),
        "queries_succeeded": succeeded,
        "rows_recalled": len(merged),
        "rows_after_dedupe": len(deduped),
        "rows_after_gate": len(gated),
        "rows_written": len(accepted),
        "rows_complete": complete,
        "detail_capture": detail_capture,
        "blocked": blocked,
        "rounds": rounds,
        "rows": accepted,
        "raw_rows": merged,
    }


def completeness(channel: str, rows: list[dict[str, Any]]) -> float:
    fields = ("candidate_id", "name", "company", "title", "url") if channel == "xsaas" else (
        "candidate_id", "name", "company", "title", "experience", "education", "city", "url",
    )
    total = len(rows) * len(fields)
    filled = sum(bool(clean_text(item.get(field))) for item in rows for field in fields)
    return round(filled / total, 4) if total else 0.0


def resume_completeness(rows: list[dict[str, Any]]) -> float:
    fields = ("full_text", "work_text", "education_text")
    total = len(rows) * len(fields)
    filled = sum(bool(section_text(item.get(field))) for item in rows for field in fields)
    return round(filled / total, 4) if total else 0.0


def capture_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        status: sum(1 for item in rows if item.get("resume_capture_status") == status)
        for status in ("complete", "partial", "failed", "not_requested")
    }


def compare(channel: str, baseline: list[dict[str, Any]], shadow: list[dict[str, Any]]) -> dict[str, Any]:
    baseline_keys = {candidate_key(channel, item) for item in baseline}
    shadow_keys = {candidate_key(channel, item) for item in shadow}
    union = baseline_keys | shadow_keys
    return {
        "baseline_count": len(baseline_keys),
        "shadow_count": len(shadow_keys),
        "overlap": len(baseline_keys & shadow_keys),
        "baseline_only": len(baseline_keys - shadow_keys),
        "shadow_only": len(shadow_keys - baseline_keys),
        "baseline_relative_recall": round(len(baseline_keys) / len(union), 4) if union else 0.0,
        "shadow_relative_recall": round(len(shadow_keys) / len(union), 4) if union else 0.0,
        "baseline_completeness": completeness(channel, baseline),
        "shadow_completeness": completeness(channel, shadow),
        "baseline_resume_completeness": resume_completeness(baseline),
        "shadow_resume_completeness": resume_completeness(shadow),
        "baseline_capture": capture_counts(baseline),
        "shadow_capture": capture_counts(shadow),
        "baseline_only_keys": sorted(hashed(value) for value in baseline_keys - shadow_keys),
        "shadow_only_keys": sorted(hashed(value) for value in shadow_keys - baseline_keys),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ASA non-blocking read-only OpenCLI sourcing shadow")
    parser.add_argument("--mode", choices=("shadow", "primary"), default="shadow")
    parser.add_argument("--channel", choices=("liepin", "xsaas"), required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--queries-json", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--raw-output", type=Path, default=None)
    parser.add_argument("--detail-limit", type=int, default=24)
    parser.add_argument("--detail-min-delay", type=float, default=2.5)
    parser.add_argument("--detail-max-delay", type=float, default=5.5)
    parser.add_argument("--detail-burst-size", type=int, default=6)
    parser.add_argument("--detail-burst-cooldown", type=float, default=15.0)
    parser.add_argument("--max-queries", type=int, default=3)
    parser.add_argument("--client", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--opencli-bin", type=Path, default=DEFAULT_OPENCLI)
    args = parser.parse_args()
    if not 1 <= args.limit <= (24 if args.channel == "liepin" else 100):
        parser.error("limit is outside the adapter range")
    if not args.opencli_bin.exists():
        raise SystemExit(f"OpenCLI not found: {args.opencli_bin}")

    if args.mode == "primary":
        queries = load_queries(args.queries_json) if args.queries_json else []
        if not queries and clean_text(args.query):
            queries = [clean_text(args.query)]
        if not queries:
            parser.error("--mode primary 需要 --queries-json 或 --query")
        if args.output is None:
            parser.error("--mode primary 需要 --output")
        summary = run_primary_recall(
            args.channel, queries, args.limit, args.port, args.opencli_bin,
            args.db, args.client, args.job, args.min_score, args.max_queries, args.detail_limit,
            args.detail_min_delay, args.detail_max_delay, args.detail_burst_size, args.detail_burst_cooldown,
        )
        rows = summary.pop("rows")
        raw_rows = summary.pop("raw_rows")
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.raw_output is not None:
            args.raw_output.write_text(json.dumps(raw_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["output"] = str(args.output)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if args.baseline is None:
        parser.error("--mode shadow 需要 --baseline")
    baseline_rows = [normalize_candidate(args.channel, item) for item in load_candidates(args.baseline)]
    if args.queries_json is not None:
        queries = load_queries(args.queries_json)
        query, sample_meta = choose_sample_query(baseline_rows, queries, clean_text(args.query))
    else:
        query = clean_text(args.query)
        sample_meta = {"sample_policy": "explicit_query"}
    if not query:
        parser.error("--query 为空且 --queries-json 无可用查询词")
    baseline = filter_baseline(baseline_rows, query)
    shadow, diagnostics = run_opencli(args.channel, query, args.limit, args.port, args.opencli_bin)
    shadow_raw_keys = {candidate_key(args.channel, item) for item in shadow}
    if args.channel == "liepin":
        shadow = apply_liepin_score_gate(shadow, args.db, args.client, args.job, args.min_score)
    comparison = compare(args.channel, baseline, shadow)
    # 分数门/基线过滤前的原始计数：0/0 对比才能区分"召回为空"与"被分数线筛空"
    comparison["baseline_file_count"] = len({candidate_key(args.channel, item) for item in baseline_rows})
    comparison["shadow_raw_count"] = len(shadow_raw_keys)
    comparison["shadow_gated_out"] = len(shadow_raw_keys) - comparison["shadow_count"]
    result = {
        "ok": True,
        "mode": "read_only_shadow",
        "affects_intake": False,
        "affects_outreach": False,
        "channel": args.channel,
        "query": query,
        **sample_meta,
        "diagnostics": diagnostics,
        "comparison": comparison,
        "action_migration_eligible": False,
        "migration_reason": "Shadow samples require aggregate cross-workflow evidence before any action pilot.",
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
