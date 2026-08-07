#!/usr/bin/env python3
"""Ensure the long-lived Liepin CDP Chrome is reachable on port 9223."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from liepin_cdp_config import (
    cdp_launch_agent_label,
    cdp_launch_agent_path,
    cdp_profile_dir,
    opencli_extension_dir,
)


PORT = 9223
CDP_BASE = f"http://127.0.0.1:{PORT}"
PROFILE_DIR = cdp_profile_dir()
LAUNCH_AGENT = cdp_launch_agent_path()
OPENCLI_EXTENSION_DIR = opencli_extension_dir()
START_URL = "https://h.liepin.com/search/getConditionItem"
STATUS_FILE = Path("/tmp/liepin_cdp_status.json")
CHROME_PATHS = [
    Path.home() / "Library" / "Caches" / "ms-playwright" / "chromium-1228" / "chrome-mac-arm64"
    / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
]


def write_status(**payload: object) -> None:
    payload.setdefault("checked_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    payload.setdefault("port", PORT)
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_json(path: str, timeout: float = 3) -> object:
    with urllib.request.urlopen(f"{CDP_BASE}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def cdp_probe(timeout: float = 3) -> tuple[bool, dict[str, object]]:
    try:
        version = fetch_json("/json/version", timeout=timeout)
        tabs = fetch_json("/json/list", timeout=timeout)
        return True, {"version": version, "tabs": tabs if isinstance(tabs, list) else []}
    except Exception as exc:
        return False, {"error_type": type(exc).__name__, "error": str(exc)}


def run_quiet(*args: str, timeout: float = 8) -> int:
    try:
        return subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode
    except Exception:
        return -1


def cleanup_profile_locks() -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = PROFILE_DIR / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def wait_for_cdp(seconds: float = 12) -> tuple[bool, dict[str, object]]:
    deadline = time.time() + seconds
    last: dict[str, object] = {}
    while time.time() < deadline:
        ok, detail = cdp_probe(timeout=1.5)
        if ok:
            return True, detail
        last = detail
        time.sleep(0.6)
    return False, last


def restart_launch_agent() -> None:
    uid = str(os.getuid())
    label = f"gui/{uid}/{cdp_launch_agent_label()}"
    run_quiet("launchctl", "enable", label)
    run_quiet("launchctl", "bootout", label)
    cleanup_profile_locks()
    if not LAUNCH_AGENT.exists():
        raise RuntimeError(f"LaunchAgent 不存在：{LAUNCH_AGENT}")
    code = run_quiet("launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT))
    if code not in (0, 37):
        raise RuntimeError(f"launchctl bootstrap 失败，退出码={code}")


def start_direct_chrome() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_profile_locks()
    chrome_path = next((path for path in CHROME_PATHS if path.exists()), None)
    if not chrome_path:
        raise RuntimeError("没有找到 Google Chrome 可执行文件")
    log_path = Path.home() / ".hermes" / "chrome_cdp_manual.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    subprocess.Popen(
        [
            str(chrome_path),
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE_DIR}",
            f"--load-extension={OPENCLI_EXTENSION_DIR}",
            f"--disable-extensions-except={OPENCLI_EXTENSION_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            START_URL,
        ],
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


def summarize_tabs(tabs: object) -> list[dict[str, str]]:
    if not isinstance(tabs, list):
        return []
    summary: list[dict[str, str]] = []
    for tab in tabs[:12]:
        if not isinstance(tab, dict):
            continue
        summary.append(
            {
                "type": str(tab.get("type", "")),
                "title": str(tab.get("title", ""))[:120],
                "url": str(tab.get("url", ""))[:240],
            }
        )
    return summary


def main() -> int:
    print(f"检查 Chrome CDP：{CDP_BASE}")
    ok, detail = cdp_probe()
    if ok:
        tabs = summarize_tabs(detail.get("tabs"))
        write_status(ok=True, action="already_available", tabs=tabs)
        print("OK：CDP Chrome 已可连接。")
        print(json.dumps({"tabs": tabs[:5]}, ensure_ascii=False, indent=2))
        return 0

    print(f"当前不可连接，开始按原链路恢复：{detail.get('error')}")
    try:
        restart_launch_agent()
        ok, detail = wait_for_cdp()
        if not ok:
            print("LaunchAgent 恢复后仍未连上，尝试直接启动 Chrome。")
            start_direct_chrome()
            ok, detail = wait_for_cdp()
        if not ok:
            write_status(ok=False, action="failed", detail=detail)
            print(f"失败：{detail}")
            return 2
        tabs = summarize_tabs(detail.get("tabs"))
        write_status(ok=True, action="recovered", tabs=tabs)
        print("OK：CDP Chrome 已恢复。")
        print(json.dumps({"tabs": tabs[:5]}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        write_status(ok=False, action="exception", error_type=type(exc).__name__, error=str(exc))
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
