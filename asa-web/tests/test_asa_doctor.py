#!/usr/bin/env python3
"""PRD R11 契约测试：asa_doctor / asa-release.json。

纯函数与临时夹具测试，不访问真实 Core、正式库或真实备份目录。
"""
from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import asa_doctor  # noqa: E402
import asa_v3_backup  # noqa: E402


def _manifest() -> dict:
    return {
        "schema": 1,
        "components": [
            {"id": "native_app", "name": "ASA 原生 macOS App", "version": "0.2.18", "build": "41", "path": "/x/ASA.app"},
            {"id": "react_app", "name": "React 前端", "version": "1.0.0", "path": "/x/ASA"},
            {"id": "asa_core", "name": "ASA Core", "version": "1.0.0", "base_url": "http://127.0.0.1:8765"},
            {"id": "liepin_extension", "name": "猎聘扩展", "version": "0.3.11", "path": "/x/liepin"},
            {"id": "xsaas_extension", "name": "X-SaaS 扩展", "version": "0.1.22", "path": "/x/xsaas"},
            {"id": "opencli_extension", "name": "OpenCLI 扩展", "version": "1.0.22", "path": "/x/opencli"},
            {"id": "v3_db", "name": "v3 库", "path": "/x/talent_system_v3_20260629.db", "backup_dir": "/x/backups"},
        ],
    }


class HealthEvalTest(unittest.TestCase):
    def test_ok(self) -> None:
        payload = {"ok": True, "service": "asa-core", "version": "1.0.0", "db": "/x/talent_system_v3_20260629.db"}
        self.assertEqual(asa_doctor.evaluate_health(payload, _manifest())["level"], "ok")

    def test_wrong_db_fails(self) -> None:
        payload = {"ok": True, "version": "1.0.0", "db": "/tmp/other.db"}
        check = asa_doctor.evaluate_health(payload, _manifest())
        self.assertEqual(check["level"], "fail")
        self.assertIn("不符", check["detail"])

    def test_version_mismatch_fails(self) -> None:
        payload = {"ok": True, "version": "9.9.9", "db": "/x/talent_system_v3_20260629.db"}
        self.assertEqual(asa_doctor.evaluate_health(payload, _manifest())["level"], "fail")

    def test_not_ok_fails(self) -> None:
        self.assertEqual(asa_doctor.evaluate_health({"ok": False}, _manifest())["level"], "fail")


class VersionConsistencyTest(unittest.TestCase):
    def test_all_match(self) -> None:
        live = {
            "native_app": {"version": "0.2.18", "build": "41"},
            "react_app": {"version": "1.0.0"},
            "liepin_extension": {"version": "0.3.11"},
            "xsaas_extension": {"version": "0.1.22"},
            "opencli_extension": {"version": "1.0.22"},
        }
        results = asa_doctor.evaluate_version_consistency(_manifest(), live)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(item["level"] == "ok" for item in results))

    def test_mismatch_fails_with_hint(self) -> None:
        live = {
            "native_app": {"version": "0.2.18", "build": "41"},
            "react_app": {"version": "1.0.0"},
            "liepin_extension": {"version": "0.3.12"},  # 扩展升级但 manifest 未刷新
            "xsaas_extension": {"version": "0.1.22"},
            "opencli_extension": {"version": "1.0.22"},
        }
        results = asa_doctor.evaluate_version_consistency(_manifest(), live)
        failed = [item for item in results if item["level"] == "fail"]
        self.assertEqual(len(failed), 1)
        self.assertIn("--update-manifest", failed[0]["detail"])

    def test_probe_error_fails(self) -> None:
        results = asa_doctor.evaluate_version_consistency(_manifest(), {"liepin_extension": {"error": "FileNotFoundError"}})
        self.assertTrue(any(item["level"] == "fail" for item in results))


class BackupFreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="asa_doctor_test_")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _touch_backup(self, name: str, age_hours: float) -> Path:
        path = self.dir / name
        path.write_bytes(b"db")
        mtime = (datetime.now() - timedelta(hours=age_hours)).timestamp()
        path.touch()
        import os
        os.utime(path, (mtime, mtime))
        return path

    def test_no_backup_fails(self) -> None:
        self.assertEqual(asa_doctor.evaluate_backup_freshness(self.dir)["level"], "fail")

    def test_fresh_backup_ok(self) -> None:
        self._touch_backup(f"{asa_v3_backup.FILENAME_PREFIX}20260722_094100_daily.db", 3)
        check = asa_doctor.evaluate_backup_freshness(self.dir)
        self.assertEqual(check["level"], "ok")
        self.assertIn("3", check["detail"])

    def test_stale_backup_fails(self) -> None:
        self._touch_backup(f"{asa_v3_backup.FILENAME_PREFIX}20260721_094100_daily.db", 25)
        check = asa_doctor.evaluate_backup_freshness(self.dir)
        self.assertEqual(check["level"], "fail")
        self.assertIn("24", check["detail"])


class ProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="asa_doctor_probe_")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_probe_native_app(self) -> None:
        info = self.root / "ASA.app" / "Contents"
        info.mkdir(parents=True)
        with (info / "Info.plist").open("wb") as fh:
            plistlib.dump({"CFBundleShortVersionString": "0.2.18", "CFBundleVersion": "41"}, fh)
        self.assertEqual(asa_doctor.probe_native_app(self.root / "ASA.app"), {"version": "0.2.18", "build": "41"})

    def test_probe_package_and_extension(self) -> None:
        (self.root / "package.json").write_text(json.dumps({"name": "asa-web", "version": "1.0.0"}), encoding="utf-8")
        self.assertEqual(asa_doctor.probe_package_version(self.root)["version"], "1.0.0")
        (self.root / "manifest.json").write_text(json.dumps({"name": "ext", "version": "0.3.11"}), encoding="utf-8")
        self.assertEqual(asa_doctor.probe_extension_version(self.root)["version"], "0.3.11")


