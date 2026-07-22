#!/usr/bin/env python3
"""Generate strategy correction rules from search experiments and client feedback."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
POSITIVE_FEEDBACK = {"approved", "interviewing", "interview_passed", "offer", "hired"}
NEGATIVE_FEEDBACK = {"rejected", "interview_failed", "eliminated"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT,
    position TEXT,
    promote_keywords_json TEXT DEFAULT '[]',
    suppress_keywords_json TEXT DEFAULT '[]',
    target_tags_json TEXT DEFAULT '[]',
    blocker_tags_json TEXT DEFAULT '[]',
    evidence_json TEXT DEFAULT '[]',
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(client, position)
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_corrections_project ON strategy_corrections(client, position)")
    conn.commit()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_json_list(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [clean(item) for item in value if clean(item)] if isinstance(value, list) else []


def dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys([item for item in items if item]))


def add(group: dict[str, list[str]], key: str, value: str) -> None:
    value = clean(value)
    if value:
        group[key].append(value)


def collect_latest_searches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not table_exists(conn, "search_experiments"):
        return []
    rows = conn.execute(
        """
        SELECT client, position, query, filters_json, viewed_count, extracted_count,
               recommended_count, reply_count, positive_reply_count, noise_notes,
               updated_at, run_time, created_at, id
        FROM search_experiments
        ORDER BY datetime(COALESCE(updated_at, run_time, created_at)) DESC, id DESC
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (
            clean(row["client"]),
            clean(row["position"]),
            clean(row["query"]),
            clean(row["filters_json"]),
        )
        if key not in grouped:
            grouped[key] = row
            continue
        current = grouped[key]
        if int(row["recommended_count"] or 0) > int(current["recommended_count"] or 0):
            grouped[key] = row
        elif int(row["positive_reply_count"] or 0) > int(current["positive_reply_count"] or 0):
            grouped[key] = row
    return list(grouped.values())


