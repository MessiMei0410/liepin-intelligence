#!/usr/bin/env python3
"""Generate a simple position action hub on top of the existing dashboard data."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from generate_position_dashboard import (
    DEFAULT_DB,
    DEFAULT_OUTPUT_DIR,
    clean,
    collect_projects,
    connect,
    display_project,
    next_action,
    priority_score,
    risk_flags,
    sort_projects,
)
from position_storage import ensure_position_storage_schema, table_exists


BASE_DIR = Path(__file__).resolve().parents[1]


REPORT_LINKS = [
    ("岗位详情", "猎聘岗位详情页.html"),
    ("客户推荐汇总", "猎聘客户推荐汇总_*.md"),
    ("推荐前校验", "猎聘推荐前校验_*.md"),
    ("下一轮搜索", "猎聘下一轮搜索策略_*.md"),
    ("唤醒机会", "猎聘唤醒机会清单_*.md"),
    ("项目归属", "猎聘项目归属修正_list_*.md"),
]


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def file_href(path: Path | None) -> str:
    if not path:
        return "#"
    try:
        return path.expanduser().resolve().as_uri()
    except ValueError:
        return "#"


def latest_file(output_dir: Path, pattern: str) -> Path | None:
    files = sorted(output_dir.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return files[0] if files else None


def project_anchor(client: str, position: str) -> str:
    raw = f"{clean(client)}-{clean(position)}"
    return "p-" + "".join(ch if ch.isalnum() else "-" for ch in raw)[:80]


def classify_stage(item: dict[str, Any]) -> tuple[str, str, str]:
    if not item["client"] or not item["position"]:
        return ("补归属", "把未定客户/岗位先归到正确项目", "project")
    if item["open_followups"]:
        return ("跟进", f"处理 {item['open_followups']} 个打开待办", "followup")
    if item["positive_feedback"]:
        return ("客户推进", "客户已有正向反馈，推进认可人选", "client")
    if item["negative_feedback"] and not item["positive_feedback"]:
        return ("复盘", "先复盘否决原因，再修正搜索口径", "review")
    if item["a_candidates"] or item["b_candidates"]:
        return ("出推荐", "已有 A/B 人选，可看客户推荐汇总", "recommend")
    if item["gap"] and item["active_candidates"] < 3:
        return ("补搜索", "岗位池偏薄，补一轮定向搜索", "search")
    if item["active_candidates"] and not item["advanced_candidates"]:
        return ("筛人", "从现有人才池筛可推荐人选", "screen")
    if item["search_experiments"] == 0 and item["open_position_rows"]:
        return ("记实验", "下一轮搜索时记录关键词和转化", "experiment")
    return ("观察", "继续观察回复、反馈和池子变化", "observe")


def action_links(item: dict[str, Any], output_dir: Path) -> list[tuple[str, str]]:
    detail = latest_file(output_dir, "猎聘岗位详情页.html")
    detail_href = file_href(detail)
    if detail_href != "#":
        detail_href = f"{detail_href}#{project_anchor(item['client'], item['position'])}"
    links = [("打开岗位详情", detail_href)]
    stage, _reason, key = classify_stage(item)
    mapping = {
        "project": ("修正项目归属", "猎聘项目归属修正_list_*.md"),
        "followup": ("看今日优先", "猎聘今日优先处理人选_*.md"),
        "client": ("看客户反馈", "猎聘客户反馈闭环_*.md"),
        "review": ("看策略修正", "猎聘策略修正规则_*.md"),
        "recommend": ("看推荐汇总", "猎聘客户推荐汇总_*.md"),
        "search": ("看下一轮搜索", "猎聘下一轮搜索策略_*.md"),
        "screen": ("看推荐前校验", "猎聘推荐前校验_*.md"),
        "experiment": ("记录搜索实验", "猎聘搜索实验复盘_*.md"),
        "observe": ("看岗位驾驶舱", "猎聘岗位驾驶舱_*.md"),
    }
    label, pattern = mapping.get(key, ("看岗位驾驶舱", "猎聘岗位驾驶舱_*.md"))
    links.append((label, file_href(latest_file(output_dir, pattern))))
    return links


def usable_projects(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = [
        project for project in sort_projects(projects)
        if clean(project.get("client")) or clean(project.get("position"))
    ]
    return items[:60]


def write_html(output_dir: Path, projects: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "猎聘岗位推进入口.html"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    selected = usable_projects(projects)
    top = selected[:18]
    report_links = "\n".join(
        f'<a href="{esc(file_href(latest_file(output_dir, pattern)))}">{esc(label)}</a>'
        for label, pattern in REPORT_LINKS
    )
    cards = []
    for item in top:
        stage, reason, key = classify_stage(item)
        risks = "、".join(risk_flags(item)[:4]) or "暂无明显风险"
        links = "\n".join(
            f'<a class="mini-link" href="{esc(href)}">{esc(label)}</a>'
            for label, href in action_links(item, output_dir)
        )
        cards.append(
            f"""
            <section class="project-card {esc(key)}">
              <div class="card-head">
                <span class="stage">{esc(stage)}</span>
                <strong>{esc(display_project(item['client'], item['position']))}</strong>
              </div>
              <div class="metrics">
                <span>优先级<b>{esc(priority_score(item))}</b></span>
                <span>人才池<b>{esc(item['active_candidates'])}</b></span>
                <span>A/B<b>{esc(item['a_candidates'] + item['b_candidates'])}</b></span>
                <span>待办<b>{esc(item['open_followups'])}</b></span>
              </div>
              <p>{esc(reason)}</p>
              <p class="muted">风险：{esc(risks)}；系统建议：{esc(next_action(item))}</p>
              <div class="links">{links}</div>
            </section>
            """
        )
    cards_html = "\n".join(cards) or '<section class="project-card"><p>暂无可推进岗位</p></section>'
    text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>猎聘岗位推进入口</title>
  <style>
    :root {{
      --bg:#f6f7f8; --panel:#fff; --text:#18212b; --muted:#66717f; --line:#d9dee5;
      --blue:#285f9f; --green:#24745a; --amber:#9a650f; --red:#a43b3b; --shadow:0 1px 2px rgba(18,28,40,.08);
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-width:320px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.45; }}
    main {{ width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:24px 0 36px; }}
    header {{ display:grid; grid-template-columns:1fr auto; gap:16px; align-items:end; margin-bottom:14px; }}
    h1 {{ margin:0; font-size:28px; letter-spacing:0; }}
    .meta {{ color:var(--muted); font-size:13px; text-align:right; }}
    .quick {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
    .quick a, .mini-link {{ display:inline-flex; align-items:center; min-height:34px; padding:7px 10px; border:1px solid var(--line); border-radius:7px; background:var(--panel); color:var(--text); text-decoration:none; font-size:13px; }}
    .quick a:hover, .mini-link:hover {{ border-color:var(--blue); }}
    .grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
    .project-card {{ min-height:218px; padding:14px; border:1px solid var(--line); border-radius:8px; background:var(--panel); box-shadow:var(--shadow); }}
    .card-head {{ display:grid; grid-template-columns:auto 1fr; gap:8px; align-items:start; }}
    .stage {{ display:inline-flex; min-width:58px; justify-content:center; padding:3px 8px; border-radius:999px; background:#eef3f7; color:var(--blue); font-size:12px; font-weight:700; }}
    .project-card.followup .stage, .project-card.client .stage {{ background:#edf7f2; color:var(--green); }}
    .project-card.search .stage, .project-card.experiment .stage {{ background:#fbf3e4; color:var(--amber); }}
    .project-card.project .stage, .project-card.review .stage {{ background:#f8ecec; color:var(--red); }}
    .card-head strong {{ font-size:16px; overflow-wrap:anywhere; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:6px; margin:12px 0; }}
    .metrics span {{ min-height:50px; padding:7px; border:1px solid var(--line); border-radius:7px; color:var(--muted); font-size:12px; }}
    .metrics b {{ display:block; margin-top:2px; color:var(--text); font-size:18px; }}
    p {{ margin:8px 0; font-size:13px; }}
    .muted {{ color:var(--muted); }}
    .links {{ display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }}
    footer {{ margin-top:16px; color:var(--muted); font-size:12px; }}
    @media (max-width: 980px) {{ .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media (max-width: 640px) {{ main {{ width:calc(100% - 18px); }} header {{ grid-template-columns:1fr; }} .meta {{ text-align:left; }} .grid {{ grid-template-columns:1fr; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>猎聘岗位推进入口</h1>
      <div class="meta">生成 {esc(generated_at)}｜{esc(len(selected))} 个可看项目</div>
    </header>
    <nav class="quick" aria-label="常用报告">{report_links}</nav>
    <section class="grid">{cards_html}</section>
    <footer>本页基于现有岗位驾驶舱数据生成，只做推进判断和入口聚合。</footer>
  </main>
</body>
</html>
"""
    path.write_text(text, encoding="utf-8")
    return path


