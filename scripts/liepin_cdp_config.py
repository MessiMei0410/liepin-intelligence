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


def assistant_extension_dirs() -> list[Path]:
    """页面桥扩展目录列表：猎聘回复助手 + X-SaaS 人选推进助手。
    这两个扩展的 content script 会把当前猎聘/X-SaaS 页面状态上报到
    /api/asa/floating/context，是浮窗"刷新页面识别"的数据来源。"""
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "liepin-reply-assistant-extension",
        repo_root / "xsaas-candidate-assistant-extension",
    ]
    return [path for path in candidates if (path / "manifest.json").exists()]
