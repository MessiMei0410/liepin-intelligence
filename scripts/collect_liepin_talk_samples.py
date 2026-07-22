#!/usr/bin/env python3
"""Collect visible Liepin IM preview talk samples without opening conversations."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sqlite3
import struct
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_PORT = 9223
DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


EXTRACT_JS = r"""
(() => {
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const stripStatus = s => clean(s).replace(/^\[(?:已读|未读|手机)\]\s*/, '');
  const items = Array.from(document.querySelectorAll('.im-ui-contact-list-item'));
  const rows = items.map((el, index) => {
    const name = clean(el.querySelector('.im-ui-contact-title-main')?.innerText || '');
    const title = clean(el.querySelector('.im-ui-contact-title-sub')?.innerText || '');
    const timeText = clean(el.querySelector('.contact-time')?.innerText || '');
    const rawMessage = clean(el.querySelector('.im-ui-last-message')?.innerText || el.querySelector('.im-ui-contact-item-message')?.innerText || '');
    const message = stripStatus(rawMessage);
    const rawText = clean(el.innerText || el.textContent || '');
    const unreadText = clean(el.querySelector('.ant-im-badge-count-sm')?.innerText || '');
    return {
      index,
      name,
      title,
      timeText,
      rawMessage,
      message,
      rawText,
      unreadCount: unreadText ? parseInt(unreadText, 10) || 0 : 0,
    };
  }).filter(row => row.name || row.message);
  return JSON.stringify({url: location.href, title: document.title, extractedAt: new Date().toISOString(), rows});
})()
"""


FIND_SCROLL_JS = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll('*'));
  const candidate = nodes
    .filter(el => el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 80)
    .map((el, index) => ({el, index, score: el.scrollHeight * el.clientHeight}))
    .sort((a, b) => b.score - a.score)[0];
  if (!candidate) return JSON.stringify({ok:false});
  window.__liepinTalkScrollEl = candidate.el;
  return JSON.stringify({
    ok: true,
    scrollTop: candidate.el.scrollTop,
    scrollHeight: candidate.el.scrollHeight,
    clientHeight: candidate.el.clientHeight,
  });
})()
"""


SCROLL_JS = r"""
((step) => {
  const el = window.__liepinTalkScrollEl;
  if (!el) return JSON.stringify({ok:false});
  const before = el.scrollTop;
  el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + step);
  el.dispatchEvent(new Event('scroll', {bubbles: true}));
  return JSON.stringify({
    ok: true,
    before,
    after: el.scrollTop,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  });
})(arguments[0])
"""


SELF_PATTERNS = [
    "您好，我这边是做半导体这块的",
    "方便的话咱们可以加个微信",
    "看机会随时沟通",
    "您向对方推荐了",
]

SYSTEM_PATTERNS = [
    "您可以修改打招呼语",
    "试试超级聊聊",
    "去发起",
]


class CDP:
    def __init__(self, ws_url: str):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect((u.hostname, u.port))
        self._id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            first_line = resp.split(b"\r\n")[0]
            raise RuntimeError(f"CDP handshake failed: {first_line!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, method: str, params: dict | None = None, timeout: int = 15) -> dict | None:
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}}, ensure_ascii=False)
        self.sock.sendall(self._frame(msg))
        return self._recv(timeout)

    def eval(self, expression: str, timeout: int = 15):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if not res:
            return None
        inner = res.get("result", {}).get("result", {})
        return inner.get("value")

    def _frame(self, text: str) -> bytes:
        data = text.encode()
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", len(data)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", len(data)))
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        return bytes(header) + mask + masked

    def _recv(self, timeout: int = 15) -> dict | None:
        self.sock.settimeout(timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = self.sock.recv(2)
                if len(hdr) < 2:
                    return None
                opcode = hdr[0] & 0x0F
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self.sock.recv(8))[0]
                mask_key = self.sock.recv(4) if hdr[1] & 0x80 else None
                data = b""
                while len(data) < length:
                    data += self.sock.recv(min(length - len(data), 65536))
                if mask_key:
                    data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                if opcode != 0x01:
                    continue
                msg = json.loads(data.decode())
                if msg.get("id") == self._id:
                    return msg
            except socket.timeout:
                return None
        return None


def http_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def find_im_tab(port: int) -> str:
    tabs = http_json(f"http://127.0.0.1:{port}/json/list")
    for tab in tabs:
        if tab.get("type") == "page" and "h.liepin.com/im/showmsgnewpage" in tab.get("url", ""):
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("没有找到猎聘职聊页面，请先在 Chrome 打开 https://h.liepin.com/im/showmsgnewpage")


def stable_key(row: dict) -> str:
    return "|".join([row.get("name", ""), row.get("title", ""), row.get("timeText", ""), row.get("message", "")])


def classify_direction(message: str) -> str:
    text = message or ""
    if any(pattern in text for pattern in SYSTEM_PATTERNS):
        return "system"
    if any(pattern in text for pattern in SELF_PATTERNS):
        return "self"
    return "candidate"


