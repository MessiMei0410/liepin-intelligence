#!/usr/bin/env python3
"""Safe Liepin publish helper.

Default mode is prepare/draft. Real publish requires --mode publish --confirm PUBLISH.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import random
import re
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_PORT = 9223


class CDP:
    def __init__(self, ws_url: str):
        u = urllib.parse.urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(20)
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

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, method, params=None, timeout=15):
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}}, ensure_ascii=False)
        self.sock.sendall(self._frame(msg))
        return self._recv(timeout)

    def eval(self, expression, timeout=15):
        res = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        if not res:
            return None
        return res.get("result", {}).get("result", {}).get("value")

    def _frame(self, text):
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

    def _recv(self, timeout=15):
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


def http_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def create_tab(port: int, url: str) -> str:
    encoded = urllib.parse.quote(url, safe=":/?&=#%")
    data = http_json(f"http://127.0.0.1:{port}/json/new?{encoded}", method="PUT")
    return data["webSocketDebuggerUrl"]


def wait_for(cdp: CDP, expression: str, predicate, timeout: float = 15, step: float = 0.4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = cdp.eval(expression, timeout=8)
        if predicate(last):
            return last
        time.sleep(step)
    return last


def js(val: str) -> str:
    return json.dumps(val, ensure_ascii=False)


def load_draft(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inspect_page(cdp: CDP) -> dict:
    raw = cdp.eval(
        r"""(() => {
          const clean = s => (s || '').trim().replace(/\s+/g, ' ');
          const fields = Array.from(document.querySelectorAll('input,textarea,button,a,[role=button],.ant-select-selector,.ant-checkbox-wrapper,.ant-switch,.ant-form-item'))
            .map((el, i) => ({
              i, tag: el.tagName,
              text: clean(el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.title || ''),
              placeholder: el.placeholder || '',
              value: el.value || '',
              href: el.href || '',
              cls: String(el.className || '').slice(0, 220),
              id: el.id || '',
              name: el.name || '',
              type: el.type || '',
              disabled: !!el.disabled,
              checked: !!el.checked
            }))
            .filter(x => x.text || x.placeholder || x.value || x.href || x.id || /ant-select|form-item|checkbox|switch/.test(x.cls))
            .slice(0, 200);
          const body = clean(document.body.innerText).slice(0, 12000);
          return JSON.stringify({url: location.href, title: document.title, body, fields});
        })()""",
        timeout=25,
    )
    return json.loads(raw) if raw else {"error": "inspect_failed"}


def click_center(cdp: CDP, rect: dict):
    x = rect["x"] + rect["w"] / 2
    y = rect["y"] + rect["h"] / 2
    for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
        params = {"type": event_type, "x": x, "y": y, "button": "left", "clickCount": 1}
        if event_type == "mousePressed":
            params["buttons"] = 1
        cdp.send("Input.dispatchMouseEvent", params, timeout=8)
        time.sleep(0.12)


def select_display(cdp: CDP, input_id: str) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
          const input = document.getElementById({js(input_id)});
          if (!input) return JSON.stringify({{ok:false, reason:'input not found', id:{js(input_id)}}});
          const select = input.closest('.ant-select');
          const text = clean(select ? select.innerText : input.value);
          const tags = select ? Array.from(select.querySelectorAll('.ant-select-selection-item')).map(el => clean(el.innerText || el.textContent || '')).filter(Boolean) : [];
          return JSON.stringify({{ok:true, id:input.id, text, value: input.value || '', tags, cls:String(select && select.className || '')}});
        }})()""",
        timeout=8,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "select_display_failed"}