def write_markdown(output_dir: Path, projects: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘岗位推进入口_{stamp}.md"
    selected = usable_projects(projects)[:40]
    lines = [
        "# 猎聘岗位推进入口",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "| 阶段 | 优先级 | 岗位 | 关键数字 | 为什么 | 下一步 |",
        "|---|---:|---|---|---|---|",
    ]
    if not selected:
        lines.append("| 暂无 | - | - | - | - | - |")
    for item in selected:
        stage, reason, _key = classify_stage(item)
        nums = f"池{item['active_candidates']} / A-B {item['a_candidates'] + item['b_candidates']} / 待办{item['open_followups']} / 回复{item['positive_replies']}"
        lines.append(
            f"| {stage} | {priority_score(item)} | {display_project(item['client'], item['position']).replace('|', '｜')} | "
            f"{nums} | {reason.replace('|', '｜')} | {next_action(item).replace('|', '｜')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a position action hub.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    conn = connect(Path(args.db).expanduser())
    try:
        ensure_position_storage_schema(conn)
        projects, _recent = collect_projects(conn)
    finally:
        conn.close()
    html_report = write_html(output_dir, projects)
    md_report = write_markdown(output_dir, projects)
    print(
        json.dumps(
            {
                "ok": True,
                "projects": len(projects),
                "report": str(html_report),
                "markdown": str(md_report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