class ReportTest(unittest.TestCase):
    def test_exit_code(self) -> None:
        ok = [{"id": "a", "title": "t", "level": "ok", "detail": "d"}]
        warn = ok + [{"id": "b", "title": "t", "level": "warn", "detail": "d"}]
        fail = warn + [{"id": "c", "title": "t", "level": "fail", "detail": "d"}]
        self.assertEqual(asa_doctor.exit_code(ok), 0)
        self.assertEqual(asa_doctor.exit_code(warn), 0)
        self.assertEqual(asa_doctor.exit_code(fail), 1)

    def test_render_report_counts(self) -> None:
        checks = [
            {"id": "a", "title": "甲", "level": "ok", "detail": "x"},
            {"id": "b", "title": "乙", "level": "warn", "detail": "y"},
            {"id": "c", "title": "丙", "level": "fail", "detail": "z"},
        ]
        text = asa_doctor.render_report(checks)
        self.assertIn("1 通过 / 1 警告 / 1 失败", text)
        self.assertIn("✗ 丙", text)


class ManifestFileTest(unittest.TestCase):
    def test_repo_manifest_shape(self) -> None:
        """仓库根的 asa-release.json 必须覆盖 7 个组件且带版本。"""
        path = REPO_ROOT / "asa-release.json"
        if not path.exists():
            self.skipTest("asa-release.json 尚未生成")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        ids = {comp.get("id") for comp in manifest.get("components", [])}
        self.assertEqual(
            ids,
            {"native_app", "react_app", "asa_core", "liepin_extension", "xsaas_extension", "opencli_extension", "v3_db"},
        )
        for comp in manifest["components"]:
            self.assertTrue(comp.get("version"), f"{comp['id']} 缺 version")
            self.assertTrue(comp.get("path") or comp.get("source_path"), f"{comp['id']} 缺路径")


class LaunchAgentEvalTest(unittest.TestCase):
    def test_all_running_ok(self) -> None:
        probed = [
            {"label": "ai.hermes.liepin-workbench", "loaded": True, "state": "running", "last_exit": None},
            {"label": "ai.hermes.asa-v3-backup", "loaded": True, "state": "not running", "last_exit": "0"},
            {"label": "ai.hermes.chrome-cdp", "loaded": True, "state": "running", "last_exit": "(never exited)"},
        ]
        levels = [c["level"] for c in asa_doctor.evaluate_launchagents(probed)]
        self.assertEqual(levels, ["ok", "ok", "ok"])

    def test_core_down_fails(self) -> None:
        probed = [{"label": "ai.hermes.liepin-workbench", "loaded": True, "state": "not running", "last_exit": "1"}]
        self.assertEqual(asa_doctor.evaluate_launchagents(probed)[0]["level"], "fail")

    def test_core_active_ok(self) -> None:
        probed = [{"label": "ai.hermes.liepin-workbench", "loaded": True, "state": "active", "last_exit": None}]
        self.assertEqual(asa_doctor.evaluate_launchagents(probed)[0]["level"], "ok")

    def test_not_loaded_levels(self) -> None:
        probed = [
            {"label": "ai.hermes.liepin-workbench", "loaded": False},
            {"label": "ai.hermes.asa-v3-backup", "loaded": False},
        ]
        levels = [c["level"] for c in asa_doctor.evaluate_launchagents(probed)]
        self.assertEqual(levels, ["fail", "warn"])

    def test_backup_bad_exit_warns(self) -> None:
        probed = [{"label": "ai.hermes.asa-v3-backup", "loaded": True, "state": "not running", "last_exit": "1"}]
        self.assertEqual(asa_doctor.evaluate_launchagents(probed)[0]["level"], "warn")


class CdpEvalTest(unittest.TestCase):
    def test_reachable_ok(self) -> None:
        self.assertEqual(asa_doctor.evaluate_cdp({"reachable": True})["level"], "ok")

    def test_unreachable_warns_only(self) -> None:
        self.assertEqual(asa_doctor.evaluate_cdp({"reachable": False, "error": "refused"})["level"], "warn")


class GitEvalTest(unittest.TestCase):
    def test_clean_ok(self) -> None:
        probed = [{"repo": str(asa_doctor.REPO_ROOT), "dirty": 0, "sample": []}]
        self.assertEqual(asa_doctor.evaluate_git(probed)[0]["level"], "ok")

    def test_dirty_warns_with_sample(self) -> None:
        probed = [{"repo": str(asa_doctor.REPO_ROOT), "dirty": 2, "sample": ["M src/api.ts", "?? e2e/x.ts"]}]
        check = asa_doctor.evaluate_git(probed)[0]
        self.assertEqual(check["level"], "warn")
        self.assertIn("src/api.ts", check["detail"])

    def test_error_warns(self) -> None:
        probed = [{"repo": "/nonexistent", "error": "not a git repository"}]
        self.assertEqual(asa_doctor.evaluate_git(probed)[0]["level"], "warn")


if __name__ == "__main__":
    unittest.main()