def wait_for_input(cdp: CDP, input_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    last = {"ok": False, "reason": "not checked", "id": input_id}
    while time.time() < deadline:
        raw = cdp.eval(
            f"""(() => {{
              const input = document.getElementById({js(input_id)});
              return JSON.stringify({{ok: !!input, id: {js(input_id)}, url: location.href, title: document.title}});
            }})()""",
            timeout=5,
        )
        last = json.loads(raw) if raw else {"ok": False, "reason": "input_wait_failed", "id": input_id}
        if last.get("ok"):
            return last
        time.sleep(0.5)
    return last


def set_input_value(cdp: CDP, element_id: str, value: str) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const input = document.getElementById({js(element_id)});
          if (!input) return JSON.stringify({{ok:false, reason:'input not found', id:{js(element_id)}}});
          input.focus();
          const proto = input.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          setter.call(input, {js(value)});
          input.dispatchEvent(new Event('input', {{bubbles:true}}));
          input.dispatchEvent(new Event('change', {{bubbles:true}}));
          input.dispatchEvent(new KeyboardEvent('keydown', {{key:'ArrowDown', code:'ArrowDown', bubbles:true}}));
          return JSON.stringify({{ok:true, value:input.value, id:input.id}});
        }})()""",
        timeout=10,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "set_input_failed"}


def open_select(cdp: CDP, input_id: str) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const input = document.getElementById({js(input_id)});
          if (!input) return JSON.stringify({{ok:false, reason:'input not found', id:{js(input_id)}}});
          if (input.disabled) return JSON.stringify({{ok:false, reason:'input disabled', id:{js(input_id)}}});
          const select = input.closest('.ant-select') || input.parentElement;
          const target = (select && select.querySelector('.ant-select-selector')) || input;
          target.scrollIntoView({{block:'center', inline:'nearest'}});
          const r = target.getBoundingClientRect();
          const eventInit = {{bubbles:true, cancelable:true, composed:true, view:window, clientX:r.x+r.width/2, clientY:r.y+r.height/2, button:0, buttons:1}};
          for (const ev of ['pointerdown','mousedown','pointerup','mouseup','click']) {{
            const EventClass = ev.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
            target.dispatchEvent(new EventClass(ev, eventInit));
          }}
          input.focus();
          return JSON.stringify({{ok:true, id:input.id, cls:String(select && select.className || ''), rect:{{x:r.x,y:r.y,w:r.width,h:r.height}}}});
        }})()""",
        timeout=10,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "open_select_failed"}


def wait_until_enabled(cdp: CDP, input_id: str, timeout: float = 8) -> dict:
    deadline = time.time() + timeout
    last = {"ok": False, "reason": "not checked"}
    while time.time() < deadline:
        raw = cdp.eval(
            f"""(() => {{
              const input = document.getElementById({js(input_id)});
              if (!input) return JSON.stringify({{ok:false, reason:'input not found', id:{js(input_id)}}});
              return JSON.stringify({{ok:!input.disabled, disabled:!!input.disabled, id:input.id}});
            }})()""",
            timeout=5,
        )
        last = json.loads(raw) if raw else {"ok": False, "reason": "enabled_check_failed"}
        if last.get("ok"):
            return last
        time.sleep(0.4)
    return last


