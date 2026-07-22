#!/usr/bin/env python3
"""asa doctor — ASA 本机一键体检（PRD R11）。

检查项:
1. core_health        Core 健康（GET /api/v1/health），并核对 health 返回的 version/db 与 manifest 一致
2. db_integrity       v3 正式库可达 + PRAGMA integrity_check（只读打开，绝不写入）
3. version_consistency 原生 App / React / 三枚扩展的实测版本与 asa-release.json 一致
4. backup_freshness   R13 备份新鲜度：~/.hermes/backups/asa_v3/ 最新备份距今 ≤ 24h
5. disk_space         v3 库所在卷剩余空间（WARN < 20GB / FAIL < 5GB）
6. log_size           Core 与备份日志大小（WARN > 200MB / FAIL > 1GB 单文件）

用法:
    python3 scripts/asa_doctor.py                  # 人类可读报告；有 FAIL 退出码 1
    python3 scripts/asa_doctor.py --json           # 机器可读报告
    python3 scripts/asa_doctor.py --update-manifest  # 实测各组件版本并刷新 asa-release.json
    npm run doctor                                 # 等价入口

合法升级组件版本后，用 --update-manifest 刷新清单再提交，doctor 即恢复一致。
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asa_v3_backup  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "asa-release.json"
LOG_DIR = Path.home() / ".hermes" / "logs"
CORE_LOGS = (
    "liepin_workbench_server.log",
    "liepin_workbench_server_error.log",
    "asa_v3_backup.log",
    "asa_v3_backup_error.log",
)
BACKUP_MAX_AGE_HOURS = 24
DISK_WARN_GB, DISK_FAIL_GB = 20, 5
LOG_WARN_MB, LOG_FAIL_MB = 200, 1024

# 组件探测定义：id → (显示名, 实测版本来源)。路径默认与 VERSIONS.md 一致，可用环境变量覆盖。
LIEPIN_ROOT = Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence")


def default_components() -> list[dict]:
    return [
        {
            "id": "native_app",
            "name": "ASA 原生 macOS App",
            "path": "/Users/messi/Applications/ASA.app",
            "source_path": str(LIEPIN_ROOT / "asa-floating-app"),
            "version_source": "Info.plist CFBundleShortVersionString/CFBundleVersion",
        },
        {
            "id": "react_app",
            "name": "React 前端（asa-web）",
            "path": str(REPO_ROOT),
            "version_source": "package.json version",
        },
        {
            "id": "asa_core",
            "name": "ASA Core",
            "source_path": str(LIEPIN_ROOT / "scripts" / "asa_core"),
            "launchagent": "ai.hermes.liepin-workbench",
            "base_url": os.environ.get("ASA_CORE_URL", "http://127.0.0.1:8765"),
            "version_source": "GET /api/v1/health version",
        },
        {
            "id": "liepin_extension",
            "name": "猎聘专业回复助手扩展",
            "path": str(LIEPIN_ROOT / "liepin-reply-assistant-extension"),
            "version_source": "manifest.json version",
        },
        {
            "id": "xsaas_extension",
            "name": "X-SaaS 人选推进助手扩展",
            "path": str(LIEPIN_ROOT / "xsaas-candidate-assistant-extension"),
            "version_source": "manifest.json version",
        },
        {
            "id": "opencli_extension",
            "name": "OpenCLI 私有扩展",
            "path": str(REPO_ROOT / "opencli" / "opencli-extension-v1.0.22"),
            "version_source": "manifest.json version",
        },
        {
            "id": "v3_db",
            "name": "A 系统 v3 SQLite（唯一业务事实源，只读）",
            "path": str(asa_v3_backup.DEFAULT_DB),
            "version_source": "文件名日期标签",
            "backup_dir": str(asa_v3_backup.DEFAULT_BACKUP_DIR),
            "backup_launchagent": "ai.hermes.asa-v3-backup",
        },
    ]


# ---------- 实测探针（IO，全部只读） ----------

def probe_native_app(app_path: Path) -> dict:
    info = app_path / "Contents" / "Info.plist"
    with info.open("rb") as fh:
        plist = plistlib.load(fh)
    return {
        "version": str(plist.get("CFBundleShortVersionString", "")),
        "build": str(plist.get("CFBundleVersion", "")),
    }


def probe_package_version(repo_path: Path) -> dict:
    data = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    return {"version": str(data.get("version", ""))}


def probe_extension_version(ext_path: Path) -> dict:
    data = json.loads((ext_path / "manifest.json").read_text(encoding="utf-8"))
    return {"version": str(data.get("version", "")), "name": str(data.get("name", ""))}


def probe_core_health(base_url: str, timeout: float = 4.0) -> dict:
    with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_live_versions(components: list[dict]) -> dict[str, dict]:
    """--update-manifest 用：逐项实测。asa_core/v3_db 走专门口径。"""
    live: dict[str, dict] = {}
    for comp in components:
        cid = comp["id"]
        try:
            if cid == "native_app":
                live[cid] = probe_native_app(Path(comp["path"]))
            elif cid == "react_app":
                live[cid] = probe_package_version(Path(comp["path"]))
            elif cid in ("liepin_extension", "xsaas_extension", "opencli_extension"):
                live[cid] = probe_extension_version(Path(comp["path"]))
            elif cid == "asa_core":
                health = probe_core_health(comp["base_url"])
                live[cid] = {"version": str(health.get("version", "")), "ok": bool(health.get("ok"))}
            elif cid == "v3_db":
                db_path = Path(comp["path"])
                stat = db_path.stat()
                live[cid] = {
                    "version": db_path.stem.replace("talent_system_v3_", ""),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                }
        except Exception as exc:
            live[cid] = {"error": f"{type(exc).__name__}: {exc}"}
    return live


def build_manifest() -> dict:
    components = default_components()
    live = probe_live_versions(components)
    merged = []
    for comp in components:
        entry = dict(comp)
        probed = live.get(comp["id"]) or {}
        entry.update({k: v for k, v in probed.items() if k != "name"})
        merged.append(entry)
    return {
        "schema": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generated_by": "scripts/asa_doctor.py --update-manifest",
        "components": merged,
    }


# ---------- 检查求值（纯函数，可单测） ----------

def _check(cid: str, title: str, level: str, detail: str) -> dict:
    return {"id": cid, "title": title, "level": level, "detail": detail}


def evaluate_health(payload: dict, manifest: dict) -> dict:
    if not payload.get("ok"):
        return _check("core_health", "Core 健康", "fail", f"health 返回异常: {payload}")
    service = payload.get("service", "asa-core")
    version = payload.get("version", "?")
    db = str(payload.get("db", ""))
    expected_db = _manifest_component(manifest, "v3_db").get("path", "")
    expected_version = _manifest_component(manifest, "asa_core").get("version", "")
    if expected_db and db != expected_db:
        return _check("core_health", "Core 健康", "fail", f"Core 连接的库与 manifest 不符: {db} ≠ {expected_db}")
    if expected_version and str(version) != str(expected_version):
        return _check("core_health", "Core 健康", "fail", f"Core 版本 {version} 与 manifest {expected_version} 不一致")
    return _check("core_health", "Core 健康", "ok", f"{service} {version} 在线，连接正式库")


def evaluate_version_consistency(manifest: dict, live: dict[str, dict]) -> list[dict]:
    results = []
    comparable = ("native_app", "react_app", "liepin_extension", "xsaas_extension", "opencli_extension")
    for cid in comparable:
        comp = _manifest_component(manifest, cid)
        title = f"版本一致 · {comp.get('name', cid)}"
        expected = str(comp.get("version", ""))
        probed = live.get(cid) or {}
        if probed.get("error"):
            results.append(_check("version_consistency", title, "fail", f"实测失败: {probed['error']}"))
            continue
        actual = str(probed.get("version", ""))
        if expected and actual == expected:
            extra = f" ({probed['build']})" if probed.get("build") else ""
            results.append(_check("version_consistency", title, "ok", f"{actual}{extra}"))
        else:
            results.append(_check(
                "version_consistency", title, "fail",
                f"实测 {actual or '不可得'} ≠ manifest {expected or '未记录'}；合法升级请跑 --update-manifest",
            ))
    return results


def evaluate_backup_freshness(directory: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    latest = asa_v3_backup.latest_backup(directory)
    if latest is None:
        return _check("backup_freshness", "备份新鲜度", "fail", f"{directory} 内没有任何备份（R13）")
    age_hours = (now.timestamp() - latest.stat().st_mtime) / 3600
    count = len(list(directory.glob(f"{asa_v3_backup.FILENAME_PREFIX}*.db")))
    detail = f"最新备份 {latest.name}，距今 {age_hours:.1f}h，共 {count} 份"
    if age_hours > BACKUP_MAX_AGE_HOURS:
        return _check("backup_freshness", "备份新鲜度", "fail", f"{detail}，超过 {BACKUP_MAX_AGE_HOURS}h")
    return _check("backup_freshness", "备份新鲜度", "ok", detail)


def evaluate_db_integrity(db_path: Path) -> dict:
    if not db_path.exists():
        return _check("db_integrity", "数据库完整性", "fail", f"库不存在: {db_path}")
    verdict = asa_v3_backup.integrity_check(db_path)
    if verdict == "ok":
        size_mb = db_path.stat().st_size / 1024 / 1024
        return _check("db_integrity", "数据库完整性", "ok", f"integrity_check ok（{size_mb:.0f} MB，只读校验）")
    return _check("db_integrity", "数据库完整性", "fail", f"integrity_check: {verdict}")


def evaluate_disk(path: Path) -> dict:
    stat = os.statvfs(path)
    free_gb = stat.f_bavail * stat.f_frsize / 1024 ** 3
    detail = f"{path} 余量 {free_gb:.1f} GB"
    if free_gb < DISK_FAIL_GB:
        return _check("disk_space", "磁盘空间", "fail", f"{detail}（< {DISK_FAIL_GB} GB）")
    if free_gb < DISK_WARN_GB:
        return _check("disk_space", "磁盘空间", "warn", f"{detail}（< {DISK_WARN_GB} GB）")
    return _check("disk_space", "磁盘空间", "ok", detail)


def evaluate_logs(log_dir: Path) -> dict:
    worst, notes = "ok", []
    for name in CORE_LOGS:
        path = log_dir / name
        if not path.exists():
            continue
        size_mb = path.stat().st_size / 1024 / 1024
        notes.append(f"{name} {size_mb:.0f}MB")
        if size_mb > LOG_FAIL_MB:
            worst = "fail"
        elif size_mb > LOG_WARN_MB and worst == "ok":
            worst = "warn"
    return _check("log_size", "日志大小", worst, "；".join(notes) if notes else "日志目录暂无日志文件")


# ---------- 汇总输出 ----------

_LEVEL_ICON = {"ok": "✓", "warn": "⚠", "fail": "✗"}


def render_report(checks: list[dict]) -> str:
    lines = ["asa doctor — ASA 本机体检报告", ""]
    for item in checks:
        lines.append(f"{_LEVEL_ICON[item['level']]} {item['title']}: {item['detail']}")
    counts = {level: sum(1 for item in checks if item["level"] == level) for level in ("ok", "warn", "fail")}
    lines += ["", f"共 {len(checks)} 项：{counts['ok']} 通过 / {counts['warn']} 警告 / {counts['fail']} 失败"]
    return "\n".join(lines)


def exit_code(checks: list[dict]) -> int:
    return 1 if any(item["level"] == "fail" for item in checks) else 0


def _manifest_component(manifest: dict, cid: str) -> dict:
    for comp in manifest.get("components", []):
        if comp.get("id") == cid:
            return comp
    return {}


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.exists():
        return {"schema": 1, "components": default_components()}
    return json.loads(path.read_text(encoding="utf-8"))


def run_checks(manifest: dict, *, core_timeout: float = 4.0) -> list[dict]:
    checks: list[dict] = []
    core = _manifest_component(manifest, "asa_core")
    db_comp = _manifest_component(manifest, "v3_db")
    db_path = Path(db_comp.get("path") or asa_v3_backup.DEFAULT_DB)

    try:
        payload = probe_core_health(str(core.get("base_url") or "http://127.0.0.1:8765"), timeout=core_timeout)
        checks.append(evaluate_health(payload, manifest))
    except Exception as exc:
        checks.append(_check("core_health", "Core 健康", "fail", f"Core 不可达: {type(exc).__name__}: {exc}"))

    checks.append(evaluate_db_integrity(db_path))

    live = probe_live_versions(manifest.get("components", []))
    checks.extend(evaluate_version_consistency(manifest, live))

    backup_dir = Path(db_comp.get("backup_dir") or asa_v3_backup.DEFAULT_BACKUP_DIR)
    checks.append(evaluate_backup_freshness(backup_dir))
    checks.append(evaluate_disk(db_path.parent if db_path.exists() else Path("/")))
    checks.append(evaluate_logs(LOG_DIR))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="asa doctor — ASA 本机一键体检（PRD R11）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    parser.add_argument("--update-manifest", action="store_true", help="实测各组件版本并刷新 asa-release.json")
    args = parser.parse_args(argv)

    if args.update_manifest:
        manifest = build_manifest()
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已刷新 {MANIFEST_PATH}（{len(manifest['components'])} 个组件，生成于 {manifest['generated_at']}）")
        for comp in manifest["components"]:
            version = comp.get("version", "?")
            suffix = f" ({comp['build']})" if comp.get("build") else ""
            flag = f" [探测异常: {comp['error']}]" if comp.get("error") else ""
            print(f"  - {comp['id']}: {version}{suffix}{flag}")
        return 0

    manifest = load_manifest()
    checks = run_checks(manifest)
    if args.json:
        print(json.dumps({"ok": exit_code(checks) == 0, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(render_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
