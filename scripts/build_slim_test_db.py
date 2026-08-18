#!/usr/bin/env python3
"""从正式库导出精简测试库（tests fixture 复制它，而不是 1.9GB 正式库）。

正式库约 1.9GB，其中 ~1.3GB 是审计/快照类大 JSON 列（payload_json、after_json、
response_json 等）。测试断言只依赖行存在性/相对增量/自有行内容，从不解析这些
存量大列（见 tests/ 下 source.backup fixture 注释）。本脚本：

1. sqlite backup 正式库（只读连接，WAL 下在线备份安全）到临时文件；
2. 将 TRIMMED_COLUMNS 里的存量大列替换为小占位值（'{}'/'')，行全部保留；
3. VACUUM + journal_mode=DELETE，原子 rename 到目标路径。

输出 <150MB，fixture 复制耗时从 ~3-7s/模块 降到 <0.5s，且测试期查询不再扫大列。

用法（可重复执行，正式库只读）：
    python3 scripts/build_slim_test_db.py
    python3 scripts/build_slim_test_db.py --source /path/to/talent_system_v3.db --output /tmp/slim.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path

SOURCE_DB_DEFAULT = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")

# (table, column, replacement)。只替换存量行内容，行数/主键/其他列原样保留，
# 测试自建行（唯一 request_id/session_id/workflow_id）在复制后写入，不受影响。
TRIMMED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agent_context_snapshots", "payload_json", "'{}'"),
    ("audit_events", "after_json", "'{}'"),
    ("api_idempotency", "response_json", "'{}'"),
    ("agent_copilot_messages", "structured_json", "'{}'"),
    ("agent_candidate_recalls", "raw_json", "'{}'"),
    ("agent_step_events", "detail_json", "'{}'"),
    ("agent_approvals", "preflight_json", "'{}'"),
    ("agent_workflow_steps", "output_json", "'{}'"),
    ("agent_artifacts", "content", "''"),
    ("agent_artifacts", "metadata_json", "'{}'"),
    ("agent_candidate_assessments", "criteria_json", "'{}'"),
    ("agent_candidate_assessments", "reviewer_json", "'{}'"),
    # candidate_events.raw_json / source_profiles.raw_json 不截断：合计仅 ~19MB，
    # 且生产代码路径会读存量行（如 sourcing_handler._ensure_sourcing_attribution
    # 从 search_shortlisted 事件 raw_json 取 source_query；知识提案聚类读停止原因）。
)


def default_output(source: Path) -> Path:
    return source.with_name(source.stem + "_slim_test.db")


def build_slim_db(source: Path, output: Path) -> None:
    if not source.exists():
        raise SystemExit(f"正式库不存在：{source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".tmp.db")
    tmp.unlink(missing_ok=True)

    started = time.monotonic()
    # 只读在线备份：WAL 模式下 backup API 与写入方（Core）并发安全。
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[1/3] backup 完成：{tmp.stat().st_size / 1e6:.0f}MB（{time.monotonic() - started:.1f}s）")

    conn = sqlite3.connect(tmp)
    try:
        for table, column, replacement in TRIMMED_COLUMNS:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                print(f"  跳过（表不存在）：{table}")
                continue
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                print(f"  跳过（列不存在）：{table}.{column}")
                continue
            cursor = conn.execute(f'UPDATE "{table}" SET "{column}" = {replacement}')
            print(f"  截断 {table}.{column}：{cursor.rowcount} 行")
        conn.commit()
        # 转回 DELETE 日志模式并收 WAL，产物是单文件，fixture 可直接复制。
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, output)
    print(f"[3/3] 完成：{output} {output.stat().st_size / 1e6:.1f}MB（总 {time.monotonic() - started:.1f}s）")


def main() -> None:
    parser = argparse.ArgumentParser(description="从正式库导出精简测试库")
    parser.add_argument("--source", type=Path, default=Path(os.environ.get("ASA_SOURCE_DB", SOURCE_DB_DEFAULT)))
    parser.add_argument("--output", type=Path, default=None, help="缺省：<source同名>_slim_test.db，可用 ASA_SLIM_TEST_DB 覆盖")
    args = parser.parse_args()
    if args.output is not None:
        output = args.output
    elif os.environ.get("ASA_SLIM_TEST_DB"):
        output = Path(os.environ["ASA_SLIM_TEST_DB"])
    else:
        output = default_output(args.source)
    build_slim_db(args.source, output)


if __name__ == "__main__":
    main()
