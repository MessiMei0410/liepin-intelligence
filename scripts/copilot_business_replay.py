#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.conversation_state import build_context_state, enrich_turn_understanding  # noqa: E402


DEFAULT_CORPUS = ROOT / "tests" / "fixtures" / "copilot_business_turn_corpus_v1.json"


def _database_observation(db_path: str | Path) -> dict[str, Any]:
    """Read stable counters/identifiers used to prove what a replay turn changed."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        def count(table: str) -> int:
            return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) if table in tables else 0

        workflows = (
            [str(row[0]) for row in conn.execute("SELECT workflow_id FROM agent_workflows ORDER BY rowid")]
            if "agent_workflows" in tables else []
        )
        commands = (
            [
                {"command_id": str(row[0]), "status": str(row[1]), "command_type": str(row[2])}
                for row in conn.execute(
                    "SELECT command_id,status,command_type FROM agent_copilot_commands ORDER BY rowid"
                )
            ]
            if "agent_copilot_commands" in tables else []
        )
        return {
            "candidate_events": count("candidate_events"),
            "workflows": workflows,
            "commands": commands,
        }
    finally:
        conn.close()


def replay_service_turn(
    *,
    db_path: str | Path,
    message: str,
    session_id: str,
    context: dict[str, Any],
    run_turn: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    read_state: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay one turn through the real service boundary without expected-value injection.

    ``run_turn`` may call AgentService directly or the Core HTTP bridge. The trace is based
    only on the request, returned payload and database observations, so failed routing and
    accidental writes remain visible instead of being replaced by corpus annotations.
    """
    before = _database_observation(db_path)
    result = run_turn(message, session_id, dict(context))
    after = _database_observation(db_path)
    understanding = dict(result.get("intent_understanding") or {})
    pending_command = dict(result.get("pending_command") or {})
    workflow_id = str(result.get("workflow_id") or (result.get("workflow") or {}).get("workflow_id") or "")
    receipt = dict(result.get("execution_receipt") or {})
    state = read_state(session_id) if read_state else {}
    new_workflows = [item for item in after["workflows"] if item not in before["workflows"]]
    before_command_ids = {str(item["command_id"]) for item in before["commands"]}
    new_commands = [item for item in after["commands"] if item["command_id"] not in before_command_ids]
    writes = {
        "candidate_events": after["candidate_events"] - before["candidate_events"],
        "workflows": len(new_workflows),
        "commands": len(new_commands),
    }
    if pending_command:
        route = "confirm"
    elif understanding.get("needs_clarification"):
        route = "clarify"
    elif workflow_id:
        route = "workflow"
    else:
        route = "read"
    return {
        "user_message": message,
        "page_context": dict(context),
        "session_id": session_id,
        "business_focus": result.get("business_focus") or {},
        "understanding": understanding,
        "condition_ledger": (state or {}).get("constraints") or [],
        "conversation_state": state or {},
        "route": route,
        "tool": {
            "pending_command": pending_command or None,
            "workflow_id": workflow_id or None,
            "new_command_ids": [item["command_id"] for item in new_commands],
            "new_workflow_ids": new_workflows,
        },
        "writes": writes,
        "receipt": receipt or None,
        "answer": str(result.get("answer") or ""),
        "result": result,
    }


