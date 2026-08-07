"""Shared configuration for the long-lived Liepin CDP browser."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_PORT = 9223
DEFAULT_PROFILE_DIR = Path.home() / ".hermes" / "chrome_profile_xhs"
DEFAULT_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "ai.hermes.chrome-cdp.plist"
DEFAULT_OPENCLI_EXTENSION_DIR = (
    Path(__file__).resolve().parents[1] / "asa-web" / "opencli" / "opencli-extension-v1.0.22"
)


def cdp_profile_dir() -> Path:
    """Return the one profile used by all Liepin CDP entry points."""
    configured = os.environ.get("A_SYSTEM_CDP_PROFILE_DIR") or os.environ.get("LIEPIN_CDP_PROFILE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_PROFILE_DIR


def cdp_launch_agent_path() -> Path:
    configured = os.environ.get("A_SYSTEM_CDP_LAUNCH_AGENT") or os.environ.get("LIEPIN_CDP_LAUNCH_AGENT")
    return Path(configured).expanduser() if configured else DEFAULT_LAUNCH_AGENT


def cdp_launch_agent_label() -> str:
    return cdp_launch_agent_path().stem


def opencli_extension_dir() -> Path:
    configured = (
        os.environ.get("A_SYSTEM_OPENCLI_EXTENSION_DIR")
        or os.environ.get("OPENCLI_EXTENSION_DIR")
    )
    return Path(configured).expanduser() if configured else DEFAULT_OPENCLI_EXTENSION_DIR
