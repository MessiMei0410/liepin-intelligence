from __future__ import annotations

import re
from typing import Any


PRIVATE_KEYS = {
    "phone",
    "mobile",
    "telephone",
    "email",
    "wechat",
    "wechat_id",
    "wxid",
    "qq",
    "id_card",
    "identity_number",
    "address",
    "exact_address",
    "source_url",
    "resume_url",
    "url",
    "xsaas_id",
    "resume_id",
    "res_id_encode",
    "source_candidate_id",
}

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9](?:[- ]?\d){9}(?!\d)")
LANDLINE_RE = re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")
URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+")
LABELED_PRIVATE_RE = re.compile(
    r"(?im)(现住址|家庭住址|详细地址|通讯地址|联系地址|微信(?:号)?|QQ(?:号)?)[：:]\s*[^\n；;]{2,120}"
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = EMAIL_RE.sub("[邮箱已隐藏]", text)
    text = PHONE_RE.sub("[手机号已隐藏]", text)
    text = LANDLINE_RE.sub("[电话已隐藏]", text)
    text = ID_CARD_RE.sub("[证件号已隐藏]", text)
    text = LABELED_PRIVATE_RE.sub(lambda match: f"{match.group(1)}：[已隐藏]", text)
    return URL_RE.sub("[外部链接已隐藏]", text)


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized == "wechat" and isinstance(item, dict) and (
                "capture_mode" in item or "text_blocks" in item or "accessibility_authorized" in item
            ):
                result[str(key)] = sanitize_payload(item)
                continue
            if normalized in PRIVATE_KEYS:
                continue
            result[str(key)] = sanitize_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_context_snapshot(value: Any) -> Any:
    sanitized = sanitize_payload(value)
    if not isinstance(sanitized, dict):
        return sanitized
    clipboard = sanitized.get("clipboard")
    if isinstance(clipboard, dict):
        sanitized["clipboard"] = {
            key: clipboard[key]
            for key in ("has_text", "change_count")
            if key in clipboard
        }
    return sanitized
