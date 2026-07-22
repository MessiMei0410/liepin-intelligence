#!/usr/bin/env python3
"""Generate position detail pages that gather the full Liepin workflow by project."""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from generate_position_dashboard import DEFAULT_DB, DEFAULT_OUTPUT_DIR, clean, collect_projects, priority_score, sort_projects
from position_storage import (
    ensure_position_storage_schema,
    fetch_latest_position_snapshot,
    fetch_position_assets,
    fetch_position_snapshots,
    table_exists,
)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def project_id(client: str, position: str) -> str:
    raw = f"{client}-{position}"
    return "p-" + "".join(ch if ch.isalnum() else "-" for ch in raw)[:80]


def project_label(client: str, position: str) -> str:
    if client and position:
        return f"{client}/{position}"
    return client or position or "未定项目"


def parse_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [clean(item) for item in value if clean(item)] if isinstance(value, list) else []


def load_project_data(conn: sqlite3.Connection, client: str, position: str) -> dict[str, list[sqlite3.Row]]:
    params = (client, position)
    return {
        "candidates": rows(
            conn,
            """
            SELECT candidate_name, candidate_company, fit_score, fit_level, next_action
            FROM candidate_intelligence
            WHERE client = ? AND position = ?
            ORDER BY fit_score DESC, updated_at DESC
            LIMIT 8
            """,
            params,
        ),
        "tasks": rows(
            conn,
            """
            SELECT id, candidate_name, task_type, priority, reason, status
            FROM followup_tasks
            WHERE COALESCE(NULLIF(confirmed_client,''), NULLIF(inferred_client,''), NULLIF(client,''), '') = ?
              AND COALESCE(NULLIF(confirmed_position,''), NULLIF(inferred_position,''), NULLIF(position,''), '') = ?
              AND COALESCE(status, 'open') = 'open'
            ORDER BY priority ASC, id DESC
            LIMIT 8
            """,
            params,
        ),
        "feedback": rows(
            conn,
            """
            SELECT candidate_name, feedback_type, status_after, feedback_detail, next_action, feedback_time
            FROM client_feedback_events
            WHERE client = ? AND position = ?
            ORDER BY datetime(feedback_time) DESC, id DESC
            LIMIT 8
            """,
            params,
        ) if table_exists(conn, "client_feedback_events") else [],
        "search": rows(
            conn,
            """
            SELECT id, query, result_count, viewed_count, extracted_count,
                   recommended_count, reply_count, positive_reply_count, status
            FROM search_experiments
            WHERE client = ? AND position = ?
            ORDER BY datetime(COALESCE(updated_at, run_time)) DESC, id DESC
            LIMIT 8
            """,
            params,
        ) if table_exists(conn, "search_experiments") else [],
        "corrections": rows(
            conn,
            """
            SELECT promote_keywords_json, suppress_keywords_json, target_tags_json,
                   blocker_tags_json, evidence_json
            FROM strategy_corrections
            WHERE client = ? AND position = ?
            LIMIT 1
            """,
            params,
        ) if table_exists(conn, "strategy_corrections") else [],
        "profile": rows(
            conn,
            """
            SELECT education_requirement, experience_requirement, ability_keywords_json,
                   hard_requirements_json, search_keywords_json, exclusion_tags_json
            FROM position_profiles
            WHERE client = ? AND position = ?
            LIMIT 1
            """,
            params,
        ) if table_exists(conn, "position_profiles") else [],
        "snapshots": fetch_position_snapshots(conn, client, position, limit=4),
        "assets": fetch_position_assets(conn, client, position, limit=10),
    }


