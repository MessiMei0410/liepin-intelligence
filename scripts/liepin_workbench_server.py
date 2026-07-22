#!/usr/bin/env python3
"""Local-only server for the Liepin intelligence workbench."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from a_system_agent import AgentService
from a_system_agent.native_attachments import (
    MAX_ATTACHMENT_BYTES,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    detect_wechat_image_bubble,
    extract_local_document,
    visible_attachment_names,
    visible_wechat_attachment_path,
)
from a_system_agent.schema import ensure_schema as ensure_agent_schema
from asa_core.stop_reasons import normalize_stop_reason

from confirm_project_assignment import (
    DEFAULT_DB as CONFIRM_DEFAULT_DB,
    DEFAULT_OUTPUT_DIR,
    connect_db,
    load_needs_confirmation,
    load_task,
    project_label,
    resolve_project,
    row_current_project,
    update_confirmation,
)
from generate_liepin_workbench import write_html
from generate_next_search_strategy import build_strategy_item, load_feedback_examples, load_recent_experiment_notes
from generate_next_search_strategy import load_position_profiles, load_strategy_corrections
from generate_position_dashboard import clean, collect_projects, connect
from generate_workflow_status_report import collect_metrics
from record_client_feedback import (
    LABEL_BY_FEEDBACK,
    build_record as build_feedback_record,
    connect as connect_feedback_db,
    ensure_schema as ensure_feedback_schema,
    insert_record as insert_feedback_record,
)
from record_candidate_reply import (
    build_record as build_reply_record,
    connect as connect_reply_db,
    ensure_schema as ensure_reply_schema,
    insert_reply_and_task,
)
from record_outreach_event import (
    build_record as build_outreach_record,
    connect as connect_outreach_db,
    ensure_schema as ensure_outreach_schema,
    insert_record as insert_outreach_record,
)
from record_search_experiment import (
    connect as connect_search_db,
    ensure_schema as ensure_search_schema,
    insert_record as insert_search_record,
    normalize_record as normalize_search_record,
)
from sync_reply_assistant_samples import (
    normalize_row as normalize_reply_sample_row,
    upsert_samples as upsert_reply_samples,
    ensure_schema as ensure_reply_sample_schema,
)
from sync_reply_assistant_outreach_events import (
    normalize_event as normalize_reply_outreach_event,
    insert_events as insert_reply_outreach_events,
    ensure_schema as ensure_reply_outreach_schema,
)


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE_DIR / "scripts"
DEFAULT_DB = CONFIRM_DEFAULT_DB
STATIC_WORKBENCH = "猎聘智能工作台.html"
SERVER_WORKBENCH = "猎聘智能工作台_可写回.html"
TALENT_SYSTEM_ROOT = Path(os.environ.get("A_SYSTEM_ROOT", "/Users/messi/Documents/Codex/2026-06-26/re")).expanduser()
TALENT_SYNC_SCRIPT = TALENT_SYSTEM_ROOT / "work" / "talent_system_sync.py"
TALENT_ACTION_DIR = BASE_DIR / "outputs" / "talent_system_action_batches"
TALENT_REPORT_PREFIX = "liepin_plugin_action"
TALENT_SYNC_LOCK = threading.Lock()
TALENT_DB = Path(os.environ.get("A_SYSTEM_DB", TALENT_SYSTEM_ROOT / "outputs" / "talent_system_v3_20260629.db")).expanduser()
TALENT_WORKBENCH_BUILD = TALENT_SYSTEM_ROOT / "work" / "build_talent_workbench.py"
MULTICHANNEL_SCRIPT = Path(
    os.environ.get(
        "A_SYSTEM_MULTICHANNEL_SCRIPT",
        "/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py",
    )
).expanduser()
CODEX_BIN = Path(os.environ.get("A_SYSTEM_CODEX_BIN", "/Users/messi/.hermes/node/bin/codex")).expanduser()
SOURCING_RUN_DIR = BASE_DIR / "outputs" / "sourcing_runs"
SOURCING_RUN_LOCK = threading.Lock()
SOURCING_RUN_TIMEOUT_SECONDS = int(os.environ.get("A_SYSTEM_SOURCING_TIMEOUT_SECONDS", "7200"))
CANDIDATE_MERGE_CONFIRMATION_TTL_SECONDS = 300
CANDIDATE_MERGE_CONFIRMATIONS: dict[str, dict[str, Any]] = {}
CANDIDATE_MERGE_LOCK = threading.Lock()
CANDIDATE_MESSAGE_CONFIRMATION_TTL_SECONDS = 300
CANDIDATE_MESSAGE_CONFIRMATIONS: dict[str, dict[str, Any]] = {}
CANDIDATE_MESSAGE_CONFIRMATION_LOCK = threading.Lock()
CANDIDATE_STATE_CONFIRMATION_TTL_SECONDS = 300
CANDIDATE_STATE_CONFIRMATIONS: dict[str, dict[str, Any]] = {}
CANDIDATE_STATE_CONFIRMATION_LOCK = threading.Lock()
ASA_FLOATING_LOCK = threading.Lock()
ASA_FLOATING_CONTEXTS: dict[str, dict[str, Any]] = {}
ASA_FLOATING_COMMANDS: dict[str, list[dict[str, Any]]] = {"liepin": [], "xsaas": [], "a_system": [], "native": []}
ASA_FLOATING_COMMAND_HISTORY: list[dict[str, Any]] = []
ASA_FLOATING_COMMAND_RESULTS: list[dict[str, Any]] = []
ASA_UPLOAD_ROOT = Path.home() / "Library/Application Support/ASA/uploads"
ASA_UPLOAD_MAX_BASE64_CHARS = ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4 + 16
ASA_FLOATING_APP_CANDIDATES = [
    Path("/Users/messi/Applications/ASA.app"),
    Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/asa-floating-app/build/ASA.app"),
    Path("/Users/messi/Applications/ASA Floating.app"),
    Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/asa-floating-app/build/ASA Floating.app"),
]
_TALENT_SYNC_MODULE: Any | None = None


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def send_common_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def json_response(handler: BaseHTTPRequestHandler, payload: dict[str, Any], status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    send_common_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, content_type: str = "text/html; charset=utf-8") -> None:
    body = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    send_common_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def binary_response(handler: BaseHTTPRequestHandler, path: Path, content_type: str, filename: str) -> None:
    body = path.read_bytes()
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type or "application/octet-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Content-Disposition", f"inline; filename*=UTF-8''{urllib.parse.quote(filename)}")
    handler.send_header("Access-Control-Allow-Origin", allowed_response_origin(handler))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"请求不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("请求 JSON 必须是对象。")
    return value


def prepare_floating_upload(data: dict[str, Any]) -> dict[str, Any]:
    raw_name = str(data.get("file_name") or "").strip()
    if not raw_name or len(raw_name) > 180 or "\x00" in raw_name:
        raise ValueError("附件名称为空或过长。")
    file_name = Path(raw_name).name
    if file_name != raw_name or file_name in {".", ".."}:
        raise ValueError("附件名称不合法。")
    suffix = Path(file_name).suffix.lower()
    supported = SUPPORTED_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS
    if suffix not in supported:
        allowed = "、".join(sorted(ext.lstrip(".") for ext in supported))
        raise ValueError(f"暂不支持 {suffix or '无扩展名'} 文件；支持：{allowed}。")

    encoded = str(data.get("content_base64") or "").strip()
    if not encoded or len(encoded) > ASA_UPLOAD_MAX_BASE64_CHARS:
        raise ValueError("附件为空或超过 25 MB 上限。")
    try:
        content = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("附件内容不是有效 Base64。") from exc
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("附件为空或超过 25 MB 上限。")

    attachment_id = f"att_{secrets.token_hex(8)}"
    result: dict[str, Any] = {
        "attachment_id": attachment_id,
        "file_name": file_name,
        "file_type": suffix.lstrip("."),
        "mime_type": clean(data.get("mime_type"))[:120],
        "size_bytes": len(content),
        "content_available": False,
        "extracted_text": "",
        "truncated": False,
        "status": "等待本机图片识别。" if suffix in SUPPORTED_IMAGE_EXTENSIONS else "正在读取附件。",
        "is_image": suffix in SUPPORTED_IMAGE_EXTENSIONS,
    }
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return {"ok": True, "attachment": result}

    ASA_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{attachment_id}-", dir=ASA_UPLOAD_ROOT) as temp_dir:
        path = Path(temp_dir) / file_name
        path.write_bytes(content)
        extracted_text, truncated = extract_local_document(path)
    result.update(
        {
            "content_available": bool(extracted_text),
            "extracted_text": extracted_text,
            "truncated": truncated,
            "status": "已读取附件正文。" if extracted_text else "附件没有可提取文本。",
        }
    )
    return {"ok": True, "attachment": result}


def sourcing_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    # A 系统以本地 file:// 页面运行，浏览器会发送 Origin: null。
    # 拒绝普通网页来源，避免网站通过 localhost 跨域启动 Codex agent。
    return clean(handler.headers.get("Origin")) in {"", "null"}


def candidate_assistant_origin_decision(origin: str) -> str:
    origin = clean(origin)
    if origin in {"", "null"}:
        return "allow"
    parsed = urllib.parse.urlparse(origin)
    host = clean(parsed.hostname).lower()
    allowed = parsed.scheme in {"http", "https"} and (
        host == "headhunt.x-saas.com.cn" or host == "liepin.com" or host.endswith(".liepin.com")
    )
    return "allow" if allowed else "deny"


def candidate_assistant_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    return candidate_assistant_origin_decision(handler.headers.get("Origin", "")) == "allow"


def agent_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    origin = clean(handler.headers.get("Origin"))
    if origin in {"", "null", "file://"}:
        return True
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme == "file" and not parsed.hostname:
        return True
    return parsed.scheme in {"http", "https"} and clean(parsed.hostname).lower() in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def ensure_effective_candidate_events_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_event_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_event_id INTEGER NOT NULL UNIQUE,
            correction_event_id INTEGER,
            reason TEXT NOT NULL,
            corrected_by TEXT DEFAULT 'local_user',
            corrected_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS v_effective_candidate_events AS
        SELECT ce.*
        FROM candidate_events ce
        LEFT JOIN candidate_event_corrections correction
          ON correction.original_event_id = ce.id
        WHERE correction.original_event_id IS NULL
          AND LOWER(COALESCE(ce.event_status, ''))
              NOT IN ('undone', 'void', 'invalid', 'retracted')
        """
    )