def summarize_service_traces(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize scenarios replayed through the real service boundary."""
    return {
        "evaluation_scope": "real_service_boundary",
        "total_scenarios": len(traces),
        "routes": {
            route: sum(str(trace.get("route") or "") == route for trace in traces)
            for route in ("read", "clarify", "confirm", "workflow")
        },
        "write_totals": {
            key: sum(int((trace.get("writes") or {}).get(key) or 0) for trace in traces)
            for key in ("candidate_events", "workflows", "commands")
        },
        "verified_receipts": sum(bool((trace.get("receipt") or {}).get("verified")) for trace in traces),
        "real_command_ids": sum(bool((trace.get("tool") or {}).get("pending_command")) for trace in traces),
        "real_workflow_ids": sum(bool((trace.get("tool") or {}).get("workflow_id")) for trace in traces),
    }


def evaluate_service_scenario_matrix(
    *,
    db_path: str | Path,
    scenarios: list[dict[str, Any]],
    run_turn: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    read_state: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score real service outputs; labels are never passed to the turn compiler."""
    results: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios):
        trace = replay_service_turn(
            db_path=db_path,
            message=str(scenario.get("message") or ""),
            session_id=str(scenario.get("session_id") or f"service-matrix-{index + 1}"),
            context=dict(scenario.get("context") or {}),
            run_turn=run_turn,
            read_state=read_state,
        )
        understanding = dict(trace.get("understanding") or {})
        expected_target = dict(scenario.get("expected_target") or {})
        expected_action = str(scenario.get("expected_action") or "")
        object_ok = not expected_target or (
            str((understanding.get("target") or {}).get("type") or "") == str(expected_target.get("type") or "")
            and str((understanding.get("target") or {}).get("id") or "") == str(expected_target.get("id") or "")
        )
        action_ok = not expected_action or str(understanding.get("action") or "none") == expected_action
        zero_execution_ok = not bool(scenario.get("expect_zero_execution")) or (
            trace["writes"]["candidate_events"] == 0
            and trace["writes"]["workflows"] == 0
            and not bool(understanding.get("safe_for_action"))
        )
        results.append({
            "id": scenario.get("id") or f"service-matrix-{index + 1}",
            "trace": trace,
            "object_ok": object_ok,
            "action_ok": action_ok,
            "zero_execution_ok": zero_execution_ok,
        })
    object_cases = [item for item, scenario in zip(results, scenarios) if scenario.get("expected_target")]
    action_cases = [item for item, scenario in zip(results, scenarios) if scenario.get("expected_action")]
    zero_execution_cases = [item for item, scenario in zip(results, scenarios) if scenario.get("expect_zero_execution")]
    return {
        "evaluation_scope": "real_service_boundary",
        "expected_values_injected": False,
        "total_scenarios": len(results),
        "metrics": {
            "explicit_object_accuracy": (
                round(sum(item["object_ok"] for item in object_cases) / len(object_cases), 4)
                if object_cases else None
            ),
            "high_frequency_action_accuracy": (
                round(sum(item["action_ok"] for item in action_cases) / len(action_cases), 4)
                if action_cases else None
            ),
            "question_or_discussion_misexecution": sum(
                not item["zero_execution_ok"] for item in zero_execution_cases
            ),
        },
        "failures": [
            {
                "id": item["id"],
                "object_ok": item["object_ok"],
                "action_ok": item["action_ok"],
                "zero_execution_ok": item["zero_execution_ok"],
            }
            for item in results
            if not (item["object_ok"] and item["action_ok"] and item["zero_execution_ok"])
        ],
        "results": results,
    }


def _question(message: str) -> bool:
    return bool(re.search(r"[?？]$|(?:吗|呢|如何|怎么|为什么|为何|是否|能不能|什么|多少|哪(?:个|些)?)", message))


def _seed_speech_act(message: str) -> str:
    compact = re.sub(r"[\s。.!！?？,，、]+", "", message)
    if _question(message):
        return "ask"
    if re.search(r"(?:讨论|聊聊|商量|怎么看)", message):
        return "discuss"
    if compact in {"确认", "可以", "行", "好", "好的", "按这个来", "就这样", "确认执行", "收到"}:
        return "confirm"
    if re.search(r"(?:算了|别执行|取消|撤回|撤销|作废)", message):
        return "cancel"
    if re.search(r"(?:不是.+是|改成|改为|纠正|更正)", message):
        return "correct"
    return "inform"


def _route_for_turn(actual: dict[str, Any]) -> str:
    if actual.get("needs_clarification"):
        return "clarify"
    if actual.get("speech_act") in {"ask", "discuss"}:
        return "read"
    if actual.get("effect") in {"confirm", "internal_write"}:
        return "confirm"
    if actual.get("effect") == "read":
        return "read"
    return "none"


