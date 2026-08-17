from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

from a_system_agent import AgentService, FakeLLM
from a_system_agent.relationship_cleanup import (
    RelationshipCleanupScopeBlocked,
    SCOPE_ALL_ACTIVE,
    SCOPE_NONMATCHING,
    apply_relationship_cleanup,
    build_relationship_cleanup_preview,
)
from test_a_system_agent_v1 import AgentDbCase, fake_assessment


def _database() -> Path:
    fd, raw_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    path = Path(raw_path)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT);
        CREATE TABLE people (
            id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
            current_title TEXT, city TEXT, education TEXT, experience TEXT
        );
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, status TEXT, notes TEXT, updated_at TEXT);
        CREATE TABLE job_candidates (
            id INTEGER PRIMARY KEY, person_id INTEGER, job_id INTEGER,
            clean_stage TEXT, flow_bucket TEXT, raw_status TEXT, raw_stage TEXT,
            clean_reason TEXT, stop_reason TEXT, source_candidate_id TEXT, updated_at TEXT
        );
        CREATE TABLE candidate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER,
            person_id INTEGER, job_id INTEGER, event_type TEXT, event_status TEXT,
            event_time TEXT, summary TEXT, raw_json TEXT, source_table TEXT
        );
        CREATE TABLE candidate_profiles (
            candidate_id INTEGER, candidate_name TEXT, candidate_company TEXT,
            position TEXT, education_level TEXT, seniority TEXT, profile_summary TEXT
        );
        INSERT INTO clients VALUES (1, '士兰微');
        INSERT INTO jobs VALUES (142, 1, '电源专家');
        INSERT INTO people VALUES
            (1, '甲', 'A公司', '电源工程师', '杭州', '本科', '8年'),
            (2, '乙', 'B公司', '机械工程师', '杭州', '本科', '8年'),
            (3, '丙', 'C公司', '电源工程师', '杭州', '本科', '6年');
        INSERT INTO candidates VALUES
            (101, 'active', '', NULL), (102, 'active', '', NULL), (103, 'active', '', NULL);
        INSERT INTO job_candidates VALUES
            (11, 1, 142, 'S1 新增寻访/待复核', '待复核', 'new', '', '', '', '101', '2026-08-17'),
            (12, 2, 142, 'S1 新增寻访/待复核', '待复核', 'new', '', '', '', '102', '2026-08-17'),
            (13, 3, 142, 'H5 最近寻访/初筛不通过', '最近寻访', 'screen_rejected', '', '', '', '103', '2026-08-17');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_all_active_preview_excludes_already_stopped_relationships() -> None:
    db = _database()
    try:
        preview = build_relationship_cleanup_preview(str(db), 142, scope_mode=SCOPE_ALL_ACTIVE)
        assert preview["relationship_count"] == 2
        assert [item["jc_id"] for item in preview["items"]] == [11, 12]
        assert preview["candidate_records_preserved"] is True
    finally:
        db.unlink()


def test_relationship_cleanup_preserves_candidate_master_status() -> None:
    db = _database()
    try:
        receipt = apply_relationship_cleanup(str(db), 142, scope_mode=SCOPE_ALL_ACTIVE)
        assert receipt["applied"] == 2
        assert receipt["candidate_records_preserved"] is True

        conn = sqlite3.connect(db)
        relation_rows = conn.execute(
            "SELECT id, clean_stage, raw_status FROM job_candidates ORDER BY id"
        ).fetchall()
        candidate_rows = conn.execute("SELECT id, status FROM candidates ORDER BY id").fetchall()
        event_rows = conn.execute(
            "SELECT job_candidate_id, source_table, raw_json FROM candidate_events ORDER BY id"
        ).fetchall()
        conn.close()

        assert relation_rows[0][1] == "H5 最近寻访/初筛不通过"
        assert relation_rows[1][1] == "H5 最近寻访/初筛不通过"
        assert candidate_rows == [(101, "active"), (102, "active"), (103, "active")]
        assert len(event_rows) == 2
        assert all(row[1] == "candidate_relationship_cleanup" for row in event_rows)
        assert all('"candidate_record_preserved": true' in row[2] for row in event_rows)
    finally:
        db.unlink()


def test_nonmatching_preview_fails_closed_for_unknown_domain() -> None:
    db = _database()
    try:
        conn = sqlite3.connect(db)
        conn.execute("UPDATE jobs SET title='财务经理' WHERE id=142")
        conn.commit()
        conn.close()

        try:
            build_relationship_cleanup_preview(str(db), 142, scope_mode=SCOPE_NONMATCHING)
        except RelationshipCleanupScopeBlocked as exc:
            assert "无法安全自动判断" in str(exc)
            assert "归档全部在推进关系" in str(exc)
        else:
            raise AssertionError("unknown domain must fail closed")
    finally:
        db.unlink()


