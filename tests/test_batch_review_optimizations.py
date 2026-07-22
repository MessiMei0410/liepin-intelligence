from __future__ import annotations

import unittest
import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest import mock


BUILDER_PATH = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py")
SERVER_PATH = Path("/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts/liepin_workbench_server.py")
SYNC_PATH = Path("/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py")


def load_sync_module():
    sys.path.insert(0, str(SYNC_PATH.parent))
    spec = importlib.util.spec_from_file_location("talent_system_sync_batch_undo_test", SYNC_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = load_sync_module()


class BatchReviewOptimizationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = BUILDER_PATH.read_text(encoding="utf-8")
        cls.server = SERVER_PATH.read_text(encoding="utf-8")
        cls.sync = SYNC_PATH.read_text(encoding="utf-8")

    def test_review_completion_uses_local_render_for_every_branch(self) -> None:
        continuation = self.builder.split(
            "function continueCandidateBatchAfterReview(candidate, result)", 1
        )[1].split("function openReviewAction", 1)[0]

        self.assertNotIn("location.reload()", continuation)
        self.assertIn("renderOverview();", continuation)
        self.assertIn("renderCandidateBatchCompletion", continuation)

    def test_batch_progress_is_stable_and_counts_completed_candidates(self) -> None:
        self.assertIn("let candidateQueueInitialCount = 0;", self.builder)
        self.assertIn("data-candidate-batch-progress", self.builder)
        self.assertIn("candidateQueueCompletedIds.size", self.builder)
        self.assertIn("共 ${{candidateQueueInitialCount}}", self.builder)

    def test_review_can_be_undone_with_a_compensating_backend_action(self) -> None:
        self.assertIn("let candidateQueueUndo = null;", self.builder)
        self.assertIn("function undoCandidateBatchReview()", self.builder)
        self.assertIn("kind:'resume_review_undo'", self.builder)
        self.assertIn("resume_review_undo", self.server)
        self.assertIn('kind == "resume_review_undo"', self.sync)
        self.assertIn('event_status="undone"', self.sync)

    def test_stop_review_has_quick_reason_options(self) -> None:
        for reason in ("太资深", "薪资太贵", "方向不符", "经验不符", "地点不符", "意愿低", "重复人选", "其他"):
            self.assertIn(reason, self.builder)
        self.assertIn("data-stop-reason", self.builder)
        self.assertIn("stop_reason_code", self.builder)

    def test_candidate_detail_scroll_position_is_preserved_per_candidate(self) -> None:
        self.assertIn("const candidateDetailScrollPositions = new Map();", self.builder)
        self.assertIn("function saveCandidateDetailScrollPosition()", self.builder)
        self.assertIn("function restoreCandidateDetailScrollPosition", self.builder)
        self.assertIn("candidateDetailScrollPositions.set", self.builder)

    def test_active_batch_polls_minimal_flow_state_endpoint(self) -> None:
        self.assertIn("function syncCandidateBatchFlowStates()", self.builder)
        self.assertIn("'/api/talent-flow-state'", self.builder)
        self.assertIn("candidateBatchSyncTimer", self.builder)
        self.assertIn('parsed.path == "/api/talent-flow-state"', self.server)
        self.assertIn("def talent_flow_state", self.server)

    def test_batch_keyboard_controls_ignore_editable_fields(self) -> None:
        self.assertIn("function handleCandidateBatchKeyboard(event)", self.builder)
        self.assertIn("ArrowDown", self.builder)
        self.assertIn("ArrowUp", self.builder)
        self.assertIn("event.key.toLowerCase() === 'p'", self.builder)
        self.assertIn("event.key.toLowerCase() === 'x'", self.builder)
        self.assertIn("isContentEditable", self.builder)

    def test_active_batch_survives_external_live_refresh(self) -> None:
        self.assertIn("const CANDIDATE_BATCH_SESSION_KEY", self.builder)
        self.assertIn("let candidateQueueInitialIds = new Set();", self.builder)
        self.assertIn("function saveCandidateBatchSession()", self.builder)
        self.assertIn("function restoreCandidateBatchSession()", self.builder)
        self.assertIn("sessionStorage.setItem(CANDIDATE_BATCH_SESSION_KEY", self.builder)
        self.assertIn("initialJobCandidateIds", self.builder)
        self.assertIn("selectedJobCandidateId", self.builder)
        self.assertIn("saveCandidateBatchSession();\n  startCandidateBatchSync();", self.builder)
        self.assertIn("if (!restoreCandidateBatchSession()) restoreFlowQueueContinuation();", self.builder)
        self.assertIn("candidateQueueInitialIds.has(String(c.jobCandidateId))", self.builder)

    def test_review_undo_restores_database_state_and_writes_audit_events(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, clean_stage TEXT, flow_bucket TEXT,
                clean_reason TEXT, raw_status TEXT, raw_stage TEXT, updated_at TEXT
            );
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY, status TEXT, notes TEXT, updated_at TEXT
            );
            CREATE TABLE candidate_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER,
                person_id INTEGER, job_id INTEGER, event_type TEXT NOT NULL,
                event_status TEXT, event_time TEXT, summary TEXT, raw_json TEXT,
                source_table TEXT, source_id TEXT
            );
            INSERT INTO job_candidates VALUES
                (42, 'H5 最近寻访/初筛不通过', '最近寻访', '停止', 'review_stop',
                 'H5 最近寻访/初筛不通过', '2026-07-14 10:00:00');
            INSERT INTO candidates VALUES (7, 'screen_rejected', '', '2026-07-14 10:00:00');
            """
        )
        row = {
            "job_candidate_id": 42,
            "person_id": 3,
            "job_id": 5,
            "source_candidate_id": "7",
            "display_name": "测试人选",
            "current_company": "测试公司",
            "current_title": "机械工程师",
            "client": "长越科技",
            "job": "机械工程师",
        }
        action = {
            "kind": "resume_review_undo",
            "action_id": "undo-42-1",
            "summary": "撤销上一次复核",
            "previous_clean_stage": "S1 新增寻访/待复核",
            "previous_flow_bucket": "待复核",
            "previous_raw_status": "search_shortlisted",
            "previous_raw_stage": "S1 新增寻访/待复核",
            "previous_candidate_status": "new",
        }
        with mock.patch.object(sync, "existing_event_count", return_value=0), mock.patch.object(
            sync, "resolve_action_candidate", return_value=("unique", row, [])
        ):
            result = sync.process_action(
                conn,
                action,
                batch_source="test",
                default_event_time="2026-07-14 10:05:00",
                default_source_thread_id="test-thread",
                index=0,
                dry_run=False,
            )
        restored = conn.execute(
            "SELECT clean_stage, flow_bucket, raw_status, raw_stage FROM job_candidates WHERE id = 42"
        ).fetchone()
        self.assertEqual(tuple(restored), ("S1 新增寻访/待复核", "待复核", "search_shortlisted", "S1 新增寻访/待复核"))
        self.assertEqual(conn.execute("SELECT status FROM candidates WHERE id = 7").fetchone()[0], "new")
        events = conn.execute(
            "SELECT event_type, event_status FROM candidate_events ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(event) for event in events], [
            ("resume_review_completed", "undone"),
            ("candidate_stage_update", "S1 新增寻访/待复核"),
        ])
        self.assertEqual(result["status"], "written")
        conn.close()


if __name__ == "__main__":
    unittest.main()
