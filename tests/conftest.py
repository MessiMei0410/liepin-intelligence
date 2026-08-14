from __future__ import annotations

import pytest

from a_system_agent.llm import FakeLLM


@pytest.fixture(autouse=True)
def disable_unrequested_external_recovery(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Copied production databases must not resume real external work in tests."""
    if request.node.get_closest_marker("allow_external_recovery") is not None:
        return
    monkeypatch.setattr(
        "a_system_agent.workflow.WorkflowEngine.recover_external_continuations",
        lambda self: 0,
    )


@pytest.fixture(autouse=True)
def stub_default_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the default LLM factory so AgentService() constructed without an
    explicit `llm=` never touches the network or the macOS Keychain
    (/usr/bin/security) in tests.

    Only the *default* construction path (asa_core.app create_app → AgentService
    without llm=) is affected: tests that pass `llm=` explicitly, or construct
    an LLM directly for error-injection, are unchanged. Deterministic fake
    responses keep corpus/copilot tests from being flaky on real model output.
    """
    monkeypatch.setattr(
        "a_system_agent.service.create_default_llm",
        lambda config=None, *, db_path=None: FakeLLM(
            {},
            chat_text="已理解。",
            review={"decision": "approve", "reason": "test stub", "assessment": {}},
            intent_understanding=None,
        ),
    )