def find_text_option(cdp: CDP, keyword: str) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const key = {js(keyword)}.toLowerCase();
          const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
          const visible = el => {{
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          }};
          const options = Array.from(document.querySelectorAll('.ant-select-item-option, .search-component-suggest li, .suggest-list li, .ant-dropdown-menu-item, .ant-cascader-menu-item, .ant-select-item'))
            .filter(visible);
          const match = options.find(el => clean(el.innerText || el.textContent || '').toLowerCase().includes(key));
          if (!match) return JSON.stringify({{ok:false, reason:'no option match', keyword:key}});
          const r = match.getBoundingClientRect();
          return JSON.stringify({{ok:true, text:clean(match.innerText || match.textContent || ''), cls:String(match.className || ''), rect:{{x:r.x,y:r.y,w:r.width,h:r.height}}}});
        }})()""",
        timeout=12,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "find_option_failed"}


def find_text_option_scrolling(cdp: CDP, keyword: str, max_scrolls: int = 80) -> dict:
    for _ in range(max_scrolls):
        option = find_text_option(cdp, keyword)
        if option.get("ok"):
            return option
        moved_raw = cdp.eval(
            r"""(() => {
              const visible = el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const holders = Array.from(document.querySelectorAll('.rc-virtual-list-holder, .ant-select-dropdown .rc-virtual-list-holder, .ant-select-dropdown'))
                .filter(visible)
                .filter(el => el.scrollHeight > el.clientHeight);
              if (!holders.length) return JSON.stringify({ok:false, reason:'no scroll holder'});
              const holder = holders[holders.length - 1];
              const before = holder.scrollTop;
              holder.scrollTop = before + Math.max(160, holder.clientHeight || 160);
              holder.dispatchEvent(new Event('scroll', {bubbles:true}));
              return JSON.stringify({ok:holder.scrollTop !== before, before, after:holder.scrollTop, max:holder.scrollHeight});
            })()""",
            timeout=5,
        )
        moved = json.loads(moved_raw) if moved_raw else {"ok": False, "reason": "scroll_failed"}
        if not moved.get("ok"):
            return {"ok": False, "reason": "no option match", "keyword": keyword, "scroll": moved}
        time.sleep(0.15)
    return {"ok": False, "reason": "no option match after scroll", "keyword": keyword}


def choose_option(cdp: CDP, keyword: str) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const key = {js(keyword)}.toLowerCase();
          const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
          const visible = el => {{
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          }};
          const options = Array.from(document.querySelectorAll('.ant-select-item-option, .search-component-suggest li, .suggest-list li, .ant-cascader-menu-item, li'))
            .filter(visible)
            .filter(el => clean(el.innerText || el.textContent || '').toLowerCase().includes(key));
          if (!options.length) return JSON.stringify({{ok:false, reason:'no option match', keyword:key}});
          const el = options[0];
          const r = el.getBoundingClientRect();
          const eventInit = {{bubbles:true, cancelable:true, composed:true, view:window, clientX:r.x+r.width/2, clientY:r.y+r.height/2, button:0, buttons:1}};
          for (const ev of ['pointerdown','mousedown','pointerup','mouseup','click']) {{
            const EventClass = ev.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
            el.dispatchEvent(new EventClass(ev, eventInit));
          }}
          if (typeof el.click === 'function') el.click();
          const selected = clean(document.activeElement && document.activeElement.value ? document.activeElement.value : '');
          return JSON.stringify({{ok:true, text:clean(el.innerText || el.textContent || ''), cls:String(el.className || ''), input:selected}});
        }})()""",
        timeout=15,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "choose_option_failed"}


def choose_dropdown_input(cdp: CDP, input_id: str, keyword: str, choice: str | None = None) -> dict:
    enabled = wait_until_enabled(cdp, input_id)
    if not enabled.get("ok"):
        return enabled
    open_res = open_select(cdp, input_id)
    if not open_res.get("ok"):
        return open_res
    time.sleep(0.4)
    set_res = set_input_value(cdp, input_id, keyword)
    if not set_res.get("ok"):
        return set_res
    time.sleep(1.1)
    target = choice or keyword
    option = find_text_option_scrolling(cdp, target)
    if not option.get("ok") and target != keyword:
        option = find_text_option_scrolling(cdp, keyword)
    if not option.get("ok"):
        return option
    choose = choose_option(cdp, option["text"])
    return {"ok": choose.get("ok", False), "open": open_res, "set": set_res, "option": option, "choose": choose}


