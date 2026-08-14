#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

CDP_DIR = Path(os.environ.get("ASA_CDP_SKILL_DIR", "/Users/messi/.codex/skills/liepin-cdp-search/scripts"))
sys.path.insert(0, str(CDP_DIR))
try:
    from cdp_client import CDP  # noqa: E402
except ImportError:
    # CI/无 skill 环境：模块仍可导入（测试 patch CDP 后运行）；真实调用给出清晰错误。
    class CDP:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "cdp_client 不可用：请安装 liepin-cdp-search skill，"
                "或用 ASA_CDP_SKILL_DIR 指向其 scripts 目录"
            )
from a_system_agent.sourcing_pagination import PageResult, collect_pages, seek_to_page  # noqa: E402


XSAAS_HOST = "headhunt.x-saas.com.cn"
RUNNER_MARKER = "asa_search_runner=1"


def query_execution_spec(value: Any) -> tuple[str, int, int]:
    item = value if isinstance(value, dict) else {"query": value}
    query = " ".join(str(item.get("query") or "").split())
    cursor = item.get("cursor") if isinstance(item.get("cursor"), dict) else {}
    try:
        start_page = max(1, int(cursor.get("page") or 1))
    except (TypeError, ValueError):
        start_page = 1
    try:
        collected_before = max(0, int(item.get("collected_before") or 0))
    except (TypeError, ValueError):
        collected_before = 0
    return query, start_page, collected_before


def query_seen_candidate_keys(value: Any) -> set[str]:
    item = value if isinstance(value, dict) else {}
    return {str(key).strip() for key in item.get("seen_candidate_keys") or [] if str(key).strip()}


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


def close_runner_tabs(port: int) -> int:
    """Close only ASA-owned isolated tabs left behind by killed/expired runners."""
    tabs = load_json(f"http://127.0.0.1:{port}/json/list")
    target_ids = [
        str(tab.get("id") or "")
        for tab in tabs
        if RUNNER_MARKER in str(tab.get("url") or "") and str(tab.get("id") or "")
    ]
    if not target_ids:
        return 0
    browser_ws = load_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
    browser = CDP(browser_ws)
    try:
        for target_id in target_ids:
            browser.send("Target.closeTarget", {"targetId": target_id})
    finally:
        browser.close()
    return len(target_ids)


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
        # 克隆页文档未就绪时 setItem 会"页面脚本执行失败"（round10 实证）：先等 readyState，失败重试一次
        ready_deadline = time.time() + 5
        while time.time() < ready_deadline:
            if evaluate(target, "document.readyState") == "complete":
                break
            time.sleep(0.3)
        for key in keys:
            value = evaluate(source, f"localStorage.getItem({json_expr(key)})")
            if value is not None:
                try:
                    evaluate(target, f"localStorage.setItem({json_expr(key)},{json_expr(value)});true")
                except RuntimeError:
                    time.sleep(1)
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


