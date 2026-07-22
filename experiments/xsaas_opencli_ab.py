#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Any

ASA_ROOT = Path(__file__).resolve().parents[1]
LIEPIN_ROOT = Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence")
XSAAS_RUNNER = LIEPIN_ROOT / "scripts" / "xsaas_candidate_search.py"
OPENCLI_BIN = Path("/Users/messi/.hermes/node/bin/opencli")
XSAAS_HOST = "headhunt.x-saas.com.cn"
REQUIRED_FIELDS = ("candidate_id", "name", "company", "title", "url")

sys.path.insert(0, str(LIEPIN_ROOT / "scripts"))
from xsaas_candidate_search import (  # noqa: E402
    CDP,
    choose_authenticated_tab,
    clone_authenticated_tab,
    evaluate,
    load_json,
    wait_for_list,
)


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def candidate_key(candidate: dict[str, Any]) -> str:
    candidate_id = clean_text(candidate.get("candidate_id"))
    if candidate_id:
        return f"id:{candidate_id}"
    identity = "|".join(
        clean_text(candidate.get(key)).casefold() for key in ("name", "company", "title")
    )
    return f"identity:{identity}"


def public_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def normalize_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": clean_text(item.get("candidate_id") or item.get("candidateId") or item.get("xsaas_id")),
        "name": clean_text(item.get("name")),
        "company": clean_text(item.get("company")),
        "title": clean_text(item.get("title")),
        "profile_text": clean_text(item.get("profile_text") or item.get("profileText")),
        "full_text": clean_text(item.get("full_text") or item.get("fullText")),
        "work_text": clean_text(item.get("work_text") or item.get("workText")),
        "project_text": clean_text(item.get("project_text") or item.get("projectText")),
        "education_text": clean_text(item.get("education_text") or item.get("educationText")),
        "url": clean_text(item.get("url") or item.get("source_url")),
        "data_stage": clean_text(item.get("dataStage") or item.get("data_stage")),
        "resume_capture_status": clean_text(item.get("resumeCaptureStatus") or item.get("resume_capture_status")),
        "query": clean_text(item.get("query")),
    }


def create_xsaas_experiment_target(source_port: int) -> tuple[str, str, dict[str, Any]]:
    """Create an isolated authenticated tab for OpenCLI and return its endpoint."""
    source_tab = choose_authenticated_tab(source_port)
    target, target_id = clone_authenticated_tab(source_port, source_tab)
    try:
        wait_for_list(target)
        tabs = load_json(f"http://127.0.0.1:{source_port}/json/list")
        target_tab = next((tab for tab in tabs if str(tab.get("id") or "") == target_id), None)
        if not target_tab or not target_tab.get("webSocketDebuggerUrl"):
            raise RuntimeError("The isolated OpenCLI X-SaaS tab has no CDP endpoint")
        return str(target_tab["webSocketDebuggerUrl"]), target_id, {
            "ok": True,
            "mode": "isolated_authenticated_tab",
            "source_port": source_port,
            "target_id_hash": public_key(target_id),
        }
    finally:
        target.close()


def close_xsaas_experiment_target(port: int, target_id: str) -> None:
    version = load_json(f"http://127.0.0.1:{port}/json/version")
    browser = CDP(version["webSocketDebuggerUrl"])
    try:
        browser.send("Target.closeTarget", {"targetId": target_id})
    finally:
        browser.close()


