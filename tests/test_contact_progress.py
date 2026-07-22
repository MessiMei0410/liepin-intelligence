from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py")
BUILDER_PATH = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py")
SERVER_PATH = ROOT / "scripts" / "liepin_workbench_server.py"
DOCTOR_PATH = Path("/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_db_doctor.py")


def load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = load_module("talent_system_sync_contact_test", SYNC_PATH)
server = load_module("liepin_workbench_server_contact_test", SERVER_PATH)


class ContactProgressTest(unittest.TestCase):
    def test_wechat_requested_maps_to_waiting_for_acceptance(self) -> None:
        progress = sync.CONTACT_PROGRESS["wechat_requested"]
        self.assertEqual(progress["stage"], "X3 已申请加微信/待通过")
        self.assertEqual(progress["flow_bucket"], "已联系/等待微信通过")
        self.assertEqual(progress["channel"], "wechat")

    def test_contact_status_is_inferred_from_legacy_note(self) -> None:
        self.assertEqual(sync.infer_contact_status("已发送加微信请求"), "wechat_requested")
        self.assertEqual(sync.infer_contact_status("微信已通过，明天沟通"), "wechat_connected")

    def test_stop_review_rejects_contact_progress_before_writing(self) -> None:
        row = {
            "job_candidate_id": 509,
            "person_id": 1,
            "job_id": 1,
            "display_name": "聂松亮",
            "client": "长越科技",
            "job": "自动化软件高级工程师",
        }
        action = {
            "kind": "resume_review",
            "action_id": "stop-with-contact",
            "review_result": "stop",
            "summary": "停止",
            "contact_status": "wechat_requested",
        }
        with mock.patch.object(sync, "existing_event_count", return_value=0), mock.patch.object(
            sync, "resolve_action_candidate", return_value=("unique", row, [])
        ), mock.patch.object(sync, "insert_linked_event") as insert_event:
            result = sync.process_action(
                mock.Mock(),
                action,
                batch_source="test",
                default_event_time="2026-07-13 12:00:00",
                default_source_thread_id="test-thread",
                index=0,
                dry_run=True,
            )
        self.assertEqual(result["reason"], "stop_review_cannot_record_contact_progress")
        insert_event.assert_not_called()

    def test_server_preserves_structured_contact_fields(self) -> None:
        action = server.build_talent_action(
            {
                "kind": "resume_review",
                "job_candidate_id": 509,
                "review_result": "continue",
                "contact_status": "wechat_requested",
                "contact_channel": "wechat",
                "contact_note": "已发送加微信请求",
            }
        )
        self.assertEqual(action["contact_status"], "wechat_requested")
        self.assertEqual(action["raw"]["contact_status"], "wechat_requested")

    def test_latest_outreach_query_includes_contact_event(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        latest_outreach = source[source.index("latest_outreach AS"):source.index(")\n                SELECT", source.index("latest_outreach AS"))]
        self.assertIn("'candidate_contact_update'", latest_outreach)

    def test_review_modal_keeps_legacy_text_inference(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("已申请加微信，等待通过", source)
        self.assertIn("contact_status:contactStatus", source)
        self.assertIn("contactStatus='wechat_requested'", source)

    def test_db_doctor_preserves_structured_contact_stage(self) -> None:
        source = DOCTOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"wechat_requested": ("X3 已申请加微信/待通过", "已联系/等待微信通过")', source)
        self.assertIn("ce.event_type = 'candidate_contact_update'", source)
        self.assertIn("contact_status in CONTACT_PROGRESS", source)


if __name__ == "__main__":
    unittest.main()
