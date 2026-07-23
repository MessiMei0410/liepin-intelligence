#!/usr/bin/env python3
"""PRD R13 契约测试：asa_v3_backup / asa_v3_restore_drill。

全部用临时目录里的临时 SQLite 库，绝不触碰正式 v3 库与真实备份目录。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import asa_v3_backup  # noqa: E402
import asa_v3_restore_drill  # noqa: E402


def _make_source_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        for table in asa_v3_restore_drill.DRILL_TABLES:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, note TEXT)")
            conn.executemany(
                f"INSERT INTO {table} (note) VALUES (?)",
                [(f"row-{i}",) for i in range(rows)],
            )
        conn.commit()
    finally:
        conn.close()


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="asa_v3_backup_test_")
        self.root = Path(self._tmp.name)
        self.db = self.root / "source.db"
        self.backup_dir = self.root / "backups"
        _make_source_db(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_integrity_check_ok(self) -> None:
        self.assertEqual(asa_v3_backup.integrity_check(self.db), "ok")

    def test_make_backup_copies_rows_and_verifies(self) -> None:
        result = asa_v3_backup.make_backup(db_path=self.db, directory=self.backup_dir, label="manual")
        self.assertTrue(result["ok"], result)
        backup = Path(result["backup"])
        self.assertTrue(backup.exists())
        self.assertTrue(backup.name.startswith(asa_v3_backup.FILENAME_PREFIX))
        # 副本自身完整且行数一致
        self.assertEqual(asa_v3_backup.integrity_check(backup), "ok")
        for table in asa_v3_restore_drill.DRILL_TABLES:
            with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 3)

    def test_rotation_keeps_only_own_prefix(self) -> None:
        for i in range(5):
            result = asa_v3_backup.make_backup(db_path=self.db, directory=self.backup_dir, label=f"r{i}", keep=3)
            self.assertTrue(result["ok"], result)
        kept = sorted(self.backup_dir.glob(f"{asa_v3_backup.FILENAME_PREFIX}*.db"))
        self.assertEqual(len(kept), 3)
        # 手工/外来文件不参与轮换
        foreign = self.backup_dir / "talent_system_v3_auto_20200101_000000_daily.db"
        foreign.write_bytes(b"keep-me")
        result = asa_v3_backup.make_backup(db_path=self.db, directory=self.backup_dir, label="rx", keep=3)
        self.assertTrue(result["ok"], result)
        self.assertTrue(foreign.exists())
        self.assertEqual(len(sorted(self.backup_dir.glob(f"{asa_v3_backup.FILENAME_PREFIX}*.db"))), 3)

    def test_missing_source_refused(self) -> None:
        result = asa_v3_backup.make_backup(
            db_path=self.root / "nope.db", directory=self.backup_dir, label="manual"
        )
        self.assertFalse(result["ok"])
        self.assertIn("源库不存在", result["error"])

    def test_corrupt_source_skipped(self) -> None:
        bad = self.root / "bad.db"
        bad.write_bytes(b"this is not a sqlite database")
        result = asa_v3_backup.make_backup(db_path=bad, directory=self.backup_dir, label="manual")
        self.assertFalse(result["ok"])
        self.assertFalse(list(self.backup_dir.glob(f"{asa_v3_backup.FILENAME_PREFIX}*.db")))

    def test_latest_backup(self) -> None:
        self.assertIsNone(asa_v3_backup.latest_backup(self.backup_dir))
        asa_v3_backup.make_backup(db_path=self.db, directory=self.backup_dir, label="a")
        latest = asa_v3_backup.latest_backup(self.backup_dir)
        self.assertIsNotNone(latest)
        self.assertTrue(str(latest).endswith("_a.db"))


class RestoreDrillTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="asa_v3_drill_test_")
        self.root = Path(self._tmp.name)
        self.live = self.root / "live.db"
        self.backup_dir = self.root / "backups"
        self.record = self.root / "RESTORE_DRILL.md"
        _make_source_db(self.live)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_drill_passes_on_matching_backup(self) -> None:
        made = asa_v3_backup.make_backup(db_path=self.live, directory=self.backup_dir, label="drill")
        self.assertTrue(made["ok"], made)
        result = asa_v3_restore_drill.run_drill(
            backup_path=Path(made["backup"]), live_db=self.live, record_file=self.record
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertTrue(result["row_counts_match"])
        self.assertEqual(result["live"], result["restored"])
        # 临时还原目录已清理
        self.assertFalse(Path(result["restored_to"]).exists())
        # 记录文件已写入且包含表名与结论
        text = self.record.read_text(encoding="utf-8")
        self.assertIn("job_candidates", text)
        self.assertIn("通过", text)

    def test_drill_flags_row_mismatch(self) -> None:
        made = asa_v3_backup.make_backup(db_path=self.live, directory=self.backup_dir, label="drill")
        self.assertTrue(made["ok"], made)
        with sqlite3.connect(str(self.live)) as conn:  # 备份后正式库新增一行 → 对比应不一致
            conn.execute("INSERT INTO jobs (note) VALUES ('after-backup')")
        result = asa_v3_restore_drill.run_drill(
            backup_path=Path(made["backup"]), live_db=self.live, record_file=self.record
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["row_counts_match"])
        self.assertIn("失败", self.record.read_text(encoding="utf-8"))

    def test_drill_refuses_live_db_as_input(self) -> None:
        result = asa_v3_restore_drill.run_drill(
            backup_path=self.live, live_db=self.live, record_file=self.record
        )
        self.assertFalse(result["ok"])
        self.assertIn("拒绝", result["error"])

    def test_record_header_written_once(self) -> None:
        made = asa_v3_backup.make_backup(db_path=self.live, directory=self.backup_dir, label="drill")
        backup = Path(made["backup"])
        asa_v3_restore_drill.run_drill(backup_path=backup, live_db=self.live, record_file=self.record)
        asa_v3_restore_drill.run_drill(backup_path=backup, live_db=self.live, record_file=self.record)
        text = self.record.read_text(encoding="utf-8")
        self.assertEqual(text.count("# ASA v3 备份恢复演练记录"), 1)
        self.assertEqual(text.count("```json"), 2)


if __name__ == "__main__":
    unittest.main()