def run_process(
    command: list[str], timeout: int, env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    started = time.perf_counter()
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return proc, elapsed_ms


def run_baseline(query: str, limit: int, source_port: int, temp_dir: Path) -> dict[str, Any]:
    token = hashlib.sha256(f"{query}-{time.time_ns()}".encode()).hexdigest()[:10]
    queries_path = temp_dir / f"baseline-{token}-queries.json"
    output_path = temp_dir / f"baseline-{token}-candidates.json"
    queries_path.write_text(json.dumps({"queries": [query]}, ensure_ascii=False), encoding="utf-8")
    proc, elapsed_ms = run_process(
        [
            sys.executable,
            str(XSAAS_RUNNER),
            "--queries", str(queries_path),
            "--output", str(output_path),
            "--port", str(source_port),
            "--max-rows", str(limit),
        ],
        timeout=180,
    )
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    raw_candidates = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else []
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    round_status = clean_text(rounds[0].get("status")) if rounds and isinstance(rounds[0], dict) else ""
    round_diagnostics = {
        key: rounds[0].get(key)
        for key in ("selected_query", "result_count", "extracted_count")
        if rounds and isinstance(rounds[0], dict) and key in rounds[0]
    }
    success = proc.returncode == 0 and round_status == "completed"
    return {
        "engine": "baseline_cdp",
        "query": query,
        "success": success,
        "status": round_status or ("completed" if proc.returncode == 0 else "failed"),
        "duration_ms": elapsed_ms,
        "candidates": [normalize_candidate(item) for item in raw_candidates if isinstance(item, dict)],
        "diagnostics": round_diagnostics,
        "error": clean_text(proc.stderr)[-1000:] if proc.returncode else "",
    }


def run_opencli(query: str, limit: int, cdp_endpoint: str) -> dict[str, Any]:
    child_env = os.environ.copy()
    child_env["OPENCLI_CDP_ENDPOINT"] = cdp_endpoint
    proc, elapsed_ms = run_process(
        [
            str(OPENCLI_BIN), "xsaas", "candidate-search", query,
            "--limit", str(limit), "-f", "json",
            "--trace", "retain-on-failure", "--window", "background",
        ],
        timeout=120,
        env=child_env,
    )
    raw_candidates: list[dict[str, Any]] = []
    if proc.returncode == 0:
        try:
            parsed = json.loads(proc.stdout)
            raw_candidates = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            pass
    empty_result = proc.returncode == 66 or "EMPTY_RESULT" in (proc.stderr or "")
    success = proc.returncode == 0 or empty_result
    diagnostics = {}
    if raw_candidates:
        diagnostics = {
            "selected_query": raw_candidates[0].get("selectedQuery"),
            "result_count": raw_candidates[0].get("resultCount"),
            "extracted_count": len(raw_candidates),
        }
    return {
        "engine": "opencli",
        "query": query,
        "success": success,
        "status": "completed_empty" if empty_result else "completed" if success else "failed",
        "duration_ms": elapsed_ms,
        "candidates": [normalize_candidate(item) for item in raw_candidates if isinstance(item, dict)],
        "diagnostics": diagnostics,
        "error": clean_text(proc.stderr or proc.stdout)[-1000:] if not success else "",
    }


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return 1.0 if not union else len(left & right) / len(union)


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [run for run in runs if run.get("success")]
    candidate_sets = [
        {candidate_key(item) for item in run.get("candidates", [])}
        for run in successful
    ]
    all_candidates = [item for run in successful for item in run.get("candidates", [])]
    unique = {candidate_key(item) for item in all_candidates}
    pairs = [
        jaccard(candidate_sets[left], candidate_sets[right])
        for left in range(len(candidate_sets))
        for right in range(left + 1, len(candidate_sets))
    ]
    filled = sum(bool(clean_text(item.get(field))) for item in all_candidates for field in REQUIRED_FIELDS)
    possible = len(all_candidates) * len(REQUIRED_FIELDS)
    return {
        "runs": len(runs),
        "successful_runs": len(successful),
        "success_rate": round(len(successful) / len(runs), 4) if runs else 0.0,
        "consistency": round(statistics.mean(pairs), 4) if pairs else None,
        "mean_duration_ms": round(statistics.mean(run["duration_ms"] for run in successful)) if successful else None,
        "mean_candidates": round(statistics.mean(len(run.get("candidates", [])) for run in successful), 2) if successful else 0.0,
        "unique_candidates": len(unique),
        "field_completeness": round(filled / possible, 4) if possible else 0.0,
        "candidate_keys": unique,
    }


def stability_score(summary: dict[str, Any]) -> float:
    consistency = summary.get("consistency")
    return 0.7 * float(summary.get("success_rate") or 0) + 0.3 * float(consistency if consistency is not None else summary.get("success_rate") or 0)


def compare_runs(baseline_runs: list[dict[str, Any]], opencli_runs: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = summarize_runs(baseline_runs)
    opencli = summarize_runs(opencli_runs)
    baseline_keys = set(baseline.pop("candidate_keys"))
    opencli_keys = set(opencli.pop("candidate_keys"))
    union = baseline_keys | opencli_keys
    baseline_recall = len(baseline_keys) / len(union) if union else 0.0
    opencli_recall = len(opencli_keys) / len(union) if union else 0.0
    baseline_stability = stability_score(baseline)
    opencli_stability = stability_score(opencli)
    gates = {
        "stability_better": opencli_stability > baseline_stability,
        "relative_recall_better": opencli_recall > baseline_recall,
        "field_completeness_not_worse": opencli["field_completeness"] >= baseline["field_completeness"],
    }
    migrate = all(gates.values())
    return {
        "baseline": baseline,
        "opencli": opencli,
        "comparison": {
            "union_unique_candidates": len(union),
            "overlap": len(baseline_keys & opencli_keys),
            "baseline_only": len(baseline_keys - opencli_keys),
            "opencli_only": len(opencli_keys - baseline_keys),
            "baseline_relative_recall": round(baseline_recall, 4),
            "opencli_relative_recall": round(opencli_recall, 4),
            "baseline_stability_score": round(baseline_stability, 4),
            "opencli_stability_score": round(opencli_stability, 4),
            "baseline_only_keys": sorted(public_key(key) for key in baseline_keys - opencli_keys),
            "opencli_only_keys": sorted(public_key(key) for key in opencli_keys - baseline_keys),
        },
        "migration_gate": {
            **gates,
            "migrate_execution_actions": migrate,
            "decision": "eligible_for_action_pilot" if migrate else "keep_existing_executor",
        },
    }


def load_queries(args: argparse.Namespace) -> list[str]:
    values = list(args.query or [])
    if args.queries_file:
        payload = json.loads(args.queries_file.read_text(encoding="utf-8"))
        entries = payload.get("queries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError("queries file must be an array or an object with a queries array")
        values.extend(item.get("query") if isinstance(item, dict) else item for item in entries)
    queries = []
    for value in values:
        query = clean_text(value)
        if query and query not in queries:
            queries.append(query)
    if not queries:
        raise ValueError("at least one --query or --queries-file entry is required")
    return queries[: args.max_queries]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only X-SaaS CDP vs OpenCLI A/B experiment")
    parser.add_argument("--query", action="append", help="Exact query to test; may be repeated")
    parser.add_argument("--queries-file", type=Path)
    parser.add_argument("--max-queries", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--source-port", type=int, default=9223)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if not 1 <= args.max_queries <= 8:
        parser.error("--max-queries must be between 1 and 8")
    queries = load_queries(args)

    cdp_endpoint, target_id, auth = create_xsaas_experiment_target(args.source_port)
    baseline_runs: list[dict[str, Any]] = []
    opencli_runs: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="asa-xsaas-ab-") as temp:
            temp_dir = Path(temp)
            for _ in range(args.repeats):
                for query in queries:
                    baseline_runs.append(run_baseline(query, args.limit, args.source_port, temp_dir))
                    opencli_runs.append(run_opencli(query, args.limit, cdp_endpoint))
    finally:
        close_xsaas_experiment_target(args.source_port, target_id)

    report = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_no_intake_no_outreach",
        "queries": queries,
        "repeats": args.repeats,
        "limit": args.limit,
        "auth_bridge": auth,
        **compare_runs(baseline_runs, opencli_runs),
        "runs": {
            "baseline": [{key: value for key, value in run.items() if key != "candidates"} for run in baseline_runs],
            "opencli": [{key: value for key, value in run.items() if key != "candidates"} for run in opencli_runs],
        },
        "notes": [
            "Relative recall uses the deduplicated union of both engines as the comparison set; it is not labeled ground-truth recall.",
            "The experiment never calls intake, apply, outreach, or A System database mutation paths.",
        ],
    }
    output = args.output or (ASA_ROOT / "work" / f"xsaas-opencli-ab-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(output),
        "migration_gate": report["migration_gate"],
        "comparison": report["comparison"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
