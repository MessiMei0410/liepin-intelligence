from __future__ import annotations

import sqlite3
from pathlib import Path

from asa_core.app import SourcingAdjustmentItemResponse
from asa_core.service import CoreService


def _service(db_path: Path) -> CoreService:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT);
        CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,clean_stage TEXT);
        CREATE TABLE agent_sourcing_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            candidate_id INTEGER,
            adjust_type TEXT NOT NULL,
            value TEXT NOT NULL,
            rationale TEXT,
            confidence REAL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            accepted_at TEXT,
            applied_at TEXT,
            applied_round INTEGER,
            dedupe_key TEXT UNIQUE,
            baseline_json TEXT,
            applied_workflow_id TEXT,
            applied_artifact_id TEXT
        );
        INSERT INTO people VALUES (1,'张三');
        INSERT INTO job_candidates VALUES (9,154,1,'H5 最近寻访/初筛不通过');
        INSERT INTO agent_sourcing_adjustments
            (job_id,candidate_id,adjust_type,value,rationale,status)
        VALUES (154,9,'add_keyword','固晶键合','停止备注提取','pending');
        """
    )
    conn.commit()
    conn.close()
    service = CoreService.__new__(CoreService)
    service.db_path = db_path
    return service


def test_confirm_accepts_without_claiming_strategy_application(tmp_path: Path) -> None:
    service = _service(tmp_path / "adjustment.db")

    first = service.confirm_sourcing_adjustment(1)
    second = service.confirm_sourcing_adjustment(1)

    assert first["status"] == "accepted"
    assert first["accepted_at"]
    assert first["applied_at"] is None
    assert first["applied_round"] is None
    assert first["applied_workflow_id"] is None
    assert first["applied_artifact_id"] is None
    assert first["already_accepted"] is False
    assert second["status"] == "accepted"
    assert second["already_accepted"] is True
    assert second["accepted_at"] == first["accepted_at"]

    listed = service.list_sourcing_adjustments(154)
    assert listed["summary"] == {"pending": 0, "accepted": 1, "applied": 0, "ignored": 0}
    assert listed["items"][0]["candidate_name"] == "张三"
    assert listed["items"][0]["status"] == "accepted"


def test_openapi_response_status_includes_accepted() -> None:
    status_schema = SourcingAdjustmentItemResponse.model_json_schema()["properties"]["status"]
    assert status_schema["enum"] == ["pending", "accepted", "applied", "ignored"]

