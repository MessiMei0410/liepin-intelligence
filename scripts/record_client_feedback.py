#!/usr/bin/env python3
"""Record client feedback and update candidate status when confirmed."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"


SCHEMA = """
CREATE TABLE IF NOT EXISTS client_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER,
    candidate_name TEXT NOT NULL,
    candidate_company TEXT,
    client TEXT,
    position TEXT,
    feedback_type TEXT NOT NULL,
    status_after TEXT,
    reason_tags_json TEXT DEFAULT '[]',
    feedback_detail TEXT,
    next_action TEXT,
    source TEXT DEFAULT 'manual',
    feedback_time TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime'))
)
"""


FEEDBACK_OPTIONS = [
    ("approved", "客户认可", "client_approved"),
    ("rejected", "客户否决", "client_rejected"),
    ("interviewing", "进入面试", "interviewing"),
    ("interview_passed", "面试通过", "passed"),
    ("interview_failed", "面试未通过", "eliminated"),
    ("offer", "进入 offer", "offered"),
    ("hired", "确认入职", "hired"),
    ("hold", "暂缓", "hold"),
    ("eliminated", "淘汰/归档", "eliminated"),
]

STATUS_BY_FEEDBACK = {key: status for key, _label, status in FEEDBACK_OPTIONS}
LABEL_BY_FEEDBACK = {key: label for key, label, _status in FEEDBACK_OPTIONS}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_feedback_project ON client_feedback_events(client, position)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_feedback_candidate ON client_feedback_events(candidate_name, candidate_company)"
    )
    conn.commit()


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def first_line(text: Any, limit: int = 84) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def parse_tags(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [item.strip() for item in text.replace("，", ",").split(",")]
    if not isinstance(value, list):
        raise SystemExit("--reason-tags 必须是列表或逗号分隔文本")
    return [clean(item) for item in value if clean(item)]


def load_candidate(conn: sqlite3.Connection, args: argparse.Namespace) -> sqlite3.Row | None:
    if args.candidate_id:
        return conn.execute(
            "SELECT * FROM candidates WHERE id = ? LIMIT 1",
            (args.candidate_id,),
        ).fetchone()
    name = clean(args.candidate_name)
    if not name:
        return None
    params: list[Any] = [name]
    filters = ["name = ?"]
    client = clean(args.client)
    position = clean(args.position)
    if client:
        filters.append("client = ?")
        params.append(client)
    if position:
        filters.append("position = ?")
        params.append(position)
    return conn.execute(
        f"""
        SELECT *
        FROM candidates
        WHERE {' AND '.join(filters)}
        ORDER BY
            CASE WHEN status IN ('recommended','contacted','replied','client_approved','interviewing','offered') THEN 0 ELSE 1 END,
            datetime(updated_at) DESC,
            id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def load_recent_candidates(conn: sqlite3.Connection, limit: int = 24) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, name, company, client, position, status, updated_at
        FROM candidates
        ORDER BY
            CASE WHEN status IN ('recommended','contacted','replied','client_approved','interviewing','offered') THEN 0 ELSE 1 END,
            datetime(updated_at) DESC,
            id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def prompt_text(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}：").strip()
        if value:
            return value
        if default:
            return default
        if not required:
            return ""
        print("这个必填，填一下就行。")


def prompt_int(label: str, default: int | None = None) -> int | None:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{label}{suffix}：").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            print("这里填数字编号，留空也可以。")


