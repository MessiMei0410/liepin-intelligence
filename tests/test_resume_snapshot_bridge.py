"""简历快照桥接（扩展直推通道）测试：合成数据，CI 可运行。

覆盖：
- update_resume_snapshot：surface 校验（仅 liepin）、resume_id 缺失 400、
  full_text 缺失 400、字段截断上限、按 resume_id 覆盖更新；
- latest_resume_snapshot：精确取/最新取、TTL 过期视为无快照、LRU 上限清理；
- 扩展 content.js 源码契约：快照直推端点、payload 字段（full_text/resume_id/
  captured_at）、签名去重（页面没变不重复推）、manifest 版本已升（AGENTS.md
  硬性约定：改扩展必须升 version）。
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import liepin_workbench_server as bridge  # noqa: E402


def _payload(resume_id: str = "res-1", full_text: str = "完整简历全文 " * 100, **resume_extra) -> dict:
    return {
        "surface": "liepin",
        "url": f"https://h.liepin.com/resume/showresumedetail/?res_id_encode={resume_id}",
        "captured_at": "2026-08-19T10:00:00",
        "instance_id": "tab-1",
        "resume": {
            "resume_id": resume_id,
            "name": "杜明",
            "company": "华虹半导体",
            "title": "设备工程师",
            "work_text": "工作经历段 " * 50,
            "project_text": "",
            "education_text": "",
            "full_text": full_text,
            **resume_extra,
        },
    }


class ResumeSnapshotBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        bridge.clear_resume_snapshots()

    def tearDown(self) -> None:
        bridge.clear_resume_snapshots()

    def test_rejects_non_liepin_surface(self) -> None:
        with self.assertRaises(ValueError):
            bridge.update_resume_snapshot({**_payload(), "surface": "xsaas"})

    def test_rejects_missing_resume_id(self) -> None:
        payload = _payload()
        payload["resume"]["resume_id"] = ""
        payload["url"] = "https://h.liepin.com/resume/showresumedetail/"
        with self.assertRaises(ValueError):
            bridge.update_resume_snapshot(payload)

    def test_resume_id_falls_back_to_url(self) -> None:
        payload = _payload()
        payload["resume"]["resume_id"] = ""
        result = bridge.update_resume_snapshot(payload)
        assert result["resume_id"] == "res-1"

    def test_rejects_missing_full_text(self) -> None:
        with self.assertRaises(ValueError):
            bridge.update_resume_snapshot(_payload(full_text=""))

    def test_latest_by_resume_id_and_overwrite(self) -> None:
        bridge.update_resume_snapshot(_payload(resume_id="res-a"))
        bridge.update_resume_snapshot(_payload(resume_id="res-b"))
        bridge.update_resume_snapshot(_payload(resume_id="res-a", full_text="更新后的全文 " * 100))
        snapshot = bridge.latest_resume_snapshot("res-a")
        assert snapshot and "更新后的全文" in snapshot["resume"]["full_text"]
        # 缺省取最新一条（res-a 刚更新）。
        assert bridge.latest_resume_snapshot()["resume_id"] == "res-a"

    def test_expired_snapshot_is_invisible(self) -> None:
        bridge.update_resume_snapshot(_payload())
        with bridge.ASA_FLOATING_LOCK:
            bridge.ASA_FLOATING_RESUME_SNAPSHOTS["res-1"]["received_at"] = (
                time.time() - bridge.ASA_FLOATING_RESUME_SNAPSHOT_TTL_SECONDS - 10
            )
        assert bridge.latest_resume_snapshot("res-1") is None
        assert bridge.latest_resume_snapshot() is None

    def test_store_is_lru_bounded(self) -> None:
        for index in range(bridge.ASA_FLOATING_RESUME_SNAPSHOT_LIMIT + 5):
            bridge.update_resume_snapshot(_payload(resume_id=f"res-{index}"))
        with bridge.ASA_FLOATING_LOCK:
            remaining = len(bridge.ASA_FLOATING_RESUME_SNAPSHOTS)
        assert remaining == bridge.ASA_FLOATING_RESUME_SNAPSHOT_LIMIT
        # 最旧的 5 条已被清掉。
        assert bridge.latest_resume_snapshot("res-0") is None
        assert bridge.latest_resume_snapshot(f"res-{bridge.ASA_FLOATING_RESUME_SNAPSHOT_LIMIT + 4}") is not None

    def test_full_text_is_not_truncated_at_bridge_context_limit(self) -> None:
        # 浮窗上下文 sanitize 限 4000 字；快照通道必须保留完整全文（上限 200k）。
        full_text = "长文本" * 20000  # 60000 字
        bridge.update_resume_snapshot(_payload(full_text=full_text))
        snapshot = bridge.latest_resume_snapshot("res-1")
        assert snapshot and len(snapshot["resume"]["full_text"]) == len(full_text)


class ResumeSnapshotExtensionContractTest(unittest.TestCase):
    def test_content_script_pushes_snapshot_with_full_text(self) -> None:
        source = (ROOT / "liepin-reply-assistant-extension" / "content.js").read_text(encoding="utf-8")
        self.assertIn("/api/asa/floating/resume-snapshot", source)
        self.assertIn("full_text: fullText", source)
        self.assertIn("resume_id: resumeId", source)
        self.assertIn("captured_at", source)
        # 签名去重：页面内容没变就不重复推。
        self.assertIn("resumeSnapshotSignature", source)
        self.assertIn("lastResumeSnapshotSignature", source)

    def test_manifest_version_bumped(self) -> None:
        manifest = json.loads((ROOT / "liepin-reply-assistant-extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(tuple(map(int, manifest["version"].split("."))), (0, 3, 13))


if __name__ == "__main__":
    unittest.main()
