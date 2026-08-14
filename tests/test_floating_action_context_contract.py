from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import liepin_workbench_server as server  # noqa: E402


def _state(raw: dict) -> dict:
    return {"active_context_raw": raw, "active_context": {"surface": raw.get("surface"), "job_candidate_id": raw.get("job_candidate_id")}}


def _context(name: str, source_id: str, *, job_candidate_id: int | None = None) -> dict:
    return {
        "surface": "liepin",
        "context_key": "liepin:tab-1",
        "instance_id": "tab-1",
        "job_candidate_id": job_candidate_id,
        "res_id_encode": source_id,
        "candidate": {"name": name, "company": "示例公司", "title": "电源工程师"},
    }


def test_same_instance_navigation_conflict_has_no_queue_side_effect() -> None:
    actual = _context("候选人 B", "res-b", job_candidate_id=202)
    expected = server.floating_context_identity_summary(_context("候选人 A", "res-a", job_candidate_id=101))
    payload = {
        "action": "generate_report",
        "expected_context_key": "liepin:tab-1",
        "expected_instance_id": "tab-1",
        "expected_job_candidate_id": 101,
        "expected_context_revision": expected["context_revision"],
    }
    state = type("State", (), {"agent_service": type("Service", (), {})()})()
    with patch.object(server, "build_floating_state", return_value=_state(actual)), patch.object(server, "enqueue_floating_command") as enqueue:
        result = server.route_floating_action(state, payload)
    assert result["error_code"] == "context_changed"
    assert result["latest_context"]["job_candidate_id"] == 202
    enqueue.assert_not_called()


def test_successful_page_action_is_bound_to_expected_revision() -> None:
    actual = _context("候选人 A", "res-a", job_candidate_id=101)
    identity = server.floating_context_identity_summary(actual)
    state = type("State", (), {"agent_service": type("Service", (), {
        "create_goal": lambda self, *args: {"ok": True, "workflow_id": "wf-a"},
    })()})()
    payload = {
        "action": "generate_report",
        "expected_context_key": actual["context_key"],
        "expected_instance_id": actual["instance_id"],
        "expected_job_candidate_id": 101,
        "expected_context_revision": identity["context_revision"],
    }
    with patch.object(server, "build_floating_state", return_value=_state(actual)):
        result = server.route_floating_action(state, payload)
    assert result["ok"] is True
    assert result["workflow_id"] == "wf-a"


def test_successful_request_id_replay_returns_first_receipt_without_new_workflow() -> None:
    actual = _context("候选人 A", "res-a", job_candidate_id=101)
    identity = server.floating_context_identity_summary(actual)
    calls: list[object] = []
    service = type("Service", (), {
        "create_goal": lambda self, *args: (calls.append(args) or {"ok": True, "workflow_id": "wf-a"}),
    })()
    state = type("State", (), {"agent_service": service})()
    payload = {
        "action": "generate_report", "request_id": "web_replay_1",
        "expected_context_key": actual["context_key"], "expected_instance_id": actual["instance_id"],
        "expected_job_candidate_id": 101, "expected_context_revision": identity["context_revision"],
    }
    server.ASA_FLOATING_ACTION_RECEIPTS.pop("web_replay_1", None)
    with patch.object(server, "build_floating_state", return_value=_state(actual)):
        first = server.route_floating_action(state, payload)
        replay = server.route_floating_action(state, payload)
    assert first["workflow_id"] == replay["workflow_id"] == "wf-a"
    assert replay["idempotent_replay"] is True
    assert len(calls) == 1


def test_identity_summary_does_not_include_private_page_values() -> None:
    context = _context("候选人 A", "res-a", job_candidate_id=None)
    context["cookie"] = "secret-cookie"
    context["cdp_session"] = "secret-session"
    summary = server.floating_context_identity_summary(context)
    assert "cookie" not in summary
    assert "cdp_session" not in summary
    assert "secret-cookie" not in str(summary)
