#!/usr/bin/env python3
"""Confirm the client/position assignment for Liepin replies and follow-up tasks."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ensure_project_confirmation_schema import DEFAULT_DB, DEFAULT_OUTPUT_DIR, connect_db, ensure_schema


NEEDS_CONFIRM_SQL = """
SELECT
    t.id AS task_id,
    t.source_id AS reply_id,
    t.candidate_id,
    COALESCE(NULLIF(t.candidate_name, ''), NULLIF(r.candidate_name, ''), '未识别') AS candidate_name,
    COALESCE(NULLIF(t.candidate_company, ''), NULLIF(r.candidate_company, ''), '') AS candidate_company,
    COALESCE(NULLIF(t.client, ''), NULLIF(r.client, ''), '') AS old_client,
    COALESCE(NULLIF(t.position, ''), NULLIF(r.position, ''), '') AS old_position,
    COALESCE(NULLIF(t.inferred_client, ''), NULLIF(r.inferred_client, ''), '') AS inferred_client,
    COALESCE(NULLIF(t.inferred_position, ''), NULLIF(r.inferred_position, ''), '') AS inferred_position,
    COALESCE(NULLIF(t.confirmed_client, ''), NULLIF(r.confirmed_client, ''), '') AS confirmed_client,
    COALESCE(NULLIF(t.confirmed_position, ''), NULLIF(r.confirmed_position, ''), '') AS confirmed_position,
    COALESCE(NULLIF(t.match_confidence, ''), NULLIF(r.match_confidence, ''), 'unmatched') AS match_confidence,
    COALESCE(NULLIF(t.task_type, ''), 'review_reply') AS task_type,
    COALESCE(NULLIF(t.status, ''), 'open') AS task_status,
    COALESCE(NULLIF(r.intent, ''), 'unclear') AS intent,
    COALESCE(NULLIF(r.raw_text, ''), t.reason, '') AS raw_text,
    COALESCE(t.priority, 2) AS priority,
    COALESCE(t.updated_at, t.created_at, r.created_at, '') AS updated_at
FROM followup_tasks t
LEFT JOIN candidate_replies r
    ON t.source_table = 'candidate_replies' AND t.source_id = r.id
WHERE COALESCE(t.status, 'open') = 'open'
  AND (
      COALESCE(t.confirmation_status, r.confirmation_status, 'unconfirmed') != 'confirmed'
      OR COALESCE(t.match_confidence, r.match_confidence, '') IN ('', 'low', 'unmatched')
      OR COALESCE(NULLIF(t.confirmed_client, ''), NULLIF(t.inferred_client, ''), NULLIF(t.client, ''),
                  NULLIF(r.confirmed_client, ''), NULLIF(r.inferred_client, ''), NULLIF(r.client, ''), '') = ''
      OR COALESCE(NULLIF(t.confirmed_position, ''), NULLIF(t.inferred_position, ''), NULLIF(t.position, ''),
                  NULLIF(r.confirmed_position, ''), NULLIF(r.inferred_position, ''), NULLIF(r.position, ''), '') = ''
  )
ORDER BY
    CASE COALESCE(t.match_confidence, r.match_confidence, 'unmatched')
        WHEN 'unmatched' THEN 0
        WHEN 'low' THEN 1
        WHEN 'medium' THEN 2
        WHEN 'high' THEN 3
        ELSE 4
    END,
    COALESCE(t.priority, 2) ASC,
    t.id DESC