def choose_company(cdp: CDP, company_choice: str) -> dict:
    enabled = wait_until_enabled(cdp, "customerId")
    if not enabled.get("ok"):
        return enabled
    open_res = open_select(cdp, "customerId")
    if not open_res.get("ok"):
        return open_res
    time.sleep(0.6)
    raw = cdp.eval(
        f"""(() => {{
          const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
          const target = {js(company_choice)};
          const visible = el => {{
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          }};
          const options = Array.from(document.querySelectorAll('[role=\"option\"], .ant-select-item-option, .search-component-suggest li, .suggest-list li, .ant-dropdown-menu-item, li'))
            .filter(visible)
            .map(el => ({{
              text: clean(el.innerText || el.textContent || ''),
              id: el.id || '',
              cls: String(el.className || ''),
              rect: (function(){{ const r = el.getBoundingClientRect(); return {{x:r.x,y:r.y,w:r.width,h:r.height}}; }})()
            }}))
            .filter(item => item.text.includes(target) || target.includes(item.text) || item.text.includes('鹏新旭') || item.text.includes('深圳市鹏新旭技术有限公司'));
          return JSON.stringify({{ok: options.length > 0, target, options}});
        }})()""",
        timeout=12,
    )
    found = json.loads(raw) if raw else {"ok": False, "reason": "company option lookup failed"}
    if not found.get("ok"):
        return {"ok": False, "open": open_res, "found": found, "display": select_display(cdp, "customerId")}

    attempts = []
    for option in found.get("options", [])[:8]:
        rect = option.get("rect")
        if not rect:
            continue
        click_center(cdp, rect)
        time.sleep(0.55)
        display = select_display(cdp, "customerId")
        attempts.append({"option": option, "display": display})
        if (
            company_choice in (display.get("text") or "")
            or company_choice in (display.get("value") or "")
            or any(company_choice in t for t in display.get("tags", []))
        ):
            cdp.eval("document.body.click()", timeout=5)
            return {"ok": True, "open": open_res, "found": found, "attempts": attempts, "display": display}
        open_select(cdp, "customerId")
        time.sleep(0.25)

    return {"ok": False, "open": open_res, "found": found, "attempts": attempts, "display": select_display(cdp, "customerId")}


def choose_select_option(cdp: CDP, input_id: str, choice: str) -> dict:
    enabled = wait_until_enabled(cdp, input_id)
    if not enabled.get("ok"):
        return enabled
    open_res = open_select(cdp, input_id)
    if not open_res.get("ok"):
        return open_res
    time.sleep(0.6)
    option = find_text_option_scrolling(cdp, choice)
    if not option.get("ok"):
        return option
    choose = choose_option(cdp, option["text"])
    return {"ok": choose.get("ok", False), "open": open_res, "option": option, "choose": choose}


def choose_job_category(cdp: CDP, keyword: str, choice: str | None = None) -> dict:
    raw = cdp.eval(
        f"""(() => {{
          const input = document.querySelector('.jobs-wrap input.search-component-input');
          if (!input) return JSON.stringify({{ok:false, reason:'job category input not found'}});
          input.focus();
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(input, {js(keyword)});
          input.dispatchEvent(new Event('input', {{bubbles:true}}));
          input.dispatchEvent(new Event('change', {{bubbles:true}}));
          input.dispatchEvent(new KeyboardEvent('keydown', {{key:'ArrowDown', code:'ArrowDown', bubbles:true}}));
          return JSON.stringify({{ok:true, value:input.value}});
        }})()""",
        timeout=10,
    )
    set_res = json.loads(raw) if raw else {"ok": False, "reason": "job category set failed"}
    if not set_res.get("ok"):
        return set_res
    time.sleep(1.1)
    target = choice or keyword
    option = find_text_option_scrolling(cdp, target)
    if not option.get("ok") and target != keyword:
        option = find_text_option_scrolling(cdp, keyword)
    if not option.get("ok"):
        return option
    choose = choose_option(cdp, option["text"])
    return {"ok": choose.get("ok", False), "set": set_res, "option": option, "choose": choose}


