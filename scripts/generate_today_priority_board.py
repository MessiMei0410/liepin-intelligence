#!/usr/bin/env python3
"""Generate a daily priority board for Liepin follow-up candidates."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row[1]) for row in rows}


def column_expr(columns: set[str], table_alias: str, column: str, fallback: str = "''") -> str:
    if column in columns:
        return f"ifnull({table_alias}.{column}, '')"
    return fallback


def safe_int(value: object, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    followup_task_columns = table_columns(conn, "followup_tasks")
    reply_columns = table_columns(conn, "candidate_replies")
    task_position_expr = (
        "ifnull(t.inferred_position, ifnull(t.position, ''))"
        if "inferred_position" in followup_task_columns
        else "ifnull(t.position, '')"
    )
    query = f"""
        SELECT
            ci.id AS intelligence_id,
            ci.fit_score,
            ci.fit_level,
            ci.candidate_name,
            ifnull(ci.candidate_company, '') AS candidate_company,
            ifnull(ci.client, '') AS client,
            ifnull(ci.position, '') AS position,
            ci.evidence_json,
            ci.risk_json,
            ci.next_action,
            ifnull(t.id, 0) AS task_id,
            ifnull(t.status, 'open') AS task_status,
            ifnull(t.priority, 2) AS priority,
            ifnull(t.task_type, '') AS task_type,
            {column_expr(followup_task_columns, 't', 'lane_tag')} AS lane_tag,
            {column_expr(followup_task_columns, 't', 'lane_reason')} AS lane_reason,
            {column_expr(followup_task_columns, 't', 'draft_message')} AS draft_message,
            {column_expr(followup_task_columns, 't', 'match_confidence')} AS match_confidence,
            ifnull(r.intent, '') AS intent,
            ifnull(r.raw_text, '') AS raw_text,
            {column_expr(reply_columns, 'r', 'candidate_title')} AS candidate_title
        FROM candidate_intelligence ci
        LEFT JOIN followup_tasks t
            ON t.candidate_name = ci.candidate_name
           AND {task_position_expr} = ifnull(ci.position, '')
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE ifnull(t.status, 'open') = 'open' OR t.id IS NULL
        ORDER BY ci.fit_score DESC, ifnull(t.priority, 99) ASC, ci.id ASC
        """
    return conn.execute(query).fetchall()


def load_recent_outreach(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """
            SELECT event_time, event_type, candidate_name, client, position, message_summary
            FROM outreach_events
            ORDER BY id DESC
            LIMIT 8
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def row_key(row: sqlite3.Row) -> tuple[str, str, str]:
    return (row["candidate_name"] or "", row["client"] or "", row["position"] or "")


def prefer_row(new: sqlite3.Row, old: sqlite3.Row | None) -> bool:
    if old is None:
        return True
    if safe_int(new["fit_score"]) != safe_int(old["fit_score"]):
        return safe_int(new["fit_score"]) > safe_int(old["fit_score"])
    if safe_int(new["priority"], 99) != safe_int(old["priority"], 99):
        return safe_int(new["priority"], 99) < safe_int(old["priority"], 99)
    return safe_int(new["task_id"]) > safe_int(old["task_id"])


def dedupe_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    best: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = row_key(row)
        if prefer_row(row, best.get(key)):
            best[key] = row
    return sorted(
        best.values(),
        key=lambda row: (-safe_int(row["fit_score"]), safe_int(row["priority"], 99), safe_int(row["intelligence_id"])),
    )


def parse_json(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value, "risks": value, "questions": value}
    return {}


def first_line(text: str, limit: int = 72) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("..." if len(clean) > limit else "")


def project(row: sqlite3.Row) -> str:
    if row["client"] and row["position"]:
        return f"{row['client']}/{row['position']}"
    if row["position"]:
        return row["position"]
    if row["client"]:
        return row["client"]
    return "待确认"


def section_rows(rows: list[sqlite3.Row], predicate) -> list[sqlite3.Row]:
    return [row for row in rows if predicate(row)]


