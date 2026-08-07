from __future__ import annotations

import pytest


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
