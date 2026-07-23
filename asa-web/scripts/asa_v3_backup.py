#!/usr/bin/env python3
"""ASA v3 SQLite 在线备份（PRD R13）。

事实源（唯一业务数据库，只读，绝不写入）:
    /Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db

行为:
- 源库一律以 ``mode=ro`` URI 只读打开；备份前先跑 ``PRAGMA integrity_check``，
  校验不过只告警不备份，退出码 1。
- 用 Python sqlite3 backup API 做在线一致性备份（WAL 安全，不锁正式库）。
- 目标目录默认 ``~/.hermes/backups/asa_v3/``（独立于项目目录），
  可用环境变量 ``ASA_V3_BACKUP_DIR`` 或 ``--dir`` 覆盖。
- 文件名 ``asa_v3_YYYYMMDD_HHMMSS_<label>.db``，只轮换本脚本产物（``asa_v3_*.db``），
  保留最近 30 份。
- 备份完成后对副本做 ``PRAGMA integrity_check``，失败即删除副本并报错。

手动执行:
    python3 scripts/asa_v3_backup.py                # 立即备份（标签 manual）
    python3 scripts/asa_v3_backup.py --list         # 列出现有备份
    npm run backup:db                               # 等价手动入口

定时执行: LaunchAgent ``ai.hermes.asa-v3-backup``（见 scripts/launchagents/，
安装脚本 scripts/install_v3_backup_agent.sh）。

输出 JSON；退出码 0 成功 / 1 失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(
    "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
)
DEFAULT_BACKUP_DIR = Path.home() / ".hermes" / "backups" / "asa_v3"
FILENAME_PREFIX = "asa_v3_"
DEFAULT_KEEP = 30


def source_db_path(cli_value: str = "") -> Path:
    """源库路径优先级: --db > ASA_V3_DB > 默认事实源。"""
    raw = cli_value or os.environ.get("ASA_V3_DB", "") or str(DEFAULT_DB)
    return Path(raw).expanduser()


def backup_dir(cli_value: str = "") -> Path:
    """备份目录优先级: --dir > ASA_V3_BACKUP_DIR > ~/.hermes/backups/asa_v3。"""
    raw = cli_value or os.environ.get("ASA_V3_BACKUP_DIR", "") or str(DEFAULT_BACKUP_DIR)
    return Path(raw).expanduser()


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def integrity_check(db_path: Path) -> str:
    """对给定 DB 跑 PRAGMA integrity_check（只读连接），返回 'ok' 或首行错误。"""
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "no result"
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _dest_path(directory: Path, label: str, now: datetime) -> Path:
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label) or "manual"
    stamp = now.strftime("%Y%m%d_%H%M%S")
    dest = directory / f"{FILENAME_PREFIX}{stamp}_{safe_label}.db"
    suffix = 2
    while dest.exists():  # 同一秒重复执行时保证文件名唯一
        dest = directory / f"{FILENAME_PREFIX}{stamp}_{safe_label}_{suffix}.db"
        suffix += 1
    return dest


def make_backup(
    *,
    db_path: Path,
    directory: Path,
    label: str = "manual",
    keep: int = DEFAULT_KEEP,
) -> dict:
    """在线备份 + 副本校验 + 轮换。任何一步失败都抛错前的状态留痕在返回 dict。"""
    if not db_path.exists():
        return {"ok": False, "error": f"源库不存在: {db_path}"}

    verdict = integrity_check(db_path)
    if verdict != "ok":
        return {
            "ok": False,
            "error": "源库 integrity_check 未通过，已跳过备份",
            "integrity_check": verdict,
            "db": str(db_path),
        }

    directory.mkdir(parents=True, exist_ok=True)
    dest = _dest_path(directory, label, datetime.now())

    # 在线一致性备份：源只读打开，backup API 处理 WAL，不锁正式库。
    src = _connect_ro(db_path)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # 副本必须自身可校验，否则备份无意义。
    dest_verdict = integrity_check(dest)
    if dest_verdict != "ok":
        dest.unlink(missing_ok=True)
        return {
            "ok": False,
            "error": "备份副本 integrity_check 未通过，副本已删除",
            "integrity_check": dest_verdict,
            "db": str(db_path),
        }

    # 只轮换本脚本产物（asa_v3_*.db），按文件名时间戳排序，保留最近 keep 份。
    autos = sorted(directory.glob(f"{FILENAME_PREFIX}*.db"))
    removed: list[str] = []
    while len(autos) > max(keep, 1):
        victim = autos.pop(0)
        victim.unlink()
        removed.append(victim.name)

    return {
        "ok": True,
        "db": str(db_path),
        "backup": str(dest),
        "size_bytes": dest.stat().st_size,
        "integrity_check": "ok",
        "kept": len(autos),
        "keep_limit": keep,
        "rotated_out": removed,
    }


def list_backups(directory: Path) -> dict:
    entries = []
    if directory.exists():
        for path in sorted(directory.glob(f"{FILENAME_PREFIX}*.db")):
            stat = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    return {"ok": True, "dir": str(directory), "count": len(entries), "backups": entries}


def latest_backup(directory: Path) -> Path | None:
    """目录内最新一份本脚本备份（供恢复演练与 doctor 新鲜度检查复用）。"""
    if not directory.exists():
        return None
    autos = sorted(directory.glob(f"{FILENAME_PREFIX}*.db"))
    return autos[-1] if autos else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASA v3 SQLite 在线备份（PRD R13）")
    parser.add_argument("--db", default="", help="源库路径（默认取 ASA_V3_DB 或正式事实源）")
    parser.add_argument("--dir", default="", help="备份目录（默认取 ASA_V3_BACKUP_DIR 或 ~/.hermes/backups/asa_v3）")
    parser.add_argument("--label", default="manual", help="备份文件名标签（默认 manual，定时任务用 daily）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留份数（默认 30）")
    parser.add_argument("--list", action="store_true", help="只列出现有备份，不新建")
    args = parser.parse_args(argv)

    directory = backup_dir(args.dir)
    if args.list:
        print(json.dumps(list_backups(directory), ensure_ascii=False, indent=2))
        return 0

    result = make_backup(
        db_path=source_db_path(args.db),
        directory=directory,
        label=args.label,
        keep=args.keep,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
