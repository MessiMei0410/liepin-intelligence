from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from _local import env_path, skip_unless_local


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "scripts" / "liepin_workbench_server.py"
REPLY_PATH = ROOT / "scripts" / "record_candidate_reply.py"
CONTENT_PATH = ROOT / "liepin-reply-assistant-extension" / "content.js"
EVIDENCE_PATH = ROOT / "liepin-reply-assistant-extension" / "message-evidence.js"
MANIFEST_PATH = ROOT / "liepin-reply-assistant-extension" / "manifest.json"
BUILDER_PATH = env_path("ASA_BUILDER_PATH", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py"))
SYNC_PATH = env_path("ASA_SYNC_SCRIPT", Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py"))
BROWSER_TEST_PATH = ROOT / "tests" / "reply_message_evidence_browser.js"


def load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


server = load_module("liepin_workbench_server_candidate_message_workflow_test", SERVER_PATH)
reply = load_module("record_candidate_reply_workflow_test", REPLY_PATH)


def create_workflow_db(path: Path, *, include_events: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT);
        CREATE TABLE people (
            id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT, current_title TEXT
        );
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, name TEXT, company TEXT, title TEXT, client TEXT,
            position TEXT, status TEXT, notes TEXT DEFAULT '', updated_at TEXT
        );
        CREATE TABLE job_candidates (
            id INTEGER PRIMARY KEY, person_id INTEGER, job_id INTEGER,
            source_candidate_id TEXT, clean_stage TEXT, flow_bucket TEXT,
            clean_reason TEXT, raw_status TEXT, raw_stage TEXT, updated_at TEXT
        );
        {'''CREATE TABLE candidate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER,
            person_id INTEGER, job_id INTEGER, event_type TEXT NOT NULL,
            event_status TEXT, event_time TEXT, summary TEXT, raw_json TEXT DEFAULT '{}',
            source_table TEXT, source_id TEXT
        );''' if include_events else ''}
        INSERT INTO clients VALUES (1, '长越科技');
        INSERT INTO jobs VALUES (137, 1, '机械高级工程师');
        INSERT INTO people VALUES (516, '张航', 'ASM中国集团公司', '高级机械设计工程师');
        INSERT INTO candidates VALUES (
            1140, '张航', 'ASM中国集团公司', '高级机械设计工程师',
            '长越科技', '机械高级工程师', 'contacted', '', '2026-07-14 11:53:15'
        );
        INSERT INTO job_candidates VALUES (
            522, 516, 137, '1140', '已触达', '猎聘触达', '已核验触达',
            'job_chat_verified', '已触达', '2026-07-14 11:53:15'
        );
        """
    )
    conn.commit()
    conn.close()


def received_payload(**overrides):
    payload = {
        "kind": "candidate_message",
        "job_candidate_id": 522,
        "candidate_id": 1140,
        "candidate_name": "张航",
        "candidate_company": "ASM中国集团公司",
        "candidate_title": "高级机械设计工程师",
        "client": "长越科技",
        "position": "机械高级工程师",
        "direction": "received",
        "raw_text": "可以了解一下",
        "message_preview": "可以了解一下",
        "message_evidence": "explicit_inbound_dom",
        "conversation_id": "liepin-conversation-zhanghang-522",
        "conversation_identity_confidence": "dom_id",
        "message_id": "msg-in-9001",
        "message_time": "2026-07-14T14:10:00+08:00",
        "outbound_draft_preview": "张工，我这边是长越科技在招机械高级工程师。",
        "summary": "候选人回复：可以了解一下",
        "stage_after": "S3 已回复",
        "flow_bucket": "正式流程",
    }
    payload.update(overrides)
    return payload


class CandidateMessageWorkflowTest(unittest.TestCase):
    def test_sent_message_requires_explicit_outbound_dom_evidence(self) -> None:
        payload = received_payload(
            direction="sent",
            raw_text="张工您好",
            message_preview="张工您好",
            message_evidence="manual_confirmation",
            stage_after="",
        )
        with self.assertRaisesRegex(ValueError, "明确的我方出站消息证据"):
            server.build_talent_action(payload)

    def test_origin_policy_allows_only_local_and_candidate_assistant_pages(self) -> None:
        self.assertTrue(hasattr(server, "candidate_assistant_origin_decision"), "缺少来源权限判定")
        decide = server.candidate_assistant_origin_decision
        self.assertEqual(decide("null"), "allow")
        self.assertEqual(decide("chrome-extension://aihpahceageafhjhedhmeikhcfbfoffn"), "allow")
        self.assertEqual(decide("chrome-extension://cecifklpjckkbclegnmapegnedelapjh"), "allow")
        self.assertEqual(decide("https://h.liepin.com"), "deny")
        self.assertEqual(decide("https://headhunt.x-saas.com.cn"), "deny")
        self.assertEqual(decide("https://evil.example"), "deny")

    def test_xsaas_intake_without_project_is_pending_review_and_never_writes(self) -> None:
        payload = {
            "kind": "xsaas_intake",
            "candidate": "缺项目测试人选",
            "company": "测试公司",
            "title": "测试职位",
            "xsaas_id": "missing-project-test",
            "client": "",
            "job": "",
        }
        dry_run = server.apply_talent_action_batch({**payload, "write": False})
        write = server.apply_talent_action_batch({**payload, "write": True})
        self.assertTrue(dry_run["ok"])
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["sync"]["result"]["summary"]["pending_review"], 1)
        self.assertEqual(dry_run["sync"]["result"]["summary"]["would_write"], 0)
        self.assertEqual(dry_run["batch_path"], "")
        self.assertFalse(write["ok"])
        self.assertEqual(write["returncode"], 2)
        self.assertEqual(write["batch_path"], "")

    def test_preflight_token_commits_reply_event_task_and_stage_once(self) -> None:
        self.assertTrue(hasattr(server, "candidate_message_preflight"), "缺少候选人消息预检")
        self.assertTrue(hasattr(server, "candidate_message_commit"), "缺少候选人消息原子提交")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "workflow.db"
            create_workflow_db(db)
            state = server.WorkbenchState(db, Path(tmp), "127.0.0.1", 0)
            preflight = server.candidate_message_preflight(state, received_payload())
            self.assertTrue(preflight["ok"])
            self.assertEqual(preflight["decision"], "allow")
            self.assertEqual(preflight["classification"]["intent"], "interested")
            self.assertTrue(preflight["confirmation_token"])

            committed = server.candidate_message_commit(
                state,
                {**received_payload(), "confirmation_token": preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertTrue(committed["ok"])
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidate_replies").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM followup_tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM candidate_events WHERE event_type='candidate_message_received'").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT clean_stage FROM job_candidates WHERE id=522").fetchone()[0], "S3 已回复")
            conn.close()

            reused = server.candidate_message_commit(
                state,
                {**received_payload(), "confirmation_token": preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertFalse(reused["ok"])
            self.assertEqual(reused["decision"], "deny")

    def test_atomic_commit_rolls_back_reply_when_event_write_fails(self) -> None:
        self.assertTrue(hasattr(server, "candidate_message_preflight"), "缺少候选人消息预检")
        self.assertTrue(hasattr(server, "candidate_message_commit"), "缺少候选人消息原子提交")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broken.db"
            create_workflow_db(db, include_events=False)
            state = server.WorkbenchState(db, Path(tmp), "127.0.0.1", 0)
            preflight = server.candidate_message_preflight(state, received_payload())
            result = server.candidate_message_commit(
                state,
                {**received_payload(), "confirmation_token": preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertFalse(result["ok"])
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidate_replies").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM followup_tasks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM candidates WHERE id=1140").fetchone()[0], "contacted")
            conn.close()

    def test_effective_event_view_excludes_corrected_and_invalid_events(self) -> None:
        self.assertTrue(hasattr(server, "ensure_effective_candidate_events_schema"), "缺少统一有效事件视图")
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE candidate_events (
                id INTEGER PRIMARY KEY, job_candidate_id INTEGER, person_id INTEGER,
                job_id INTEGER, event_type TEXT, event_status TEXT, event_time TEXT,
                summary TEXT, raw_json TEXT, source_table TEXT, source_id TEXT
            );
            INSERT INTO candidate_events VALUES
                (1,522,516,137,'candidate_message_received','done','2026-07-14 12:00:00','错误回复','{}','test','1'),
                (2,522,516,137,'liepin_outreach','job_chat_verified','2026-07-14 11:00:00','已触达','{}','test','2'),
                (3,522,516,137,'candidate_message_received','undone','2026-07-14 12:10:00','已作废','{}','test','3');
            """
        )
        server.ensure_effective_candidate_events_schema(conn)
        conn.execute(
            "INSERT INTO candidate_event_corrections(original_event_id, reason) VALUES (1, '方向误判')"
        )
        rows = conn.execute("SELECT id FROM v_effective_candidate_events ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], [2])
        conn.close()

    def test_state_evidence_and_correction_restore_all_linked_facts(self) -> None:
        self.assertTrue(hasattr(server, "candidate_state_evidence"), "缺少状态依据接口")
        self.assertTrue(hasattr(server, "candidate_state_correction_preflight"), "缺少状态纠正预检")
        self.assertTrue(hasattr(server, "candidate_state_correction_commit"), "缺少状态纠正原子提交")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "correction.db"
            create_workflow_db(db)
            state = server.WorkbenchState(db, Path(tmp), "127.0.0.1", 0)
            message_preflight = server.candidate_message_preflight(state, received_payload())
            message_commit = server.candidate_message_commit(
                state,
                {**received_payload(), "confirmation_token": message_preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertTrue(message_commit["ok"])

            evidence = server.candidate_state_evidence(state, 522)
            self.assertEqual(evidence["current_state"]["clean_stage"], "S3 已回复")
            self.assertEqual(evidence["message_basis"]["raw_text"], "可以了解一下")
            self.assertEqual(evidence["message_basis"]["message_evidence"], "explicit_inbound_dom")
            self.assertEqual(evidence["message_basis"]["intent"], "interested")

            correction_payload = {
                "job_candidate_id": 522,
                "target_state": "contacted_waiting_reply",
                "reason": "人工核对后确认该消息不是候选人回复",
            }
            correction_preflight = server.candidate_state_correction_preflight(state, correction_payload)
            self.assertTrue(correction_preflight["ok"])
            self.assertGreaterEqual(len(correction_preflight["invalidate_event_ids"]), 1)
            corrected = server.candidate_state_correction_commit(
                state,
                {**correction_payload, "confirmation_token": correction_preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertTrue(corrected["ok"])

            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT clean_stage FROM job_candidates WHERE id=522").fetchone()[0], "已触达")
            self.assertEqual(conn.execute("SELECT status FROM candidates WHERE id=1140").fetchone()[0], "contacted")
            self.assertEqual(conn.execute("SELECT correction_status FROM candidate_replies").fetchone()[0], "undone")
            self.assertEqual(conn.execute("SELECT status FROM followup_tasks").fetchone()[0], "closed")
            effective_received = conn.execute(
                "SELECT COUNT(*) FROM v_effective_candidate_events WHERE event_type='candidate_message_received'"
            ).fetchone()[0]
            self.assertEqual(effective_received, 0)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM candidate_event_corrections").fetchone()[0], 1)
            conn.close()

            reused = server.candidate_state_correction_commit(
                state,
                {**correction_payload, "confirmation_token": correction_preflight["confirmation_token"]},
                refresh=False,
            )
            self.assertFalse(reused["ok"])

    def test_reply_insert_can_participate_in_an_outer_transaction(self) -> None:
        source = REPLY_PATH.read_text(encoding="utf-8")
        self.assertIn("commit: bool = True", source)
        self.assertIn("if commit:", source)

    def test_runtime_schema_migration_is_safe_to_run_at_server_start(self) -> None:
        self.assertTrue(hasattr(server, "ensure_workbench_runtime_schema"), "缺少服务启动 schema 迁移")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runtime.db"
            create_workflow_db(db)
            server.ensure_workbench_runtime_schema(db)
            server.ensure_workbench_runtime_schema(db)
            conn = sqlite3.connect(db)
            reply_columns = {row[1] for row in conn.execute("PRAGMA table_info(candidate_replies)")}
            self.assertTrue({"message_id", "message_evidence", "correction_status"}.issubset(reply_columns))
            self.assertIsNotNone(
                conn.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='v_effective_candidate_events'").fetchone()
            )
            conn.close()

    def test_extension_uses_evidence_module_and_atomic_message_endpoints(self) -> None:
        self.assertTrue(EVIDENCE_PATH.exists(), "缺少可独立测试的消息证据模块")
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        scripts = manifest["content_scripts"][0]["js"]
        self.assertIn("message-evidence.js", scripts)
        self.assertGreaterEqual(
            tuple(int(part) for part in manifest["version"].split(".")),
            (0, 3, 1),
        )
        source = CONTENT_PATH.read_text(encoding="utf-8")
        self.assertIn("/api/candidate-message-preflight", source)
        self.assertIn("/api/candidate-message-commit", source)
        self.assertIn("conversationSnapshotMatches", source)
        self.assertIn("body?.reason", source)
        self.assertNotIn("await postCandidateReply(payload);", source)

    @skip_unless_local(env_path("ASA_NODE_MODULES", Path("/Users/messi/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")), "codex-runtime node_modules")
    def test_browser_fixture_covers_direction_and_conversation_switch(self) -> None:
        self.assertTrue(BROWSER_TEST_PATH.exists(), "缺少消息方向浏览器测试")
        proc = subprocess.run(
            ["node", str(BROWSER_TEST_PATH)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "NODE_PATH": env_path("ASA_NODE_MODULES", Path("/Users/messi/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules")).__str__(),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["received"], "候选人回复：可以了解")
        self.assertEqual(report["sent"], "猎头发出：长越机械岗位")
        self.assertEqual(report["conversationId"], "chat-zhanghang")
        self.assertFalse(report["conversationSwitchMatches"])

    @skip_unless_local(BUILDER_PATH, "build_talent_workbench.py 脚本")
    def test_builder_exposes_state_evidence_correction_reply_queues_and_sla(self) -> None:
        source = BUILDER_PATH.read_text(encoding="utf-8")
        for marker in (
            "v_effective_candidate_events",
            "状态依据",
            "纠正状态",
            "/api/candidate-state-correction-preflight",
            "/api/candidate-state-correction-commit",
            "正向回复待推进",
            "回复待补岗位信息",
            "回复待确认薪资",
            "回复待确认地点",
            "回复待确认停止",
            "触达2天待跟进",
            "触达5天未回",
        ):
            self.assertIn(marker, source)

    @skip_unless_local(SYNC_PATH, "talent_system_sync.py 脚本")
    def test_sync_and_builder_share_effective_event_view(self) -> None:
        sync_source = SYNC_PATH.read_text(encoding="utf-8")
        builder_source = BUILDER_PATH.read_text(encoding="utf-8")
        self.assertIn("v_effective_candidate_events", sync_source)
        self.assertIn("v_effective_candidate_events", builder_source)


if __name__ == "__main__":
    unittest.main()
