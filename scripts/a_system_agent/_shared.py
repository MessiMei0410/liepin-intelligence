from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config, public_config
from .capability_runtime import RecruitingCapabilityRuntime, ZERO_RESULT_ATTRIBUTION_LABELS
from .context import build_candidate_context
from .evaluation import compute_evaluation
from .job_status import job_status_intake_allowed
from .llm import BaseLLM, LLMError, PROMPT_VERSION, create_default_llm
from .liepin_capture import capture_open_liepin_resumes, resume_matches_identity
from .native_attachments import attachment_read_requested, image_analysis_requested, resolve_wechat_attachments
from .panel import (
    ROLE_DEFINITIONS,
    fallback_role_review,
    normalize_role_review,
    role_payload,
    synthesize_panel,
)
from .policy import action_decision, is_stopped
from .privacy import sanitize_payload
from .schema import ensure_schema
from .scoring import normalize_assessment
from .skills import SkillRegistry, SkillSpec
from . import strategy_v2
from .workflow import BUSINESS_OUTCOME_LABELS, WorkflowEngine, classify_business_outcome, sourcing_target_stats


ASSESSMENT_VERSION = "candidate-assessment-v1"
PANEL_VERSION = "candidate-panel-v2"
OPENCLI_BIN = Path(os.environ.get("A_SYSTEM_OPENCLI_BIN", "/Users/messi/.hermes/node/bin/opencli")).expanduser()
OPENCLI_BROWSER_READ_COMMANDS = {
    "analyze",
    "bind",
    "console",
    "extract",
    "find",
    "frames",
    "get",
    "network",
    "screenshot",
    "state",
    "wait",
    "verify",
}
OPENCLI_BROWSER_TAB_READ_COMMANDS = {"current", "list"}
DECISION_LABELS = {
    "priority_review": "建议优先复核",
    "verify_first": "先核验后判断",
    "hold": "暂缓",
    "not_recommended": "建议不推进",
}
SOURCING_SIGNAL_WEIGHTS = {
    "review_pass": 1.0,
    "contacted": 2.0,
    "recommended": 3.0,
    "stopped": -2.0,
    "stopped_neutral": 0.0,
    "client_approved": 4.0,
    "client_interview": 4.5,
    "client_offer": 6.0,
    "client_hired": 8.0,
    "client_rejected": -3.0,
    "client_hold": 0.0,
}
SOURCING_SIGNAL_LABELS = {
    "review_pass": "用户复核通过",
    "contacted": "用户已联系",
    "recommended": "用户已推荐客户",
    "stopped": "用户停止推进",
    "stopped_neutral": "候选人意向不足停止",
    "client_approved": "客户认可",
    "client_interview": "客户进入面试",
    "client_offer": "客户进入 Offer",
    "client_hired": "客户确认入职",
    "client_rejected": "客户否决",
    "client_hold": "客户暂缓",
}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if row else {}


def _is_short_ack(message: str) -> bool:
    cleaned = re.sub(r"[\s。.!！?？,，、]+", "", str(message or ""))
    return cleaned in {"好", "好的", "好了", "可以", "行", "嗯", "收到", "明白", "ok", "OK"}


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _latest_event(context: dict[str, Any], event_type: str) -> dict[str, Any]:
    for event in context.get("events", []) or []:
        if event.get("event_type") == event_type:
            return event
    return {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _channel_key(source: Any) -> str:
    value = str(source or "").strip().lower()
    if "xsaas" in value or "x-saas" in value:
        return "xsaas"
    if "liepin" in value or "猎聘" in value:
        return "liepin"
    if "legacy" in value or "talent_pool" in value or "历史" in value:
        return "talent_pool"
    if value:
        return "other"
    return "unknown"