def write_table(lines: list[str], rows: list[sqlite3.Row]) -> None:
    lines.extend(
        [
            "| 分数 | 候选人 | 项目 | 意图 | 下一步 | 原话 |",
            "|---:|---|---|---|---|---|",
        ]
    )
    if not rows:
        lines.append("| - | 暂无 | - | - | - | - |")
        return
    for row in rows:
        lines.append(
            f"| {row['fit_score']} | {row['candidate_name']} | {project(row)} | "
            f"{row['intent'] or '未分类'} | {first_line(row['next_action'], 40)} | "
            f"{first_line(row['raw_text'], 46)} |"
        )


def write_report(rows: list[sqlite3.Row], outreach: list[sqlite3.Row], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘今日优先处理人选_{stamp}.md"

    levels = Counter(row["fit_level"] for row in rows)
    intents = Counter(row["intent"] or "未分类" for row in rows)
    priority = section_rows(rows, lambda row: row["fit_score"] >= 80 or row["lane_tag"] == "fast_lane")
    confirm = section_rows(
        rows,
        lambda row: 55 <= row["fit_score"] < 80 and (not row["client"] or row["match_confidence"] in ("low", "unmatched", "")),
    )
    salary_or_contact = section_rows(rows, lambda row: row["intent"] in ("salary_concern", "need_contact"))
    light_touch = section_rows(rows, lambda row: row["task_type"] == "light_touch_followup" or row["intent"] == "short_confirmation")
    pause = section_rows(rows, lambda row: row["fit_score"] < 55 or row["intent"] == "not_interested")

    lines = [
        "# 猎聘今日优先处理人选",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 今日判断",
        "",
        f"- 待处理画像：{len(rows)} 人",
        f"- A/B 优先推进：{len(priority)} 人",
        f"- 需补客户/岗位：{len(confirm)} 人",
        f"- 需转联系方式或谈薪：{len(salary_or_contact)} 人",
        f"- 快推池：{len(section_rows(rows, lambda row: row['lane_tag'] == 'fast_lane'))} 人",
        f"- 轻跟进池：{len(light_touch)} 人",
        f"- 暂缓/沉淀：{len(pause)} 人",
        f"- 等级分布：{'、'.join(f'{k} {v}' for k, v in levels.items()) or '暂无'}",
        f"- 意图分布：{'、'.join(f'{k} {v}' for k, v in intents.items()) or '暂无'}",
        "",
        "## 先推进",
        "",
    ]
    write_table(lines, priority)

    lines.extend(["", "## 先补确认", ""])
    write_table(lines, confirm)

    lines.extend(["", "## 转微信/薪资处理", ""])
    write_table(lines, salary_or_contact)

    lines.extend(["", "## 轻跟进池", ""])
    write_table(lines, light_touch)

    lines.extend(["", "## 暂缓或只沉淀原因", ""])
    write_table(lines, pause)

    lines.extend(["", "## 每个人的验证问题", ""])
    for row in rows:
        evidence = parse_json(row["evidence_json"])
        risk = parse_json(row["risk_json"])
        questions = evidence.get("questions") or []
        risks = risk.get("risks") or []
        lines.append(
            f"- {row['candidate_name']}｜{project(row)}｜验证："
            f"{'；'.join(questions) if questions else '暂无'}｜风险：{'；'.join(risks) if risks else '无明显风险'}"
        )

    lines.extend(["", "## 最近触达事件", ""])
    if outreach:
        for event in outreach:
            lines.append(
                f"- {event['event_time']}｜{event['event_type']}｜{event['candidate_name']}｜"
                f"{event['client'] or '未定客户'}/{event['position'] or '未定岗位'}｜{first_line(event['message_summary'], 64)}"
            )
    else:
        lines.append("- 暂无真实触达事件。回复助手 0.1.5 起会记录“采纳修改/填入输入框”，同步后这里会出现。")

    lines.extend(
        [
            "",
            "## 操作建议",
            "",
            "1. 先处理“先推进”和“快推池”：优先承接热度高、方向明确的人。",
            "2. 再处理“先补确认”：只问一个关键缺口，避免一次问太多。",
            "3. 轻跟进池先发一个项目锚点，不急着约电话。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate today's Liepin priority board.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        rows = dedupe_rows(load_rows(conn))
        outreach = load_recent_outreach(conn)
    finally:
        conn.close()

    report = write_report(rows, outreach, Path(args.output_dir).expanduser())
    print(
        json.dumps(
            {
                "ok": True,
                "rows": len(rows),
                "outreach_events": len(outreach),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
