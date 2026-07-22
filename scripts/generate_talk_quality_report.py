#!/usr/bin/env python3
"""Generate a quality report for Liepin talk drafts."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            id,
            ifnull(candidate_name, '') AS candidate_name,
            ifnull(inferred_client, confirmed_client) AS inferred_client,
            ifnull(inferred_position, confirmed_position) AS inferred_position,
            ifnull(talk_strategy, '') AS talk_strategy,
            ifnull(talk_score, 0) AS talk_score,
            ifnull(talk_reason, '') AS talk_reason,
            ifnull(talk_risk, '') AS talk_risk,
            ifnull(talk_missing, '') AS talk_missing,
            ifnull(draft_message, '') AS draft_message,
            ifnull(status, 'open') AS status
        FROM followup_tasks
        WHERE ifnull(status, 'open') = 'open'
        ORDER BY talk_score DESC, id ASC
        """
    ).fetchall()


def bucket(score: int) -> str:
    if score >= 85:
        return "可直接复制"
    if score >= 70:
        return "复制前看一眼"
    if score > 0:
        return "先补信息"
    return "未生成"


def short(text: str, limit: int = 70) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + ("..." if len(clean) > limit else "")


def write_report(rows: list[sqlite3.Row], output_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘话术质量报告_{stamp}.md"
    bucket_counts = Counter(bucket(int(row["talk_score"] or 0)) for row in rows)
    strategy_counts = Counter(row["talk_strategy"] or "未生成" for row in rows)
    avg = round(sum(int(row["talk_score"] or 0) for row in rows) / len(rows), 1) if rows else 0
    lines = [
        "# 猎聘话术质量报告",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 总览",
        "",
        f"- 打开待办草稿：{len(rows)}",
        f"- 平均分：{avg}",
        f"- 分档：{'、'.join(f'{k} {v}' for k, v in bucket_counts.items())}",
        f"- 策略：{'、'.join(f'{k} {v}' for k, v in sorted(strategy_counts.items()))}",
        "",
        "## 明细",
        "",
        "| 待办 | 候选人 | 分档 | 分数 | 策略 | 风险 | 缺失 | 草稿摘要 |",
        "|---:|---|---|---:|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {name} | {bucket} | {score} | {strategy} | {risk} | {missing} | {draft} |".format(
                id=row["id"],
                name=(row["candidate_name"] or "未识别").replace("|", "｜"),
                bucket=bucket(int(row["talk_score"] or 0)),
                score=row["talk_score"],
                strategy=(row["talk_strategy"] or "未生成").replace("|", "｜"),
                risk=(row["talk_risk"] or "无").replace("|", "｜"),
                missing=(row["talk_missing"] or "无").replace("|", "｜"),
                draft=short(row["draft_message"]).replace("|", "｜"),
            )
        )
    lines.extend(
        [
            "",
            "## 使用建议",
            "",
            "- 85 分以上：通常可以直接复制，但仍建议看一眼项目是否已确认。",
            "- 70-84 分：复制前看风险提示，尤其是客户名/岗位名是否明确。",
            "- 70 分以下：先在工作台确认项目或补候选人信息，再重新跑话术生成。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate talk draft quality report.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect_db(db_path)
    try:
        rows = load_rows(conn)
    finally:
        conn.close()
    report = write_report(rows, output_dir)
    print(json.dumps({"ok": True, "rows": len(rows), "report": str(report)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