def replay_case(case: dict[str, Any]) -> dict[str, Any]:
    message = str(case.get("message") or "").strip()
    raw = {
        "speech_act": _seed_speech_act(message),
        "action": "none",
        "topic": "",
        "objective": "",
        "target": dict(case.get("target") or case.get("context") or {}),
        "confidence": 1.0,
        "needs_clarification": bool(case.get("needs_clarification")),
        "fact_updates": list(case.get("fact_updates") or []),
        "constraint_changes": list(case.get("constraint_changes") or []),
        "source_quotes": list(case.get("source_quotes") or []),
    }
    actual = enrich_turn_understanding(raw, message=message, pending_plan_ref={})
    expected_action = str(case.get("primary_action") or "none")
    expected_speech = str(case.get("speech_act") or "other")
    failures: list[dict[str, str]] = []
    if str(actual.get("action") or "none") != expected_action:
        failures.append({"layer": "understanding", "field": "primary_action"})
    if str(actual.get("speech_act") or "") != expected_speech:
        failures.append({"layer": "understanding", "field": "speech_act"})
    expected_target = dict(case.get("target") or case.get("context") or {})
    actual_target = dict(actual.get("target") or {})
    if expected_target.get("id") is not None and str(actual_target.get("id")) != str(expected_target.get("id")):
        failures.append({"layer": "context", "field": "target"})
    if bool(actual.get("needs_clarification")) != bool(case.get("needs_clarification")):
        failures.append({"layer": "context", "field": "needs_clarification"})
    expected_quotes = [str(item) for item in case.get("source_quotes") or []]
    if any(quote not in list(actual.get("source_quotes") or []) for quote in expected_quotes):
        failures.append({"layer": "understanding", "field": "source_quotes"})
    if expected_speech in {"ask", "discuss"} and actual.get("safe_for_action"):
        failures.append({"layer": "safety", "field": "question_or_discussion_execution"})
    expected_route = str(case.get("expected_route") or case.get("effect") or "none")
    actual_route = _route_for_turn(actual)
    if expected_route != actual_route:
        failures.append({"layer": "routing", "field": "route"})
    expected_operations = [str(item) for item in case.get("expected_operation_order") or []]
    actual_operations = [str(item.get("action") or "") for item in actual.get("operations") or []]
    if expected_operations != actual_operations:
        failures.append({"layer": "tool", "field": "operation_order"})
    receipt_state = "pending_confirmation" if actual_route == "confirm" else "read_only" if actual_route == "read" else "not_executed"
    if case.get("should_execute") is False and receipt_state not in {"pending_confirmation", "read_only", "not_executed"}:
        failures.append({"layer": "receipt", "field": "unexpected_execution"})
    business_focus = {
        "context": dict(case.get("context") or {}),
        "references": list(case.get("references") or []),
        "history_turns": len(case.get("history") or []),
    }
    return {
        "id": case.get("id"),
        "message": message,
        "expected": {
            "speech_act": expected_speech,
            "primary_action": expected_action,
            "route": expected_route,
            "operation_order": expected_operations,
        },
        "trace": {
            "user_message": message,
            "page_context": case.get("context") or {},
            "conversation_history": case.get("history") or [],
            "business_focus": business_focus,
            "understanding": actual,
            "condition_ledger_changes": actual.get("constraint_changes") or [],
            "route": actual_route,
            "tool": {"operations": actual_operations, "executed": False},
            "receipt": {"state": receipt_state, "verified": False},
        },
        "failures": failures,
    }