def choose_city(cdp: CDP, city_choice: str) -> dict:
    def wait_city_display(required_city: str, timeout: float = 3.0) -> dict:
        deadline = time.time() + timeout
        last = select_display(cdp, "rc_select_2")
        while time.time() < deadline:
            last = select_display(cdp, "rc_select_2")
            display_text = last.get("text") or ""
            display_tags = last.get("tags", []) or []
            if (required_city and required_city in display_text) or any(required_city and required_city in tag for tag in display_tags):
                cdp.eval(
                    r"""(() => {
                      const btn = Array.from(document.querySelectorAll('.ant-modal-close,button,[role=button]'))
                        .find(el => /ant-modal-close/.test(String(el.className || '')) || /^确\s*定$|^确定$/.test((el.innerText || '').trim()));
                      if (btn) btn.click();
                      document.body.click();
                      return true;
                    })()""",
                    timeout=5,
                )
                return {"ok": True, "display": last}
            time.sleep(0.25)
        return {"ok": False, "display": last}

    def click_all_city_if_present() -> dict:
        raw = cdp.eval(
            r"""(() => {
              const clean = s => (s || '').trim().replace(/\s+/g, ' ');
              const visible = el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              };
              const el = Array.from(document.querySelectorAll('.ant-modal-wrap .ant-tag, .ant-modal-wrap li, .ant-modal-wrap span, .ant-modal-wrap button'))
                .filter(visible)
                .find(node => clean(node.innerText || node.textContent || '') === '全苏州');
              if (!el) return JSON.stringify({ok:false, reason:'no 全苏州 option'});
              const r = el.getBoundingClientRect();
              const init = {bubbles:true, cancelable:true, composed:true, view:window, clientX:r.x+r.width/2, clientY:r.y+r.height/2, button:0, buttons:1};
              for (const ev of ['pointerdown','mousedown','pointerup','mouseup','click']) {
                const C = ev.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                el.dispatchEvent(new C(ev, init));
              }
              if (typeof el.click === 'function') el.click();
              return JSON.stringify({ok:true, text:clean(el.innerText || el.textContent || '')});
            })()""",
            timeout=8,
        )
        return json.loads(raw) if raw else {"ok": False, "reason": "all_city_lookup_failed"}

    def click_city_option_dom(required_city: str) -> dict:
        raw = cdp.eval(
            f"""(async () => {{
              const city = {js(required_city)};
              const sleep = ms => new Promise(r => setTimeout(r, ms));
              const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
              const visible = el => {{
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              }};
              const fire = el => {{
                const r = el.getBoundingClientRect();
                const init = {{bubbles:true, cancelable:true, composed:true, view:window, clientX:r.x+r.width/2, clientY:r.y+r.height/2, button:0, buttons:1}};
                for (const ev of ['pointerdown','mousedown','pointerup','mouseup','click']) {{
                  const C = ev.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                  el.dispatchEvent(new C(ev, init));
                }}
                if (typeof el.click === 'function') el.click();
              }};
              const pickByText = text => {{
                const nodes = Array.from(document.querySelectorAll('.ant-modal-wrap .ant-tag, .ant-modal-wrap li, .ant-modal-wrap span, .ant-modal-wrap button'))
                  .filter(visible)
                  .filter(el => clean(el.innerText || el.textContent || '') === text);
                const exact = nodes.find(el => /ant-tag-checkable|LI|BUTTON/.test(String(el.className || '') + el.tagName)) || nodes[0];
                if (!exact) return false;
                fire(exact);
                return true;
              }};
              const first = pickByText(city);
              await sleep(900);
              const second = pickByText('全' + city);
              await sleep(900);
              const input = document.getElementById('rc_select_2');
              const select = input && input.closest('.ant-select');
              const display = clean(select ? select.innerText : '');
              if (display.includes(city)) {{
                const ok = Array.from(document.querySelectorAll('.ant-modal-wrap button,[role=button]'))
                  .filter(visible)
                  .find(el => /^确\\s*定$|^确定$/.test(clean(el.innerText || el.textContent || '')));
                if (ok) fire(ok);
                document.body.click();
              }}
              return JSON.stringify({{ok: display.includes(city), first, second, display}});
            }})()""",
            timeout=8,
        )
        return json.loads(raw) if raw else {"ok": False, "reason": "city_dom_click_failed"}

    enabled = wait_until_enabled(cdp, "rc_select_2")
    if not enabled.get("ok"):
        return enabled
    open_res = open_select(cdp, "rc_select_2")
    if not open_res.get("ok"):
        return open_res
    time.sleep(0.6)
    raw = cdp.eval(
        f"""(() => {{
          const clean = s => (s || '').trim().replace(/\\s+/g, ' ');
          const target = {js(city_choice)};
          const visible = el => {{
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          }};
          const targetTokens = target.split(/[·\s]+/).filter(Boolean);
          const cityName = targetTokens[targetTokens.length - 1] || target;
          const nodes = Array.from(document.querySelectorAll('.data-list li span.ant-tag-checkable, .data-list li, .data-list .ant-tag, .ant-select-item-option'))
            .filter(visible)
            .filter(node => {{
              const text = clean(node.innerText || node.textContent || '');
              return text.includes(target) || (cityName && text === cityName);
            }});
          const choices = nodes.map(el => {{
            const r = el.getBoundingClientRect();
            return {{
              text: clean(el.innerText || el.textContent || ''),
              id: el.id || '',
              parentId: String(el.parentElement && el.parentElement.id || ''),
              cls: String(el.className || ''),
              rect: {{x:r.x, y:r.y, w:r.width, h:r.height}}
            }};
          }});
          return JSON.stringify({{ok: choices.length > 0, reason: choices.length ? '' : 'city option not found', target, choices}});
        }})()""",
        timeout=12,
    )
    found = json.loads(raw) if raw else {"ok": False, "reason": "city option lookup failed"}
    if not found.get("ok"):
        return {"ok": False, "open": open_res, "found": found}

    attempts = []
    city_tokens = [t for t in re.split(r"[·\s]+", city_choice) if t]
    required_city = city_tokens[-1] if city_tokens else city_choice
    for choice in found.get("choices", [])[:6]:
        rect = choice.get("rect")
        if not rect:
            continue
        dom_click = click_city_option_dom(required_city)
        time.sleep(0.55)
        all_city = click_all_city_if_present()
        if all_city.get("ok"):
            time.sleep(0.55)
        waited = wait_city_display(required_city, timeout=6.0)
        if waited.get("ok"):
            return {"ok": True, "open": open_res, "found": found, "attempts": attempts, "domClick": dom_click, "allCity": all_city, "display": waited.get("display")}
        click_center(cdp, rect)
        time.sleep(0.55)
        waited = wait_city_display(required_city, timeout=6.0)
        if waited.get("ok"):
            return {"ok": True, "open": open_res, "found": found, "attempts": attempts, "domClick": dom_click, "allCity": all_city, "display": waited.get("display")}
        display = select_display(cdp, "rc_select_2")
        attempts.append({"choice": choice, "display": display})
        display_text = display.get("text") or ""
        display_tags = display.get("tags", []) or []
        if (required_city and required_city in display_text) or any(required_city and required_city in tag for tag in display_tags):
            cdp.eval("document.body.click()", timeout=5)
            return {"ok": True, "open": open_res, "found": found, "attempts": attempts, "display": display}
        open_select(cdp, "rc_select_2")
        time.sleep(0.25)

    return {"ok": False, "open": open_res, "found": found, "attempts": attempts, "display": select_display(cdp, "rc_select_2")}


