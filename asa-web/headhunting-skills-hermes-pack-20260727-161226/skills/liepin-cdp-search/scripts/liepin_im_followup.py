#!/usr/bin/env python3
"""Conservative Liepin IM follow-up sender via Chrome CDP.

Default mode is dry-run: select a verified conversation, fill the message, verify
the draft is in the IM textarea, then clear it. Use --send to actually click send.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from cdp_client import CDP  # noqa: E402


BLOCKER_RE = "验证码|账号异常|安全验证|登录过期|请登录|操作频繁|异常访问|环境异常|账号存在异常|滑块验证"
IM_URL_MARKER = "https://h.liepin.com/im/showmsgnewpage"


def load_tabs(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def choose_im_tab(port: int) -> dict:
    tabs = [t for t in load_tabs(port) if t.get("type") == "page"]
    im_tabs = [
        t
        for t in tabs
        if IM_URL_MARKER in (t.get("url") or "") and (t.get("title") or "") == "职聊"
    ]
    if not im_tabs:
        raise SystemExit("NO_IM_TAB: open https://h.liepin.com/im/showmsgnewpage first")
    return im_tabs[0]


def eval_js(cdp: CDP, expression: str) -> object:
    result = cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": False},
    )
    if not result:
        raise RuntimeError("CDP_EVAL_TIMEOUT")
    if "exceptionDetails" in result.get("result", {}):
        raise RuntimeError(json.dumps(result["result"]["exceptionDetails"], ensure_ascii=False))
    return result.get("result", {}).get("result", {}).get("value")


def js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


STATE_JS = r"""
(() => {
  const text = document.body ? document.body.innerText || "" : "";
  const href = location.href;
  const title = document.title;
  const textarea = document.querySelector("textarea.im-ui-textarea");
  const sendButton = document.querySelector("button.im-ui-basic-send-btn");
  return {
    href, title,
    isIM: href.indexOf("/im/showmsgnewpage") >= 0 && title === "职聊",
    isSearch: href.indexOf("/search/getConditionItem") >= 0 || /找简历\s*人才管理/.test(text),
    hasBlocker: /__BLOCKER_RE__/.test(text),
    hasTextarea: !!textarea,
    hasSendButton: !!sendButton,
    draft: textarea ? textarea.value : "",
    textSample: text.slice(0, 1200)
  };
})()
""".replace("__BLOCKER_RE__", BLOCKER_RE)


def get_state(cdp: CDP) -> dict:
    return as_dict(eval_js(cdp, STATE_JS))


def as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"Expected JS object or JSON string, got {type(value).__name__}")


def assert_locked_im(cdp: CDP, phase: str) -> dict:
    state = get_state(cdp)
    if state.get("hasBlocker"):
        raise SystemExit(f"BLOCKED_{phase}: captcha/account abnormal/login issue detected")
    if state.get("isSearch"):
        raise SystemExit(f"PAGE_DRIFT_{phase}: current tab is search/getConditionItem")
    if not state.get("isIM"):
        raise SystemExit(f"NOT_IM_{phase}: current tab is not Liepin IM: {state.get('href')}")
    return state


def click_conversation(cdp: CDP, candidate_name: str) -> dict:
    expression = f"""
(() => {{
  const name = {js_string(candidate_name)};
  const root = document.querySelector("aside") || document.body;
  function visible(el) {{
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }}
  function exactLine(text) {{
    return String(text || "").split("\\n").some(line => line.trim() === name);
  }}
  function clickable(el) {{
    let cur = el;
    while (cur && cur !== document.body) {{
      const cls = String(cur.className || "");
      const role = cur.getAttribute && cur.getAttribute("role");
      const style = window.getComputedStyle(cur);
      if (cur.tagName === "BUTTON" || cur.tagName === "A" || role === "button" ||
          role === "listitem" || style.cursor === "pointer" ||
          /item|contact|session|conversation|chat|list/.test(cls)) return cur;
      cur = cur.parentElement;
    }}
    return el;
  }}
  const matches = Array.from(root.querySelectorAll("*")).filter(visible).filter(el => exactLine(el.innerText || el.textContent || ""));
  if (!matches.length) return {{ok:false, reason:"candidate_not_found", name}};
  matches.sort((a, b) => {{
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return (ar.width * ar.height) - (br.width * br.height);
  }});
  const target = clickable(matches[0]);
  target.scrollIntoView({{block:"center", inline:"nearest"}});
  const r = target.getBoundingClientRect();
  target.dispatchEvent(new MouseEvent("mousedown", {{bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2}}));
  target.dispatchEvent(new MouseEvent("mouseup", {{bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2}}));
  target.click();
  return {{ok:true, clickedText:(target.innerText || target.textContent || "").slice(0, 240)}};
}})()
"""
    return as_dict(eval_js(cdp, expression))


def verify_conversation(cdp: CDP, checks: list[str]) -> dict:
    expression = f"""
(() => {{
  const text = document.body ? document.body.innerText || "" : "";
  const checks = {json.dumps(checks, ensure_ascii=False)};
  const missing = checks.filter(x => text.indexOf(x) < 0);
  return {{ok: missing.length === 0, missing, hasBlocker: /{BLOCKER_RE}/.test(text), href: location.href, title: document.title, sample: text.slice(0, 5000)}};
}})()
"""
    return as_dict(eval_js(cdp, expression))


def fill_message(cdp: CDP, message: str) -> dict:
    expression = f"""