def run_replay(corpus_path: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    results = [replay_case(case) for case in corpus.get("cases") or []]
    total = len(results)
    action_ok = sum(not any(f["field"] == "primary_action" for f in row["failures"]) for row in results)
    command_results = [
        row for row in results if str((row.get("expected") or {}).get("primary_action") or "none") != "none"
    ]
    command_action_ok = sum(
        not any(f["field"] == "primary_action" for f in row["failures"]) for row in command_results
    )
    speech_ok = sum(not any(f["field"] == "speech_act" for f in row["failures"]) for row in results)
    operation_ok = sum(not any(f["field"] == "operation_order" for f in row["failures"]) for row in results)
    route_ok = sum(not any(f["field"] == "route" for f in row["failures"]) for row in results)
    unsafe = sum(any(f["layer"] == "safety" for f in row["failures"]) for row in results)
    by_layer: dict[str, int] = {}
    for row in results:
        for failure in row["failures"]:
            layer = failure["layer"]
            by_layer[layer] = by_layer.get(layer, 0) + 1
    return {
        "version": "copilot_business_replay_v1",
        "corpus_version": corpus.get("version"),
        "total": total,
        "methodology": {
            "evaluation_scope": "deterministic_understanding_postprocessor",
            "end_to_end": False,
            "expected_fields_seeded": [
                "target", "needs_clarification", "fact_updates", "constraint_changes", "source_quotes",
            ],
            "not_authoritative": ["explicit_object_accuracy"],
            "service_evidence": "Use replay_service_turn and summarize_service_traces for real routing and write evidence.",
        },
        "metrics": {
            "action_accuracy": round(action_ok / total, 4) if total else 0.0,
            "high_frequency_action_accuracy": round(command_action_ok / len(command_results), 4) if command_results else 0.0,
            "speech_act_accuracy": round(speech_ok / total, 4) if total else 0.0,
            "operation_order_accuracy": round(operation_ok / total, 4) if total else 0.0,
            "routing_accuracy": round(route_ok / total, 4) if total else 0.0,
            "explicit_object_accuracy": None,
            "five_turn_goal_and_condition_retention": _five_turn_retention(),
            "question_or_discussion_misexecution": unsafe,
        },
        "failures_by_layer": by_layer,
        "failures": [row for row in results if row["failures"]],
    }


def _five_turn_retention() -> float:
    focus = {
        "context": {"type": "job", "id": 137}, "client": "客户甲",
        "job": {"id": 137, "title": "机械高级工程师"}, "candidate": {}, "confidence": 1.0,
    }
    constraint = {"kind": "location", "quote": "江浙沪", "value": "江浙沪"}
    state = build_context_state(
        None, message="再找 10 人，地点江浙沪", context={"type": "job", "id": 137},
        business_focus=focus,
        understanding={
            "turn_kind": "command", "topic": "sourcing", "action": "candidate_sourcing",
            "objective": "再找 10 人", "target": {"type": "job", "id": 137}, "fact_updates": [],
        },
        decision={"effect": "create_plan", "effective_constraints": [constraint]},
        workflow_intent=None, now="2026-08-15 10:00:00",
    )
    for index, message in enumerate(["这轮只找到 2 人", "客户说先看", "预算不变", "先不要触达"]):
        turn = enrich_turn_understanding(
            {"speech_act": "inform", "action": "none", "confidence": 1.0},
            message=message, pending_plan_ref={},
        )
        state = build_context_state(
            state, message=message, context={"type": "job", "id": 137}, business_focus=focus,
            understanding=turn, decision={"effect": "answer"}, workflow_intent=None,
            now=f"2026-08-15 10:0{index + 1}:00",
        )
    retained = state.get("active_goal", {}).get("objective") == "再找 10 人" and any(
        item.get("quote") == "江浙沪" for item in state.get("constraints") or []
    )
    return 1.0 if retained else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay the ASA recruiter-language turn corpus")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_replay(args.corpus)
    if args.baseline and args.baseline.exists():
        previous = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["comparison"] = {
            key: round(float(report["metrics"].get(key) or 0) - float((previous.get("metrics") or {}).get(key) or 0), 4)
            for key in report["metrics"]
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Corpus: {report['corpus_version']} ({report['total']} turns)")
        for key, value in report["metrics"].items():
            print(f"{key}: {value}")
        print("failures_by_layer:", json.dumps(report["failures_by_layer"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