def classify_strategy(message: str, direction: str) -> str:
    text = message or ""
    if direction == "system":
        return "system"
    if "您向对方推荐了" in text:
        return "job_recommendation"
    if "半导体" in text and "加个微信" in text:
        return "broad_semiconductor_wechat"
    classified = classify_reply(text)
    tags = set(classified.get("reply_tags") or [])
    intent = classified.get("intent") or "unclear"
    if "确认是否在招" in tags:
        return "asks_if_open"
    if "方向确认" in tags:
        return "asks_direction"
    if "自荐匹配" in tags:
        return "self_recommendation"
    if intent == "location_concern":
        return "location"
    if "公司是哪家" in text or "哪家公司" in text or intent == "need_more_info":
        return "asks_company"
    if "匹配" in text or "可以聊聊" in text or "进一步沟通" in text or intent == "interested":
        return "positive_fit"
    if "不对口" in text or "不是" in text or "不考虑" in text or "没兴趣" in text or intent == "not_interested":
        return "mismatch_or_reject"
    if "薪资" in text or "可谈" in text or intent == "salary_concern":
        return "salary"
    if "微信" in text or "联系" in text or intent == "need_contact":
        return "contact_exchange"
    if normalize_message(text) in {"您好", "你好", "hello", "hi"}:
        return "short_ping"
    return "other"


def collect_rows(port: int, scrolls: int, pause: float) -> dict:
    ws = find_im_tab(port)
    cdp = CDP(ws)
    try:
        scroll_info = json.loads(cdp.eval(FIND_SCROLL_JS) or "{}")
        seen: dict[str, dict] = {}
        snapshots = []
        for step_idx in range(max(1, scrolls)):
            payload = json.loads(cdp.eval(EXTRACT_JS) or "{}")
            rows = payload.get("rows", [])
            for row in rows:
                row["direction_guess"] = classify_direction(row.get("message", ""))
                row["strategy_guess"] = classify_strategy(row.get("message", ""), row["direction_guess"])
                seen[stable_key(row)] = row
            snapshots.append({
                "step": step_idx,
                "count": len(rows),
                "unique": len(seen),
                "first": rows[0].get("name") if rows else "",
                "last": rows[-1].get("name") if rows else "",
            })
            if step_idx < scrolls - 1:
                cdp.eval(SCROLL_JS.replace("arguments[0]", str(420)))
                time.sleep(pause)
        return {
            "url": payload.get("url", ""),
            "title": payload.get("title", ""),
            "extractedAt": datetime.now().isoformat(timespec="seconds"),
            "scrollInfo": scroll_info,
            "snapshots": snapshots,
            "rows": list(seen.values()),
        }
    finally:
        cdp.close()


def write_outputs(payload: dict, output_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"liepin_talk_samples_{stamp}.json"
    md_path = output_dir / f"猎聘历史话术样本_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = payload["rows"]
    counts: dict[str, int] = {}
    for row in rows:
        key = row["strategy_guess"]
        counts[key] = counts.get(key, 0) + 1
    lines = [
        "# 猎聘历史话术样本",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- 样本数：{len(rows)}",
        f"- 策略分布：{'、'.join(f'{k} {v}' for k, v in sorted(counts.items()))}",
        "",
        "| 姓名 | 头衔 | 时间 | 方向 | 策略 | 预览消息 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {name} | {title} | {time} | {direction} | {strategy} | {message} |".format(
                name=(row.get("name") or "").replace("|", "｜"),
                title=(row.get("title") or "").replace("|", "｜"),
                time=(row.get("timeText") or "").replace("|", "｜"),
                direction=row.get("direction_guess", ""),
                strategy=row.get("strategy_guess", ""),
                message=(row.get("message") or "").replace("|", "｜"),
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS talk_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT DEFAULT 'liepin',
            candidate_name TEXT,
            candidate_title TEXT,
            time_text TEXT,
            direction_guess TEXT,
            strategy_guess TEXT,
            message TEXT NOT NULL,
            raw_text TEXT,
            source TEXT,
            collected_at TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(channel, candidate_name, candidate_title, time_text, message)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_talk_samples_strategy ON talk_samples(strategy_guess)")
    conn.commit()


def insert_rows(db_path: Path, rows: list[dict], source: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_table(conn)
        before = conn.total_changes
        for row in rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO talk_samples
                    (candidate_name, candidate_title, time_text, direction_guess, strategy_guess, message, raw_text, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("name", ""),
                    row.get("title", ""),
                    row.get("timeText", ""),
                    row.get("direction_guess", ""),
                    row.get("strategy_guess", ""),
                    row.get("message", ""),
                    row.get("rawText", ""),
                    source,
                ),
            )
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Liepin IM preview talk samples without clicking conversations.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--scrolls", type=int, default=8)
    parser.add_argument("--pause", type=float, default=0.6)
    parser.add_argument("--save-db", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = collect_rows(args.port, args.scrolls, args.pause)
    json_path, md_path = write_outputs(payload, output_dir)
    inserted = 0
    if args.save_db:
        inserted = insert_rows(Path(args.db).expanduser(), payload["rows"], str(json_path))
    print(
        json.dumps(
            {
                "ok": True,
                "rows": len(payload["rows"]),
                "inserted": inserted,
                "json": str(json_path),
                "report": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
