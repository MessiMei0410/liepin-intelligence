#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import re
from datetime import date, datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_LIEPIN_ROOT = HERE.parent
DEFAULT_A_SYSTEM_ROOT = Path("/Users/messi/Documents/Codex/2026-06-26/re")
DEFAULT_SKILL_ROOT = Path("/Users/messi/.codex/skills/a-system-workbench")

WORK_FILES = [
    "build_talent_workbench.py",
    "sync_a_system_client.py",
    "talent_system_sync.py",
    "a_system_live_refresh.py",
    "xsaas_extension_dom_guard.js",
    "xsaas_open_pages_guard.js",
]

INTEGRATION_SCRIPTS = [
    "confirm_project_assignment.py",
    "ensure_project_confirmation_schema.py",
    "export_reply_assistant_samples.py",
    "generate_liepin_workbench.py",
    "generate_next_search_strategy.py",
    "generate_position_dashboard.py",
    "generate_workflow_status_report.py",
    "liepin_cdp_config.py",
    "liepin_workbench_server.py",
    "position_storage.py",
    "record_candidate_reply.py",
    "record_client_feedback.py",
    "record_outreach_event.py",
    "record_search_experiment.py",
    "reply_intelligence_rules.py",
    "sync_reply_assistant_outreach_events.py",
    "sync_reply_assistant_samples.py",
]

INTEGRATION_TREES = [
    "a_system_agent",
]

SKILL_SCRIPTS = [
    "a_system_db_doctor.py",
    "liepin_outreach_status.py",
]

TEXT_SUFFIXES = {".py", ".js", ".md", ".txt", ".json", ".sh", ".command", ".html", ".sql"}
FORBIDDEN_FILES = {"talent_system_v3.db", "A系统.html"}
FORBIDDEN_PATH_FRAGMENTS = ("/Users/messi/", "Cookies", ".cognee")
KNOWN_PRIVATE_TERMS = {
    "长越科技",
    "士兰微",
    "视源电子",
    "苏科思",
    "秦亚霄",
    "ACDC服务器电源研发总监",
    "FPGA技术主管",
    "用户指定最高优先级",
}

RUNTIME_FILES = [
    "install.sh",
    "configure_install.py",
    "requirements.txt",
    "README.md",
]

BIN_FILES = [
    "start.sh",
    "stop.sh",
    "sync.sh",
    "doctor.sh",
    "uninstall.sh",
    "a_system_startup.py",
]


