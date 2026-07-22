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
    }


def candidate_key(channel: str, item: dict[str, Any]) -> str:
    if channel == "xsaas" and item.get("candidate_id"):
        return f"id:{item['candidate_id']}"
    identity = "|".join(clean_text(item.get(key)).casefold() for key in ("name", "company", "title"))
    return f"identity:{identity}"


def select_baseline(channel: str, path: Path, query: str) -> list[dict[str, Any]]:
    rows = [normalize_candidate(channel, item) for item in load_candidates(path)]
    return [item for item in rows if item.get("query") == query]


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
    channel: str, rows: list[dict[str, Any]], port: int, endpoint: str,
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
            stats: dict[str, Any] = capture_resume_details(port, rows, len(rows))
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
        detail_capture = capture_opencli_details(channel, rows, port, endpoint)
        detail_duration_ms = round((time.perf_counter() - detail_started) * 1000)
        diagnostics = {
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "adapter_duration_ms": duration_ms,
            "detail_duration_ms": detail_duration_ms,
            "status": "completed_empty" if empty else "completed",
            "result_count": raw[0].get("resultCount") if raw and isinstance(raw[0], dict) else 0,
            "detail_capture": detail_capture,
        }
        return rows, diagnostics
    finally:
        close_target(port, target_id)


def apply_liepin_score_gate(
    rows: list[dict[str, Any]], db_path: Path, client: str, job: str, min_score: int,
) -> list[dict[str, Any]]:
    profile = build_db_position_profile(str(db_path), client, job)
    accepted = []
    for item in rows:
        card = {
            "name": item.get("name"),
            "current_company": item.get("company"),
            "current_title": item.get("title"),
            "experience": item.get("experience"),
            "education": item.get("education"),
            "city": item.get("city"),
            "raw_text": item.get("recall_profile_text") or item.get("profile_text"),
            "skills": [],
            "work": [],
        }
        score, _, _, _ = score_candidate_for_profile(card, None, profile)
        if score >= min_score:
            accepted.append(item)
    return accepted


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
    parser.add_argument("--channel", choices=("liepin", "xsaas"), required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
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

    query = clean_text(args.query)
    baseline = select_baseline(args.channel, args.baseline, query)
    shadow, diagnostics = run_opencli(args.channel, query, args.limit, args.port, args.opencli_bin)
    if args.channel == "liepin":
        shadow = apply_liepin_score_gate(shadow, args.db, args.client, args.job, args.min_score)
    result = {
        "ok": True,
        "mode": "read_only_shadow",
        "affects_intake": False,
        "affects_outreach": False,
        "channel": args.channel,
        "query": query,
        "diagnostics": diagnostics,
        "comparison": compare(args.channel, baseline, shadow),
        "action_migration_eligible": False,
        "migration_reason": "Shadow samples require aggregate cross-workflow evidence before any action pilot.",
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
