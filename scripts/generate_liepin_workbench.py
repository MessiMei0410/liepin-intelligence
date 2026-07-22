#!/usr/bin/env python3
"""Generate a local HTML workbench for the Liepin intelligence workflow."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from generate_next_search_strategy import (
    build_strategy_item,
    load_feedback_examples,
    load_position_profiles,
    load_recent_experiment_notes,
    load_strategy_corrections,
)
from generate_position_dashboard import DEFAULT_DB, DEFAULT_OUTPUT_DIR, clean, collect_projects, connect
from generate_workflow_status_report import collect_metrics


BASE_DIR = Path(__file__).resolve().parents[1]
ACTION_FILES = [
    ("刷新数据", BASE_DIR / "刷新猎聘智能.command", "重新生成所有报告"),
    ("客户反馈", BASE_DIR / "记录客户反馈.command", "认可、否决、面试、offer"),
    ("搜索实验", BASE_DIR / "记录猎聘搜索实验.command", "关键词、筛选、转化"),
    ("项目归属", BASE_DIR / "修正猎聘项目归属.command", "客户和岗位修正"),
]
REPORT_SPECS = [
    ("今日优先", "猎聘今日优先处理人选_*.md"),
    ("岗位推进入口", "猎聘岗位推进入口.html"),
    ("岗位驾驶舱", "猎聘岗位驾驶舱_*.md"),
    ("岗位详情页", "猎聘岗位详情页.html"),
    ("下一轮搜索", "猎聘下一轮搜索策略_*.md"),
    ("推荐前校验", "猎聘推荐前校验_*.md"),
    ("客户推荐汇总", "猎聘客户推荐汇总_*.md"),
    ("唤醒机会", "猎聘唤醒机会清单_*.md"),
    ("客户反馈", "猎聘客户反馈闭环_*.md"),
    ("画像标准化", "猎聘画像标准化_*.md"),
    ("项目归属", "猎聘项目归属修正_list_*.md"),
    ("回复驾驶舱", "猎聘回复智能驾驶舱_*.md"),
    ("话术学习器", "猎聘话术学习器_*.md"),
    ("话术草稿", "猎聘话术算法草稿_applied_*.md"),
    ("可发送话术", "猎聘分层可直接发送话术_*.md"),
    ("快推卡片", "猎聘今日7条快推卡片_*.md"),
    ("话术质量", "猎聘话术质量报告_*.md"),
    ("搜索复盘", "猎聘搜索实验复盘_*.md"),
    ("策略修正", "猎聘策略修正规则_*.md"),
    ("主线状态", "猎聘全流程主任务树推进报告_*.md"),
]


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def file_href(path: Path) -> str:
    try:
        return path.expanduser().resolve().as_uri()
    except ValueError:
        return "#"


def latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = sorted(output_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None


def fmt_time(path: Path | None) -> str:
    if path is None:
        return "暂无"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")


def metric(metrics: dict[str, Any], key: str) -> int:
    return int(metrics.get("counts", {}).get(key, 0) or 0)


def top_search_items(conn: sqlite3.Connection, limit: int = 6) -> list[dict[str, Any]]:
    projects, _recent = collect_projects(conn)
    feedback_examples = load_feedback_examples(conn)
    experiment_notes = load_recent_experiment_notes(conn)
    strategy_corrections = load_strategy_corrections(conn)
    position_profiles = load_position_profiles(conn)
    items = [
        build_strategy_item(project, feedback_examples, experiment_notes, strategy_corrections, position_profiles)
        for project in projects
        if clean(project.get("client"))
        and clean(project.get("position"))
        and (
            int(project.get("open_position_rows") or 0)
            or int(project.get("search_experiments") or 0)
            or int(project.get("outreach_events") or 0)
        )
    ]
    return sorted(items, key=lambda item: item["score"], reverse=True)[:limit]


def load_latest_reports(output_dir: Path) -> list[dict[str, str]]:
    reports = []
    for label, pattern in REPORT_SPECS:
        path = latest_file(output_dir, pattern)
        reports.append(
            {
                "label": label,
                "name": path.name if path else "暂无",
                "time": fmt_time(path),
                "href": file_href(path) if path else "#",
                "exists": "1" if path else "0",
            }
        )
    return reports


def risk_items(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    counts = metrics["counts"]
    items: list[tuple[str, str]] = []
    if metrics["open_followups"]:
        items.append(("打开待办", f"{metrics['open_followups']} 个"))
    if metrics["high_confidence_tasks"]:
        items.append(("高/中置信待办", f"{metrics['high_confidence_tasks']} 个"))
    if counts["search_experiments"] == 0:
        items.append(("搜索实验", "暂无记录"))
    if counts["client_feedback_events"] == 0:
        items.append(("客户反馈", "暂无记录"))
    if counts["outreach_events"] == 0:
        items.append(("触达事件", "暂无记录"))
    return items[:6]


def write_html(
    output_dir: Path,
    metrics: dict[str, Any],
    search_items: list[dict[str, Any]],
    reports: list[dict[str, str]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "猎聘智能工作台.html"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cards = [
        ("候选人", metric(metrics, "candidates"), "本地人才库"),
        ("岗位", metric(metrics, "positions"), "本地岗位池"),
        ("打开待办", metrics["open_followups"], "仍需处理"),
        ("正向回复", metrics["positive_replies"], "可继续沟通"),
        ("客户反馈", metric(metrics, "client_feedback_events"), "正负样本"),
        ("搜索实验", metric(metrics, "search_experiments"), "搜索复盘"),
        ("触达事件", metric(metrics, "outreach_events"), "推荐/填入"),
        ("智能画像", metric(metrics, "candidate_intelligence"), "匹配评分"),
    ]
    action_cards = "\n".join(
        f"""
        <a class="action" href="{esc(file_href(path))}">
          <span class="action-title">{esc(label)}</span>
          <span class="action-copy">{esc(desc)}</span>
        </a>
        """
        for label, path, desc in ACTION_FILES
    )
    metric_cards = "\n".join(
        f"""
        <section class="metric">
          <span>{esc(label)}</span>
          <strong>{esc(value)}</strong>
          <small>{esc(note)}</small>
        </section>
        """
        for label, value, note in cards
    )
    search_rows = "\n".join(
        f"""
        <tr>
          <td>{esc(item['score'])}</td>
          <td>{esc(item['project'])}</td>
          <td>{esc(item['mode'])}</td>
          <td>{esc("、".join(item['keywords'][:4]))}</td>
        </tr>
        """
        for item in search_items
    ) or '<tr><td colspan="4">暂无需要立刻补搜的岗位</td></tr>'
    report_rows = "\n".join(
        f"""
        <a class="report {'missing' if item['exists'] == '0' else ''}" href="{esc(item['href'])}">
          <span>{esc(item['label'])}</span>
          <strong>{esc(item['time'])}</strong>
        </a>
        """
        for item in reports
    )
    risk_rows = "\n".join(
        f"<li><span>{esc(label)}</span><strong>{esc(value)}</strong></li>"
        for label, value in risk_items(metrics)
    ) or "<li><span>状态</span><strong>暂无明显提醒</strong></li>"

    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>猎聘智能工作台</title>
  <style>
    :root {{
      --bg: #f6f7f8;
      --panel: #ffffff;
      --text: #18212b;
      --muted: #66717f;
      --line: #d9dee5;
      --blue: #285f9f;
      --green: #24745a;
      --amber: #9a650f;
      --red: #a43b3b;
      --shadow: 0 1px 2px rgba(18, 28, 40, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.45;
    }}
    main {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 24px 0 36px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 16px;
      align-items: end;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      letter-spacing: 0;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      text-align: right;
      white-space: nowrap;
    }}
    .actions {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .action {{
      display: block;
      min-height: 76px;
      padding: 14px;
      color: var(--text);
      text-decoration: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .action:hover {{ border-color: var(--blue); }}
    .action-title {{
      display: block;
      font-weight: 700;
      font-size: 16px;
    }}
    .action-copy {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .metric {{
      min-height: 92px;
      padding: 12px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .metric span, .metric small {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .metric strong {{
      display: block;
      margin: 6px 0 5px;
      font-size: 24px;
      letter-spacing: 0;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.65fr) minmax(280px, 0.85fr);
      gap: 14px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .panel h2 {{
      margin: 0;
      padding: 14px 16px 10px;
      font-size: 17px;
      letter-spacing: 0;
      border-bottom: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: #fbfbfc;
    }}
    th:first-child, td:first-child {{ width: 72px; }}
    th:nth-child(3), td:nth-child(3) {{ width: 130px; }}
    .side {{
      display: grid;
      gap: 14px;
      align-content: start;
    }}
    .reports {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      padding: 12px;
    }}
    .report {{
      min-height: 58px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--text);
      text-decoration: none;
    }}
    .report:hover {{ border-color: var(--green); }}
    .report span, .report strong {{ display: block; }}
    .report span {{ font-size: 13px; color: var(--muted); }}
    .report strong {{ margin-top: 5px; font-size: 14px; }}
    .report.missing {{ opacity: 0.55; }}
    .alerts {{
      margin: 0;
      padding: 10px 14px 14px;
      list-style: none;
    }}
    .alerts li {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
    }}
    .alerts li:last-child {{ border-bottom: 0; }}
    .alerts span {{ color: var(--muted); }}
    .alerts strong {{ text-align: right; }}
    footer {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 920px) {{
      main {{ width: min(100% - 20px, 760px); padding-top: 18px; }}
      header {{ grid-template-columns: 1fr; }}
      .meta {{ text-align: left; white-space: normal; }}
      .actions {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .metrics {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
      .layout {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 560px) {{
      main {{ width: calc(100% - 16px); }}
      h1 {{ font-size: 24px; }}
      .actions {{ grid-template-columns: 1fr; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .reports {{ grid-template-columns: 1fr; }}
      th:nth-child(3), td:nth-child(3) {{ width: auto; }}
      table {{ font-size: 12px; }}
      th, td {{ padding: 9px 8px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>猎聘智能工作台</h1>
      <div class="meta">生成 {esc(generated_at)}</div>
    </header>
    <nav class="actions" aria-label="常用动作">
      {action_cards}
    </nav>
    <section class="metrics" aria-label="关键数字">
      {metric_cards}
    </section>
    <section class="layout">
      <section class="panel">
        <h2>下一轮搜索</h2>
        <table>
          <thead>
            <tr><th>分</th><th>项目</th><th>模式</th><th>关键词</th></tr>
          </thead>
          <tbody>{search_rows}</tbody>
        </table>
      </section>
      <aside class="side">
        <section class="panel">
          <h2>最新报告</h2>
          <div class="reports">{report_rows}</div>
        </section>
        <section class="panel">
          <h2>提醒</h2>
          <ul class="alerts">{risk_rows}</ul>
        </section>
      </aside>
    </section>
    <footer>本地文件：{esc(str(path))}</footer>
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the local Liepin intelligence workbench.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    conn = connect(Path(args.db).expanduser())
    try:
        metrics = collect_metrics(conn)
        search_items = top_search_items(conn)
        reports = load_latest_reports(output_dir)
    finally:
        conn.close()

    report = write_html(output_dir, metrics, search_items, reports)
    print(
        json.dumps(
            {
                "ok": True,
                "report": str(report),
                "search_items": len(search_items),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
