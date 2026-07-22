from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "runtime": {"max_workers": 3, "copilot_max_skills": 3},
    "model": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "timeout_seconds": 60,
        "retry_attempts": 3,
        "keychain_service": "a-system-agent-deepseek",
        "keychain_account": "api.deepseek.com",
    },
    "automation": {
        "high_score": 75,
        "low_score": 55,
        "min_confidence": 0.80,
        "min_evidence_coverage": 0.75,
    },
    "skills": {
        "enabled": [
            "job_diagnosis",
            "candidate_assessment",
            "verification_plan",
            "communication_draft",
            "liepin_resume_capture",
            "opencli_usage",
            "opencli_browser_read",
            "document_understanding",
            "job_intake", "jd_calibration", "job_library_update", "talent_pool_search", "search_strategy",
            "multi_channel_sourcing", "job_publish_prepare", "job_publish_execute", "resume_export",
            "candidate_batch_assessment", "matching_report", "recommendation_report",
            "client_recommendation", "reply_triage", "communication_draft_batch", "outreach_prepare", "outreach_execute", "identity_merge_preflight",
            "interview_followup", "salary_verification", "salary_negotiation", "decision_coaching",
            "offer_confirmation", "onboarding_followup", "project_retrospective", "memory_capture",
        ]
    },
    "memory": {"enabled": True, "mode": "shadow", "candidate_limit": 12, "result_limit": 5},
    "learning": {
        "minimum_support": 3,
        "minimum_candidates": 2,
        "contradiction_window": 10,
        "pause_rate": 0.30,
    },
    "ui": {"copilot_default_open": True, "flow_poll_seconds": 30},
}

ENV_OVERRIDES = {
    "A_SYSTEM_AGENT_BASE_URL": ("model", "base_url", str),
    "A_SYSTEM_AGENT_MODEL": ("model", "model", str),
    "A_SYSTEM_AGENT_TIMEOUT": ("model", "timeout_seconds", int),
    "A_SYSTEM_AGENT_RETRY_ATTEMPTS": ("model", "retry_attempts", int),
    "A_SYSTEM_AGENT_MAX_WORKERS": ("runtime", "max_workers", int),
    "A_SYSTEM_AGENT_MEMORY_MODE": ("memory", "mode", str),
}


def _merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "asa.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = deepcopy(DEFAULTS)
    selected = Path(path or os.environ.get("A_SYSTEM_AGENT_CONFIG") or default_config_path()).expanduser()
    if selected.exists():
        with selected.open("rb") as handle:
            loaded = tomllib.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("ASA 配置必须是 TOML 对象")
        _merge(config, loaded)
    for env_name, (section, key, caster) in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw not in (None, ""):
            config[section][key] = caster(raw)
    if config["memory"]["mode"] not in {"shadow", "active", "off"}:
        raise ValueError("memory.mode 必须是 shadow/active/off")
    config["_path"] = str(selected)
    return config


def public_config(config: dict[str, Any], *, model_available: bool) -> dict[str, Any]:
    return {
        "runtime": dict(config["runtime"]),
        "model": {
            "provider": "deepseek_official" if "api.deepseek.com" in config["model"]["base_url"] else "openai_compatible",
            "model": config["model"]["model"],
            "timeout_seconds": config["model"]["timeout_seconds"],
            "configured": bool(model_available),
        },
        "automation": dict(config["automation"]),
        "skills": {"enabled": list(config["skills"].get("enabled") or [])},
        "memory": dict(config["memory"]),
        "learning": dict(config["learning"]),
        "ui": dict(config["ui"]),
    }