def company_keyword_variants(company: str) -> list[str]:
    variants = [company]
    display = company
    for token in ("深圳市", "上海市", "北京市", "广州市"):
        display = display.replace(token, "")
    display = display.replace("有限公司", "公司")
    if display and display not in variants:
        variants.append(display)
    stripped = company
    for token in ("深圳市", "上海市", "北京市", "广州市", "有限公司", "股份有限公司", "科技", "技术"):
        stripped = stripped.replace(token, "")
    stripped = stripped.replace(" ", "")
    if stripped and stripped not in variants:
        variants.append(stripped)
    compact = company.replace("有限公司", "").replace("深圳市", "").replace("上海市", "").replace("北京市", "").replace("广州市", "").replace(" ", "")
    if compact and compact not in variants:
        variants.append(compact)
    if "鹏新旭" in company and "鹏新旭" not in variants:
        variants.insert(0, "鹏新旭")
    return [v for v in variants if v]


def fill_publish_form(cdp: CDP, draft: dict) -> dict:
    result = {"filled": [], "blocked": [], "warnings": []}

    # 公司
    company_choice = draft.get("client_company")
    if not company_choice:
        result["blocked"].append({"field": "customerId", "reason": "missing client_company"})
    else:
        r = choose_company(cdp, company_choice)
        if not r.get("ok"):
            r = {"ok": False, "reason": "no company variant matched", "last": r}
            for kw in company_keyword_variants(company_choice):
                r = choose_dropdown_input(cdp, "customerId", kw, company_choice)
                if r.get("ok"):
                    break
        result["filled"].append({"field": "customerId", **r})
        if not r.get("ok"):
            result["blocked"].append({"field": "customerId", "reason": r.get("reason", "unknown")})

    # 职位名称
    r = set_input_value(cdp, "hjobTitle", draft["job_title"])
    result["filled"].append({"field": "hjobTitle", **r})

    # 职位类别
    r = choose_job_category(cdp, draft["job_category_keyword"], draft.get("job_category_choice", draft["job_category_keyword"]))
    result["filled"].append({"field": "job_category", **r})

    # 城市
    r = choose_city(cdp, draft.get("city_choice", draft["city_keyword"]))
    result["filled"].append({"field": "city", **r})

    # 薪资三个下拉，按顺序选低/高/月数
    r1 = choose_select_option(cdp, "rc_select_3", f'{draft["salary_low_k"]}k')
    r2 = choose_select_option(cdp, "rc_select_4", f'{draft["salary_high_k"]}k')
    r3 = choose_select_option(cdp, "rc_select_5", f'{draft["salary_months"]}个月')
    result["filled"].extend([
        {"field": "salary_low", **r1},
        {"field": "salary_high", **r2},
        {"field": "salary_months", **r3},
    ])

    # 工作年限
    r = choose_select_option(cdp, "workYear", draft.get("work_year_choice", draft["work_year_keyword"]))
    result["filled"].append({"field": "work_year", **r})
    # fallback: set numeric bounds too
    set_input_value(cdp, "workYearLow", str(draft["work_year_low"]))
    set_input_value(cdp, "workYearHigh", str(draft["work_year_high"]))

    # 学历
    r = choose_select_option(cdp, "eduLevelCode", draft.get("education_choice", draft["education_choice"]))
    result["filled"].append({"field": "education", **r})
    if draft.get("education_tongzhao"):
        cdp.eval(r"""(()=>{const c=document.getElementById('eduLevelTz'); if(c && !c.checked) c.click(); return !!(c&&c.checked)})()""", timeout=8)

    # 行业
    r = choose_dropdown_input(cdp, "rc_select_8", draft["industry_keyword"], draft.get("industry_choice", draft["industry_keyword"]))
    result["filled"].append({"field": "industry", **r})

    # 职位描述
    r = set_input_value(cdp, "detailDuty", draft["description"])
    result["filled"].append({"field": "detailDuty", **r})

    # 其他数值/开关
    set_input_value(cdp, "recruitCnt", str(draft.get("recruit_count", 1)))
    set_input_value(cdp, "hjobCloseDate", draft.get("close_date", "2026-09-15"))
    if draft.get("private_job"):
        cdp.eval(r"""(()=>{const el=document.getElementById('privateJob'); if(el && !el.classList.contains('ant-switch-checked')) el.click(); return true})()""", timeout=8)
    else:
        cdp.eval(r"""(()=>{const el=document.getElementById('privateJob'); if(el && el.classList.contains('ant-switch-checked')) el.click(); return true})()""", timeout=8)

    # 协议
    cdp.eval(r"""(()=>{const el=document.getElementById('agreement'); if(el && !el.checked) el.click(); return !!(el&&el.checked)})()""", timeout=8)

    return result