def talent_sync_module():
    global _TALENT_SYNC_MODULE
    if _TALENT_SYNC_MODULE is not None:
        return _TALENT_SYNC_MODULE
    spec = importlib.util.spec_from_file_location("a_system_talent_sync_runtime", TALENT_SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载统一同步引擎：{TALENT_SYNC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TALENT_SYNC_MODULE = module
    return module


def api_error(handler: BaseHTTPRequestHandler, exc: Exception, status: int = 400) -> None:
    json_response(handler, {"ok": False, "error": str(exc)}, status)


def make_namespace(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def first_present(data: dict[str, Any], key: str, default: Any = None) -> Any:
    value = data.get(key, default)
    if isinstance(value, str):
        return clean(value)
    return value


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return clean(value).lower() in {"1", "true", "yes", "y", "write", "confirm", "confirmed"}
    return bool(value)


def refresh_a_system_workbench() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(TALENT_WORKBENCH_BUILD)],
        cwd=str(TALENT_WORKBENCH_BUILD.parent),
        capture_output=True,
        text=True,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def bridge_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    return agent_origin_allowed(handler) or candidate_assistant_origin_allowed(handler)


def sanitize_bridge_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return ""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = clean(value)
        return text[:4000]
    if isinstance(value, list):
        return [sanitize_bridge_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            cleaned[clean(str(key))[:80]] = sanitize_bridge_value(item, depth + 1)
        return cleaned
    return clean(str(value))[:1000]


def normalize_bridge_surface(value: Any) -> str:
    surface = clean(str(value or "")).lower().replace("-", "_")
    if surface in {"liepin", "xsaas", "a_system", "native"}:
        return surface
    if surface in {"x_saas", "xsass", "xsaas_candidate"}:
        return "xsaas"
    return "unknown"


def is_asa_floating_native_context(data: dict[str, Any]) -> bool:
    frontmost_app = data.get("frontmost_app") if isinstance(data.get("frontmost_app"), dict) else {}
    return clean(frontmost_app.get("bundle_id")).lower() == "local.asa.floating"


def native_context_has_wechat_text(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    if normalize_bridge_surface(context.get("surface")) != "native":
        return False
    wechat = context.get("wechat") if isinstance(context.get("wechat"), dict) else {}
    if not wechat:
        return False
    visible_clean = clean(wechat.get("visible_text_clean"))
    combined = clean(wechat.get("combined_text"))
    blocks = wechat.get("text_blocks") if isinstance(wechat.get("text_blocks"), list) else []
    return bool(visible_clean or combined or blocks)


def native_context_is_control_surface(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    if normalize_bridge_surface(context.get("surface")) != "native" or native_context_has_wechat_text(context):
        return False
    app = context.get("frontmost_app") if isinstance(context.get("frontmost_app"), dict) else {}
    bundle = clean(app.get("bundle_id")).lower()
    name = clean(app.get("name")).lower()
    return (
        bundle in {"local.asa.floating", "com.openai.codex", "com.openai.chatgpt"}
        or name == "chatgpt"
        or "codex" in name
    )


def preserve_native_invocation_trigger(
    current: dict[str, Any], incoming: dict[str, Any], now: datetime
) -> str:
    incoming_trigger = clean(incoming.get("trigger")).lower()
    current_trigger = clean(current.get("trigger")).lower()
    if incoming_trigger != "timer" or current_trigger not in {"activation", "hotkey", "toggle", "show", "image_action"}:
        return incoming_trigger
    if floating_context_age(now, current) > floating_context_stale_after(current):
        return incoming_trigger
    current_app = current.get("frontmost_app") if isinstance(current.get("frontmost_app"), dict) else {}
    incoming_app = incoming.get("frontmost_app") if isinstance(incoming.get("frontmost_app"), dict) else {}
    current_bundle = clean(current_app.get("bundle_id")).lower()
    incoming_bundle = clean(incoming_app.get("bundle_id")).lower()
    if current_bundle and current_bundle == incoming_bundle:
        return current_trigger
    return incoming_trigger


def update_floating_context(data: dict[str, Any]) -> dict[str, Any]:
    surface = normalize_bridge_surface(data.get("surface"))
    if surface == "unknown":
        raise ValueError("未知桥接 surface")
    if surface == "native" and is_asa_floating_native_context(data):
        with ASA_FLOATING_LOCK:
            current = dict(ASA_FLOATING_CONTEXTS.get("native") or {})
        return {"ok": True, "ignored": True, "reason": "asa_self_context", "context": current}
    now = datetime.now().isoformat(timespec="seconds")
    context = sanitize_bridge_value(data)
    if not isinstance(context, dict):
        context = {}
    context["surface"] = surface
    context["updated_at"] = now
    context["connected"] = True
    instance_id = clean(context.get("instance_id"))
    context_key = surface
    if surface == "native" and native_context_has_wechat_text(context):
        context_key = "native:wechat"
    if surface in {"liepin", "xsaas"} and instance_id:
        context_key = f"{surface}:{instance_id[:80]}"
    context["context_key"] = context_key
    with ASA_FLOATING_LOCK:
        if surface == "native":
            current = ASA_FLOATING_CONTEXTS.get(context_key) or ASA_FLOATING_CONTEXTS.get("native") or {}
            context["trigger"] = preserve_native_invocation_trigger(current, context, datetime.now())
        ASA_FLOATING_CONTEXTS[context_key] = context
    return {"ok": True, "context": context}


def enqueue_floating_command(data: dict[str, Any]) -> dict[str, Any]:
    surface = normalize_bridge_surface(data.get("surface"))
    if surface == "unknown":
        raise ValueError("未知命令 surface")
    action = clean(data.get("action") or data.get("command"))
    if not action:
        raise ValueError("缺少命令 action")
    command = {
        "id": f"cmd_{secrets.token_hex(8)}",
        "surface": surface,
        "action": action,
        "target_instance_id": clean(data.get("target_instance_id")),
        "payload": sanitize_bridge_value(data.get("payload") if isinstance(data.get("payload"), dict) else {}),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
    }
    with ASA_FLOATING_LOCK:
        ASA_FLOATING_COMMANDS.setdefault(surface, []).append(command)
        ASA_FLOATING_COMMAND_HISTORY.insert(0, command)
        del ASA_FLOATING_COMMAND_HISTORY[100:]
    return {"ok": True, "command": command}


def drain_floating_commands(surface: str, instance_id: str = "") -> dict[str, Any]:
    surface = normalize_bridge_surface(surface)
    if surface == "unknown":
        raise ValueError("未知命令 surface")
    instance_id = clean(instance_id)
    with ASA_FLOATING_LOCK:
        commands = ASA_FLOATING_COMMANDS.setdefault(surface, [])
        deliver: list[dict[str, Any]] = []
        remain: list[dict[str, Any]] = []
        for command in commands:
            target = clean(command.get("target_instance_id"))
            if target and target != instance_id:
                remain.append(command)
            elif target and target == instance_id:
                deliver.append(command)
            elif not target:
                deliver.append(command)
        ASA_FLOATING_COMMANDS[surface] = remain
    return {"ok": True, "surface": surface, "commands": deliver}


def update_floating_command_result(data: dict[str, Any]) -> dict[str, Any]:
    surface = normalize_bridge_surface(data.get("surface"))
    if surface == "unknown":
        raise ValueError("未知命令结果 surface")
    command_id = clean(data.get("command_id") or data.get("id"))
    action = clean(data.get("action") or data.get("command"))
    status = clean(data.get("status")) or "completed"
    message = clean(data.get("message")) or "页面动作已完成。"
    instance_id = clean(data.get("instance_id"))
    result = {
        "id": command_id,
        "surface": surface,
        "action": action,
        "status": status,
        "message": message,
        "result": sanitize_bridge_value(data.get("result") if isinstance(data.get("result"), dict) else {}),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with ASA_FLOATING_LOCK:
        matched = False
        for command in ASA_FLOATING_COMMAND_HISTORY:
            if command_id and command.get("id") == command_id:
                command["status"] = status
                command["message"] = message
                command["result"] = result["result"]
                command["updated_at"] = result["updated_at"]
                matched = True
                break
        if not matched:
            ASA_FLOATING_COMMAND_HISTORY.insert(0, result)
            del ASA_FLOATING_COMMAND_HISTORY[100:]
        ASA_FLOATING_COMMAND_RESULTS.insert(0, result)
        del ASA_FLOATING_COMMAND_RESULTS[100:]
        context_key = f"{surface}:{instance_id[:80]}" if surface in {"liepin", "xsaas"} and instance_id else surface
        if context_key in ASA_FLOATING_CONTEXTS:
            ASA_FLOATING_CONTEXTS[context_key]["last_command_result"] = result
            ASA_FLOATING_CONTEXTS[context_key]["status"] = message
            ASA_FLOATING_CONTEXTS[context_key]["updated_at"] = result["updated_at"]
    return {"ok": True, "command_result": result}


def show_asa_floating_app() -> dict[str, Any]:
    app = next((candidate for candidate in ASA_FLOATING_APP_CANDIDATES if candidate.exists()), None)
    if app is None:
        paths = " / ".join(str(candidate) for candidate in ASA_FLOATING_APP_CANDIDATES)
        return {"ok": False, "error": f"ASA Copilot App 不存在：{paths}"}
    subprocess.Popen(["open", str(app)])
    return {"ok": True, "message": "已唤起 ASA Copilot", "app": str(app)}


def floating_db_rows(db_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"active_goals": [], "pending_approvals": [], "recent_artifacts": []}
    if not db_path.exists():
        return payload
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            payload["active_goals"] = [
                row_dict(row)
                for row in conn.execute(
                    """
                    SELECT g.goal_id,g.objective,g.status,g.priority,g.created_at,g.updated_at,
                           w.workflow_id,w.status AS workflow_status,
                           (SELECT COUNT(*) FROM agent_approvals a WHERE a.goal_id=g.goal_id AND a.status='pending') AS pending_approvals
                    FROM agent_goals g
                    LEFT JOIN agent_workflows w ON w.goal_id=g.goal_id
                    WHERE status NOT IN ('completed','cancelled','superseded')
                      AND (status != 'failed' OR updated_at >= datetime('now','-1 day'))
                    ORDER BY CASE status WHEN 'waiting_approval' THEN 0 WHEN 'running' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,
                             updated_at DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
        except sqlite3.Error:
            payload["active_goals"] = []
        try:
            approvals = []
            for row in conn.execute(
                """
                SELECT approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,
                       preflight_json,created_at,expires_at
                FROM agent_approvals
                WHERE status='pending'
                ORDER BY id DESC
                LIMIT 10
                """
            ).fetchall():
                item = row_dict(row)
                try:
                    item["preflight"] = json.loads(item.pop("preflight_json") or "{}")
                except json.JSONDecodeError:
                    item["preflight"] = {}
                approvals.append(item)
            payload["pending_approvals"] = approvals
        except sqlite3.Error:
            payload["pending_approvals"] = []
        try:
            payload["recent_artifacts"] = [
                row_dict(row)
                for row in conn.execute(
                    """
                    SELECT artifact_id,goal_id,workflow_id,artifact_type,title,mime_type,
                           validation_status,created_at
                    FROM agent_artifacts
                    ORDER BY id DESC
                    LIMIT 8
                    """
                ).fetchall()
            ]
        except sqlite3.Error:
            payload["recent_artifacts"] = []
    finally:
        conn.close()
    return payload


def floating_context_age(now: datetime, context: dict[str, Any]) -> float:
    updated = clean(context.get("updated_at"))
    try:
        return max(0.0, (now - datetime.fromisoformat(updated)).total_seconds())
    except ValueError:
        return 999999.0


def find_nested_value(value: Any, keys: set[str], depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if clean(str(key)).lower() in keys and item not in (None, ""):
                return item
        for item in value.values():
            found = find_nested_value(item, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value[:20]:
            found = find_nested_value(item, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def floating_context_job_candidate_id(context: dict[str, Any] | None) -> int | None:
    if not context:
        return None
    nested_context = context.get("context") if isinstance(context.get("context"), dict) else {}
    if clean(nested_context.get("type")) == "candidate":
        parsed = parse_optional_int(nested_context.get("id"))
        if parsed:
            return parsed
    parsed = parse_optional_int(
        find_nested_value(
            context,
            {"job_candidate_id", "jobcandidateid", "job_candidateid", "relation_id", "progress_id"},
        )
    )
    return parsed


def floating_context_is_stopped(context: dict[str, Any] | None) -> bool:
    if not context:
        return False
    lookup = context.get("talent_lookup") if isinstance(context.get("talent_lookup"), dict) else {}
    match = lookup.get("match") if isinstance(lookup.get("match"), dict) else {}
    text = " ".join(
        clean(value)
        for value in [
            context.get("status"),
            match.get("clean_stage"),
            match.get("progress_stage"),
            match.get("latest_review_status"),
            match.get("latest_review_summary"),
        ]
        if clean(value)
    ).lower()
    return bool(
        text.startswith("h5 ")
        or "h5 最近寻访" in text
        or "初筛不通过" in text
        or "停止推进" in text
        or "screen_rejected" in text
        or "rejected" in text
        or re.search(r"\bstop\b", text)
    )


def floating_context_label(context: dict[str, Any] | None) -> str:
    if not context:
        return "通用 ASA"
    surface = normalize_bridge_surface(context.get("surface"))
    surface_label = {"liepin": "猎聘", "xsaas": "X-SaaS", "a_system": "A 系统"}.get(surface, "通用")
    nested_context = context.get("context") if isinstance(context.get("context"), dict) else {}
    candidate = context.get("candidate") if isinstance(context.get("candidate"), dict) else {}
    label = (
        clean(nested_context.get("label"))
        or clean(candidate.get("name"))
        or clean(context.get("candidate_name"))
        or clean(context.get("name"))
        or clean(context.get("title"))
        or "当前页面"
    )
    subtitle = (
        clean(nested_context.get("subtitle"))
        or " / ".join([item for item in [clean(context.get("client")), clean(context.get("job"))] if item])
        or " · ".join([item for item in [clean(candidate.get("company") or context.get("company")), clean(candidate.get("title") or context.get("candidate_title"))] if item])
    )
    return " · ".join([item for item in [surface_label, label, subtitle] if item])


def compact_floating_text(value: Any, limit: int = 72) -> str:
    text = clean(value).replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "手机号已脱敏", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "邮箱已脱敏", text)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def usable_context_fragment(value: Any, limit: int = 80) -> str:
    text = compact_floating_text(value, limit + 1)
    if not text or len(text) > limit:
        return ""
    return text


def floating_context_stale_after(context: dict[str, Any]) -> int:
    surface = normalize_bridge_surface(context.get("surface"))
    if native_context_has_wechat_text(context):
        return 180
    if surface == "a_system" and (truthy(context.get("explicit")) or truthy(context.get("user_selected"))):
        return 120
    trigger = clean(context.get("trigger")).lower()
    if surface == "native" and trigger in {"hotkey", "toggle", "show", "image_action"}:
        return 180
    return 20


def native_context_is_browser_window(context: dict[str, Any] | None) -> bool:
    if not context or normalize_bridge_surface(context.get("surface")) != "native":
        return False
    if native_context_has_wechat_text(context) or native_context_is_control_surface(context):
        return False
    app = context.get("frontmost_app") if isinstance(context.get("frontmost_app"), dict) else {}
    bundle = clean(app.get("bundle_id")).lower()
    name = clean(app.get("name")).lower()
    return bool(
        bundle in {
            "com.google.chrome",
            "com.google.chrome.canary",
            "com.apple.safari",
            "com.microsoft.edgemac",
            "company.thebrowser.browser",
        }
        or any(token in name for token in ("chrome", "safari", "edge", "arc"))
    )


def select_floating_active_context(contexts: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    has_recent_wechat = any(
        native_context_has_wechat_text(context)
        and floating_context_age(now, context) <= floating_context_stale_after(context)
        for context in contexts.values()
    )
    recent_browser_windows = [
        context
        for context in contexts.values()
        if native_context_is_browser_window(context) and floating_context_age(now, context) <= 180
    ]
    recent_wechat_contexts = [
        context
        for context in contexts.values()
        if native_context_has_wechat_text(context) and floating_context_age(now, context) <= 180
    ]
    latest_browser_age = min((floating_context_age(now, context) for context in recent_browser_windows), default=9999)
    latest_wechat_age = min((floating_context_age(now, context) for context in recent_wechat_contexts), default=9999)
    latest_wechat = min(recent_wechat_contexts, key=lambda context: floating_context_age(now, context), default={})
    latest_wechat_trigger = clean(latest_wechat.get("trigger")).lower()
    has_fresh_visible_browser_bridge = any(
        normalize_bridge_surface(context.get("surface") or key.split(":", 1)[0]) in {"liepin", "xsaas"}
        and truthy(context.get("page_visible"))
        and floating_context_age(now, context) <= floating_context_stale_after(context)
        for key, context in contexts.items()
    )
    browser_bridge_preferred = bool(
        recent_browser_windows
        and has_fresh_visible_browser_bridge
        and (
            latest_browser_age <= latest_wechat_age + 2
            or latest_wechat_trigger not in {"activation", "hotkey", "toggle", "show", "image_action"}
        )
    )
    candidates = []
    for surface, context in contexts.items():
        context_surface = normalize_bridge_surface(context.get("surface") or surface.split(":", 1)[0])
        age = floating_context_age(now, context)
        stale = age > floating_context_stale_after(context)
        explicit = truthy(context.get("explicit")) or truthy(context.get("user_selected"))
        trigger = clean(context.get("trigger")).lower()
        native_invocation = (
            context_surface == "native"
            and trigger in {"hotkey", "toggle", "show", "image_action"}
        )
        focused = truthy(context.get("page_focused"))
        visible = truthy(context.get("page_visible"))
        browser_surface = context_surface in {"liepin", "xsaas"}
        wechat_native = native_context_has_wechat_text(context)
        generic_native = context_surface == "native" and not wechat_native
        control_native = native_context_is_control_surface(context)
        explicit_workbench = context_surface == "a_system" and explicit
        score = (
            (500 if focused else 0)
            + (180 if visible else 0)
            + (300 if explicit else 0)
            + (1000 if explicit_workbench else 0)
            + (1000 if native_invocation else 0)
            + (420 if wechat_native else 0)
            + (120 if browser_surface else 80 if surface == "a_system" else 0)
            - min(age, 120)
        )
        if has_recent_wechat and generic_native and trigger != "hotkey":
            score -= 900
        if has_recent_wechat and control_native:
            score -= 1600
        if browser_bridge_preferred:
            if browser_surface and visible:
                score += 1000
            elif wechat_native:
                score -= 1200
            elif generic_native:
                score -= 500
            elif explicit_workbench:
                # 存在新鲜可见的猎聘/X-SaaS 页面时，取消 a_system explicit 的 +1000 加分，
                # 避免过期的 A 系统点击长期压住当前浏览器页面。
                score -= 1000
        if not stale:
            score += 80
        else:
            score -= 1000
        candidates.append((score, context))
    if not candidates:
        return None
    return dict(max(candidates, key=lambda item: item[0])[1])


def aggregate_floating_contexts_by_surface(contexts: dict[str, dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for key, context in contexts.items():
        surface = normalize_bridge_surface(context.get("surface") or key.split(":", 1)[0])
        if surface == "unknown":
            continue
        grouped.setdefault(surface, {})[key] = context
    aggregated: dict[str, dict[str, Any]] = {}
    for surface, items in grouped.items():
        chosen = select_floating_active_context(items, now)
        if chosen:
            aggregated[surface] = chosen
    return aggregated


def build_floating_active_payload(active_context: dict[str, Any] | None) -> dict[str, Any]:
    if not active_context:
        return {
            "surface": "global",
            "source_label": "通用",
            "type": "global",
            "id": None,
            "title": "通用 ASA 对话",
            "subtitle": "未连接 A 系统、猎聘或 X-SaaS 页面",
            "status": "未连接页面",
            "connected": False,
            "job_candidate_id": None,
        }
    surface = normalize_bridge_surface(active_context.get("surface"))
    nested_context = active_context.get("context") if isinstance(active_context.get("context"), dict) else {}
    candidate = active_context.get("candidate") if isinstance(active_context.get("candidate"), dict) else {}
    if surface == "native":
        app = active_context.get("frontmost_app") if isinstance(active_context.get("frontmost_app"), dict) else {}
        window = active_context.get("window") if isinstance(active_context.get("window"), dict) else {}
        clipboard = active_context.get("clipboard") if isinstance(active_context.get("clipboard"), dict) else {}
        wechat = active_context.get("wechat") if isinstance(active_context.get("wechat"), dict) else {}
        if wechat:
            blocks = wechat.get("text_blocks") if isinstance(wechat.get("text_blocks"), list) else []
            visible_text = (
                clean(wechat.get("visible_text_clean"))
                or clean(wechat.get("combined_text"))
                or " ".join(clean(item) for item in blocks[:12] if clean(item))
            )
            quality = wechat.get("ocr_quality") if isinstance(wechat.get("ocr_quality"), dict) else {}
            quality_label = clean(quality.get("quality"))
            title = clean(wechat.get("window_title")) or clean(window.get("title")) or "微信当前对话"
            if title in {"微信", "WeChat"}:
                title = "微信当前对话"
            status = "低置信" if quality_label in {"none", "low"} else "已识别"
            subtitle_parts = [
                usable_context_fragment(wechat.get("status") or active_context.get("status"), 34),
                usable_context_fragment(visible_text, 72),
            ]
            return {
                "surface": "native",
                "source_label": "微信",
                "type": "wechat",
                "id": clean(app.get("bundle_id")) or None,
                "title": compact_floating_text(title, 40),
                "subtitle": compact_floating_text(" · ".join(item for item in subtitle_parts if item), 92),
                "status": status,
                "confidence": quality_label or None,
                "connected": not truthy(active_context.get("stale")),
                "stopped": False,
                "job_candidate_id": None,
                "updated_at": clean(active_context.get("updated_at")),
                "age_seconds": active_context.get("age_seconds"),
            }
        title = clean(window.get("title")) or clean(app.get("name")) or "当前 macOS 上下文"
        subtitle = " · ".join(
            item for item in [
                usable_context_fragment(app.get("name"), 36),
                usable_context_fragment(clipboard.get("preview"), 52),
            ]
            if item
        )
        return {
            "surface": "native",
            "source_label": "macOS",
            "type": "system",
            "id": clean(app.get("bundle_id")) or None,
            "title": compact_floating_text(title, 40),
            "subtitle": compact_floating_text(subtitle or "系统上下文已同步", 72),
            "status": "已同步",
            "connected": not truthy(active_context.get("stale")),
            "stopped": False,
            "job_candidate_id": None,
            "updated_at": clean(active_context.get("updated_at")),
            "age_seconds": active_context.get("age_seconds"),
        }
    title = (
        clean(nested_context.get("label"))
        or clean(candidate.get("name"))
        or clean(active_context.get("candidate_name"))
        or clean(active_context.get("name"))
        or clean(active_context.get("title"))
        or "当前页面"
    )
    company = usable_context_fragment(candidate.get("company") or active_context.get("company"), 36)
    role = usable_context_fragment(candidate.get("title") or active_context.get("candidate_title"), 42)
    subtitle = (
        usable_context_fragment(nested_context.get("subtitle"), 72)
        or " / ".join([item for item in [usable_context_fragment(active_context.get("client"), 32), usable_context_fragment(active_context.get("job"), 36)] if item])
        or " · ".join([item for item in [company, role] if item])
    )
    job_candidate_id = floating_context_job_candidate_id(active_context)
    connected = not truthy(active_context.get("stale"))
    stopped = floating_context_is_stopped(active_context)
    return {
        "surface": surface,
        "source_label": {"liepin": "猎聘", "xsaas": "X-SaaS", "a_system": "A 系统", "native": "macOS"}.get(surface, "通用"),
        "type": clean(nested_context.get("type")) or clean(active_context.get("page_type")) or "page",
        "id": nested_context.get("id"),
        "title": compact_floating_text(title, 40),
        "subtitle": compact_floating_text(subtitle, 72),
        "status": "已停止" if connected and stopped else "已同步" if connected else "页面桥离线",
        "connected": connected,
        "stopped": stopped,
        "job_candidate_id": job_candidate_id,
        "updated_at": clean(active_context.get("updated_at")),
        "age_seconds": active_context.get("age_seconds"),
    }


def build_floating_suggested_actions(active_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not active_context:
        return [
            {"id": "open_workbench", "title": "打开 A 系统", "detail": "进入 ASA 总览后会自动同步当前工作上下文。", "kind": "local"},
            {"id": "new_goal", "title": "描述一个目标", "detail": "例如：推进今天所有正向回复。", "kind": "prompt"},
        ]
    surface = normalize_bridge_surface(active_context.get("surface"))
    job_candidate_id = floating_context_job_candidate_id(active_context)
    if job_candidate_id:
        if floating_context_is_stopped(active_context):
            return [
                {"id": str(job_candidate_id), "type": "open_candidate", "title": "打开人选", "detail": "查看停止原因、历史记录和人工纠正入口。", "kind": "local"},
                {"id": "refresh_bridge", "title": "刷新页面识别", "detail": "重新读取当前猎聘/X-SaaS 页面状态。", "kind": "bridge"},
            ]
        return [
            {"id": str(job_candidate_id), "type": "open_candidate", "title": "打开人选", "detail": "进入 A 系统人选详情。", "kind": "local"},
            {"id": "assess_current", "title": "评估当前人选", "detail": "直接调用 ASA 评估当前人岗关系。", "kind": "asa"},
            {"id": "generate_report", "title": "生成推荐报告", "detail": "创建推荐报告工作流，产物挂到人选资料。", "kind": "workflow"},
        ]
    if surface == "liepin":
        return [
            {"id": "refresh_bridge", "title": "刷新页面识别", "detail": "让猎聘桥接层重新读取当前简历或会话。", "kind": "bridge"},
            {"id": "fill_resume", "title": "补全简历并定位", "detail": "先采集页面证据，再由 ASA 入库/评估。", "kind": "bridge"},
            {"id": "open_source", "title": "回到猎聘页面", "detail": "打开当前猎聘源页面。", "kind": "bridge"},
        ]
    if surface == "xsaas":
        return [
            {"id": "refresh_bridge", "title": "刷新 X-SaaS 定位", "detail": "重新匹配 A 系统人才和岗位关系。", "kind": "bridge"},
            {"id": "dry-intake", "title": "入库预检", "detail": "只做写入前检查，不直接写库。", "kind": "bridge"},
            {"id": "dry-continue", "title": "推进预检", "detail": "验证能否推进到待人工联系。", "kind": "bridge"},
        ]
    if surface == "native":
        return [
            {"id": "new_goal", "title": "基于当前窗口建目标", "detail": "ASA 会把 macOS 前台窗口和剪贴板作为上下文。", "kind": "prompt"},
            {"id": "open_workbench", "title": "打开 A 系统", "detail": "回到猎头工作台核对任务状态。", "kind": "local"},
        ]
    return [
        {"id": "open_workbench", "title": "打开当前工作区", "detail": "回到 A 系统当前对象。", "kind": "local"},
        {"id": "new_goal", "title": "创建 ASA 目标", "detail": "让 ASA 按当前页面生成执行计划。", "kind": "prompt"},
    ]


def floating_context_source_summary(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {"surface": "global", "source_label": "通用", "type": "global", "connected": False}
    surface = normalize_bridge_surface(context.get("surface"))
    payload = build_floating_active_payload(context)
    return {
        "surface": payload.get("surface") or surface,
        "source_label": payload.get("source_label") or {"native": "macOS"}.get(surface, "通用"),
        "type": payload.get("type") or clean(context.get("page_type")) or "page",
        "title": payload.get("title") or floating_context_label(context),
        "subtitle": payload.get("subtitle") or "",
        "connected": bool(payload.get("connected")),
        "updated_at": payload.get("updated_at") or clean(context.get("updated_at")),
        "age_seconds": payload.get("age_seconds") if payload.get("age_seconds") is not None else context.get("age_seconds"),
    }


def floating_wechat_attachment_status(wechat: dict[str, Any]) -> dict[str, Any]:
    names = visible_attachment_names(wechat) if wechat else []
    return {
        "scope": "explicit_visible_filename",
        "chat_database_accessed": False,
        "supported_extensions": ["pdf", "docx", "txt", "md", "csv", "xls", "xlsx", "pptx"],
        "visible_filenames": names,
        "available": bool(names),
        "status": "visible_exact_match" if names else "none_detected",
        "summary": f"识别到 {len(names)} 个可精确匹配附件文件名" if names else "当前可见窗口未识别到 pdf/docx/txt 附件文件名",
    }


def floating_wechat_image_status(wechat: dict[str, Any]) -> dict[str, Any]:
    if not wechat:
        return {"status": "not_applicable", "available": False, "confirmation_required": False}
    analysis = wechat.get("image_analysis") if isinstance(wechat.get("image_analysis"), dict) else {}
    if analysis:
        return {
            "status": "recognized",
            "available": True,
            "confirmation_required": False,
            "source": clean(analysis.get("source")) or "opened_current_wechat_image",
            "summary": compact_floating_text(analysis.get("ocr_text") or analysis.get("summary") or "图片已完成本机识别", 80),
        }
    text = " ".join(
        clean(item)
        for item in [
            wechat.get("visible_text_clean"),
            wechat.get("combined_text"),
            " ".join(clean(block) for block in (wechat.get("text_blocks") or [])[:20]) if isinstance(wechat.get("text_blocks"), list) else "",
        ]
        if clean(item)
    )
    has_image_hint = bool(re.search(r"(\[图片\]|图片|照片|截图)", text))
    return {
        "status": "confirmation_required" if has_image_hint else "none_detected",
        "available": False,
        "confirmation_required": has_image_hint,
        "summary": "检测到图片线索，需用户确认打开当前图片后识别" if has_image_hint else "当前可见窗口未识别到图片线索",
    }


def floating_permission_status(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {"status": "unknown", "items": []}
    wechat = context.get("wechat") if isinstance(context.get("wechat"), dict) else {}
    items: list[dict[str, Any]] = []
    for key, label in (
        ("accessibility_authorized", "辅助功能"),
        ("screen_capture_authorized", "屏幕录制"),
    ):
        if key in wechat:
            items.append({"key": key, "label": label, "authorized": bool(wechat.get(key))})
    status = "ok" if items and all(item["authorized"] for item in items) else "needs_attention" if items else "unknown"
    debug = wechat.get("permission_debug") if isinstance(wechat.get("permission_debug"), dict) else {}
    stderr = clean(wechat.get("screencapture_stderr"))
    return {"status": status, "items": items, "debug": debug, "screencapture_stderr": compact_floating_text(stderr, 120)}


def floating_context_quality(active_context: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    source = floating_context_source_summary(active_context)
    if not active_context:
        return {
            **source,
            "quality": "missing",
            "confidence": "none",
            "stale": True,
            "permission_status": {"status": "unknown", "items": []},
            "ocr_quality": {"quality": "none"},
            "attachment_status": {"status": "not_applicable", "available": False},
            "image_status": {"status": "not_applicable", "available": False, "confirmation_required": False},
            "summary": "尚未收到可用桌面或页面上下文",
        }
    age = floating_context_age(now, active_context)
    stale = age > floating_context_stale_after(active_context)
    wechat = active_context.get("wechat") if isinstance(active_context.get("wechat"), dict) else {}
    ocr_quality = wechat.get("ocr_quality") if isinstance(wechat.get("ocr_quality"), dict) else {}
    confidence = clean(ocr_quality.get("quality")) or clean(build_floating_active_payload(active_context).get("confidence")) or ("high" if source.get("connected") else "low")
    permission_status = floating_permission_status(active_context)
    attachment_status = floating_wechat_attachment_status(wechat) if wechat else {"status": "not_applicable", "available": False}
    image_status = floating_wechat_image_status(wechat) if wechat else {"status": "not_applicable", "available": False, "confirmation_required": False}
    if stale:
        quality = "stale"
    elif confidence in {"none", "low"}:
        quality = "low"
    elif permission_status.get("status") == "needs_attention":
        quality = "degraded"
    else:
        quality = "high" if confidence in {"high", "medium"} else "medium"
    return {
        **source,
        "quality": quality,
        "confidence": confidence,
        "stale": stale,
        "age_seconds": round(age),
        "updated_at": clean(active_context.get("updated_at")),
        "permission_status": permission_status,
        "ocr_quality": ocr_quality or {"quality": confidence},
        "attachment_status": attachment_status,
        "image_status": image_status,
        "summary": " · ".join(
            item
            for item in [
                source.get("source_label"),
                "已过期" if stale else "可用",
                f"OCR {confidence}" if native_context_has_wechat_text(active_context) else "",
                "权限需处理" if permission_status.get("status") == "needs_attention" else "",
            ]
            if item
        ),
    }


def floating_context_diagnostics(
    active_context: dict[str, Any] | None,
    contexts: dict[str, dict[str, Any]],
    queues: dict[str, int],
    db_payload: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    quality = floating_context_quality(active_context, now)
    if not active_context:
        diagnostics.append({"level": "warning", "code": "context_missing", "title": "未捕获当前上下文", "detail": "请回到 A 系统、猎聘、X-SaaS 或使用 ASA Copilot 热键同步。"})
    elif quality.get("stale"):
        diagnostics.append({"level": "warning", "code": "context_stale", "title": "上下文可能已过期", "detail": f"最近更新时间距今 {quality.get('age_seconds')} 秒，建议刷新当前窗口。"})
    if native_context_has_wechat_text(active_context) and quality.get("confidence") in {"none", "low"}:
        diagnostics.append({"level": "warning", "code": "ocr_low_confidence", "title": "OCR 置信度偏低", "detail": "微信可见文本可能不完整，执行前请核对窗口内容。"})
    permission = quality.get("permission_status") if isinstance(quality.get("permission_status"), dict) else {}
    for item in permission.get("items") or []:
        if not item.get("authorized"):
            diagnostics.append({"level": "error", "code": f"permission_{item.get('key')}", "title": f"{item.get('label')}权限未授权", "detail": "ASA 只能基于受限上下文工作，请在 macOS 隐私设置中确认授权。"})
    if (quality.get("image_status") or {}).get("confirmation_required"):
        diagnostics.append({"level": "info", "code": "image_confirmation_required", "title": "图片识别需要确认", "detail": "检测到微信图片线索，只有确认打开当前图片后才会做本机识别。"})
    pending = db_payload.get("pending_approvals") or []
    if pending:
        diagnostics.append({"level": "approval", "code": "pending_approvals", "title": f"{len(pending)} 项动作待审批", "detail": "R2/R3 动作会等待审批记录，R4 动作仍永久禁止。"})
    blocked = [
        goal for goal in (db_payload.get("active_goals") or [])
        if clean(goal.get("status") or goal.get("workflow_status")) in {"blocked", "failed"}
    ]
    if blocked:
        diagnostics.append({"level": "workflow", "code": "blocked_goals", "title": f"{len(blocked)} 个目标需要处理", "detail": "进入 ASA 内嵌驾驶舱查看阻塞步骤、证据和下一步。"})
    queued = sum(int(value or 0) for value in queues.values())
    if queued:
        diagnostics.append({"level": "info", "code": "bridge_commands_queued", "title": f"{queued} 条桥接命令排队", "detail": "猎聘/X-SaaS 页面会按实例拉取本机命令。"})
    return diagnostics[:8]


def floating_recent_contexts(contexts: dict[str, dict[str, Any]], now: datetime, limit: int = 6) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key, context in contexts.items():
        payload = build_floating_active_payload(context)
        age = floating_context_age(now, context)
        items.append({
            "key": key,
            "surface": payload.get("surface"),
            "source_label": payload.get("source_label"),
            "type": payload.get("type"),
            "title": payload.get("title"),
            "subtitle": payload.get("subtitle"),
            "status": payload.get("status"),
            "connected": payload.get("connected"),
            "confidence": payload.get("confidence"),
            "updated_at": clean(context.get("updated_at")),
            "age_seconds": round(age),
            "stale": age > floating_context_stale_after(context),
        })
    items.sort(key=lambda item: (item["stale"], item["age_seconds"]))
    return items[: max(1, min(int(limit or 6), 12))]


def build_floating_state(state: "WorkbenchState") -> dict[str, Any]:
    now = datetime.now()
    with ASA_FLOATING_LOCK:
        contexts = {key: dict(value) for key, value in ASA_FLOATING_CONTEXTS.items()}
        queues = {key: len(value) for key, value in ASA_FLOATING_COMMANDS.items()}
        history = list(ASA_FLOATING_COMMAND_HISTORY[:20])
        command_results = list(ASA_FLOATING_COMMAND_RESULTS[:20])
    for context in contexts.values():
        age = floating_context_age(now, context)
        context["stale"] = age > floating_context_stale_after(context)
        context["age_seconds"] = round(age)
    active_context = select_floating_active_context(contexts, now)
    display_contexts = aggregate_floating_contexts_by_surface(contexts, now)
    db_payload = floating_db_rows(state.agent_service.db_path)
    runtime_payload = state.agent_service.get_runtime_timeline(limit=8)
    context_quality = floating_context_quality(active_context, now)
    return {
        "ok": True,
        "server_time": now.isoformat(timespec="seconds"),
        "active_context": build_floating_active_payload(active_context),
        "active_context_raw": active_context or {},
        "context_quality": context_quality,
        "diagnostics": floating_context_diagnostics(active_context, contexts, queues, db_payload, now),
        "recent_contexts": floating_recent_contexts(contexts, now),
        "suggested_actions": build_floating_suggested_actions(active_context),
        "show_suggested_actions": False,
        "bridge": {
            "contexts": display_contexts,
            "context_instances": contexts,
            "command_queues": queues,
            "recent_commands": history,
            "recent_command_results": command_results,
        },
        "runtime": runtime_payload,
        "context_snapshot": (runtime_payload.get("context_snapshots") or [{}])[0],
        **db_payload,
    }


def floating_goal_context(active_context: dict[str, Any] | None) -> dict[str, Any]:
    if not active_context:
        return {"type": "global", "id": None, "source": "asa_floating"}
    nested_context = active_context.get("context") if isinstance(active_context.get("context"), dict) else {}
    job_candidate_id = floating_context_job_candidate_id(active_context)
    if job_candidate_id:
        return {"type": "candidate", "id": job_candidate_id, "source": "asa_floating", "bridge": active_context}
    if nested_context:
        return {
            "type": clean(nested_context.get("type")) or "page",
            "id": nested_context.get("id"),
            "page": nested_context.get("page"),
            "filters": nested_context.get("filters") if isinstance(nested_context.get("filters"), dict) else {},
            "source": "asa_floating",
            "bridge": active_context,
        }
    return {
        "type": normalize_bridge_surface(active_context.get("surface")) or "page",
        "id": None,
        "source": "asa_floating",
        "bridge": active_context,
    }


def route_floating_action(state: "WorkbenchState", data: dict[str, Any]) -> dict[str, Any]:
    action = clean(data.get("action"))
    if not action:
        raise ValueError("缺少浮窗动作")
    snapshot = build_floating_state(state)
    active_raw = snapshot.get("active_context_raw") if isinstance(snapshot.get("active_context_raw"), dict) else {}
    active = snapshot.get("active_context") if isinstance(snapshot.get("active_context"), dict) else {}
    surface = normalize_bridge_surface(active_raw.get("surface") or active.get("surface"))

    def queue_bridge(bridge_action: str) -> dict[str, Any]:
        if surface == "unknown" or surface == "global":
            return {"ok": False, "status": "blocked", "message": "当前没有可执行的页面桥，请先打开猎聘或 X-SaaS 页面。"}
        return {
            **enqueue_floating_command(
                {
                    "surface": surface,
                    "action": bridge_action,
                    "target_instance_id": clean(active_raw.get("instance_id")),
                    "payload": {"context_hint": floating_context_label(active_raw), "requested_by": "asa_floating"},
                }
            ),
            "status": "queued",
            "message": "已发送到当前页面，等待页面执行并回传结果。",
        }

    if action in {"refresh_bridge", "open_source", "dry-intake", "dry-continue", "dry-stop", "copy-current", "identity-match"}:
        return queue_bridge(action)
    if action.startswith("open_wechat_attachment::"):
        filename = clean(action.split("::", 1)[1])
        path = visible_wechat_attachment_path(active_raw, filename)
        if path is None:
            return {"ok": False, "status": "blocked", "message": "当前微信窗口已找不到该附件，请刷新后重试。"}
        subprocess.Popen(
            ["/usr/bin/open", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {
            "ok": True,
            "status": "local",
            "message": f"正在打开“{filename}”，下载完成后 ASA 会自动重新读取。",
            "retry_previous": True,
        }
    job_candidate_id = floating_context_job_candidate_id(active_raw)
    if action == "open_candidate" or action.isdigit():
        target_id = parse_optional_int(data.get("id")) or parse_optional_int(action) or job_candidate_id
        if not target_id:
            return {"ok": False, "status": "blocked", "message": "当前没有可打开的人选关系。"}
        return {
            "ok": True,
            "status": "local",
            "message": f"正在打开 A 系统人选详情：关系 #{target_id}。",
            "open_url": f"/asa-app#candidate={target_id}",
        }
    if action == "open_workbench":
        return {"ok": True, "status": "local", "message": "已准备打开 A 系统。", "open_url": "/asa-app"}
    if action in {"new_goal"}:
        return {"ok": True, "status": "prompt", "message": "请在输入框描述目标，ASA 会生成执行计划。"}
    if action == "open_workflow":
        workflow_id = clean(data.get("workflow_id") or data.get("id"))
        if not workflow_id:
            raise ValueError("缺少 workflow_id")
        summary = state.agent_service.get_workflow_summary(workflow_id)
        return {
            "ok": True,
            "status": "local",
            "message": f"正在打开 ASA 目标计划：{workflow_id}。",
            "open_url": f"/asa-app#workflow={workflow_id}",
            "workflow_id": workflow_id,
            "workflow_summary": summary,
        }

    if action == "assess_current":
        if job_candidate_id:
            result = state.agent_service.submit_assessment(job_candidate_id, force=True, trigger="floating")
            return {
                "ok": True,
                "status": "assessment_started" if result.get("status") != "completed" else "completed",
                "message": "ASA 已开始评估当前人选。" if result.get("status") != "completed" else "当前人选评估已完成。",
                "result": result,
            }
        if surface in {"liepin", "xsaas"}:
            queued = queue_bridge("refresh_bridge")
            queued["message"] = "当前页面还没有唯一人岗关系，已先请求页面重新识别。"
            return queued
        return {"ok": False, "status": "blocked", "message": "请先在人选列表选择一个人选，或在猎聘/X-SaaS 页面完成定位。"}

    objective_by_action = {
        "fill_resume": "补全当前人选简历，定位 A 系统人岗关系并完成 ASA 评估",
        "generate_report": "为当前人选生成候选人匹配分析报告和嘉驰推荐报告，并挂到候选人资料下",
        "draft_outreach": "为当前人选准备猎聘触达草稿，正式发送前保留人工审批",
        "job_publish_prepare": "准备当前岗位的猎聘发布草稿并完成发布前预检",
    }
    if action in objective_by_action:
        if action == "fill_resume" and surface in {"liepin", "xsaas"} and not job_candidate_id:
            queued = queue_bridge("fill_resume")
            queued["message"] = "已发送到猎聘页面，正在采集简历并做入库预检；预检通过后会在猎聘页弹出确认入库。"
            return queued
        workflow = state.agent_service.create_goal(objective_by_action[action], floating_goal_context(active_raw), 2)
        workflow_id = clean(workflow.get("workflow_id") or (workflow.get("workflow") or {}).get("workflow_id"))
        return {
            "ok": True,
            "status": "planned",
            "message": "ASA 已生成执行计划，可在目标与执行区启动。",
            "workflow_id": workflow_id,
            "workflow": workflow,
        }

    if action == "start_workflow":
        workflow_id = clean(data.get("workflow_id") or data.get("id"))
        if not workflow_id:
            raise ValueError("缺少 workflow_id")
        return {
            "ok": True,
            "status": "queued",
            "message": "目标已进入执行队列。",
            "workflow": state.agent_service.start_workflow(workflow_id),
        }
    return {"ok": False, "status": "blocked", "message": f"暂不支持该浮窗动作：{action}"}


def asa_floating_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ASA Copilot</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#f7f8fb; --panel:#ffffff; --panel-soft:#f9fafc; --line:#d8dee8;
      --text:#171d26; --muted:#667085; --soft:#eef2f7; --blue:#1f5eff;
      --green:#087443; --amber:#b54708; --red:#b42318; --shadow:0 10px 30px rgba(15,23,42,.10);
    }
    * { box-sizing:border-box; }
    html, body { width:100%; height:100%; min-height:100%; overflow:hidden; }
    body { margin:0; background:#fff; color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    .app { height:100vh; max-height:100vh; min-height:0; min-width:320px; display:flex; flex-direction:column; overflow:hidden; }
    header { flex:0 0 auto; height:78px; overflow:hidden; padding:9px 12px 8px; border-bottom:1px solid #edf1f7; background:rgba(255,255,255,.98); position:relative; z-index:5; }
    .brand-row { height:32px; display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .brand { display:flex; align-items:center; gap:8px; min-width:0; flex:0 1 auto; }
    .mark { width:26px; height:26px; border-radius:8px; background:#111827; color:#fff; display:grid; place-items:center; font-weight:760; font-size:13px; }
    h1 { margin:0; font-size:14px; letter-spacing:0; white-space:nowrap; }
    .sub { margin-top:1px; color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:245px; }
    .header-actions { display:flex; align-items:center; flex:0 0 auto; gap:5px; }
    .icon-btn { width:28px; height:28px; flex:0 0 28px; border-radius:8px; padding:0; display:grid; place-items:center; font-size:15px; }
    .context-line { height:28px; margin-top:1px; display:grid; grid-template-columns:auto minmax(0,1fr); align-items:center; gap:8px; min-width:0; }
    .context-copy { min-width:0; display:flex; align-items:baseline; gap:6px; overflow:hidden; }
    .object-title { flex:0 1 auto; font-size:12px; font-weight:720; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .object-subtitle { flex:1 1 auto; min-width:0; color:var(--muted); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .pillrow { display:flex; gap:6px; flex-wrap:wrap; }
    .pill { border:1px solid var(--line); background:#fff; border-radius:999px; padding:2px 7px; color:#344054; font-size:11px; line-height:1.45; white-space:nowrap; }
    .pill.ok { color:var(--green); border-color:#abefc6; background:#ecfdf3; }
    .pill.warn { color:var(--amber); border-color:#fedf89; background:#fffaeb; }
    .pill.off { color:#697586; border-color:#e3e8ef; background:#f8fafc; }
    .main { overflow:auto; padding:12px; display:grid; gap:12px; align-content:start; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; box-shadow:0 1px 2px rgba(16,24,40,.04); }
    .sec-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:9px; }
    h2 { margin:0; font-size:13px; color:#111827; }
    button { border:1px solid var(--line); background:#fff; color:#1f2937; border-radius:9px; padding:7px 10px; font:inherit; cursor:pointer; }
    button.primary { background:var(--blue); color:#fff; border-color:var(--blue); }
    button.danger { color:var(--red); border-color:#fecdca; background:#fff5f4; }
    button.ghost { background:transparent; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .grid { display:grid; gap:9px; }
    .item { border:1px solid #edf1f7; border-radius:10px; padding:10px; background:#fff; }
    .item-title { font-weight:650; margin-bottom:4px; }
    .muted { color:var(--muted); font-size:12px; }
    .empty { color:var(--muted); padding:8px 0; }
    .suggestion { display:grid; grid-template-columns:1fr auto; gap:9px; align-items:center; }
    .suggestion strong { display:block; font-size:13px; }
    .suggestion span { color:var(--muted); font-size:12px; }
    .chat-shell { flex:1 1 auto; min-height:0; overflow:hidden; display:flex; flex-direction:column; background:#fff; }
    .chat-shell { position:relative; }
    .context-panels { flex:0 0 auto; display:grid; gap:8px; padding:10px 12px 0; }
    .context-panels.empty { display:none; }
    .context-actions { display:flex; gap:7px; overflow-x:auto; padding-bottom:2px; scrollbar-width:none; }
    .context-actions.empty { display:none; }
    .context-actions::-webkit-scrollbar { display:none; }
    .context-action { flex:0 0 auto; display:grid; gap:1px; min-width:126px; max-width:172px; text-align:left; border-radius:11px; padding:8px 10px; background:#f8fafc; border-color:#e3e8ef; }
    .context-action b { font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .context-action span { color:var(--muted); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .run-summary { display:grid; gap:7px; border:1px solid #e3e8ef; border-radius:12px; background:#fff; padding:9px 10px; }
    .run-summary.compact { display:flex; align-items:center; justify-content:space-between; gap:7px; padding:6px 8px; border-radius:10px; box-shadow:none; }
    .run-summary.empty { display:none; }
    .run-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
    .run-title { font-size:12px; font-weight:760; color:#344054; }
    .run-grid { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:6px; }
    .run-chip { min-width:0; border:1px solid #edf1f7; border-radius:9px; padding:6px 7px; background:#fbfcfe; }
    .run-chip.warn { border-color:#fedf89; background:#fffaeb; }
    .run-chip b { display:block; font-size:13px; }
    .run-chip span { display:block; color:var(--muted); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .diagnostic-strip { min-width:0; display:flex; align-items:center; gap:5px; overflow:hidden; }
    .diag-chip { flex:0 1 auto; min-width:0; max-width:148px; border:1px solid #e3e8ef; border-radius:999px; padding:2px 7px; background:#f8fafc; color:#475467; font-size:11px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .diag-chip.warn, .diag-chip.warning, .diag-chip.approval { border-color:#fedf89; background:#fffaeb; color:var(--amber); }
    .diag-chip.error { border-color:#fecdca; background:#fff5f4; color:var(--red); }
    .diag-chip.ok { border-color:#abefc6; background:#ecfdf3; color:var(--green); }
    .run-summary.compact button { flex:0 0 auto; padding:3px 7px; border-radius:999px; font-size:11px; }
    .snapshot-card, .tool-timeline { display:grid; gap:7px; border:1px solid #e3e8ef; border-radius:12px; background:#fff; padding:9px 10px; }
    .tool-timeline.compact { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:6px 8px; }
    .snapshot-card.empty, .tool-timeline.empty { display:none; }
    .snapshot-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
    .snapshot-title { min-width:0; font-size:12px; font-weight:760; color:#344054; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .snapshot-meta { color:var(--muted); font-size:11px; white-space:nowrap; }
    .snapshot-body { color:#475467; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .tool-list { display:grid; gap:5px; }
    .tool-row { display:grid; grid-template-columns:1fr auto; gap:8px; align-items:center; font-size:12px; }
    .tool-row b { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:650; color:#344054; }
    .tool-row span { color:var(--muted); white-space:nowrap; }
    .tool-row.warn span { color:var(--amber); }
    .tool-row.error span { color:var(--red); }
    .messages { flex:1 1 auto; min-height:0; overflow-x:hidden; overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain; touch-action:pan-y; padding:14px 12px 16px; display:flex; flex-direction:column; gap:12px; scrollbar-gutter:stable; }
    .msg { padding:9px 11px; border-radius:14px; word-break:break-word; line-height:1.56; }
    .msg.user { background:#f4f4f5; align-self:flex-end; max-width:86%; }
    .msg.assistant { background:transparent; border:0; align-self:stretch; max-width:100%; padding:0; overflow:visible; }
    .msg-body { display:grid; gap:9px; padding:0 2px; }
    .msg-body p { margin:0; }
    .msg-body strong { color:#111827; font-weight:720; }
    .msg-body code { border-radius:5px; background:#eef2f7; padding:1px 4px; color:#244034; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
    .msg-body ol, .msg-body ul { margin:0; padding-left:20px; }
    .msg-body li { margin:5px 0; padding-left:1px; }
    .msg-section { display:grid; gap:6px; padding:0; background:transparent; border:0; border-radius:0; box-shadow:none; }
    .msg-section + .msg-section { padding-top:9px; border-top:1px solid #eef2f7; }
    .msg-section:first-child { padding-top:0; border-top:0; }
    .msg-section h3 { display:flex; align-items:center; gap:6px; margin:0; color:#334155; font-size:12px; font-weight:760; }
    .msg-section.generic h3 { display:none; }
    .msg-section h3:before { content:""; width:3px; height:13px; border-radius:99px; background:#256f5b; }
    .msg-section.conclusion h3:before { background:#0f62fe; }
    .msg-section.next h3:before { background:#2f6f5e; }
    .msg-section.evidence h3:before { background:#8a6d3b; }
    details.msg-section { display:block; }
    details.msg-section > summary { display:flex; align-items:center; gap:6px; width:max-content; max-width:100%; padding:4px 8px; border:1px solid #e3e8ef; border-radius:999px; color:#475467; background:#f8fafc; font-size:12px; font-weight:650; cursor:pointer; }
    details.msg-section > summary::-webkit-details-marker { display:none; }
    details.msg-section > summary:before { content:""; width:3px; height:12px; border-radius:99px; background:#8a6d3b; }
    .msg-detail-body { display:grid; gap:6px; margin-top:8px; padding:9px 10px; border:1px solid #edf1f7; border-radius:10px; background:#fbfcfe; color:#344054; font-size:13px; }
    .thinking { display:flex; align-items:center; gap:9px; padding:11px 12px; color:#475467; font-size:13px; }
    .thinking-dots { display:flex; align-items:center; gap:4px; }
    .thinking-dots i { width:6px; height:6px; border-radius:999px; background:#667085; opacity:.35; animation:thinkingPulse 1.05s infinite ease-in-out; }
    .thinking-dots i:nth-child(2) { animation-delay:.15s; }
    .thinking-dots i:nth-child(3) { animation-delay:.3s; }
    @keyframes thinkingPulse { 0%, 80%, 100% { transform:translateY(0); opacity:.35; } 40% { transform:translateY(-3px); opacity:1; } }
    .msg-actions { display:flex; gap:7px; flex-wrap:wrap; margin:8px 2px 0; }
    .msg-action { border-color:#d0d7e2; background:#fff; border-radius:999px; padding:5px 9px; font-size:12px; color:#1f2937; }
    .msg-action:hover { background:#f8fafc; }
    .goal-card { border:1px solid #d6e3ff; background:#f5f8ff; border-radius:12px; padding:10px; display:grid; gap:6px; }
    .composer { flex:0 0 auto; min-height:0; padding:10px 12px 12px; background:#fff; border-top:1px solid #edf1f7; display:grid; gap:7px; }
    .attachment-list { display:flex; gap:6px; flex-wrap:wrap; min-height:0; }
    .attachment-list.empty { display:none; }
    .attachment-chip { max-width:100%; min-height:30px; display:grid; grid-template-columns:18px minmax(0,1fr) 24px; align-items:center; gap:6px; border:1px solid #d9e1eb; border-radius:8px; padding:4px 4px 4px 7px; background:#f8fafc; color:#344054; font-size:12px; }
    .attachment-chip.pending { border-color:#bfd3ff; background:#f5f8ff; }
    .attachment-chip.error { border-color:#f3b7b7; background:#fff7f7; color:#9b1c1c; }
    .attachment-kind { color:#667085; font-weight:750; text-transform:uppercase; }
    .attachment-copy { min-width:0; display:grid; gap:1px; }
    .attachment-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:650; }
    .attachment-status { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#667085; font-size:11px; }
    .attachment-remove { width:24px; height:24px; border:0; border-radius:6px; padding:0; background:transparent; color:#667085; font-size:17px; }
    .attachment-remove:hover { background:#e9eef5; color:#111827; }
    .composer-box { border:1px solid #d0d7e2; border-radius:14px; display:grid; grid-template-columns:auto minmax(0,1fr) auto; gap:8px; align-items:end; padding:8px; background:#fff; box-shadow:0 1px 2px rgba(16,24,40,.04); }
    textarea { width:100%; min-height:36px; max-height:110px; resize:none; border:0; padding:2px 0; font:inherit; outline:none; }
    .attach { width:32px; height:32px; border-radius:8px; padding:0; border-color:transparent; background:transparent; color:#475467; font-size:20px; line-height:1; }
    .attach:hover { background:#f1f4f8; border-color:#e2e8f0; }
    .attach:disabled { opacity:.4; cursor:not-allowed; }
    .send { width:32px; height:32px; border-radius:9px; padding:0; background:#111827; color:#fff; border-color:#111827; font-size:18px; line-height:1; }
    .send:disabled { opacity:.45; cursor:wait; }
    .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    details.drawer { background:transparent; border:0; padding:0; box-shadow:none; }
    details.drawer > summary { list-style:none; cursor:pointer; background:#fff; border:1px solid var(--line); border-radius:12px; padding:10px 12px; display:flex; align-items:center; justify-content:space-between; }
    details.drawer > summary::-webkit-details-marker { display:none; }
    details.drawer[open] > summary { border-radius:12px 12px 0 0; }
    .drawer-body { border:1px solid var(--line); border-top:0; background:#fff; border-radius:0 0 12px 12px; padding:10px; display:grid; gap:10px; max-height:310px; overflow:auto; }
    .approval pre { white-space:pre-wrap; word-break:break-word; max-height:140px; overflow:auto; background:#f8fafc; border:1px solid #edf1f7; border-radius:8px; padding:8px; font-size:12px; }
    .statusline { min-height:18px; color:var(--muted); font-size:12px; }
    .history-panel { position:absolute; left:10px; right:10px; top:10px; max-height:68%; overflow:auto; z-index:8; display:none; background:#fff; border:1px solid var(--line); border-radius:13px; box-shadow:var(--shadow); padding:10px; }
    .history-panel.open { display:grid; gap:8px; }
    .history-head { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:2px 2px 7px; border-bottom:1px solid var(--line); }
    .history-list { display:grid; gap:7px; }
    .history-item { width:100%; text-align:left; display:grid; gap:3px; padding:9px; border-radius:10px; }
    .history-item.active { border-color:#b8ccff; background:#f5f8ff; }
    .history-item b { font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .focus-bar { flex:0 0 auto; margin:10px 12px 0; display:flex; align-items:baseline; gap:6px; border:1px solid #d6e3ff; background:#f5f8ff; border-radius:10px; padding:6px 9px; font-size:12px; overflow:hidden; }
    .focus-bar.empty { display:none; }
    .focus-bar .focus-label { flex:0 0 auto; color:#1d4ed8; font-size:11px; font-weight:760; white-space:nowrap; }
    .focus-bar .focus-title { flex:0 1 auto; min-width:0; font-weight:650; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .focus-bar .focus-meta { flex:0 1 auto; min-width:0; color:var(--muted); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .focus-bar.conflict { border-color:#fedf89; background:#fffaeb; }
    .focus-bar.conflict .focus-label { color:var(--amber); }
  </style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand-row">
      <div class="brand">
        <span class="mark">A</span>
        <h1>ASA Copilot</h1>
      </div>
      <div class="header-actions">
        <button class="icon-btn" title="历史对话" onclick="toggleHistory()">⌕</button>
        <button class="icon-btn" title="截图" onclick="startScreenshot()">⌗</button>
        <button class="icon-btn" title="打开 A 系统" onclick="openWorkbench()">↗</button>
        <button class="icon-btn" title="新对话" onclick="newChat()">＋</button>
      </div>
    </div>
    <div class="context-line">
      <span id="syncPill" class="pill off">未连接</span>
      <div class="context-copy">
        <span id="objectTitle" class="object-title">通用 ASA 对话</span>
        <span id="objectSubtitle" class="object-subtitle">未连接业务页面</span>
      </div>
    </div>
    <div id="headline" class="sub" style="display:none">正在连接本机 ASA 服务</div>
  </header>
  <div class="chat-shell">
    <div id="historyPanel" class="history-panel"></div>
    <div id="focusBar" class="focus-bar empty"></div>
    <div class="context-panels">
      <div id="contextActions" class="context-actions"></div>
      <div id="runSummary" class="run-summary empty"></div>
      <div id="contextSnapshot" class="snapshot-card empty"></div>
      <div id="toolTimeline" class="tool-timeline empty"></div>
    </div>
    <div id="messages" class="messages"></div>
    <div class="composer">
      <div id="attachmentList" class="attachment-list empty"></div>
      <div class="composer-box">
        <input id="attachmentInput" type="file" multiple hidden accept=".docx,.pdf,.txt,.md,.csv,.xls,.xlsx,.pptx,.png,.jpg,.jpeg,.webp">
        <button id="attachButton" class="attach" type="button" onclick="chooseAttachments()" title="添加文件或图片" aria-label="添加文件或图片">＋</button>
        <textarea id="input" rows="1" placeholder="告诉 ASA 你要推进的目标..."></textarea>
        <button id="sendButton" class="send" type="button" onclick="sendMessage()" title="发送" aria-label="发送">↑</button>
      </div>
      <div id="chatStatus" class="statusline"></div>
    </div>
  </div>
</div>
<script>
const state = { data:null, sessionId: localStorage.getItem('asaFloatingSession') || `floating_${Math.random().toString(16).slice(2)}`, messages: [], sessions:[], attachments:[], historyOpen:false, historyLoading:false, restored:false, loading:false, requestStartedAt:0, pendingNativeContext:null, businessFocus:null };
localStorage.setItem('asaFloatingSession', state.sessionId);
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function floatingInline(value){
  return esc(String(value || ''))
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}
function sectionClass(label){
  if (/结论|总结/.test(label)) return 'conclusion';
  if (/依据|证据|原因/.test(label)) return 'evidence';
  if (/下一步|建议|行动/.test(label)) return 'next';
  if (/回复|要点/.test(label)) return 'generic';
  return '';
}
function renderFloatingMarkdown(value){
  const lines = String(value || '').replaceAll(String.fromCharCode(13), '').split(String.fromCharCode(10));
  let html = '';
  let listType = '';
  let sectionOpen = false;
  let sectionTag = '';
  const closeList = () => {
    if (!listType) return;
    html += `</${listType}>`;
    listType = '';
  };
  const closeSection = () => {
    closeList();
    if (sectionOpen) html += sectionTag === 'details' ? '</div></details>' : '</section>';
    sectionOpen = false;
    sectionTag = '';
  };
  const openSection = label => {
    closeSection();
    const cls = sectionClass(label);
    if (cls === 'evidence') {
      html += `<details class="msg-section ${cls}"><summary>查看${esc(label)}</summary><div class="msg-detail-body">`;
      sectionTag = 'details';
    } else {
      html += `<section class="msg-section ${cls}"><h3>${esc(label)}</h3>`;
      sectionTag = 'section';
    }
    sectionOpen = true;
  };
  lines.forEach(rawLine => {
    const line = String(rawLine || '').trimEnd();
    const heading = line.match(/^\s*(结论|依据|原因|证据|下一步建议|下一步|建议|行动建议|风险)[:：]\s*(.*)$/);
    if (heading) {
      openSection(heading[1]);
      if (heading[2]) html += `<p>${floatingInline(heading[2])}</p>`;
      return;
    }
    const ordered = line.match(/^[ \t]*[0-9]+[.、][ \t]*(.+)/);
    const bullet = line.match(/^[ \t]*[-*][ \t]+(.+)/);
    if (ordered || bullet) {
      if (!sectionOpen) openSection('要点');
      const nextType = ordered ? 'ol' : 'ul';
      if (listType !== nextType) {
        closeList();
        html += `<${nextType}>`;
        listType = nextType;
      }
      html += `<li>${floatingInline((ordered || bullet)[1])}</li>`;
      return;
    }
    if (!line.trim()) return;
    closeList();
    if (!sectionOpen) openSection('回复');
    html += `<p>${floatingInline(line)}</p>`;
  });
  closeSection();
  return `<div class="msg-body">${html || '<p>暂无回复。</p>'}</div>`;
}
function renderFloatingMessage(message){
  const role = message?.role === 'user' ? 'user' : 'assistant';
  if (role === 'user') return `<div class="msg user">${esc(message?.content || '')}</div>`;
  const actionList = Array.isArray(message?.suggested_actions) ? [...message.suggested_actions] : [];
  const content = String(message?.content || '');
  if (!actionList.length && /补全简历并定位|页面采集|入库预检/.test(content)) {
    actionList.push({type:'floating_action', id:'fill_resume', label:'补全简历并定位'});
  }
  const actions = actionList
    .filter(action => action && (action.type === 'floating_action' || action.type === 'open_candidate' || action.id))
    .filter(action => !(action.type === 'open_candidate' && actionList.length === 1))
    .slice(0, 3)
    .map(action => `<button class="msg-action" data-action-type="${esc(action.type || 'floating_action')}" data-action-id="${esc(action.id || action.action || '')}">${esc(action.label || action.title || '执行动作')}</button>`)
    .join('');
  return `<div class="msg assistant">${renderFloatingMarkdown(message?.content || '')}${actions ? `<div class="msg-actions">${actions}</div>` : ''}</div>`;
}
function renderThinkingMessage(){
  return '<div class="msg assistant"><div class="thinking"><span class="thinking-dots"><i></i><i></i><i></i></span><span>ASA 正在分析当前上下文</span></div></div>';
}
// business_focus 焦点条：与 React Copilot 焦点卡同语义——候选人姓名优先，
// 其次「客户 / 岗位标题」；needs_clarification 为真时切冲突态，提示需要确认对象。
// 数据来自每条 copilot 响应与 /api/agent/copilot/session 恢复响应的 business_focus 字段；
// 响应没有该字段（旧 Core）时保持不渲染。
const FOCUS_ACTION_LABELS = {job_archive:'归档岗位', job_split:'拆分岗位', job_publish:'发布岗位', candidate_sourcing:'寻访人选', candidate_outreach:'触达人选', candidate_review:'复核人选', recommendation:'客户推荐', salary:'谈薪处理'};
function focusBarLabel(focus){
  if (!focus || typeof focus !== 'object') return '';
  return String(focus?.candidate?.name || [focus?.client, focus?.job?.title].filter(Boolean).join(' / ') || focus?.client || '').trim();
}
function renderFocusBar(){
  const node = document.getElementById('focusBar');
  if (!node) return;
  const focus = state.businessFocus;
  const title = focusBarLabel(focus);
  if (!title) {
    node.className = 'focus-bar empty';
    node.innerHTML = '';
    return;
  }
  const conflict = focus?.needs_clarification === true;
  const action = String(focus?.action || '');
  const directions = Array.isArray(focus?.directions) && focus.directions.length ? ` · ${focus.directions.join(' / ')}` : '';
  const meta = action ? `${FOCUS_ACTION_LABELS[action] || action}${directions}` : '';
  node.className = `focus-bar ${conflict ? 'conflict' : ''}`;
  node.innerHTML = `<span class="focus-label">${conflict ? '需要确认' : '当前焦点'}</span><span class="focus-title">${esc(title)}</span>${meta ? `<span class="focus-meta">${esc(meta)}</span>` : ''}`;
}
async function api(path, opts={}){
  const timeoutMs = Number(opts.timeoutMs || 12000);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const requestOpts = {...opts};
  delete requestOpts.timeoutMs;
  try {
    const res = await fetch(path, {headers:{'Content-Type':'application/json'}, signal:controller.signal, ...requestOpts});
    const json = await res.json().catch(() => ({}));
    if(!res.ok || json.ok === false) throw new Error(json.error || `HTTP ${res.status}`);
    return json;
  } catch (err) {
    if (err?.name === 'AbortError') throw new Error('请求超时，请重试');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
// --- asa-floating-copilot-transport ---
// 发送统一走 v1：POST /api/v1/copilot/messages，Idempotency-Key=floating-{sessionId}-{messageHash}
// （FNV-1a 32 位稳定散列，同一 session 重发同一消息派生同一键，服务端 execute_idempotent 去重），
// request_id 同理派生。v1 不可用（网络失败、旧版本无此路由、4xx/5xx）时回退 legacy
// /api/agent/copilot 一次，并在 console 留痕；legacy 端点保持纯转发语义不变。
function floatingMessageHash(text){
  let hash = 0x811c9dc5;
  const value = String(text || '');
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(36);
}
function floatingCopilotIdempotencyKey(sessionId, text){
  return `floating-${sessionId}-${floatingMessageHash(text)}`;
}
function floatingCopilotRequestId(sessionId, text){
  return `floating_req_${sessionId}_${floatingMessageHash(text)}`;
}
async function postCopilotMessage(payload, idempotencyKey, requestId){
  try {
    return await api('/api/v1/copilot/messages', {method:'POST', headers:{'Content-Type':'application/json', 'Idempotency-Key':idempotencyKey}, body:JSON.stringify({...payload, request_id:requestId}), timeoutMs:45000});
  } catch (err) {
    console.warn(`[ASA floating] /api/v1/copilot/messages 不可用（${err?.message || err}），回退 /api/agent/copilot 重试一次`);
    return await api('/api/agent/copilot', {method:'POST', body:JSON.stringify(payload), timeoutMs:45000});
  }
}
// --- end asa-floating-copilot-transport ---
function attachmentSize(bytes){
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
function renderAttachments(){
  const node = document.getElementById('attachmentList');
  if (!node) return;
  node.className = `attachment-list ${state.attachments.length ? '' : 'empty'}`;
  node.innerHTML = state.attachments.map(item => {
    const pending = ['uploading', 'analyzing'].includes(item.ui_status);
    const klass = item.ui_status === 'error' ? 'error' : (pending ? 'pending' : '');
    const kind = item.is_image ? '图' : String(item.file_type || '文件').slice(0, 4);
    return `<div class="attachment-chip ${klass}">
      <span class="attachment-kind">${esc(kind)}</span>
      <span class="attachment-copy">
        <span class="attachment-name" title="${esc(item.file_name || '')}">${esc(item.file_name || '附件')}</span>
        <span class="attachment-status">${esc(item.status || attachmentSize(item.size_bytes))}</span>
      </span>
      <button class="attachment-remove" type="button" onclick="removeAttachment('${esc(item.attachment_id || item.local_id)}')" title="移除附件" aria-label="移除附件">×</button>
    </div>`;
  }).join('');
}
function chooseAttachments(){
  if (state.loading || state.attachments.length >= 3) return;
  document.getElementById('attachmentInput')?.click();
}
function removeAttachment(id){
  state.attachments = state.attachments.filter(item => (item.attachment_id || item.local_id) !== id);
  renderMessages();
}
function fileToPayload(file){
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`无法读取 ${file.name || '附件'}`));
    reader.onload = () => {
      const value = String(reader.result || '');
      resolve({
        file_name:file.name || `clipboard-${Date.now()}.png`,
        mime_type:file.type || '',
        content_base64:value.includes(',') ? value.split(',', 2)[1] : value,
      });
    };
    reader.readAsDataURL(file);
  });
}
function applyAttachmentImageAnalysis(attachmentId, analysis){
  const item = state.attachments.find(entry => entry.attachment_id === attachmentId);
  if (!item) return;
  const ocrText = String(analysis?.ocr_text || '').trim();
  const classifications = Array.isArray(analysis?.classifications) ? analysis.classifications.slice(0, 12) : [];
  item.image_analysis = {source:analysis?.source || 'pasted_clipboard_image', ocr_text:ocrText, classifications};
  item.extracted_text = ocrText;
  item.content_available = Boolean(ocrText || classifications.length);
  item.ui_status = 'ready';
  item.status = item.content_available ? '图片已在本机识别。' : '图片已读取，未识别到文字。';
  renderMessages();
}
async function uploadAttachmentPayload(payload){
  if (!payload?.content_base64) return;
  if (state.attachments.length >= 3) {
    document.getElementById('chatStatus').textContent = '一次最多添加 3 个附件。';
    return;
  }
  const localId = `local_${Math.random().toString(16).slice(2)}`;
  const pending = {local_id:localId, file_name:payload.file_name || '附件', file_type:String(payload.file_name || '').split('.').pop(), size_bytes:Math.floor(payload.content_base64.length * .75), status:'正在上传并读取...', ui_status:'uploading', is_image:/^image\//.test(payload.mime_type || '') || /\.(png|jpe?g|webp)$/i.test(payload.file_name || '')};
  state.attachments.push(pending);
  renderMessages();
  try{
    const result = await api('/api/asa/floating/upload', {method:'POST', body:JSON.stringify({file_name:payload.file_name, mime_type:payload.mime_type || '', content_base64:payload.content_base64}), timeoutMs:30000});
    const index = state.attachments.findIndex(item => item.local_id === localId);
    if (index < 0) return;
    const attachment = {...result.attachment, ui_status:'ready'};
    state.attachments[index] = attachment;
    if (attachment.is_image) {
      if (payload.image_analysis) {
        applyAttachmentImageAnalysis(attachment.attachment_id, payload.image_analysis);
      } else if (window.webkit?.messageHandlers?.asaNative) {
        attachment.ui_status = 'analyzing';
        attachment.status = '正在本机识别图片...';
        renderMessages();
        window.webkit.messageHandlers.asaNative.postMessage({type:'analyzePastedImage', attachment_id:attachment.attachment_id, file_name:attachment.file_name, content_base64:payload.content_base64});
      } else {
        attachment.ui_status = 'error';
        attachment.status = '图片识别需要 ASA 桌面端。';
      }
    }
  }catch(err){
    const item = state.attachments.find(entry => entry.local_id === localId);
    if (item) { item.ui_status = 'error'; item.status = err.message || '附件读取失败'; }
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    renderMessages();
  }
}
async function addAttachmentFiles(files){
  const available = Math.max(0, 3 - state.attachments.length);
  for (const file of Array.from(files || []).slice(0, available)) {
    try { await uploadAttachmentPayload(await fileToPayload(file)); }
    catch (err) { document.getElementById('chatStatus').textContent = err.message; }
  }
}
window.asaReceiveNativeAttachments = async payloads => {
  const available = Math.max(0, 3 - state.attachments.length);
  for (const payload of Array.from(payloads || []).slice(0, available)) await uploadAttachmentPayload(payload);
};
window.addEventListener('asa-native-attachment-analysis', event => {
  const detail = event.detail || {};
  if (detail.error) {
    const item = state.attachments.find(entry => entry.attachment_id === detail.attachment_id);
    if (item) { item.ui_status = 'error'; item.status = detail.error; renderMessages(); }
    return;
  }
  applyAttachmentImageAnalysis(detail.attachment_id, detail.analysis || {});
});
async function loadState(){
  try{
    state.data = await api('/api/asa/floating/state');
    render();
  }catch(err){
    renderConnectionError(err);
  }
}
function native(type, payload={}){
  const handler = window.webkit?.messageHandlers?.asaNative;
  if (!handler) return false;
  handler.postMessage({type, ...payload});
  return true;
}
function renderConnectionError(err){
  const message = `连接失败：${err.message}`;
  document.getElementById('headline').textContent = message;
  document.getElementById('objectTitle').textContent = '本机 ASA 服务未连接';
  document.getElementById('objectSubtitle').textContent = '请启动 8765 工作台服务后重试';
  const syncPill = document.getElementById('syncPill');
  syncPill.textContent = '未连接';
  syncPill.className = 'pill warn';
  document.getElementById('chatStatus').textContent = message;
  document.getElementById('contextActions').innerHTML = [
    `<button class="context-action" onclick="native('startWorkbenchService') || openWorkbench()"><b>启动本机服务</b><span>检查 8765 工作台</span></button>`,
    `<button class="context-action" onclick="native('reload') || loadState()"><b>重试连接</b><span>重新加载浮窗</span></button>`,
    `<button class="context-action" onclick="native('openWorkbench') || openWorkbench()"><b>打开 A 系统</b><span>进入工作台</span></button>`
  ].join('');
  document.getElementById('runSummary').className = 'run-summary empty';
}
function findBridgeCommand(commandId){
  const commands = state.data?.bridge?.recent_commands || [];
  const results = state.data?.bridge?.recent_command_results || [];
  return [...results, ...commands].find(item => item && item.id === commandId) || null;
}
async function waitBridgeCommand(commandId, timeoutMs=15000){
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    await loadState();
    const command = findBridgeCommand(commandId);
    if (command && !['pending', 'queued', 'running'].includes(command.status || '')) return command;
  }
  return null;
}
function render(){
  const active = state.data?.active_context || {};
  document.getElementById('headline').textContent = `${active.source_label || '通用'} · ${active.status || '未连接'}`;
  document.getElementById('objectTitle').textContent = active.title || '通用 ASA 对话';
  document.getElementById('objectSubtitle').textContent = active.subtitle || `${active.source_label || '通用'} · ${active.status || '未连接'}`;
  const syncPill = document.getElementById('syncPill');
  syncPill.textContent = active.status || '未连接';
  syncPill.className = `pill ${active.connected ? 'ok' : 'warn'}`;
  document.getElementById('chatStatus').textContent = '';
  renderContextActions();
  renderRunSummary();
  renderContextSnapshot();
  renderToolTimeline();
  renderHistory();
}
function openWorkbench(){
  const url = workbenchTargetUrl(false);
  if (native('openWorkbench', {url})) return;
  window.open(url, '_blank');
}
function openWorkbenchRun(){
  const url = workbenchTargetUrl(true);
  if (native('openWorkbench', {url})) return;
  window.open(url, '_blank');
}
function workbenchTargetUrl(preferRun=false){
  const active = state.data?.active_context || {};
  const raw = state.data?.active_context_raw || {};
  const nested = raw.context && typeof raw.context === 'object' ? raw.context : {};
  const goals = Array.isArray(state.data?.active_goals) ? state.data.active_goals : [];
  const workflowId = goals.find(goal => goal.workflow_id)?.workflow_id || '';
  if (preferRun && workflowId) return `/asa-app#workflow=${encodeURIComponent(workflowId)}`;
  if (active.job_candidate_id) return `/asa-app#candidate=${encodeURIComponent(active.job_candidate_id)}`;
  const contextType = String(nested.type || raw.type || '').toLowerCase();
  const contextId = nested.id || raw.job_id || raw.position_id || raw.id || '';
  if (contextType === 'job' && contextId) return `/asa-app#job=${encodeURIComponent(contextId)}`;
  if (workflowId) return `/asa-app#workflow=${encodeURIComponent(workflowId)}`;
  return '/asa-app';
}
function workbenchTargetLabel(preferRun=false){
  const active = state.data?.active_context || {};
  const raw = state.data?.active_context_raw || {};
  const nested = raw.context && typeof raw.context === 'object' ? raw.context : {};
  const goals = Array.isArray(state.data?.active_goals) ? state.data.active_goals : [];
  const workflowId = goals.find(goal => goal.workflow_id)?.workflow_id || '';
  if (preferRun && workflowId) return '目标';
  if (active.job_candidate_id) return '人选';
  const contextType = String(nested.type || raw.type || '').toLowerCase();
  if ((contextType === 'job' && (nested.id || raw.job_id || raw.position_id || raw.id)) || ((raw.client || nested.client) && (raw.job || raw.position || nested.job || nested.position))) return '岗位';
  if (workflowId) return '目标';
  return '网页';
}
function compact(value, fallback=''){
  const text = String(value || fallback || '').replace(/\s+/g, ' ').trim();
  return text.length > 42 ? `${text.slice(0, 41)}…` : text;
}
function renderContextActions(){
  const actions = Array.isArray(state.data?.suggested_actions) ? state.data.suggested_actions : [];
  const shouldShow = state.data?.show_suggested_actions === true;
  const html = shouldShow ? actions.slice(0, 5).map(action => {
    const id = esc(action.id || action.action || '');
    const type = esc(action.type || 'floating_action');
    return `<button class="context-action" data-context-action="${id}" data-context-action-type="${type}">
      <b>${esc(action.title || action.label || '执行动作')}</b>
      <span>${esc(compact(action.detail || action.kind || ''))}</span>
    </button>`;
  }).join('') : '';
  const node = document.getElementById('contextActions');
  node.className = `context-actions ${html ? '' : 'empty'}`;
  node.innerHTML = html;
  node.querySelectorAll('[data-context-action]').forEach(button => {
    button.addEventListener('click', () => runMessageAction(button.dataset.contextActionType, button.dataset.contextAction));
  });
  updateContextPanelsVisibility();
}
function runSummaryHasAttention(approvals, goals, artifacts){
  const diagnostics = Array.isArray(state.data?.diagnostics) ? state.data.diagnostics : [];
  if (diagnostics.some(item => ['error', 'warning', 'approval', 'workflow'].includes(String(item.level || '').toLowerCase()))) return true;
  if (approvals.length) return true;
  if (goals.some(goal => ['running', 'queued', 'waiting_approval', 'waiting_external', 'blocked', 'failed', 'paused', 'pending', 'planning'].includes(String(goal.status || goal.workflow_status || '').toLowerCase()))) return true;
  return artifacts.some(artifact => ['failed', 'error', 'warning', 'blocked'].includes(String(artifact.validation_status || artifact.status || '').toLowerCase()));
}
function updateContextPanelsVisibility(){
  const panels = document.querySelector('.context-panels');
  if (!panels) return;
  const hasActions = !document.getElementById('contextActions')?.classList.contains('empty');
  const hasRunSummary = !document.getElementById('runSummary')?.classList.contains('empty');
  const hasSnapshot = !document.getElementById('contextSnapshot')?.classList.contains('empty');
  const hasTimeline = !document.getElementById('toolTimeline')?.classList.contains('empty');
  panels.className = `context-panels ${hasActions || hasRunSummary || hasSnapshot || hasTimeline ? '' : 'empty'}`;
}
function renderRunSummary(){
  const approvals = Array.isArray(state.data?.pending_approvals) ? state.data.pending_approvals : [];
  const goals = Array.isArray(state.data?.active_goals) ? state.data.active_goals : [];
  const artifacts = Array.isArray(state.data?.recent_artifacts) ? state.data.recent_artifacts : [];
  const diagnostics = Array.isArray(state.data?.diagnostics) ? state.data.diagnostics : [];
  const quality = state.data?.context_quality || {};
  const node = document.getElementById('runSummary');
  if (!runSummaryHasAttention(approvals, goals, artifacts)) {
    node.className = 'run-summary empty';
    node.innerHTML = '';
    updateContextPanelsVisibility();
    return;
  }
  const attentionArtifacts = artifacts.filter(artifact => ['failed', 'error', 'warning', 'blocked'].includes(String(artifact.validation_status || artifact.status || '').toLowerCase()));
  const qualityLabel = quality.quality === 'high' ? '上下文高' : quality.quality === 'medium' ? '上下文中' : quality.quality === 'low' ? '上下文低' : quality.quality === 'stale' ? '上下文过期' : quality.quality === 'degraded' ? '权限受限' : '';
  const diagItems = diagnostics
    .filter(item => ['error', 'warning', 'approval', 'workflow', 'info'].includes(String(item.level || '').toLowerCase()))
    .slice(0, 2)
    .map(item => `<span class="diag-chip ${esc(String(item.level || '').toLowerCase())}" title="${esc(item.detail || '')}">${esc(compact(item.title || item.code || '诊断提示', '诊断提示'))}</span>`);
  const chips = [
    qualityLabel ? `<span class="diag-chip ${quality.quality === 'high' ? 'ok' : 'warn'}">${esc(qualityLabel)}</span>` : '',
    ...diagItems,
    approvals.length ? `<span class="diag-chip approval">${approvals.length} 待审批</span>` : '',
    goals.length ? `<span class="diag-chip">${goals.length} 目标在网页</span>` : '',
    attentionArtifacts.length ? `<span class="diag-chip warn">${attentionArtifacts.length} 产物需看</span>` : '',
  ].filter(Boolean).join('');
  node.className = 'run-summary compact';
  node.innerHTML = `
    <div class="diagnostic-strip">${chips}</div>
    <button class="ghost" onclick="openWorkbenchRun()" title="在 A 系统网页查看对应目标、人选或岗位">${esc(workbenchTargetLabel(true))}</button>`;
  updateContextPanelsVisibility();
}
function renderContextSnapshot(){
  const node = document.getElementById('contextSnapshot');
  const snapshot = state.data?.context_snapshot || {};
  const active = state.data?.active_context || {};
  const title = snapshot.title || active.title || '';
  // Context metadata belongs in the header unless a future surface explicitly opts in.
  if (snapshot.show_in_floating !== true || !title || !snapshot.snapshot_id) {
    node.className = 'snapshot-card empty';
    node.innerHTML = '';
    updateContextPanelsVisibility();
    return;
  }
  node.className = 'snapshot-card';
  node.innerHTML = `
    <div class="snapshot-head">
      <div class="snapshot-title">上下文快照 · ${esc(compact(snapshot.source || active.source_label || 'ASA', 'ASA'))}</div>
      <div class="snapshot-meta">${esc(compact(snapshot.created_at || active.updated_at || '', ''))}</div>
    </div>
    <div class="snapshot-body">${esc(compact(title, '当前上下文'))}${snapshot.summary ? ` · ${esc(compact(snapshot.summary, ''))}` : ''}</div>`;
  updateContextPanelsVisibility();
}
function renderToolTimeline(){
  const node = document.getElementById('toolTimeline');
  const calls = Array.isArray(state.data?.runtime?.tool_calls) ? state.data.runtime.tool_calls : [];
  const failures = calls.filter(item => {
    const status = String(item?.status || '').toLowerCase();
    const permission = String(item?.permission_level || '').toLowerCase();
    return Boolean(item?.error)
      || (permission !== 'read' && /fail|error|blocked|denied/.test(status));
  });
  if (!failures.length) {
    node.className = 'tool-timeline empty';
    node.innerHTML = '';
    updateContextPanelsVisibility();
    return;
  }
  node.className = 'tool-timeline compact';
  node.innerHTML = `
    <span class="diag-chip error">${failures.length} 项执行异常</span>
    <button class="ghost" onclick="openWorkbenchRun()" title="在 ASA 网页查看执行详情">查看</button>`;
  updateContextPanelsVisibility();
}
function startScreenshot(){
  const native = window.webkit?.messageHandlers?.asaNative;
  if (!native) {
    document.getElementById('chatStatus').textContent = '截图功能需要在 ASA macOS 浮窗里使用。';
    return;
  }
  document.getElementById('chatStatus').textContent = '进入截图模式...';
  native.postMessage({type:'screenshot'});
}
window.addEventListener('asa-native-status', event => {
  const message = event.detail?.message || '';
  if (message) document.getElementById('chatStatus').textContent = message;
  if (event.detail?.action === 'imageAnalysisReady') answerAfterNativeImage();
});
function newChat(){ state.sessionId = `floating_${Math.random().toString(16).slice(2)}`; localStorage.setItem('asaFloatingSession', state.sessionId); state.messages=[]; state.attachments=[]; state.historyOpen=false; state.businessFocus=null; renderHistory(); renderMessages(); }
function bindComposer(){
  const input = document.getElementById('input');
  if (!input) return;
  input.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    sendMessage();
  });
  input.addEventListener('paste', event => {
    const files = Array.from(event.clipboardData?.files || []);
    if (!files.length) return;
    event.preventDefault();
    addAttachmentFiles(files);
  });
  const attachmentInput = document.getElementById('attachmentInput');
  attachmentInput?.addEventListener('change', () => {
    addAttachmentFiles(attachmentInput.files || []);
    attachmentInput.value = '';
  });
}
function renderMessages(){
  const base = state.messages.map(renderFloatingMessage).join('') || `<div class="msg assistant">${renderFloatingMarkdown('说一个目标，ASA 会按当前对象生成可审计的执行计划。外部触达、发布和推荐仍会先进入审批。')}</div>`;
  document.getElementById('messages').innerHTML = base + (state.loading ? renderThinkingMessage() : '');
  document.querySelectorAll('[data-action-id]').forEach(button => {
    button.addEventListener('click', () => runMessageAction(button.dataset.actionType, button.dataset.actionId));
  });
  const messages = document.getElementById('messages');
  messages.scrollTop = messages.scrollHeight;
  renderAttachments();
  renderFocusBar();
  const blockedByAttachment = state.attachments.some(item => ['uploading', 'analyzing', 'error'].includes(item.ui_status));
  const sendButton = document.getElementById('sendButton');
  if (sendButton) {
    sendButton.disabled = state.loading || blockedByAttachment;
    sendButton.textContent = state.loading ? '…' : '↑';
    sendButton.title = state.loading ? 'ASA 正在处理' : (blockedByAttachment ? '等待附件读取完成' : '发送');
  }
  const attachButton = document.getElementById('attachButton');
  if (attachButton) attachButton.disabled = state.loading || state.attachments.length >= 3;
}
function recoverStuckRequest(){
  if (!state.loading) return;
  const startedAt = Number(state.requestStartedAt || 0);
  if (startedAt && Date.now() - startedAt < 50000) return;
  state.loading = false;
  state.requestStartedAt = 0;
  document.getElementById('chatStatus').textContent = '上一次请求已中断，可以重新发送。';
  renderMessages();
}
function runMessageAction(type, id){
  if (type === 'open_candidate') {
    const suffix = id ? `#candidate=${encodeURIComponent(id)}` : '';
    window.open(`/asa-app${suffix}`, '_blank');
    return;
  }
  if (type === 'native_action') {
    document.getElementById('chatStatus').textContent = '等待本机确认动作完成...';
    state.pendingNativeContext = state.data?.active_context_raw || null;
    if (!native(id)) {
      document.getElementById('chatStatus').textContent = '该动作需要在 ASA macOS 浮窗中执行。';
    }
    return;
  }
  if (type === 'floating_action' && id?.startsWith('open_wechat_attachment::')) {
    state.pendingNativeContext = state.data?.active_context_raw || null;
  }
  runFloatingAction(type, id);
}
function floatingMessageContext(){
  const active = state.data?.active_context || {};
  const raw = state.data?.active_context_raw || {};
  const recents = Array.isArray(state.data?.recent_contexts) ? state.data.recent_contexts : [];
  const freshBridgeAges = recents
    .filter(item => ['liepin','xsaas'].includes(item?.surface) && !item?.stale)
    .map(item => Number(item?.age_seconds))
    .filter(Number.isFinite);
  const freshestBridgeAge = freshBridgeAges.length ? Math.min(...freshBridgeAges) : null;
  const activeAge = Number(active.age_seconds ?? 0);
  const staleAsystemPin = active.surface === 'a_system'
    && freshestBridgeAge !== null
    && Number.isFinite(activeAge)
    && activeAge > freshestBridgeAge + 60;
  if (active.job_candidate_id && !staleAsystemPin) {
    return {type:'candidate', id:active.job_candidate_id, source:'asa_floating', display_mode:'floating_compact', bridge:raw};
  }
  if (staleAsystemPin) {
    // active 是明显过期的 A 系统点击，而浏览器已有更新的猎聘/X-SaaS 页面：
    // 不附 job_candidate_id，让服务端按 global+bridge 处理，避免钉住旧候选人。
    return {type:'global', id:null, source:'asa_floating', display_mode:'floating_compact', bridge:raw};
  }
  return {type:active.type || 'global', id:active.id || null, source:'asa_floating', display_mode:'floating_compact', bridge:raw};
}
async function answerAfterNativeImage(){
  if (state.loading) return;
  const previous = [...state.messages].reverse().find(item => item?.role === 'user' && item?.content);
  if (!previous) return;
  state.loading = true;
  renderMessages();
  document.getElementById('chatStatus').textContent = 'ASA 正在基于本地图片识别结果回答...';
  try{
    await loadState();
    const context = floatingMessageContext();
    const retryMessage = `${previous.content}\n（已确认打开当前微信图片，请基于最新的本地图片识别结果回答。）`;
    const result = await api('/api/agent/copilot', {method:'POST', body:JSON.stringify({session_id:state.sessionId, message:retryMessage, context})});
    state.messages.push({role:'assistant', content:(result.answer || result.message || '图片识别已完成。'), suggested_actions:result.suggested_actions || []});
    document.getElementById('chatStatus').textContent = '';
    await loadState();
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    state.loading = false;
    renderMessages();
  }
}
async function answerAfterNativeAttachment(){
  if (state.loading) return;
  const previous = [...state.messages].reverse().find(item => item?.role === 'user' && item?.content);
  if (!previous) return;
  const raw = state.pendingNativeContext || state.data?.active_context_raw || {};
  state.pendingNativeContext = null;
  state.loading = true;
  renderMessages();
  document.getElementById('chatStatus').textContent = 'ASA 正在重新读取本地附件...';
  try{
    const context = {type:'global', id:null, source:'asa_floating', display_mode:'floating_compact', bridge:raw};
    const retryMessage = `${previous.content}\n（已在微信打开当前附件，请重新读取本地副本后回答。）`;
    const result = await api('/api/agent/copilot', {method:'POST', body:JSON.stringify({session_id:state.sessionId, message:retryMessage, context})});
    state.messages.push({role:'assistant', content:(result.answer || result.message || '附件读取已完成。'), suggested_actions:result.suggested_actions || []});
    document.getElementById('chatStatus').textContent = '';
    await loadState();
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    state.loading = false;
    renderMessages();
  }
}
async function runFloatingAction(type, id){
  const action = type && type !== 'floating_action' ? type : id;
  if (!action || state.loading) return;
  if (action.startsWith('open_wechat_attachment::') && !state.pendingNativeContext) {
    state.pendingNativeContext = state.data?.active_context_raw || null;
  }
  let retryPrevious = false;
  state.loading = true;
  renderMessages();
  document.getElementById('chatStatus').textContent = 'ASA 正在执行动作...';
  try{
    const payload = {action};
    if (id && id !== action) payload.id = id;
    if (['start_workflow', 'open_workflow'].includes(action) && id) payload.workflow_id = id;
    const result = await api('/api/asa/floating/action', {method:'POST', body:JSON.stringify(payload)});
    retryPrevious = result.retry_previous === true;
    state.messages.push({role:'assistant', content:result.message || '动作已提交。'});
    renderMessages();
    if (result.open_url) {
      if (!native('openWorkbench', {url:result.open_url})) window.open(result.open_url, '_blank');
    }
    if (action === 'start_workflow' && id) {
      await monitorWorkflow(id);
    }
    const commandId = result.command?.id;
    if (commandId) {
      document.getElementById('chatStatus').textContent = '等待页面执行结果...';
      const bridgeResult = await waitBridgeCommand(commandId);
      if (bridgeResult?.message) {
        state.messages.push({role:'assistant', content:`页面执行结果：${bridgeResult.message}`});
      } else {
        state.messages.push({role:'assistant', content:'页面暂未回传执行结果。请确认猎聘页面仍打开，并查看页面右侧状态。'});
      }
    }
    document.getElementById('chatStatus').textContent = '';
    await loadState();
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    state.loading = false;
    renderMessages();
    if (retryPrevious) setTimeout(answerAfterNativeAttachment, 2500);
  }
}
async function monitorWorkflow(workflowId, timeoutMs=120000){
  const started = Date.now();
  let lastSignature = '';
  while (Date.now() - started < timeoutMs) {
    const payload = await api(`/api/v1/workflows/${encodeURIComponent(workflowId)}`);
    const workflow = payload.workflow || {};
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    const active = steps.find(step => ['running','waiting_approval','waiting_external'].includes(step.status))
      || steps.find(step => step.status === 'pending');
    const completed = steps.filter(step => step.status === 'completed').length;
    const status = String(workflow.status || 'queued');
    const signature = `${status}|${completed}|${active?.id || ''}`;
    if (signature !== lastSignature) {
      lastSignature = signature;
      const label = active?.business_label || workflow.current_stage || '准备执行';
      document.getElementById('chatStatus').textContent = `目标 ${status} · ${completed}/${steps.length} · ${label}`;
    }
    if (['waiting_approval','waiting_external','blocked','failed','completed','cancelled'].includes(status)) {
      const failed = steps.find(step => step.status === 'failed');
      const messages = {
        waiting_approval: `目标已执行 ${completed}/${steps.length} 步，正在等待人工审批。`,
        waiting_external: `目标已执行 ${completed}/${steps.length} 步，正在等待外部页面完成。`,
        blocked: `目标执行到 ${completed}/${steps.length} 步后被阻塞。`,
        failed: `目标执行失败：${failed?.error || payload.goal?.error || '请查看计划详情。'}`,
        completed: `目标已完成，共执行 ${steps.length} 步。`,
        cancelled: '目标已取消。',
      };
      state.messages.push({role:'assistant', content:messages[status] || `目标状态：${status}`});
      renderMessages();
      await loadState();
      return payload;
    }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
  state.messages.push({role:'assistant', content:'目标仍在后台执行，可点击“查看计划”继续跟踪。'});
  renderMessages();
  return null;
}
async function restoreCurrentSession(){
  if (state.restored || !state.sessionId) return;
  state.restored = true;
  try{
    const result = await api(`/api/agent/copilot/session?session_id=${encodeURIComponent(state.sessionId)}&limit=100`);
    state.messages = result.messages || [];
    state.businessFocus = result.business_focus;
    renderMessages();
  }catch(_){}
}
async function toggleHistory(){
  state.historyOpen = !state.historyOpen;
  renderHistory();
  if (state.historyOpen) await loadSessions();
}
async function loadSessions(){
  state.historyLoading = true;
  renderHistory();
  try{
    const result = await api('/api/agent/copilot/sessions?limit=30');
    state.sessions = result.sessions || [];
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    state.historyLoading = false;
    renderHistory();
  }
}
async function loadSession(sessionId){
  if (!sessionId) return;
  document.getElementById('chatStatus').textContent = '正在恢复历史对话...';
  try{
    const result = await api(`/api/agent/copilot/session?session_id=${encodeURIComponent(sessionId)}&limit=100`);
    state.sessionId = sessionId;
    localStorage.setItem('asaFloatingSession', state.sessionId);
    state.messages = result.messages || [];
    state.businessFocus = result.business_focus;
    state.historyOpen = false;
    document.getElementById('chatStatus').textContent = '';
    renderHistory();
    renderMessages();
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }
}
function renderHistory(){
  const panel = document.getElementById('historyPanel');
  if (!panel) return;
  panel.className = `history-panel ${state.historyOpen ? 'open' : ''}`;
  if (!state.historyOpen) { panel.innerHTML = ''; return; }
  const body = state.historyLoading
    ? '<div class="empty">正在读取历史对话...</div>'
    : (state.sessions.length ? state.sessions.map(session => `
      <button class="history-item ${session.session_id === state.sessionId ? 'active' : ''}" data-session-id="${esc(session.session_id)}">
        <b>${esc(session.title || '未命名对话')}</b>
        <span class="muted">${esc(session.updated_at || '')} · ${Number(session.message_count || 0)} 条消息</span>
        <span class="muted">${esc(session.preview || '')}</span>
      </button>`).join('') : '<div class="empty">暂无历史对话。</div>');
  panel.innerHTML = `<div class="history-head"><b>历史对话</b><button class="ghost" onclick="toggleHistory()">关闭</button></div><div class="history-list">${body}</div>`;
  panel.querySelectorAll('[data-session-id]').forEach(btn => btn.addEventListener('click', () => loadSession(btn.dataset.sessionId)));
}
async function sendMessage(){
  const input = document.getElementById('input');
  const typedText = input.value.trim();
  const readyAttachments = state.attachments.filter(item => item.ui_status === 'ready');
  const blockedAttachment = state.attachments.find(item => ['uploading', 'analyzing', 'error'].includes(item.ui_status));
  if (blockedAttachment) {
    document.getElementById('chatStatus').textContent = blockedAttachment.ui_status === 'error' ? '请先移除读取失败的附件。' : '附件仍在读取，请稍候。';
    return;
  }
  if(!typedText && !readyAttachments.length) return;
  const text = typedText || `请分析附件：${readyAttachments.map(item => item.file_name).join('、')}`;
  if(state.loading) {
    const elapsed = Date.now() - Number(state.requestStartedAt || 0);
    if (elapsed < 50000) {
      document.getElementById('chatStatus').textContent = '上一条消息仍在处理中...';
      return;
    }
    state.loading = false;
    state.requestStartedAt = 0;
  }
  input.value = '';
  state.loading = true;
  state.requestStartedAt = Date.now();
  state.messages.push({role:'user', content:text}); renderMessages();
  document.getElementById('chatStatus').textContent = 'ASA 正在处理...';
  try{
    const context = floatingMessageContext();
    context.uploaded_attachments = readyAttachments.map(item => ({attachment_id:item.attachment_id, file_name:item.file_name, file_type:item.file_type, mime_type:item.mime_type, size_bytes:item.size_bytes, content_available:item.content_available, extracted_text:item.extracted_text || '', truncated:Boolean(item.truncated), status:item.status || '', is_image:Boolean(item.is_image), image_analysis:item.image_analysis || {}}));
    const result = await postCopilotMessage(
      {session_id:state.sessionId, message:text, context},
      floatingCopilotIdempotencyKey(state.sessionId, text),
      floatingCopilotRequestId(state.sessionId, text),
    );
    state.businessFocus = result.business_focus;
    state.sessionId = result.session_id || state.sessionId;
    localStorage.setItem('asaFloatingSession', state.sessionId);
    const workflowText = result.workflow_id ? `\n\n已生成目标计划：${result.workflow_id}` : '';
    state.messages.push({role:'assistant', content:(result.answer || result.message || '已处理。') + workflowText, suggested_actions: result.suggested_actions || []});
    state.attachments = [];
    document.getElementById('chatStatus').textContent = '';
    loadState();
  }catch(err){
    document.getElementById('chatStatus').textContent = err.message;
  }finally{
    state.loading = false;
    state.requestStartedAt = 0;
    renderMessages();
  }
}
loadState();
renderMessages();
restoreCurrentSession();
bindComposer();
setInterval(loadState, 2500);
setInterval(recoverStuckRequest, 1000);
</script>
</body>
</html>"""


def find_job_candidate(
    conn: sqlite3.Connection,
    candidate_id: int | None,
    client: str,
    position: str,
    job_candidate_id: int | None = None,
) -> sqlite3.Row | None:
    if job_candidate_id:
        return conn.execute(
            """
            SELECT jc.*, p.display_name, p.current_company, p.current_title,
                   c.name AS client, j.title AS job
            FROM job_candidates jc
            JOIN people p ON p.id = jc.person_id
            LEFT JOIN jobs j ON j.id = jc.job_id
            LEFT JOIN clients c ON c.id = j.client_id
            WHERE jc.id = ?
            LIMIT 1
            """,
            (job_candidate_id,),
        ).fetchone()
    if not candidate_id:
        return None
    return conn.execute(
        """
        SELECT jc.*, p.display_name, p.current_company, p.current_title,
               c.name AS client, j.title AS job
        FROM job_candidates jc
        JOIN people p ON p.id = jc.person_id
        LEFT JOIN jobs j ON j.id = jc.job_id
        LEFT JOIN clients c ON c.id = j.client_id
        WHERE CAST(jc.source_candidate_id AS TEXT) = CAST(? AS TEXT)
          AND (? = '' OR c.name = ?)
          AND (? = '' OR j.title = ? OR jc.raw_position = ?)
        ORDER BY jc.id DESC
        LIMIT 1
        """,
        (candidate_id, client, client, position, position, position),
    ).fetchone()


def extract_resume_id(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    query = urllib.parse.parse_qs(parsed.query)
    resume_id = query.get("res_id_encode", [""])[0]
    if resume_id:
        return clean(resume_id)
    return clean(text.split("res_id_encode=", 1)[1].split("&", 1)[0]) if "res_id_encode=" in text else ""


def extract_xsaas_candidate_id(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    match = urllib.parse.urlparse(text)
    combined = " ".join([match.path, match.fragment, match.query, text])
    for pattern in [
        r"/candidate/info/([0-9]+)",
        r"candidateId=([0-9]+)",
        r"candidate_id=([0-9]+)",
        r"\bid=([0-9]{4,})\b",
    ]:
        found = re.search(pattern, combined)
        if found:
            return found.group(1)
    return ""


def normalize_candidate_name(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    return text.replace("老师", "").strip()


def is_masked_candidate_name(value: str) -> bool:
    text = clean(value)
    return bool(text) and (
        "*" in text
        or "某" in text
        or text.endswith("先生")
        or text.endswith("女士")
        or text.endswith("老师")
    )


def candidate_names_can_correspond(query_name: str, row_name: str) -> bool:
    query_raw = normalize_candidate_name(query_name)
    row_raw = normalize_candidate_name(row_name)
    query_masked = is_masked_candidate_name(query_name)
    row_masked = is_masked_candidate_name(row_name)
    query = query_raw.replace("先生", "").replace("女士", "").strip()
    row = row_raw.replace("先生", "").replace("女士", "").strip()
    if not query or not row:
        return False
    if query == row:
        return True
    if (query_masked or row_masked) and query[:1] and query[:1] == row[:1]:
        return True
    return False


def normalize_talent_project(client: str, job: str) -> tuple[str, str]:
    normalized_client = clean(client)
    normalized_job = clean(job)
    if "鹏新旭" in normalized_client and any(token in normalized_job.upper() for token in ["PQE", "质量"]):
        return "鹏新旭", "PQE专家"
    return normalized_client, normalized_job


def like_text(value: str) -> str:
    return f"%{clean(value)}%"


def is_generic_liepin_im_source_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(clean(value))
    return parsed.netloc.endswith("liepin.com") and parsed.path.rstrip("/") == "/im/showmsgnewpage"


def project_lookup_score(row: sqlite3.Row, query: dict[str, str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    query_resume_id = extract_resume_id(query.get("resume_id", "")) or extract_resume_id(query.get("source_url", ""))
    row_resume_id = extract_resume_id(row["source_url"] or "")
    if query_resume_id and row_resume_id and query_resume_id == row_resume_id:
        score += 120
        reasons.append("简历链接精确匹配")

    query_url = clean(query.get("source_url", ""))
    row_url = clean(row["source_url"] or "")
    if query_url and row_url and query_url == row_url and not is_generic_liepin_im_source_url(query_url):
        score += 80
        reasons.append("来源链接完全一致")

    query_name = normalize_candidate_name(query.get("candidate_name", ""))
    row_name = normalize_candidate_name(row["candidate_name"] or "")
    if query_name and row_name and query_name == row_name:
        score += 45
        reasons.append("候选人姓名一致")
    elif candidate_names_can_correspond(query_name, row_name):
        score += 45
        reasons.append("候选人姓名可互证")

    query_company = clean(query.get("candidate_company", ""))
    row_company = clean(row["candidate_company"] or "")
    if query_company and row_company and (query_company in row_company or row_company in query_company):
        score += 30
        reasons.append("候选人公司相近")

    event_type = clean(row["event_type"] or "")
    event_status = clean(row["event_status"] or "")
    communication_markers = {
        "greeting_open_chat",
        "already_continue_chat",
        "reply_assistant_fill",
        "reply_assistant_accept",
        "sent_verified",
        "message_outreach_verified",
        "im_followup_verified",
        "job_chat_verified",
        "job_recommended_verified",
    }
    if event_type in communication_markers or event_status in communication_markers:
        score += 12
        reasons.append("来自开聊/沟通动作")
    elif event_type == "resume_link_captured":
        score += 6
        reasons.append("来自寻访简历链接记录")

    return score, reasons


def project_lookup_auto_apply(score: int, reasons: list[str]) -> bool:
    exact_reasons = {"简历链接精确匹配", "来源链接完全一致"}
    if any(reason in exact_reasons for reason in reasons):
        return True
    if "候选人姓名一致" in reasons and "来自开聊/沟通动作" in reasons:
        return True
    if (
        "候选人姓名可互证" in reasons
        and "候选人公司相近" in reasons
        and "来自开聊/沟通动作" in reasons
    ):
        return True
    return score >= 80 and (
        "候选人姓名一致" in reasons
        or ("候选人姓名可互证" in reasons and "候选人公司相近" in reasons)
    )


def talent_project_lookup_score(row: sqlite3.Row, query: dict[str, str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    profile_text = clean(query.get("candidate_profile_text", ""))

    query_name = normalize_candidate_name(query.get("candidate_name", ""))
    row_name = normalize_candidate_name(row["name"] or "")
    if query_name and row_name and query_name == row_name:
        score += 45
        reasons.append("v3候选人姓名一致")
    elif candidate_names_can_correspond(query_name, row_name):
        score += 35
        reasons.append("v3候选人姓名可互证")

    query_company = clean(query.get("candidate_company", ""))
    row_company = clean(row["company"] or "")
    if query_company and row_company and (query_company in row_company or row_company in query_company):
        score += 35
        reasons.append("v3候选人公司相近")
    elif row_company and profile_text and row_company in profile_text:
        score += 35
        reasons.append("v3候选人公司出现在简历正文")

    query_title = clean(query.get("candidate_title", ""))
    row_title = clean(row["title"] or "")
    if query_title and row_title and (query_title in row_title or row_title in query_title):
        score += 35
        reasons.append("v3候选人职位相近")
    elif row_title and profile_text and row_title in profile_text:
        score += 35
        reasons.append("v3候选人职位出现在简历正文")

    if clean(row["status"] or "") in {"contacted", "recommended", "interviewing", "offered", "hired"}:
        score += 10
        reasons.append("v3候选人已有推进状态")

    return score, reasons


def lookup_talent_current_project(query: dict[str, str]) -> dict[str, Any] | None:
    if not TALENT_DB.exists():
        return None
    requested_client, requested_position = normalize_talent_project(
        first_present(query, "client", "") or first_present(query, "candidate_client", ""),
        first_present(query, "job", "") or first_present(query, "position", "") or first_present(query, "candidate_position", ""),
    )

    conn = sqlite3.connect(str(TALENT_DB))
    conn.row_factory = sqlite3.Row
    try:
        where = [
            "COALESCE(client, '') != ''",
            "COALESCE(position, '') != ''",
        ]
        params: list[Any] = []
        if requested_client:
            where.append("client = ?")
            params.append(requested_client)
        if requested_position:
            where.append("position = ?")
            params.append(requested_position)
        rows = conn.execute(
            f"""
            SELECT id, name, company, title, client, position, level, status, updated_at
            FROM candidates
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(updated_at, '') DESC, id DESC
            LIMIT 3000
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[int, sqlite3.Row, list[str]]] = []
    for row in rows:
        score, reasons = talent_project_lookup_score(row, query)
        if score >= 80 and (
            "v3候选人姓名一致" in reasons
            or "v3候选人姓名可互证" in reasons
        ):
            scored.append((score, row, reasons))

    if not scored:
        return None

    scored.sort(key=lambda item: (item[0], clean(item[1]["updated_at"] or ""), int(item[1]["id"] or 0)), reverse=True)
    score, row, reasons = scored[0]
    client, position = normalize_talent_project(clean(row["client"]), clean(row["position"]))
    if requested_client:
        reasons.append("显式客户一致")
    if requested_position:
        reasons.append("显式岗位一致")
    return {
        "ok": True,
        "matched": True,
        "auto_apply": True,
        "message": "已按A系统v3当前人才库定位岗位",
        "project": {
            "client": client,
            "position": position,
            "confidence": "A系统v3-高",
            "rule": "talent_system_v3",
        },
        "match": {
            "score": score,
            "confidence": "高",
            "auto_apply": True,
            "reasons": reasons,
            "candidate_id": int(row["id"]) if row["id"] is not None else None,
            "candidate_name": clean(row["name"]),
            "candidate_company": clean(row["company"]),
            "candidate_title": clean(row["title"]),
            "candidate_level": clean(row["level"]),
            "candidate_status": clean(row["status"]),
            "updated_at": clean(row["updated_at"]),
        },
    }


def lookup_recent_outreach_project(state: WorkbenchState, query: dict[str, str]) -> dict[str, Any]:
    talent_project = lookup_talent_current_project(query)
    if talent_project:
        return talent_project

    conn = connect_outreach_db(state.db_path)
    try:
        ensure_outreach_schema(conn)
        rows = conn.execute(
            """
            SELECT id, candidate_id, candidate_name, candidate_company, client, position,
                   channel, event_type, event_status, message_summary, source_url, event_time
            FROM outreach_events
            WHERE COALESCE(client, '') != ''
              AND COALESCE(position, '') != ''
              AND COALESCE(event_status, 'done') NOT IN ('failed', 'error', 'skipped')
            ORDER BY id DESC
            LIMIT 800
            """
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[int, sqlite3.Row, list[str]]] = []
    for row in rows:
        score, reasons = project_lookup_score(row, query)
        if score > 0:
            scored.append((score, row, reasons))

    if not scored:
        return {"ok": True, "matched": False, "message": "没有找到可关联的推荐岗位记录"}

    scored.sort(key=lambda item: (item[0], int(item[1]["id"] or 0)), reverse=True)
    score, row, reasons = scored[0]
    auto_apply = project_lookup_auto_apply(score, reasons)
    confidence = "高" if auto_apply else "中" if score >= 50 else "低"
    return {
        "ok": True,
        "matched": True,
        "auto_apply": auto_apply,
        "message": "找到可自动应用的高置信岗位记录" if auto_apply else "找到疑似触达岗位记录，仅供参考",
        "project": {
            "client": clean(row["client"]),
            "position": clean(row["position"]),
            "confidence": f"触达记录-{confidence}" if auto_apply else f"触达记录-{confidence}（未自动切换）",
            "rule": "outreach_event",
        },
        "match": {
            "score": score,
            "confidence": confidence,
            "auto_apply": auto_apply,
            "reasons": reasons,
            "event_id": int(row["id"]),
            "event_type": clean(row["event_type"]),
            "event_status": clean(row["event_status"]),
            "event_time": clean(row["event_time"]),
            "candidate_name": clean(row["candidate_name"]),
            "candidate_company": clean(row["candidate_company"]),
            "source_url": clean(row["source_url"]),
        },
    }


def action_locator(data: dict[str, Any]) -> dict[str, Any]:
    locator = data.get("locator") if isinstance(data.get("locator"), dict) else data
    action: dict[str, Any] = {}
    job_candidate_id = first_present(locator, "job_candidate_id", "")
    if job_candidate_id:
        action["job_candidate_id"] = int(job_candidate_id)
        return action
    client, job = normalize_talent_project(
        first_present(locator, "client", ""),
        first_present(locator, "job") or first_present(locator, "position", ""),
    )
    action.update(
        {
            "candidate": first_present(locator, "candidate")
            or first_present(locator, "candidateName")
            or first_present(locator, "candidate_name", ""),
            "company": first_present(locator, "company")
            or first_present(locator, "candidateCompany")
            or first_present(locator, "candidate_company", ""),
            "title": first_present(locator, "title")
            or first_present(locator, "candidateTitle")
            or first_present(locator, "candidate_title", ""),
            "client": client,
            "job": job,
        }
    )
    return action


def candidate_match_payload(row: sqlite3.Row) -> dict[str, Any]:
    latest_review_status = clean(row["latest_review_status"]) if "latest_review_status" in row.keys() else ""
    latest_review_time = clean(row["latest_review_time"]) if "latest_review_time" in row.keys() else ""
    latest_review_summary = clean(row["latest_review_summary"]) if "latest_review_summary" in row.keys() else ""
    review_count = int(row["review_count"] or 0) if "review_count" in row.keys() else 0
    latest_outreach_status = clean(row["latest_outreach_status"]) if "latest_outreach_status" in row.keys() else ""
    latest_outreach_type = clean(row["latest_outreach_type"]) if "latest_outreach_type" in row.keys() else ""
    latest_outreach_time = clean(row["latest_outreach_time"]) if "latest_outreach_time" in row.keys() else ""
    latest_event_type = clean(row["latest_event_type"]) if "latest_event_type" in row.keys() else ""
    latest_event_status = clean(row["latest_event_status"]) if "latest_event_status" in row.keys() else ""
    latest_event_time = clean(row["latest_event_time"]) if "latest_event_time" in row.keys() else ""
    return {
        "job_candidate_id": int(row["job_candidate_id"]),
        "person_id": int(row["person_id"]) if "person_id" in row.keys() and row["person_id"] is not None else None,
        "candidate": clean(row["candidate"]),
        "company": clean(row["company"]),
        "title": clean(row["title"]),
        "client": clean(row["client"]),
        "job": clean(row["job"]),
        "source_candidate_id": clean(row["source_candidate_id"]),
        "clean_stage": clean(row["clean_stage"]),
        "progress_stage": clean(row["clean_stage"]),
        "reviewed": review_count > 0,
        "review_count": review_count,
        "latest_review_status": latest_review_status,
        "latest_review_time": latest_review_time,
        "latest_review_summary": latest_review_summary,
        "latest_outreach_type": latest_outreach_type,
        "latest_outreach_status": latest_outreach_status,
        "latest_outreach_time": latest_outreach_time,
        "latest_event_type": latest_event_type,
        "latest_event_status": latest_event_status,
        "latest_event_time": latest_event_time,
    }


def candidate_library_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"] or "",
        "company": row["company"] or "",
        "title": row["title"] or "",
        "city": row["city"] or "",
        "education": row["education"] or "",
        "experience": row["experience"] or "",
        "level": row["level"] or "",
        "status": row["status"] or "",
        "client": row["client"] or "",
        "position": row["position"] or "",
        "source": row["source"] or "",
        "xsaas_id": row["xsaas_id"] or "",
        "updated_at": row["updated_at"] or "",
    }


def xsaas_candidate_score(row: sqlite3.Row, locator: dict[str, str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    xsaas_id = clean(locator.get("xsaas_id", ""))
    if xsaas_id and clean(row["xsaas_id"] or "") == xsaas_id and clean(row["source"] or "") == "xsaas":
        score += 160
        reasons.append("X-SaaS ID一致")

    candidate = normalize_candidate_name(locator.get("candidate", ""))
    row_candidate = normalize_candidate_name(row["name"] or "")
    if candidate and row_candidate and candidate == row_candidate:
        score += 50
        reasons.append("姓名一致")
    elif candidate_names_can_correspond(candidate, row_candidate):
        score += 35
        reasons.append("姓名可互证")

    profile_text = clean(locator.get("candidate_profile_text", ""))
    company = clean(locator.get("company", ""))
    row_company = clean(row["company"] or "")
    if company and row_company and (company in row_company or row_company in company):
        score += 35
        reasons.append("公司相近")
    elif row_company and profile_text and row_company in profile_text:
        score += 30
        reasons.append("公司出现在页面")

    title = clean(locator.get("title", ""))
    row_title = clean(row["title"] or "")
    if title and row_title and (title in row_title or row_title in title):
        score += 30
        reasons.append("职位相近")
    elif row_title and profile_text and row_title in profile_text:
        score += 25
        reasons.append("职位出现在页面")

    client = clean(locator.get("client", ""))
    job = clean(locator.get("job", ""))
    if client and clean(row["client"] or "") == client:
        score += 10
        reasons.append("客户一致")
    if job and clean(row["position"] or "") == job:
        score += 10
        reasons.append("岗位一致")

    return score, reasons


def xsaas_candidate_can_auto_match(score: int, reasons: list[str]) -> bool:
    if "X-SaaS ID一致" in reasons:
        return True
    if "姓名一致" not in reasons and "姓名可互证" not in reasons:
        return False
    identity_hits = {"公司相近", "公司出现在页面", "职位相近", "职位出现在页面"}
    return score >= 80 and bool(identity_hits.intersection(reasons))


def lookup_xsaas_candidate_library(conn: sqlite3.Connection, locator: dict[str, str]) -> dict[str, Any]:
    xsaas_id = clean(locator.get("xsaas_id", ""))
    candidate = normalize_candidate_name(locator.get("candidate", ""))
    company = clean(locator.get("company", ""))
    title = clean(locator.get("title", ""))
    attempts: list[tuple[str, str, list[Any]]] = []
    if xsaas_id:
        attempts.append(("xsaas_id", "SELECT * FROM candidates WHERE source = 'xsaas' AND xsaas_id = ? ORDER BY id DESC", [xsaas_id]))
    if candidate and company:
        attempts.append((
            "name_company",
            "SELECT * FROM candidates WHERE name = ? AND COALESCE(company, '') LIKE ? ORDER BY updated_at DESC, id DESC",
            [candidate, like_text(company)],
        ))
    if candidate and title:
        attempts.append((
            "name_title",
            "SELECT * FROM candidates WHERE name = ? AND COALESCE(title, '') LIKE ? ORDER BY updated_at DESC, id DESC",
            [candidate, like_text(title)],
        ))
    if candidate:
        attempts.append(("name", "SELECT * FROM candidates WHERE name = ? ORDER BY updated_at DESC, id DESC", [candidate]))

    seen: set[Any] = set()
    collected: list[dict[str, Any]] = []
    for reason, query, params in attempts:
        rows = conn.execute(query, params).fetchall()
        scored: list[tuple[int, sqlite3.Row, list[str]]] = []
        for row in rows:
            score, score_reasons = xsaas_candidate_score(row, locator)
            payload = candidate_library_payload(row)
            payload["score"] = score
            payload["reasons"] = score_reasons
            if row["id"] not in seen:
                seen.add(row["id"])
                collected.append(payload)
            if xsaas_candidate_can_auto_match(score, score_reasons):
                scored.append((score, row, score_reasons))
        if scored:
            scored.sort(key=lambda item: (item[0], int(item[1]["id"] or 0)), reverse=True)
            top_score = scored[0][0]
            tied = [item for item in scored if item[0] == top_score]
            if len(tied) == 1:
                payload = candidate_library_payload(scored[0][1])
                payload["score"] = scored[0][0]
                payload["reasons"] = scored[0][2]
                return {"matched": True, "ambiguous": False, "reason": reason, "candidate": payload, "candidates": collected[:8]}
    collected.sort(key=lambda item: (int(item.get("score") or 0), int(item.get("id") or 0)), reverse=True)
    return {"matched": False, "ambiguous": bool(collected), "reason": "weak_library_match" if collected else "no_library_match", "candidate": None, "candidates": collected[:8]}


def lookup_xsaas_candidate_status(data: dict[str, Any]) -> dict[str, Any]:
    if not TALENT_DB.exists():
        return {"ok": False, "status": "error", "message": f"找不到 v3 数据库：{TALENT_DB}", "chips": ["人才库不可用"]}

    client, job = normalize_talent_project(
        first_present(data, "client", ""),
        first_present(data, "job") or first_present(data, "position", ""),
    )
    locator = {
        "xsaas_id": first_present(data, "xsaas_id", "") or first_present(data, "source_candidate_id", "") or extract_xsaas_candidate_id(first_present(data, "source_url", "")),
        "candidate": first_present(data, "candidate", "") or first_present(data, "candidateName", "") or first_present(data, "candidate_name", ""),
        "company": first_present(data, "company", "") or first_present(data, "candidateCompany", "") or first_present(data, "candidate_company", ""),
        "title": first_present(data, "title", "") or first_present(data, "candidateTitle", "") or first_present(data, "candidate_title", ""),
        "client": client,
        "job": job,
        "candidate_profile_text": first_present(data, "candidate_profile_text", ""),
    }

    conn = sqlite3.connect(TALENT_DB)
    conn.row_factory = sqlite3.Row
    try:
        library = lookup_xsaas_candidate_library(conn, locator)
    finally:
        conn.close()

    candidate_row = library.get("candidate") if library.get("matched") else None
    progress_lookup: dict[str, Any] = {"ok": True, "matched": False, "reason": "library_not_matched"}
    if candidate_row:
        progress_lookup = lookup_talent_link(
            {
                **locator,
                "source_candidate_id": str(candidate_row.get("id") or ""),
                "candidate": candidate_row.get("name") or locator["candidate"],
                "company": candidate_row.get("company") or locator["company"],
                "title": candidate_row.get("title") or locator["title"],
            }
        )

    progress_match = progress_lookup.get("match") if progress_lookup.get("matched") else None
    if library.get("ambiguous"):
        status_name = "ambiguous"
        candidate_count = len(library.get("candidates") or [])
        chips = ["疑似已入库", f"{candidate_count or '多'}条需核对", "补全姓名/公司/职位"]
        message = "人才库存在疑似匹配，但证据不足以唯一定位；请核对字段或打开定位诊断。"
    elif not library.get("matched"):
        status_name = "not_in_library"
        chips = ["未入库", "可确认入库"]
        message = "v3 人才库未找到该 X-SaaS 人选。"
    elif progress_match:
        status_name = "progress_matched"
        stage = clean(progress_match.get("clean_stage", "")) or "已有推进关系"
        chips = ["已入库", f"已有推进关系：{stage}"]
        if progress_match.get("latest_event_status"):
            chips.append(f"最近事件：{progress_match.get('latest_event_status')}")
        message = f"已定位到当前客户/岗位推进关系：人才ID {candidate_row.get('id') or '-'} / 推进ID {progress_lookup.get('job_candidate_id') or '-'}。"
    else:
        status_name = "library_only"
        original_project = " / ".join(
            item
            for item in [clean(candidate_row.get("client", "")), clean(candidate_row.get("position", ""))]
            if item
        )
        chips = ["已入库", "未关联当前岗位"]
        if original_project:
            chips.append(f"原关联：{original_project}")
        message = (
            f"人才库已有该人：人才ID {candidate_row.get('id') or '-'}"
            + (f"，原关联 {original_project}" if original_project else "")
            + "；当前客户/岗位下未定位到推进关系。"
        )

    return {
        "ok": True,
        "status": status_name,
        "message": message,
        "chips": chips,
        "candidate_matched": bool(library.get("matched")),
        "candidate": candidate_row,
        "candidate_matches": library.get("candidates") or [],
        "progress_matched": bool(progress_match),
        "job_candidate_id": progress_lookup.get("job_candidate_id") if progress_match else None,
        "progress": progress_match,
        "progress_lookup": progress_lookup,
        "normalized": locator,
    }


def candidate_link_score(row: sqlite3.Row, locator: dict[str, str]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    profile_text = clean(locator.get("candidate_profile_text", ""))
    source_candidate_id = clean(locator.get("source_candidate_id", ""))

    row_source_id = clean(row["source_candidate_id"] or "")
    row_candidate_id = clean(row["candidate_id"] or "")
    if source_candidate_id and source_candidate_id in {row_source_id, row_candidate_id}:
        score += 120
        reasons.append("来源ID一致")

    candidate = normalize_candidate_name(locator.get("candidate", ""))
    row_candidate = normalize_candidate_name(row["candidate"] or "")
    if candidate and row_candidate and candidate == row_candidate:
        score += 45
        reasons.append("候选人姓名一致")
    elif candidate_names_can_correspond(candidate, row_candidate):
        score += 35
        reasons.append("候选人姓名可互证")

    company = clean(locator.get("company", ""))
    row_company = clean(row["company"] or "")
    if company and row_company and (company in row_company or row_company in company):
        score += 40
        reasons.append("候选人公司相近")
    elif row_company and profile_text and row_company in profile_text:
        score += 40
        reasons.append("候选人公司出现在简历正文")

    title = clean(locator.get("title", ""))
    row_title = clean(row["title"] or "")
    if title and row_title and (title in row_title or row_title in title):
        score += 35
        reasons.append("候选人职位相近")
    elif row_title and profile_text and row_title in profile_text:
        score += 35
        reasons.append("候选人职位出现在简历正文")

    return score, reasons


def candidate_link_can_auto_match(score: int, reasons: list[str]) -> bool:
    if "来源ID一致" in reasons:
        return True
    if "候选人姓名一致" not in reasons and "候选人姓名可互证" not in reasons:
        return False
    identity_hits = {
        "候选人姓名一致",
        "候选人姓名可互证",
        "候选人公司相近",
        "候选人公司出现在简历正文",
        "候选人职位相近",
        "候选人职位出现在简历正文",
    }
    return score >= 75 and len(identity_hits.intersection(reasons)) >= 2


def clear_candidate_merge_confirmations() -> None:
    with CANDIDATE_MERGE_LOCK:
        CANDIDATE_MERGE_CONFIRMATIONS.clear()


def candidate_identity_locator(data: dict[str, Any]) -> dict[str, str]:
    source_type = clean(first_present(data, "source_type", "")).lower()
    source_url = first_present(data, "source_url", "")
    source_candidate_id = first_present(data, "source_candidate_id", "")
    if not source_candidate_id:
        source_candidate_id = extract_resume_id(source_url) if source_type == "liepin" else extract_xsaas_candidate_id(source_url)
    return {
        "source_type": source_type,
        "source_candidate_id": clean(source_candidate_id),
        "source_url": clean(source_url),
        "candidate": normalize_candidate_name(
            first_present(data, "candidate", "")
            or first_present(data, "candidateName", "")
            or first_present(data, "candidate_name", "")
        ),
        "company": clean(
            first_present(data, "company", "")
            or first_present(data, "candidateCompany", "")
            or first_present(data, "candidate_company", "")
        ),
        "title": clean(
            first_present(data, "title", "")
            or first_present(data, "candidateTitle", "")
            or first_present(data, "candidate_title", "")
        ),
        "client": clean(first_present(data, "client", "")),
        "position": clean(first_present(data, "position", "") or first_present(data, "job", "")),
        "raw_status": clean(first_present(data, "raw_status", "")),
    }


def identity_text_matches(left: str, right: str) -> bool:
    left_value = clean(left)
    right_value = clean(right)
    return bool(left_value and right_value and (left_value in right_value or right_value in left_value))


def candidate_identity_score(
    row: sqlite3.Row,
    locator: dict[str, str],
    source_types: set[str],
    exact_source: bool = False,
) -> tuple[int, list[str], bool]:
    reasons: list[str] = []
    score = 0
    if exact_source:
        score += 200
        reasons.append("来源账号ID一致")

    query_name = normalize_candidate_name(locator.get("candidate", ""))
    row_name = normalize_candidate_name(row["display_name"] or "")
    masked = is_masked_candidate_name(query_name) or is_masked_candidate_name(row_name)
    exact_name = bool(query_name and row_name and query_name == row_name)
    corresponding_name = candidate_names_can_correspond(query_name, row_name)
    if not exact_source and not corresponding_name:
        return 0, [], False
    if exact_name and not masked:
        score += 60
        reasons.append("完整姓名一致")
    elif corresponding_name:
        score += 20 if masked else 45
        reasons.append("遮罩姓名可互证" if masked else "姓名可互证")

    company_match = identity_text_matches(locator.get("company", ""), row["current_company"] or "")
    title_match = identity_text_matches(locator.get("title", ""), row["current_title"] or "")
    if company_match:
        score += 35
        reasons.append("当前公司一致")
    if title_match:
        score += 35
        reasons.append("当前职位一致")
    if locator.get("source_type") and source_types and locator["source_type"] not in source_types:
        reasons.append("跨来源档案")

    merge_allowed = exact_source or (
        (exact_name and not masked and (company_match or title_match))
        or (corresponding_name and masked and company_match and title_match)
    )
    return score, reasons, merge_allowed


def discover_candidate_identity_matches(
    data: dict[str, Any],
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(db_path or TALENT_DB)
    if not path.exists():
        return {"ok": False, "matches": [], "error": f"找不到 v3 数据库：{path}"}
    locator = candidate_identity_locator(data)
    if not locator["candidate"] and not locator["source_candidate_id"]:
        return {"ok": False, "matches": [], "error": "至少需要候选人姓名或来源候选人ID"}

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        current_person_id = parse_optional_int(data.get("current_person_id"))
        if locator["source_type"] and locator["source_candidate_id"]:
            current = conn.execute(
                """SELECT person_id FROM source_profiles
                   WHERE source_type = ? AND CAST(source_candidate_id AS TEXT) = ?
                   ORDER BY id DESC LIMIT 1""",
                (locator["source_type"], locator["source_candidate_id"]),
            ).fetchone()
            if current:
                current_person_id = int(current["person_id"])

        rows = conn.execute(
            "SELECT id, display_name, current_company, current_title, city, education, experience FROM people ORDER BY id DESC"
        ).fetchall()
        profile_rows = conn.execute(
            "SELECT person_id, source_type, source_candidate_id, raw_json FROM source_profiles"
        ).fetchall()
    finally:
        conn.close()

    profiles_by_person: dict[int, list[dict[str, Any]]] = {}
    for profile in profile_rows:
        raw = {}
        try:
            raw = json.loads(profile["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
        profiles_by_person.setdefault(int(profile["person_id"]), []).append(
            {
                "source_type": clean(profile["source_type"]),
                "source_candidate_id": clean(profile["source_candidate_id"]),
                "source_url": clean(raw.get("source_url", "")) if isinstance(raw, dict) else "",
            }
        )

    matches: list[dict[str, Any]] = []
    for row in rows:
        person_id = int(row["id"])
        if current_person_id == person_id:
            continue
        profiles = profiles_by_person.get(person_id, [])
        source_types = {clean(item["source_type"]).lower() for item in profiles if item["source_type"]}
        exact_source = any(
            locator["source_type"] == clean(item["source_type"]).lower()
            and locator["source_candidate_id"]
            and locator["source_candidate_id"] == clean(item["source_candidate_id"])
            for item in profiles
        )
        score, reasons, merge_allowed = candidate_identity_score(row, locator, source_types, exact_source)
        if score <= 0:
            continue
        confidence = "high" if merge_allowed else "medium" if score >= 55 else "low"
        matches.append(
            {
                "person_id": person_id,
                "candidate": clean(row["display_name"]),
                "company": clean(row["current_company"]),
                "title": clean(row["current_title"]),
                "city": clean(row["city"]),
                "education": clean(row["education"]),
                "experience": clean(row["experience"]),
                "score": score,
                "confidence": confidence,
                "merge_allowed": merge_allowed,
                "reasons": reasons,
                "source_profiles": profiles,
            }
        )
    matches.sort(key=lambda item: (item["merge_allowed"], item["score"], item["person_id"]), reverse=True)
    return {
        "ok": True,
        "decision": "ask" if any(item["merge_allowed"] for item in matches) else "deny",
        "current_person_id": current_person_id,
        "normalized": locator,
        "matches": matches[:20],
    }


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def candidate_merge_signature(data: dict[str, Any], db_path: Path) -> str:
    source_profile = data.get("source_profile") if isinstance(data.get("source_profile"), dict) else {}
    payload = {
        "db_path": str(db_path.resolve()),
        "canonical_person_id": int(data.get("canonical_person_id") or 0),
        "merged_person_id": int(data.get("merged_person_id") or 0),
        "source_profile": candidate_identity_locator(source_profile),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def candidate_merge_candidate_ids(conn: sqlite3.Connection, person_id: int) -> list[int]:
    if not sqlite_table_exists(conn, "candidates"):
        return []
    ids: set[int] = set()
    for row in conn.execute(
        "SELECT source_candidate_id FROM job_candidates WHERE person_id = ?", (person_id,)
    ).fetchall():
        value = clean(row["source_candidate_id"])
        if value.isdigit() and conn.execute("SELECT 1 FROM candidates WHERE id = ?", (int(value),)).fetchone():
            ids.add(int(value))
    for row in conn.execute(
        "SELECT source_type, source_candidate_id FROM source_profiles WHERE person_id = ?", (person_id,)
    ).fetchall():
        if clean(row["source_type"]).lower() != "xsaas" or not clean(row["source_candidate_id"]):
            continue
        candidate = conn.execute(
            "SELECT id FROM candidates WHERE source = 'xsaas' AND CAST(xsaas_id AS TEXT) = ? ORDER BY id LIMIT 1",
            (clean(row["source_candidate_id"]),),
        ).fetchone()
        if candidate:
            ids.add(int(candidate["id"]))
    return sorted(ids)


def build_candidate_merge_preflight(
    conn: sqlite3.Connection,
    data: dict[str, Any],
) -> dict[str, Any]:
    requested_canonical_person_id = int(data.get("canonical_person_id") or 0)
    requested_merged_person_id = int(data.get("merged_person_id") or 0)
    if not requested_canonical_person_id or not requested_merged_person_id or requested_canonical_person_id == requested_merged_person_id:
        return {"ok": False, "decision": "deny", "error": "必须选择两个不同的人才档案"}
    people = conn.execute(
        "SELECT id, display_name, current_company, current_title FROM people WHERE id IN (?, ?)",
        (requested_canonical_person_id, requested_merged_person_id),
    ).fetchall()
    by_id = {int(row["id"]): row for row in people}
    if requested_canonical_person_id not in by_id or requested_merged_person_id not in by_id:
        return {"ok": False, "decision": "deny", "error": "待合并的人才档案不存在"}

    source_profile = data.get("source_profile") if isinstance(data.get("source_profile"), dict) else {}
    locator = candidate_identity_locator(source_profile)
    if not locator["candidate"]:
        merged = by_id[requested_merged_person_id]
        locator.update(
            {
                "candidate": clean(merged["display_name"]),
                "company": clean(merged["current_company"]),
                "title": clean(merged["current_title"]),
            }
        )
    identity_target = by_id[requested_canonical_person_id]
    score, reasons, merge_allowed = candidate_identity_score(identity_target, locator, set())
    if not merge_allowed:
        return {
            "ok": False,
            "decision": "deny",
            "error": "身份互证不足，禁止合并",
            "evidence": {"score": score, "reasons": reasons},
        }

    def person_quality(row: sqlite3.Row) -> int:
        name = normalize_candidate_name(row["display_name"] or "")
        masked = is_masked_candidate_name(name)
        return (100 if name and not masked else 20 if name else 0) + (
            20 if clean(row["current_company"]) else 0
        ) + (20 if clean(row["current_title"]) else 0)

    canonical_person_id = requested_canonical_person_id
    merged_person_id = requested_merged_person_id
    if person_quality(by_id[requested_merged_person_id]) > person_quality(by_id[requested_canonical_person_id]):
        canonical_person_id, merged_person_id = merged_person_id, canonical_person_id
    canonical = by_id[canonical_person_id]

    relations = conn.execute(
        """SELECT id, job_id, person_id, COALESCE(raw_position, '') AS raw_position,
                  COALESCE(clean_stage, '') AS clean_stage, source_candidate_id
           FROM job_candidates WHERE person_id IN (?, ?) ORDER BY id""",
        (canonical_person_id, merged_person_id),
    ).fetchall()
    relation_merges: list[dict[str, int]] = []
    conflicts: list[str] = []
    relation_groups: dict[tuple[Any, str], list[sqlite3.Row]] = {}
    for row in relations:
        relation_groups.setdefault((row["job_id"], clean(row["raw_position"])), []).append(row)
    for group in relation_groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda item: (int(item["person_id"]) != canonical_person_id, int(item["id"])))
        survivor = group[0]
        stages = {clean(item["clean_stage"]) for item in group if clean(item["clean_stage"])}
        if len(stages) > 1:
            conflicts.append(f"同岗位推进阶段冲突：{' / '.join(sorted(stages))}")
        review_statuses: set[str] = set()
        for group_row in group:
            review_statuses.update(
                clean(item["event_status"]).lower()
                for item in conn.execute(
                    "SELECT event_status FROM candidate_events WHERE job_candidate_id = ? AND event_type = 'resume_review'",
                    (int(group_row["id"]),),
                ).fetchall()
                if clean(item["event_status"])
            )
        stop_markers = {"stop", "stopped", "reject", "rejected", "淘汰", "停止"}
        continue_markers = {"continue", "continued", "pass", "passed", "推进", "继续"}
        if review_statuses.intersection(stop_markers) and review_statuses.intersection(continue_markers):
            conflicts.append("同岗位复核结论冲突：存在继续推进与停止推进记录")
        relation_merges.extend(
            {"survivor_id": int(survivor["id"]), "removed_id": int(group_row["id"])}
            for group_row in group[1:]
        )

    if conflicts:
        return {"ok": False, "decision": "deny", "error": conflicts[0], "conflicts": conflicts}

    canonical_candidate_ids = candidate_merge_candidate_ids(conn, canonical_person_id)
    merged_candidate_ids = candidate_merge_candidate_ids(conn, merged_person_id)
    candidate_groups: dict[tuple[str, str], list[int]] = {}
    for candidate_id in sorted(set(canonical_candidate_ids + merged_candidate_ids)):
        candidate_row = conn.execute(
            "SELECT COALESCE(client, '') AS client, COALESCE(position, '') AS position FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if candidate_row:
            candidate_groups.setdefault(
                (clean(candidate_row["client"]), clean(candidate_row["position"])), []
            ).append(candidate_id)
    candidate_merges: list[dict[str, int]] = []
    for group in candidate_groups.values():
        if len(group) < 2:
            continue
        survivor_id = next((item for item in group if item in canonical_candidate_ids), min(group))
        candidate_merges.extend(
            {"survivor_id": survivor_id, "removed_id": item}
            for item in group
            if item != survivor_id
        )
    surviving_candidate_id = canonical_candidate_ids[0] if canonical_candidate_ids else (
        merged_candidate_ids[0] if merged_candidate_ids else None
    )
    removed_candidate_ids = sorted({item["removed_id"] for item in candidate_merges})
    return {
        "ok": True,
        "decision": "ask",
        "canonical": {
            "person_id": canonical_person_id,
            "candidate": clean(canonical["display_name"]),
            "company": clean(canonical["current_company"]),
            "title": clean(canonical["current_title"]),
        },
        "merged": {
            "person_id": merged_person_id,
            "candidate": clean(by_id[merged_person_id]["display_name"]),
            "company": clean(by_id[merged_person_id]["current_company"]),
            "title": clean(by_id[merged_person_id]["current_title"]),
        },
        "evidence": {"score": score, "reasons": reasons},
        "plan": {
            "source_profiles": conn.execute(
                "SELECT COUNT(*) FROM source_profiles WHERE person_id = ?", (merged_person_id,)
            ).fetchone()[0],
            "job_relations": conn.execute(
                "SELECT COUNT(*) FROM job_candidates WHERE person_id = ?", (merged_person_id,)
            ).fetchone()[0],
            "events": conn.execute(
                "SELECT COUNT(*) FROM candidate_events WHERE person_id = ?", (merged_person_id,)
            ).fetchone()[0],
            "relation_merges": relation_merges,
            "surviving_candidate_id": surviving_candidate_id,
            "removed_candidate_ids": removed_candidate_ids,
            "candidate_merges": candidate_merges,
        },
        "source_profile": locator,
    }


def ensure_candidate_merge_audit_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS candidate_merge_audit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               canonical_person_id INTEGER NOT NULL,
               merged_person_id INTEGER NOT NULL,
               source_type TEXT,
               source_candidate_id TEXT,
               evidence_json TEXT NOT NULL,
               snapshot_json TEXT NOT NULL,
               actor TEXT NOT NULL,
               created_at TEXT DEFAULT (datetime('now','localtime'))
           )"""
    )


def merge_candidate_profiles(
    data: dict[str, Any],
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(db_path or TALENT_DB)
    if not path.exists():
        return {"ok": False, "decision": "deny", "error": f"找不到 v3 数据库：{path}"}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        preflight = build_candidate_merge_preflight(conn, data)
        if not preflight.get("ok"):
            return preflight
        signature = candidate_merge_signature(data, path)
        if not truthy(data.get("write")):
            now = time.time()
            token = secrets.token_urlsafe(32)
            with CANDIDATE_MERGE_LOCK:
                expired = [key for key, value in CANDIDATE_MERGE_CONFIRMATIONS.items() if value["expires_at"] <= now]
                for key in expired:
                    CANDIDATE_MERGE_CONFIRMATIONS.pop(key, None)
                CANDIDATE_MERGE_CONFIRMATIONS[token] = {
                    "signature": signature,
                    "expires_at": now + CANDIDATE_MERGE_CONFIRMATION_TTL_SECONDS,
                }
            return {
                **preflight,
                "confirmation_token": token,
                "confirmation_expires_in": CANDIDATE_MERGE_CONFIRMATION_TTL_SECONDS,
            }

        token = clean(data.get("confirmation_token"))
        with CANDIDATE_MERGE_LOCK:
            confirmation = CANDIDATE_MERGE_CONFIRMATIONS.get(token)
            if confirmation and confirmation["expires_at"] > time.time() and confirmation["signature"] == signature:
                CANDIDATE_MERGE_CONFIRMATIONS.pop(token, None)
            else:
                confirmation = None
        if not confirmation:
            return {"ok": False, "decision": "deny", "error": "合并确认已失效，请重新对比档案"}

        canonical_person_id = int(preflight["canonical"]["person_id"])
        merged_person_id = int(preflight["merged"]["person_id"])
        plan = preflight["plan"]
        snapshot = {
            "canonical": preflight["canonical"],
            "merged": preflight["merged"],
            "plan": plan,
            "source_profile": preflight["source_profile"],
        }
        conn.execute("BEGIN IMMEDIATE")
        ensure_candidate_merge_audit_schema(conn)

        source = preflight["source_profile"]
        if source["source_type"] and source["source_candidate_id"]:
            existing_profile = conn.execute(
                """SELECT id FROM source_profiles
                   WHERE source_type = ? AND CAST(source_candidate_id AS TEXT) = ? LIMIT 1""",
                (source["source_type"], source["source_candidate_id"]),
            ).fetchone()
            if not existing_profile:
                conn.execute(
                    """INSERT INTO source_profiles(
                           person_id, source_type, source_candidate_id, source_date,
                           raw_status, raw_client, raw_position, raw_json
                       ) VALUES (?, ?, ?, date('now','localtime'), ?, ?, ?, ?)""",
                    (
                        merged_person_id,
                        source["source_type"],
                        source["source_candidate_id"],
                        source["raw_status"],
                        source["client"],
                        source["position"],
                        json.dumps(source, ensure_ascii=False),
                    ),
                )
        conn.execute("UPDATE source_profiles SET person_id = ? WHERE person_id = ?", (canonical_person_id, merged_person_id))

        relation_map = {
            int(item["removed_id"]): int(item["survivor_id"])
            for item in plan["relation_merges"]
        }
        for removed_id, survivor_id in relation_map.items():
            conn.execute("UPDATE candidate_events SET job_candidate_id = ? WHERE job_candidate_id = ?", (survivor_id, removed_id))
            for table in ("followup_tasks", "client_feedback_events"):
                if sqlite_table_exists(conn, table):
                    conn.execute(f"UPDATE {table} SET job_candidate_id = ? WHERE job_candidate_id = ?", (survivor_id, removed_id))
            conn.execute("DELETE FROM job_candidates WHERE id = ?", (removed_id,))
        conn.execute("UPDATE job_candidates SET person_id = ? WHERE person_id = ?", (canonical_person_id, merged_person_id))
        conn.execute("UPDATE candidate_events SET person_id = ? WHERE person_id = ?", (canonical_person_id, merged_person_id))

        for candidate_merge in plan.get("candidate_merges", []):
            surviving_candidate_id = int(candidate_merge["survivor_id"])
            removed_candidate_id = int(candidate_merge["removed_id"])
            conn.execute(
                """UPDATE job_candidates SET source_candidate_id = ?
                   WHERE CAST(source_candidate_id AS TEXT) = CAST(? AS TEXT)""",
                (surviving_candidate_id, removed_candidate_id),
            )
            for table in (
                "candidate_profiles",
                "candidate_intelligence",
                "candidate_replies",
                "outreach_events",
                "followup_tasks",
                "client_feedback_events",
            ):
                if sqlite_table_exists(conn, table):
                    conn.execute(
                        f"UPDATE {table} SET candidate_id = ? WHERE candidate_id = ?",
                        (surviving_candidate_id, removed_candidate_id),
                    )
            conn.execute("DELETE FROM candidates WHERE id = ?", (removed_candidate_id,))

        conn.execute(
            """INSERT INTO candidate_merge_audit(
                   canonical_person_id, merged_person_id, source_type, source_candidate_id,
                   evidence_json, snapshot_json, actor
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                canonical_person_id,
                merged_person_id,
                source["source_type"],
                source["source_candidate_id"],
                json.dumps(preflight["evidence"], ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                clean(data.get("actor")) or "candidate-assistant",
            ),
        )
        conn.execute("DELETE FROM people WHERE id = ?", (merged_person_id,))
        conn.commit()
        return {
            "ok": True,
            "decision": "allow",
            "message": "候选人档案已合并，两个来源简历均已保留",
            "canonical_person_id": canonical_person_id,
            "merged_person_id": merged_person_id,
            "plan": plan,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def lookup_talent_link(data: dict[str, Any]) -> dict[str, Any]:
    if not TALENT_DB.exists():
        return {"ok": False, "matched": False, "reason": f"找不到 v3 数据库：{TALENT_DB}"}
    locator = data.get("locator") if isinstance(data.get("locator"), dict) else data
    job_candidate_id = parse_optional_int(first_present(locator, "job_candidate_id", ""))
    client, job = normalize_talent_project(
        first_present(locator, "client", ""),
        first_present(locator, "job") or first_present(locator, "position", ""),
    )
    candidate = normalize_candidate_name(
        first_present(locator, "candidate")
        or first_present(locator, "candidateName")
        or first_present(locator, "candidate_name", "")
    )
    company = first_present(locator, "company") or first_present(locator, "candidateCompany") or first_present(locator, "candidate_company", "")
    title = first_present(locator, "title") or first_present(locator, "candidateTitle") or first_present(locator, "candidate_title", "")
    source_candidate_id = first_present(locator, "source_candidate_id", "") or extract_resume_id(first_present(locator, "source_url", ""))
    profile_text = first_present(locator, "candidate_profile_text", "")
    project_resolution: dict[str, Any] | None = None

    if not client or not job:
        project_resolution = lookup_talent_current_project(
            {
                "candidate_name": candidate,
                "candidate_company": company,
                "candidate_title": title,
                "candidate_profile_text": profile_text,
            }
        )
        project = project_resolution.get("project") if isinstance(project_resolution, dict) else {}
        resolved_client = clean(project.get("client", "")) if isinstance(project, dict) else ""
        resolved_job = clean(project.get("position", "")) if isinstance(project, dict) else ""
        if resolved_client and resolved_job:
            client, job = normalize_talent_project(resolved_client, resolved_job)

    conn = sqlite3.connect(TALENT_DB)
    conn.row_factory = sqlite3.Row
    try:
        ensure_effective_candidate_events_schema(conn)
        select_body = """
            SELECT
                jc.id AS job_candidate_id,
                jc.person_id,
                jc.source_candidate_id,
                jc.clean_stage,
                p.display_name AS candidate,
                p.current_company AS company,
                p.current_title AS title,
                c.name AS client,
                j.title AS job,
                cand.id AS candidate_id,
                (
                    SELECT COUNT(*)
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type = 'resume_review_completed'
                ) AS review_count,
                (
                    SELECT ce.event_status
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type = 'resume_review_completed'
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_review_status,
                (
                    SELECT ce.event_time
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type = 'resume_review_completed'
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_review_time,
                (
                    SELECT ce.summary
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type = 'resume_review_completed'
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_review_summary,
                (
                    SELECT ce.event_type
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type IN (
                          'liepin_outreach',
                          'outreach',
                          'outreach_status_backfill',
                          'candidate_outreach',
                          'candidate_message_sent',
                          'candidate_message_received'
                      )
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_outreach_type,
                (
                    SELECT ce.event_status
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type IN (
                          'liepin_outreach',
                          'outreach',
                          'outreach_status_backfill',
                          'candidate_outreach',
                          'candidate_message_sent',
                          'candidate_message_received'
                      )
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_outreach_status,
                (
                    SELECT ce.event_time
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                      AND ce.event_type IN (
                          'liepin_outreach',
                          'outreach',
                          'outreach_status_backfill',
                          'candidate_outreach',
                          'candidate_message_sent',
                          'candidate_message_received'
                      )
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_outreach_time,
                (
                    SELECT ce.event_type
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_event_type,
                (
                    SELECT ce.event_status
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_event_status,
                (
                    SELECT ce.event_time
                    FROM v_effective_candidate_events ce
                    WHERE ce.job_candidate_id = jc.id
                    ORDER BY ce.id DESC
                    LIMIT 1
                ) AS latest_event_time
            FROM job_candidates jc
            JOIN people p ON p.id = jc.person_id
            LEFT JOIN jobs j ON j.id = jc.job_id
            LEFT JOIN clients c ON c.id = j.client_id
            LEFT JOIN candidates cand ON cand.id = CAST(jc.source_candidate_id AS INTEGER)
        """
        base_select = select_body + " WHERE c.name = ? AND j.title = ?"
        broad_select = select_body + " WHERE COALESCE(c.name, '') != '' AND COALESCE(j.title, '') != ''"
        if job_candidate_id is not None:
            rows = conn.execute(select_body + " WHERE jc.id = ? ORDER BY jc.id", [job_candidate_id]).fetchall()
            if len(rows) == 1:
                matched_client, matched_job = normalize_talent_project(clean(rows[0]["client"]), clean(rows[0]["job"]))
                return {
                    "ok": True,
                    "matched": True,
                    "reason": "job_candidate_id",
                    "job_candidate_id": int(rows[0]["job_candidate_id"]),
                    "match": candidate_match_payload(rows[0]),
                    "project_resolution": project_resolution,
                    "normalized": {
                        "client": matched_client,
                        "job": matched_job,
                        "candidate": candidate,
                        "company": company,
                        "title": title,
                        "source_candidate_id": source_candidate_id,
                    },
                }
            return {
                "ok": True,
                "matched": False,
                "reason": "job_candidate_id_not_found",
                "matches": [],
                "project_resolution": project_resolution,
                "normalized": {
                    "client": client,
                    "job": job,
                    "candidate": candidate,
                    "company": company,
                    "title": title,
                    "source_candidate_id": source_candidate_id,
                },
            }
        attempts: list[tuple[str, str, list[Any]]] = []
        if client and job:
            if source_candidate_id:
                attempts.append(("source_candidate_id", base_select + " AND jc.source_candidate_id = ? ORDER BY jc.id", [client, job, source_candidate_id]))
            if candidate:
                attempts.append(("candidate_exact", base_select + " AND p.display_name = ? ORDER BY jc.id", [client, job, candidate]))
            if candidate and company:
                attempts.append((
                    "candidate_company_like",
                    base_select + " AND p.display_name = ? AND COALESCE(p.current_company, '') LIKE ? ORDER BY jc.id",
                    [client, job, candidate, like_text(company)],
                ))
            if company and title:
                attempts.append((
                    "company_title_like",
                    base_select + " AND COALESCE(p.current_company, '') LIKE ? AND COALESCE(p.current_title, '') LIKE ? ORDER BY jc.id",
                    [client, job, like_text(company), like_text(title)],
                ))
            if company:
                attempts.append((
                    "company_like",
                    base_select + " AND COALESCE(p.current_company, '') LIKE ? ORDER BY jc.id",
                    [client, job, like_text(company)],
                ))
        else:
            if source_candidate_id:
                attempts.append(("source_candidate_id_global", broad_select + " AND jc.source_candidate_id = ? ORDER BY jc.id", [source_candidate_id]))
            if candidate and company:
                attempts.append((
                    "candidate_company_like_global",
                    broad_select + " AND p.display_name = ? AND COALESCE(p.current_company, '') LIKE ? ORDER BY jc.id",
                    [candidate, like_text(company)],
                ))
            if candidate and title:
                attempts.append((
                    "candidate_title_like_global",
                    broad_select + " AND p.display_name = ? AND COALESCE(p.current_title, '') LIKE ? ORDER BY jc.id",
                    [candidate, like_text(title)],
                ))

        seen: set[int] = set()
        collected: list[dict[str, Any]] = []
        for reason, query, params in attempts:
            rows = conn.execute(query, params).fetchall()
            matches = [candidate_match_payload(row) for row in rows]
            if len(rows) == 1:
                if reason in {"source_candidate_id", "source_candidate_id_global"}:
                    matched_client, matched_job = normalize_talent_project(clean(rows[0]["client"]), clean(rows[0]["job"]))
                    return {
                        "ok": True,
                        "matched": True,
                        "reason": reason,
                        "job_candidate_id": int(rows[0]["job_candidate_id"]),
                        "match": matches[0],
                        "project_resolution": project_resolution,
                        "normalized": {"client": matched_client, "job": matched_job, "candidate": candidate, "company": company, "title": title, "source_candidate_id": source_candidate_id},
                    }
                score, score_reasons = candidate_link_score(
                    rows[0],
                    {
                        "candidate": candidate,
                        "company": company,
                        "title": title,
                        "source_candidate_id": source_candidate_id,
                        "candidate_profile_text": profile_text,
                    },
                )
                if candidate_link_can_auto_match(score, score_reasons):
                    matched_client, matched_job = normalize_talent_project(clean(rows[0]["client"]), clean(rows[0]["job"]))
                    matches[0]["score"] = score
                    matches[0]["reasons"] = score_reasons
                    return {
                        "ok": True,
                        "matched": True,
                        "reason": reason,
                        "job_candidate_id": int(rows[0]["job_candidate_id"]),
                        "match": matches[0],
                        "project_resolution": project_resolution,
                        "normalized": {"client": matched_client, "job": matched_job, "candidate": candidate, "company": company, "title": title, "source_candidate_id": source_candidate_id},
                    }
            for item in matches:
                if item["job_candidate_id"] not in seen:
                    seen.add(item["job_candidate_id"])
                    collected.append(item)
        if client and job:
            rows = conn.execute(base_select + " ORDER BY jc.id", [client, job]).fetchall()
        else:
            rows = conn.execute(broad_select + " ORDER BY jc.id").fetchall()
        scored: list[tuple[int, sqlite3.Row, list[str]]] = []
        scoring_locator = {
            "candidate": candidate,
            "company": company,
            "title": title,
            "source_candidate_id": source_candidate_id,
            "candidate_profile_text": profile_text,
        }
        for row in rows:
            score, reasons = candidate_link_score(row, scoring_locator)
            if candidate_link_can_auto_match(score, reasons):
                scored.append((score, row, reasons))
        if scored:
            scored.sort(key=lambda item: (item[0], int(item[1]["job_candidate_id"] or 0)), reverse=True)
            top_score, top_row, top_reasons = scored[0]
            tied = [item for item in scored if item[0] == top_score]
            if len(tied) == 1:
                match = candidate_match_payload(top_row)
                matched_client, matched_job = normalize_talent_project(clean(top_row["client"]), clean(top_row["job"]))
                match["score"] = top_score
                match["reasons"] = top_reasons
                return {
                    "ok": True,
                    "matched": True,
                    "reason": "scored_identity",
                    "job_candidate_id": int(top_row["job_candidate_id"]),
                    "match": match,
                    "project_resolution": project_resolution,
                    "normalized": {"client": matched_client, "job": matched_job, "candidate": candidate, "company": company, "title": title, "source_candidate_id": source_candidate_id},
                }
            for _score, row, reasons in scored[:8]:
                item = candidate_match_payload(row)
                item["score"] = _score
                item["reasons"] = reasons
                if item["job_candidate_id"] not in seen:
                    seen.add(item["job_candidate_id"])
                    collected.append(item)
        return {
            "ok": True,
            "matched": False,
            "reason": "no_unique_match",
            "matches": collected[:8],
            "project_resolution": project_resolution,
            "normalized": {"client": client, "job": job, "candidate": candidate, "company": company, "title": title, "source_candidate_id": source_candidate_id},
        }
    finally:
        conn.close()


def enrich_talent_action_locator(data: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(data)
    client, job = normalize_talent_project(
        first_present(enriched, "client", ""),
        first_present(enriched, "job") or first_present(enriched, "position", ""),
    )
    enriched["client"] = client
    enriched["job"] = job
    lookup = lookup_talent_link(enriched)
    enriched["_talent_lookup"] = lookup
    if lookup.get("matched") and lookup.get("job_candidate_id"):
        enriched["job_candidate_id"] = lookup["job_candidate_id"]
    return enriched


def make_action_id(prefix: str, data: dict[str, Any]) -> str:
    stable_parts = {
        key: data.get(key)
        for key in [
            "kind",
            "action_type",
            "job_candidate_id",
            "candidate",
            "candidateName",
            "candidate_name",
            "client",
            "job",
            "position",
            "source_url",
            "summary",
            "message_summary",
            "review_result",
            "direction",
        ]
        if data.get(key) not in (None, "")
    }
    seed = json.dumps(stable_parts or data, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def normalize_message_direction_text(value: Any) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】<>《》-]+", "", clean(value)).lower()


def build_talent_action(data: dict[str, Any]) -> dict[str, Any]:
    kind = first_present(data, "kind") or first_present(data, "action_type", "")
    if kind == "resume_review_undo":
        action = action_locator(data)
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("resume_review_undo", data),
                "kind": "resume_review_undo",
                "summary": first_present(data, "summary", "") or "撤销上一次简历复核",
                "previous_clean_stage": first_present(data, "previous_clean_stage", ""),
                "previous_flow_bucket": first_present(data, "previous_flow_bucket", ""),
                "previous_raw_status": first_present(data, "previous_raw_status", ""),
                "previous_raw_stage": first_present(data, "previous_raw_stage", ""),
                "previous_candidate_status": first_present(data, "previous_candidate_status", ""),
                "raw": {
                    "source": "a_system_batch_review",
                    "plugin_surface": "candidate_batch",
                    "undo_review_result": first_present(data, "undo_review_result", ""),
                },
            }
        )
        return action
    if kind in {"xsaas_intake", "xsaas_candidate_intake"}:
        action = action_locator(data)
        source_url = first_present(data, "source_url", "")
        xsaas_id = first_present(data, "xsaas_id", "") or first_present(data, "source_candidate_id", "") or extract_xsaas_candidate_id(source_url)
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("xsaas_intake", data),
                "kind": "xsaas_intake",
                "source": "xsaas",
                "source_candidate_id": xsaas_id or "xsaas_plugin",
                "xsaas_id": xsaas_id,
                "city": first_present(data, "city", "") or first_present(data, "location", ""),
                "education": first_present(data, "education", ""),
                "experience": first_present(data, "experience", "") or first_present(data, "years", ""),
                "skills": first_present(data, "skills", ""),
                "level": first_present(data, "level", ""),
                "fit_score": first_present(data, "fit_score", "") or first_present(data, "score", ""),
                "fit_level": first_present(data, "fit_level", "") or first_present(data, "grade", ""),
                "profile_summary": first_present(data, "profile_summary", "") or first_present(data, "candidate_profile_text", ""),
                "summary": first_present(data, "summary", "") or "X-SaaS插件确认入库",
                "reason": first_present(data, "reason", "") or "X-SaaS插件确认入库，待人工复核",
                "clean_stage": first_present(data, "clean_stage", "") or "X1 X-SaaS入库/待复核",
                "flow_bucket": first_present(data, "flow_bucket", "") or "X-SaaS库内",
                "raw": {
                    "source": "xsaas_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "xsaas_candidate"),
                    "source_url": source_url,
                    "xsaas_id": xsaas_id,
                    "score": first_present(data, "score", ""),
                    "grade": first_present(data, "grade", ""),
                    "salary": first_present(data, "salary", ""),
                    "expected_salary": first_present(data, "expected_salary", ""),
                    "candidate_profile_text": first_present(data, "candidate_profile_text", ""),
                },
            }
        )
        return action

    if kind in {"xsaas_review", "xsaas_resume_review"}:
        action = action_locator(data)
        result = first_present(data, "review_result", "") or first_present(data, "result", "") or "continue"
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("xsaas_review", data),
                "kind": "xsaas_review",
                "review_result": result,
                "summary": first_present(data, "summary", "") or f"X-SaaS插件复核：{result}",
                "next_action": first_present(data, "next_action", ""),
                "reason": first_present(data, "reason", ""),
                "stop_reason_code": first_present(data, "stop_reason_code", ""),
                "stop_reason_label": first_present(data, "stop_reason_label", ""),
                "stop_reason_note": first_present(data, "stop_reason_note", ""),
                "raw": {
                    "source": "xsaas_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "xsaas_candidate"),
                    "source_url": first_present(data, "source_url", ""),
                    "xsaas_id": first_present(data, "xsaas_id", "") or first_present(data, "source_candidate_id", ""),
                    "score": first_present(data, "score", ""),
                    "grade": first_present(data, "grade", ""),
                },
            }
        )
        if result == "stop":
            # R10 停止原因标准化：枚举命中保留；缺失/未知/自由文本降级 other
            # 并把原文并入 stop_reason_note（保持转发给同步引擎的语义不变）。
            action["stop_reason_code"], action["stop_reason_note"] = normalize_stop_reason(
                action.get("stop_reason_code"), action.get("stop_reason_note") or ""
            )
        stage_after = first_present(data, "stage_after", "")
        if stage_after:
            action["stage_after"] = stage_after
            action["flow_bucket"] = first_present(data, "flow_bucket", "")
        return action

    if kind in {"candidate_intake", "intake"}:
        action = action_locator(data)
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("candidate_intake", data),
                "kind": "candidate_intake",
                "summary": first_present(data, "summary", "") or "插件确认入库",
                "reason": first_present(data, "reason", "") or "插件内人工确认加入统一人才库",
                "clean_stage": first_present(data, "clean_stage", "H1 最近寻访/待筛") or "H1 最近寻访/待筛",
                "flow_bucket": first_present(data, "flow_bucket", "最近寻访") or "最近寻访",
                "source_candidate_id": first_present(data, "source_candidate_id", "") or extract_resume_id(first_present(data, "source_url", "")) or "liepin_plugin",
                "raw": {
                    "source": "liepin_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "resume_match"),
                    "source_url": first_present(data, "source_url", ""),
                    "score": first_present(data, "score", ""),
                    "grade": first_present(data, "grade", ""),
                },
            }
        )
        if truthy(first_present(data, "pool_only", "")):
            action["pool_only"] = True
        return action
    if kind in {"resume_review", "review"}:
        action = action_locator(data)
        result = first_present(data, "review_result", "") or first_present(data, "result", "") or "reviewed"
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("resume_review", data),
                "kind": "resume_review",
                "review_result": result,
                "summary": first_present(data, "summary", "")
                or f"插件内简历复核：{result}",
                "next_action": first_present(data, "next_action", ""),
                "reason": first_present(data, "reason", ""),
                "raw": {
                    "source": "liepin_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "resume_match"),
                    "source_url": first_present(data, "source_url", ""),
                    "score": first_present(data, "score", ""),
                    "grade": first_present(data, "grade", ""),
                    "outreach_status": first_present(data, "outreach_status", ""),
                    "verification_evidence": first_present(data, "verification_evidence", ""),
                    "contact_status": first_present(data, "contact_status", ""),
                    "contact_channel": first_present(data, "contact_channel", ""),
                    "contact_note": first_present(data, "contact_note", ""),
                    "stop_reason_code": first_present(data, "stop_reason_code", ""),
                    "stop_reason_label": first_present(data, "stop_reason_label", ""),
                    "stop_reason_note": first_present(data, "stop_reason_note", ""),
                },
            }
        )
        if result == "stop":
            # R10：与 xsaas_review 同一停止原因归一规则（该分支字段在 raw 内）。
            raw_stop = action["raw"]
            raw_stop["stop_reason_code"], raw_stop["stop_reason_note"] = normalize_stop_reason(
                raw_stop.get("stop_reason_code"), raw_stop.get("stop_reason_note") or ""
            )
        stage_after = first_present(data, "stage_after", "")
        if stage_after:
            action["stage_after"] = stage_after
            action["flow_bucket"] = first_present(data, "flow_bucket", "") or "正式流程"
        outreach_status = first_present(data, "outreach_status", "")
        if outreach_status:
            action["outreach_status"] = outreach_status
            action["raw_status"] = outreach_status
            action["verification_evidence"] = first_present(data, "verification_evidence", "")
        for field in ("contact_status", "contact_channel", "contact_note"):
            value = first_present(data, field, "")
            if value:
                action[field] = value
        return action

    if kind in {"candidate_message", "message"}:
        action = action_locator(data)
        direction = first_present(data, "direction", "") or "sent"
        if direction == "pending":
            raise ValueError("pending 不是真实消息动作；请用 dry-run 或待确认动作，不要写成已发送。")
        message_preview = first_present(data, "message_preview", "")
        message_evidence = first_present(data, "message_evidence", "")
        outbound_draft_preview = first_present(data, "outbound_draft_preview", "")
        if direction == "received":
            if message_evidence not in {"explicit_inbound_dom", "manual_transcription"}:
                raise ValueError("候选人已回复必须提供明确的候选人入站消息证据。")
            if not clean(message_preview):
                raise ValueError("候选人已回复必须保留候选人回复原文。")
            if (
                normalize_message_direction_text(message_preview)
                and normalize_message_direction_text(message_preview) == normalize_message_direction_text(outbound_draft_preview)
            ):
                raise ValueError("候选人回复原文与我方草稿相同，禁止按已回复写入。")
        if direction == "sent" and message_evidence != "explicit_outbound_dom":
            raise ValueError("已发送消息必须提供明确的我方出站消息证据。")
        conversation_id = first_present(data, "conversation_id", "")
        conversation_confidence = first_present(data, "conversation_identity_confidence", "")
        message_id = first_present(data, "message_id", "")
        message_time = first_present(data, "message_time", "")
        if not conversation_id or "showmsgnewpage" in conversation_id:
            raise ValueError("真实消息动作必须绑定稳定的猎聘会话 ID。")
        if conversation_confidence not in {"dom_id", "url_id", "stable_contact_fallback"}:
            raise ValueError("真实消息动作缺少可信的会话身份来源。")
        if not message_id or not message_time:
            raise ValueError("真实消息动作必须保留消息 ID 和消息时间。")
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("candidate_message", data),
                "kind": "candidate_message",
                "direction": direction,
                "channel": first_present(data, "channel", "liepin") or "liepin",
                "message_status": first_present(data, "message_status", "done") or "done",
                "message_intent": first_present(data, "message_intent", ""),
                "summary": first_present(data, "summary", "")
                or first_present(data, "message_summary", "")
                or "插件内确认候选人沟通动作",
                "reason": first_present(data, "reason", ""),
                "raw": {
                    "source": "liepin_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "reply_assistant"),
                    "source_url": first_present(data, "source_url", ""),
                    "message_preview": message_preview,
                    "message_evidence": message_evidence,
                    "outbound_draft_preview": outbound_draft_preview,
                    "conversation_id": conversation_id,
                    "conversation_identity_confidence": conversation_confidence,
                    "message_id": message_id,
                    "message_time": message_time,
                },
            }
        )
        stage_after = first_present(data, "stage_after", "")
        if stage_after:
            action["stage_after"] = stage_after
            action["flow_bucket"] = first_present(data, "flow_bucket", "") or "正式流程"
        return action

    if kind in {"outreach_verification", "verify_outreach"}:
        action = action_locator(data)
        outreach_status = first_present(data, "outreach_status", "") or "job_chat_verified"
        action.update(
            {
                "action_id": first_present(data, "action_id", "") or make_action_id("outreach_verification", data),
                "kind": "outreach_verification",
                "outreach_status": outreach_status,
                "clean_stage": first_present(data, "clean_stage", "已触达") or "已触达",
                "flow_bucket": first_present(data, "flow_bucket", "") or (
                    "猎聘消息触达" if outreach_status in {"message_outreach_verified", "im_followup_verified"} else "猎聘触达"
                ),
                "summary": first_present(data, "summary", "")
                or f"猎聘插件核验触达：{outreach_status}",
                "reason": first_present(data, "reason", "") or "插件内人工确认猎聘触达已核验",
                "verification_evidence": first_present(data, "verification_evidence", ""),
                "raw": {
                    "source": "liepin_plugin",
                    "plugin_surface": first_present(data, "plugin_surface", "resume_match"),
                    "source_url": first_present(data, "source_url", ""),
                    "outreach_status": outreach_status,
                    "verification_evidence": first_present(data, "verification_evidence", ""),
                },
            }
        )
        return action

    raise ValueError(f"未知动作类型：{kind or '空'}")


def candidate_message_payload_hash(data: dict[str, Any]) -> str:
    excluded = {"confirmation_token", "write", "refresh_workbench", "refresh"}
    payload = {key: value for key, value in data.items() if key not in excluded and not key.startswith("_")}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_message_record(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    candidate_id = parse_optional_int(data.get("candidate_id"))
    if candidate_id is None and first_present(data, "job_candidate_id", ""):
        row = conn.execute(
            "SELECT source_candidate_id FROM job_candidates WHERE id = ? LIMIT 1",
            (int(first_present(data, "job_candidate_id", "")),),
        ).fetchone()
        if row and clean(row["source_candidate_id"]).isdigit():
            candidate_id = int(row["source_candidate_id"])
    args = make_namespace(
        candidate_id=candidate_id,
        candidate_name=first_present(data, "candidate_name", "") or first_present(data, "candidate", ""),
        candidate_company=first_present(data, "candidate_company", "") or first_present(data, "company", ""),
        candidate_title=first_present(data, "candidate_title", "") or first_present(data, "title", ""),
        client=first_present(data, "client", ""),
        position=first_present(data, "position", "") or first_present(data, "job", ""),
        channel=first_present(data, "channel", "liepin") or "liepin",
        conversation_id=first_present(data, "conversation_id", ""),
        message_time=first_present(data, "message_time", ""),
        raw_text=first_present(data, "raw_text", "") or first_present(data, "message_preview", ""),
    )
    candidate = load_candidate_by_id(conn, candidate_id)
    record = build_reply_record(args, candidate)
    record.update(
        {
            "message_id": first_present(data, "message_id", ""),
            "message_evidence": first_present(data, "message_evidence", ""),
            "conversation_identity_confidence": first_present(data, "conversation_identity_confidence", ""),
        }
    )
    return record


def candidate_message_preflight(state: "WorkbenchState", data: dict[str, Any]) -> dict[str, Any]:
    try:
        action = build_talent_action(data)
        sync = talent_sync_module()
        conn = connect_reply_db(state.db_path)
        try:
            ensure_reply_schema(conn)
            resolve_status, row, matches = sync.resolve_action_candidate(conn, action)
            if row is None:
                return {
                    "ok": False,
                    "decision": "deny",
                    "reason": resolve_status,
                    "matches": matches,
                }
            if action["direction"] == "received":
                record = candidate_message_record(conn, data)
                classification = {
                    key: record[key]
                    for key in (
                        "intent",
                        "sentiment",
                        "suggested_next_action",
                        "task_type",
                        "priority",
                        "classification_reason",
                        "classifier_version",
                    )
                }
            else:
                record = None
                classification = {
                    "intent": clean(action.get("message_intent")) or "outbound",
                    "sentiment": "neutral",
                    "suggested_next_action": "等待候选人回复。",
                    "task_type": "none",
                    "priority": 3,
                    "classification_reason": "猎聘明确出站消息节点已核验。",
                    "classifier_version": "direction-evidence-v1",
                }
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "decision": "deny", "reason": str(exc)}

    token = secrets.token_urlsafe(24)
    issued_at = time.time()
    payload_hash = candidate_message_payload_hash(data)
    with CANDIDATE_MESSAGE_CONFIRMATION_LOCK:
        expired = [
            key
            for key, value in CANDIDATE_MESSAGE_CONFIRMATIONS.items()
            if issued_at - float(value.get("issued_at") or 0) > CANDIDATE_MESSAGE_CONFIRMATION_TTL_SECONDS
        ]
        for key in expired:
            CANDIDATE_MESSAGE_CONFIRMATIONS.pop(key, None)
        CANDIDATE_MESSAGE_CONFIRMATIONS[token] = {
            "issued_at": issued_at,
            "payload_hash": payload_hash,
            "job_candidate_id": int(row["job_candidate_id"]),
        }
    return {
        "ok": True,
        "decision": "allow",
        "confirmation_token": token,
        "expires_in": CANDIDATE_MESSAGE_CONFIRMATION_TTL_SECONDS,
        "job_candidate_id": int(row["job_candidate_id"]),
        "candidate": clean(row["display_name"]),
        "client": clean(row["client"]),
        "job": clean(row["job"]),
        "classification": classification,
        "message_preview": clean(action["raw"].get("message_preview")),
        "message_evidence": clean(action["raw"].get("message_evidence")),
        "conversation_id": clean(action["raw"].get("conversation_id")),
        "message_id": clean(action["raw"].get("message_id")),
    }


def consume_candidate_message_confirmation(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    token = clean(data.get("confirmation_token"))
    if not token:
        return None, "missing_confirmation_token"
    with CANDIDATE_MESSAGE_CONFIRMATION_LOCK:
        confirmation = CANDIDATE_MESSAGE_CONFIRMATIONS.pop(token, None)
    if confirmation is None:
        return None, "confirmation_token_invalid_or_used"
    if time.time() - float(confirmation.get("issued_at") or 0) > CANDIDATE_MESSAGE_CONFIRMATION_TTL_SECONDS:
        return None, "confirmation_token_expired"
    if confirmation.get("payload_hash") != candidate_message_payload_hash(data):
        return None, "confirmation_payload_changed"
    return confirmation, ""

def _close_orphan_followup_tasks(conn: sqlite3.Connection, action: dict[str, Any]) -> int:
    """关闭与当前 action 同姓名+客户+岗位、但缺少 job_candidate_id 的遗留跟进任务。"""
    candidate = clean(action.get("candidate") or action.get("candidate_name") or "")
    client = clean(action.get("client"))
    job = clean(action.get("job"))
    jc_id = clean(action.get("job_candidate_id"))
    if not (candidate and client and job and jc_id):
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        """
        UPDATE followup_tasks
        SET status = 'closed',
            closed_at = ?,
            completed_at = ?,
            job_candidate_id = ?,
            candidate_id = COALESCE(candidate_id, (SELECT source_candidate_id FROM job_candidates WHERE id = ? LIMIT 1)),
            resolution_note = '候选人消息已通过原子写入确认，旧流程任务自动关闭。关联 jc_id=' || ? || '。'
        WHERE candidate_name = ?
          AND client = ?
          AND position = ?
          AND (status IS NULL OR status = 'open')
          AND (job_candidate_id IS NULL OR job_candidate_id = 0)
        """,
        (now, now, int(jc_id), int(jc_id), jc_id, candidate, client, job),
    )
    return cursor.rowcount




def candidate_message_commit(
    state: "WorkbenchState",
    data: dict[str, Any],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    confirmation, denial_reason = consume_candidate_message_confirmation(data)
    if confirmation is None:
        return {"ok": False, "decision": "deny", "reason": denial_reason}
    conn = connect_reply_db(state.db_path)
    try:
        ensure_reply_schema(conn)
        action = build_talent_action(data)
        action["job_candidate_id"] = int(confirmation["job_candidate_id"])
        action["source_id"] = "candidate_message_atomic:{conversation}:{message}".format(
            conversation=clean(data.get("conversation_id")),
            message=clean(data.get("message_id")),
        )
        sync = talent_sync_module()
        conn.execute("BEGIN IMMEDIATE")
        reply_stats = None
        classification = None
        if action["direction"] == "received":
            record = candidate_message_record(conn, data)
            classification = {
                key: record[key]
                for key in (
                    "intent",
                    "sentiment",
                    "suggested_next_action",
                    "task_type",
                    "priority",
                    "classification_reason",
                    "classifier_version",
                )
            }
            action["message_intent"] = record["intent"]
            action["raw"]["message_intent"] = record["intent"]
            reply_stats = insert_reply_and_task(conn, record, create_task=True, commit=False)
        result = sync.process_action(
            conn,
            action,
            batch_source="candidate_message_atomic",
            default_event_time=clean(data.get("message_time")) or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            default_source_thread_id="liepin_reply_assistant",
            index=0,
            dry_run=False,
        )
        if result.get("status") not in {"written", "already_exists"}:
            raise ValueError(clean(result.get("reason")) or "candidate_message_write_rejected")
        conn.commit()
        # 自动关闭同名孤儿跟进任务：旧管线创建的 task 没有 job_candidate_id，
        # 无法感知候选人已被推动；这里在消息原子写入成功后回关。
        _close_orphan_followup_tasks(conn, action)
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "decision": "deny", "reason": str(exc)}
    finally:
        conn.close()
    refresh_result = state.run_refresh() if refresh else None
    return {
        "ok": True,
        "decision": "allow",
        "message": "候选人消息、跟进任务和 A 系统阶段已原子写入",
        "job_candidate_id": int(confirmation["job_candidate_id"]),
        "reply": reply_stats,
        "classification": classification,
        "sync": result,
        "refresh": refresh_result,
    }


CANDIDATE_STATE_TARGETS = {
    "pending_review": {
        "clean_stage": "S1 新增寻访/待复核",
        "flow_bucket": "待复核",
        "raw_status": "pending_review",
        "candidate_status": "new",
        "label": "待复核",
    },
    "reviewed_waiting_contact": {
        "clean_stage": "S2 已复核/待联系",
        "flow_bucket": "待联系",
        "raw_status": "review_continue",
        "candidate_status": "new",
        "label": "已复核待联系",
    },
    "contacted_waiting_reply": {
        "clean_stage": "已触达",
        "flow_bucket": "猎聘触达",
        "raw_status": "job_chat_verified",
        "candidate_status": "contacted",
        "label": "已触达待回复",
    },
    "stopped": {
        "clean_stage": "H5 最近寻访/初筛不通过",
        "flow_bucket": "最近寻访",
        "raw_status": "screen_rejected",
        "candidate_status": "screen_rejected",
        "label": "停止推进",
    },
}


def candidate_state_evidence(state: "WorkbenchState", job_candidate_id: int) -> dict[str, Any]:
    conn = connect_reply_db(state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_reply_schema(conn)
        ensure_effective_candidate_events_schema(conn)
        conn.commit()
        row = conn.execute(
            """
            SELECT jc.id, jc.source_candidate_id, jc.clean_stage, jc.flow_bucket,
                   jc.raw_status, jc.raw_stage, jc.updated_at,
                   p.display_name AS candidate, p.current_company AS company,
                   p.current_title AS title, c.name AS client, j.title AS job
            FROM job_candidates jc
            JOIN people p ON p.id = jc.person_id
            LEFT JOIN jobs j ON j.id = jc.job_id
            LEFT JOIN clients c ON c.id = j.client_id
            WHERE jc.id = ?
            """,
            (int(job_candidate_id),),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "job_candidate_not_found"}
        events = [
            row_dict(event)
            for event in conn.execute(
                """
                SELECT id, event_type, event_status, event_time, summary, raw_json, source_id
                FROM v_effective_candidate_events
                WHERE job_candidate_id = ?
                ORDER BY id DESC
                LIMIT 12
                """,
                (int(job_candidate_id),),
            ).fetchall()
        ]
        source_candidate_id = clean(row["source_candidate_id"])
        message_basis = None
        if source_candidate_id.isdigit():
            reply_row = conn.execute(
                """
                SELECT id, raw_text, intent, sentiment, suggested_next_action,
                       task_type, priority, conversation_id, message_time, message_id,
                       message_evidence, conversation_identity_confidence,
                       classification_reason, classifier_version
                FROM candidate_replies
                WHERE candidate_id = ?
                  AND COALESCE(correction_status, 'active') != 'undone'
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(source_candidate_id),),
            ).fetchone()
            if reply_row is not None:
                message_basis = row_dict(reply_row)
        return {
            "ok": True,
            "job_candidate_id": int(job_candidate_id),
            "candidate": clean(row["candidate"]),
            "client": clean(row["client"]),
            "job": clean(row["job"]),
            "current_state": {
                "clean_stage": clean(row["clean_stage"]),
                "flow_bucket": clean(row["flow_bucket"]),
                "raw_status": clean(row["raw_status"]),
                "raw_stage": clean(row["raw_stage"]),
                "updated_at": clean(row["updated_at"]),
            },
            "message_basis": message_basis,
            "effective_events": events,
            "available_targets": [
                {"key": key, **value} for key, value in CANDIDATE_STATE_TARGETS.items()
            ],
        }
    finally:
        conn.close()


def candidate_state_correction_payload_hash(data: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in data.items()
        if key not in {"confirmation_token", "refresh"} and not key.startswith("_")
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_state_correction_preflight(state: "WorkbenchState", data: dict[str, Any]) -> dict[str, Any]:
    job_candidate_id = parse_optional_int(data.get("job_candidate_id"))
    target_state = clean(data.get("target_state"))
    reason = clean(data.get("reason"))
    if job_candidate_id is None:
        return {"ok": False, "decision": "deny", "reason": "missing_job_candidate_id"}
    if target_state not in CANDIDATE_STATE_TARGETS:
        return {"ok": False, "decision": "deny", "reason": "invalid_target_state"}
    if not reason:
        return {"ok": False, "decision": "deny", "reason": "correction_reason_required"}
    evidence = candidate_state_evidence(state, job_candidate_id)
    if not evidence.get("ok"):
        return {"ok": False, "decision": "deny", "reason": evidence.get("reason")}
    effective_events = evidence.get("effective_events") or []
    latest_received = next(
        (event for event in effective_events if event.get("event_type") == "candidate_message_received"),
        None,
    )
    invalidate_ids: list[int] = []
    if latest_received:
        invalidate_ids.append(int(latest_received["id"]))
        received_id = int(latest_received["id"])
        linked_stage = next(
            (
                event
                for event in effective_events
                if event.get("event_type") == "candidate_stage_update"
                and int(event.get("id") or 0) > received_id
                and clean(json.loads(event.get("raw_json") or "{}").get("trigger")) == "candidate_message"
            ),
            None,
        )
        if linked_stage:
            invalidate_ids.append(int(linked_stage["id"]))
    token = secrets.token_urlsafe(24)
    issued_at = time.time()
    with CANDIDATE_STATE_CONFIRMATION_LOCK:
        CANDIDATE_STATE_CONFIRMATIONS[token] = {
            "issued_at": issued_at,
            "payload_hash": candidate_state_correction_payload_hash(data),
            "job_candidate_id": job_candidate_id,
            "target_state": target_state,
            "invalidate_event_ids": invalidate_ids,
        }
    return {
        "ok": True,
        "decision": "allow",
        "confirmation_token": token,
        "expires_in": CANDIDATE_STATE_CONFIRMATION_TTL_SECONDS,
        "job_candidate_id": job_candidate_id,
        "current_state": evidence["current_state"],
        "target": CANDIDATE_STATE_TARGETS[target_state],
        "invalidate_event_ids": invalidate_ids,
        "message_basis": evidence.get("message_basis"),
    }


def consume_candidate_state_confirmation(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    token = clean(data.get("confirmation_token"))
    if not token:
        return None, "missing_confirmation_token"
    with CANDIDATE_STATE_CONFIRMATION_LOCK:
        confirmation = CANDIDATE_STATE_CONFIRMATIONS.pop(token, None)
    if confirmation is None:
        return None, "confirmation_token_invalid_or_used"
    if time.time() - float(confirmation.get("issued_at") or 0) > CANDIDATE_STATE_CONFIRMATION_TTL_SECONDS:
        return None, "confirmation_token_expired"
    if confirmation.get("payload_hash") != candidate_state_correction_payload_hash(data):
        return None, "confirmation_payload_changed"
    return confirmation, ""


def candidate_state_correction_commit(
    state: "WorkbenchState",
    data: dict[str, Any],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    confirmation, denial_reason = consume_candidate_state_confirmation(data)
    if confirmation is None:
        return {"ok": False, "decision": "deny", "reason": denial_reason}
    target = CANDIDATE_STATE_TARGETS[confirmation["target_state"]]
    job_candidate_id = int(confirmation["job_candidate_id"])
    reason = clean(data.get("reason"))
    conn = connect_reply_db(state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_reply_schema(conn)
        ensure_effective_candidate_events_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT person_id, job_id, source_candidate_id FROM job_candidates WHERE id = ?",
            (job_candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("job_candidate_not_found")
        event_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        correction_cursor = conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id, person_id, job_id, event_type, event_status,
             event_time, summary, raw_json, source_table, source_id)
            VALUES (?, ?, ?, 'candidate_state_correction', 'corrected', ?, ?, ?,
                    'cross_thread_sync', ?)
            """,
            (
                job_candidate_id,
                int(row["person_id"]),
                int(row["job_id"]),
                event_time,
                reason,
                json.dumps(
                    {
                        "target_state": confirmation["target_state"],
                        "invalidate_event_ids": confirmation["invalidate_event_ids"],
                        "stage_after": target["clean_stage"],
                        "flow_bucket": target["flow_bucket"],
                        "raw_status": target["raw_status"],
                    },
                    ensure_ascii=False,
                ),
                f"candidate_state_correction:{job_candidate_id}:{int(time.time())}",
            ),
        )
        correction_event_id = int(correction_cursor.lastrowid)
        for original_event_id in confirmation["invalidate_event_ids"]:
            conn.execute(
                """
                INSERT OR IGNORE INTO candidate_event_corrections
                (original_event_id, correction_event_id, reason)
                VALUES (?, ?, ?)
                """,
                (int(original_event_id), correction_event_id, reason),
            )
        conn.execute(
            """
            UPDATE job_candidates
            SET clean_stage = ?, flow_bucket = ?, clean_reason = ?,
                raw_status = ?, raw_stage = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (
                target["clean_stage"],
                target["flow_bucket"],
                reason,
                target["raw_status"],
                target["clean_stage"],
                job_candidate_id,
            ),
        )
        source_candidate_id = clean(row["source_candidate_id"])
        if source_candidate_id.isdigit():
            candidate_id = int(source_candidate_id)
            conn.execute(
                "UPDATE candidates SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (target["candidate_status"], candidate_id),
            )
            if confirmation["invalidate_event_ids"]:
                reply_row = conn.execute(
                    """
                    SELECT id FROM candidate_replies
                    WHERE candidate_id = ?
                      AND COALESCE(correction_status, 'active') != 'undone'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (candidate_id,),
                ).fetchone()
                if reply_row:
                    reply_id = int(reply_row["id"])
                    conn.execute(
                        """
                        UPDATE candidate_replies
                        SET correction_status='undone', correction_reason=?,
                            corrected_at=datetime('now','localtime')
                        WHERE id=?
                        """,
                        (reason, reply_id),
                    )
                    conn.execute(
                        """
                        UPDATE followup_tasks
                        SET status='closed', resolution_note=?, closed_at=datetime('now','localtime'),
                            updated_at=datetime('now','localtime')
                        WHERE source_table='candidate_replies' AND source_id=?
                          AND COALESCE(status, 'open') NOT IN ('closed','已关闭','关闭')
                        """,
                        (f"状态纠正：{reason}", reply_id),
                    )
        stage_cursor = conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id, person_id, job_id, event_type, event_status,
             event_time, summary, raw_json, source_table, source_id)
            VALUES (?, ?, ?, 'candidate_stage_update', ?, ?, ?, ?,
                    'cross_thread_sync', ?)
            """,
            (
                job_candidate_id,
                int(row["person_id"]),
                int(row["job_id"]),
                target["clean_stage"],
                event_time,
                f"状态纠正：{reason}",
                json.dumps(
                    {
                        "trigger": "candidate_state_correction",
                        "correction_event_id": correction_event_id,
                        "stage_after": target["clean_stage"],
                        "flow_bucket": target["flow_bucket"],
                        "raw_status": target["raw_status"],
                    },
                    ensure_ascii=False,
                ),
                f"candidate_state_correction:{job_candidate_id}:{correction_event_id}:stage",
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "decision": "deny", "reason": str(exc)}
    finally:
        conn.close()
    refresh_result = state.run_refresh() if refresh else None
    return {
        "ok": True,
        "decision": "allow",
        "job_candidate_id": job_candidate_id,
        "target_state": confirmation["target_state"],
        "target": target,
        "correction_event_id": correction_event_id,
        "stage_event_id": int(stage_cursor.lastrowid),
        "invalidated_event_ids": confirmation["invalidate_event_ids"],
        "refresh": refresh_result,
    }


def write_talent_action_batch(data: dict[str, Any]) -> Path:
    TALENT_ACTION_DIR.mkdir(parents=True, exist_ok=True)
    action = build_talent_action(data)
    default_source_prefix = "xsaas_plugin" if str(action.get("kind", "")).startswith("xsaas_") else "liepin_plugin"
    source_id = first_present(data, "source_id", "") or f"{default_source_prefix}:{action['action_id']}"
    batch = {
        "source_thread_id": first_present(data, "source_thread_id", "") or default_source_prefix,
        "source_id": source_id,
        "event_time": first_present(data, "event_time", "") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "actions": [action],
    }
    path = TALENT_ACTION_DIR / f"{action['action_id']}.json"
    path.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply_talent_action_batch(data: dict[str, Any]) -> dict[str, Any]:
    if not TALENT_SYNC_SCRIPT.exists():
        raise FileNotFoundError(f"找不到统一同步脚本：{TALENT_SYNC_SCRIPT}")
    kind = first_present(data, "kind") or first_present(data, "action_type", "")
    write = truthy(data.get("write"))
    if kind not in {"candidate_intake", "intake", "xsaas_intake", "xsaas_candidate_intake"}:
        data = enrich_talent_action_locator(data)
        lookup = data.get("_talent_lookup") if isinstance(data.get("_talent_lookup"), dict) else {}
        if not lookup.get("matched") or not lookup.get("job_candidate_id"):
            summary = {
                "total": 1,
                "would_write": 0,
                "written": 0,
                "already_exists": 0,
                "pending_review": 1,
            }
            return {
                "ok": not write,
                "dry_run": not write,
                "batch_path": "",
                "lookup": lookup,
                "returncode": 0 if not write else 2,
                "stdout": "",
                "stderr": "talent action requires a unique A 系统 job_candidate_id before write",
                "sync": {
                    "ok": not write,
                    "result": {
                        "dry_run": not write,
                        "summary": summary,
                        "items": [
                            {
                                "index": 0,
                                "kind": kind,
                                "status": "pending_review",
                                "reason": lookup.get("reason") or "no_unique_match",
                                "matches": lookup.get("matches") or [],
                            }
                        ],
                        "report_paths": {},
                        "refresh_workbench": None,
                    },
                },
            }
    batch_path = write_talent_action_batch(data)
    refresh_workbench = truthy(data.get("refresh_workbench"))
    cmd = [
        sys.executable,
        str(TALENT_SYNC_SCRIPT),
        "apply-action-batch",
        "--input",
        str(batch_path),
        "--output-prefix",
        f"{TALENT_REPORT_PREFIX}_{batch_path.stem}_{'write' if write else 'dryrun'}",
    ]
    if write:
        cmd.append("--write")
    if refresh_workbench:
        cmd.append("--refresh-workbench")
    with TALENT_SYNC_LOCK:
        proc = subprocess.run(cmd, cwd=str(TALENT_SYSTEM_ROOT), capture_output=True, text=True)
        if proc.returncode != 0 and "database is locked" in (proc.stderr or ""):
            proc = subprocess.run(cmd, cwd=str(TALENT_SYSTEM_ROOT), capture_output=True, text=True)
    parsed: dict[str, Any] | None = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    sync_summary = ((parsed or {}).get("result") or {}).get("summary") or {}
    blocked_write = (
        write
        and int(sync_summary.get("pending_review") or 0) > 0
        and int(sync_summary.get("written") or 0) == 0
        and int(sync_summary.get("would_write") or 0) == 0
    )
    ok = proc.returncode == 0 and not blocked_write
    stop_reason_error = ""
    if write and ok:
        try:
            persist_talent_action_stop_reason(data)
        except Exception as exc:
            # 停止原因列回填失败不阻断已成功的同步写入，仅在响应里留痕。
            stop_reason_error = str(exc)[:300]
    result = {
        "ok": ok,
        "dry_run": not write,
        "batch_path": str(batch_path),
        "lookup": data.get("_talent_lookup"),
        "returncode": 2 if blocked_write and proc.returncode == 0 else proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip() or ("talent action write blocked by pending_review" if blocked_write else ""),
        "sync": parsed,
    }
    if stop_reason_error:
        result["stop_reason_error"] = stop_reason_error
    return result


def ensure_stop_reason_schema(conn: sqlite3.Connection) -> None:
    """job_candidates.stop_reason（PRD 阶段 4 R10），幂等加列；历史数据不迁移。"""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(job_candidates)")}
    if "stop_reason" not in columns:
        conn.execute("ALTER TABLE job_candidates ADD COLUMN stop_reason TEXT")


def persist_talent_action_stop_reason(data: dict[str, Any]) -> None:
    """talent-action 停止写入成功后，把标准化停止原因回填到 job_candidates.stop_reason。

    同步引擎（talent_system_sync.py）只把 stop_reason_code 写进 candidate_events.raw_json，
    本函数在 legacy 侧补落关系级列，供 Core 统计端点按列聚合；可重复调用（幂等）。
    """
    kind = first_present(data, "kind") or first_present(data, "action_type", "")
    if kind not in {"xsaas_review", "xsaas_resume_review", "resume_review", "review"}:
        return
    review_result = first_present(data, "review_result", "") or first_present(data, "result", "")
    if review_result != "stop":
        return
    lookup = data.get("_talent_lookup") if isinstance(data.get("_talent_lookup"), dict) else {}
    job_candidate_id = data.get("job_candidate_id") or lookup.get("job_candidate_id")
    if not str(job_candidate_id or "").isdigit():
        return
    stop_reason, _note = normalize_stop_reason(
        first_present(data, "stop_reason_code", ""), first_present(data, "stop_reason_note", "")
    )
    conn = sqlite3.connect(str(TALENT_DB))
    try:
        ensure_stop_reason_schema(conn)
        conn.execute("UPDATE job_candidates SET stop_reason=? WHERE id=?", (stop_reason, int(job_candidate_id)))
        conn.commit()
    finally:
        conn.close()


def talent_flow_state(data: dict[str, Any]) -> dict[str, Any]:
    raw_ids = data.get("job_candidate_ids") or []
    if not isinstance(raw_ids, list):
        raise ValueError("job_candidate_ids 必须是数组")
    ids = sorted({int(value) for value in raw_ids if str(value).isdigit()})[:200]
    if not ids:
        return {"ok": True, "states": []}
    if not TALENT_DB.exists():
        raise FileNotFoundError(f"找不到 A 系统 v3 数据库：{TALENT_DB}")
    placeholders = ",".join("?" for _ in ids)
    conn = sqlite3.connect(str(TALENT_DB))
    conn.row_factory = sqlite3.Row
    try:
        ensure_effective_candidate_events_schema(conn)
        rows = conn.execute(
            f"""
            SELECT id AS job_candidate_id, clean_stage, flow_bucket,
                   raw_status, raw_stage, updated_at
            FROM job_candidates
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            ids,
        ).fetchall()
        states = []
        for row in rows:
            events = conn.execute(
                """
                SELECT event_type, event_status, event_time, summary
                FROM v_effective_candidate_events
                WHERE job_candidate_id = ?
                  AND event_type IN ('resume_review_completed', 'candidate_contact_update',
                                     'candidate_message_sent', 'candidate_message_received', 'liepin_outreach')
                ORDER BY COALESCE(event_time, '') DESC, id DESC
                """,
                (int(row["job_candidate_id"]),),
            ).fetchall()
            latest_review = next((event for event in events if event["event_type"] == "resume_review_completed"), None)
            latest_outreach = next((event for event in events if event["event_type"] != "resume_review_completed"), None)
            states.append(
                {
                    "jobCandidateId": int(row["job_candidate_id"]),
                    "cleanStage": clean(row["clean_stage"]),
                    "flowBucket": clean(row["flow_bucket"]),
                    "rawStatus": clean(row["raw_status"]),
                    "rawStage": clean(row["raw_stage"]),
                    "updatedAt": clean(row["updated_at"]),
                    "latestReviewStatus": clean(latest_review["event_status"]) if latest_review else "",
                    "latestReviewTime": clean(latest_review["event_time"]) if latest_review else "",
                    "latestReviewSummary": clean(latest_review["summary"]) if latest_review else "",
                    "latestOutreachType": clean(latest_outreach["event_type"]) if latest_outreach else "",
                    "latestOutreachStatus": clean(latest_outreach["event_status"]) if latest_outreach else "",
                    "latestOutreachSummary": clean(latest_outreach["summary"]) if latest_outreach else "",
                }
            )
    finally:
        conn.close()
    return {"ok": True, "states": states}


class WorkbenchState:
    def __init__(self, db_path: Path, output_dir: Path, host: str, port: int) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self.agent_lock = threading.Lock()
        self.last_refresh: dict[str, Any] | None = None
        self._agent_service: AgentService | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def agent_service(self) -> AgentService:
        if self._agent_service is None:
            with self.agent_lock:
                if self._agent_service is None:
                    self._agent_service = AgentService(self.db_path)
        return self._agent_service

    def close(self) -> None:
        if self._agent_service is not None:
            self._agent_service.close()

    def run_refresh(self) -> dict[str, Any]:
        with self.lock:
            cmd = [
                sys.executable,
                str(SCRIPTS_DIR / "refresh_liepin_intelligence.py"),
                "--db",
                str(self.db_path),
                "--output-dir",
                str(self.output_dir),
            ]
            proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)
            parsed: dict[str, Any] = {}
            if proc.stdout.strip():
                try:
                    parsed = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    parsed = {"stdout": proc.stdout.strip()}
            self.last_refresh = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "parsed": parsed,
                "time": datetime.now().isoformat(timespec="seconds"),
            }
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "刷新失败")
            return self.last_refresh

    def build_server_workbench(self) -> Path:
        conn = connect(self.db_path)
        try:
            metrics = collect_metrics(conn)
            projects, _recent = collect_projects(conn)
            feedback_examples = load_feedback_examples(conn)
            experiment_notes = load_recent_experiment_notes(conn)
            strategy_corrections = load_strategy_corrections(conn)
            position_profiles = load_position_profiles(conn)
            search_items = [
                build_strategy_item(project, feedback_examples, experiment_notes, strategy_corrections, position_profiles)
                for project in projects
                if clean(project.get("client"))
                and clean(project.get("position"))
                and int(project.get("open_position_rows") or 0)
            ]
            search_items = sorted(search_items, key=lambda item: item["score"], reverse=True)[:6]
            reports = self.latest_reports()
        finally:
            conn.close()
        static_path = write_html(self.output_dir, metrics, search_items, reports)
        server_path = self.output_dir / SERVER_WORKBENCH
        html = static_path.read_text(encoding="utf-8")
        html = inject_workbench_forms(html, self.base_url)
        server_path.write_text(html, encoding="utf-8")
        return server_path

    def asa_workbench(self) -> Path:
        path = TALENT_SYSTEM_ROOT / "outputs" / "A系统.html"
        if not path.exists():
            raise FileNotFoundError("ASA 页面尚未生成")
        return path

    def live_refresh_status_script(self) -> Path:
        candidates = [
            self.output_dir / "health" / "a_system_live_refresh_status.js",
            TALENT_SYSTEM_ROOT / "outputs" / "health" / "a_system_live_refresh_status.js",
        ]
        for path in candidates:
            if path.exists():
                return path
        raise FileNotFoundError("ASA 实时同步状态文件不存在")

    def latest_reports(self) -> list[dict[str, str]]:
        from generate_liepin_workbench import load_latest_reports

        return load_latest_reports(self.output_dir)


def sourcing_run_state_path(run_id: str) -> Path:
    return SOURCING_RUN_DIR / f"{run_id}.json"


def write_sourcing_run_state(payload: dict[str, Any]) -> None:
    SOURCING_RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = sourcing_run_state_path(str(payload["run_id"]))
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def load_sourcing_runs() -> list[dict[str, Any]]:
    if not SOURCING_RUN_DIR.exists():
        return []
    runs: list[dict[str, Any]] = []
    for path in SOURCING_RUN_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("run_id"):
            runs.append(payload)
    return sorted(runs, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def refresh_sourcing_run_liveness(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("status") not in {"queued", "running"}:
        return run
    pid = int(run.get("pid") or 0)
    if pid and process_is_alive(pid):
        if not clean(run.get("thread_id")) and clean(run.get("log_path")):
            thread_id = extract_codex_thread_id(Path(str(run["log_path"])))
            if thread_id:
                updated = {**run, "thread_id": thread_id}
                write_sourcing_run_state(updated)
                return updated
        return run
    updated = {
        **run,
        "status": "interrupted",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "message": "Codex 进程已退出但未写入完成状态，请查看日志后重新启动。",
    }
    write_sourcing_run_state(updated)
    return updated


def latest_sourcing_run(client: str = "", job: str = "") -> dict[str, Any] | None:
    for raw in load_sourcing_runs():
        run = refresh_sourcing_run_liveness(raw)
        if client and clean(run.get("client")) != clean(client):
            continue
        if job and clean(run.get("job")) != clean(job):
            continue
        return run
    return None


def validate_sourcing_position(state: WorkbenchState, client: str, job: str) -> dict[str, Any]:
    if not MULTICHANNEL_SCRIPT.exists():
        raise FileNotFoundError(f"找不到多渠道寻访脚本：{MULTICHANNEL_SCRIPT}")
    cmd = [
        sys.executable,
        str(MULTICHANNEL_SCRIPT),
        "context",
        "--db",
        str(TALENT_DB),
        "--client",
        client,
        "--job",
        job,
    ]
    proc = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.strip() or proc.stdout.strip() or f"无法解析在招岗位：{client}/{job}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("多渠道寻访岗位校验返回格式异常") from exc
    if payload.get("ok") is False:
        raise ValueError(clean(payload.get("error")) or f"无法解析在招岗位：{client}/{job}")
    return payload


def sourcing_prompt(client: str, job: str, run_id: str) -> str:
    return f"""你由 A 系统今日工作台的“启动 Codex 寻访”按钮派发。

目标：为 A 系统标准岗位 `{client} / {job}` 立即执行一次真实的多渠道候选人补池。
任务 ID：`{run_id}`。

必须遵守：
1. 先运行 A 系统 Cognee startup，并完整使用 `a-system-workbench` 与 `multi-channel-search` skill 的 A 系统岗位模式。
2. 从 v3 positions / position_profiles 解析岗位，不得从旧 candidates.position 猜测。
3. 执行 context、plan、CDP 9223 渠道预检，然后在猎聘和 X-SaaS 进行真实搜索、打开详情复核、历史排重和 A/B/C 分层。
4. 本次范围是补充候选池：可将通过详情复核的新候选人按 dry-run -> apply 写入待复核阶段，并同步审计；不得自动发送消息、开聊、推荐岗位或触达候选人。
5. 登录阻断、缓存结果、泛化推荐流必须如实记录，不能当成 0 结果。最新 stop/H5/screen_rejected 永远排除。
6. 完成后运行该客户/岗位的 A 系统同步、严格审计和回归守卫，并按渠道汇报搜索轮次、查看数、新增数、重复数、停止数与阻断原因。
7. 不要向用户提澄清问题；在上述边界内按岗位画像作合理判断并持续执行到完成。
"""


def extract_codex_thread_id(log_path: Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = event.get("thread_id") or (event.get("thread") or {}).get("id")
        if thread_id:
            return str(thread_id)
    return ""


def watch_sourcing_process(proc: subprocess.Popen[str], run: dict[str, Any], log_handle: Any) -> None:
    try:
        try:
            returncode = proc.wait(timeout=SOURCING_RUN_TIMEOUT_SECONDS)
            status = "completed" if returncode == 0 else "failed"
            message = "Codex 寻访已完成。" if returncode == 0 else f"Codex 寻访失败，退出码 {returncode}。"
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            returncode = -1
            status = "timed_out"
            message = f"Codex 寻访超过 {SOURCING_RUN_TIMEOUT_SECONDS // 60} 分钟，已停止。"
    finally:
        log_handle.close()
    updated = {
        **run,
        "status": status,
        "returncode": returncode,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "message": message,
        "thread_id": extract_codex_thread_id(Path(str(run["log_path"]))),
    }
    write_sourcing_run_state(updated)


def start_sourcing_run(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    client = clean(data.get("client"))
    job = clean(data.get("job") or data.get("position"))
    if not client or not job:
        raise ValueError("启动寻访需要 client 和 job")
    context = validate_sourcing_position(state, client, job)
    write = str(data.get("write", "")).lower() in {"1", "true", "yes", "on"} or data.get("write") is True
    if not CODEX_BIN.exists():
        raise FileNotFoundError(f"找不到 Codex CLI：{CODEX_BIN}")
    active = next((refresh_sourcing_run_liveness(run) for run in load_sourcing_runs() if run.get("status") in {"queued", "running"}), None)
    if active and active.get("status") in {"queued", "running"}:
        same_job = clean(active.get("client")) == client and clean(active.get("job")) == job
        return {
            "ok": same_job,
            "started": False,
            "already_running": same_job,
            "error": "" if same_job else f"已有寻访任务运行中：{active.get('client')} / {active.get('job')}",
            "run": active,
        }
    if not write:
        return {"ok": True, "dry_run": True, "started": False, "context": context}

    with SOURCING_RUN_LOCK:
        active = next((refresh_sourcing_run_liveness(run) for run in load_sourcing_runs() if run.get("status") in {"queued", "running"}), None)
        if active and active.get("status") in {"queued", "running"}:
            raise RuntimeError(f"已有寻访任务运行中：{active.get('client')} / {active.get('job')}")
        created_at = datetime.now().isoformat(timespec="seconds")
        digest = hashlib.sha1(f"{client}|{job}|{created_at}".encode("utf-8")).hexdigest()[:10]
        run_id = f"sourcing_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{digest}"
        SOURCING_RUN_DIR.mkdir(parents=True, exist_ok=True)
        log_path = SOURCING_RUN_DIR / f"{run_id}.jsonl"
        final_path = SOURCING_RUN_DIR / f"{run_id}_final.md"
        prompt_path = SOURCING_RUN_DIR / f"{run_id}_prompt.md"
        prompt = sourcing_prompt(client, job, run_id)
        prompt_path.write_text(prompt, encoding="utf-8")
        child_env = os.environ.copy()
        codex_node_dir = str(CODEX_BIN.parent)
        child_env["PATH"] = codex_node_dir + os.pathsep + child_env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        cmd = [
            str(CODEX_BIN),
            "-a",
            "never",
            "-s",
            "danger-full-access",
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(BASE_DIR),
            "--add-dir",
            str(TALENT_SYSTEM_ROOT),
            "-o",
            str(final_path),
            prompt,
        ]
        log_handle = log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env=child_env,
        )
        run = {
            "run_id": run_id,
            "client": client,
            "job": job,
            "job_id": context.get("job_id"),
            "position_id": context.get("position_id"),
            "status": "running",
            "pid": proc.pid,
            "created_at": created_at,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": "",
            "returncode": None,
            "thread_id": "",
            "log_path": str(log_path),
            "final_path": str(final_path),
            "prompt_path": str(prompt_path),
            "message": "Codex 已启动，正在执行多渠道候选人寻访。",
        }
        write_sourcing_run_state(run)
        threading.Thread(target=watch_sourcing_process, args=(proc, run, log_handle), daemon=True).start()
        return {"ok": True, "dry_run": False, "started": True, "run": run}


def sourcing_run_status(data: dict[str, Any]) -> dict[str, Any]:
    run_id = clean(data.get("run_id"))
    if run_id:
        path = sourcing_run_state_path(run_id)
        if not path.exists():
            return {"ok": False, "error": f"找不到寻访任务：{run_id}"}
        run = refresh_sourcing_run_liveness(json.loads(path.read_text(encoding="utf-8")))
    else:
        run = latest_sourcing_run(clean(data.get("client")), clean(data.get("job") or data.get("position")))
    return {"ok": True, "run": run}


def load_recent_candidates(db_path: Path, limit: int = 24) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, name, company, title, client, position, status, updated_at
            FROM candidates
            ORDER BY
                CASE WHEN status IN ('recommended','contacted','replied','client_approved','interviewing','offered') THEN 0 ELSE 1 END,
                datetime(updated_at) DESC,
                id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_dict(row) for row in rows]
    finally:
        conn.close()


def load_positions_for_form(db_path: Path, limit: int = 300) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_position(row: sqlite3.Row | dict[str, Any], source: str) -> None:
        client = clean(row["client"])
        position = clean(row["position"])
        if not client or not position:
            return
        key = (client, position)
        if key in seen:
            return
        seen.add(key)
        item = dict(row)
        item["client"] = client
        item["position"] = position
        item["source"] = source
        items.append(item)

    if TALENT_DB.exists():
        conn = sqlite3.connect(str(TALENT_DB))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT j.id, c.name AS client, j.title AS position, j.status, NULL AS gap,
                       j.location, j.updated_at, COALESCE(m.priority, '') AS priority,
                       COALESCE(m.metric_date, '') AS priority_date
                FROM jobs j
                JOIN clients c ON c.id = j.client_id
                LEFT JOIN job_pipeline_metrics m
                  ON m.id = (
                    SELECT id
                    FROM job_pipeline_metrics
                    WHERE job_id = j.id
                    ORDER BY COALESCE(metric_date, '') DESC, id DESC
                    LIMIT 1
                  )
                WHERE COALESCE(j.status, 'open') NOT IN ('关闭', '暂停', '已关闭', 'closed', 'paused')
                  AND COALESCE(j.status, '') NOT LIKE '%误归属%'
                  AND COALESCE(j.status, '') NOT LIKE '%已迁移%'
                ORDER BY
                  CASE
                    WHEN COALESCE(m.priority, '') LIKE 'P0-最急%' THEN 0
                    WHEN COALESCE(m.priority, '') LIKE 'P0%' THEN 1
                    WHEN COALESCE(j.status, '') LIKE 'P0%' THEN 2
                    WHEN COALESCE(m.priority, '') LIKE 'P1%' THEN 3
                    WHEN j.status IN ('已发布', '已发布/推进中', 'open', '进行中') THEN 4
                    WHEN j.status LIKE '%搜索%' OR j.status LIKE '%反馈%' THEN 5
                    ELSE 9
                  END,
                  datetime(COALESCE(j.updated_at, j.created_at)) DESC,
                  c.name,
                  j.title,
                  j.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                add_position(row, "talent_system_v3")
        finally:
            conn.close()

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, client, title AS position, status, gap, '' AS priority, '' AS priority_date
            FROM positions
            WHERE COALESCE(status, 'open') NOT IN ('关闭', '暂停', '已关闭', 'closed', 'paused')
              AND COALESCE(status, '') NOT LIKE '%误归属%'
              AND COALESCE(status, '') NOT LIKE '%已迁移%'
            ORDER BY
              CASE
                WHEN COALESCE(status, '') LIKE 'P0%' THEN 0
                WHEN COALESCE(status, '') LIKE 'P1%' THEN 1
                ELSE 9
              END,
              datetime(COALESCE(updated_at, created_at)) DESC,
              client,
              title,
              id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for row in rows:
            add_position(row, "liepin_talent_pool")
        return items[:limit]
    finally:
        conn.close()


def load_confirmation_tasks(db_path: Path, limit: int = 30) -> list[dict[str, Any]]:
    conn = connect_db(db_path)
    try:
        rows = load_needs_confirmation(conn, limit)
        items = []
        for row in rows:
            old_client, old_position = row_current_project(row)
            item = row_dict(row)
            item["current_project"] = project_label(old_client, old_position)
            items.append(item)
        return items
    finally:
        conn.close()


def load_open_tasks(db_path: Path, limit: int = 40) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, candidate_id, candidate_name, candidate_company,
                   COALESCE(NULLIF(confirmed_client, ''), NULLIF(inferred_client, ''), NULLIF(client, ''), '') AS client,
                   COALESCE(NULLIF(confirmed_position, ''), NULLIF(inferred_position, ''), NULLIF(position, ''), '') AS position,
                   task_type, priority, reason, status, updated_at
            FROM followup_tasks
            WHERE COALESCE(status, 'open') NOT IN ('关闭', '暂停', '已关闭', 'closed', 'paused')
            ORDER BY priority ASC, datetime(updated_at) DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row_dict(row) for row in rows]
    finally:
        conn.close()


def load_candidate_by_id(conn: sqlite3.Connection, candidate_id: int | None) -> sqlite3.Row | None:
    if not candidate_id:
        return None
    return conn.execute("SELECT * FROM candidates WHERE id = ? LIMIT 1", (candidate_id,)).fetchone()


def write_position_jd(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    client = first_present(data, "client", "")
    position = first_present(data, "position", "") or first_present(data, "job", "") or first_present(data, "title", "")
    responsibilities = first_present(data, "responsibilities", "")
    requirements = first_present(data, "requirements", "")
    if not client:
        raise ValueError("需要客户名。")
    if not position:
        raise ValueError("需要岗位名。")
    if not TALENT_DB.exists():
        raise ValueError(f"找不到 A 系统 v3 数据库：{TALENT_DB}")

    updated_at = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(str(TALENT_DB))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, client, title
            FROM positions
            WHERE client = ? AND title = ?
            ORDER BY id
            """,
            (client, position),
        ).fetchall()
        if not rows:
            raise ValueError(f"找不到岗位：{client} / {position}")
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        before = conn.total_changes
        conn.execute(
            f"""
            UPDATE positions
            SET responsibilities = ?,
                requirements = ?,
                updated_at = datetime('now','localtime')
            WHERE id IN ({placeholders})
            """,
            (responsibilities, requirements, *ids),
        )
        db_updated_at = conn.execute(
            f"SELECT MAX(updated_at) AS updated_at FROM positions WHERE id IN ({placeholders})",
            ids,
        ).fetchone()["updated_at"]
        updates = conn.total_changes - before
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "message": f"已同步岗位 JD：{client} / {position}",
        "client": client,
        "position": position,
        "position_ids": ids,
        "updated_rows": updates,
        "updated_at": db_updated_at or updated_at,
        "responsibilities_chars": len(responsibilities),
        "requirements_chars": len(requirements),
    }


def write_client_feedback(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    write = truthy(data.get("write", True))
    args = make_namespace(
        candidate_id=int(data["candidate_id"]) if data.get("candidate_id") else None,
        candidate_name=first_present(data, "candidate_name", ""),
        candidate_company=first_present(data, "candidate_company", ""),
        client=first_present(data, "client", ""),
        position=first_present(data, "position", ""),
        feedback_type=first_present(data, "feedback_type", ""),
        status_after=first_present(data, "status_after", ""),
        reason_tags=first_present(data, "reason_tags", ""),
        feedback_detail=first_present(data, "feedback_detail", ""),
        next_action=first_present(data, "next_action", ""),
        source="workbench",
        feedback_time=datetime.now().isoformat(timespec="seconds"),
        dry_run=not write,
        no_status_update=False,
    )
    conn = connect_feedback_db(state.db_path)
    try:
        ensure_feedback_schema(conn)
        candidate = None
        if args.candidate_id:
            candidate = conn.execute("SELECT * FROM candidates WHERE id = ? LIMIT 1", (args.candidate_id,)).fetchone()
        record = build_feedback_record(args, candidate)
        relation = find_job_candidate(
            conn,
            record["candidate_id"],
            record["client"],
            record["position"],
            parse_optional_int(data.get("job_candidate_id")),
        )
        if relation is None:
            raise ValueError("未唯一定位 A 系统推进关系，客户反馈未写入。")
        stats = {"event_id": None, "event_updates": 0, "candidate_updates": 0}
        timeline_event_id = None
        if write:
            stats = insert_feedback_record(conn, record, True)
            event_status = record["feedback_type"]
            summary = record["feedback_detail"] or LABEL_BY_FEEDBACK.get(event_status, event_status)
            cursor = conn.execute(
                """
                INSERT INTO candidate_events (
                    job_candidate_id, person_id, job_id, event_type, event_status,
                    event_time, summary, raw_json, source_table, source_id
                ) VALUES (?, ?, ?, 'client_feedback', ?, ?, ?, ?, 'client_feedback_events', ?)
                """,
                (
                    relation["id"], relation["person_id"], relation["job_id"], event_status,
                    record["feedback_time"], summary,
                    json.dumps({"reason_tags": json.loads(record["reason_tags_json"] or "[]"), "next_action": record["next_action"]}, ensure_ascii=False),
                    str(stats["event_id"]),
                ),
            )
            timeline_event_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE client_feedback_events SET job_candidate_id = ?, event_id = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (relation["id"], timeline_event_id, stats["event_id"]),
            )
            stage_by_status = {
                "client_approved": "S8 客户反馈/认可",
                "client_rejected": "S14 淘汰/关闭",
                "interviewing": "S9 面试安排",
                "passed": "S10 面试通过",
                "eliminated": "S14 淘汰/关闭",
                "offered": "S12 Offer",
                "hired": "S13 入职跟进",
                "hold": "S8 客户反馈/暂缓",
            }
            next_stage = stage_by_status.get(record["status_after"])
            if next_stage:
                conn.execute(
                    """
                    UPDATE job_candidates
                    SET raw_status = ?, clean_stage = ?, flow_bucket = ?, clean_reason = ?, updated_at = datetime('now','localtime')
                    WHERE id = ?
                    """,
                    (
                        record["status_after"], next_stage,
                        "淘汰/关闭" if record["status_after"] in {"client_rejected", "eliminated"} else "正式流程",
                        "客户反馈闭环", relation["id"],
                    ),
                )
            conn.commit()
    finally:
        conn.close()
    sourcing_learning = None
    if write and timeline_event_id and getattr(state, "agent_service", None):
        signal_by_feedback = {
            "approved": "client_approved",
            "interviewing": "client_interview",
            "interview_passed": "client_interview",
            "offer": "client_offer",
            "hired": "client_hired",
            "rejected": "client_rejected",
            "interview_failed": "client_rejected",
            "eliminated": "client_rejected",
            "hold": "client_hold",
        }
        signal = signal_by_feedback.get(record["feedback_type"])
        if signal:
            sourcing_learning = state.agent_service.record_sourcing_business_signal(
                int(relation["id"]), signal, actor_type="client",
                note=record["feedback_detail"] or LABEL_BY_FEEDBACK.get(record["feedback_type"], record["feedback_type"]),
                source_type="client_feedback_event", source_id=stats["event_id"],
            )
    refresh = state.run_refresh() if write else None
    a_system_refresh = refresh_a_system_workbench() if write else None
    return {
        "ok": True,
        "dry_run": not write,
        "message": f"已记录客户反馈：{record['candidate_name']}｜{LABEL_BY_FEEDBACK.get(record['feedback_type'], record['feedback_type'])}",
        "record": record,
        "stats": stats,
        "job_candidate_id": relation["id"],
        "timeline_event_id": timeline_event_id,
        "sourcing_learning": sourcing_learning,
        "refresh": refresh,
        "a_system_refresh": a_system_refresh,
    }


def write_followup_task(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    write = truthy(data.get("write", True))
    refresh = truthy(data.get("refresh", True))
    job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
    task_type = first_present(data, "task_type", "manual_followup") or "manual_followup"
    reason = first_present(data, "reason", "")
    due_at = first_present(data, "due_at", "") or (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    priority = parse_optional_int(data.get("priority")) or 2
    if priority not in {0, 1, 2, 3}:
        raise ValueError("任务优先级只能是 0-3。")
    conn = connect_feedback_db(state.db_path)
    try:
        relation = find_job_candidate(conn, None, "", "", job_candidate_id)
        if relation is None:
            raise ValueError(f"没有找到推进关系 #{job_candidate_id}")
        task_id = None
        event_id = None
        if write:
            cursor = conn.execute(
                """
                INSERT INTO followup_tasks (
                    candidate_id, candidate_name, candidate_company, client, position,
                    task_type, priority, due_at, status, reason, source_table, source_id,
                    created_at, updated_at, job_candidate_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 'a_system_workbench', ?,
                          datetime('now','localtime'), datetime('now','localtime'), ?)
                """,
                (
                    int(relation["source_candidate_id"]) if str(relation["source_candidate_id"] or "").isdigit() else None,
                    relation["display_name"], relation["current_company"], relation["client"], relation["job"],
                    task_type, priority, due_at, reason, job_candidate_id, job_candidate_id,
                ),
            )
            task_id = int(cursor.lastrowid)
            conn.execute("UPDATE followup_tasks SET id = ? WHERE rowid = ?", (task_id, task_id))
            event = conn.execute(
                """
                INSERT INTO candidate_events (
                    job_candidate_id, person_id, job_id, event_type, event_status,
                    event_time, summary, raw_json, source_table, source_id
                ) VALUES (?, ?, ?, 'followup_task', 'open', datetime('now','localtime'), ?, ?, 'followup_tasks', ?)
                """,
                (job_candidate_id, relation["person_id"], relation["job_id"], reason or task_type,
                 json.dumps({"task_type": task_type, "priority": priority, "due_at": due_at}, ensure_ascii=False), str(task_id)),
            )
            event_id = int(event.lastrowid)
            conn.commit()
    finally:
        conn.close()
    a_system_refresh = refresh_a_system_workbench() if write and refresh else None
    return {
        "ok": True,
        "dry_run": not write,
        "message": f"已创建跟进任务：{relation['display_name']}｜{task_type}",
        "task_id": task_id,
        "event_id": event_id,
        "job_candidate_id": job_candidate_id,
        "due_at": due_at,
        "a_system_refresh": a_system_refresh,
    }


def execute_agent_task_proposal(
    state: WorkbenchState,
    proposal_id: str,
    result: dict[str, Any],
    *,
    refresh: bool = True,
) -> dict[str, Any]:
    if result["action_type"] != "create_task":
        state.agent_service.finish_proposal(
            proposal_id,
            success=False,
            note=f"不支持自动执行动作：{result['action_type']}",
        )
        raise ValueError(f"不支持自动执行动作：{result['action_type']}")
    task_request = dict(result["request"])
    conn = connect_feedback_db(state.db_path)
    try:
        existing_task = conn.execute(
            """
            SELECT id,due_at FROM followup_tasks
            WHERE job_candidate_id=? AND task_type=? AND COALESCE(reason,'')=?
              AND COALESCE(status,'open')='open'
            ORDER BY id DESC LIMIT 1
            """,
            (
                result["job_candidate_id"],
                task_request.get("task_type") or "agent_verification",
                task_request.get("reason") or "",
            ),
        ).fetchone()
    finally:
        conn.close()
    if existing_task:
        task_result = {
            "task_id": existing_task["id"],
            "job_candidate_id": result["job_candidate_id"],
            "due_at": existing_task["due_at"],
            "message": "已有相同的开放 Agent 任务",
        }
    else:
        write_request = {**task_request, "refresh": refresh}
        task_result = write_followup_task(state, write_request)
    action_result = state.agent_service.record_external_action(
        job_candidate_id=result["job_candidate_id"],
        action_type="create_task",
        request=task_request,
        result=task_result,
        idempotency_key=result["dedupe_key"],
    )
    action = state.agent_service.get_action(result["dedupe_key"])
    state.agent_service.finish_proposal(
        proposal_id,
        success=True,
        action_id=int(action["id"]) if action and action.get("id") else None,
    )
    return {"ok": True, "proposal_id": proposal_id, "status": "executed", **action_result}


def write_outreach_event(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    candidate_id = parse_optional_int(data.get("candidate_id"))
    conn = connect_outreach_db(state.db_path)
    try:
        ensure_outreach_schema(conn)
        candidate = load_candidate_by_id(conn, candidate_id)
        candidate_name = first_present(data, "candidate_name", "")
        if candidate is not None and not candidate_name:
            candidate_name = clean(candidate["name"])
        if not candidate_name:
            raise ValueError("需要选择或填写候选人。")
        args = make_namespace(
            candidate_id=candidate_id,
            candidate_name=candidate_name,
            candidate_company=first_present(data, "candidate_company", ""),
            client=first_present(data, "client", ""),
            position=first_present(data, "position", ""),
            channel=first_present(data, "channel", "liepin") or "liepin",
            event_type=first_present(data, "event_type", "manual_touch") or "manual_touch",
            event_status=first_present(data, "event_status", "done") or "done",
            message_summary=first_present(data, "message_summary", ""),
            message=first_present(data, "message", ""),
            source_url=first_present(data, "source_url", ""),
            event_time=datetime.now().isoformat(timespec="seconds"),
        )
        record = build_outreach_record(args, candidate)
        record_id = insert_outreach_record(conn, record)
    finally:
        conn.close()
    refresh = state.run_refresh()
    return {
        "ok": True,
        "message": f"已记录触达事件 #{record_id}",
        "record_id": record_id,
        "record": record,
        "refresh": refresh,
    }


def write_candidate_reply(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    candidate_id = parse_optional_int(data.get("candidate_id"))
    args = make_namespace(
        candidate_id=candidate_id,
        candidate_name=first_present(data, "candidate_name", ""),
        candidate_company=first_present(data, "candidate_company", ""),
        candidate_title=first_present(data, "candidate_title", ""),
        client=first_present(data, "client", ""),
        position=first_present(data, "position", ""),
        channel=first_present(data, "channel", "liepin") or "liepin",
        conversation_id=first_present(data, "conversation_id", ""),
        message_time=datetime.now().isoformat(timespec="seconds"),
        raw_text=first_present(data, "raw_text", ""),
    )
    conn = connect_reply_db(state.db_path)
    try:
        ensure_reply_schema(conn)
        candidate = load_candidate_by_id(conn, candidate_id)
        record = build_reply_record(args, candidate)
        stats = insert_reply_and_task(conn, record, create_task=True)
    finally:
        conn.close()
    refresh = state.run_refresh()
    if stats.get("duplicate"):
        message = f"这条回复已存在，未重复新增：{record['candidate_name']}"
    else:
        message = f"已记录候选人回复：{record['candidate_name']}｜{record['intent']}｜P{record['priority']}"
    return {
        "ok": True,
        "message": message,
        "record": record,
        "stats": stats,
        "refresh": refresh,
    }


def write_reply_assistant_sample(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    sample = data.get("sample") if isinstance(data.get("sample"), dict) else data
    source = first_present(data, "source", "extension-direct") or "extension-direct"
    row = normalize_reply_sample_row(sample, source)
    conn = connect_reply_db(state.db_path)
    try:
        ensure_reply_sample_schema(conn)
        stats = upsert_reply_samples(conn, [row])
    finally:
        conn.close()
    return {
        "ok": True,
        "message": "已同步采纳样本",
        "sample_id": row["sample_id"],
        "stats": stats,
    }


def write_reply_assistant_outreach(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    event = data.get("event") if isinstance(data.get("event"), dict) else data
    source = first_present(data, "source", "extension-direct") or "extension-direct"
    normalized = normalize_reply_outreach_event(event, source)
    conn = connect_outreach_db(state.db_path)
    try:
        ensure_reply_outreach_schema(conn)
        stats = insert_reply_outreach_events(conn, [normalized])
    finally:
        conn.close()
    return {
        "ok": True,
        "message": "已同步触达事件",
        "event_id": normalized["event_id"],
        "stats": stats,
    }


def write_candidate_status(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    candidate_id = parse_required_int(data.get("candidate_id"), "candidate_id")
    status = first_present(data, "status", "")
    if not status:
        raise ValueError("需要选择候选人状态。")
    note = first_present(data, "note", "")
    allowed = {
        "new",
        "recommended",
        "contacted",
        "replied",
        "screen_rejected",
        "client_approved",
        "client_rejected",
        "interviewing",
        "passed",
        "offered",
        "hired",
        "hold",
        "eliminated",
        "duplicate",
    }
    if status not in allowed:
        raise ValueError(f"未知状态：{status}")
    conn = connect(state.db_path)
    try:
        candidate = load_candidate_by_id(conn, candidate_id)
        if candidate is None:
            raise ValueError(f"没有找到候选人 #{candidate_id}")
        before = conn.total_changes
        conn.execute(
            """
            UPDATE candidates
            SET status = ?,
                notes = CASE
                    WHEN ? = '' THEN notes
                    WHEN notes IS NULL OR notes = '' THEN ?
                    ELSE notes || char(10) || ?
                END,
                elimination_reason = CASE
                    WHEN ? IN ('client_rejected', 'eliminated') AND ? != '' THEN ?
                    ELSE elimination_reason
                END,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (status, note, note, note, status, note, note, candidate_id),
        )
        updates = conn.total_changes - before
        conn.commit()
    finally:
        conn.close()
    refresh = state.run_refresh()
    return {
        "ok": True,
        "message": f"已更新候选人状态：#{candidate_id} -> {status}",
        "candidate_id": candidate_id,
        "status": status,
        "updates": updates,
        "refresh": refresh,
    }


def ensure_task_resolution_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(followup_tasks)")}
    if "resolution_note" not in columns:
        conn.execute("ALTER TABLE followup_tasks ADD COLUMN resolution_note TEXT")
    if "closed_at" not in columns:
        conn.execute("ALTER TABLE followup_tasks ADD COLUMN closed_at TEXT")
    conn.commit()


def close_followup_task(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    task_id = parse_required_int(data.get("task_id"), "task_id")
    status = first_present(data, "status", "closed") or "closed"
    note = first_present(data, "note", "")
    if status not in {"done", "closed", "skipped", "no_action"}:
        raise ValueError("待办状态只能是 done/closed/skipped/no_action。")
    conn = connect(state.db_path)
    try:
        ensure_task_resolution_schema(conn)
        task = conn.execute("SELECT id, job_candidate_id FROM followup_tasks WHERE id = ? LIMIT 1", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"没有找到待办 #{task_id}")
        before = conn.total_changes
        conn.execute(
            """
            UPDATE followup_tasks
            SET status = ?,
                resolution_note = ?,
                closed_at = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (status, note, datetime.now().isoformat(timespec="seconds"), task_id),
        )
        if task["job_candidate_id"]:
            relation = find_job_candidate(conn, None, "", "", int(task["job_candidate_id"]))
            if relation is not None:
                conn.execute(
                    """
                    INSERT INTO candidate_events (
                        job_candidate_id, person_id, job_id, event_type, event_status,
                        event_time, summary, raw_json, source_table, source_id
                    ) VALUES (?, ?, ?, 'followup_task', ?, datetime('now','localtime'), ?, '{}', 'followup_tasks', ?)
                    """,
                    (relation["id"], relation["person_id"], relation["job_id"], status, note or "跟进任务已完成", str(task_id)),
                )
        updates = conn.total_changes - before
        conn.commit()
    finally:
        conn.close()
    refresh = state.run_refresh()
    a_system_refresh = refresh_a_system_workbench()
    return {
        "ok": True,
        "message": f"已关闭待办 #{task_id}",
        "task_id": task_id,
        "status": status,
        "updates": updates,
        "refresh": refresh,
        "a_system_refresh": a_system_refresh,
    }


def complete_agent_verification(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    task_id = parse_required_int(data.get("task_id"), "task_id")
    raw_answers = data.get("answers")
    if not isinstance(raw_answers, list) or not raw_answers:
        raise ValueError("至少需要填写一项核验结果。")
    status_labels = {"met": "满足", "not_met": "不满足", "unknown": "仍未知"}
    answers: list[dict[str, str]] = []
    seen_questions: set[str] = set()
    for raw in raw_answers[:20]:
        if not isinstance(raw, dict):
            raise ValueError("核验结果格式无效。")
        question = clean(raw.get("question"))
        answer_status = clean(raw.get("status")).lower()
        note = clean(raw.get("note"))
        if not question or answer_status not in status_labels:
            raise ValueError("每个核验问题都必须选择满足、不满足或仍未知。")
        if question in seen_questions:
            raise ValueError("核验问题不能重复。")
        seen_questions.add(question)
        answers.append({"question": question, "status": answer_status, "note": note})
    overall_note = clean(data.get("note"))
    candidate_state = state.agent_service.get_candidate_state(
        parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
    )
    expected_questions = list(
        (candidate_state.get("verification_task") or {}).get("questions") or []
    )
    if expected_questions and set(expected_questions) != seen_questions:
        raise ValueError("请完成当前任务中的全部核验问题。")

    conn = connect(state.db_path)
    try:
        ensure_task_resolution_schema(conn)
        task = conn.execute(
            """
            SELECT id,job_candidate_id,task_type,status FROM followup_tasks
            WHERE id=? LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if task is None:
            raise ValueError(f"没有找到核验任务 #{task_id}")
        if int(task["job_candidate_id"] or 0) != int(candidate_state["job_candidate_id"]):
            raise ValueError("核验任务与当前人选不匹配。")
        if task["task_type"] != "agent_verification":
            raise ValueError("该任务不是 ASA 核验任务。")
        if str(task["status"] or "open") != "open":
            return {
                "ok": True,
                "cached": True,
                "task_id": task_id,
                "job_candidate_id": int(task["job_candidate_id"]),
                "status": task["status"],
                "run_id": None,
            }
        relation = find_job_candidate(conn, None, "", "", int(task["job_candidate_id"]))
        if relation is None:
            raise ValueError("核验任务对应的人岗关系不存在。")
        result_payload = {"answers": answers, "note": overall_note, "source": "asa_v1_1"}
        result_lines = [
            f"{item['question']}={status_labels[item['status']]}"
            + (f"（{item['note']}）" if item["note"] else "")
            for item in answers
        ]
        summary = "核验结果：" + "；".join(result_lines)
        if overall_note:
            summary += f"；补充说明：{overall_note}"
        conn.execute(
            """
            UPDATE followup_tasks
            SET status='done',resolution_note=?,closed_at=datetime('now','localtime'),
                updated_at=datetime('now','localtime')
            WHERE id=? AND COALESCE(status,'open')='open'
            """,
            (summary, task_id),
        )
        event = conn.execute(
            """
            INSERT INTO candidate_events(
                job_candidate_id,person_id,job_id,event_type,event_status,event_time,
                summary,raw_json,source_table,source_id
            ) VALUES (?,?,?,'agent_verification_completed','done',datetime('now','localtime'),
                      ?,?,'followup_tasks',?)
            """,
            (
                relation["id"], relation["person_id"], relation["job_id"], summary,
                json.dumps(result_payload, ensure_ascii=False), str(task_id),
            ),
        )
        event_id = int(event.lastrowid)
        conn.commit()
    finally:
        conn.close()

    action_request = {
        "task_id": task_id,
        "job_candidate_id": int(candidate_state["job_candidate_id"]),
        **result_payload,
    }
    idempotency_key = hashlib.sha256(
        f"asa-verification-complete|{task_id}|{json.dumps(action_request, ensure_ascii=False, sort_keys=True)}".encode("utf-8")
    ).hexdigest()
    state.agent_service.record_external_action(
        job_candidate_id=int(candidate_state["job_candidate_id"]),
        action_type="complete_task",
        request=action_request,
        result={"task_id": task_id, "event_id": event_id, "status": "done"},
        idempotency_key=idempotency_key,
    )
    state.agent_service.store_memory(
        scope_type="candidate",
        scope_id=int(candidate_state["job_candidate_id"]),
        memory_type="verification_result",
        content=summary,
        source_type="candidate_event",
        source_id=event_id,
        confidence=1.0,
    )
    assessment = state.agent_service.submit_assessment(
        int(candidate_state["job_candidate_id"]),
        force=True,
        trigger="verification_completed",
    )
    return {
        "ok": True,
        "cached": False,
        "task_id": task_id,
        "event_id": event_id,
        "job_candidate_id": int(candidate_state["job_candidate_id"]),
        "status": "done",
        "run_id": assessment.get("run_id"),
        "assessment_status": assessment.get("status"),
        "message": "核验结果已写入，ASA 正在重新评估当前人选。",
    }


def write_search_experiment(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    args = make_namespace(
        experiment_id=None,
        position_id=None,
        client=first_present(data, "client", ""),
        position=first_present(data, "position", ""),
        channel="liepin",
        round_name=first_present(data, "round_name", ""),
        query=first_present(data, "query", ""),
        filters_json=None,
        city=first_present(data, "city", ""),
        expected_city=first_present(data, "expected_city", ""),
        education=first_present(data, "education", ""),
        experience=first_present(data, "experience", ""),
        company=first_present(data, "company", ""),
        page_scope=first_present(data, "page_scope", ""),
        result_count=parse_optional_int(data.get("result_count")),
        viewed_count=parse_optional_int(data.get("viewed_count")),
        extracted_count=parse_optional_int(data.get("extracted_count")),
        recommended_count=parse_optional_int(data.get("recommended_count")) or 0,
        reply_count=parse_optional_int(data.get("reply_count")) or 0,
        positive_reply_count=parse_optional_int(data.get("positive_reply_count")) or 0,
        status=first_present(data, "status", ""),
        source_url=first_present(data, "source_url", ""),
        noise_notes=first_present(data, "noise_notes", ""),
        run_time=datetime.now().isoformat(timespec="seconds"),
        dry_run=False,
    )
    conn = connect_search_db(state.db_path)
    try:
        ensure_search_schema(conn)
        record = normalize_search_record(args, None)
        record_id = insert_search_record(conn, record)
    finally:
        conn.close()
    refresh = state.run_refresh()
    return {
        "ok": True,
        "message": f"已记录搜索实验 #{record_id}",
        "record_id": record_id,
        "record": record,
        "refresh": refresh,
    }


def write_project_confirmation(state: WorkbenchState, data: dict[str, Any]) -> dict[str, Any]:
    task_id = parse_required_int(data.get("task_id"), "task_id")
    args = make_namespace(
        position_id=parse_optional_int(data.get("position_id")),
        client=first_present(data, "client", ""),
        position=first_present(data, "position", ""),
    )
    conn = connect_db(state.db_path)
    try:
        task = load_task(conn, task_id)
        client, position = resolve_project(args, conn)
        note = first_present(data, "note", "工作台确认项目归属") or "工作台确认项目归属"
        stats = update_confirmation(conn, task, client, position, note, "workbench")
    finally:
        conn.close()
    refresh = state.run_refresh()
    return {
        "ok": True,
        "message": f"已把待办 #{task_id} 归到 {project_label(client, position)}",
        "task_id": task_id,
        "project": {"client": client, "position": position},
        "stats": stats,
        "refresh": refresh,
    }


def parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"数量必须是数字：{value}") from exc
    if parsed < 0:
        raise ValueError("数量不能小于 0")
    return parsed


def parse_required_int(value: Any, label: str) -> int:
    parsed = parse_optional_int(value)
    if parsed is None:
        raise ValueError(f"缺少必填字段：{label}")
    return parsed


def inject_workbench_forms(html_text: str, base_url: str) -> str:
    css = """
  <style>
    .writeback { margin-top: 14px; }
    .writeback-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .write-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); padding: 14px; }
    .write-card h2 { margin: 0 0 12px; font-size: 17px; letter-spacing: 0; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .field { display: grid; gap: 5px; min-width: 0; }
    .field.full { grid-column: 1 / -1; }
    label { color: var(--muted); font-size: 12px; }
    input, select, textarea {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 9px;
      color: var(--text);
      background: #fff;
      font: inherit;
      font-size: 13px;
    }
    textarea { min-height: 72px; resize: vertical; }
    .primary-btn {
      min-height: 40px;
      margin-top: 12px;
      border: 0;
      border-radius: 7px;
      background: var(--blue);
      color: #fff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    .primary-btn:disabled { opacity: .55; cursor: wait; }
    .status-line { margin-top: 10px; min-height: 18px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .status-line.ok { color: var(--green); }
    .status-line.error { color: var(--red); }
    @media (max-width: 920px) { .writeback-grid { grid-template-columns: 1fr; } }
    @media (max-width: 560px) { .form-grid { grid-template-columns: 1fr; } }
  </style>
"""
    html_text = html_text.replace("</style>", css + "\n</style>", 1)
    forms = f"""
    <section class="writeback">
      <div class="writeback-grid">
        <form class="write-card" data-endpoint="/api/client-feedback">
          <h2>记录客户反馈</h2>
          <div class="form-grid">
            <label class="field full">候选人
              <select name="candidate_id" data-source="candidates"><option value="">手填或选择</option></select>
            </label>
            <label class="field">客户<input name="client" autocomplete="off"></label>
            <label class="field">岗位<input name="position" autocomplete="off"></label>
            <label class="field">反馈类型
              <select name="feedback_type">
                <option value="approved">客户认可</option>
                <option value="rejected">客户否决</option>
                <option value="interviewing">进入面试</option>
                <option value="interview_passed">面试通过</option>
                <option value="interview_failed">面试未通过</option>
                <option value="offer">进入 offer</option>
                <option value="hired">确认入职</option>
                <option value="hold">暂缓</option>
              </select>
            </label>
            <label class="field">原因标签<input name="reason_tags" placeholder="技术不符, 城市"></label>
            <label class="field full">反馈摘要<textarea name="feedback_detail"></textarea></label>
            <label class="field full">下一步<textarea name="next_action"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/search-experiment">
          <h2>记录搜索实验</h2>
          <div class="form-grid">
            <label class="field">客户<input name="client" autocomplete="off"></label>
            <label class="field">岗位<input name="position" autocomplete="off"></label>
            <label class="field full">关键词<input name="query" required></label>
            <label class="field">城市<input name="city"></label>
            <label class="field">年限<input name="experience"></label>
            <label class="field">结果数<input name="result_count" inputmode="numeric"></label>
            <label class="field">查看数<input name="viewed_count" inputmode="numeric"></label>
            <label class="field">入库数<input name="extracted_count" inputmode="numeric"></label>
            <label class="field">推荐数<input name="recommended_count" inputmode="numeric"></label>
            <label class="field">回复数<input name="reply_count" inputmode="numeric"></label>
            <label class="field full">备注<textarea name="noise_notes"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/project-confirmation">
          <h2>修正项目归属</h2>
          <div class="form-grid">
            <label class="field full">待办
              <select name="task_id" data-source="tasks" required><option value="">选择待办</option></select>
            </label>
            <label class="field full">归到岗位
              <select name="position_id" data-source="positions"><option value="">手填客户/岗位</option></select>
            </label>
            <label class="field">客户<input name="client" autocomplete="off"></label>
            <label class="field">岗位<input name="position" autocomplete="off"></label>
            <label class="field full">备注<input name="note" value="工作台确认项目归属"></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/candidate-reply">
          <h2>记录候选人回复</h2>
          <div class="form-grid">
            <label class="field full">候选人
              <select name="candidate_id" data-source="candidates"><option value="">手填或选择</option></select>
            </label>
            <label class="field">姓名<input name="candidate_name" autocomplete="off"></label>
            <label class="field">头衔<input name="candidate_title" autocomplete="off"></label>
            <label class="field">客户<input name="client" autocomplete="off"></label>
            <label class="field">岗位<input name="position" autocomplete="off"></label>
            <label class="field full">回复原文<textarea name="raw_text" required placeholder="把候选人在猎聘里的回复粘贴到这里"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">分类并生成待办</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/outreach-event">
          <h2>记录触达事件</h2>
          <div class="form-grid">
            <label class="field full">候选人
              <select name="candidate_id" data-source="candidates"><option value="">手填或选择</option></select>
            </label>
            <label class="field">客户<input name="client" autocomplete="off"></label>
            <label class="field">岗位<input name="position" autocomplete="off"></label>
            <label class="field">动作
              <select name="event_type">
                <option value="recommend_position">推荐岗位</option>
                <option value="reply_assistant_fill">填入回复</option>
                <option value="wechat_added">加微信</option>
                <option value="phone_call">电话沟通</option>
                <option value="resume_requested">要简历</option>
                <option value="followup_message">二次跟进</option>
              </select>
            </label>
            <label class="field">结果
              <select name="event_status">
                <option value="done">已完成</option>
                <option value="pending">待观察</option>
                <option value="failed">未成功</option>
              </select>
            </label>
            <label class="field full">摘要<textarea name="message_summary"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/candidate-status">
          <h2>更新候选人状态</h2>
          <div class="form-grid">
            <label class="field full">候选人
              <select name="candidate_id" data-source="candidates" required><option value="">选择候选人</option></select>
            </label>
            <label class="field full">状态
              <select name="status" required>
                <option value="contacted">已触达</option>
                <option value="recommended">已推荐</option>
                <option value="replied">已回复</option>
                <option value="client_approved">客户认可</option>
                <option value="client_rejected">客户否决</option>
                <option value="interviewing">面试中</option>
                <option value="offered">Offer</option>
                <option value="hired">入职</option>
                <option value="hold">暂缓</option>
                <option value="eliminated">淘汰</option>
              </select>
            </label>
            <label class="field full">备注<textarea name="note"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
        <form class="write-card" data-endpoint="/api/close-task">
          <h2>关闭待办</h2>
          <div class="form-grid">
            <label class="field full">待办
              <select name="task_id" data-source="open-tasks" required><option value="">选择待办</option></select>
            </label>
            <label class="field full">处理结果
              <select name="status">
                <option value="done">已处理</option>
                <option value="closed">关闭</option>
                <option value="skipped">跳过</option>
                <option value="no_action">无需处理</option>
              </select>
            </label>
            <label class="field full">备注<textarea name="note"></textarea></label>
          </div>
          <button class="primary-btn" type="submit">写入并刷新</button>
          <div class="status-line"></div>
        </form>
      </div>
    </section>
"""
    html_text = html_text.replace("</section>\n    <footer>", "</section>\n" + forms + "\n    <footer>", 1)
    script = f"""
  <script>
    window.LIEPIN_WORKBENCH_BASE = {json.dumps(base_url)};
    async function api(path, payload) {{
      const res = await fetch(path, {{
        method: payload ? 'POST' : 'GET',
        headers: payload ? {{'Content-Type': 'application/json'}} : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      }});
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '操作失败');
      return data;
    }}
    function formData(form) {{
      return Object.fromEntries([...new FormData(form).entries()].filter(([, value]) => String(value).trim() !== ''));
    }}
    async function loadOptions() {{
      const data = await api('/api/context');
      for (const select of document.querySelectorAll('select[data-source=\"candidates\"]')) {{
        for (const item of data.candidates) {{
          const opt = document.createElement('option');
          opt.value = item.id;
          opt.textContent = `${{item.name}}｜${{item.company || '未填公司'}}｜${{item.client || '未定客户'}}/${{item.position || '未定岗位'}}`;
          opt.dataset.name = item.name || '';
          opt.dataset.title = item.title || '';
          opt.dataset.client = item.client || '';
          opt.dataset.position = item.position || '';
          select.appendChild(opt);
        }}
        select.addEventListener('change', () => {{
          const opt = select.selectedOptions[0];
          const form = select.closest('form');
          if (opt && form) {{
            form.elements.client.value = opt.dataset.client || form.elements.client.value;
            form.elements.position.value = opt.dataset.position || form.elements.position.value;
            if (form.elements.candidate_name) form.elements.candidate_name.value = opt.dataset.name || form.elements.candidate_name.value;
            if (form.elements.candidate_title) form.elements.candidate_title.value = opt.dataset.title || form.elements.candidate_title.value;
          }}
        }});
      }}
      for (const select of document.querySelectorAll('select[data-source=\"positions\"]')) {{
        for (const item of data.positions) {{
          const opt = document.createElement('option');
          opt.value = item.id;
          opt.textContent = `${{item.client}} / ${{item.position}}${{item.gap ? '｜缺口 ' + item.gap : ''}}`;
          opt.dataset.client = item.client || '';
          opt.dataset.position = item.position || '';
          select.appendChild(opt);
        }}
        select.addEventListener('change', () => {{
          const opt = select.selectedOptions[0];
          const form = select.closest('form');
          if (opt && form) {{
            form.elements.client.value = opt.dataset.client || form.elements.client.value;
            form.elements.position.value = opt.dataset.position || form.elements.position.value;
          }}
        }});
      }}
      for (const select of document.querySelectorAll('select[data-source=\"tasks\"]')) {{
        for (const item of data.tasks) {{
          const opt = document.createElement('option');
          opt.value = item.task_id;
          opt.textContent = `#${{item.task_id}}｜${{item.candidate_name}}｜${{item.current_project}}｜${{item.match_confidence}}`;
          select.appendChild(opt);
        }}
      }}
      for (const select of document.querySelectorAll('select[data-source=\"open-tasks\"]')) {{
        for (const item of data.open_tasks) {{
          const opt = document.createElement('option');
          opt.value = item.id;
          opt.textContent = `#${{item.id}}｜${{item.candidate_name || '未识别'}}｜${{item.task_type}}｜${{item.reason || ''}}`;
          select.appendChild(opt);
        }}
      }}
    }}
    for (const form of document.querySelectorAll('form[data-endpoint]')) {{
      form.addEventListener('submit', async (event) => {{
        event.preventDefault();
        const button = form.querySelector('button');
        const status = form.querySelector('.status-line');
        button.disabled = true;
        status.className = 'status-line';
        status.textContent = '正在写入并刷新...';
        try {{
          const data = await api(form.dataset.endpoint, formData(form));
          status.className = 'status-line ok';
          status.textContent = data.message || '已完成';
          setTimeout(() => window.location.reload(), 800);
        }} catch (err) {{
          status.className = 'status-line error';
          status.textContent = err.message || String(err);
        }} finally {{
          button.disabled = false;
        }}
      }});
    }}
    loadOptions().catch(err => console.warn(err));
  </script>
"""
    return html_text.replace("</body>", script + "\n</body>", 1)


def inject_asa_navigation_hash(html_text: str) -> str:
    if "data-asa-navigation-hash" in html_text:
        return html_text
    script = r"""
<script data-asa-navigation-hash>
(function(){
  function showHashNavigationHint(label){
    var id = 'asa-hash-navigation-hint';
    var node = document.getElementById(id);
    if (!node) {
      node = document.createElement('div');
      node.id = id;
      node.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:260;max-width:min(320px,calc(100vw - 32px));padding:8px 10px;border:1px solid #cfd8d3;background:#fff;color:#24352f;box-shadow:0 8px 22px rgba(15,23,42,.16);font:12px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;';
      document.body.appendChild(node);
    }
    node.textContent = label || '已定位到 A 系统对象';
    clearTimeout(node._asaTimer);
    node._asaTimer = setTimeout(function(){ if (node && node.parentNode) node.parentNode.removeChild(node); }, 2200);
  }
  function hashParams(){
    var raw = String(location.hash || '').replace(/^#/, '');
    try { return new URLSearchParams(raw); } catch (_) { return new URLSearchParams(); }
  }
  function parseCandidateHash(){
    var params = hashParams();
    var id = params.get('candidate') || params.get('jobCandidateId') || '';
    if (id) return id;
    var match = String(location.hash || '').match(/(?:candidate|jobCandidateId)=([0-9]+)/i);
    return match ? match[1] : '';
  }
  function parseWorkflowHash(){
    return hashParams().get('workflow') || '';
  }
  function parseJobHash(){
    var params = hashParams();
    return {
      jobId: params.get('job') || params.get('jobId') || '',
      client: params.get('client') || '',
      position: params.get('position') || params.get('jobTitle') || ''
    };
  }
  function openHashedCandidate(){
    var id = parseCandidateHash();
    if (!id) return false;
    if (typeof window.openCandidateFromFlow === 'function') {
      var ok = !!window.openCandidateFromFlow(id);
      if (ok) showHashNavigationHint('已打开人选关系 #' + id);
      return ok;
    }
    return false;
  }
  function openHashedWorkflow(){
    var id = parseWorkflowHash();
    if (!id) return false;
    if (typeof window.openAgentGoalWorkflow === 'function') {
      window.openAgentGoalWorkflow(id, true);
      showHashNavigationHint('已打开 ASA 目标');
      return true;
    }
    return false;
  }
  function openHashedJob(){
    var target = parseJobHash();
    var client = target.client;
    var position = target.position;
    var data = (typeof DATA !== 'undefined') ? DATA : window.DATA;
    if (target.jobId && /^\d+$/.test(target.jobId) && data && data.v3 && Array.isArray(data.v3.jobLifecycle)) {
      var hit = data.v3.jobLifecycle.find(function(item){ return String(item.jobId || item.id || '') === String(target.jobId); });
      if (hit) { client = hit.client || client; position = hit.job || hit.position || position; }
    }
    if (!client || !position) return false;
    if (typeof window.activateWorkbenchTab === 'function') window.activateWorkbenchTab('positions');
    if (typeof window.activateQueueItem === 'function') {
      window.activateQueueItem(client, position);
      showHashNavigationHint('已定位岗位：' + client + ' / ' + position);
      return true;
    }
    return false;
  }
  function openHashedTarget(){
    return openHashedWorkflow() || openHashedCandidate() || openHashedJob();
  }
  function retryOpenHashedTarget(){
    var attempts = 0;
    var timer = setInterval(function(){
      attempts += 1;
      if (openHashedTarget() || attempts >= 30) clearInterval(timer);
    }, 200);
  }
  window.addEventListener('hashchange', retryOpenHashedTarget);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', retryOpenHashedTarget);
  else retryOpenHashedTarget();
})();
</script>
"""
    return html_text.replace("</body>", script + "\n</body>", 1)


class WorkbenchHandler(BaseHTTPRequestHandler):
    state: WorkbenchState

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        send_common_headers(self)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path in {"/", "/workbench"}:
                path = self.state.asa_workbench()
                text_response(self, inject_asa_navigation_hash(path.read_text(encoding="utf-8")))
            elif parsed.path in {"/asa", "/a-system"}:
                path = self.state.asa_workbench()
                text_response(self, inject_asa_navigation_hash(path.read_text(encoding="utf-8")))
            elif parsed.path == "/asa-floating":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地页面读取 ASA 浮窗"}, HTTPStatus.FORBIDDEN)
                    return
                text_response(self, asa_floating_html())
            elif parsed.path == "/legacy-workbench":
                path = self.state.build_server_workbench()
                text_response(self, path.read_text(encoding="utf-8"))
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                send_common_headers(self)
                self.end_headers()
            elif parsed.path == "/health/a_system_live_refresh_status.js":
                path = self.state.live_refresh_status_script()
                text_response(
                    self,
                    path.read_text(encoding="utf-8"),
                    "application/javascript; charset=utf-8",
                )
            elif parsed.path == "/api/context":
                json_response(
                    self,
                    {
                        "ok": True,
                        "candidates": load_recent_candidates(self.state.db_path),
                        "positions": load_positions_for_form(self.state.db_path),
                        "tasks": load_confirmation_tasks(self.state.db_path),
                        "open_tasks": load_open_tasks(self.state.db_path),
                    },
                )
            elif parsed.path == "/api/recent-outreach-project":
                query = {
                    key: values[-1]
                    for key, values in urllib.parse.parse_qs(parsed.query).items()
                    if values
                }
                json_response(self, lookup_recent_outreach_project(self.state, query))
            elif parsed.path == "/api/asa/floating/state":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地页面读取 ASA 浮窗状态"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, build_floating_state(self.state))
            elif parsed.path == "/api/asa/floating/commands":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非猎聘/X-SaaS 页面读取桥接命令"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                surface = clean((query.get("surface") or [""])[-1])
                instance_id = clean((query.get("instance_id") or [""])[-1])
                json_response(self, drain_floating_commands(surface, instance_id))
            elif parsed.path == "/api/agent/run":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 运行"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                run_id = clean((query.get("run_id") or [""])[-1])
                result = self.state.agent_service.get_run(run_id)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.NOT_FOUND)
            elif parsed.path == "/api/agent/candidate-state":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 判断"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                job_candidate_id = parse_required_int((query.get("job_candidate_id") or [""])[-1], "job_candidate_id")
                json_response(self, self.state.agent_service.get_candidate_state(job_candidate_id))
            elif parsed.path == "/api/agent/workbench":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 工作台"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 12
                json_response(self, self.state.agent_service.get_workbench(limit))
            elif parsed.path == "/api/agent/dashboard":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 驾驶舱"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, self.state.agent_service.get_dashboard())
            elif parsed.path == "/api/agent/config/public":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 配置"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, self.state.agent_service.get_public_config())
            elif parsed.path == "/api/agent/skills":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Skill"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, self.state.agent_service.list_skills())
            elif parsed.path == "/api/agent/copilot/session":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取对话"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                session_id = clean((query.get("session_id") or [""])[-1])
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 100
                json_response(self, self.state.agent_service.get_copilot_session(session_id, limit))
            elif parsed.path == "/api/agent/copilot/sessions":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取对话历史"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 30
                json_response(self, self.state.agent_service.list_copilot_sessions(limit))
            elif parsed.path == "/api/agent/goals":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取目标"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                status = clean((query.get("status") or [""])[-1])
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 30
                json_response(self, self.state.agent_service.list_goals(status, limit))
            elif parsed.path == "/api/agent/goal-templates":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取目标模板"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, self.state.agent_service.list_goal_templates())
            elif parsed.path == "/api/agent/workflows/quality":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取工作流质量"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, self.state.agent_service.get_workflow_quality())
            elif parsed.path.startswith("/api/agent/workflows/") and parsed.path.endswith("/summary"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取工作流摘要"}, HTTPStatus.FORBIDDEN)
                    return
                suffix = clean(parsed.path.removeprefix("/api/agent/workflows/"))
                workflow_id = clean(suffix.removesuffix("/summary"))
                json_response(self, self.state.agent_service.get_workflow_summary(workflow_id))
            elif parsed.path.startswith("/api/agent/workflows/"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取工作流"}, HTTPStatus.FORBIDDEN)
                    return
                workflow_id = clean(parsed.path.removeprefix("/api/agent/workflows/"))
                json_response(self, self.state.agent_service.get_workflow(workflow_id))
            elif parsed.path.startswith("/api/agent/artifacts/"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取产物"}, HTTPStatus.FORBIDDEN)
                    return
                suffix = clean(parsed.path.removeprefix("/api/agent/artifacts/"))
                artifact_id, _, action = suffix.partition("/")
                payload = self.state.agent_service.get_workflow_artifact(artifact_id)
                if action == "file":
                    artifact = payload.get("artifact") or {}
                    path = Path(str(artifact.get("file_path") or "")).expanduser().resolve()
                    allowed_root = (self.state.agent_service.db_path.parent / "asa_artifacts").resolve()
                    if not path.exists() or allowed_root not in path.parents:
                        raise ValueError("产物文件不存在或不在 ASA 产物目录")
                    binary_response(self, path, str(artifact.get("mime_type") or "application/octet-stream"), path.name)
                else:
                    json_response(self, payload)
            elif parsed.path == "/api/agent/events":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取执行事件"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                event_id = parse_optional_int((query.get("since") or [""])[-1]) or 0
                workflow_id = clean((query.get("workflow_id") or [""])[-1])
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 100
                payload = self.state.agent_service.get_workflow_events(event_id, workflow_id, limit)
                if truthy((query.get("stream") or [""])[-1]):
                    text_response(self, f"event: workflow\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n", "text/event-stream; charset=utf-8")
                else:
                    json_response(self, payload)
            elif parsed.path == "/api/agent/memories":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取记忆"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                status = clean((query.get("status") or ["active"])[-1]) or "active"
                scope_type = clean((query.get("scope_type") or [""])[-1])
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 50
                json_response(self, self.state.agent_service.list_memories(status=status, scope_type=scope_type, limit=limit))
            elif parsed.path == "/api/flow/inbox":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取行动收件箱"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                json_response(
                    self,
                    self.state.agent_service.get_flow_inbox(
                        queue=clean((query.get("queue") or ["今日待办"])[-1]) or "今日待办",
                        client=clean((query.get("client") or [""])[-1]),
                        job=clean((query.get("job") or [""])[-1]),
                        search=clean((query.get("search") or [""])[-1]),
                        view=clean((query.get("view") or ["action"])[-1]) or "action",
                        limit=parse_optional_int((query.get("limit") or [""])[-1]) or 100,
                    ),
                )
            elif parsed.path == "/api/flow/item":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取行动项"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                job_candidate_id = parse_required_int((query.get("job_candidate_id") or [""])[-1], "job_candidate_id")
                json_response(self, self.state.agent_service.get_flow_item(job_candidate_id))
            elif parsed.path == "/api/agent/proposals":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面读取 Agent 待确认提案"}, HTTPStatus.FORBIDDEN)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                status = clean((query.get("status") or ["pending"])[-1]) or "pending"
                limit = parse_optional_int((query.get("limit") or [""])[-1]) or 20
                json_response(self, self.state.agent_service.list_proposals(status, limit))
            elif parsed.path == "/api/refresh":
                refresh = self.state.run_refresh()
                json_response(self, {"ok": True, "refresh": refresh})
            elif parsed.path.startswith("/outputs/"):
                target = (self.state.output_dir / parsed.path.removeprefix("/outputs/")).resolve()
                if not str(target).startswith(str(self.state.output_dir.resolve())) or not target.exists():
                    raise FileNotFoundError("文件不存在")
                text_response(self, target.read_text(encoding="utf-8"), "text/plain; charset=utf-8")
            else:
                json_response(self, {"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            api_error(self, exc, 500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            data = read_request_json(self)
            if parsed.path == "/api/client-feedback":
                json_response(self, write_client_feedback(self.state, data))
            elif parsed.path == "/api/followup-task":
                json_response(self, write_followup_task(self.state, data))
            elif parsed.path == "/api/search-experiment":
                json_response(self, write_search_experiment(self.state, data))
            elif parsed.path == "/api/project-confirmation":
                json_response(self, write_project_confirmation(self.state, data))
            elif parsed.path == "/api/outreach-event":
                json_response(self, write_outreach_event(self.state, data))
            elif parsed.path == "/api/candidate-reply":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非猎聘/X-SaaS页面写入候选人回复"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, write_candidate_reply(self.state, data))
            elif parsed.path == "/api/reply-assistant-sample":
                json_response(self, write_reply_assistant_sample(self.state, data))
            elif parsed.path == "/api/reply-assistant-outreach":
                json_response(self, write_reply_assistant_outreach(self.state, data))
            elif parsed.path == "/api/talent-action":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统或候选人助手页面写入人才动作"}, HTTPStatus.FORBIDDEN)
                    return
                result = apply_talent_action_batch(data)
                json_response(self, result, 200 if result["ok"] else 500)
            elif parsed.path == "/api/candidate-message-preflight":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "decision": "deny", "error": "拒绝非猎聘/X-SaaS页面预检消息"}, HTTPStatus.FORBIDDEN)
                    return
                result = candidate_message_preflight(self.state, data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/candidate-message-commit":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "decision": "deny", "error": "拒绝非猎聘/X-SaaS页面提交消息"}, HTTPStatus.FORBIDDEN)
                    return
                result = candidate_message_commit(self.state, data, refresh=truthy(data.get("refresh")))
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/candidate-state-evidence":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地工作台或候选人助手页面读取状态依据"}, HTTPStatus.FORBIDDEN)
                    return
                job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
                result = candidate_state_evidence(self.state, job_candidate_id)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.NOT_FOUND)
            elif parsed.path == "/api/candidate-state-correction-preflight":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "decision": "deny", "error": "拒绝非本地工作台或候选人助手页面预检状态纠正"}, HTTPStatus.FORBIDDEN)
                    return
                result = candidate_state_correction_preflight(self.state, data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/candidate-state-correction-commit":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "decision": "deny", "error": "拒绝非本地工作台或候选人助手页面提交状态纠正"}, HTTPStatus.FORBIDDEN)
                    return
                result = candidate_state_correction_commit(self.state, data, refresh=truthy(data.get("refresh")))
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/talent-flow-state":
                json_response(self, talent_flow_state(data))
            elif parsed.path == "/api/asa/floating/upload":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 ASA 浮窗上传附件"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, prepare_floating_upload(data))
            elif parsed.path == "/api/asa/floating/context":
                if not bridge_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地或候选人页面上报 ASA 浮窗上下文"}, HTTPStatus.FORBIDDEN)
                    return
                result = update_floating_context(data)
                context = result.get("context") if isinstance(result.get("context"), dict) else {}
                try:
                    snapshot = self.state.agent_service.record_context_snapshot(
                        clean(context.get("surface")) or "floating",
                        context,
                    )
                    result["snapshot"] = snapshot
                except Exception as exc:
                    result["snapshot_error"] = str(exc)[:300]
                json_response(self, result)
            elif parsed.path == "/api/asa/floating/command-result":
                if not bridge_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地或候选人页面上报 ASA 浮窗命令结果"}, HTTPStatus.FORBIDDEN)
                    return
                result = update_floating_command_result(data)
                try:
                    self.state.agent_service.record_tool_call(
                        tool_name=f"bridge.{clean(data.get('action') or data.get('command') or 'command_result')}",
                        permission_level="write",
                        request=data,
                        result=result,
                        status=clean(data.get("status")) or "completed",
                    )
                except Exception:
                    pass
                json_response(self, result)
            elif parsed.path == "/api/asa/floating/show":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面唤起 ASA 浮窗"}, HTTPStatus.FORBIDDEN)
                    return
                result = show_asa_floating_app()
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.NOT_FOUND)
            elif parsed.path == "/api/asa/floating/action":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 ASA 浮窗执行动作"}, HTTPStatus.FORBIDDEN)
                    return
                result = route_floating_action(self.state, data)
                action_name = clean(data.get("action")) or "unknown"
                permission_level = "read"
                if action_name in {"start_workflow", "fill_resume", "generate_report", "draft_outreach", "job_publish_prepare"}:
                    permission_level = "write"
                if action_name in {"dry-intake", "dry-continue", "dry-stop", "identity-match"}:
                    permission_level = "write"
                try:
                    self.state.agent_service.record_tool_call(
                        tool_name=f"floating.{action_name}",
                        permission_level=permission_level,
                        request=data,
                        result=result,
                        status=clean(result.get("status")) or ("completed" if result.get("ok") else "blocked"),
                    )
                    if result.get("workflow") and permission_level != "read":
                        self.state.agent_service.record_permission_request(
                            tool_name=f"floating.{action_name}",
                            permission_level=permission_level,
                            risk_level="medium",
                            reason="ASA Floating 已创建可审计工作流；真正外部动作仍由 workflow approval 单次确认。",
                            preview=result,
                            status="planned",
                            scope="asa_floating",
                        )
                except Exception:
                    pass
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/asa/floating/command":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 ASA 浮窗创建页面命令"}, HTTPStatus.FORBIDDEN)
                    return
                result = enqueue_floating_command(data)
                try:
                    self.state.agent_service.record_tool_call(
                        tool_name=f"bridge.enqueue.{clean(data.get('action') or data.get('command') or 'command')}",
                        permission_level="write",
                        request=data,
                        result=result,
                        status="queued",
                    )
                except Exception:
                    pass
                json_response(self, result, HTTPStatus.CREATED)
            elif parsed.path == "/api/agent/candidate-assess":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面启动 Agent 评估"}, HTTPStatus.FORBIDDEN)
                    return
                job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
                trigger = clean(data.get("trigger")) or "manual"
                if trigger == "candidate_open" and not truthy(data.get("page_active")):
                    json_response(
                        self,
                        {"ok": False, "error": "人选列表未激活，拒绝后台自动评估"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                result = self.state.agent_service.submit_assessment(
                    job_candidate_id,
                    force=truthy(data.get("force")),
                    trigger=trigger,
                )
                status = 200 if result.get("status") == "completed" else HTTPStatus.ACCEPTED
                json_response(self, result, status)
            elif parsed.path == "/api/agent/copilot":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面调用 Copilot"}, HTTPStatus.FORBIDDEN)
                    return
                raw_context = data.get("context")
                context = raw_context if isinstance(raw_context, dict) else {}
                core_service = getattr(self.state, "core_service", None)
                json_response(
                    self,
                    (core_service.copilot if core_service is not None else self.state.agent_service.copilot)(
                        clean(data.get("message")),
                        session_id=clean(data.get("session_id")),
                        context=context,
                    ),
                )
            elif parsed.path == "/api/asa/floating/image-detect":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 ASA 浮窗识别图片"}, HTTPStatus.FORBIDDEN)
                    return
                encoded = clean(data.get("image_base64"))
                if not encoded or len(encoded) > 24 * 1024 * 1024:
                    raise ValueError("图片识别请求为空或超过大小限制")
                try:
                    image_data = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ValueError("图片识别请求不是有效 Base64") from exc
                result = detect_wechat_image_bubble(image_data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY)
            elif parsed.path == "/api/agent/goals":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面创建目标"}, HTTPStatus.FORBIDDEN)
                    return
                raw_context = data.get("context")
                result = self.state.agent_service.create_goal(
                    clean(data.get("objective")),
                    raw_context if isinstance(raw_context, dict) else {},
                    parse_optional_int(data.get("priority")) or 2,
                )
                json_response(self, result, HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/agent/workflows/"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面修改工作流"}, HTTPStatus.FORBIDDEN)
                    return
                suffix = clean(parsed.path.removeprefix("/api/agent/workflows/"))
                workflow_id, _, action = suffix.partition("/")
                if action == "start":
                    result = self.state.agent_service.start_workflow(workflow_id)
                elif action == "revise":
                    result = self.state.agent_service.revise_workflow(workflow_id, clean(data.get("instruction")))
                elif action == "cancel":
                    result = self.state.agent_service.cancel_workflow(workflow_id, clean(data.get("note")))
                elif action == "feedback":
                    raw_correction = data.get("correction")
                    result = self.state.agent_service.record_workflow_feedback(
                        workflow_id, clean(data.get("feedback_type")), clean(data.get("note")),
                        raw_correction if isinstance(raw_correction, dict) else {},
                    )
                else:
                    raise ValueError("未知工作流动作")
                json_response(self, result)
            elif parsed.path.startswith("/api/agent/steps/"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面重试步骤"}, HTTPStatus.FORBIDDEN)
                    return
                suffix = clean(parsed.path.removeprefix("/api/agent/steps/"))
                step_text, _, action = suffix.partition("/")
                if action != "retry":
                    if action != "complete-external":
                        raise ValueError("未知步骤动作")
                    raw_result = data.get("result")
                    json_response(
                        self,
                        self.state.agent_service.complete_external_workflow_step(
                            parse_required_int(step_text, "step_id"), raw_result if isinstance(raw_result, dict) else {}
                        ),
                    )
                else:
                    json_response(self, self.state.agent_service.retry_workflow_step(parse_required_int(step_text, "step_id")))
            elif parsed.path.startswith("/api/agent/approvals/"):
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面处理审批"}, HTTPStatus.FORBIDDEN)
                    return
                suffix = clean(parsed.path.removeprefix("/api/agent/approvals/"))
                approval_id, _, action = suffix.partition("/")
                if action != "decide":
                    raise ValueError("未知审批动作")
                json_response(
                    self,
                    self.state.agent_service.decide_workflow_approval(
                        approval_id, clean(data.get("decision")), clean(data.get("note"))
                    ),
                )
            elif parsed.path == "/api/agent/skills/execute":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面执行 Skill"}, HTTPStatus.FORBIDDEN)
                    return
                raw_context = data.get("context")
                raw_inputs = data.get("inputs")
                json_response(
                    self,
                    self.state.agent_service.execute_skill(
                        clean(data.get("skill_id")),
                        context=raw_context if isinstance(raw_context, dict) else {},
                        inputs=raw_inputs if isinstance(raw_inputs, dict) else {},
                    ),
                )
            elif parsed.path == "/api/agent/memory/revoke":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面撤销记忆"}, HTTPStatus.FORBIDDEN)
                    return
                memory_id = parse_required_int(data.get("memory_id"), "memory_id")
                json_response(self, self.state.agent_service.revoke_memory(memory_id))
            elif parsed.path == "/api/agent/batch-assess":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面批量启动 Agent 评估"}, HTTPStatus.FORBIDDEN)
                    return
                raw_ids = data.get("job_candidate_ids")
                job_candidate_ids = raw_ids if isinstance(raw_ids, list) else []
                result = self.state.agent_service.batch_assess(
                    job_candidate_ids,
                    limit=parse_optional_int(data.get("limit")) or 5,
                    trigger=clean(data.get("trigger")) or "agent_workbench_batch",
                )
                json_response(self, result, HTTPStatus.ACCEPTED if result.get("started") else 200)
            elif parsed.path == "/api/agent/auto-assess-all":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面启动自动评估"}, HTTPStatus.FORBIDDEN)
                    return
                result = self.state.agent_service.auto_assess_all(
                    limit=parse_optional_int(data.get("limit")) or 50,
                    trigger=clean(data.get("trigger")) or "overview_auto_queue",
                )
                json_response(self, result, HTTPStatus.ACCEPTED if result.get("started") else 200)
            elif parsed.path == "/api/agent/proposals-generate":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面生成 Agent 提案"}, HTTPStatus.FORBIDDEN)
                    return
                raw_ids = data.get("job_candidate_ids")
                job_candidate_ids = raw_ids if isinstance(raw_ids, list) else []
                json_response(
                    self,
                    self.state.agent_service.generate_proposals(
                        job_candidate_ids,
                        limit=parse_optional_int(data.get("limit")) or 12,
                    ),
                )
            elif parsed.path == "/api/agent/verification-tasks-sync":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面同步核验任务"}, HTTPStatus.FORBIDDEN)
                    return
                raw_ids = data.get("job_candidate_ids")
                job_candidate_ids = raw_ids if isinstance(raw_ids, list) else []
                generated = self.state.agent_service.generate_proposals(
                    job_candidate_ids,
                    limit=parse_optional_int(data.get("limit")) or 50,
                )
                executed: list[dict[str, Any]] = []
                skipped: list[dict[str, Any]] = list(generated.get("skipped") or [])
                for proposal in generated.get("proposals") or []:
                    if proposal.get("status") != "pending":
                        continue
                    proposal_id = clean(proposal.get("proposal_id"))
                    try:
                        preflight = self.state.agent_service.proposal_preflight(proposal_id)
                        policy = preflight.get("policy") or {}
                        if (
                            preflight.get("action_type") != "create_task"
                            or policy.get("decision") != "allow"
                            or policy.get("risk_level") != "R1"
                        ):
                            skipped.append({"proposal_id": proposal_id, "reason": "仅允许自动创建 R1 内部任务"})
                            continue
                        approved = self.state.agent_service.decide_proposal(
                            proposal_id,
                            preflight["confirmation_token"],
                            "approve",
                            "Agent 自动创建低风险内部核验任务",
                        )
                        executed.append(
                            execute_agent_task_proposal(
                                self.state,
                                proposal_id,
                                approved,
                                refresh=False,
                            )
                        )
                    except Exception as exc:
                        skipped.append({"proposal_id": proposal_id, "reason": str(exc)[:500]})
                if executed:
                    refresh_a_system_workbench()
                json_response(
                    self,
                    {
                        "ok": True,
                        "executed": executed,
                        "executed_total": len(executed),
                        "skipped": skipped,
                    },
                )
            elif parsed.path == "/api/agent/proposal-preflight":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面预检 Agent 提案"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(
                    self,
                    self.state.agent_service.proposal_preflight(clean(data.get("proposal_id"))),
                )
            elif parsed.path == "/api/agent/proposal-decide":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面确认 Agent 提案"}, HTTPStatus.FORBIDDEN)
                    return
                proposal_id = clean(data.get("proposal_id"))
                result = self.state.agent_service.decide_proposal(
                    proposal_id,
                    clean(data.get("confirmation_token")),
                    clean(data.get("decision")),
                    clean(data.get("note")),
                )
                if result["status"] == "rejected":
                    json_response(self, result)
                    return
                if result["action_type"] != "create_task":
                    self.state.agent_service.finish_proposal(
                        proposal_id,
                        success=False,
                        note=f"V1.5 不支持执行动作：{result['action_type']}",
                    )
                    raise ValueError(f"V1.5 不支持执行动作：{result['action_type']}")
                task_request = result["request"]
                try:
                    conn = connect_feedback_db(self.state.db_path)
                    try:
                        existing_task = conn.execute(
                            """
                            SELECT id,due_at FROM followup_tasks
                            WHERE job_candidate_id=? AND task_type=? AND COALESCE(reason,'')=?
                              AND COALESCE(status,'open')='open'
                            ORDER BY id DESC LIMIT 1
                            """,
                            (
                                result["job_candidate_id"],
                                task_request.get("task_type") or "agent_verification",
                                task_request.get("reason") or "",
                            ),
                        ).fetchone()
                    finally:
                        conn.close()
                    if existing_task:
                        task_result = {
                            "task_id": existing_task["id"],
                            "job_candidate_id": result["job_candidate_id"],
                            "due_at": existing_task["due_at"],
                            "message": "已有相同的开放 Agent 任务",
                        }
                    else:
                        task_result = write_followup_task(self.state, task_request)
                    action_result = self.state.agent_service.record_external_action(
                        job_candidate_id=result["job_candidate_id"],
                        action_type="create_task",
                        request=task_request,
                        result=task_result,
                        idempotency_key=result["dedupe_key"],
                    )
                    action = self.state.agent_service.get_action(result["dedupe_key"])
                    self.state.agent_service.finish_proposal(
                        proposal_id,
                        success=True,
                        action_id=int(action["id"]) if action and action.get("id") else None,
                    )
                    json_response(self, {"ok": True, "proposal_id": proposal_id, "status": "executed", **action_result})
                except Exception as exc:
                    self.state.agent_service.finish_proposal(
                        proposal_id,
                        success=False,
                        note=str(exc)[:500],
                    )
                    raise
            elif parsed.path == "/api/agent/chat":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面使用 Agent 对话"}, HTTPStatus.FORBIDDEN)
                    return
                job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
                result = self.state.agent_service.chat(
                    job_candidate_id,
                    clean(data.get("message")),
                    clean(data.get("session_id")),
                )
                json_response(self, result)
            elif parsed.path == "/api/agent/feedback":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面写入 Agent 反馈"}, HTTPStatus.FORBIDDEN)
                    return
                assessment_id = parse_required_int(data.get("assessment_id"), "assessment_id")
                corrected = data.get("corrected") if isinstance(data.get("corrected"), dict) else {}
                result = self.state.agent_service.record_feedback(
                    assessment_id,
                    clean(data.get("feedback_type")),
                    corrected=corrected,
                    note=clean(data.get("note")),
                )
                json_response(self, result)
            elif parsed.path == "/api/agent/draft":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面生成 Agent 草稿"}, HTTPStatus.FORBIDDEN)
                    return
                job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
                json_response(
                    self,
                    self.state.agent_service.create_draft(
                        job_candidate_id,
                        clean(data.get("instructions")),
                    ),
                )
            elif parsed.path == "/api/agent/task":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面创建 Agent 任务"}, HTTPStatus.FORBIDDEN)
                    return
                job_candidate_id = parse_required_int(data.get("job_candidate_id"), "job_candidate_id")
                task_request = {
                    "job_candidate_id": job_candidate_id,
                    "task_type": clean(data.get("task_type")) or "agent_verification",
                    "reason": clean(data.get("reason")) or "核验 Agent 判断中的关键证据",
                    "due_at": clean(data.get("due_at")),
                    "priority": parse_optional_int(data.get("priority")) or 2,
                    "write": True,
                }
                key_payload = json.dumps(task_request, ensure_ascii=False, sort_keys=True)
                idempotency_key = clean(data.get("idempotency_key")) or hashlib.sha256(
                    f"agent-task|{key_payload}".encode("utf-8")
                ).hexdigest()
                existing_action = self.state.agent_service.get_action(idempotency_key)
                if existing_action and existing_action.get("status") == "executed":
                    cached = json.loads(existing_action.get("result_json") or "{}")
                    json_response(self, {"ok": True, "cached": True, **cached})
                    return
                conn = connect_feedback_db(self.state.db_path)
                try:
                    existing_task = conn.execute(
                        """
                        SELECT id,due_at FROM followup_tasks
                        WHERE job_candidate_id=? AND task_type=? AND COALESCE(reason,'')=?
                          AND COALESCE(status,'open')='open'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (job_candidate_id, task_request["task_type"], task_request["reason"]),
                    ).fetchone()
                finally:
                    conn.close()
                if existing_task:
                    task_result = {
                        "task_id": existing_task["id"],
                        "job_candidate_id": job_candidate_id,
                        "due_at": existing_task["due_at"],
                        "message": "已有相同的开放 Agent 任务",
                    }
                else:
                    task_result = write_followup_task(self.state, task_request)
                result = self.state.agent_service.record_external_action(
                    job_candidate_id=job_candidate_id,
                    action_type="create_task",
                    request=task_request,
                    result=task_result,
                    idempotency_key=idempotency_key,
                )
                json_response(self, result)
            elif parsed.path == "/api/agent/verification-complete":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 ASA 页面提交核验结果"}, HTTPStatus.FORBIDDEN)
                    return
                result = complete_agent_verification(self.state, data)
                json_response(
                    self,
                    result,
                    HTTPStatus.ACCEPTED if result.get("run_id") else 200,
                )
            elif parsed.path == "/api/agent/learning-preflight":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面预检学习规则"}, HTTPStatus.FORBIDDEN)
                    return
                rule_id = parse_required_int(data.get("rule_id"), "rule_id")
                json_response(self, self.state.agent_service.learning_preflight(rule_id))
            elif parsed.path == "/api/agent/learning-commit":
                if not agent_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面启用学习规则"}, HTTPStatus.FORBIDDEN)
                    return
                rule_id = parse_required_int(data.get("rule_id"), "rule_id")
                json_response(
                    self,
                    self.state.agent_service.learning_commit(
                        rule_id,
                        clean(data.get("confirmation_token")),
                    ),
                )
            elif parsed.path == "/api/talent-link-lookup":
                json_response(self, lookup_talent_link(data))
            elif parsed.path == "/api/xsaas-candidate-lookup":
                json_response(self, lookup_xsaas_candidate_status(data))
            elif parsed.path == "/api/candidate-identity-matches":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非猎聘/X-SaaS页面访问候选人身份接口"}, HTTPStatus.FORBIDDEN)
                    return
                json_response(self, discover_candidate_identity_matches(data))
            elif parsed.path == "/api/candidate-merge":
                if not candidate_assistant_origin_allowed(self):
                    json_response(self, {"ok": False, "decision": "deny", "error": "拒绝非猎聘/X-SaaS页面执行档案合并"}, HTTPStatus.FORBIDDEN)
                    return
                result = merge_candidate_profiles(data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/position-jd":
                json_response(self, write_position_jd(self.state, data))
            elif parsed.path == "/api/candidate-status":
                json_response(self, write_candidate_status(self.state, data))
            elif parsed.path == "/api/close-task":
                json_response(self, close_followup_task(self.state, data))
            elif parsed.path == "/api/sourcing-run":
                if not sourcing_origin_allowed(self):
                    json_response(self, {"ok": False, "error": "拒绝非本地 A 系统页面启动 Codex 寻访"}, HTTPStatus.FORBIDDEN)
                    return
                result = start_sourcing_run(self.state, data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.CONFLICT)
            elif parsed.path == "/api/sourcing-run-status":
                result = sourcing_run_status(data)
                json_response(self, result, 200 if result.get("ok") else HTTPStatus.NOT_FOUND)
            else:
                json_response(self, {"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            api_error(self, exc)


def ensure_workbench_runtime_schema(db_path: Path) -> None:
    conn = connect_reply_db(db_path)
    try:
        ensure_reply_schema(conn)
        ensure_effective_candidate_events_schema(conn)
        ensure_agent_schema(conn)
        ensure_stop_reason_schema(conn)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Liepin workbench server.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open-refresh", action="store_true")
    args = parser.parse_args()

    state = WorkbenchState(Path(args.db).expanduser(), Path(args.output_dir).expanduser(), args.host, args.port)
    ensure_workbench_runtime_schema(state.db_path)
    if not args.no_open_refresh:
        try:
            state.run_refresh()
        except Exception as exc:
            print(f"启动前刷新失败：{exc}", file=sys.stderr)
    handler = type("ConfiguredWorkbenchHandler", (WorkbenchHandler,), {"state": state})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"ok": True, "url": state.base_url, "db": str(state.db_path)}, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
