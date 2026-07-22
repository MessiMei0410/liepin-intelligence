#!/usr/bin/env python3
"""Inject a professional reply assistant into the Liepin IM page.

The assistant runs in the current Liepin page. It generates and can fill draft
text, but it never clicks the send button.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import textwrap
import time
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_PORT = 9223
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


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


def find_im_tab(port: int) -> str:
    tabs = http_json(f"http://127.0.0.1:{port}/json/list")
    for tab in tabs:
        if tab.get("type") == "page" and "h.liepin.com/im/showmsgnewpage" in tab.get("url", ""):
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("没有找到猎聘职聊页面，请先在 Chrome 打开 https://h.liepin.com/im/showmsgnewpage")


def assistant_js() -> str:
    return r"""
(() => {
  if (window.__liepinReplyAssistantInstalled) {
    window.__liepinReplyAssistantPanel?.remove();
  }
  window.__liepinReplyAssistantInstalled = true;

  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const stripStatus = s => clean(s).replace(/^\[(?:已读|未读|手机)\]\s*/, '');
  const textOf = el => clean(el?.innerText || el?.textContent || '');

  const style = document.createElement('style');
  style.id = 'liepin-reply-assistant-style';
  style.textContent = `
    #liepin-reply-assistant {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 2147483647;
      width: 360px;
      background: #111827;
      color: #eef2ff;
      border: 1px solid rgba(148,163,184,.5);
      border-radius: 10px;
      box-shadow: 0 18px 55px rgba(0,0,0,.35);
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      overflow: hidden;
    }
    #liepin-reply-assistant header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 12px;
      background: #0b1220;
      border-bottom: 1px solid rgba(148,163,184,.25);
      font-weight: 650;
      font-size: 13px;
    }
    #liepin-reply-assistant button {
      border: 1px solid rgba(148,163,184,.45);
      background: #1f2937;
      color: #f8fafc;
      border-radius: 7px;
      padding: 6px 9px;
      font-size: 12px;
      cursor: pointer;
    }
    #liepin-reply-assistant button.primary { background: #2563eb; border-color: #3b82f6; }
    #liepin-reply-assistant button.good { background: #047857; border-color: #10b981; }
    #liepin-reply-assistant button.warn { background: #92400e; border-color: #f59e0b; }
    #liepin-reply-assistant .body { padding: 12px; display: grid; gap: 9px; }
    #liepin-reply-assistant .meta { font-size: 12px; color: #cbd5e1; line-height: 1.45; }
    #liepin-reply-assistant textarea {
      width: 100%;
      min-height: 138px;
      box-sizing: border-box;
      background: #020617;
      color: #e2e8f0;
      border: 1px solid rgba(148,163,184,.35);
      border-radius: 8px;
      padding: 9px;
      resize: vertical;
      font-size: 13px;
      line-height: 1.55;
      font-family: inherit;
    }
    #liepin-reply-assistant .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    #liepin-reply-assistant .hint { font-size: 11px; color: #94a3b8; line-height: 1.45; }
  `;
  document.getElementById(style.id)?.remove();
  document.head.appendChild(style);

  function activeContact() {
    const selected = document.querySelector('.im-ui-contact-list-item.active, .im-ui-contact-list-item[class*="active"], .im-ui-contact-list-item[class*="selected"]');
    const fallback = Array.from(document.querySelectorAll('.im-ui-contact-list-item')).find(el => {
      const r = el.getBoundingClientRect();
      return r.width && r.height && r.top > 0;
    });
    const el = selected || fallback;
    if (!el) return {};
    return {
      name: clean(el.querySelector('.im-ui-contact-title-main')?.innerText || ''),
      title: clean(el.querySelector('.im-ui-contact-title-sub')?.innerText || ''),
      message: stripStatus(el.querySelector('.im-ui-last-message')?.innerText || el.querySelector('.im-ui-contact-item-message')?.innerText || ''),
      rawText: clean(el.innerText || el.textContent || ''),
    };
  }

  function chatText() {
    const wrap = document.querySelector('.im-ui-chat-content-wrapper') || document.body;
    return clean(wrap.innerText || wrap.textContent || '');
  }

  function strategyFor(message) {
    const text = message || '';
    if (/哪家公司|公司是哪|工作年限|要求/.test(text)) return 'asks_company';
    if (/微信|手机号|联系方式|简历|联系我/.test(text)) return 'contact_exchange';
    if (/薪资|待遇|年包|月薪|可谈/.test(text)) return 'salary';
    if (/不对口|不是|不考虑|没兴趣|不合适|区域/.test(text)) return 'mismatch_or_reject';
    if (/匹配|感兴趣|详聊|还在招|可以聊|进一步沟通|应聘/.test(text)) return 'positive_fit';
    return 'general_followup';
  }

  function projectGuess(text) {
    const lower = text.toLowerCase();
    if (/device\s*专家|device专家/i.test(text)) return {client: '鹏新旭', position: 'Device专家', confidence: '高'};
    if (/机械工程师|资深机械/.test(text)) return {client: '微导纳米', position: '机械工程师', confidence: '高'};
    if (/acdc|服务器电源研发总监/i.test(text)) return {client: '', position: 'ACDC服务器电源研发总监', confidence: '中'};
    if (/fpga|fpag/i.test(text)) return {client: '', position: 'FPGA相关岗位', confidence: '低'};
    if (/电力电子|硬件开发|hardware development/i.test(text)) return {client: '', position: '硬件/电力电子研发相关岗位', confidence: '低'};
    if (/电源/.test(text)) return {client: '', position: '电源研发相关岗位', confidence: '低'};
    return {client: '', position: '', confidence: '待确认'};
  }

  function salutation(name) {
    if (!name) return '您好';
    if (/先生$|女士$|老师$/.test(name)) return `${name}，您好`;
    return `${name}您好`;
  }

  function projectText(project) {
    if (project.client && project.position) return `${project.client}的${project.position}`;
    if (project.position) return project.position;
    if (project.client) return `${project.client}的岗位`;
    return '这个机会';
  }

  function hasProjectAnchor(project) {
    return !!clean(`${project.client || ''}${project.position || ''}`);
  }

  function oneKeyQuestion(strategy, project) {
    if (strategy === 'asks_company') return /资深|专家|主管|经理|总监/.test(project.position || '') ? '您这块大概有几年相关经验？' : '您方便先说下这块大概几年经验吗？';
    if (strategy === 'salary') return '您方便先说下目前大概总包区间吗？';
    if (strategy === 'positive_fit') return '您今天方便约 10 分钟电话吗？';
    return '方便先加个微信沟通吗？';
  }

  function draftFor(ctx) {
    const strategy = strategyFor(ctx.message);
    const combined = `${ctx.message} ${ctx.title} ${chatText()}`;
    const project = projectGuess(combined);
    const prefix = salutation(ctx.name);
    const p = projectText(project);
    const titleHint = ctx.title ? `看您目前是${ctx.title}，` : '';
    let draft = '';

    if (strategy === 'asks_company') {
      draft = project.client
        ? `${prefix}，可以的，目前沟通的是${p}。${titleHint}${oneKeyQuestion(strategy, project)}`
        : `${prefix}，收到。客户名称我这边先确认下可透露范围，岗位方向是${p}。${titleHint}${oneKeyQuestion(strategy, project)}`;
    } else if (strategy === 'contact_exchange') {
      draft = hasProjectAnchor(project)
        ? `${prefix}，我加您。我这边是${p}在看，微信里先把岗位要点发您。`
        : `${prefix}，我加您。我这边主要看半导体和高端制造方向机会，微信里再和您同步。`;
    } else if (strategy === 'salary') {
      draft = `${prefix}，收到，薪资这块可以先对齐。${hasProjectAnchor(project) ? `我先按${p}判断预算匹配度。` : ''}${oneKeyQuestion(strategy, project)}`;
    } else if (strategy === 'mismatch_or_reject') {
      draft = '明白，有合适机会随时沟通。';
    } else if (strategy === 'positive_fit') {
      draft = `${prefix}，好的，我看您和${p}方向是匹配的。${titleHint}${oneKeyQuestion(strategy, project)}我先把岗位重点和您这边经历快速对一下。`;
    } else {
      draft = `${prefix}，我这边主要看半导体和高端制造方向机会。方便的话咱们先加个微信，后面有贴近您背景的岗位我及时同步。`;
    }
    return {strategy, project, draft};
  }

  function findInput() {
    const candidates = [
      ...document.querySelectorAll('textarea'),
      ...document.querySelectorAll('[contenteditable="true"]'),
      ...document.querySelectorAll('.ant-im-input, input[type="text"]')
    ];
    return candidates.find(el => {
      const r = el.getBoundingClientRect();
      return r.width > 80 && r.height > 20;
    });
  }

  function fillInput(text) {
    const input = findInput();
    if (!input) return false;
    input.focus();
    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
      const setter = Object.getOwnPropertyDescriptor(input.constructor.prototype, 'value')?.set
        || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
        || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (setter) setter.call(input, text);
      else input.value = text;
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      input.textContent = text;
      input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: text}));
    }
    return true;
  }

  const panel = document.createElement('section');
  panel.id = 'liepin-reply-assistant';
  panel.innerHTML = `
    <header>
      <span>猎聘专业回复助手</span>
      <button data-close>收起</button>
    </header>
    <div class="body">
      <div class="meta" data-meta>选择左侧人选后点击生成。</div>
      <textarea data-draft placeholder="这里会生成专业回复草稿，不会自动发送。"></textarea>
      <div class="actions">
        <button class="primary" data-generate>生成回复</button>
        <button class="good" data-copy>复制草稿</button>
        <button class="warn" data-fill>填入输入框</button>
      </div>
      <div class="hint">安全边界：只生成/复制/填入草稿，不会点击发送。发送前请你人工确认。</div>
    </div>
  `;
  window.__liepinReplyAssistantPanel = panel;
  document.body.appendChild(panel);

  const meta = panel.querySelector('[data-meta]');
  const draftBox = panel.querySelector('[data-draft]');
  let last = null;

  function generate() {
    const ctx = activeContact();
    last = draftFor(ctx);
    draftBox.value = last.draft;
    meta.textContent = `${ctx.name || '当前人选'}｜${ctx.title || '头衔未知'}｜策略：${last.strategy}｜项目：${projectText(last.project)}｜置信：${last.project.confidence}`;
  }

  panel.querySelector('[data-generate]').addEventListener('click', generate);
  panel.querySelector('[data-copy]').addEventListener('click', async () => {
    const text = draftBox.value.trim();
    if (!text) generate();
    await navigator.clipboard.writeText(draftBox.value.trim());
    meta.textContent = '已复制草稿。发送前请人工确认。';
  });
  panel.querySelector('[data-fill]').addEventListener('click', () => {
    const text = draftBox.value.trim();
    if (!text) generate();
    const ok = fillInput(draftBox.value.trim());
    meta.textContent = ok ? '已填入输入框，未发送。请人工确认后再发送。' : '未找到输入框，请先点开具体会话。';
  });
  panel.querySelector('[data-close]').addEventListener('click', () => {
    panel.style.display = 'none';
  });
  generate();
  return JSON.stringify({ok: true, message: 'Liepin reply assistant injected'});
})()
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject Liepin professional reply assistant into current IM page.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    ws = find_im_tab(args.port)
    js = assistant_js()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    js_path = output_dir / "liepin_reply_assistant_last.js"
    js_path.write_text(js, encoding="utf-8")

    cdp = CDP(ws)
    try:
        value = cdp.eval(js, timeout=20)
    finally:
        cdp.close()
    print(json.dumps({"ok": True, "result": value, "script": str(js_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