def get_publish_button_enabled(cdp: CDP) -> dict:
    raw = cdp.eval(
        r"""(() => {
          const btn = Array.from(document.querySelectorAll('button'))
            .find(el => /发布职位/.test((el.innerText || '').trim()));
          if (!btn) return JSON.stringify({ok:false, reason:'no publish button'});
          const disabled = !!btn.disabled || btn.getAttribute('aria-disabled') === 'true';
          return JSON.stringify({ok:true, disabled, text:(btn.innerText || '').trim(), cls:String(btn.className || '')});
        })()""",
        timeout=8,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "no_publish_button"}


def check_publish_result(cdp: CDP) -> dict:
    raw = cdp.eval(
        r"""(() => {
          const body = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
          const hasEdit = /职位修改_职位管理|职位修改/.test(document.title) || /职位修改/.test(body);
          const hasList = /职位管理/.test(document.title) && /发职位/.test(body) && /职位列表|职位管理/.test(body);
          const msg = Array.from(document.querySelectorAll('body *'))
            .map(el => (el.innerText || '').trim())
            .find(t => /发布成功|职位已发布|保存成功|提交成功|审核中|待审核/.test(t));
          return JSON.stringify({ok:true, title:document.title, hasEdit, hasList, msg: msg || '', body: body.slice(0, 2000)});
        })()""",
        timeout=10,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "publish_result_check_failed"}


