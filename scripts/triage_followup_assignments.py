#!/usr/bin/env python3
"""Triage low-confidence follow-up task project assignments.

This script does not force uncertain tasks into projects. It separates open
follow-ups into safe confirmations, suggested candidates, and manual review so
the recruiting loop can improve without corrupting project ownership.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_PRIVATE_VAULT = Path.home() / "Documents" / "Obsidian Liepin Private Vault"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"

AUTO_START = "<!-- AUTO_SYNC_START -->"
AUTO_END = "<!-- AUTO_SYNC_END -->"
MANUAL_SECTION = "## 人工补充\n\n- \n"
GENERIC_WORDS = {"相关", "岗位", "研发", "工程师", "主管", "技术", "硬件", "电力电子"}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def clean(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u3000", " ").split())


def short(value: Any, limit: int = 90) -> str:
    text = clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def project_label(client: Any, position: Any) -> str:
    client = clean(client)
    position = clean(position)
    if client and position:
        return f"{client}/{position}"
    if client:
        return f"{client}/未定岗位"
    if position:
        return f"未定客户/{position}"
    return "未定客户/未定岗位"


def tokens(text: Any) -> list[str]:
    raw = clean(text).lower()
    parts = re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", raw)
    result: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in GENERIC_WORDS:
            continue
        if len(part) <= 1 and not part.isascii():
            continue
        result.append(part)
    if "fpga" in raw:
        result.append("fpga")
    if "电源" in raw:
        result.append("电源")
    if "电力电子" in raw:
        result.append("电力电子")
    if "硬件" in raw:
        result.append("硬件")
    return list(dict.fromkeys(result))


def load_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            t.id,
            t.candidate_id,
            t.candidate_name,
            t.candidate_company,
            t.client,
            t.position,
            t.inferred_client,
            t.inferred_position,
            t.confirmed_client,
            t.confirmed_position,
            t.match_confidence,
            t.confirmation_status,
            t.task_type,
            t.priority,
            t.lane_tag,
            t.reason,
            t.source_id,
            r.intent,
            r.raw_text,
            c.client AS candidate_client,
            c.position AS candidate_position,
            c.name AS db_candidate_name,
            c.company AS db_candidate_company,
            c.title AS db_candidate_title
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        LEFT JOIN candidates c
            ON t.candidate_id = c.id
        WHERE COALESCE(t.status, 'open') = 'open'
          AND (
              COALESCE(t.confirmation_status, 'unconfirmed') != 'confirmed'
              OR COALESCE(t.match_confidence, '') IN ('', 'low', 'unmatched')
              OR COALESCE(NULLIF(t.confirmed_client, ''), NULLIF(t.inferred_client, ''), NULLIF(t.client, ''), '') = ''
              OR COALESCE(NULLIF(t.confirmed_position, ''), NULLIF(t.inferred_position, ''), NULLIF(t.position, ''), '') = ''
          )
        ORDER BY
            CASE COALESCE(t.match_confidence, 'unmatched')
                WHEN 'unmatched' THEN 0
                WHEN 'low' THEN 1
                ELSE 2
            END,
            t.priority ASC,
            t.id ASC
        """
    ).fetchall()


