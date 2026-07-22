#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

CDP_DIR = Path("/Users/messi/.codex/skills/liepin-cdp-search/scripts")
sys.path.insert(0, str(CDP_DIR))
from cdp_client import CDP  # noqa: E402


XSAAS_HOST = "headhunt.x-saas.com.cn"
RUNNER_MARKER = "asa_search_runner=1"


def load_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def evaluate(cdp: CDP, expression: str) -> Any:
    payload = cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": False})
    if not payload:
        raise RuntimeError("X-SaaS CDP 执行超时")
    result = payload.get("result", {})
    if result.get("exceptionDetails"):
        raise RuntimeError("X-SaaS 页面脚本执行失败")
    return result.get("result", {}).get("value")


def json_expr(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def choose_authenticated_tab(port: int) -> dict[str, Any]:
    tabs = load_json(f"http://127.0.0.1:{port}/json/list")
    candidates = [
        tab for tab in tabs
        if XSAAS_HOST in str(tab.get("url") or "")
        and "#/app/" in str(tab.get("url") or "")
        and RUNNER_MARKER not in str(tab.get("url") or "")
    ]
    if not candidates:
        raise RuntimeError("X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面")
    return candidates[0]


def clone_authenticated_tab(port: int, source_tab: dict[str, Any]) -> tuple[CDP, str]:
    source = CDP(source_tab["webSocketDebuggerUrl"])
    browser_ws = load_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
    browser = CDP(browser_ws)
    target_id = ""
    try:
        created = browser.send("Target.createTarget", {"url": f"https://{XSAAS_HOST}/?{RUNNER_MARKER}#/login"}) or {}
        target_id = str(created.get("result", {}).get("targetId") or "")
        if not target_id:
            raise RuntimeError("无法创建隔离的 X-SaaS 搜索标签页")
        deadline = time.time() + 12
        target_tab = None
        while time.time() < deadline:
            tabs = load_json(f"http://127.0.0.1:{port}/json/list")
            target_tab = next((tab for tab in tabs if tab.get("id") == target_id), None)
            if target_tab and target_tab.get("webSocketDebuggerUrl"):
                break
            time.sleep(0.3)
        if not target_tab:
            raise RuntimeError("隔离的 X-SaaS 标签页未就绪")
        target = CDP(target_tab["webSocketDebuggerUrl"])
        deadline = time.time() + 8
        while time.time() < deadline:
            if evaluate(target, "location.hostname") == XSAAS_HOST:
                break
            time.sleep(0.25)
        keys = ["token", "userinfo", "username", "localSystemInfo", "bPrivate", "localLoginTime"]
        for key in keys:
            value = evaluate(source, f"localStorage.getItem({json_expr(key)})")
            if value is not None:
                evaluate(target, f"localStorage.setItem({json_expr(key)},{json_expr(value)});true")
        evaluate(target, f"location.href='https://{XSAAS_HOST}/?{RUNNER_MARKER}#/app/candidate/list';true")
        return target, target_id
    except Exception:
        if target_id:
            browser.send("Target.closeTarget", {"targetId": target_id})
        raise
    finally:
        source.close()
        browser.close()


def wait_for_list(cdp: CDP) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        state = evaluate(cdp, "({href:location.href,title:document.title,ready:!!document.querySelector('input.search-input[ng-model=\"ngkeyword\"]')})") or {}
        if state.get("ready") and "#/app/candidate/list" in str(state.get("href") or ""):
            return
        if "#/login" in str(state.get("href") or ""):
            raise RuntimeError("X-SAAS_LOGIN_REQUIRED: 隔离标签页未继承登录态")
        time.sleep(0.5)
    raise RuntimeError("X-SaaS 候选人列表加载超时")


SEARCH_JS = r"""
(query => {
  const input = document.querySelector('input.search-input[ng-model="ngkeyword"]');
  const button = document.querySelector('[ng-click="fnQuerySearch();"]');
  if (!input || !button) return {ok:false, reason:'search_controls_missing'};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, query);
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.dispatchEvent(new Event('change', {bubbles:true}));
  button.click();
  return {ok:true};
})
"""


EXTRACT_JS = r"""
(() => {
  const bodyText = document.body ? document.body.innerText || '' : '';
  const countMatch = bodyText.match(/([0-9,]+)条记录/);
  const selected = (bodyText.match(/已选条件\s+关键字：([^\n]+)/) || [])[1] || '';
  const rows = Array.from(document.querySelectorAll('table.candidate-list tbody tr'));
  const candidates = rows.map(row => {
    const scoped = window.angular ? angular.element(row).scope()?.candidate : null;
    const cells = Array.from(row.querySelectorAll('td'));
    const links = Array.from(row.querySelectorAll('a'));
    const profile = links.find(a => /candidate\/info\/(\d+)/.test(a.href || ''));
    const match = profile ? (profile.href || '').match(/candidate\/info\/(\d+)/) : null;
    const first = (cells[0]?.innerText || '').trim().split('\n').map(x => x.trim()).filter(Boolean);
    const second = (cells[1]?.innerText || '').trim().split('\n').map(x => x.trim()).filter(Boolean);
    const jobs = Array.isArray(scoped?.arrJobDetail) ? scoped.arrJobDetail : [];
    const currentJob = jobs.find(item => item?.isnow) || jobs[0] || {};
    const personId = String(scoped?.ipersonid || (match ? match[1] : '') || '');
    const name = String(
      scoped?.sNameView || scoped?.sName || scoped?.sname
      || profile?.innerText || first.find(x => !/^\d+$/.test(x)) || ''
    ).trim();
    const company = String(scoped?.scompany || currentJob.scompanyname || second[0] || '').trim();
    const title = String(scoped?.sposition || currentJob.sposition || currentJob.scompanyposition || second[1] || '').trim();
    const workText = jobs.slice(0, 4).map(item => [
      item?.scompanyname, item?.sposition || item?.scompanyposition, item?.sstart, item?.send
    ].filter(Boolean).join(' · ')).filter(Boolean).join(' | ');
    return {
      channel:'xsaas', xsaas_id:personId, name,
      company, title, profile_text:workText || second.slice(0,4).join(' | '),
      source_url:personId ? `https://headhunt.x-saas.com.cn/#/app/candidate/info/${personId}` : (profile ? profile.href : '')
    };
  }).filter(item => item.xsaas_id && item.name);
  return {selected_query:selected.trim(), result_count:countMatch ? Number(countMatch[1].replace(/,/g,'')) : 0, candidates};
})()
"""


DETAIL_JS = r"""
(() => {
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const root = document.querySelector('.app-candidate-info, .candidate-info, .candidate-detail, [ui-view]') || document.body;
  const lines = (root?.innerText || '').split('\n').map(clean).filter(Boolean).filter(line =>
    !/^(首页|候选人|项目|客户|系统设置|退出登录|返回列表|编辑|保存|取消)$/.test(line) &&
    !/Copyright|ICP备|X-SaaS|loading\.\.\./i.test(line)
  );
  const section = (startRe, endRe) => {
    const start = lines.findIndex(line => startRe.test(line));
    if (start < 0) return '';
    const rest = lines.slice(start + 1);
    const end = rest.findIndex(line => endRe.test(line));
    return (end >= 0 ? rest.slice(0, end) : rest).join('\n').slice(0, 30000);
  };
  const workText = section(/^(工作经历|工作经验|任职经历)$/, /^(项目经历|项目经验|教育经历|教育背景|培训经历|语言能力|技能|自我评价)$/);
  const projectText = section(/^(项目经历|项目经验)$/, /^(教育经历|教育背景|培训经历|语言能力|技能|自我评价)$/);
  const educationText = section(/^(教育经历|教育背景)$/, /^(培训经历|语言能力|技能|自我评价|证书|附件)$/);
  const idMatch = location.href.match(/candidate\/info\/(\d+)/);
  return {
    source_url: location.href,
    xsaas_id: idMatch ? idMatch[1] : '',
    full_text: lines.join('\n').slice(0, 60000),
    work_text: workText,
    project_text: projectText,
    education_text: educationText,
    captured_at: new Date().toISOString()
  };
})()
"""


def capture_candidate_details(cdp: CDP, candidates: list[dict[str, Any]], enabled: bool) -> dict[str, int]:
    stats = {"requested": len(candidates), "complete": 0, "partial": 0, "failed": 0}
    if not enabled:
        return stats
    for candidate in candidates:
        candidate_id = str(candidate.get("xsaas_id") or "").strip()
        if not candidate_id:
            candidate.update({"resume_capture_status": "failed", "resume_capture_missing": ["X-SaaS ID"], "resume_capture_error": "候选人 ID 缺失"})
            stats["failed"] += 1
            continue
        try:
            url = f"https://{XSAAS_HOST}/?{RUNNER_MARKER}#/app/candidate/info/{candidate_id}"
            evaluate(cdp, f"location.href={json_expr(url)};true")
            deadline = time.time() + 18
            ready = False
            while time.time() < deadline:
                state = evaluate(cdp, "({href:location.href,text:(document.body?.innerText||'').length,login:location.href.includes('#/login')})") or {}
                if state.get("login"):
                    raise RuntimeError("X-SAAS_LOGIN_REQUIRED: 详情页登录态失效")
                if f"candidate/info/{candidate_id}" in str(state.get("href") or "") and int(state.get("text") or 0) >= 200:
                    ready = True
                    break
                time.sleep(0.5)
            if not ready:
                raise RuntimeError("X-SaaS 候选人详情页未加载出可读内容")
            detail = evaluate(cdp, DETAIL_JS) or {}
            if str(detail.get("xsaas_id") or "") != candidate_id:
                raise RuntimeError("详情页身份与列表候选人不一致")
            full_text = str(detail.get("full_text") or "").strip()
            work_text = str(detail.get("work_text") or "").strip()
            project_text = str(detail.get("project_text") or "").strip()
            education_text = str(detail.get("education_text") or "").strip()
            missing = []
            if len(full_text) < 100:
                missing.append("完整履历")
            if len(work_text) < 20:
                missing.append("工作经历")
            if len(education_text) < 10:
                missing.append("教育经历")
            candidate.update({
                "profile_text": full_text,
                "full_text": full_text,
                "work_text": work_text,
                "project_text": project_text,
                "education_text": education_text,
                "source_url": str(candidate.get("source_url") or detail.get("source_url") or ""),
                "resume_capture_status": "complete" if not missing else "partial",
                "resume_capture_missing": missing,
                "resume_capture_error": "" if not missing else f"缺少：{'、'.join(missing)}",
                "resume_captured_at": str(detail.get("captured_at") or ""),
            })
            stats[str(candidate["resume_capture_status"])] += 1
        except Exception as exc:
            candidate.update({"resume_capture_status": "failed", "resume_capture_missing": ["完整履历"], "resume_capture_error": str(exc)[:300]})
            stats["failed"] += 1
    return stats


def run_search(port: int, queries: list[str], max_rows: int, capture_details: bool = True) -> dict[str, Any]:
    source = choose_authenticated_tab(port)
    browser_ws = load_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
    browser = CDP(browser_ws)
    cdp = None
    target_id = ""
    try:
        cdp, target_id = clone_authenticated_tab(port, source)
        wait_for_list(cdp)
        rounds = []
        dedup: dict[str, dict[str, Any]] = {}
        for query in queries[:8]:
            query = " ".join(str(query or "").split())
            if not query:
                continue
            started = evaluate(cdp, f"({SEARCH_JS})({json_expr(query)})") or {}
            if not started.get("ok"):
                rounds.append({"query": query, "status": "failed", "reason": started.get("reason")})
                continue
            time.sleep(3.5)
            extracted = evaluate(cdp, EXTRACT_JS) or {}
            selected = str(extracted.get("selected_query") or "")
            status = "completed" if query in selected else "stale_query"
            candidates = extracted.get("candidates") if isinstance(extracted.get("candidates"), list) else []
            for candidate in candidates[:max_rows]:
                candidate["query"] = query
                dedup.setdefault(str(candidate.get("xsaas_id")), candidate)
            rounds.append({"query": query, "status": status, "selected_query": selected, "result_count": int(extracted.get("result_count") or 0), "extracted_count": len(candidates[:max_rows])})
        candidates = list(dedup.values())[:max_rows]
        detail_capture = capture_candidate_details(cdp, candidates, capture_details)
        return {"ok": True, "channel": "xsaas", "rounds": rounds, "detail_capture": detail_capture, "candidates": candidates}
    finally:
        if cdp:
            cdp.close()
        if target_id:
            browser.send("Target.closeTarget", {"targetId": target_id})
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Restricted read-only X-SaaS candidate search")
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--max-rows", type=int, default=30)
    parser.add_argument("--capture-details", dest="capture_details", action="store_true", default=True)
    parser.add_argument("--no-capture-details", dest="capture_details", action="store_false")
    args = parser.parse_args()
    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    values = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise SystemExit("queries 文件必须是数组或包含 queries 数组")
    queries = [item.get("query") if isinstance(item, dict) else item for item in values]
    result = run_search(args.port, [str(item or "") for item in queries], max(1, min(args.max_rows, 100)), capture_details=args.capture_details)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result.get("candidates") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**result, "candidates": len(result.get("candidates") or []), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
