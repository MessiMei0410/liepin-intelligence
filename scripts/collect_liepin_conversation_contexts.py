#!/usr/bin/env python3
"""Collect read-only message contexts from visible Liepin IM conversations.

This script only opens existing conversations in the UI and reads text.
It does not type, send, or click any outbound action.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_PORT = 9223
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
DEFAULT_SELF_TEMPLATE = "您好，我这边是做半导体这块的，方便的话咱们可以加个微信，看机会随时沟通。"


LIST_JS = r"""
(() => {
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const stripStatus = s => clean(s).replace(/^\[(?:已读|未读|手机)\]\s*/, '');
  const items = Array.from(document.querySelectorAll('.im-ui-contact-list-item'));
  const rows = items.map((el, index) => {
    const name = clean(el.querySelector('.im-ui-contact-title-main')?.innerText || '');
    const title = clean(el.querySelector('.im-ui-contact-title-sub')?.innerText || '');
    const timeText = clean(el.querySelector('.contact-time')?.innerText || '');
    const message = stripStatus(el.querySelector('.im-ui-last-message')?.innerText || el.querySelector('.im-ui-contact-item-message')?.innerText || '');
    const rect = el.getBoundingClientRect();
    return {
      index,
      name,
      title,
      timeText,
      message,
      visible: !!(rect.width && rect.height),
    };
  }).filter(row => row.name || row.message);
  return JSON.stringify({rows});
})()
"""


SCROLL_LIST_JS = r"""
(() => {
  const wrap = document.querySelector('.im-ui-contacts-wrap');
  if (!wrap) return JSON.stringify({ok:false, reason:'no_contacts_wrap'});
  const scrollers = [wrap, ...Array.from(wrap.querySelectorAll('div'))].filter(el => el && el.scrollHeight > el.clientHeight + 20);
  const target = scrollers.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
  if (!target) return JSON.stringify({ok:false, reason:'no_scroll_target'});
  const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
  const nextTop = Math.min(maxTop, target.scrollTop + Math.max(target.clientHeight * 0.85, 480));
  target.scrollTop = nextTop;
  target.dispatchEvent(new Event('scroll', { bubbles: true }));
  return JSON.stringify({ok:true, scrollTop: target.scrollTop, maxTop});
})()
"""


OPEN_ROW_JS = r"""
((targetIndex) => {
  const items = Array.from(document.querySelectorAll('.im-ui-contact-list-item'));
  const el = items[targetIndex];
  if (!el) return JSON.stringify({ok:false, reason:'not_found'});
  el.scrollIntoView({block:'center'});
  el.click();
  return JSON.stringify({ok:true});
})(arguments[0])
"""


CONTEXT_JS = r"""
(() => {
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const header = document.querySelector('.im-ui-pro-chat-header') || document.querySelector('.im-ui-chat-container');
  const title = clean(
    header?.querySelector('.im-ui-pro-chat-header-name')?.innerText ||
    header?.querySelector('.im-ui-pro-chat-header-basic-info-content')?.innerText ||
    ''
  );
  const subtitle = clean(
    header?.querySelector('.im-ui-pro-chat-header-work-title')?.innerText ||
    header?.querySelector('.im-ui-pro-chat-header-ext-content')?.innerText ||
    ''
  );
  const nodes = Array.from(document.querySelectorAll('.im-ui-message-item-wrapper'));
  const messages = nodes.map((el, idx) => {
    const text = clean(
      el.querySelector('.im-ui-txt-content .text')?.innerText ||
      el.querySelector('.im-ui-txt-content')?.innerText ||
      el.querySelector('.im-ui-system-tip')?.innerText ||
      el.innerText ||
      ''
    );
    const time = clean(el.querySelector('.format-time')?.innerText || '');
    const cls = String(el.className || '');
    const bodyCls = String(el.querySelector('.im-ui-message-item-body')?.className || '');
    const txtCls = String(el.querySelector('.im-ui-txt')?.className || '');
    let side = 'system';
    if (cls.includes('send') || bodyCls.includes('send') || txtCls.includes('send') || el.querySelector('.im-ui-message-item-send')) side = 'self';
    else if (cls.includes('receive') || bodyCls.includes('receive') || txtCls.includes('receive') || el.querySelector('.im-ui-message-item-receive')) side = 'candidate';
    else if (el.querySelector('.im-ui-txt.send')) side = 'self';
    else if (el.querySelector('.im-ui-txt.receive')) side = 'candidate';
    return { idx, side, time, text, cls, bodyCls, txtCls };
  }).filter(item => item.text && item.text !== '未读' && item.text !== '已读' && item.text !== item.time);
  return JSON.stringify({
    title,
    subtitle,
    messageCount: messages.length,
    messages: messages.slice(-40)
  });
})()
"""


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
                    chunk = self.sock.recv(min(length - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
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


def clean(value: object) -> str:
    return " ".join(str(value or "").split())


def find_im_tab(port: int) -> str:
    tabs = http_json(f"http://127.0.0.1:{port}/json/list")
    for tab in tabs:
        if tab.get("type") == "page" and "h.liepin.com/im/showmsgnewpage" in tab.get("url", ""):
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("没有找到猎聘职聊页面，请先在 Chrome 打开 https://h.liepin.com/im/showmsgnewpage")


def detect_direction(line: str) -> str:
    text = normalize_message(line)
    if not text:
        return "unknown"
    if "您好，我这边是做半导体这块的" in text or "您向对方推荐了" in text:
        return "self"
    if "您可以修改打招呼语" in text or "试试超级聊聊" in text:
        return "system"
    return "candidate_or_mixed"


def detect_direction_from_side(side: str, text: str) -> str:
    if side in {"self", "candidate", "system"}:
        return side
    return detect_direction(text)


def looks_like_self_preview(text: str) -> bool:
    normalized = normalize_message(text)
    if not normalized:
        return False
    return normalized == normalize_message(DEFAULT_SELF_TEMPLATE)


def collect_contexts(port: int, limit: int, pause: float, scroll_steps: int, candidate_first: bool) -> dict:
    ws = find_im_tab(port)
    cdp = CDP(ws)
    try:
        seen_rows: dict[str, dict] = {}
        conversations_by_key: dict[str, dict] = {}
        for step in range(max(scroll_steps, 1)):
            payload = json.loads(cdp.eval(LIST_JS) or "{}")
            for row in payload.get("rows", []):
                row_key = "｜".join(
                    [
                        clean(row.get("name")),
                        clean(row.get("title")),
                        clean(row.get("timeText")),
                        clean(row.get("message")),
                    ]
                )
                if row_key.strip("｜"):
                    seen_rows[row_key] = row
            current_rows = list(payload.get("rows", []))
            if candidate_first:
                current_rows.sort(
                    key=lambda row: (
                        0 if not looks_like_self_preview(clean(row.get("message"))) else 1,
                        clean(row.get("timeText")),
                    )
                )
            for row in current_rows:
                row_key = "｜".join(
                    [
                        clean(row.get("name")),
                        clean(row.get("title")),
                        clean(row.get("timeText")),
                        clean(row.get("message")),
                    ]
                )
                if not row_key.strip("｜") or row_key in conversations_by_key:
                    continue
                cdp.eval(OPEN_ROW_JS.replace("arguments[0]", str(row["index"])), timeout=10)
                time.sleep(pause)
                context: dict = {}
                expected_name = clean(row.get("name"))
                for _ in range(6):
                    context = json.loads(cdp.eval(CONTEXT_JS, timeout=15) or "{}")
                    title = clean(context.get("title"))
                    messages = context.get("messages") or []
                    if (expected_name and expected_name in title and messages) or len(messages) >= 2:
                        break
                    time.sleep(0.45)
                messages = context.get("messages", [])
                classified = []
                for item in messages[-24:]:
                    text = item.get("text", "")
                    direction = detect_direction_from_side(item.get("side", ""), text)
                    info = classify_reply(text) if direction == "candidate" else None
                    classified.append(
                        {
                            "time": item.get("time", ""),
                            "text": text,
                            "direction_hint": direction,
                            "intent": info.get("intent") if info else "",
                            "tags": info.get("reply_tags") if info else [],
                        }
                    )
                conversations_by_key[row_key] = {
                    "preview": row,
                    "context_title": context.get("title", ""),
                    "context_subtitle": context.get("subtitle", ""),
                    "message_count": context.get("messageCount", 0),
                    "messages": classified,
                }
                if len(conversations_by_key) >= limit:
                    break
            if len(conversations_by_key) >= limit:
                break
            if step < scroll_steps - 1:
                cdp.eval(SCROLL_LIST_JS, timeout=10)
                time.sleep(min(pause, 0.8))
        conversations = list(conversations_by_key.values())[:limit]
        return {
            "collectedAt": datetime.now().isoformat(timespec="seconds"),
            "limit": limit,
            "scrollSteps": scroll_steps,
            "conversations": conversations,
        }
    finally:
        cdp.close()


def write_outputs(payload: dict, output_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"liepin_conversation_contexts_{stamp}.json"
    md_path = output_dir / f"猎聘职聊上下文采样_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 猎聘职聊上下文采样",
        "",
        f"生成时间：{payload['collectedAt']}",
        f"- 会话数：{len(payload['conversations'])}",
        "",
    ]
    for idx, convo in enumerate(payload["conversations"], start=1):
        preview = convo["preview"]
        lines.extend(
            [
                f"## {idx}. {preview.get('name','未识别')}｜{preview.get('title','')}",
                "",
                f"- 预览消息：{preview.get('message','')}",
                f"- 消息块数：{convo.get('message_count', 0)}",
                "",
            ]
        )
        for line in convo["messages"][-12:]:
            label = line["direction_hint"]
            when = f"{line['time']} " if line.get("time") else ""
            intent = f"｜{line['intent']}" if line.get("intent") else ""
            lines.append(f"- [{label}{intent}] {when}{line['text']}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only Liepin conversation contexts.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--pause", type=float, default=1.2)
    parser.add_argument("--scroll-steps", type=int, default=1)
    parser.add_argument("--candidate-first", action="store_true")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = collect_contexts(args.port, args.limit, args.pause, args.scroll_steps, args.candidate_first)
    json_path, md_path = write_outputs(payload, output_dir)
    print(
        json.dumps(
            {
                "ok": True,
                "conversations": len(payload["conversations"]),
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
