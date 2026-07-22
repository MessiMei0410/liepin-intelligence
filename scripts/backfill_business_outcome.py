#!/usr/bin/env python3
"""回填存量工作流的 business_outcome（业务终态）列。

默认 dry-run：只打印逐条决策明细，不写库；加 --apply 才执行 UPDATE。
只更新 agent_workflows / agent_goals 的 business_outcome 列，不触碰其他字段（含 updated_at）。
加列迁移由 a_system_agent.schema.ensure_schema 幂等完成，本脚本开头会调用一次（老库自动补齐）。

用法：
    python3 scripts/backfill_business_outcome.py --db /path/to/talent_system.db            # dry-run
    python3 scripts/backfill_business_outcome.py --db /path/to/talent_system.db --apply    # 实际写库
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_system_agent.schema import ensure_schema
from a_system_agent.workflow import _loads, classify_business_outcome, sourcing_target_stats

TERMINAL_STATUSES = ("blocked", "completed", "failed")


def collect_terminal_workflows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT w.workflow_id,w.status,w.business_outcome AS workflow_outcome,
               g.goal_id,g.business_outcome AS goal_outcome,g.objective,g.context_json
        FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
        WHERE w.status IN ('blocked','completed','failed')
        ORDER BY w.id
        """
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 agent_workflows/agent_goals 的 business_outcome（业务终态）")
    parser.add_argument("--db", required=True, help="目标 SQLite 数据库路径")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="只打印决策明细，不写库（默认）")
    mode.add_argument("--apply", dest="apply", action="store_true", help="实际写库（仅 UPDATE business_outcome 列）")
    parser.set_defaults(apply=False)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.commit()
        rows = collect_terminal_workflows(conn)
        print(f"模式：{'APPLY（写库）' if args.apply else 'DRY-RUN（只读）'}；终局工作流共 {len(rows)} 条")
        decisions: Counter[str] = Counter()
        changed = 0
        for row in rows:
            workflow_id = str(row["workflow_id"])
            outcome = classify_business_outcome(conn, workflow_id)
            stats = sourcing_target_stats(conn, row["objective"], _loads(row["context_json"], {}), workflow_id)
            current = row["workflow_outcome"] if row["workflow_outcome"] is not None else row["goal_outcome"]
            decisions[outcome or "(none)"] += 1
            if stats is None:
                detail = "非寻访类目标"
            else:
                detail = (
                    f"target={stats['target']},assessed={stats['assessed']},"
                    f"score_75_plus={stats['score_75_plus']},verify_first={stats['verify_first']},"
                    f"low_score={stats['low_score']}"
                )
            action = "skip"
            if outcome and outcome != current:
                changed += 1
                action = "update" if args.apply else "would-update"
                if args.apply:
                    conn.execute(
                        "UPDATE agent_workflows SET business_outcome=? WHERE workflow_id=?",
                        (outcome, workflow_id),
                    )
                    conn.execute(
                        "UPDATE agent_goals SET business_outcome=? WHERE goal_id=?",
                        (outcome, row["goal_id"]),
                    )
            print(
                f"[{action:13}] {workflow_id} status={row['status']:<9} "
                f"outcome: {current or 'NULL'} -> {outcome or 'NULL'} ({detail})"
            )
        if args.apply:
            conn.commit()
        print("分类统计：" + ", ".join(f"{key}={count}" for key, count in sorted(decisions.items())))
        print(f"{'已更新' if args.apply else '待更新（dry-run）'} {changed} 条；跳过 {len(rows) - changed} 条")
        if not args.apply:
            print("确认无误后加 --apply 实际写库。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