def apply_interactive(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    candidates = load_recent_candidates(conn, args.limit)
    print("\n最近候选人（可直接输入候选人编号，留空则手填姓名）：")
    for row in candidates:
        project = f"{row['client'] or '未定客户'}/{row['position'] or '未定岗位'}"
        print(f"  {row['id']}. {row['name']}｜{row['company'] or '未填公司'}｜{project}｜{row['status'] or 'new'}")
    candidate_id = prompt_int("候选人编号")
    if candidate_id:
        args.candidate_id = candidate_id
    else:
        args.candidate_name = prompt_text("候选人姓名", args.candidate_name or "", required=True)
        args.client = prompt_text("客户", args.client or "")
        args.position = prompt_text("岗位", args.position or "")

    print("\n反馈类型：")
    for idx, (key, label, status) in enumerate(FEEDBACK_OPTIONS, start=1):
        print(f"  {idx}. {label} -> {status}")
    feedback_idx = prompt_int("选择反馈类型", 1)
    if not feedback_idx or feedback_idx < 1 or feedback_idx > len(FEEDBACK_OPTIONS):
        raise SystemExit("反馈类型选择无效。")
    args.feedback_type = FEEDBACK_OPTIONS[feedback_idx - 1][0]
    args.reason_tags = prompt_text("原因标签（可逗号分隔）", args.reason_tags or "")
    args.feedback_detail = prompt_text("客户反馈原话/摘要", args.feedback_detail or "")
    args.next_action = prompt_text("下一步动作", args.next_action or "")


def build_record(args: argparse.Namespace, candidate: sqlite3.Row | None) -> dict[str, Any]:
    feedback_type = clean(args.feedback_type)
    if feedback_type not in STATUS_BY_FEEDBACK:
        allowed = "、".join(f"{key}={label}" for key, label, _status in FEEDBACK_OPTIONS)
        raise SystemExit(f"未知反馈类型：{feedback_type}。可选：{allowed}")

    candidate_name = clean(args.candidate_name) or (clean(candidate["name"]) if candidate else "")
    if not candidate_name:
        raise SystemExit("需要候选人姓名或 candidate_id。")

    status_after = clean(args.status_after) or STATUS_BY_FEEDBACK[feedback_type]
    return {
        "candidate_id": int(args.candidate_id or (candidate["id"] if candidate else 0)) or None,
        "candidate_name": candidate_name,
        "candidate_company": clean(args.candidate_company) or (clean(candidate["company"]) if candidate else ""),
        "client": clean(args.client) or (clean(candidate["client"]) if candidate else ""),
        "position": clean(args.position) or (clean(candidate["position"]) if candidate else ""),
        "feedback_type": feedback_type,
        "status_after": status_after,
        "reason_tags_json": json.dumps(parse_tags(args.reason_tags), ensure_ascii=False),
        "feedback_detail": clean(args.feedback_detail),
        "next_action": clean(args.next_action),
        "source": clean(args.source) or "manual",
        "feedback_time": clean(args.feedback_time) or datetime.now().isoformat(timespec="seconds"),
    }


def insert_record(conn: sqlite3.Connection, record: dict[str, Any], update_status: bool) -> dict[str, int]:
    before = conn.total_changes
    cursor = conn.execute(
        """
        INSERT INTO client_feedback_events (
            candidate_id, candidate_name, candidate_company, client, position,
            feedback_type, status_after, reason_tags_json, feedback_detail,
            next_action, source, feedback_time
        ) VALUES (
            :candidate_id, :candidate_name, :candidate_company, :client, :position,
            :feedback_type, :status_after, :reason_tags_json, :feedback_detail,
            :next_action, :source, :feedback_time
        )
        """,
        record,
    )
    event_updates = conn.total_changes - before

    candidate_updates = 0
    if update_status and record["candidate_id"]:
        before = conn.total_changes
        conn.execute(
            """
            UPDATE candidates
            SET status = ?,
                client_feedback = ?,
                elimination_reason = CASE WHEN ? IN ('client_rejected', 'eliminated') THEN ? ELSE elimination_reason END,
                updated_at = datetime('now','localtime')
            WHERE id = ?
            """,
            (
                record["status_after"],
                record["feedback_detail"] or LABEL_BY_FEEDBACK[record["feedback_type"]],
                record["status_after"],
                record["feedback_detail"] or ",".join(json.loads(record["reason_tags_json"] or "[]")),
                record["candidate_id"],
            ),
        )
        candidate_updates = conn.total_changes - before
    conn.commit()
    return {"event_id": int(cursor.lastrowid), "event_updates": event_updates, "candidate_updates": candidate_updates}


def write_receipt(output_dir: Path, record: dict[str, Any], stats: dict[str, int], dry_run: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "dryrun" if dry_run else f"id{stats.get('event_id')}"
    path = output_dir / f"猎聘客户反馈记录_{suffix}_{stamp}.md"
    tags = json.loads(record["reason_tags_json"] or "[]")
    storage_status = "未写入（干跑验证）" if dry_run else f"已写入 #{stats.get('event_id')}"
    lines = [
        "# 猎聘客户反馈记录",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"写入状态：{storage_status}",
        "",
        "## 反馈",
        "",
        f"- 候选人：{record['candidate_name']}",
        f"- 公司：{record['candidate_company'] or '未填'}",
        f"- 项目：{record['client'] or '未定客户'}/{record['position'] or '未定岗位'}",
        f"- 反馈类型：{LABEL_BY_FEEDBACK.get(record['feedback_type'], record['feedback_type'])}",
        f"- 状态更新为：{record['status_after']}",
        f"- 原因标签：{'、'.join(tags) if tags else '无'}",
        f"- 反馈摘要：{record['feedback_detail'] or '无'}",
        f"- 下一步：{record['next_action'] or '未填'}",
        "",
        "## 写入结果",
        "",
        f"- 反馈事件：{stats.get('event_updates', 0)}",
        f"- 候选人状态更新：{stats.get('candidate_updates', 0)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Record client feedback for a candidate.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--candidate-id", type=int)
    parser.add_argument("--candidate-name")
    parser.add_argument("--candidate-company")
    parser.add_argument("--client")
    parser.add_argument("--position")
    parser.add_argument("--feedback-type")
    parser.add_argument("--status-after")
    parser.add_argument("--reason-tags")
    parser.add_argument("--feedback-detail")
    parser.add_argument("--next-action")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--feedback-time")
    parser.add_argument("--no-status-update", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        if args.interactive:
            apply_interactive(args, conn)
        candidate = load_candidate(conn, args)
        record = build_record(args, candidate)
        if args.interactive and not args.dry_run:
            print(
                f"\n准备记录：{record['candidate_name']}｜"
                f"{LABEL_BY_FEEDBACK[record['feedback_type']]} -> {record['status_after']}"
            )
            confirm = prompt_text("确认写入？输入 y 确认", "")
            if confirm.lower() not in {"y", "yes"} and confirm != "确认":
                raise SystemExit("未确认写入，已退出。")
        stats = {"event_id": None, "event_updates": 0, "candidate_updates": 0}
        if not args.dry_run:
            stats = insert_record(conn, record, not args.no_status_update)
    finally:
        conn.close()

    receipt = write_receipt(Path(args.output_dir).expanduser(), record, stats, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "record": record,
                "stats": stats,
                "receipt": str(receipt),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