def load_projects(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    project_counts = Counter()
    for row in conn.execute(
        """
        SELECT client, position, COUNT(*) AS cnt
        FROM candidates
        WHERE COALESCE(client, '') != '' AND COALESCE(position, '') != ''
        GROUP BY client, position
        """
    ):
        project_counts[(clean(row["client"]), clean(row["position"]))] += int(row["cnt"] or 0)
    for row in conn.execute(
        """
        SELECT client, position, COUNT(*) AS cnt
        FROM candidate_intelligence
        WHERE COALESCE(client, '') != '' AND COALESCE(position, '') != ''
        GROUP BY client, position
        """
    ):
        project_counts[(clean(row["client"]), clean(row["position"]))] += int(row["cnt"] or 0)
    for row in conn.execute(
        """
        SELECT client, title AS position, 0 AS cnt
        FROM positions
        WHERE COALESCE(status, 'open') = 'open'
        """
    ):
        project_counts.setdefault((clean(row["client"]), clean(row["position"])), 0)

    projects: list[dict[str, Any]] = []
    for (client, position), count in project_counts.items():
        projects.append(
            {
                "client": client,
                "position": position,
                "label": project_label(client, position),
                "count": count,
                "tokens": tokens(f"{client} {position}"),
            }
        )
    return sorted(projects, key=lambda item: (item["client"], item["position"]))


def current_project(task: sqlite3.Row) -> tuple[str, str]:
    client = (
        clean(task["confirmed_client"])
        or clean(task["inferred_client"])
        or clean(task["client"])
        or clean(task["candidate_client"])
    )
    position = (
        clean(task["confirmed_position"])
        or clean(task["inferred_position"])
        or clean(task["position"])
        or clean(task["candidate_position"])
    )
    return client, position


def is_generic_project(client: str, position: str) -> bool:
    if not client:
        return True
    if not position:
        return True
    return any(marker in position for marker in ["相关岗位", "未定岗位"])


def score_project(task: sqlite3.Row, project: dict[str, Any]) -> int:
    task_text = " ".join(
        [
            clean(task["inferred_position"]),
            clean(task["position"]),
            clean(task["raw_text"]),
            clean(task["reason"]),
            clean(task["db_candidate_title"]),
        ]
    )
    task_tokens = set(tokens(task_text))
    score = 0
    for token in project["tokens"]:
        if token in task_tokens:
            score += 3 if token.isascii() else 2
    if clean(task["inferred_position"]) and clean(task["inferred_position"]) in project["position"]:
        score += 6
    if clean(task["candidate_client"]) and clean(task["candidate_client"]) == project["client"]:
        score += 8
    if clean(task["candidate_position"]) and clean(task["candidate_position"]) == project["position"]:
        score += 10
    if score > 0 and project["count"]:
        score += min(5, int(project["count"]) // 10)
    return score


def suggest_projects(task: sqlite3.Row, projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for project in projects:
        score = score_project(task, project)
        if score >= 3:
            item = dict(project)
            item["score"] = score
            scored.append(item)
    scored.sort(key=lambda item: (item["score"], item["count"]), reverse=True)
    return scored[:5]


def classify(task: sqlite3.Row, projects: list[dict[str, Any]]) -> dict[str, Any]:
    client, position = current_project(task)
    suggestions = suggest_projects(task, projects)
    safe_project = ""
    status = "manual_required"
    reason = "缺少可用于确认客户/岗位的稳定信息"

    if client and position and not is_generic_project(client, position):
        safe_project = project_label(client, position)
        status = "safe_confirm"
        reason = "任务或候选人记录已有明确客户和岗位"
    elif len(suggestions) == 1 and suggestions[0]["score"] >= 10:
        safe_project = suggestions[0]["label"]
        status = "safe_confirm_candidate"
        reason = "只有一个高分候选项目，可人工快速复核后确认"
    elif suggestions:
        status = "has_suggestions"
        reason = "存在候选项目，但不唯一或分数不够高"

    return {
        "task": task,
        "status": status,
        "reason": reason,
        "safe_project": safe_project,
        "suggestions": suggestions,
    }


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [short(cell, 120).replace("|", "\\|") if cell not in (None, "") else "未填" for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_report(items: list[dict[str, Any]]) -> str:
    counts = Counter(item["status"] for item in items)
    suggestion_rows = []
    manual_rows = []
    safe_rows = []

    for item in items:
        task = item["task"]
        suggestions = "；".join(
            f"{suggestion['label']}({suggestion['score']})" for suggestion in item["suggestions"][:3]
        )
        row = [
            task["id"],
            clean(task["candidate_name"]) or clean(task["db_candidate_name"]),
            project_label(*current_project(task)),
            clean(task["match_confidence"]) or "unmatched",
            clean(task["task_type"]),
            clean(task["lane_tag"]),
            item["reason"],
            suggestions,
        ]
        if item["status"].startswith("safe"):
            safe_rows.append(row)
        elif item["status"] == "has_suggestions":
            suggestion_rows.append(row)
        else:
            manual_rows.append(row)

    return "\n".join(
        [
            f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
            "",
            "## 总体结论",
            "",
            f"- 待归属打开待办：{len(items)}",
            f"- 可安全确认/快速复核：{counts['safe_confirm'] + counts['safe_confirm_candidate']}",
            f"- 有候选项目但需人工选择：{counts['has_suggestions']}",
            f"- 必须人工补信息：{counts['manual_required']}",
            "",
            "## 处理原则",
            "",
            "- 不把只有泛岗位方向的待办强行归入客户项目。",
            "- 只有客户和岗位同时明确，或唯一高分候选项目时，才进入快速复核。",
            "- 其余待办保留在人工确认清单，避免污染主库。",
            "",
            "## 可安全确认/快速复核",
            "",
            md_table(["待办", "人选", "当前项目", "置信", "类型", "分层", "原因", "候选项目"], safe_rows) if safe_rows else "- 暂无",
            "",
            "## 有候选项目但需人工选择",
            "",
            md_table(["待办", "人选", "当前项目", "置信", "类型", "分层", "原因", "候选项目"], suggestion_rows) if suggestion_rows else "- 暂无",
            "",
            "## 必须人工补信息",
            "",
            md_table(["待办", "人选", "当前项目", "置信", "类型", "分层", "原因", "候选项目"], manual_rows[:80]) if manual_rows else "- 暂无",
            "",
            "## 下一步",
            "",
            "1. 先处理“有候选项目但需人工选择”的少量待办。",
            "2. 对“必须人工补信息”的待办，优先补客户/岗位两个字段，不急着改候选人主状态。",
            "3. 确认后运行 `confirm_project_assignment.py` 写入，再跑一键刷新。",
        ]
    )


def frontmatter(title: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return "\n".join(
        [
            "---",
            f'title: "{title}"',
            f"created: {today}",
            f"updated: {today}",
            "type: review",
            "tags: [liepin, private, followup, assignment]",
            "---",
        ]
    )


def replace_auto_block(path: Path, title: str, body: str, dry_run: bool) -> None:
    block = f"{AUTO_START}\n{body.rstrip()}\n{AUTO_END}"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if AUTO_START in existing and AUTO_END in existing:
            prefix, rest = existing.split(AUTO_START, 1)
            _, suffix = rest.split(AUTO_END, 1)
            content = prefix.rstrip() + "\n\n" + block + suffix
        else:
            content = existing.rstrip() + "\n\n" + block + "\n"
    else:
        content = f"{frontmatter(title)}\n\n# {title}\n\n{block}\n\n{MANUAL_SECTION}"
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def write_outputs(body: str, output_dir: Path, private_vault: Path, dry_run: bool) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"猎聘待办归属分流_{stamp}.md"
    private_path = private_vault / "60_Reviews" / "待办归属分流.md"
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# 猎聘待办归属分流\n\n" + body.rstrip() + "\n", encoding="utf-8")
    replace_auto_block(private_path, "待办归属分流", body, dry_run=dry_run)
    return output_path, private_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Triage low-confidence follow-up task project assignments.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--private-vault", default=str(DEFAULT_PRIVATE_VAULT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        tasks = load_tasks(conn)
        projects = load_projects(conn)
        items = [classify(task, projects) for task in tasks]
    finally:
        conn.close()

    body = render_report(items)
    output_path, private_path = write_outputs(
        body,
        Path(args.output_dir).expanduser(),
        Path(args.private_vault).expanduser(),
        args.dry_run,
    )
    counts = Counter(item["status"] for item in items)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "total": len(items),
                "safe": counts["safe_confirm"] + counts["safe_confirm_candidate"],
                "suggested": counts["has_suggestions"],
                "manual": counts["manual_required"],
                "report": str(output_path),
                "private_note": str(private_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