def copy_file(source: Path, target: Path, executable: bool = False) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def write_schema(db_path: Path, output: Path) -> None:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE sql IS NOT NULL
              AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, name
            """
        ).fetchall()
    finally:
        conn.close()
    lines = ["PRAGMA foreign_keys=OFF;", "BEGIN;"]
    for _kind, _name, sql in rows:
        lines.append(sql.rstrip(";") + ";")
    lines.extend(["COMMIT;", "PRAGMA foreign_keys=ON;", ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def extension_version(path: Path) -> str:
    payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    return str(payload.get("version") or "unknown")


def tenant_terms(db_path: Path) -> dict[str, set[str]]:
    """Read only values that must never be copied into a colleague bundle."""
    conn = sqlite3.connect(db_path)
    try:
        values = {
            "clients": {str(row[0]).strip() for row in conn.execute("SELECT name FROM clients") if row[0]},
            "jobs": {str(row[0]).strip() for row in conn.execute("SELECT title FROM jobs") if row[0]},
            "candidates": {str(row[0]).strip() for row in conn.execute("SELECT name FROM candidates") if row[0]},
        }
    finally:
        conn.close()
    values["clients"].update(KNOWN_PRIVATE_TERMS & {"长越科技", "士兰微", "视源电子", "苏科思"})
    values["candidates"].add("秦亚霄")
    values["jobs"].update({"ACDC服务器电源研发总监", "FPGA技术主管"})
    return values


def replace_between(text: str, start: str, end: str, replacement: str) -> str:
    start_at = text.find(start)
    if start_at < 0:
        raise RuntimeError(f"sanitizer marker missing: {start}")
    end_at = text.find(end, start_at)
    if end_at < 0:
        raise RuntimeError(f"sanitizer marker missing: {end}")
    return text[:start_at] + replacement + text[end_at:]


def sanitize_generator(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"MANUAL_POSITION_ALIASES = \{.*?\n\}\n",
        "MANUAL_POSITION_ALIASES: dict[tuple[str, str], str] = {}\n",
        text,
        count=1,
        flags=re.S,
    )
    # Portable ranking is driven solely by each tenant DB's structured priority.
    text = re.sub(r'\n    if client == ".*?\n        score \+= \d+', "", text)
    text = re.sub(r'\n  if \(p\.client === .*?score \+= \d+;', "", text)
    text = text.replace(
        ".grid2 {{ display: grid; grid-template-columns: minmax(720px, 1.35fr) minmax(360px, .65fr); gap: 16px; }}",
        ".grid2 {{ display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, .65fr); gap: 16px; }}",
    )
    path.write_text(text, encoding="utf-8")


def sanitize_liepin_extension(extension: Path) -> None:
    content = extension / "content.js"
    text = content.read_text(encoding="utf-8")
    text = replace_between(text, "  const PROJECT_RULES = [", "\n\n  const PROJECT_OPTIONS = [", "  const PROJECT_RULES = [];")
    text = replace_between(
        text,
        "  const PROJECT_OPTIONS = [",
        "\n\n  function clean(text)",
        "  const PROJECT_OPTIONS = [\n"
        "    { key: 'auto', label: '从 A 系统动态加载', client: '', position: '', confidence: '自动' },\n"
        "    { key: 'custom', label: '手动输入客户/岗位', client: '', position: '', confidence: '手动输入' }\n"
        "  ];",
    )
    text = replace_between(
        text,
        "  const POSITION_MATCH_PROFILES = window.LIEPIN_MATCH_PROFILES || {",
        "\n\n  function findActiveContactElement()",
        "  const POSITION_MATCH_PROFILES = window.LIEPIN_MATCH_PROFILES || {};",
    )
    content.write_text(text, encoding="utf-8")
    (extension / "match-profiles.js").write_text(
        "(function () {\n  'use strict';\n  window.LIEPIN_MATCH_PROFILES = {};\n})();\n",
        encoding="utf-8",
    )


def scrub_tenant_values(root: Path, values: dict[str, set[str]]) -> None:
    replacements: list[tuple[str, str]] = []
    replacements.extend((value, "客户") for value in values["clients"] if len(value) >= 3)
    replacements.extend((value, "人选") for value in values["candidates"] if len(value) >= 3)
    replacements.extend((value, "岗位") for value in values["jobs"] if len(value) >= 8)
    replacements.extend((value, "通用规则") for value in KNOWN_PRIVATE_TERMS)
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for old, new in replacements:
            text = text.replace(old, new)
        text = text.replace("/Users/messi/", "/Users/portable/")
        path.write_text(text, encoding="utf-8")


def audit_bundle(bundle: Path, private_values: dict[str, set[str]]) -> None:
    problems: list[str] = []
    forbidden_terms = set(KNOWN_PRIVATE_TERMS)
    forbidden_terms.update(value for value in private_values["clients"] if len(value) >= 3)
    forbidden_terms.update(value for value in private_values["candidates"] if len(value) >= 3)
    forbidden_terms.update(value for value in private_values["jobs"] if len(value) >= 8)
    for path in bundle.rglob("*"):
        relative = str(path.relative_to(bundle))
        if any(fragment.lower() in relative.lower() for fragment in FORBIDDEN_PATH_FRAGMENTS):
            problems.append(f"forbidden path: {relative}")
        if not path.is_file():
            continue
        if path.name in FORBIDDEN_FILES or path.suffix.lower() in {".db", ".sqlite", ".xlsx", ".docx", ".pdf"}:
            problems.append(f"forbidden data file: {relative}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for fragment in FORBIDDEN_PATH_FRAGMENTS:
            if fragment in text:
                problems.append(f"private path in {relative}: {fragment}")
        leaked = sorted((term for term in forbidden_terms if term in text), key=len, reverse=True)
        if leaked:
            problems.append(f"tenant content in {relative}: {', '.join(leaked[:5])}")
    if problems:
        raise RuntimeError("portable bundle privacy audit failed:\n" + "\n".join(problems[:50]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an architecture-neutral A 系统 migration bundle.")
    parser.add_argument("--output", type=Path, default=HERE / "dist")
    parser.add_argument("--name", default=f"A-System-Portable-{date.today():%Y%m%d}")
    parser.add_argument("--liepin-root", type=Path, default=Path(os.environ.get("A_SYSTEM_LIEPIN_ROOT", DEFAULT_LIEPIN_ROOT)))
    parser.add_argument("--a-system-root", type=Path, default=Path(os.environ.get("A_SYSTEM_ROOT", DEFAULT_A_SYSTEM_ROOT)))
    parser.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    args = parser.parse_args()

    output_root = args.output.expanduser().resolve()
    bundle = output_root / args.name
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)

    for name in RUNTIME_FILES:
        copy_file(HERE / name, bundle / name, executable=name.endswith(".sh"))
    for name in BIN_FILES:
        copy_file(HERE / "bin" / name, bundle / "bin" / name, executable=True)
    copy_file(HERE / "config" / "a-system.env.example", bundle / "config" / "a-system.env.example")

    payload = bundle / "payload"
    work_source = args.a_system_root / "work"
    for name in WORK_FILES:
        source = work_source / name
        if source.exists():
            copy_file(source, payload / "app" / "work" / name)

    scripts_target = payload / "integrations" / "liepin-intelligence" / "scripts"
    for name in INTEGRATION_SCRIPTS:
        copy_file(args.liepin_root / "scripts" / name, scripts_target / name)
    for name in INTEGRATION_TREES:
        copy_tree(args.liepin_root / "scripts" / name, scripts_target / name)
    copy_file(
        args.liepin_root / "config" / "asa.toml",
        payload / "integrations" / "liepin-intelligence" / "config" / "asa.toml",
    )
    for extension in ("liepin-reply-assistant-extension", "xsaas-candidate-assistant-extension"):
        copy_tree(args.liepin_root / extension, payload / "extensions" / extension)

    for name in SKILL_SCRIPTS:
        copy_file(args.skill_root / "scripts" / name, payload / "codex-skill" / "scripts" / name, executable=True)
    copy_file(HERE / "assets" / "portable_regression_guard.py", payload / "codex-skill" / "scripts" / "a_system_regression_guard.py", executable=True)
    copy_file(HERE / "assets" / "SKILL.md", payload / "codex-skill" / "SKILL.md")
    copy_file(HERE / "assets" / "AGENTS.md", payload / "AGENTS.md")

    current_db = args.a_system_root / "outputs" / "talent_system_v3_20260629.db"
    write_schema(current_db, payload / "data" / "schema.sql")
    private_values = tenant_terms(current_db)
    sanitize_generator(payload / "app" / "work" / "build_talent_workbench.py")
    sanitize_liepin_extension(payload / "extensions" / "liepin-reply-assistant-extension")
    scrub_tenant_values(bundle, private_values)

    versions = {
        "bundle_name": args.name,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "data_included": False,
        "content_policy": "empty-schema-no-candidates-no-jobs-no-clients",
        "supported_architectures": ["arm64", "x86_64"],
        "liepin_extension": extension_version(args.liepin_root / "liepin-reply-assistant-extension"),
        "xsaas_extension": extension_version(args.liepin_root / "xsaas-candidate-assistant-extension"),
    }
    (bundle / "bundle-manifest.json").write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_bundle(bundle, private_values)

    archive = shutil.make_archive(str(output_root / args.name), "zip", root_dir=output_root, base_dir=args.name)
    print(json.dumps({"ok": True, "bundle": str(bundle), "archive": archive, **versions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
