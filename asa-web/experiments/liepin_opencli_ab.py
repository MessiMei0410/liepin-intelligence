#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ASA_ROOT = Path(__file__).resolve().parents[1]
LIEPIN_ROOT = Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence")
LIEPIN_RUNNER = LIEPIN_ROOT / "scripts" / "run_published_position_search.py"
DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
OPENCLI_BIN = Path("/Users/messi/.hermes/node/bin/opencli")

sys.path.insert(0, str(ASA_ROOT / "experiments"))
from xsaas_opencli_ab import candidate_key, clean_text, compare_runs, public_key  # noqa: E402


def load_json(url: str) -> Any:
    from urllib.request import urlopen

    with urlopen(url, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def live_liepin_target(port: int) -> tuple[str, dict[str, Any]]:
    tabs = load_json(f"http://127.0.0.1:{port}/json/list")
    target = next(
        (
            tab for tab in tabs
            if tab.get("type") == "page"
            and "h.liepin.com/search/getConditionItem" in str(tab.get("url") or "")
        ),
        None,
    )
    if not target or not target.get("webSocketDebuggerUrl"):
        raise RuntimeError("No signed-in Liepin search tab is available on the CDP port")
    return str(target["webSocketDebuggerUrl"]), {
        "ok": True,
        "mode": "live_authenticated_tab",
        "source_port": port,
        "target_id_hash": public_key(str(target.get("id") or "")),
    }


def normalize_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": clean_text(item.get("resumeId") or item.get("res_id_encode")),
        "name": clean_text(item.get("name")),
        "company": clean_text(item.get("company") or item.get("currentCompany")),
        "title": clean_text(item.get("title") or item.get("currentTitle")),
        "experience": clean_text(item.get("experience")),
        "education": clean_text(item.get("education")),
        "city": clean_text(item.get("city")),
        "profile_text": clean_text(item.get("profile_text") or item.get("profileText")),
        "full_text": clean_text(item.get("full_text") or item.get("fullText")),
        "work_text": clean_text(item.get("work_text") or item.get("workText")),
        "project_text": clean_text(item.get("project_text") or item.get("projectText")),
        "education_text": clean_text(item.get("education_text") or item.get("educationText")),
        "url": clean_text(item.get("url") or item.get("resume_url")),
        "data_stage": clean_text(item.get("dataStage") or item.get("data_stage")),
        "resume_capture_status": clean_text(item.get("resumeCaptureStatus") or item.get("resume_capture_status")),
        "query": clean_text(item.get("query")),
    }


def run_process(
    command: list[str], timeout: int, env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    started = time.perf_counter()
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)
    return proc, round((time.perf_counter() - started) * 1000)


def run_baseline(
    query: str,
    limit: int,
    port: int,
    client: str,
    job: str,
    db_path: Path,
    temp_dir: Path,
) -> dict[str, Any]:
    token = hashlib.sha256(f"{query}-{time.time_ns()}".encode()).hexdigest()[:10]
    queries_path = temp_dir / f"liepin-{token}-queries.json"
    output_path = temp_dir / f"liepin-{token}-candidates.json"
    report_dir = temp_dir / f"liepin-{token}-report"
    queries_path.write_text(
        json.dumps({"queries": [{"round": "ab", "query": query, "purpose": "readonly_ab"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    proc, elapsed_ms = run_process(
        [
            sys.executable, str(LIEPIN_RUNNER),
            "--client", client, "--position", job,
            "--db", str(db_path), "--output-dir", str(report_dir),
            "--port", str(port), "--rounds", "1", "--max-cards", str(limit),
            "--min-score", "0", "--recommend-score", "65",
            "--no-open-links", "--dry-run", "--json-output", str(output_path),
            "--queries-json", str(queries_path),
            "--min-delay", "0", "--max-delay", "0",
        ],
        timeout=180,
    )
    raw = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else []
    candidates = [normalize_candidate(item) for item in raw if isinstance(item, dict)]
    return {
        "engine": "baseline_cdp",
        "query": query,
        "success": proc.returncode == 0,
        "status": "completed" if proc.returncode == 0 else "failed",
        "duration_ms": elapsed_ms,
        "candidates": candidates,
        "error": clean_text(proc.stderr)[-1000:] if proc.returncode else "",
    }


def run_opencli(query: str, limit: int, cdp_endpoint: str) -> dict[str, Any]:
    child_env = os.environ.copy()
    child_env["OPENCLI_CDP_ENDPOINT"] = cdp_endpoint
    proc, elapsed_ms = run_process(
        [
            str(OPENCLI_BIN), "liepin", "candidate-search", query,
            "--limit", str(limit), "-f", "json",
            "--trace", "retain-on-failure", "--window", "background",
        ],
        timeout=120,
        env=child_env,
    )
    raw: list[dict[str, Any]] = []
    if proc.returncode == 0:
        try:
            payload = json.loads(proc.stdout)
            raw = payload if isinstance(payload, list) else []
        except json.JSONDecodeError:
            pass
    empty_result = proc.returncode == 66 or "EMPTY_RESULT" in (proc.stderr or "")
    success = proc.returncode == 0 or empty_result
    return {
        "engine": "opencli",
        "query": query,
        "success": success,
        "status": "completed_empty" if empty_result else "completed" if success else "failed",
        "duration_ms": elapsed_ms,
        "candidates": [normalize_candidate(item) for item in raw if isinstance(item, dict)],
        "error": clean_text(proc.stderr or proc.stdout)[-1000:] if not success else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Liepin CDP vs OpenCLI A/B experiment")
    parser.add_argument("--client", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("--repeats must be between 1 and 10")
    if not 1 <= args.limit <= 24:
        parser.error("--limit must be between 1 and 24")
    queries = []
    for value in args.query:
        query = clean_text(value)
        if query and query not in queries:
            queries.append(query)
    cdp_endpoint, auth = live_liepin_target(args.port)

    baseline_runs: list[dict[str, Any]] = []
    opencli_runs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="asa-liepin-ab-") as temp:
        temp_dir = Path(temp)
        for _ in range(args.repeats):
            for query in queries:
                baseline_runs.append(run_baseline(query, args.limit, args.port, args.client, args.job, args.db, temp_dir))
                opencli_runs.append(run_opencli(query, args.limit, cdp_endpoint))

    report = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "read_only_no_intake_no_outreach",
        "channel": "liepin",
        "client": args.client,
        "job": args.job,
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
            "Relative recall uses the deduplicated union of both engines, not labeled ground truth.",
            "Masked Liepin identities are compared by normalized name, current company, and current title.",
            "The baseline uses the production runner with --dry-run, --min-score 0, and no link opening.",
        ],
    }
    output = args.output or (ASA_ROOT / "work" / f"liepin-opencli-ab-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
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