"""


def clean(text: Any) -> str:
    return " ".join(str(text or "").split())


def first_line(text: Any, limit: int = 84) -> str:
    value = clean(text)
    return value[:limit] + ("..." if len(value) > limit else "")


def project_label(client: str, position: str) -> str:
    client = clean(client)
    position = clean(position)
    if client and position:
        return f"{client}/{position}"
    if client:
        return f"{client}/未定岗位"
    if position:
        return f"未定客户/{position}"
    return "未定客户/未定岗位"


def row_current_project(row: sqlite3.Row) -> tuple[str, str]:
    client = row["confirmed_client"] or row["inferred_client"] or row["old_client"] or ""
    position = row["confirmed_position"] or row["inferred_position"] or row["old_position"] or ""
    return clean(client), clean(position)


def load_needs_confirmation(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(f"{NEEDS_CONFIRM_SQL} LIMIT ?", (limit,)).fetchall()


def load_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            t.id AS task_id,
            t.source_id AS reply_id,
            t.candidate_id,
            COALESCE(NULLIF(t.candidate_name, ''), NULLIF(r.candidate_name, ''), '未识别') AS candidate_name,
            COALESCE(NULLIF(t.candidate_company, ''), NULLIF(r.candidate_company, ''), '') AS candidate_company,
            COALESCE(NULLIF(t.client, ''), NULLIF(r.client, ''), '') AS old_client,
            COALESCE(NULLIF(t.position, ''), NULLIF(r.position, ''), '') AS old_position,
            COALESCE(NULLIF(t.inferred_client, ''), NULLIF(r.inferred_client, ''), '') AS inferred_client,
            COALESCE(NULLIF(t.inferred_position, ''), NULLIF(r.inferred_position, ''), '') AS inferred_position,
            COALESCE(NULLIF(t.confirmed_client, ''), NULLIF(r.confirmed_client, ''), '') AS confirmed_client,
            COALESCE(NULLIF(t.confirmed_position, ''), NULLIF(r.confirmed_position, ''), '') AS confirmed_position,
            COALESCE(NULLIF(t.match_confidence, ''), NULLIF(r.match_confidence, ''), 'unmatched') AS match_confidence,
            COALESCE(NULLIF(t.task_type, ''), 'review_reply') AS task_type,
            COALESCE(NULLIF(t.status, ''), 'open') AS task_status,
            COALESCE(NULLIF(r.intent, ''), 'unclear') AS intent,
            COALESCE(NULLIF(r.raw_text, ''), t.reason, '') AS raw_text,
            COALESCE(t.priority, 2) AS priority,
            COALESCE(t.updated_at, t.created_at, r.created_at, '') AS updated_at
        FROM followup_tasks t
        LEFT JOIN candidate_replies r
            ON t.source_table = 'candidate_replies' AND t.source_id = r.id
        WHERE t.id = ?
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"没有找到待办 #{task_id}")
    return row


def load_position(conn: sqlite3.Connection, position_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, client, title AS position, status, gap
        FROM positions
        WHERE id = ?
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"没有找到岗位 #{position_id}")
    return row


def load_positions(conn: sqlite3.Connection, limit: int = 40, keyword: str = "") -> list[sqlite3.Row]:
    keyword = clean(keyword)
    if keyword:
        pattern = f"%{keyword}%"
        return conn.execute(
            """
            SELECT id, client, title AS position, status, gap
            FROM positions
            WHERE COALESCE(status, 'open') = 'open'
              AND (client LIKE ? OR title LIKE ?)
            ORDER BY client, title, id
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    return conn.execute(
        """
        SELECT id, client, title AS position, status, gap
        FROM positions
        WHERE COALESCE(status, 'open') = 'open'
        ORDER BY client, title, id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def resolve_project(args: argparse.Namespace, conn: sqlite3.Connection) -> tuple[str, str]:
    if args.position_id:
        row = load_position(conn, args.position_id)
        return clean(args.client) or clean(row["client"]), clean(args.position) or clean(row["position"])
    client = clean(args.client)
    position = clean(args.position)
    if not client and not position:
        raise SystemExit("需要指定岗位：可用 --position-id，或 --client/--position。")
    return client, position


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
    rows = load_needs_confirmation(conn, args.limit)
    if not rows:
        print("当前没有需要补客户/岗位归属的打开待办。")
        raise SystemExit(0)

    print("\n需要补项目归属的待办：")
    for row in rows:
        client, position = row_current_project(row)
        print(
            f"  {row['task_id']}. {row['candidate_name']}｜{project_label(client, position)}｜"
            f"{row['match_confidence']}｜{row['intent']}｜{first_line(row['raw_text'], 48)}"
        )
    task_id = prompt_int("选择要修正的待办编号")
    if not task_id:
        raise SystemExit("未选择待办，已退出。")
    args.task_id = task_id

    task = load_task(conn, task_id)
    client, position = row_current_project(task)
    keyword = clean(position or task["candidate_name"])
    positions = load_positions(conn, limit=args.limit, keyword=keyword)
    if not positions and keyword:
        positions = load_positions(conn, limit=args.limit)
    if positions:
        print("\n可选岗位（留空可手填）：")
        for row in positions:
            gap = f"，缺口 {row['gap']}" if row["gap"] is not None else ""
            print(f"  {row['id']}. {row['client']} / {row['position']}（{row['status'] or 'open'}{gap}）")
    position_id = prompt_int("选择岗位编号")
    if position_id:
        args.position_id = position_id
    else:
        args.client = prompt_text("客户", client)
        args.position = prompt_text("岗位", position, required=True)
    args.note = prompt_text("确认备注", args.note or "人工确认项目归属")


def update_confirmation(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    client: str,
    position: str,
    note: str,
    source: str,
) -> dict[str, int]:
    now = datetime.now().isoformat(timespec="seconds")
    task_id = int(task["task_id"])
    reply_id = int(task["reply_id"] or 0)
    old_client, old_position = row_current_project(task)
    old_confidence = clean(task["match_confidence"]) or "unmatched"

    before = conn.total_changes
    conn.execute(
        """
        UPDATE followup_tasks
        SET confirmed_client = ?,
            confirmed_position = ?,
            confirmation_status = 'confirmed',
            confirmation_note = ?,
            confirmed_at = ?,
            match_confidence = 'confirmed',
            updated_at = datetime('now','localtime')
        WHERE id = ?
        """,
        (client, position, note, now, task_id),
    )
    task_updates = conn.total_changes - before

    reply_updates = 0
    if reply_id:
        before = conn.total_changes
        conn.execute(
            """
            UPDATE candidate_replies
            SET confirmed_client = ?,
                confirmed_position = ?,
                confirmation_status = 'confirmed',
                confirmation_note = ?,
                confirmed_at = ?,
                match_confidence = 'confirmed'
            WHERE id = ?
            """,
            (client, position, note, now, reply_id),
        )
        reply_updates = conn.total_changes - before

    candidate_updates = 0
    if task["candidate_id"]:
        before = conn.total_changes
        conn.execute(
            """
            UPDATE candidates
            SET client = ?,
                position = ?,
                updated_at = datetime('now','localtime')
            WHERE id = ?
              AND (COALESCE(client, '') = '' OR COALESCE(position, '') = '' OR source = 'liepin_im')
            """,
            (client, position, int(task["candidate_id"])),
        )
        candidate_updates = conn.total_changes - before

    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO project_confirmations (
            task_id, reply_id, candidate_name, old_client, old_position, old_confidence,
            confirmed_client, confirmed_position, source, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            reply_id or None,
            task["candidate_name"],
            old_client,
            old_position,
            old_confidence,
            client,
            position,
            source,
            note,
        ),
    )
    confirmation_rows = conn.total_changes - before
    cleaned_intelligence = cleanup_old_intelligence(conn, task_id, reply_id, client, position)
    conn.commit()
    return {
        "task_updates": task_updates,
        "reply_updates": reply_updates,
        "candidate_updates": candidate_updates,
        "confirmation_rows": confirmation_rows,
        "cleaned_intelligence": cleaned_intelligence,
    }


def cleanup_old_intelligence(
    conn: sqlite3.Connection,
    task_id: int,
    reply_id: int,
    client: str,
    position: str,
) -> int:
    rows = conn.execute(
        "SELECT id, client, position, evidence_json FROM candidate_intelligence"
    ).fetchall()
    delete_ids: list[int] = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            continue
        matches_task = int(evidence.get("task_id") or 0) == task_id
        matches_reply = reply_id and int(evidence.get("reply_id") or 0) == reply_id
        if (matches_task or matches_reply) and (
            clean(row["client"]) != client or clean(row["position"]) != position
        ):
            delete_ids.append(int(row["id"]))
    if delete_ids:
        conn.executemany("DELETE FROM candidate_intelligence WHERE id = ?", [(item,) for item in delete_ids])
    return len(delete_ids)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    client, position = row_current_project(row)
    result["current_project"] = project_label(client, position)
    result["raw_text"] = first_line(row["raw_text"], 140)
    return result


def write_report(
    output_dir: Path,
    task: sqlite3.Row | None,
    client: str,
    position: str,
    stats: dict[str, int],
    dry_run: bool,
    listed: list[sqlite3.Row] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "list" if listed is not None else ("dryrun" if dry_run else "applied")
    path = output_dir / f"猎聘项目归属修正_{suffix}_{stamp}.md"
    lines = [
        "# 猎聘项目归属修正",
        "",
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
    ]
    if listed is not None:
        lines.extend(
            [
                "",
                "## 待修正清单",
                "",
                "| 待办 | 人选 | 当前项目 | 置信 | 意图 | 原话 |",
                "|---:|---|---|---|---|---|",
            ]
        )
        if listed:
            for row in listed:
                old_client, old_position = row_current_project(row)
                lines.append(
                    f"| {row['task_id']} | {row['candidate_name']} | {project_label(old_client, old_position)} | "
                    f"{row['match_confidence']} | {row['intent']} | {first_line(row['raw_text'], 60).replace('|', '｜')} |"
                )
        else:
            lines.append("| - | 暂无 | - | - | - | - |")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    assert task is not None
    old_client, old_position = row_current_project(task)
    lines.extend(
        [
            f"写入状态：{'未写入（干跑验证）' if dry_run else '已写入确认字段'}",
            "",
            "## 修正内容",
            "",
            f"- 待办编号：{task['task_id']}",
            f"- 回复编号：{task['reply_id'] or '无'}",
            f"- 人选：{task['candidate_name']}",
            f"- 原项目：{project_label(old_client, old_position)}",
            f"- 新项目：{project_label(client, position)}",
            f"- 原置信度：{task['match_confidence']}",
            f"- 原话：{first_line(task['raw_text'], 120)}",
            "",
            "## 写入结果",
            "",
        ]
    )
    for key, value in stats.items():
        lines.append(f"- {key}：{value}")
    lines.extend(
        [
            "",
            "## 下一步",
            "",
            "- 运行一键刷新后，回复驾驶舱、今日优先页、岗位驾驶舱和候选人画像会按新项目重新聚合。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Confirm client/position assignment for Liepin follow-up tasks.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--position-id", type=int)
    parser.add_argument("--client")
    parser.add_argument("--position")
    parser.add_argument("--note")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = connect_db(Path(args.db).expanduser())
    try:
        ensure_schema(conn)
        if args.list:
            listed = load_needs_confirmation(conn, args.limit)
            report = write_report(Path(args.output_dir).expanduser(), None, "", "", {}, False, listed=listed)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "items": [row_to_dict(row) for row in listed],
                        "report": str(report),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.interactive:
            apply_interactive(args, conn)
        if not args.task_id:
            raise SystemExit("需要指定待办编号。可用 --interactive，或先用 --list 查看。")

        task = load_task(conn, args.task_id)
        client, position = resolve_project(args, conn)
        note = clean(args.note) or "人工确认项目归属"
        if args.interactive and not args.dry_run:
            print(
                f"\n准备把待办 #{args.task_id}（{task['candidate_name']}）"
                f"归到：{project_label(client, position)}"
            )
            confirm = prompt_text("确认写入？输入 y 确认", "")
            if confirm.lower() not in {"y", "yes"} and confirm != "确认":
                raise SystemExit("未确认写入，已退出。")
        if args.dry_run:
            stats = {
                "task_updates": 0,
                "reply_updates": 0,
                "candidate_updates": 0,
                "confirmation_rows": 0,
                "cleaned_intelligence": 0,
            }
        else:
            stats = update_confirmation(conn, task, client, position, note, args.source)
    finally:
        conn.close()

    report = write_report(Path(args.output_dir).expanduser(), task, client, position, stats, args.dry_run)
    print(
        json.dumps(
            {
                "ok": True,
                "dry_run": args.dry_run,
                "task_id": args.task_id,
                "project": {"client": client, "position": position},
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
