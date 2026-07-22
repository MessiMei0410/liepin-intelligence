#!/usr/bin/env python3
"""Backfill candidate-last Liepin context messages into candidate_replies/followup_tasks."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from record_candidate_reply import (
    DEFAULT_DB,
    connect,
    ensure_schema,
    infer_candidate,
    build_record,
    insert_reply_and_task,
)


DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def latest_context_file(output_dir: Path) -> Path:
    files = sorted(output_dir.glob("liepin_conversation_contexts_*.json"))
    if not files:
        raise FileNotFoundError("还没有可用的上下文文件。")
    return files[-1]


def load_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for convo in payload.get("conversations", []):
        messages = convo.get("messages") or []
        if not messages:
            continue
        last = messages[-1]
        if last.get("direction_hint") != "candidate":
            continue
        preview = convo.get("preview") or {}
        rows.append(
            {
                "candidate_name": clean(preview.get("name")),
                "candidate_title": clean(preview.get("title")),
                "message_time": clean(last.get("time")),
                "raw_text": clean(last.get("text")),
            }
        )
    return rows


def write_report(output_dir: Path, source_file: Path, stats: dict[str, int], examples: list[dict[str, str]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"猎聘上下文待跟进回填_{stamp}.md"
    lines = [
        "# 猎聘上下文待跟进回填",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 来源文件：{source_file}",
        f"- 候选人最后一句线索：{stats['candidate_last']}",
        f"- 新增回复记录：{stats['inserted']}",
        f"- 重复跳过：{stats['duplicate']}",
        f"- 新增待办：{stats['tasks']}",
        "",
        "## 样例",
        "",
    ]
    for item in examples[:12]:
        lines.append(f"- {item['candidate_name']}｜{item['candidate_title']}：{item['raw_text']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill candidate-last context messages into follow-up tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--input-json", default="")
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser()
    source_file = Path(args.input_json).expanduser() if args.input_json else latest_context_file(output_dir)
    payload = json.loads(source_file.read_text(encoding="utf-8"))
    candidates = load_candidates(payload)[: args.limit]

    conn = connect(Path(args.db).expanduser())
    stats = {"candidate_last": len(candidates), "inserted": 0, "duplicate": 0, "tasks": 0}
    try:
        ensure_schema(conn)
        for item in candidates:
            candidate = infer_candidate(conn, item["candidate_name"], "", "", "")
            record_args = argparse.Namespace(
                candidate_id=(candidate["id"] if candidate else None),
                candidate_name=item["candidate_name"],
                candidate_company="",
                candidate_title=item["candidate_title"],
                client=(clean(candidate["client"]) if candidate else ""),
                position=(clean(candidate["position"]) if candidate else ""),
                channel="liepin_context_backfill",
                conversation_id="",
                message_time=item["message_time"],
                raw_text=item["raw_text"],
            )
            record = build_record(record_args, candidate)
            result = insert_reply_and_task(conn, record, create_task=True)
            stats["inserted"] += int(result.get("inserted") or 0)
            stats["duplicate"] += int(result.get("duplicate") or 0)
            stats["tasks"] += 1 if result.get("task_id") else 0
    finally:
        conn.close()

    report = write_report(output_dir, source_file, stats, candidates)
    print(
        json.dumps(
            {
                "ok": True,
                "source": str(source_file),
                "stats": stats,
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