# 渲染完成信号与本轮关键词绑定（任务卡 UX-1 问题 B）：新标签页默认列表自带"N 条记录"，
# 只看 hasCount && !loading 会在 Angular 提交查询前误判就绪，读到上一轮/默认列表（串词）。
# 必须同时满足：已选条件出现本轮关键词 + 结果计数就绪 + loading 消失。
SETTLE_JS = r"""
(expected => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const bodyText = document.body ? document.body.innerText || '' : '';
  const selected = norm((bodyText.match(/已选条件\s+关键字：([^\n]+)/) || [])[1] || '');
  const want = norm(expected);
  return {
    loading: bodyText.includes('loading...'),
    hasCount: /[0-9,]+条记录/.test(bodyText),
    selected,
    queryMatch: !!want && !!selected && (selected === want || selected.indexOf(want) >= 0),
  };
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
    const schools = Array.isArray(scoped?.arrSchoolDetail) ? scoped.arrSchoolDetail : [];
    const currentJob = jobs.find(item => item?.isnow) || jobs[0] || {};
    const highestSchool = schools[0] || {};
    const personId = String(scoped?.ipersonid || (match ? match[1] : '') || '');
    const name = String(
      scoped?.sNameView || scoped?.sName || scoped?.sname
      || profile?.innerText || first.find(x => !/^\d+$/.test(x)) || ''
    ).trim();
    const company = String(scoped?.scompany || currentJob.scompanyname || second[0] || '').trim();
    const title = String(scoped?.sposition || currentJob.sposition || currentJob.scompanyposition || second[1] || '').trim();
    const city = String(scoped?.scurcity || scoped?.scity || '').trim();
    const education = String(highestSchool.seducation || highestSchool.schoolname || '').trim();
    const experience = (() => { const total = jobs.reduce((sum, j) => { const s = parseInt(j?.sstart); const e = parseInt(j?.send || new Date().getFullYear()); return sum + (isNaN(s) || isNaN(e) ? 0 : Math.max(0, e - s)); }, 0); return total > 0 ? total + '年' : ''; })();
    const workText = jobs.slice(0, 4).map(item => [
      item?.scompanyname, item?.sposition || item?.scompanyposition, item?.sstart, item?.send
    ].filter(Boolean).join(' · ')).filter(Boolean).join(' | ');
    const eduText = schools.slice(0, 2).map(item => [
      item?.schoolname, item?.smajor, item?.seducation, item?.sstart, item?.send
    ].filter(Boolean).join(' · ')).filter(Boolean).join(' | ');
    return {
      channel:'xsaas', xsaas_id:personId, name,
      company, title, city: city || second[4] || '', education, experience,
      profile_text: workText || second.slice(0,4).join(' | '),
      work_text: workText,
      education_text: eduText,
      source_url:personId ? `https://headhunt.x-saas.com.cn/#/app/candidate/info/${personId}` : (profile ? profile.href : '')
    };
  }).filter(item => item.xsaas_id && item.name);
  return {selected_query:selected.trim(), result_count:countMatch ? Number(countMatch[1].replace(/,/g,'')) : 0, candidates};
})()
"""


PAGINATION_STATE_JS = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll(
    '.pagination li, .pagination a, .pagination button, .pager li, .pager a, [ng-click*="selectPage"]'
  ));
  const node = nodes.find(item => {
    const text = String(item.innerText || item.textContent || '').trim();
    const title = String(item.getAttribute('title') || item.getAttribute('aria-label') || '');
    return /^(下一页|next|›|»|>)$/i.test(text) || /下一页|next/i.test(title);
  });
  if (!node) return {hasNext:false, reason:'next_control_missing'};
  const container = node.closest('li') || node;
  const disabled = container.matches('.disabled,:disabled,[disabled]')
    || container.getAttribute('aria-disabled') === 'true';
  return {hasNext:!disabled, reason:disabled ? 'next_disabled' : ''};
})()
"""


NEXT_PAGE_JS = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll(
    '.pagination li, .pagination a, .pagination button, .pager li, .pager a, [ng-click*="selectPage"]'
  ));
  const node = nodes.find(item => {
    const text = String(item.innerText || item.textContent || '').trim();
    const title = String(item.getAttribute('title') || item.getAttribute('aria-label') || '');
    return /^(下一页|next|›|»|>)$/i.test(text) || /下一页|next/i.test(title);
  });
  if (!node) return false;
  const container = node.closest('li') || node;
  const disabled = container.matches('.disabled,:disabled,[disabled]')
    || container.getAttribute('aria-disabled') === 'true';
  if (disabled) return false;
  (node.querySelector('a,button') || node).click();
  return true;
})()
"""


