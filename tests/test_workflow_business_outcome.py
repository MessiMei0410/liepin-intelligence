from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from a_system_agent.workflow import classify_business_outcome  # noqa: E402
import backfill_business_outcome  # noqa: E402


LEGACY_GOALS_DDL = """
CREATE TABLE agent_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    title TEXT NOT NULL,
    context_type TEXT NOT NULL DEFAULT 'global',
    context_id INTEGER,
    context_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'draft',
    progress REAL NOT NULL DEFAULT 0,
    result_summary TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT
);
"""

LEGACY_WORKFLOWS_DDL = """
CREATE TABLE agent_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    current_stage TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    plan_json TEXT NOT NULL DEFAULT '{}',
    active_step_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(goal_id) REFERENCES agent_goals(goal_id)
);
"""


class BusinessOutcomeTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))

    def tearDown(self) -> None:
        self.service.close()
        super().tearDown()

    def wait_for(self, workflow_id: str, statuses: set[str], timeout: float = 5) -> dict:
        deadline = time.time() + timeout
        state = self.service.get_workflow(workflow_id)
        while time.time() < deadline and state["workflow"]["status"] not in statuses:
            time.sleep(0.03)
            state = self.service.get_workflow(workflow_id)
        return state

    def drive_sourcing_to_terminal(self, objective: str) -> tuple[str, dict]:
        result = self.service.create_goal(objective, {"type": "job", "id": 10})
        workflow_id = result["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        waiting = self.wait_for(workflow_id, {"waiting_approval", "failed"})
        approval = next(item for item in waiting["approvals"] if item["status"] == "pending")
        self.service.decide_workflow_approval(approval["approval_id"], "approve")
        external = self.wait_for(workflow_id, {"waiting_external", "failed"})
        external_step = next(step for step in external["steps"] if step["status"] == "waiting_external")
        self.service.complete_external_workflow_step(
            external_step["id"],
            {
                "verified": True,
                "run_id": "source-test",
                "channel_runs": [{"channel": "liepin", "status": "blocked"}],
                "intake": {"accepted_count": 0},
                "audit": {"ok": True},
            },
        )
        return workflow_id, self.wait_for(workflow_id, {"blocked", "completed", "failed"})

    def db_outcomes(self, workflow_id: str) -> tuple:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT w.business_outcome,g.business_outcome
                FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                WHERE w.workflow_id=?
                """,
                (workflow_id,),
            ).fetchone()
            return (row[0], row[1]) if row else (None, None)
        finally:
            conn.close()

    def classify(self, workflow_id: str):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return classify_business_outcome(conn, workflow_id)
        finally:
            conn.close()

    def test_completed_pool_insufficient_terminal_writes_outcome(self) -> None:
        workflow_id, state = self.drive_sourcing_to_terminal("给长越科技机械高级工程师补充10位合适人选")
        assert state["workflow"]["status"] == "blocked"
        assert state["business_outcome"] == "completed_pool_insufficient"
        assert state["workflow"]["business_outcome"] == "completed_pool_insufficient"
        assert state["goal"]["business_outcome"] == "completed_pool_insufficient"
        assert self.db_outcomes(workflow_id) == ("completed_pool_insufficient", "completed_pool_insufficient")
        assert self.classify(workflow_id) == "completed_pool_insufficient"
        summary = self.service.get_workflow_summary(workflow_id)
        assert summary["business_outcome"] == "completed_pool_insufficient"

    def test_completed_target_met_terminal_writes_outcome(self) -> None:
        assessed = self.service.submit_assessment(30, wait=True)
        assert assessed["status"] == "completed"
        assert assessed["assessment"]["fit_score"] >= 75
        workflow_id, state = self.drive_sourcing_to_terminal("给长越科技机械高级工程师补充1位合适人选")
        assert state["workflow"]["status"] == "completed"
        assert state["business_outcome"] == "completed_target_met"
        assert state["workflow"]["business_outcome"] == "completed_target_met"
        assert state["goal"]["business_outcome"] == "completed_target_met"
        assert self.db_outcomes(workflow_id) == ("completed_target_met", "completed_target_met")
        assert self.classify(workflow_id) == "completed_target_met"
        summary = self.service.get_workflow_summary(workflow_id)
        assert summary["business_outcome"] == "completed_target_met"

    def test_completed_needs_review_terminal_writes_outcome(self) -> None:
        self.service.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment("unknown"), chat_text="测试回答"))
        assessed = self.service.submit_assessment(30, wait=True)
        assert assessed["status"] == "completed"
        assert assessed["assessment"]["recommendation"] == "verify_first"
        assert assessed["assessment"]["fit_score"] < 75
        workflow_id, state = self.drive_sourcing_to_terminal("给长越科技机械高级工程师补充1位合适人选")
        assert state["workflow"]["status"] == "blocked"
        assert "待核验" in state["goal"]["result_summary"]
        assert state["business_outcome"] == "completed_needs_review"
        assert state["workflow"]["business_outcome"] == "completed_needs_review"
        assert state["goal"]["business_outcome"] == "completed_needs_review"
        assert self.db_outcomes(workflow_id) == ("completed_needs_review", "completed_needs_review")
        assert self.classify(workflow_id) == "completed_needs_review"

    def test_failed_technical_classification_and_backfill(self) -> None:
        result = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        workflow_id = result["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        waiting = self.wait_for(workflow_id, {"waiting_approval", "failed"})
        approval = next(item for item in waiting["approvals"] if item["status"] == "pending")
        self.service.decide_workflow_approval(approval["approval_id"], "approve")
        external = self.wait_for(workflow_id, {"waiting_external", "failed"})
        external_step = next(step for step in external["steps"] if step["status"] == "waiting_external")
        self.service.workflow_engine.fail_external_step(external_step["id"], "渠道执行失败：测试注入")
        state = self.service.get_workflow(workflow_id)
        assert state["workflow"]["status"] == "failed"
        # 引擎只在 _finish() 终局写 business_outcome；failed 路径由分类函数/回填覆盖
        assert self.db_outcomes(workflow_id) == (None, None)
        assert self.classify(workflow_id) == "failed_technical"

        with mock.patch.object(sys, "argv", ["backfill", "--db", str(self.db_path)]):
            assert backfill_business_outcome.main() == 0
        assert self.db_outcomes(workflow_id) == (None, None), "dry-run 不应写库"

        with mock.patch.object(sys, "argv", ["backfill", "--db", str(self.db_path), "--apply"]):
            assert backfill_business_outcome.main() == 0
        assert self.db_outcomes(workflow_id) == ("failed_technical", "failed_technical")
        refreshed = self.service.get_workflow(workflow_id)
        assert refreshed["business_outcome"] == "failed_technical"
        assert refreshed["workflow"]["business_outcome"] == "failed_technical"

    def test_backfill_restores_legacy_blocked_rows(self) -> None:
        workflow_id, state = self.drive_sourcing_to_terminal("给长越科技机械高级工程师补充10位合适人选")
        assert state["workflow"]["status"] == "blocked"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE agent_workflows SET business_outcome=NULL WHERE workflow_id=?", (workflow_id,))
            conn.execute("UPDATE agent_goals SET business_outcome=NULL WHERE goal_id=(SELECT goal_id FROM agent_workflows WHERE workflow_id=?)", (workflow_id,))
            conn.commit()
        finally:
            conn.close()
        assert self.db_outcomes(workflow_id) == (None, None)
        with mock.patch.object(sys, "argv", ["backfill", "--db", str(self.db_path), "--apply"]):
            assert backfill_business_outcome.main() == 0
        assert self.db_outcomes(workflow_id) == ("completed_pool_insufficient", "completed_pool_insufficient")

    def test_non_sourcing_completed_outcome_is_null(self) -> None:
        result = self.service.create_goal("给张航发送猎聘触达消息", {"type": "candidate", "id": 30})
        workflow_id = result["workflow"]["workflow_id"]
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE agent_workflow_steps SET status='completed' WHERE workflow_id=?", (workflow_id,))
            conn.commit()
        finally:
            conn.close()
        self.service.workflow_engine.run_workflow(workflow_id)
        state = self.service.get_workflow(workflow_id)
        assert state["workflow"]["status"] == "completed"
        assert "business_outcome" in state and state["business_outcome"] is None
        assert "business_outcome" in state["workflow"] and state["workflow"]["business_outcome"] is None
        assert "business_outcome" in state["goal"] and state["goal"]["business_outcome"] is None
        assert self.db_outcomes(workflow_id) == (None, None)
        assert self.classify(workflow_id) is None
        summary = self.service.get_workflow_summary(workflow_id)
        assert "business_outcome" in summary and summary["business_outcome"] is None

    def test_step_blocked_sourcing_workflow_outcome_is_none(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                INSERT INTO agent_goals (goal_id,objective,title,context_json,status)
                VALUES ('goal_step_blocked','给长越科技机械高级工程师补充10位合适人选','寻访','{"type":"job","id":10}','blocked')
                """
            )
            conn.execute(
                "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf_step_blocked','goal_step_blocked','blocked')"
            )
            conn.execute(
                """
                INSERT INTO agent_workflow_steps
                (workflow_id,step_key,sequence,capability_id,business_label,business_stage,risk_level,status)
                VALUES ('wf_step_blocked','step_1',1,'job_publish_prepare','岗位发布准备','job_publish','R1','blocked')
                """
            )
            conn.commit()
            assert classify_business_outcome(conn, "wf_step_blocked") is None
        finally:
            conn.close()

    def test_api_exposes_business_outcome_key_before_terminal(self) -> None:
        result = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        workflow_id = result["workflow"]["workflow_id"]
        state = self.service.get_workflow(workflow_id)
        assert "business_outcome" in state and state["business_outcome"] is None
        assert "business_outcome" in state["workflow"] and state["workflow"]["business_outcome"] is None
        assert "business_outcome" in state["goal"] and state["goal"]["business_outcome"] is None
        summary = self.service.get_workflow_summary(workflow_id)
        assert "business_outcome" in summary and summary["business_outcome"] is None