def click_publish(cdp: CDP) -> dict:
    raw = cdp.eval(
        r"""(() => {
          const btn = Array.from(document.querySelectorAll('button'))
            .find(el => /发布职位/.test((el.innerText || '').trim()) && !el.disabled);
          if (!btn) return JSON.stringify({ok:false, reason:'publish button not ready'});
          btn.click();
          return JSON.stringify({ok:true, text:(btn.innerText || '').trim()});
        })()""",
        timeout=10,
    )
    return json.loads(raw) if raw else {"ok": False, "reason": "click_publish_failed"}


def main():
    parser = argparse.ArgumentParser(description="猎聘职位发布助手（默认仅填草稿）")
    parser.add_argument("--draft", default="outputs/鹏新旭_MFG-CIM_猎聘发布草稿.json", help="职位草稿 JSON")
    parser.add_argument("--mode", choices=["inspect", "prepare", "publish"], default="prepare")
    parser.add_argument("--confirm", help="真实发布必须传 --confirm PUBLISH")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default="", help="日志输出路径")
    args = parser.parse_args()

    if args.mode == "publish" and args.confirm != "PUBLISH":
        print("拒绝真实发布：必须同时提供 --mode publish --confirm PUBLISH", file=sys.stderr)
        return 2

    draft = load_draft(Path(args.draft))
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = Path(args.log) if args.log else Path("outputs") / f"liepin_publish_job_log_{ts}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ws = create_tab(args.port, "https://h.liepin.com/job/showaddpage/")
    cdp = CDP(ws)
    try:
        cdp.send("Runtime.enable")
        cdp.send("Page.enable")
        cdp.send("Page.bringToFront")
        wait_for(cdp, "document.readyState", lambda v: v in {"interactive", "complete"}, timeout=30)
        time.sleep(5)

        page = inspect_page(cdp)
        if args.mode == "inspect":
            payload = {"mode": "inspect", "draft": draft, "page": page}
            log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写入页面检查: {log_path}")
            return 0

        filled = fill_publish_form(cdp, draft)
        time.sleep(1.5)
        enabled = get_publish_button_enabled(cdp)
        summary = {
            "mode": args.mode,
            "draft": draft,
            "page": page,
            "filled": filled,
            "publish_button": enabled,
        }

        if args.mode == "prepare":
            log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已完成草稿填充，未发布: {log_path}")
            return 0

        if enabled.get("disabled"):
            log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"发布按钮仍不可用，已记录日志: {log_path}", file=sys.stderr)
            return 3

        click = click_publish(cdp)
        summary["publish_click"] = click
        time.sleep(3)
        summary["after"] = inspect_page(cdp)
        summary["result"] = check_publish_result(cdp)
        log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if click.get("ok"):
            print(f"已点击发布按钮，日志: {log_path}")
            return 0
        print(f"发布点击失败，日志: {log_path}", file=sys.stderr)
        return 4
    finally:
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