DETAIL_JS = r"""
(() => {
  const clean = value => String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const root = document.querySelector('.app-candidate-info, .candidate-info, .candidate-detail') || document.body;
  const allLines = (root?.innerText || '').split('\n').map(clean).filter(Boolean);
  // 跳过 X-SaaS 平台外壳/菜单行（从页面实测提取），保留候选人内容
  const skipSet = new Set([
    'Amount', 'in dollars', '新增', '订阅收藏', '关闭', 
    '个人资料', '头像/二维码', '个人专注领域', '修改密码', '邮件账户', '邮件签名', '自定义设置',
    '我的桌面', '工作台', '审核中心', '候选人', '热门候选人',
    '联系人', '公司', '职位', '公共池', '职位表单', '发票', '分析报表', '知识库', '插件',
    '首页', '项目', '客户', '系统设置', '退出登录',
    '编辑', '保存', '取消', '返回列表', '重置', '导出', '搜索',
  ]);
  const lines = allLines.filter(line => {
    if (skipSet.has(line)) return false;
    if (/^(金额|Amount|\\d+)$/.test(line.trim())) return false;
    if (/Copyright|ICP备|X-SaaS|loading\.\.\./i.test(line)) return false;
    return line.length >= 3;
  });
  const section = (startRe, endRe) => {
    const start = lines.findIndex(line => startRe.test(line));
    if (start < 0) return '';
    const rest = lines.slice(start + 1);
    const end = rest.findIndex(line => endRe.test(line));
    return (end >= 0 ? rest.slice(0, end) : rest).join('\n').slice(0, 30000);
  };
  const workText = section(/^(工作经历|工作经验|任职经历)$/, /^(项目经历|项目经验|教育经历|教育背景|培训经历|语言能力|技能|自我评价)$/);
  const projectText = section(/^(项目经历|项目经验)$/, /^(教育经历|教育背景|培训经历|语言能力|技能|自我评价)$/);
  const educationText = section(/^(教育经历|教育背景)$/, /^(项目经历|项目经验|培训经历|语言能力|技能|自我评价|证书|附件)$/);
  const idMatch = location.href.match(/candidate\/info\/(\d+)/);
  let scope = null;
  try {
    scope = root && window.angular ? angular.element(root).scope() : null;
  } catch (_) {}
  const candidate = scope?.oCurrentCandidate || {};
  const basicText = lines.join('\n');
  const education = String(candidate?.oDegree?.text || (basicText.match(/最高学历：\s*([^\s(]+)/) || [])[1] || '').trim();
  const city = String(candidate?.oCurrentCity?.text || (basicText.match(/目前城市：\s*([^\s]+)/) || [])[1] || '').trim();
  return {
    source_url: location.href,
    xsaas_id: idMatch ? idMatch[1] : '',
    full_text: lines.join('\n').slice(0, 60000),
    work_text: workText,
    project_text: projectText,
    education_text: educationText,
    company: String(candidate?.oCurrentCompany?.text || '').trim(),
    title: String(candidate?.sCurrentPositionName || '').trim(),
    education,
    city,
    experience: Number(candidate?.iWorkingLife || 0) > 0 ? `${candidate.iWorkingLife}年` : '',
    captured_at: new Date().toISOString()
  };
})()
"""


DETAIL_READY_JS = r"""
(expectedId => {
  const root = document.querySelector('.app-candidate-info, .candidate-info, .candidate-detail');
  const text = root?.innerText || '';
  const bodyText = document.body?.innerText || '';
  let scope = null;
  try {
    scope = root && window.angular ? angular.element(root).scope() : null;
  } catch (_) {}
  const candidate = scope?.oCurrentCandidate || null;
  const candidateId = String(candidate?.iPersonId || '');
  const loading = scope?.bVitaeloading;
  return {
    href: location.href,
    login: location.href.includes('#/login'),
    candidate_id: candidateId,
    loading,
    encrypted: /加密/.test(text) || /加密/.test(bodyText),
    ready: candidateId === String(expectedId)
      && loading === false
      && /基本信息/.test(text)
      && /工作经历|工作经验|任职经历/.test(text)
      && /教育经历|教育背景/.test(text),
  };
})
"""


def capture_candidate_details(cdp: CDP, candidates: list[dict[str, Any]], enabled: bool) -> dict[str, int]:
    stats = {"requested": len(candidates), "complete": 0, "partial": 0, "failed": 0, "skipped_encrypted": 0}
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
            encrypted = False
            while time.time() < deadline:
                state = evaluate(cdp, f"({DETAIL_READY_JS})({json_expr(candidate_id)})") or {}
                if state.get("login"):
                    raise RuntimeError("X-SAAS_LOGIN_REQUIRED: 详情页登录态失效")
                if state.get("encrypted"):
                    encrypted = True
                    break
                if state.get("ready"):
                    ready = True
                    break
                time.sleep(0.5)
            if encrypted:
                candidate.update({
                    "resume_capture_status": "skipped_encrypted",
                    "resume_capture_error": "该候选人处于加密状态，需联系Frank Wang",
                })
                stats["skipped_encrypted"] += 1
                continue
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
            for field in ("company", "title", "education", "city", "experience"):
                value = str(detail.get(field) or "").strip()
                if value:
                    candidate[field] = value
            stats[str(candidate["resume_capture_status"])] += 1
        except Exception as exc:
            candidate.update({"resume_capture_status": "failed", "resume_capture_missing": ["完整履历"], "resume_capture_error": str(exc)[:300]})
            stats["failed"] += 1
    return stats


