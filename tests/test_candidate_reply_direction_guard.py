from __future__ import annotations

import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path
from _local import env_path, require_local, skip_unless_local
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "scripts" / "liepin_workbench_server.py"
CONTENT_PATH = ROOT / "liepin-reply-assistant-extension" / "content.js"
SYNC_PATH = env_path("ASA_SYNC_SCRIPT", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py"))
BUILDER_PATH = env_path("ASA_BUILDER_PATH", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py"))


def load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


server = load_module("liepin_workbench_server_reply_direction_test", SERVER_PATH)
require_local(SYNC_PATH, "talent_system_sync.py 脚本")
sync = load_module("talent_system_sync_reply_direction_test", SYNC_PATH)


class CandidateReplyDirectionGuardTest(unittest.TestCase):
    def received_payload(self, **overrides):
        payload = {
            "kind": "candidate_message",
            "job_candidate_id": 522,
            "direction": "received",
            "summary": "候选人回复：可以了解",
            "message_preview": "可以了解",
            "message_evidence": "explicit_inbound_dom",
            "outbound_draft_preview": "张工，我这边是长越科技在招机械高级工程师。",
            "conversation_id": "liepin-conversation-522",
            "conversation_identity_confidence": "dom_id",
            "message_id": "received-522-1",
            "message_time": "2026-07-14T14:00:00+08:00",
            "stage_after": "S3 已回复",
        }
        payload.update(overrides)
        return payload

    def test_server_rejects_received_message_without_inbound_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "明确的候选人入站消息证据"):
            server.build_talent_action(self.received_payload(message_evidence=""))

    def test_server_rejects_received_message_equal_to_outbound_draft(self) -> None:
        text = "张工，我这边是长越科技在招机械高级工程师。"
        with self.assertRaisesRegex(ValueError, "与我方草稿相同"):
            server.build_talent_action(
                self.received_payload(message_preview=text, outbound_draft_preview=text)
            )

    def test_server_preserves_received_message_evidence(self) -> None:
        action = server.build_talent_action(self.received_payload())
        self.assertEqual(action["raw"]["message_evidence"], "explicit_inbound_dom")
        self.assertIn("outbound_draft_preview", action["raw"])

    def test_sync_rejects_unproven_received_message_before_writing(self) -> None:
        conn = sqlite3.connect(":memory:")
        row = {
            "job_candidate_id": 522,
            "person_id": 1,
            "job_id": 1,
            "source_candidate_id": "1",
            "display_name": "张航",
            "current_company": "测试公司",
            "current_title": "机械工程师",
            "client": "长越科技",
            "job": "机械高级工程师",
        }
        action = {
            "kind": "candidate_message",
            "action_id": "received-without-proof",
            "direction": "received",
            "summary": "候选人已回复",
            "stage_after": "S3 已回复",
            "raw": {"message_preview": "张工，我这边是长越科技在招机械高级工程师。"},
        }
        with mock.patch.object(sync, "existing_event_count", return_value=0), mock.patch.object(
            sync, "resolve_action_candidate", return_value=("unique", row, [])
        ), mock.patch.object(sync, "insert_linked_event") as insert_event:
            result = sync.process_action(
                conn,
                action,
                batch_source="test",
                default_event_time="2026-07-14 14:00:00",
                default_source_thread_id="test-thread",
                index=0,
                dry_run=True,
            )
        self.assertEqual(result["reason"], "received_message_requires_inbound_evidence")
        insert_event.assert_not_called()
        conn.close()

    def test_extension_uses_explicit_inbound_dom_and_manual_fallback(self) -> None:
        source = CONTENT_PATH.read_text(encoding="utf-8")
        page_context = source.split("function readPageContext()", 1)[1].split(
            "function readResumeContext", 1
        )[0]
        latest_reply = source.split("function latestCandidateReplyText", 1)[1].split(
            "function candidateReplyPayload", 1
        )[0]
        self.assertIn("latestReceivedMessage", page_context)
        self.assertIn("source.latestReceivedMessage", latest_reply)
        self.assertNotIn("source.latestMessage || source.contact?.preview", latest_reply)
        self.assertIn("manual_transcription", source)
        self.assertIn("explicit_inbound_dom", source)

    @skip_unless_local(BUILDER_PATH, "build_talent_workbench.py 脚本")
    def test_undone_reply_events_are_excluded_from_current_state_queries(self) -> None:
        builder = BUILDER_PATH.read_text(encoding="utf-8")
        server_source = SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE VIEW IF NOT EXISTS v_effective_candidate_events", builder)
        self.assertIn("FROM v_effective_candidate_events ce", builder)
        self.assertIn("CREATE VIEW IF NOT EXISTS v_effective_candidate_events", server_source)


if __name__ == "__main__":
    unittest.main()