def collect(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    projects: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(
        lambda: {
            "promote": [],
            "suppress": [],
            "target": [],
            "blocker": [],
            "evidence": [],
        }
    )
    if table_exists(conn, "search_experiments"):
        for row in collect_latest_searches(conn):
            key = (clean(row["client"]), clean(row["position"]))
            group = projects[key]
            query = clean(row["query"])
            positive = int(row["positive_reply_count"] or 0)
            recommended = int(row["recommended_count"] or 0)
            viewed = int(row["viewed_count"] or 0)
            extracted = int(row["extracted_count"] or 0)
            if positive or recommended:
                add(group, "promote", query)
                add(group, "evidence", f"搜索有效：{query} 推荐 {recommended} 正向 {positive}")
            elif viewed and extracted == 0:
                add(group, "suppress", query)
                add(group, "evidence", f"搜索降噪：{query} 查看 {viewed} 入库 0")
            if row["noise_notes"]:
                add(group, "blocker", clean(row["noise_notes"]))

    if table_exists(conn, "client_feedback_events"):
        rows = conn.execute(
            """
            SELECT f.client, f.position, f.candidate_id, f.candidate_name, f.feedback_type,
                   f.reason_tags_json, f.feedback_detail,
                   COALESCE(cp.function_tags_json, '[]') AS function_tags_json,
                   COALESCE(cp.industry_tags_json, '[]') AS industry_tags_json
            FROM client_feedback_events f
            LEFT JOIN candidate_profiles cp ON f.candidate_id = cp.candidate_id
            ORDER BY datetime(COALESCE(f.feedback_time, f.created_at)) DESC, f.id DESC
            """
        ).fetchall()
        for row in rows:
            key = (clean(row["client"]), clean(row["position"]))
            group = projects[key]
            feedback_type = clean(row["feedback_type"])
            function_tags = parse_json_list(row["function_tags_json"])
            industry_tags = parse_json_list(row["industry_tags_json"])
            reason_tags = parse_json_list(row["reason_tags_json"])
            if feedback_type in POSITIVE_FEEDBACK:
                group["promote"].extend(function_tags)
                group["target"].extend(industry_tags)
                add(group, "evidence", f"客户正向：{clean(row['candidate_name'])} {clean(row['feedback_detail'])}")
            elif feedback_type in NEGATIVE_FEEDBACK:
                group["suppress"].extend(function_tags)
                group["blocker"].extend(reason_tags)
                add(group, "evidence", f"客户负向：{clean(row['candidate_name'])} {clean(row['feedback_detail'])}")
            elif feedback_type == "hold":
                group["blocker"].extend(reason_tags or ["客户暂缓"])

    records = []
    for (client, position), group in projects.items():
        records.append(
            {
                "client": client,
                "position": position,
                "promote_keywords_json": json.dumps(dedupe(group["promote"])[:20], ensure_ascii=False),
                "suppress_keywords_json": json.dumps(dedupe(group["suppress"])[:20], ensure_ascii=False),
                "target_tags_json": json.dumps(dedupe(group["target"])[:12], ensure_ascii=False),
                "blocker_tags_json": json.dumps(dedupe(group["blocker"])[:20], ensure_ascii=False),
                "evidence_json": json.dumps(dedupe(group["evidence"])[:30], ensure_ascii=False),
            }
        )
    return sorted(records, key=lambda row: len(json.loads(row["evidence_json"])), reverse=True)


def upsert(conn: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    sql = """
    INSERT INTO strategy_corrections (
        client, position, promote_keywords_json, suppress_keywords_json,
        target_tags_json, blocker_tags_json, evidence_json, updated_at
    ) VALUES (
        :client, :position, :promote_keywords_json, :suppress_keywords_json,
        :target_tags_json, :blocker_tags_json, :evidence_json, datetime('now','localtime')
    )
    ON CONFLICT(client, position) DO UPDATE SET
        promote_keywords_json=excluded.promote_keywords_json,
        suppress_keywords_json=excluded.suppress_keywords_json,
        target_tags_json=excluded.target_tags_json,
        blocker_tags_json=excluded.blocker_tags_json,
        evidence_json=excluded.evidence_json,
        updated_at=datetime('now','localtime')
    """
    conn.executemany(sql, records)
    conn.commit()


def write_report(output_dir: Path, records: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘策略修正规则_{stamp}.md"
    lines = [
        "# 猎聘策略修正规则",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 当前状态",
        "",
    ]
    if not records:
        lines.extend(
            [
                "- 暂无可沉淀的真实搜索实验或客户反馈。",
                "- 下一步在工作台记录搜索实验、客户认可/否决后，本报告会自动生成保留/降权规则。",
            ]
        )
    else:
        lines.append(f"- 已生成策略修正规则：{len(records)} 个项目")

    lines.extend(["", "## 项目规则", "", "| 项目 | 保留/强化 | 降权/避开 | 目标标签 | 阻力 | 证据 |", "|---|---|---|---|---|---|"])
    if not records:
        lines.append("| 暂无 | - | - | - | - | - |")
    for row in records[:30]:
        project = f"{row['client'] or '未定客户'}/{row['position'] or '未定岗位'}"
        lines.append(
            "| {project} | {promote} | {suppress} | {target} | {blocker} | {evidence} |".format(
                project=project.replace("|", "｜"),
                promote="、".join(json.loads(row["promote_keywords_json"])[:6]) or "无",
                suppress="、".join(json.loads(row["suppress_keywords_json"])[:6]) or "无",
                target="、".join(json.loads(row["target_tags_json"])[:4]) or "无",
                blocker="、".join(json.loads(row["blocker_tags_json"])[:4]) or "无",
                evidence="；".join(json.loads(row["evidence_json"])[:2]).replace("|", "｜") or "无",
            )
        )
    lines.extend(["", "## 使用规则", ""])
    lines.append("1. 保留/强化：下轮搜索优先复用这些关键词、能力标签或候选人来源。")
    lines.append("2. 降权/避开：下轮搜索需要加限制词、排除词，或先解释为什么还要覆盖。")
    lines.append("3. 阻力：用于调整话术、验证问题和客户推荐理由。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Liepin strategy correction rules.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        records = collect(conn)
        if not args.dry_run:
            upsert(conn, records)
    finally:
        conn.close()
    report = write_report(Path(args.output_dir).expanduser(), records)
    print(json.dumps({"ok": True, "rules": len(records), "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