(() => {{
  const textarea = document.querySelector("textarea.im-ui-textarea");
  if (!textarea) return {{ok:false, reason:"no_textarea", href: location.href, title: document.title}};
  textarea.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setter.call(textarea, {js_string(message)});
  textarea.dispatchEvent(new Event("input", {{bubbles:true}}));
  textarea.dispatchEvent(new Event("change", {{bubbles:true}}));
  return {{ok: textarea.value.indexOf({js_string(message[:30])}) >= 0, len: textarea.value.length, draft: textarea.value.slice(0, 120)}};
}})()
"""
    return as_dict(eval_js(cdp, expression))


def clear_message(cdp: CDP) -> dict:
    expression = """
(() => {
  const textarea = document.querySelector("textarea.im-ui-textarea");
  if (!textarea) return {ok:false, reason:"no_textarea"};
  textarea.focus();
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setter.call(textarea, "");
  textarea.dispatchEvent(new Event("input", {bubbles:true}));
  textarea.dispatchEvent(new Event("change", {bubbles:true}));
  return {ok: textarea.value === ""};
})()
"""
    return as_dict(eval_js(cdp, expression))


def click_send(cdp: CDP) -> dict:
    expression = """
(() => {
  const textarea = document.querySelector("textarea.im-ui-textarea");
  const buttons = Array.from(document.querySelectorAll("button.im-ui-basic-send-btn")).filter(b => {
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !b.disabled;
  });
  if (!textarea || !textarea.value.trim()) return {ok:false, reason:"empty_draft_or_no_textarea"};
  if (!buttons.length) return {ok:false, reason:"no_visible_send_button", href: location.href, title: document.title};
  const b = buttons[0];
  const r = b.getBoundingClientRect();
  b.dispatchEvent(new MouseEvent("mousedown", {bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2}));
  b.dispatchEvent(new MouseEvent("mouseup", {bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2}));
  b.click();
  return {ok:true, text:b.innerText || "", rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
})()
"""
    return as_dict(eval_js(cdp, expression))


def verify_sent(cdp: CDP, message: str) -> dict:
    expression = f"""
(() => {{
  const text = document.body ? document.body.innerText || "" : "";
  const needle = {js_string(message[:50])};
  const idx = text.indexOf(needle);
  return {{ok: idx >= 0, hasBlocker: /{BLOCKER_RE}/.test(text), href: location.href, title: document.title, evidence: idx >= 0 ? text.slice(Math.max(0, idx - 240), idx + {len(message)} + 240) : text.slice(-1600)}};
}})()
"""
    return as_dict(eval_js(cdp, expression))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--send", action="store_true", help="Actually click send. Default is dry-run.")
    parser.add_argument("--keep-draft", action="store_true", help="Dry-run only: keep filled draft.")
    args = parser.parse_args()

    tab = choose_im_tab(args.port)
    cdp = CDP(tab["webSocketDebuggerUrl"])
    try:
        cdp.send("Page.bringToFront")
        assert_locked_im(cdp, "initial")
        click_result = click_conversation(cdp, args.candidate)
        time.sleep(2.0)
        assert_locked_im(cdp, "after_select")
        if not click_result.get("ok"):
            raise SystemExit("SELECT_FAILED: " + json.dumps(click_result, ensure_ascii=False))

        review = verify_conversation(cdp, [args.candidate] + args.check)
        if review.get("hasBlocker"):
            raise SystemExit("BLOCKED_REVIEW")
        if not review.get("ok"):
            raise SystemExit("VERIFY_FAILED: " + json.dumps(review, ensure_ascii=False))

        fill = fill_message(cdp, args.message)
        time.sleep(0.8)
        assert_locked_im(cdp, "after_fill")
        if not fill.get("ok"):
            raise SystemExit("FILL_FAILED: " + json.dumps(fill, ensure_ascii=False))

        if not args.send:
            clear = {"ok": True, "skipped": True} if args.keep_draft else clear_message(cdp)
            print(json.dumps({
                "status": "dry_run_ok",
                "candidate": args.candidate,
                "selected_tab_id": tab.get("id"),
                "review": {k: review[k] for k in ("ok", "missing", "href", "title")},
                "fill": fill,
                "clear": clear,
            }, ensure_ascii=False, indent=2))
            return 0

        send = click_send(cdp)
        time.sleep(3.0)
        assert_locked_im(cdp, "after_send")
        sent = verify_sent(cdp, args.message)
        status = "sent_verified" if sent.get("ok") and not sent.get("hasBlocker") else "clicked_unverified"
        print(json.dumps({
            "status": status,
            "candidate": args.candidate,
            "selected_tab_id": tab.get("id"),
            "review": {k: review[k] for k in ("ok", "missing", "href", "title")},
            "fill": fill,
            "send": send,
            "sent": sent,
        }, ensure_ascii=False, indent=2))
        return 0 if status == "sent_verified" else 2
    finally:
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