class TestRelationshipCleanupWorkflow(AgentDbCase):
    def _wait_for(self, service: AgentService, workflow_id: str, statuses: set[str]) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            state = service.get_workflow(workflow_id)
            if state["workflow"]["status"] in statuses:
                return state
            time.sleep(0.02)
        return service.get_workflow(workflow_id)

    def test_r2_approval_previews_scope_and_preserves_candidate_master(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        objective = "清理当前岗位全部候选关系，保留人选主档"
        created = service.create_goal(
            objective,
            {
                "type": "job",
                "id": 10,
                "page": "positions",
                "goal_inputs": {"scope_mode": "all_active"},
                "intent_understanding": {
                    "speech_act": "execute",
                    "action": "candidate_relationship_cleanup",
                    "objective": objective,
                    "target": {"type": "job", "id": 10},
                    "confidence": 1.0,
                },
            },
        )
        workflow_id = created["workflow"]["workflow_id"]
        plan_ref = created["plan_ref"]
        service.start_workflow(
            workflow_id,
            expected_plan_version=int(plan_ref["version"]),
            expected_plan_hash=str(plan_ref["plan_hash"]),
        )
        waiting = self._wait_for(service, workflow_id, {"waiting_approval", "failed"})

        assert waiting["workflow"]["status"] == "waiting_approval"
        approval = next(item for item in waiting["approvals"] if item["status"] == "pending")
        assert approval["risk_level"] == "R2"
        assert approval["preflight"]["batch_size"] == 1
        assert approval["preflight"]["candidate_records_preserved"] is True

        conn = sqlite3.connect(self.db_path)
        assert conn.execute("SELECT clean_stage FROM job_candidates WHERE id=30").fetchone()[0] == "X1 待复核"
        assert conn.execute("SELECT status FROM candidates WHERE id=40").fetchone()[0] == "new"
        conn.execute(
            "INSERT INTO people (id,display_name,current_company,current_title,city,education,experience) "
            "VALUES (21,'审批后新增','新增公司','机械工程师','上海','本科','5年')"
        )
        conn.execute(
            "INSERT INTO candidates (id,name,company,title,status,notes) "
            "VALUES (41,'审批后新增','新增公司','机械工程师','new','')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id,job_id,person_id,raw_status,clean_stage,flow_bucket,source_candidate_id,updated_at) "
            "VALUES (31,10,21,'new','S1 新增寻访/待复核','待复核','41','2026-08-17')"
        )
        conn.commit()
        conn.close()

        service.decide_workflow_approval(approval["approval_id"], "approve")
        completed = self._wait_for(service, workflow_id, {"completed", "failed", "blocked"})
        assert completed["workflow"]["status"] == "completed"

        conn = sqlite3.connect(self.db_path)
        relation_stage = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=30").fetchone()[0]
        new_relation_stage = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=31").fetchone()[0]
        candidate_status = conn.execute("SELECT status FROM candidates WHERE id=40").fetchone()[0]
        new_candidate_status = conn.execute("SELECT status FROM candidates WHERE id=41").fetchone()[0]
        event = conn.execute(
            "SELECT event_status, raw_json FROM candidate_events WHERE job_candidate_id=30 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert relation_stage == "H5 最近寻访/初筛不通过"
        assert new_relation_stage == "S1 新增寻访/待复核"
        assert candidate_status == "new"
        assert new_candidate_status == "new"
        assert event[0] == "stop"
        assert '"candidate_record_preserved": true' in event[1]

    def test_unknown_domain_nonmatching_blocks_without_approval_or_running_leak(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE jobs SET title='财务经理',summary='' WHERE id=10")
        conn.commit()
        conn.close()
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        objective = "清理当前岗位不匹配的候选关系，保留人选主档"
        created = service.create_goal(
            objective,
            {
                "type": "job",
                "id": 10,
                "page": "positions",
                "goal_inputs": {"scope_mode": "nonmatching"},
                "intent_understanding": {
                    "speech_act": "execute",
                    "action": "candidate_relationship_cleanup",
                    "objective": objective,
                    "target": {"type": "job", "id": 10},
                    "confidence": 1.0,
                },
            },
        )
        workflow_id = created["workflow"]["workflow_id"]
        plan_ref = created["plan_ref"]
        service.start_workflow(
            workflow_id,
            expected_plan_version=int(plan_ref["version"]),
            expected_plan_hash=str(plan_ref["plan_hash"]),
        )

        blocked = self._wait_for(service, workflow_id, {"blocked", "failed", "waiting_approval"})

        assert blocked["workflow"]["status"] == "blocked"
        assert blocked["goal"]["status"] == "blocked"
        assert blocked["steps"][0]["status"] == "blocked"
        assert "无法安全自动判断" in str(blocked["steps"][0]["error"])
        assert blocked["approvals"] == []
        assert not [event for event in blocked["events"] if event["event_type"] == "approval_required"]

    def test_copilot_unknown_domain_nonmatching_does_not_claim_approval_exists(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE jobs SET title='财务经理',summary='' WHERE id=10")
        conn.commit()
        conn.close()
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)

        result = service.copilot(
            "可以，直接清理和这个岗位关联，不用清理掉人选",
            session_id="unknown-domain-cleanup",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        assert result.get("workflow_id") is None
        assert "未创建归档审批" in str(result.get("answer") or "")
        assert "无法安全自动判断" in str(result.get("answer") or "")
        assert result["turn_decision"]["blocked_reason"] == "unsupported_candidate_filter_domain"
