#!/usr/bin/env python3
"""Run a Liepin search loop for an already-published position."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from a_system_agent.liepin_capture import EXTRACT_RESUME_JS, resume_matches_identity


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
LIEPIN_SEARCH_URL = "https://h.liepin.com/search/getConditionItem"
MODEL_VERSION = "published-position-liepin-v1"
CDP_PROFILE_DIR = Path.home() / ".hermes" / "chrome_profile_xhs"
CDP_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "ai.hermes.chrome-cdp.plist"
CHROME_PATHS = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
]


@dataclass
class SearchRound:
    name: str
    query: str
    filters: dict[str, Any]
    result_count: int = 0
    extracted_count: int = 0
    recommended_count: int = 0
    noise_notes: str = ""


@dataclass
class PositionProfile:
    slug: str
    headline: str
    default_city: str
    default_salary: str
    report_title: str
    file_prefix: str
    search_rounds: list[SearchRound]
    target_companies: list[str]
    core_keywords: list[str]
    tool_keywords: list[str]
    title_keywords: list[str]
    noise_keywords: list[str]
    default_noise_note: str
    outreach_summary: str


class CDP:
    """Tiny CDP websocket client; stdlib only so it works in the desktop app."""

    def __init__(self, ws_url: str, timeout: int = 12):
        parsed = urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((parsed.hostname, parsed.port or 80))
        self._id = 0
        key = b64encode(random.randbytes(16) if hasattr(random, "randbytes") else bytes(random.getrandbits(8) for _ in range(16))).decode()
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        if b"101" not in response.split(b"\r\n", 1)[0]:
            first_line = response.split(b"\r\n", 1)[0]
            raise RuntimeError(f"CDP handshake failed: {first_line!r}")

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: int = 12) -> dict[str, Any] | None:
        self._id += 1
        payload = json.dumps({"id": self._id, "method": method, "params": params or {}}, ensure_ascii=False)
        self.sock.sendall(self._frame(payload))
        return self._recv(timeout=timeout)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _frame(self, text: str) -> bytes:
        data = text.encode()
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = random.randbytes(4) if hasattr(random, "randbytes") else bytes(random.getrandbits(8) for _ in range(4))
        masked = bytearray(byte ^ mask[index % 4] for index, byte in enumerate(data))
        return bytes(header) + mask + masked

    def _recv(self, timeout: int = 12) -> dict[str, Any] | None:
        self.sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                header = self.sock.recv(2)
                if len(header) < 2:
                    return None
                opcode = header[0] & 0x0F
                length = header[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.sock.recv(8))[0]
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(min(length - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
                if opcode == 0x08:
                    return None
                if opcode != 0x01:
                    continue
                message = json.loads(data.decode())
                if message.get("id") == self._id:
                    return message
            except socket.timeout:
                return None
        return None


def fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


def is_local_network_permission_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, PermissionError):
        return True
    text = str(exc)
    return "Operation not permitted" in text or "EPERM" in text


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


def cleanup_cdp_profile_locks() -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = CDP_PROFILE_DIR / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def cdp_available(port: int, timeout: float = 2) -> tuple[bool, BaseException | None]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            response.read()
        return True, None
    except BaseException as exc:
        return False, exc


def start_cdp_chrome(port: int) -> bool:
    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    uid = str(os.getuid())
    if CDP_LAUNCH_AGENT.exists():
        label = f"gui/{uid}/ai.hermes.chrome-cdp"
        run_quiet("launchctl", "enable", label)
        run_quiet("launchctl", "bootout", label)
        cleanup_cdp_profile_locks()
        run_quiet("launchctl", "bootstrap", f"gui/{uid}", str(CDP_LAUNCH_AGENT))
        for _ in range(16):
            ok, _ = cdp_available(port, timeout=1)
            if ok:
                return True
            time.sleep(0.5)

    cleanup_cdp_profile_locks()
    chrome_path = next((path for path in CHROME_PATHS if path.exists()), None)
    if not chrome_path:
        return False
    log_path = Path.home() / ".hermes" / "chrome_cdp_manual.log"
    try:
        log_file = log_path.open("ab")
        subprocess.Popen(
            [
                str(chrome_path),
                f"--remote-debugging-port={port}",
                f"--user-data-dir={CDP_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                LIEPIN_SEARCH_URL,
            ],
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        return False
    for _ in range(16):
        ok, _ = cdp_available(port, timeout=1)
        if ok:
            return True
        time.sleep(0.5)
    return False


def ensure_cdp_available(port: int) -> None:
    ok, error = cdp_available(port)
    if ok:
        return
    if error and is_local_network_permission_error(error):
        raise RuntimeError(
            "当前 Codex 执行环境不能访问本机 CDP 端口 "
            f"127.0.0.1:{port}（Operation not permitted）。"
            "Chrome/LaunchAgent 本身可能是正常的；请用“修复猎聘CDP Chrome.command”"
            "或一站式寻访工作站从 macOS 正常权限启动。"
        ) from error

    print("Chrome CDP 未连接，正在按 Hermes LaunchAgent 旧链路恢复...", file=sys.stderr)
    if start_cdp_chrome(port):
        return
    ok, error = cdp_available(port, timeout=5)
    if not ok:
        raise RuntimeError(f"Chrome CDP 未连接，自动恢复失败：{error}") from error


def get_or_create_liepin_tab(port: int, prefer_new: bool) -> str:
    tabs = fetch_json(f"http://127.0.0.1:{port}/json/list")
    if not prefer_new:
        for tab in tabs:
            if tab.get("type") == "page" and "h.liepin.com" in tab.get("url", ""):
                return str(tab["webSocketDebuggerUrl"])
    encoded = quote(LIEPIN_SEARCH_URL, safe="")
    try:
        new_tab = fetch_json(f"http://127.0.0.1:{port}/json/new?{encoded}")
    except urllib.error.HTTPError:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded}",
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            new_tab = json.loads(response.read().decode())
    return str(new_tab["webSocketDebuggerUrl"])


def create_cdp_tab(port: int, url: str) -> str:
    encoded = quote(url, safe="")
    try:
        tab = fetch_json(f"http://127.0.0.1:{port}/json/new?{encoded}")
    except urllib.error.HTTPError:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/new?{encoded}",
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            tab = json.loads(response.read().decode())
    return str(tab["webSocketDebuggerUrl"])


def close_cdp_tab(port: int, ws_url: str) -> None:
    target_id = urlparse(ws_url).path.rstrip("/").split("/")[-1]
    if not target_id:
        return
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/json/close/{quote(target_id, safe='')}",
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except Exception:
        pass


def merge_resume_detail(card: dict[str, Any], resume: dict[str, Any]) -> dict[str, Any]:
    full_text = str(resume.get("full_text") or "").strip()
    work_text = str(resume.get("work_text") or "").strip()
    project_text = str(resume.get("project_text") or "").strip()
    education_text = str(resume.get("education_text") or "").strip()
    missing = []
    if len(full_text) < 100:
        missing.append("完整履历")
    if len(work_text) < 20:
        missing.append("工作经历")
    if len(education_text) < 10:
        missing.append("教育经历")
    card.update(
        {
            "profile_text": full_text or str(card.get("raw_text") or "").strip(),
            "full_text": full_text,
            "work_text": work_text,
            "project_text": project_text,
            "education_text": education_text,
            "resume_url": str(resume.get("source_url") or card.get("resume_url") or "").strip(),
            "raw_text": full_text or str(card.get("raw_text") or "").strip(),
            "resume_capture_status": "complete" if not missing else "partial",
            "resume_capture_missing": missing,
            "resume_capture_error": "" if not missing else f"缺少：{'、'.join(missing)}",
            "resume_captured_at": str(resume.get("captured_at") or datetime.now().isoformat(timespec="seconds")),
        }
    )
    return card


def capture_resume_details(port: int, candidates: list[dict[str, Any]], limit: int) -> dict[str, int]:
    stats = {"requested": len(candidates), "attempted": min(len(candidates), max(0, limit)), "complete": 0, "partial": 0, "failed": 0}
    for card in candidates[: max(0, limit)]:
        url = clean_text(card.get("resume_url", ""))
        if not url:
            card.update({"resume_capture_status": "failed", "resume_capture_missing": ["来源链接"], "resume_capture_error": "搜索卡片未返回简历详情链接"})
            stats["failed"] += 1
            continue
        ws_url = ""
        detail_cdp: CDP | None = None
        try:
            ws_url = create_cdp_tab(port, url)
            detail_cdp = CDP(ws_url, timeout=20)
            deadline = time.time() + 18
            ready = False
            while time.time() < deadline:
                state = evaluate(
                    detail_cdp,
                    "({href:location.href,ready:document.readyState,text:(document.body?.innerText||'').length,login:location.href.includes('login')})",
                    timeout=8,
                ) or {}
                if state.get("login"):
                    raise RuntimeError("猎聘登录已过期")
                if state.get("ready") == "complete" and int(state.get("text") or 0) >= 200:
                    ready = True
                    break
                time.sleep(0.5)
            if not ready:
                raise RuntimeError("简历详情页未加载出可读内容")
            raw_resume = evaluate(detail_cdp, EXTRACT_RESUME_JS, timeout=25)
            resume = json.loads(str(raw_resume or "{}"))
            identity = {"name": card.get("name"), "company": card.get("current_company"), "title": card.get("current_title")}
            if not resume_matches_identity(identity, resume):
                raise RuntimeError("详情页身份与搜索卡片不一致")
            merge_resume_detail(card, resume)
            stats[str(card["resume_capture_status"])] += 1
        except Exception as exc:
            card.update({"resume_capture_status": "failed", "resume_capture_missing": ["完整履历"], "resume_capture_error": str(exc)[:300]})
            stats["failed"] += 1
        finally:
            if detail_cdp:
                detail_cdp.close()
            if ws_url:
                close_cdp_tab(port, ws_url)
    for card in candidates[max(0, limit) :]:
        card.update({"resume_capture_status": "failed", "resume_capture_missing": ["完整履历"], "resume_capture_error": "超过本轮详情抓取上限"})
        stats["failed"] += 1
    return stats


def value_from_eval(result: dict[str, Any] | None) -> Any:
    if not result:
        return None
    return result.get("result", {}).get("result", {}).get("value")


def evaluate(cdp: CDP, expression: str, timeout: int = 12) -> Any:
    result = cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        timeout=timeout,
    )
    return value_from_eval(result)


def navigate(cdp: CDP, url: str, wait_seconds: float = 3.5) -> None:
    cdp.send("Page.navigate", {"url": url}, timeout=10)
    time.sleep(wait_seconds)


def open_resume_links_in_cdp(port: int, candidates: list[dict[str, Any]], limit: int) -> list[str]:
    opened: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if len(opened) >= limit:
            break
        url = clean_text(item.get("resume_url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        create_cdp_tab(port, url)
        opened.append(url)
        time.sleep(0.6)
    return opened


SEARCH_JS = r"""
async (query) => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function setNativeValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", {bubbles:true}));
    el.dispatchEvent(new Event("change", {bubbles:true}));
  }
  let input = document.querySelector("#rc_select_1")
    || document.querySelector(".ant-select-selection-search-input")
    || document.querySelector("input[placeholder*='搜索']")
    || document.querySelector("input[type='text']");
  if (!input) return {ok:false, reason:"no_input", href: location.href, title: document.title};
  input.focus();
  setNativeValue(input, query);
  await sleep(250);
  let button = document.querySelector(".search-btn")
    || Array.from(document.querySelectorAll("button,a,div")).find(el => (el.innerText || "").trim() === "搜索");
  if (button) button.click();
  else input.dispatchEvent(new KeyboardEvent("keydown", {key:"Enter", code:"Enter", keyCode:13, bubbles:true}));
  await sleep(4200);
  return {
    ok: true,
    query,
    href: location.href,
    title: document.title,
    cards: document.querySelectorAll(".tlog-common-resume-card").length,
    totalText: document.querySelector("[data-nick=totalcnt]")?.textContent?.trim() || ""
  };
}
"""


EXTRACT_JS = r"""
() => {
  function lines(el) {
    return (el.innerText || el.textContent || "")
      .split(/\n+/)
      .map(x => x.trim())
      .filter(Boolean);
  }
  function parseWorkEdu(items) {
    const work = [];
    const education = [];
    for (let i = 0; i < items.length - 1; i++) {
      const desc = items[i];
      const dates = items[i + 1];
      if (!/(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2}|至今)/.test(dates)) continue;
      if (!desc.includes("·")) continue;
      if (desc.includes("统招") || desc.includes("非统招") || /(本科|硕士|博士|大专|中专\/中技)/.test(desc)) {
        const parts = desc.split("·").map(x => x.trim()).filter(Boolean);
        education.push({
          school: parts[0] || "",
          major: parts[1] || "",
          degree: parts[2] || "",
          type: parts[3] || "",
          dates
        });
      } else {
        const parts = desc.split("·").map(x => x.trim()).filter(Boolean);
        work.push({company: parts[0] || "", title: parts.slice(1).join("·") || "", dates});
      }
    }
    return {work, education};
  }
  function resumeIdentity(card) {
    for (const node of card.querySelectorAll('[data-tlg-ext]')) {
      const encoded = node.getAttribute('data-tlg-ext') || '';
      try {
        const payload = JSON.parse(decodeURIComponent(encoded));
        const resumeId = String(payload.res_id_encode || payload.resIdEncode || '').trim();
        if (resumeId) return resumeId;
      } catch (error) {}
    }
    for (const node of card.querySelectorAll('[data-tlg-scm]')) {
      const match = String(node.getAttribute('data-tlg-scm') || '').match(/(?:^|&)cid=([^&]+)/);
      if (match?.[1]) return decodeURIComponent(match[1]);
    }
    return '';
  }
  const cards = Array.from(document.querySelectorAll(".tlog-common-resume-card")).slice(0, 24);
  return cards.map((card, index) => {
    const visible = lines(card);
    const pairData = parseWorkEdu(visible);
    const fullText = visible.join(" ");
    const nameNode = card.querySelector(".new-resume-personal-name em")?.textContent?.trim()
      || visible.find(t => /^[\u4e00-\u9fa5A-Za-z]{1,4}\*{1,3}$/.test(t))
      || visible.find(t => t.includes("**")) || "";
    const detail = Array.from(card.querySelectorAll(".new-resume-personal-detail span")).map(x => (x.innerText || x.textContent || "").trim()).filter(Boolean);
    const expect = Array.from(card.querySelectorAll(".new-resume-personal-expect span")).map(x => (x.innerText || x.textContent || "").trim()).filter(Boolean);
    const skillText = Array.from(card.querySelectorAll(".new-resume-personal-skills span")).map(x => (x.innerText || x.textContent || "").trim()).filter(Boolean);
    const educationNode = detail.find(t => /^(博士|硕士|本科|大专|MBA|MBA\/EMBA|中专\/中技|高中及以下)$/.test(t)) || "";
    const ageNode = detail.find(t => /^\d+岁$/.test(t)) || "";
    const expNode = detail.find(t => /^工作\d+年/.test(t) || t === "--") || "";
    const cityNode = detail.find(t => !/^\d+岁$/.test(t) && !/^工作\d+年/.test(t) && !/^(博士|硕士|本科|大专|MBA|MBA\/EMBA|中专\/中技|高中及以下|--)$/.test(t)) || "";
    const firstWork = pairData.work[0] || {};
    const resumeId = resumeIdentity(card);
    return {
      index,
      name: nameNode.replace(/\*+/g, "**"),
      age: ageNode,
      experience: expNode.replace("工作", ""),
      education: educationNode,
      city: cityNode,
      expected_city: expect[0] || "",
      expected_title: expect.slice(1).join(" ") || "",
      current_company: firstWork.company || "",
      current_title: firstWork.title || "",
      skills: skillText,
      work: pairData.work,
      education_history: pairData.education,
      res_id_encode: resumeId,
      resume_url: resumeId ? `https://h.liepin.com/resume/showresumedetail/?showsearchfeedback=1&res_id_encode=${encodeURIComponent(resumeId)}` : '',
      nodes: visible.slice(0, 80),
      raw_text: fullText.slice(0, 2500)
    };
  });
}
"""


CAPTURE_LINKS_JS = r"""
async (limit) => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const cards = Array.from(document.querySelectorAll(".tlog-common-resume-card")).slice(0, limit);
  const captured = [];
  const originalOpen = window.open;
  function directUrl(card) {
    const anchor = card.querySelector('a[href*="resume"],a[href*="res_id_encode"],a[href*="showresumedetail"]');
    if (anchor?.href) return anchor.href;
    for (const node of card.querySelectorAll('[data-tlg-ext]')) {
      try {
        const payload = JSON.parse(decodeURIComponent(node.getAttribute('data-tlg-ext') || ''));
        const resumeId = String(payload.res_id_encode || payload.resIdEncode || '').trim();
        if (resumeId) return `https://h.liepin.com/resume/showresumedetail/?showsearchfeedback=1&res_id_encode=${encodeURIComponent(resumeId)}`;
      } catch (error) {}
    }
    return '';
  }
  try {
    for (let i = 0; i < cards.length; i++) {
      let current = directUrl(cards[i]);
      window.open = function(url) {
        current = url || current;
        return null;
      };
      const target = cards[i].querySelector("[data-tlg-ext]") || cards[i].querySelector(".new-resume-personal-name") || cards[i];
      if (!current) try {
        target.click();
        await sleep(350);
      } catch (err) {}
      captured.push(current || '');
    }
  } finally {
    window.open = originalOpen;
  }
  return captured.map(url => !url ? "" : (String(url).startsWith("http") ? String(url) : "https://h.liepin.com" + String(url)));
}
"""


def parse_total_count(text: str) -> int:
    match = re.search(r"[\d,]+", text or "")
    if not match:
        return 0
    return int(match.group(0).replace(",", ""))


def normalize_name(name: str) -> str:
    return " ".join((name or "").split()) or "未识别"


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def build_position_profile(position: str) -> PositionProfile:
    position_text = clean_text(position)
    position_lower = position_text.lower()
    if "pqe" in position_lower or "质量" in position_text or "品质" in position_text:
        return PositionProfile(
            slug="pqe",
            headline="PQE专家",
            default_city="深圳/苏州",
            default_salary="面议",
            report_title="鹏新旭PQE专家猎聘寻访推荐",
            file_prefix="鹏新旭_PQE专家_猎聘寻访推荐",
            search_rounds=[
                SearchRound("R1 12吋fab痛点", "12吋 fab loading SPC", {"city": "深圳/苏州优先", "function": "12吋fab/loading/SPC"}),
                SearchRound("R1 300mm产线", "300mm fab SPC", {"city": "深圳/苏州优先", "function": "300mm fab/SPC"}),
                SearchRound("R1 晶圆产线", "晶圆产线 SPC PQE", {"city": "深圳/苏州优先", "function": "晶圆产线/SPC/PQE"}),
                SearchRound("R1 12吋PQE", "12吋 PQE SPC", {"city": "深圳/苏州优先", "function": "12吋/PQE/SPC"}),
                SearchRound("R1 loading痛点", "loading SPC", {"city": "深圳/苏州优先", "function": "loading问题+SPC"}),
                SearchRound("R1 loading质量", "loading PQE", {"city": "深圳/苏州优先", "function": "loading问题+PQE"}),
                SearchRound("R2 华力标杆", "华力 SPC loading", {"company": "华力", "function": "华力/SPC/loading"}),
                SearchRound("R2 12吋工具型", "12吋 SPC loading", {"city": "深圳/苏州优先", "function": "12吋晶圆/SPC/loading"}),
                SearchRound("R2 12吋良率", "12吋 良率 SPC", {"city": "深圳/苏州优先", "function": "12吋/良率/SPC"}),
                SearchRound("R2 良率制程", "SPC 良率 制程质量", {"city": "深圳/苏州优先", "function": "SPC/良率/制程异常"}),
                SearchRound("R2 过程能力", "SPC CPK Minitab", {"city": "深圳/苏州优先", "function": "SPC/CPK/Minitab"}),
                SearchRound("R2 CQE工具型", "CQE SPC", {"city": "深圳/苏州优先", "function": "CQE+SPC"}),
                SearchRound("R3 PQE辅助", "PQE 半导体", {"city": "深圳/苏州优先", "function": "PQE/产品质量"}),
                SearchRound("R3 MSA加分", "MSA GRR Minitab", {"city": "深圳/苏州优先", "function": "MSA/GRR加分"}),
                SearchRound("R3 客户质量辅助", "MRB PA 客户审核 PQE", {"city": "深圳/苏州优先", "function": "MRB/PA/客户审核"}),
                SearchRound("R3 公司定向", "长鑫 PQE", {"company": "长鑫"}),
                SearchRound("R3 公司定向", "中芯 PQE", {"company": "中芯"}),
                SearchRound("R3 公司定向", "华虹 质量 PQE", {"company": "华虹"}),
                SearchRound("R3 公司定向", "华力 PQE CQE", {"company": "华力"}),
                SearchRound("R3 公司定向", "SK海力士 QRA", {"company": "SK海力士"}),
                SearchRound("R3 公司定向", "华天 封装 PQE", {"company": "华天科技"}),
            ],
            target_companies=[
                "长鑫", "长江存储", "中芯", "SMIC", "华虹", "华力", "晶合集成", "粤芯",
                "士兰", "芯联集成", "积塔", "闻泰", "安世", "SK海力士", "Hynix", "三星",
                "台积电", "TSMC", "联电", "UMC", "华天科技", "通富微电", "长电科技",
                "长飞先进", "荣芯半导体", "源杰", "芯恩", "芯粤能", "燕东微",
                "中微", "北方华创", "拓荆", "微导纳米", "盛美", "华海清科", "芯源微",
                "东山精密", "瑞仪光电",
            ],
            core_keywords=[
                "12吋", "12寸", "12英寸", "12-inch", "12 inch", "300mm", "300 mm", "12吋线", "12寸线",
                "晶圆厂", "Fab", "wafer fab", "前道", "晶圆制造", "晶圆产线", "wafer line", "半导体产线",
                "loading", "Loading", "loading问题", "wafer loading", "loading effect", "负载", "装载", "上料", "载片", "片盒", "微负载", "负载效应",
                "SPC", "统计过程控制", "控制图", "过程能力", "CPK", "PPK", "Minitab",
                "PQE", "CQE", "Product Quality", "Customer Quality", "产品质量", "制程质量", "过程质量", "客户质量",
                "客诉", "客户审核", "外审", "8D", "FA", "失效分析", "异常处理", "质量改善", "质量闭环",
                "MRB", "PA改善", "PCCB", "CCR", "良率", "Yield", "line yield", "报废", "质量成本",
                "QRA", "QRE", "可靠性", "可靠性验证", "量产质量", "新产品上量",
                "NPI质量", "项目质量", "供应商质量", "SQE", "QE", "品质工程师",
            ],
            tool_keywords=[
                "SPC", "MSA", "GRR", "Gage R&R", "Gauge R&R", "Minitab", "JMP", "控制图", "过程能力",
                "FMEA", "PFMEA", "CPK", "DOE", "8D报告", "5Why", "鱼骨图",
                "QMS", "ISO9001", "IATF16949", "APQP", "PPAP", "QC七大手法", "Minitab",
                "JMP", "SEM", "EDS", "CP", "FT", "WAT", "RMA", "MRB", "CAR", "PCCB", "CCR",
            ],
            title_keywords=[
                "PQE专家", "PQE主管", "PQE经理", "质量主管", "质量经理", "品质主管",
                "品质经理", "CQE", "QRA", "QRE", "QE主管", "主任工程师", "资深", "高级",
                "专家", "负责人", "Leader", "Lead", "Staff",
            ],
            noise_keywords=[
                "生产操作员", "质检员", "检验员", "IPQC", "OQC", "IQC", "仓库", "采购",
                "销售", "客服", "售后", "软件开发", "算法工程师", "纯设备维护", "机械设计",
            ],
            default_noise_note="已按12吋fab产线硬主线过滤，loading问题+SPC为核心方法，MSA/GRR作为加分项",
            outreach_summary=(
                "鹏新旭这边有PQE专家机会，核心是12吋fab产线里解决loading问题，主方向看SPC质量工具能力，"
                "最好有12吋/300mm晶圆产线、统计过程控制、控制图/过程能力分析、Minitab经验；MSA/GRR是加分项。"
            ),
        )
    if "fpga" in position_lower:
        return PositionProfile(
            slug="fpga",
            headline="FPGA技术主管",
            default_city="苏州",
            default_salary="面议",
            report_title="苏科思FPGA技术主管猎聘寻访推荐",
            file_prefix="苏科思_FPGA技术主管_猎聘寻访推荐",
            search_rounds=[
                SearchRound("R1 FPGA架构", "FPGA 架构 运动控制", {"city": "苏州优先/长三角", "function": "FPGA架构/负责人"}),
                SearchRound("R1 时序验证", "FPGA 时序 CDC", {"city": "苏州优先/长三角", "function": "时序约束/CDC"}),
                SearchRound("R1 伺服控制", "FPGA 伺服 驱动器", {"city": "苏州优先/长三角", "function": "伺服/驱动器FPGA"}),
                SearchRound("R2 关键模块", "PWM 采样同步 编码器 FPGA", {"city": "苏州优先/长三角", "function": "PWM/采样/编码器"}),
                SearchRound("R2 平台化", "可复用IP FPGA 负责人", {"city": "苏州优先/长三角", "function": "平台化/IP复用"}),
                SearchRound("R3 公司定向", "汇川 FPGA", {"company": "汇川"}),
                SearchRound("R3 公司定向", "台达 FPGA 伺服", {"company": "台达"}),
                SearchRound("R3 公司定向", "固高 FPGA 运动控制", {"company": "固高"}),
                SearchRound("R3 公司定向", "华卓精科 FPGA", {"company": "华卓精科"}),
                SearchRound("R3 公司定向", "上海微电子 FPGA", {"company": "上海微电子"}),
            ],
            target_companies=[
                "汇川", "禾川", "雷赛", "固高", "埃斯顿", "台达", "Delta", "西门子", "Siemens",
                "博世力士乐", "Bosch Rexroth", "倍福", "Beckhoff", "安川", "松下", "三菱电机",
                "ASML", "阿斯麦", "华卓精科", "上海微电子", "SMEE", "隐冠", "KLA", "科磊",
                "Applied Materials", "AMAT", "应用材料", "Lam", "泛林", "中微", "北方华创",
                "中科飞测", "华海清科", "拓荆", "微导纳米", "盛美", "芯源微", "新凯来",
            ],
            core_keywords=[
                "FPGA", "SoC FPGA", "RTL", "Verilog", "SystemVerilog", "VHDL", "逻辑架构",
                "模块划分", "可复用IP", "接口规范", "PWM", "采样同步", "编码器接口",
                "总线通信", "EtherCAT", "故障保护", "保护逻辑", "数据通路", "控制时序",
                "时钟规划", "时序约束", "CDC", "综合实现", "时序收敛",
            ],
            tool_keywords=[
                "Vivado", "Quartus", "Xilinx", "Altera", "Intel FPGA", "Modelsim", "仿真",
                "板级验证", "回归测试", "在线调试", "示波器", "逻辑分析仪", "bring-up",
                "低延迟", "低抖动", "资源优化",
            ],
            title_keywords=[
                "FPGA主管", "FPGA经理", "FPGA负责人", "技术负责人", "项目负责人", "模块负责人",
                "主管", "经理", "负责人", "Team Leader", "Leader", "Lead", "主任", "专家",
                "高级", "资深", "Staff", "Principal",
            ],
            noise_keywords=["消费电子", "手机", "耳机", "家电", "纯软件", "算法工程师", "FAE", "技术支持", "测试工程师"],
            default_noise_note="已按FPGA逻辑架构/时序验证/运动控制或高端装备场景自动过滤",
            outreach_summary=(
                "苏科思苏州这边有FPGA技术主管机会，核心看FPGA/SoC FPGA逻辑架构、"
                "PWM/采样同步/编码器接口、时序约束、验证调试和平台化模块沉淀。"
            ),
        )

    if any(key in position_lower for key in ["硬件", "电控", "电气", "电路"]):
        return PositionProfile(
            slug="hardware_platform",
            headline="硬件技术主管",
            default_city="苏州",
            default_salary="面议",
            report_title="苏科思硬件技术主管猎聘寻访推荐",
            file_prefix="苏科思_硬件技术主管_猎聘寻访推荐",
            search_rounds=[
                SearchRound("R1 驱控硬件", "驱动器 硬件 架构", {"city": "苏州优先/长三角", "function": "驱动器硬件架构"}),
                SearchRound("R1 硬件平台", "硬件平台 运动控制", {"city": "苏州优先/长三角", "function": "硬件平台/控制器"}),
                SearchRound("R1 伺服控制器", "伺服控制器 硬件", {"city": "苏州优先/长三角", "function": "伺服控制器硬件"}),
                SearchRound("R2 关键接口", "编码器接口 采样电路 硬件", {"city": "苏州优先/长三角", "function": "采样/编码器接口"}),
                SearchRound("R2 产品化", "EMC 可靠性 硬件 量产", {"city": "苏州优先/长三角", "function": "EMC/可靠性/量产"}),
                SearchRound("R2 技术负责", "硬件技术负责人 工业控制", {"city": "苏州优先/长三角", "function": "硬件技术负责人"}),
                SearchRound("R3 公司定向", "汇川 硬件 架构", {"company": "汇川"}),
                SearchRound("R3 公司定向", "台达 伺服 硬件", {"company": "台达"}),
                SearchRound("R3 公司定向", "固高 运动控制 硬件", {"company": "固高"}),
                SearchRound("R3 公司定向", "华卓精科 硬件", {"company": "华卓精科"}),
                SearchRound("R3 公司定向", "上海微电子 硬件 平台", {"company": "上海微电子"}),
            ],
            target_companies=[
                "汇川", "禾川", "雷赛", "固高", "埃斯顿", "台达", "Delta", "西门子", "Siemens",
                "博世力士乐", "Bosch Rexroth", "倍福", "Beckhoff", "安川", "松下", "三菱电机",
                "ASML", "阿斯麦", "华卓精科", "上海微电子", "SMEE", "隐冠", "KLA", "科磊",
                "Applied Materials", "AMAT", "应用材料", "Lam", "泛林", "中微", "北方华创",
                "中科飞测", "华海清科", "拓荆", "微导纳米", "盛美", "芯源微", "新凯来",
            ],
            core_keywords=[
                "驱控", "驱动器", "控制器", "伺服控制器", "伺服驱动", "高性能驱动",
                "运动控制", "工业控制器", "硬件平台", "硬件架构", "总体架构", "需求分解",
                "平台化", "接口定义", "关键器件选型", "方案评审", "数字电路", "模拟电路",
                "电源完整性", "采样链路", "采样电路", "编码器接口", "隔离保护", "保护机制",
                "通信接口", "原理图", "PCB", "板卡", "EMC", "热设计", "可靠性", "DFM", "DFT",
                "认证测试", "生产导入", "量产导入",
            ],
            tool_keywords=[
                "Altium", "Cadence", "OrCAD", "PADS", "AD", "EtherCAT", "CAN", "RS485", "LVDS",
                "示波器", "频谱仪", "逻辑分析仪", "万用表", "电源分析仪", "波形分析",
                "bring-up", "样机调试", "板级调试", "边界工况", "根因定位", "整改闭环",
            ],
            title_keywords=[
                "主管", "经理", "负责人", "Team Leader", "Leader", "Lead", "主任", "专家",
                "高级", "资深", "Staff", "Principal", "硬件经理", "硬件主管", "硬件技术负责人",
            ],
            noise_keywords=[
                "消费电子", "手机", "耳机", "家电", "电池包", "BMS", "充电器", "电源适配器",
                "车载娱乐", "ADAS", "纯FPGA", "FPGA工程师", "RTL", "Verilog", "VHDL", "FAE", "技术支持",
            ],
            default_noise_note="已按驱控硬件平台/控制器驱动器架构/EMC可靠性/量产导入信号自动过滤",
            outreach_summary=(
                "苏科思苏州这边有硬件技术主管机会，核心看驱控系统硬件平台、控制器/驱动器架构、"
                "采样/编码器/隔离保护等关键方案，以及样机调试、EMC可靠性和量产导入闭环。"
            ),
        )

    return PositionProfile(
        slug="mechanical",
        headline="资深机械工程师",
        default_city="苏州",
        default_salary="60万",
        report_title="苏科思资深机械工程师猎聘寻访推荐",
        file_prefix="苏科思_资深机械工程师_猎聘寻访推荐",
        search_rounds=[
            SearchRound("R1 核心技术", "精密机械 设计 半导体", {"city": "苏州优先/长三角", "salary": "60万"}),
            SearchRound("R1 运动台", "运动台 机械设计", {"city": "苏州优先/长三角", "salary": "60万"}),
            SearchRound("R1 精密运动", "精密运动 机械 半导体", {"city": "苏州优先/长三角", "salary": "60万"}),
            SearchRound("R2 仿真能力", "Ansys 机械 半导体", {"city": "苏州优先/长三角", "salary": "60万"}),
            SearchRound("R2 光机结构", "光机 结构设计 精密", {"city": "苏州优先/长三角", "salary": "60万"}),
            SearchRound("R3 公司定向", "华卓精科 机械 运动台", {"company": "华卓精科"}),
            SearchRound("R3 公司定向", "上海微电子 机械设计", {"company": "上海微电子"}),
            SearchRound("R3 公司定向", "隐冠半导体 机械", {"company": "隐冠半导体"}),
            SearchRound("R3 公司定向", "KLA 机械 半导体", {"company": "KLA"}),
            SearchRound("R3 公司定向", "应用材料 机械 半导体", {"company": "应用材料"}),
        ],
        target_companies=[
            "ASML", "阿斯麦", "华卓精科", "上海微电子", "SMEE", "隐冠", "Aerotech", "PI",
            "Physik", "ETEL", "Newport", "KLA", "科磊", "Applied Materials", "AMAT",
            "应用材料", "Lam", "泛林", "睿励", "中微", "北方华创", "中科飞测",
            "华海清科", "拓荆", "微导纳米", "迈为", "汇川", "埃斯顿", "Akribis",
            "联影", "蔡司", "Zeiss", "Thermo", "Agilent", "博世", "Bosch",
        ],
        core_keywords=["精密机械", "精密运动", "运动台", "定位平台", "纳米定位", "光机", "半导体设备", "光刻", "量测", "检测设备"],
        tool_keywords=["Ansys", "SolidWorks", "CAD", "有限元", "模态", "热分析", "振动", "公差分析", "GD&T", "电机", "丝杆", "导轨", "编码器", "气浮"],
        title_keywords=["资深", "高级", "主任", "专家", "主管", "Lead", "Senior", "Staff"],
        noise_keywords=["消费电子", "夹具", "钣金", "包装", "体育", "运动品牌"],
        default_noise_note="已按精密机械/半导体/运动台/目标公司信号自动过滤",
        outreach_summary=(
            "苏科思苏州这边有资深机械工程师机会，预算约60万，核心是微米/纳米级精密机械、"
            "运动/定位平台、仿真和机电选型。"
        ),
    )


def build_db_position_profile(db_path: str | Path, client: str, position: str) -> PositionProfile:
    """Build search behavior from the v3 position profile, with legacy profiles only as fallback."""
    base = build_position_profile(position)
    conn = sqlite3.connect(str(Path(db_path).expanduser()))
    conn.row_factory = sqlite3.Row
    try:
        profile = conn.execute(
            "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY id DESC LIMIT 1",
            (client, position),
        ).fetchone()
        position_row = conn.execute(
            "SELECT * FROM positions WHERE client=? AND title=? ORDER BY id DESC LIMIT 1",
            (client, position),
        ).fetchone()
    except sqlite3.OperationalError:
        return base
    finally:
        conn.close()
    if profile is None:
        return base

    def values(column: str) -> list[str]:
        try:
            raw = profile[column]
        except (IndexError, KeyError):
            return []
        if not raw:
            return []
        try:
            parsed = json.loads(str(raw))
        except json.JSONDecodeError:
            parsed = [item.strip() for item in re.split(r"[；;，,\n]", str(raw)) if item.strip()]
        return [clean_text(str(item)) for item in parsed if clean_text(str(item))] if isinstance(parsed, list) else []

    queries = values("search_keywords_json") or values("ability_keywords_json")
    if not queries:
        queries = [position]
    rounds = [SearchRound(f"画像词 {index}", query, {"source": "position_profiles"}) for index, query in enumerate(queries[:16], 1)]
    ability = values("ability_keywords_json")
    target = values("target_companies_json")
    exclusions = values("exclusion_tags_json")
    city = clean_text(position_row["location"] if position_row is not None and "location" in position_row.keys() else "") or base.default_city
    salary = clean_text(position_row["salary"] if position_row is not None and "salary" in position_row.keys() else "") or base.default_salary
    return PositionProfile(
        slug=f"v3-{_slug_for_profile(position)}",
        headline=position,
        default_city=city,
        default_salary=salary,
        report_title=f"{client}{position}猎聘寻访结果",
        file_prefix=f"{client}_{position}_猎聘寻访",
        search_rounds=rounds,
        target_companies=target,
        core_keywords=ability or [position],
        tool_keywords=[],
        title_keywords=[position, *[item for item in ability if any(token in item for token in ("工程师", "经理", "总监", "专家", "主管"))]],
        noise_keywords=exclusions,
        default_noise_note="按 v3 岗位画像能力词、目标公司和排除项过滤",
        outreach_summary=f"{client}的{position}机会，具体职责与条件以本次岗位画像为准。",
    )


def _slug_for_profile(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value).strip("-")[:40] or "job"


def keyword_hits(text: str, keywords: list[str]) -> list[str]:
    lower = text.lower()
    return [item for item in keywords if item and item.lower() in lower]


def score_candidate(candidate: dict[str, Any], target_city: str) -> tuple[int, list[str], list[str], str]:
    return score_candidate_for_profile(candidate, target_city, build_position_profile("资深机械工程师"))


def score_candidate_for_profile(
    candidate: dict[str, Any],
    target_city: str,
    profile: PositionProfile,
) -> tuple[int, list[str], list[str], str]:
    text = clean_text(candidate.get("raw_text", ""))
    work_text = " ".join(
        clean_text(f"{item.get('company', '')} {item.get('title', '')}") for item in candidate.get("work", [])
    )
    all_text = f"{text} {work_text}"
    score = 30
    evidence: list[str] = []
    risks: list[str] = []

    company_hits = keyword_hits(all_text, profile.target_companies)
    if company_hits:
        score += min(28, 12 + 4 * len(company_hits))
        evidence.append(f"目标公司/相近公司：{', '.join(company_hits[:4])}")

    core_hits = keyword_hits(all_text, profile.core_keywords)
    if core_hits:
        score += min(30, 8 + 3 * len(core_hits))
        evidence.append(f"{profile.headline}核心关键词：{', '.join(core_hits[:6])}")

    tool_hits = keyword_hits(all_text, profile.tool_keywords)
    if tool_hits:
        score += min(18, 4 + 2 * len(tool_hits))
        evidence.append(f"工具/技术关键词：{', '.join(tool_hits[:6])}")

    title = clean_text(candidate.get("current_title", ""))
    seniority_hit = False
    if any(word.lower() in title.lower() for word in profile.title_keywords):
        seniority_hit = True
        score += 10
        evidence.append(f"资深度匹配：{title}")
    elif any(word in title for word in ["机械", "硬件", "电气", "电控", "FPGA"]):
        score += 5
        evidence.append(f"岗位方向：{title}")

    education = clean_text(candidate.get("education", ""))
    if education in {"博士", "硕士"}:
        score += 7
        evidence.append(f"学历较优：{education}")
    elif education == "本科":
        score += 4
        evidence.append("学历满足本科")
    elif education:
        risks.append(f"学历需复核：{education}")

    city_text = f"{candidate.get('city', '')} {candidate.get('expected_city', '')}"
    target_city_parts = [part for part in re.split(r"[/、,，\s]+", target_city or "") if part]
    if target_city_parts and any(part in city_text for part in target_city_parts):
        score += 8
        evidence.append(f"地点相关：{city_text.strip()}")
    elif any(city in city_text for city in ["上海", "无锡", "常州", "南京", "杭州"]):
        score += 4
        evidence.append(f"长三角可沟通：{city_text.strip()}")
    elif city_text.strip():
        risks.append(f"地点需确认：{city_text.strip()}")

    if keyword_hits(all_text, profile.noise_keywords):
        score -= 16
        if profile.slug == "hardware_platform":
            risks.append("可能偏纯FPGA/消费电子/支持类方向，需确认是否真正负责驱控硬件平台、原理设计和量产导入")
        elif profile.slug == "fpga":
            risks.append("可能偏非FPGA逻辑架构/验证调试主线，需要谨慎确认")
        else:
            risks.append("可能偏非核心设备或消费类硬件，需要谨慎确认")
    if not core_hits and not company_hits:
        score -= 12
        risks.append(f"卡片摘要未体现{profile.headline}强信号")
    if profile.slug == "hardware_platform":
        fpga_only = re.search(r"FPGA工程师|资深FPGA|RTL|Verilog|VHDL|时序约束|CDC|时序收敛", all_text, re.I)
        hardware_platform_hit = re.search(
            r"驱控|驱动器|控制器|伺服|运动控制|工业控制器|硬件平台|硬件架构|原理图|PCB|采样|编码器|隔离保护|EMC|可靠性|生产导入|量产导入",
            all_text,
            re.I,
        )
        if fpga_only and not hardware_platform_hit:
            score = min(score, 58)
            risks.append("偏FPGA逻辑方向，先转入FPGA技术主管池，不作为硬件技术主管优先触达")
    if profile.slug == "pqe":
        if re.search(r"鹏新旭|深圳市鹏新旭|鹏芯旭", all_text):
            score = min(score, 60)
            risks.append("当前或近期在鹏新旭：只作为标杆履历学习，不建议触达")
        fab_line_hit = re.search(
            r"12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|晶圆厂|Fab|wafer\s*fab|前道|晶圆制造|晶圆产线|wafer\s*line|半导体产线|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix",
            all_text,
            re.I,
        )
        pqe_hit = re.search(r"PQE|CQE|产品质量|制程质量|客户质量|客诉|8D|FA|失效分析|QRA|QRE|可靠性|良率|Yield|QE|品质", all_text, re.I)
        spc_loading_hit = re.search(r"SPC|统计过程控制|控制图|过程能力|CPK|Minitab|loading|Loading|负载|装载|上料|载片|片盒|微负载|负载效应", all_text, re.I)
        if not pqe_hit:
            score = min(score, 55)
            risks.append("PQE/质量主线不够明确，需打开简历复核")
        if not fab_line_hit:
            score = min(score - 16, 72)
            risks.append("12吋fab产线背景不明确：只有封测、设备、消费电子或泛半导体质量经验不算核心匹配")
        if not spc_loading_hit:
            score = min(score, 74)
            risks.append("SPC或loading问题改善证据不够明确，需打开简历复核")
    if profile.slug == "hardware_platform" and re.search(r"硬件主管|硬件经理|硬件负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|设计规范|技术评审|评审机制|规范沉淀|方案复盘|团队带教|带教", all_text):
        seniority_hit = True
    if profile.slug == "fpga" and re.search(r"FPGA主管|FPGA经理|FPGA负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|代码评审|设计规范|仿真规范|规范沉淀|问题复盘|团队带教|带教", all_text, re.I):
        seniority_hit = True
    if profile.slug in {"hardware_platform", "fpga", "pqe"} and not seniority_hit:
        score = min(score, 78)
        if profile.slug == "pqe":
            risks.append("专家/主管层级待核实：卡片摘要未明显体现质量专项负责、客户接口或体系改善牵头")
        else:
            risks.append("主管/资深专家层级待核实：卡片摘要未明显体现技术负责、模块负责、团队带教或规范评审")

    score = max(0, min(100, score))
    if score >= 78:
        level = "A-优先推荐"
    elif score >= 65:
        level = "B-可沟通"
    elif score >= 52:
        level = "C-需复核"
    else:
        level = "D-暂缓"
    if not evidence:
        evidence.append("需打开完整简历复核")
    return score, evidence, risks, level


def salutation(name: str, title: str) -> str:
    clean_name = normalize_name(name).replace("**", "")
    surname = clean_name[:1] if clean_name and clean_name != "未识别" else ""
    if not surname:
        return "您好"
    title_lower = (title or "").lower()
    if any(word in title_lower for word in ["经理", "总监", "总经理", "副总", "vp", "director", "head", "负责人"]):
        return f"{surname}总，您好"
    if any(word in title_lower for word in ["工程师", "专家", "主任", "主管", "机械", "设计", "研发", "engineer"]):
        return f"{surname}工，您好"
    return f"{surname}老师，您好"


def outreach_draft(candidate: dict[str, Any], score: int, evidence: list[str]) -> str:
    return outreach_draft_for_profile(candidate, score, evidence, build_position_profile("资深机械工程师"))


def outreach_draft_for_profile(
    candidate: dict[str, Any],
    score: int,
    evidence: list[str],
    profile: PositionProfile,
) -> str:
    title = clean_text(candidate.get("current_title", ""))
    prefix = salutation(candidate.get("name", ""), title)
    anchor = talk_anchor_for_profile(candidate, evidence, profile)
    if anchor:
        anchor = f"看到您{anchor}，"
    elif title:
        anchor = f"看到您目前做{title}方向，"
    location_phrase = "深圳/苏州" if profile.slug == "pqe" else "苏州或长三角"
    return f"{prefix}，{anchor}{profile.outreach_summary}想先和您确认下：您现在是否还看{location_phrase}的机会？"


def talk_anchor(candidate: dict[str, Any], evidence: list[str]) -> str:
    return talk_anchor_for_profile(candidate, evidence, build_position_profile("资深机械工程师"))


def talk_anchor_for_profile(candidate: dict[str, Any], evidence: list[str], profile: PositionProfile) -> str:
    text = " ".join([candidate.get("raw_text", ""), " ".join(evidence)])
    anchors: list[str] = []
    if profile.slug == "hardware_platform":
        if any(word in text for word in ["半导体设备", "光刻", "量测", "检测设备", "晶圆"]):
            anchors.append("有半导体设备硬件相关经验")
        if any(word in text for word in ["硬件平台", "硬件架构", "驱动器", "控制器", "伺服", "运动控制"]):
            anchors.append("有驱控硬件平台或控制器/驱动器背景")
        if any(word in text for word in ["采样", "编码器接口", "隔离保护", "EMC", "可靠性", "量产导入", "bring-up", "样机调试"]):
            anchors.append("涉及关键硬件方案、调试验证或量产闭环")
        if any(word in text for word in ["主管", "经理", "负责人", "Team Leader", "Leader", "带队"]):
            anchors.append("有团队或模块负责经验")
    elif profile.slug == "fpga":
        if any(word in text for word in ["FPGA", "SoC FPGA", "RTL", "Verilog", "VHDL"]):
            anchors.append("有FPGA/RTL开发背景")
        if any(word in text for word in ["时序约束", "CDC", "时序收敛", "仿真", "板级验证", "在线调试"]):
            anchors.append("涉及时序验证、板级调试或问题闭环")
        if any(word in text for word in ["PWM", "采样同步", "编码器接口", "EtherCAT", "数据通路", "保护逻辑"]):
            anchors.append("命中控制时序、接口或保护逻辑模块")
        if any(word in text for word in ["主管", "经理", "负责人", "Team Leader", "Leader", "带队"]):
            anchors.append("有团队或模块负责经验")
    elif profile.slug == "pqe":
        if re.search(r"12吋|12寸|12英寸|300\s*mm|300mm|晶圆厂|Fab|wafer\s*fab|前道|晶圆制造|晶圆产线|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix", text, re.I):
            anchors.append("有12吋fab或晶圆产线质量相关背景")
        if re.search(r"loading|Loading|负载|装载|上料|载片|片盒|微负载|负载效应", text, re.I):
            anchors.append("涉及loading问题或产线异常改善")
        if re.search(r"SPC|统计过程控制|控制图|过程能力|CPK|Minitab", text, re.I):
            anchors.append("有SPC/过程能力分析经验")
        if re.search(r"PQE|产品质量|制程质量|客户质量|品质|QE", text, re.I):
            anchors.append("有PQE/产品质量相关背景")
        if any(word in text for word in ["客诉", "8D", "FA", "失效分析", "异常处理", "质量闭环"]):
            anchors.append("涉及客诉、8D或失效分析闭环")
        if any(word in text for word in ["良率", "Yield", "制程", "晶圆"]):
            anchors.append("有晶圆产线良率或制程质量场景")
        if any(word in text for word in ["可靠性", "QRA", "QRE", "量产质量", "NPI"]):
            anchors.append("有可靠性或量产质量经验")
    elif any(word in text for word in ["超精密", "精密机械", "精密运动", "运动台", "定位平台", "微米级", "纳米"]):
        anchors.append("有精密机械/运动定位相关背景")
    if profile.slug not in {"hardware_platform", "fpga"} and any(word in text for word in ["半导体设备", "光刻", "量测", "检测设备", "晶圆"]):
        anchors.append("有半导体设备相关经验")
    if profile.slug not in {"hardware_platform", "fpga"} and any(word in text for word in ["光机", "光学机械", "光机设计"]):
        anchors.append("做过光机结构方向")
    if profile.slug not in {"hardware_platform", "fpga"} and any(word in text for word in ["Ansys", "有限元", "模态", "振动", "热分析", "公差分析", "GD&T"]):
        anchors.append("有仿真/公差分析经验")
    if not anchors and candidate.get("current_title"):
        anchors.append(f"目前是{candidate['current_title']}")
    return "，".join(dict.fromkeys(anchors[:2]))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_intelligence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER,
            candidate_name TEXT NOT NULL,
            candidate_company TEXT,
            client TEXT,
            position TEXT,
            fit_score INTEGER DEFAULT 0,
            fit_level TEXT DEFAULT 'unrated',
            evidence_json TEXT DEFAULT '{}',
            risk_json TEXT DEFAULT '{}',
            next_action TEXT,
            last_evaluated_at TEXT,
            model_version TEXT DEFAULT 'rules-v0',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(candidate_name, candidate_company, client, position)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT,
            position TEXT,
            channel TEXT DEFAULT 'liepin',
            round_name TEXT,
            query TEXT NOT NULL,
            filters_json TEXT DEFAULT '{}',
            result_count INTEGER,
            viewed_count INTEGER,
            extracted_count INTEGER,
            recommended_count INTEGER DEFAULT 0,
            reply_count INTEGER DEFAULT 0,
            positive_reply_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open',
            source_url TEXT,
            noise_notes TEXT,
            run_time TEXT DEFAULT (datetime('now','localtime')),
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_experiments_client_position ON search_experiments(client, position)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ci_client_position ON candidate_intelligence(client, position)"
    )
    conn.commit()


def next_iteration(conn: sqlite3.Connection, client: str, position: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(iteration), 0) FROM candidates WHERE client=? AND position=?",
        (client, position),
    ).fetchone()
    return int(row[0] or 0) + 1


def upsert_candidate(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    client: str,
    position: str,
    iteration: int,
    source_url: str,
    note: str,
) -> int:
    name = normalize_name(candidate.get("name", ""))
    company = clean_text(candidate.get("current_company", ""))
    title = clean_text(candidate.get("current_title", ""))
    skills = clean_text(candidate.get("raw_text", ""))[:900]
    city = clean_text(candidate.get("city", ""))
    conn.execute(
        """
        INSERT OR IGNORE INTO candidates
            (name, company, title, education, experience, skills, city, client, position,
             search_date, status, notes, iteration, source, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, date('now'), 'new', ?, ?, 'liepin',
                datetime('now','localtime'), datetime('now','localtime'))
        """,
        (
            name,
            company,
            title,
            clean_text(candidate.get("education", "")),
            clean_text(candidate.get("experience", "")),
            skills,
            city,
            client,
            position,
            note,
            iteration,
        ),
    )
    conn.execute(
        """
        UPDATE candidates
        SET title=COALESCE(NULLIF(?, ''), title),
            education=COALESCE(NULLIF(?, ''), education),
            experience=COALESCE(NULLIF(?, ''), experience),
            skills=COALESCE(NULLIF(?, ''), skills),
            city=COALESCE(NULLIF(?, ''), city),
            notes=COALESCE(NULLIF(?, ''), notes),
            updated_at=datetime('now','localtime')
        WHERE name=? AND ifnull(company,'')=ifnull(?, '') AND client=? AND position=?
        """,
        (
            title,
            clean_text(candidate.get("education", "")),
            clean_text(candidate.get("experience", "")),
            skills,
            city,
            note,
            name,
            company,
            client,
            position,
        ),
    )
    row = conn.execute(
        """
        SELECT id FROM candidates
        WHERE name=? AND ifnull(company,'')=ifnull(?, '') AND client=? AND position=?
        ORDER BY id DESC LIMIT 1
        """,
        (name, company, client, position),
    ).fetchone()
    if source_url:
        conn.execute(
            """
            INSERT INTO outreach_events (
                candidate_id, candidate_name, candidate_company, client, position,
                channel, event_type, event_status, message_summary, source_url, event_time
            )
            SELECT ?, ?, ?, ?, ?, 'liepin', 'resume_link_captured', 'ready',
                   '猎聘寻访抓取到简历链接，待人工打开复核后沟通', ?, datetime('now','localtime')
            WHERE NOT EXISTS (
                SELECT 1 FROM outreach_events
                WHERE candidate_name=? AND ifnull(candidate_company,'')=ifnull(?, '')
                  AND client=? AND position=? AND event_type='resume_link_captured'
            )
            """,
            (row["id"] if row else None, name, company, client, position, source_url, name, company, client, position),
        )
    return int(row["id"]) if row else 0


def upsert_intelligence(
    conn: sqlite3.Connection,
    candidate_id: int,
    candidate: dict[str, Any],
    client: str,
    position: str,
    score: int,
    level: str,
    evidence: list[str],
    risks: list[str],
    draft: str,
) -> None:
    name = normalize_name(candidate.get("name", ""))
    company = clean_text(candidate.get("current_company", ""))
    conn.execute(
        """
        INSERT INTO candidate_intelligence (
            candidate_id, candidate_name, candidate_company, client, position,
            fit_score, fit_level, evidence_json, risk_json, next_action,
            last_evaluated_at, model_version, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'), ?,
                datetime('now','localtime'), datetime('now','localtime'))
        ON CONFLICT(candidate_name, candidate_company, client, position) DO UPDATE SET
            candidate_id=excluded.candidate_id,
            fit_score=excluded.fit_score,
            fit_level=excluded.fit_level,
            evidence_json=excluded.evidence_json,
            risk_json=excluded.risk_json,
            next_action=excluded.next_action,
            last_evaluated_at=datetime('now','localtime'),
            model_version=excluded.model_version,
            updated_at=datetime('now','localtime')
        """,
        (
            candidate_id,
            name,
            company,
            client,
            position,
            score,
            level,
            json.dumps({"evidence": evidence, "draft": draft}, ensure_ascii=False),
            json.dumps({"risks": risks}, ensure_ascii=False),
            "打开完整简历复核；若信号成立，用推荐话术发起沟通",
            MODEL_VERSION,
        ),
    )


def record_round(conn: sqlite3.Connection, client: str, position: str, search_round: SearchRound, source_url: str) -> None:
    conn.execute(
        """
        INSERT INTO search_experiments (
            client, position, channel, round_name, query, filters_json,
            result_count, viewed_count, extracted_count, recommended_count,
            status, source_url, noise_notes, run_time, created_at, updated_at
        )
        VALUES (?, ?, 'liepin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                datetime('now','localtime'), datetime('now','localtime'), datetime('now','localtime'))
        """,
        (
            client,
            position,
            search_round.name,
            search_round.query,
            json.dumps(search_round.filters, ensure_ascii=False, sort_keys=True),
            search_round.result_count,
            search_round.extracted_count,
            search_round.extracted_count,
            search_round.recommended_count,
            "tracking" if search_round.recommended_count else "open",
            source_url,
            search_round.noise_notes,
        ),
    )


def apply_filters(cdp: CDP, filters: dict[str, Any]) -> None:
    # Keep filters intentionally light. Current Liepin selector labels drift often;
    # keyword precision does most of the narrowing here.
    if not filters:
        return
    time.sleep(0.8)


def build_rounds(limit_rounds: int, profile: PositionProfile) -> list[SearchRound]:
    return profile.search_rounds[:limit_rounds]


def load_query_rounds(path: str | None) -> list[SearchRound]:
    if not path:
        return []
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    values = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("自定义寻访关键词必须是数组")
    rounds: list[SearchRound] = []
    for index, value in enumerate(values):
        item = value if isinstance(value, dict) else {"query": value}
        query = clean_text(str(item.get("query") or ""))
        if not query:
            continue
        rounds.append(
            SearchRound(
                clean_text(str(item.get("round") or f"模型策略 {index + 1}")),
                query,
                {"source": "asa_llm_strategy", "purpose": clean_text(str(item.get("purpose") or ""))},
            )
        )
    return rounds


def run_search(args: argparse.Namespace) -> dict[str, Any]:
    if not args.ws:
        ensure_cdp_available(args.port)
    ws = args.ws or get_or_create_liepin_tab(args.port, prefer_new=args.new_tab)
    profile = build_db_position_profile(args.db, args.client, args.position)
    conn = sqlite3.connect(str(Path(args.db).expanduser()))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    cdp = CDP(ws)
    all_candidates: list[dict[str, Any]] = []
    iteration = next_iteration(conn, args.client, args.position)
    try:
        navigate(cdp, LIEPIN_SEARCH_URL, wait_seconds=3)
        href = str(evaluate(cdp, "location.href", timeout=8) or "")
        if "login" in href:
            raise SystemExit("猎聘登录已过期，请在 Chrome 里登录猎聘后再继续。")
        custom_rounds = load_query_rounds(args.queries_json)
        rounds = custom_rounds[: args.rounds] if custom_rounds else build_rounds(args.rounds, profile)
        seen_keys: set[tuple[str, str]] = set()
        for search_round in rounds:
            expression = f"({SEARCH_JS})({json.dumps(search_round.query, ensure_ascii=False)})"
            search_result = evaluate(cdp, expression, timeout=16) or {}
            time.sleep(random.uniform(args.min_delay, args.max_delay))
            total_text = clean_text(str(search_result.get("totalText") or ""))
            search_round.result_count = parse_total_count(total_text)
            apply_filters(cdp, search_round.filters)
            cards = evaluate(cdp, f"({EXTRACT_JS})()", timeout=12) or []
            if not isinstance(cards, list):
                cards = []
            if args.capture_links:
                links = evaluate(cdp, f"({CAPTURE_LINKS_JS})({min(args.max_cards, len(cards))})", timeout=18) or []
            else:
                links = []
            for index, card in enumerate(cards[: args.max_cards]):
                if not isinstance(card, dict):
                    continue
                card["source_query"] = search_round.query
                card["source_round"] = search_round.name
                card["resume_url"] = (links[index] if index < len(links) else "") or card.get("resume_url", "")
                score, evidence, risks, level = score_candidate_for_profile(card, args.city, profile)
                card["fit_score"] = score
                card["fit_level"] = level
                card["evidence"] = evidence
                card["risks"] = risks
                card["draft"] = outreach_draft_for_profile(card, score, evidence, profile)
                key = (normalize_name(card.get("name", "")), clean_text(card.get("current_company", "")))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if score < args.min_score:
                    continue
                note = (
                    f"{args.client}{args.position}寻访；轮次={search_round.name}；关键词={search_round.query}；"
                    f"评分={score}；级别={level}；简历链接={card.get('resume_url', '')}"
                )
                candidate_id = 0 if args.dry_run else upsert_candidate(
                    conn, card, args.client, args.position, iteration, card.get("resume_url", ""), note
                )
                if not args.dry_run:
                    upsert_intelligence(
                        conn,
                        candidate_id,
                        card,
                        args.client,
                        args.position,
                        score,
                        level,
                        card["evidence"],
                        card["risks"],
                        card["draft"],
                    )
                all_candidates.append(card)
            search_round.extracted_count = len(cards)
            search_round.recommended_count = sum(
                1 for item in all_candidates if item.get("source_query") == search_round.query and item.get("fit_score", 0) >= args.recommend_score
            )
            search_round.noise_notes = profile.default_noise_note
            if not args.dry_run:
                record_round(conn, args.client, args.position, search_round, str(search_result.get("href") or LIEPIN_SEARCH_URL))
                conn.commit()
            time.sleep(random.uniform(args.min_delay, args.max_delay))
    finally:
        cdp.close()
        conn.close()

    all_candidates.sort(key=lambda item: int(item.get("fit_score") or 0), reverse=True)
    detail_capture = (
        capture_resume_details(args.port, all_candidates, args.detail_limit)
        if args.capture_details
        else {"requested": 0, "complete": 0, "partial": 0, "failed": 0}
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "client": args.client,
        "position": args.position,
        "city": args.city,
        "salary": args.salary,
        "profile": profile,
        "iteration": iteration,
        "dry_run": args.dry_run,
        "candidates": all_candidates,
        "detail_capture": detail_capture,
        "rounds": [
            {
                "name": search_round.name,
                "query": search_round.query,
                "result_count": search_round.result_count,
                "extracted_count": search_round.extracted_count,
                "recommended_count": search_round.recommended_count,
            }
            for search_round in rounds
        ],
        "ws": ws,
    }


def escape_cell(text: Any) -> str:
    return clean_text(str(text or "")).replace("|", "｜")


def write_report(result: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    profile: PositionProfile = result["profile"]
    safe_prefix = _slug_for_profile(profile.file_prefix)
    path = output_dir / f"{safe_prefix}_{stamp}.md"
    candidates = result["candidates"]
    a_list = [item for item in candidates if int(item.get("fit_score") or 0) >= 78]
    b_list = [item for item in candidates if 65 <= int(item.get("fit_score") or 0) < 78]
    lines = [
        f"# {profile.report_title}",
        "",
        f"生成时间：{result['generated_at']}",
        f"客户：{result['client']}",
        f"岗位：{result['position']}",
        f"地点/预算：{result['city']} / {result['salary']}",
        f"模式：{'预览未入库' if result['dry_run'] else '已写入本地人才池与评分表'}",
        "",
        "## 总览",
        "",
        f"- 入围候选人：{len(candidates)}",
        f"- A级优先推荐：{len(a_list)}",
        f"- B级可沟通：{len(b_list)}",
        "",
        "## 推荐名单",
        "",
        "| 级别 | 分数 | 姓名 | 当前公司 | 当前职位 | 学历 | 年限 | 城市 | 匹配依据 | 风险/待确认 | 简历链接 |",
        "|---|---:|---|---|---|---|---|---|---|---|---|",
    ]
    for item in candidates[:20]:
        link = item.get("resume_url") or ""
        link_text = f"[打开]({link})" if link else "待打开搜索结果复核"
        lines.append(
            "| {level} | {score} | {name} | {company} | {title} | {edu} | {exp} | {city} | {evidence} | {risk} | {link} |".format(
                level=escape_cell(item.get("fit_level")),
                score=int(item.get("fit_score") or 0),
                name=escape_cell(item.get("name")),
                company=escape_cell(item.get("current_company")),
                title=escape_cell(item.get("current_title")),
                edu=escape_cell(item.get("education")),
                exp=escape_cell(item.get("experience")),
                city=escape_cell(item.get("city") or item.get("expected_city")),
                evidence=escape_cell("；".join(item.get("evidence", [])[:3])),
                risk=escape_cell("；".join(item.get("risks", [])[:3]) or "无明显风险"),
                link=link_text,
            )
        )
    lines.extend(["", "## 候选人推荐岗位话术", ""])
    for index, item in enumerate(candidates[:12], start=1):
        lines.extend(
            [
                f"### {index}. {escape_cell(item.get('name'))}｜{escape_cell(item.get('current_company'))}",
                "",
                f"- 级别：{escape_cell(item.get('fit_level'))}（{int(item.get('fit_score') or 0)}分）",
                f"- 依据：{escape_cell('；'.join(item.get('evidence', [])[:4]))}",
                f"- 待确认：{escape_cell('；'.join(item.get('risks', [])[:4]) or '地点/薪资/看机会意愿')}",
                "",
                item.get("draft", ""),
                "",
            ]
        )
    lines.extend(
        [
            "## 下一步",
            "",
            f"- 先打开 A/B 级候选人完整简历，复核{profile.headline}核心经验、项目深度、苏州接受度。",
            "- 复核通过后，在猎聘回复助手里使用上方话术；你修改并采纳后，系统会继续学习你的修改。",
            "- 人选回复后，回到工作台读取回复并生成跟进待办。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Search candidates for an already-published Liepin position.")
    parser.add_argument("--client", default="苏科思")
    parser.add_argument("--position", default="资深机械工程师")
    parser.add_argument("--city")
    parser.add_argument("--salary")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--ws")
    parser.add_argument("--new-tab", action="store_true")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--max-cards", type=int, default=12)
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--recommend-score", type=int, default=65)
    parser.add_argument("--capture-links", action="store_true")
    parser.add_argument("--capture-details", dest="capture_details", action="store_true", default=True)
    parser.add_argument("--no-capture-details", dest="capture_details", action="store_false")
    parser.add_argument("--detail-limit", type=int, default=40)
    parser.add_argument("--open-links", dest="open_links", action="store_true", default=True)
    parser.add_argument("--no-open-links", dest="open_links", action="store_false")
    parser.add_argument("--open-link-limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-delay", type=float, default=1.2)
    parser.add_argument("--max-delay", type=float, default=2.6)
    parser.add_argument("--json-output", help="Write structured candidates for audited downstream intake")
    parser.add_argument("--queries-json", help="Use the audited ASA strategy queries instead of profile defaults")
    args = parser.parse_args()
    profile = build_db_position_profile(args.db, args.client, args.position)
    args.city = args.city or profile.default_city
    args.salary = args.salary or profile.default_salary

    try:
        result = run_search(args)
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "port": args.port,
                    "client": args.client,
                    "position": args.position,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    report = write_report(result, Path(args.output_dir).expanduser())
    if args.json_output:
        json_output = Path(args.json_output).expanduser().resolve()
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(
            json.dumps(
                [
                    {
                        "channel": "liepin",
                        "query": item.get("source_query") or "",
                        "name": item.get("name") or "",
                        "company": item.get("current_company") or "",
                        "title": item.get("current_title") or "",
                        "education": item.get("education") or "",
                        "experience": item.get("experience") or "",
                        "city": item.get("city") or "",
                        "profile_text": item.get("raw_text") or "",
                        "full_text": item.get("full_text") or item.get("raw_text") or "",
                        "work_text": item.get("work_text") or "",
                        "project_text": item.get("project_text") or "",
                        "education_text": item.get("education_text") or "",
                        "resume_url": item.get("resume_url") or "",
                        "res_id_encode": item.get("res_id_encode") or "",
                        "work": item.get("work") or [],
                        "education_history": item.get("education_history") or [],
                        "fit_score": item.get("fit_score"),
                        "fit_level": item.get("fit_level"),
                        "evidence": item.get("evidence") or [],
                        "risks": item.get("risks") or [],
                        "resume_capture_status": item.get("resume_capture_status") or "not_requested",
                        "resume_capture_missing": item.get("resume_capture_missing") or [],
                        "resume_capture_error": item.get("resume_capture_error") or "",
                        "resume_captured_at": item.get("resume_captured_at") or "",
                    }
                    for item in result["candidates"]
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    opened_links: list[str] = []
    if args.capture_links and args.open_links:
        opened_links = open_resume_links_in_cdp(args.port, result["candidates"], args.open_link_limit)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "candidates": len(result["candidates"]),
                "a_candidates": sum(1 for item in result["candidates"] if int(item.get("fit_score") or 0) >= 78),
                "b_candidates": sum(1 for item in result["candidates"] if 65 <= int(item.get("fit_score") or 0) < 78),
                "detail_capture": result.get("detail_capture") or {},
                "rounds": result.get("rounds") or [],
                "opened_links": len(opened_links),
                "report": str(report),
                "json_output": str(Path(args.json_output).expanduser().resolve()) if args.json_output else "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