class BusinessOutcomeMigrationTest(unittest.TestCase):
    def test_ensure_schema_adds_business_outcome_to_legacy_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(LEGACY_GOALS_DDL + LEGACY_WORKFLOWS_DDL)
                conn.execute(
                    "INSERT INTO agent_goals (goal_id,objective,title,status) VALUES ('g1','整理资料','旧目标','completed')"
                )
                conn.execute(
                    "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('w1','g1','completed')"
                )
                conn.commit()
                ensure_schema(conn)
                ensure_schema(conn)  # 幂等：第二次执行不报错
                conn.commit()
                goal_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_goals)")}
                workflow_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_workflows)")}
                assert "business_outcome" in goal_columns
                assert "business_outcome" in workflow_columns
                row = conn.execute(
                    """
                    SELECT w.business_outcome,g.business_outcome
                    FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
                    WHERE w.workflow_id='w1'
                    """
                ).fetchone()
                assert row[0] is None and row[1] is None
            finally:
                conn.close()

    def test_ensure_schema_on_fresh_db_contains_business_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "fresh.db"
            conn = sqlite3.connect(db_path)
            try:
                ensure_schema(conn)
                ensure_schema(conn)
                conn.commit()
                goal_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_goals)")}
                workflow_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_workflows)")}
                assert "business_outcome" in goal_columns
                assert "business_outcome" in workflow_columns
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
