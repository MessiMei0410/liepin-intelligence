#!/usr/bin/env python3
"""ASA v3 备份恢复演练（PRD R13）。

把最新备份（或 ``--backup`` 指定的一份）还原到系统临时目录，然后:
1. 对还原副本跑 ``PRAGMA integrity_check``；
2. 对比还原副本与正式库（只读）的关键表行数:
   jobs / candidates / job_candidates / candidate_events；
3. 把演练结果追加写入仓库根目录 RESTORE_DRILL.md。

红线:
- 绝不还原覆盖正式库；还原目标只在临时目录，演练结束即清理。
- 正式库始终以 ``mode=ro`` 只读打开。

用法:
    python3 scripts/asa_v3_restore_drill.py            # 演练最新备份
    python3 scripts/asa_v3_restore_drill.py --fresh    # 先做一次新备份再演练（推荐：行数对比不受备份后写入影响）
    python3 scripts/asa_v3_restore_drill.py --backup /path/to/asa_v3_xxx.db
    npm run drill:db

输出 JSON；退出码 0 通过 / 1 失败。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asa_v3_backup  # noqa: E402

DRILL_TABLES = ("jobs", "candidates", "job_candidates", "candidate_events")
RECORD_FILE = Path(__file__).resolve().parent.parent / "RESTORE_DRILL.md"

RECORD_HEADER = """# ASA v3 备份恢复演练记录（PRD R13）

每次演练把备份还原到系统临时目录（绝不覆盖正式库），校验 `PRAGMA integrity_check`
并对比关键表行数（jobs / candidates / job_candidates / candidate_events）。

- 备份脚本：`scripts/asa_v3_backup.py`（LaunchAgent `ai.hermes.asa-v3-backup` 每日执行）
- 演练命令：`python3 scripts/asa_v3_restore_drill.py --fresh`（先备后演，行数对比不受备份后写入影响）
- 备份目录：`~/.hermes/backups/asa_v3/`（独立于项目目录，不纳入 git）

"""


def _row_counts(db_path: Path) -> dict[str, int]:
    conn = asa_v3_backup._connect_ro(db_path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in DRILL_TABLES
        }
    finally:
        conn.close()


def run_drill(
    *,
    backup_path: Path,
    live_db: Path,
    record_file: Path = RECORD_FILE,
) -> dict:
    """单轮演练：还原到临时目录 → integrity_check → 行数对比 → 写记录。"""
    if not backup_path.exists():
        return {"ok": False, "error": f"备份不存在: {backup_path}"}
    if not live_db.exists():
        return {"ok": False, "error": f"正式库不可达: {live_db}"}
    if backup_path.resolve() == live_db.resolve():
        return {"ok": False, "error": "拒绝把正式库本身当作演练输入"}

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    temp_dir = Path(tempfile.mkdtemp(prefix="asa_v3_restore_drill_"))
    restored = temp_dir / backup_path.name
    try:
        # “还原”：备份即完整 DB 文件，复制到临时目标路径即完成还原。
        shutil.copyfile(backup_path, restored)
        integrity = asa_v3_backup.integrity_check(restored)
        restored_counts = _row_counts(restored)
        live_counts = _row_counts(live_db)
        counts_match = restored_counts == live_counts
        ok = integrity == "ok" and counts_match
        result = {
            "ok": ok,
            "ts": ts,
            "backup": backup_path.name,
            "restored_to": str(restored),
            "integrity_check": integrity,
            "row_counts_match": counts_match,
            "live": live_counts,
            "restored": restored_counts,
        }
    except Exception as exc:  # 演练失败也要留痕
        result = {
            "ok": False,
            "ts": ts,
            "backup": backup_path.name,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    _append_record(record_file, result)
    return result


def _append_record(record_file: Path, result: dict) -> None:
    verdict = "通过" if result.get("ok") else "失败"
    entry = f"\n## {result.get('ts', '')} — {verdict}\n\n```json\n{json.dumps(result, ensure_ascii=False, indent=2)}\n```\n"
    if record_file.exists():
        existing = record_file.read_text(encoding="utf-8")
        if not existing.startswith("# ASA v3 备份恢复演练记录"):
            existing = RECORD_HEADER + existing
    else:
        existing = RECORD_HEADER
    record_file.write_text(existing.rstrip("\n") + "\n" + entry, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASA v3 备份恢复演练（PRD R13）")
    parser.add_argument("--db", default="", help="正式库路径（默认取 ASA_V3_DB 或正式事实源，只读）")
    parser.add_argument("--dir", default="", help="备份目录（默认取 ASA_V3_BACKUP_DIR 或 ~/.hermes/backups/asa_v3）")
    parser.add_argument("--backup", default="", help="指定演练的备份文件（默认取备份目录内最新一份）")
    parser.add_argument("--fresh", action="store_true", help="先做一次新备份（标签 drill）再演练最新备份")
    args = parser.parse_args(argv)

    live_db = asa_v3_backup.source_db_path(args.db)
    directory = asa_v3_backup.backup_dir(args.dir)

    if args.fresh:
        made = asa_v3_backup.make_backup(db_path=live_db, directory=directory, label="drill")
        if not made.get("ok"):
            print(json.dumps(made, ensure_ascii=False, indent=2))
            return 1

    backup_path = Path(args.backup).expanduser() if args.backup else asa_v3_backup.latest_backup(directory)
    if backup_path is None:
        result = {"ok": False, "error": f"备份目录内没有可演练的备份: {directory}"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    result = run_drill(backup_path=backup_path, live_db=live_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
