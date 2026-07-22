#!/usr/bin/env python3
"""Check Liepin reply assistant project/profile registration consistency."""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "liepin-reply-assistant-extension"
CONTENT_JS = EXTENSION / "content.js"
PROFILES_JS = EXTENSION / "match-profiles.js"

IGNORED_OPTION_KEYS = {
    "auto",
    "custom",
    "mechanical",
    "device",
    "acdc",
    "weida_mechanical",
    "pengxinxu_device",
    "acdc_director",
    "fpga",
    "power_hardware",
    "power",
    "pqe",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_balanced(source: str, name: str, open_char: str, close_char: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*\{open_char}", source)
    if not match:
        return ""

    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        char = source[i]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
        i += 1

    return source[start : i - 1]


def top_level_object_keys(source: str, object_name: str) -> set[str]:
    body = extract_balanced(source, object_name, "{", "}")
    keys: set[str] = set()
    depth = 0
    line_start = True
    i = 0
    while i < len(body):
        char = body[i]
        if char == "\n":
            line_start = True
            i += 1
            continue
        if char in " \t" and line_start:
            i += 1
            continue
        if char == "{":
            depth += 1
            line_start = False
            i += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            line_start = False
            i += 1
            continue
        if depth == 0 and line_start:
            match = re.match(r"([A-Za-z0-9_]+)\s*:", body[i:])
            if match:
                keys.add(match.group(1))
                i += len(match.group(0))
                line_start = False
                continue
        line_start = False
        i += 1

    return keys


def keys_from_project_options(source: str) -> set[str]:
    body = extract_balanced(source, "PROJECT_OPTIONS", "[", "]")
    return set(re.findall(r"\{\s*key:\s*'([^']+)'", body))


def keys_from_project_rules(source: str) -> set[str]:
    body = extract_balanced(source, "PROJECT_RULES", "[", "]")
    return set(re.findall(r"\bkey:\s*'([^']+)'", body))


def check_recent_project_auto_apply() -> list[str]:
    errors: list[str] = []
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from liepin_workbench_server import WorkbenchState, lookup_recent_outreach_project
    from record_outreach_event import connect, ensure_schema

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        db_path = temp_path / "talent_pool.db"
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO outreach_events (
                    candidate_name, candidate_company, client, position, channel,
                    event_type, event_status, message_summary, source_url, event_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "张三",
                    "示例公司",
                    "士兰微",
                    "技术市场经理（三次电源/服务器或PC市场）",
                    "liepin",
                    "greeting_open_chat",
                    "done",
                    "测试记录",
                    "https://h.liepin.com/resume/showresumedetail/?res_id_encode=abc123",
                    "2026-06-23T10:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        state = WorkbenchState(db_path, temp_path, "127.0.0.1", 0)
        exact = lookup_recent_outreach_project(
            state,
            {
                "resume_id": "abc123",
                "source_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=abc123",
                "candidate_name": "张三",
            },
        )
        name_only = lookup_recent_outreach_project(
            state,
            {
                "candidate_name": "张三",
                "source_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=other456",
            },
        )

    if not exact.get("matched") or exact.get("auto_apply") is not True:
        errors.append("精确简历链接命中时未允许自动识别岗位")
    if name_only.get("auto_apply") is True or name_only.get("match", {}).get("auto_apply") is True:
        errors.append("仅姓名命中时不应自动切换岗位")
    return errors


def main() -> int:
    content = read(CONTENT_JS)
    profiles = read(PROFILES_JS)

    profile_keys = top_level_object_keys(profiles, "profiles")
    option_keys = keys_from_project_options(content)
    rule_keys = keys_from_project_rules(content)

    profile_options = option_keys - IGNORED_OPTION_KEYS
    missing_profiles = sorted(profile_options - profile_keys)
    missing_options = sorted(
        key for key in profile_keys - option_keys
        if not key.startswith("generic_")
    )
    rules_without_profiles = sorted(
        key for key in rule_keys - IGNORED_OPTION_KEYS - profile_keys
    )

    errors = []
    if missing_profiles:
        errors.append(f"下拉选项缺少岗位画像: {', '.join(missing_profiles)}")
    if missing_options:
        errors.append(f"岗位画像缺少手动下拉入口: {', '.join(missing_options)}")
    if rules_without_profiles:
        errors.append(f"自动识别规则缺少岗位画像: {', '.join(rules_without_profiles)}")
    errors.extend(check_recent_project_auto_apply())

    if errors:
        print("猎聘回复助手岗位注册检查未通过:")
        for item in errors:
            print(f"- {item}")
        return 1

    print(
        "猎聘回复助手岗位注册检查通过: "
        f"{len(profile_keys)} 个画像, {len(profile_options)} 个岗位下拉入口, "
        f"{len(rule_keys)} 条识别规则"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