def query_matches(query: str, selected: str) -> bool:
    """轮次绑定校验：结果集的已选条件必须覆盖本轮关键词（归一化空白后相等或包含）。

    防止串词：页面仍展示上一轮/默认列表时，已选条件不含本轮关键词，该轮结果不得并入。
    """
    want = " ".join(str(query or "").split())
    got = " ".join(str(selected or "").split())
    return bool(want) and bool(got) and (got == want or want in got)


def merge_round_candidates(rounds: list[list[dict[str, Any]]], max_rows: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_depth = max((len(items) for items in rounds), default=0)
    for index in range(max_depth):
        for items in rounds:
            if index >= len(items):
                continue
            candidate = items[index]
            identity = str(candidate.get("xsaas_id") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(candidate)
            if len(merged) >= max_rows:
                return merged
    return merged


def apply_position_score_gate(
    candidates: list[dict[str, Any]], db_path: Path, client: str, job: str, min_score: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    from run_published_position_search import build_db_position_profile, score_candidate_for_profile

    profile = build_db_position_profile(db_path, client, job)
    accepted: list[dict[str, Any]] = []
    scored = 0
    rejected = 0
    for candidate in candidates:
        if candidate.get("resume_capture_status") != "complete":
            accepted.append(candidate)
            continue
        card = {
            "name": candidate.get("name"),
            "current_company": candidate.get("company"),
            "current_title": candidate.get("title"),
            "experience": candidate.get("experience"),
            "education": candidate.get("education"),
            "city": candidate.get("city"),
            "raw_text": candidate.get("full_text") or candidate.get("profile_text"),
            "skills": [],
            "work": [],
        }
        score, evidence, risks, level = score_candidate_for_profile(
            card, getattr(profile, "default_city", None), profile,
        )
        candidate.update({"fit_score": score, "fit_level": level, "evidence": evidence, "risks": risks})
        scored += 1
        if score >= min_score:
            accepted.append(candidate)
        else:
            rejected += 1
    return accepted, {
        "input": len(candidates),
        "scored": scored,
        "accepted": len(accepted),
        "rejected_low_score": rejected,
        "min_score": min_score,
    }


def run_search(
    port: int,
    queries: list[Any],
    max_rows: int,
    capture_details: bool = True,
    max_pages: int = 50,
) -> dict[str, Any]:
    close_runner_tabs(port)
    source = choose_authenticated_tab(port)
    browser_ws = load_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
    browser = CDP(browser_ws)
    cdp = None
    target_id = ""
    try:
        rounds = []
        candidate_rounds: list[list[dict[str, Any]]] = []
        raw_candidates: list[dict[str, Any]] = []
        for index, query_spec in enumerate(queries):
            query, start_page, collected_before = query_execution_spec(query_spec)
            seen_candidate_keys = query_seen_candidate_keys(query_spec)
            query_item = query_spec if isinstance(query_spec, dict) else {}
            evaluation_constraints = (
                query_item.get("evaluation_constraints")
                if isinstance(query_item.get("evaluation_constraints"), dict)
                else {}
            )
            execution_filters = (
                query_item.get("execution_filters")
                if isinstance(query_item.get("execution_filters"), dict)
                else {}
            )
            if execution_filters:
                raise RuntimeError(
                    "XSAAS_UNSUPPORTED_EXECUTION_FILTER: query_plan requested unavailable platform filters"
                )
            if not query:
                continue
            # 每组独立克隆标签页：X-SaaS 是 hash 路由 SPA，已选条件（筛选 chips）保留在页面内存态，
            # hash 跳转/location.reload 均无法可靠清零（round3/5/7/8 四次实证），
            # 只有全新标签页能保证每组在干净条件下执行（第 1 组历来干净即为此理）。
            # 竞态硬门（任务卡 UX-1 问题 B）：渲染完成信号与本轮关键词绑定后才允许读结果；
            # 20s 超时兜底并重试一次（全新标签页重来），仍失败记日志并标记"跳过"，不静默丢失。
            round_entry: dict[str, Any] | None = None
            round_candidates: list[dict[str, Any]] = []
            attempts = 0
            while attempts < 2 and round_entry is None:
                attempts += 1
                if cdp:
                    cdp.close()
                if target_id:
                    browser.send("Target.closeTarget", {"targetId": target_id})
                cdp, target_id = clone_authenticated_tab(port, source)
                wait_for_list(cdp)
                started = evaluate(cdp, f"({SEARCH_JS})({json_expr(query)})") or {}
                if not started.get("ok"):
                    # 搜索控件缺失：重试无意义，直接记录失败（非静默，进 rounds 日志）。
                    round_entry = {"query": query, "status": "failed", "reason": started.get("reason"), "attempts": attempts}
                    break
                # 等待搜索结果渲染完成且属于本轮关键词：固定 sleep/只看计数会读到加载中间态或
                # 默认列表（MPS 165 条实证渲染 >4s），误判 0 召回或把上一轮结果并入本轮。
                settled = False
                deadline = time.time() + 45  # 大结果集渲染实测 >20s（技术市场 round10 实证），放宽到 45s
                while time.time() < deadline:
                    settle_state = evaluate(cdp, f"({SETTLE_JS})({json_expr(query)})") or {}
                    if settle_state.get("queryMatch") and settle_state.get("hasCount") and not settle_state.get("loading"):
                        settled = True
                        break
                    time.sleep(0.8)
                if not settled:
                    if attempts < 2:
                        print(f"[xsaas_candidate_search] 关键词「{query}」第 {attempts} 次等待渲染超时（45s），换新标签页重试一次", file=sys.stderr)
                        continue
                    print(f"[xsaas_candidate_search] 关键词「{query}」重试后仍等待渲染超时，标记跳过（settle_timeout）", file=sys.stderr)
                    round_entry = {"query": query, "status": "skipped", "reason": "settle_timeout", "attempts": attempts}
                    break
                extracted = evaluate(cdp, EXTRACT_JS) or {}
                selected = str(extracted.get("selected_query") or "")
                if not query_matches(query, selected):
                    # 轮次绑定校验失败：结果集不属于本轮关键词（串词），不得并入，重试一次。
                    if attempts < 2:
                        print(f"[xsaas_candidate_search] 关键词「{query}」第 {attempts} 次结果与本轮不匹配（页面已选条件：{selected or '空'}），换新标签页重试一次", file=sys.stderr)
                        continue
                    print(f"[xsaas_candidate_search] 关键词「{query}」重试后结果仍不匹配（页面已选条件：{selected or '空'}），标记跳过（stale_query），结果不并入", file=sys.stderr)
                    round_entry = {"query": query, "status": "stale_query", "selected_query": selected, "attempts": attempts}
                    break
                last_signature = ""

                def fetch_page(page_number: int) -> PageResult:
                    nonlocal last_signature
                    page_payload = evaluate(cdp, EXTRACT_JS) or {}
                    page_selected = str(page_payload.get("selected_query") or "")
                    if not query_matches(query, page_selected):
                        raise RuntimeError("XSAAS_STALE_QUERY_DURING_PAGINATION")
                    page_candidates = (
                        page_payload.get("candidates")
                        if isinstance(page_payload.get("candidates"), list)
                        else []
                    )
                    page_candidates = [
                        candidate for candidate in page_candidates[:max_rows] if isinstance(candidate, dict)
                    ]
                    for position_index, candidate in enumerate(page_candidates, 1):
                        candidate["query"] = query
                        candidate["page_number"] = page_number
                        candidate["position_index"] = position_index
                    last_signature = "|".join(
                        str(candidate.get("xsaas_id") or "") for candidate in page_candidates[:3]
                    )
                    page_total = int(page_payload.get("result_count") or 0)
                    pagination_state = evaluate(cdp, PAGINATION_STATE_JS) or {}
                    return PageResult(
                        items=page_candidates,
                        reported_total=None if page_total == 0 and page_candidates else page_total,
                        has_next=bool(pagination_state.get("hasNext")),
                    )

                def advance_page(_next_page: int) -> bool:
                    nonlocal last_signature
                    previous_signature = last_signature
                    if evaluate(cdp, NEXT_PAGE_JS) is not True:
                        return False
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        time.sleep(0.8)
                        state = evaluate(cdp, f"({SETTLE_JS})({json_expr(query)})") or {}
                        if state.get("loading") or not state.get("queryMatch"):
                            continue
                        next_payload = evaluate(cdp, EXTRACT_JS) or {}
                        next_candidates = (
                            next_payload.get("candidates")
                            if isinstance(next_payload.get("candidates"), list)
                            else []
                        )
                        signature = "|".join(
                            str(candidate.get("xsaas_id") or "")
                            for candidate in next_candidates[:3]
                            if isinstance(candidate, dict)
                        )
                        if signature and signature != previous_signature:
                            last_signature = signature
                            return True
                    return False

                seek_failure = seek_to_page(
                    fetch_page=fetch_page,
                    advance_page=advance_page,
                    start_page=start_page,
                )
                pagination = seek_failure or collect_pages(
                    fetch_page=fetch_page,
                    advance_page=advance_page,
                    start_page=start_page,
                    max_pages=max_pages,
                    collected_before=collected_before,
                    seen_before_keys=seen_candidate_keys,
                    item_key=lambda candidate: str(
                        candidate.get("xsaas_id")
                        or "|".join(
                            str(candidate.get(key) or "").strip().casefold()
                            for key in ("name", "company", "title")
                        )
                    ),
                )
                round_candidates = pagination.items
                round_entry = {
                    "query": query,
                    "status": "completed",
                    "selected_query": selected,
                    "result_count": pagination.reported_total,
                    "extracted_count": len(round_candidates),
                    "unique_count": len({str(item.get("xsaas_id") or "") for item in round_candidates}),
                    "pages_fetched": pagination.pages_fetched,
                    "terminal_state": pagination.terminal_state,
                    "terminal_reason": pagination.terminal_reason,
                    "cursor": pagination.cursor,
                    "attempts": attempts,
                }
            if round_entry is None:  # 防御：循环异常退出也不静默丢失该词
                print(f"[xsaas_candidate_search] 关键词「{query}」未取得结果，标记跳过", file=sys.stderr)
                round_entry = {"query": query, "status": "skipped", "reason": "settle_timeout", "attempts": attempts}
            round_entry["filter_receipt"] = {
                "platform_filters_applied": [],
                "evaluation_only": evaluation_constraints,
            }
            candidate_rounds.append(round_candidates)
            raw_candidates.extend(round_candidates)
            rounds.append(round_entry)
        candidates = merge_round_candidates(candidate_rounds, max_rows)
        detail_capture = capture_candidate_details(cdp, candidates, capture_details)
        return {
            "ok": True,
            "channel": "xsaas",
            "rounds": rounds,
            "detail_capture": detail_capture,
            "candidates": candidates,
            "raw_candidates": raw_candidates,
        }
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
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--max-rows", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--client", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--capture-details", dest="capture_details", action="store_true", default=True)
    parser.add_argument("--no-capture-details", dest="capture_details", action="store_false")
    args = parser.parse_args()
    payload = json.loads(args.queries.read_text(encoding="utf-8"))
    values = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise SystemExit("queries 文件必须是数组或包含 queries 数组")
    queries = [
        item for item in values
        if (isinstance(item, dict) and str(item.get("query") or "").strip())
        or (not isinstance(item, dict) and str(item or "").strip())
    ]
    result = run_search(
        args.port,
        queries,
        max(1, min(args.max_rows, 100)),
        capture_details=args.capture_details,
        max_pages=max(1, args.max_pages),
    )
    if args.db and args.client and args.job:
        result["candidates"], result["score_gate"] = apply_position_score_gate(
            result.get("candidates") or [], args.db, args.client, args.job, args.min_score
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result.get("candidates") or [], ensure_ascii=False, indent=2), encoding="utf-8")
    if args.raw_output:
        args.raw_output.parent.mkdir(parents=True, exist_ok=True)
        args.raw_output.write_text(
            json.dumps(result.get("raw_candidates") or [], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps({**result, "candidates": len(result.get("candidates") or []), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