def mini_table(headers: list[str], body: str, empty_cols: int) -> str:
    if not body:
        body = f"<tr><td colspan='{empty_cols}'>暂无</td></tr>"
    head = "".join(f"<th>{esc(item)}</th>" for item in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_project(conn: sqlite3.Connection, project: dict[str, Any]) -> str:
    client = clean(project["client"])
    position = clean(project["position"])
    data = load_project_data(conn, client, position)
    pid = project_id(client, position)
    profile = data["profile"][0] if data["profile"] else None
    correction = data["corrections"][0] if data["corrections"] else None

    candidate_rows = "".join(
        f"<tr><td>{esc(row['fit_score'])}</td><td>{esc(row['candidate_name'])}</td><td>{esc(row['candidate_company'])}</td><td>{esc(row['fit_level'])}</td><td>{esc(row['next_action'])}</td></tr>"
        for row in data["candidates"]
    )
    task_rows = "".join(
        f"<tr><td>#{esc(row['id'])}</td><td>{esc(row['candidate_name'])}</td><td>{esc(row['task_type'])}</td><td>{esc(row['priority'])}</td><td>{esc(row['reason'])}</td></tr>"
        for row in data["tasks"]
    )
    feedback_rows = "".join(
        f"<tr><td>{esc(row['candidate_name'])}</td><td>{esc(row['feedback_type'])}</td><td>{esc(row['status_after'])}</td><td>{esc(row['feedback_detail'])}</td><td>{esc(row['next_action'])}</td></tr>"
        for row in data["feedback"]
    )
    search_rows = "".join(
        f"<tr><td>#{esc(row['id'])}</td><td>{esc(row['query'])}</td><td>{esc(row['viewed_count'])}/{esc(row['result_count'])}</td><td>{esc(row['extracted_count'])}</td><td>{esc(row['recommended_count'])}/{esc(row['reply_count'])}/{esc(row['positive_reply_count'])}</td></tr>"
        for row in data["search"]
    )
    ability = "、".join(parse_list(profile["ability_keywords_json"])) if profile else "暂无"
    hard = "、".join(parse_list(profile["hard_requirements_json"])[:6]) if profile else "暂无"
    search_keywords = "、".join(parse_list(profile["search_keywords_json"])[:8]) if profile else "暂无"
    promote = "、".join(parse_list(correction["promote_keywords_json"])[:8]) if correction else "暂无"
    suppress = "、".join(parse_list(correction["suppress_keywords_json"])[:8]) if correction else "暂无"
    blockers = "、".join(parse_list(correction["blocker_tags_json"])[:8]) if correction else "暂无"
    snapshot_rows = "".join(
        f"<tr><td>{esc(row['captured_at'])}</td><td>{esc(row['source_type'])}</td><td>{esc(row['source_ref'])}</td><td>{esc(row['source_title'])}</td><td>{esc(row['raw_text'][:120])}</td></tr>"
        for row in data["snapshots"]
    )
    asset_rows = "".join(
        f"<tr><td>{esc(row['asset_type'])}</td><td>{esc(row['asset_title'])}</td><td>{esc(row['file_path'])}</td><td>{esc(row['asset_summary'])}</td></tr>"
        for row in data["assets"]
    )

    return f"""
    <section class="project" id="{esc(pid)}">
      <h2>{esc(project_label(client, position))}</h2>
      <div class="stats">
        <span>优先级 <strong>{esc(priority_score(project))}</strong></span>
        <span>缺口 <strong>{esc(project['gap'])}</strong></span>
        <span>人才池 <strong>{esc(project['active_candidates'])}</strong></span>
        <span>待办 <strong>{esc(project['open_followups'])}</strong></span>
        <span>A/B <strong>{esc(project['a_candidates'] + project['b_candidates'])}</strong></span>
        <span>反馈 <strong>{esc(project['positive_feedback'])}/{esc(project['negative_feedback'])}</strong></span>
      </div>
      <div class="profile">
        <p><b>岗位能力：</b>{esc(ability)}</p>
        <p><b>硬性门槛：</b>{esc(hard)}</p>
        <p><b>学历/年限：</b>{esc(profile['education_requirement']) if profile else '暂无'} / {esc(profile['experience_requirement']) if profile else '暂无'}</p>
        <p><b>搜索关键词：</b>{esc(search_keywords)}</p>
        <p><b>策略强化：</b>{esc(promote)} <b>降权：</b>{esc(suppress)} <b>阻力：</b>{esc(blockers)}</p>
      </div>
      <h3>重点人选</h3>
      {mini_table(['分','人选','公司','等级','下一步'], candidate_rows, 5)}
      <h3>打开待办</h3>
      {mini_table(['待办','人选','类型','优先级','原因'], task_rows, 5)}
      <h3>客户反馈</h3>
      {mini_table(['人选','类型','状态','反馈','下一步'], feedback_rows, 5)}
      <h3>搜索实验</h3>
      {mini_table(['编号','关键词','查看/结果','入库','推荐/回复/正向'], search_rows, 5)}
      <h3>岗位快照</h3>
      {mini_table(['时间','来源','引用','标题','原文摘录'], snapshot_rows, 5)}
      <h3>岗位资产</h3>
      {mini_table(['类型','标题','路径','摘要'], asset_rows, 4)}
    </section>
    """


def write_html(conn: sqlite3.Connection, projects: list[dict[str, Any]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "猎聘岗位详情页.html"
    selected = [
        project for project in sort_projects(projects)
        if clean(project["client"]) and clean(project["position"])
    ][:30]
    nav = "\n".join(
        f"<a href='#{esc(project_id(clean(project['client']), clean(project['position'])))}'>{esc(project_label(clean(project['client']), clean(project['position'])))}</a>"
        for project in selected
    )
    content = "\n".join(render_project(conn, project) for project in selected)
    text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>猎聘岗位详情页</title>
  <style>
    body {{ margin:0; background:#f6f7f8; color:#18212b; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.45; }}
    main {{ width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:24px 0 36px; }}
    header {{ display:flex; justify-content:space-between; gap:16px; align-items:end; margin-bottom:16px; }}
    h1 {{ margin:0; font-size:28px; letter-spacing:0; }}
    .meta {{ color:#66717f; font-size:13px; }}
    .layout {{ display:grid; grid-template-columns:260px minmax(0,1fr); gap:14px; }}
    nav {{ position:sticky; top:12px; align-self:start; display:grid; gap:7px; }}
    nav a {{ display:block; padding:9px 10px; background:#fff; border:1px solid #d9dee5; border-radius:7px; color:#18212b; text-decoration:none; font-size:13px; overflow-wrap:anywhere; }}
    .project {{ background:#fff; border:1px solid #d9dee5; border-radius:8px; margin-bottom:14px; padding:16px; box-shadow:0 1px 2px rgba(18,28,40,.08); }}
    h2 {{ margin:0 0 12px; font-size:20px; letter-spacing:0; }}
    h3 {{ margin:18px 0 8px; font-size:15px; letter-spacing:0; }}
    .stats {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; margin-bottom:12px; }}
    .stats span {{ min-height:54px; padding:8px; border:1px solid #d9dee5; border-radius:7px; color:#66717f; font-size:12px; }}
    .stats strong {{ display:block; color:#18212b; font-size:20px; margin-top:3px; }}
    .profile {{ padding:10px 12px; background:#fbfbfc; border:1px solid #d9dee5; border-radius:7px; }}
    .profile p {{ margin:5px 0; font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; table-layout:fixed; font-size:13px; }}
    th, td {{ padding:9px 8px; border-bottom:1px solid #d9dee5; text-align:left; vertical-align:top; overflow-wrap:anywhere; }}
    th {{ color:#66717f; background:#fbfbfc; }}
    @media (max-width: 860px) {{ main {{ width:calc(100% - 18px); }} header {{ display:block; }} .layout {{ grid-template-columns:1fr; }} nav {{ position:static; grid-template-columns:1fr 1fr; }} .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media (max-width: 520px) {{ nav {{ grid-template-columns:1fr; }} table {{ font-size:12px; }} th,td {{ padding:8px 6px; }} }}
  </style>
</head>
<body>
  <main>
    <header><h1>猎聘岗位详情页</h1><div class="meta">生成 {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div></header>
    <section class="layout">
      <nav>{nav or '<span>暂无岗位</span>'}</nav>
      <div>{content or '<section class="project">暂无岗位详情</section>'}</div>
    </section>
  </main>
</body>
</html>
"""
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Liepin position detail pages.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_position_storage_schema(conn)
        projects, _recent = collect_projects(conn)
        report = write_html(conn, projects, Path(args.output_dir).expanduser())
    finally:
        conn.close()
    print(json.dumps({"ok": True, "projects": len(projects), "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
