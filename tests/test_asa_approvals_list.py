"""GET /api/v1/approvals 只读审批列表：默认 pending、status 过滤、字段完整、空结果。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app

SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))
GOAL_ID = "goal_approval_list_test"
WORKFLOW_ID = "workflow_approval_list_test"
GOAL_TITLE = "测试目标：审批列表验收"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    target = tmp_path_factory.mktemp("approvals-list") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    conn = sqlite3.connect(target)
    try:
        conn.execute(
            "INSERT INTO agent_goals (goal_id,objective,title) VALUES (?,?,?)",
            (GOAL_ID, "审批列表端点测试目标", GOAL_TITLE),
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id) VALUES (?,?)",
            (WORKFLOW_ID, GOAL_ID),
        )
        conn.executemany(
            """INSERT INTO agent_approvals
               (approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,status)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                ("approval_list_pending_r3", GOAL_ID, WORKFLOW_ID, 1, "multi_channel_sourcing", "R3", "外部寻访审批", "pending"),
                ("approval_list_pending_r2", GOAL_ID, WORKFLOW_ID, 2, "consultant_recommendation", "R2", "推荐审批", "pending"),
                ("approval_list_approved", GOAL_ID, WORKFLOW_ID, 3, "multi_channel_sourcing", "R3", "已批准的历史审批", "approved"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"db_path": target}


def test_approvals_default_returns_only_pending(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get("/api/v1/approvals")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["items"], "fixture 里至少有两条 pending 审批"
    assert all(item["status"] == "pending" for item in body["items"])
    ids = {item["approval_id"] for item in body["items"]}
    assert {"approval_list_pending_r3", "approval_list_pending_r2"} <= ids
    assert "approval_list_approved" not in ids


def test_approvals_status_filter(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get("/api/v1/approvals", params={"status": "approved"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert items and all(item["status"] == "approved" for item in items)
    assert "approval_list_approved" in {item["approval_id"] for item in items}


def test_approvals_item_fields_complete(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get("/api/v1/approvals")
    item = next(row for row in response.json()["items"] if row["approval_id"] == "approval_list_pending_r3")
    for key in ("approval_id", "workflow_id", "goal_id", "risk_level", "title", "status", "created_at", "goal_title"):
        assert key in item, f"缺少字段 {key}"
    assert item["workflow_id"] == WORKFLOW_ID
    assert item["goal_id"] == GOAL_ID
    assert item["risk_level"] == "R3"
    assert item["title"] == "外部寻访审批"
    # fixture 库有触发器把 goal title 改写为「工作流｜+objective」，这里只校验标题确实来自目标
    assert "审批列表端点测试目标" in str(item["goal_title"] or "")
    assert item["created_at"]


def test_approvals_empty_result(env: dict) -> None:
    with TestClient(create_app(db_path=env["db_path"], start_legacy=False)) as client:
        response = client.get("/api/v1/approvals", params={"status": "no_such_status"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "items": []}
