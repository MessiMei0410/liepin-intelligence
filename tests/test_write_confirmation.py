"""写确认链路（人确认机制闸门）测试。

背景：DSH 脑的写动作此前只靠 prompt 约束——模型调 preflight 自由铸造 5 分钟
一次性 token 后同轮即可 commit 写库。本套测试覆盖机制闸门：

- 候选人动作 commit：token 未激活 → 409 confirmation_required（不消费 token）；
  经 UI 通道（ASAApp UA 前缀门）激活后方可写入；token 一次性。
- 审批决定 / 工作流动作（cancel/pause/resume）：同一套 preflight → 激活 → 写入机制。
- 激活端点 UA 门：非 ASAApp/ UA（模型 DSH 工具面 fetch 通道）→ 403。
- Python 脑 pending_intent 签名确认链路（intents/confirm）不经 HTTP 写端点，
  不要求激活，行为不回归。
- DSH 写确认卡回填：confirm_request 随轮次落库、confirm_result 回写终态。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.intent import intent_signature
from asa_core.service import CoreService
from a_system_agent import AgentService, FakeLLM
from test_a_system_agent_v1 import fake_assessment

SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))
CANDIDATE_ID = 558
ACTIVE_STAGE = ("S1 新增寻访/待复核", "待复核", "search_shortlisted")
APP_HEADERS = {"User-Agent": "ASAApp/test-suite"}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。各测试用独立
    # request_id/approval_id，候选人 558 相关测试先复位状态，共享安全。
    target = tmp_path_factory.mktemp("write-confirmation") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _reset_candidate(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
        (*ACTIVE_STAGE, CANDIDATE_ID),
    )
    conn.commit()
    conn.close()


def _preflight(client: TestClient, candidate_id: int = CANDIDATE_ID, action: str = "advance") -> dict:
    response = client.post(
        "/api/v1/candidate-actions/preflight",
        json={"request_id": f"wc-preflight-{uuid.uuid4().hex[:8]}", "candidate_id": candidate_id, "action": action},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _activate(client: TestClient, token: str, headers: dict | None = None):
    request_id = f"wc-activate-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/write-confirmations/activate",
        headers={"Idempotency-Key": request_id, **(headers or APP_HEADERS)},
        json={"request_id": request_id, "preflight_token": token},
    )


def _commit(client: TestClient, token: str, candidate_id: int = CANDIDATE_ID, action: str = "advance"):
    request_id = f"wc-commit-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/candidate-actions/commit",
        headers={"Idempotency-Key": request_id},
        json={
            "request_id": request_id,
            "candidate_id": candidate_id,
            "action": action,
            "preflight_token": token,
        },
    )


def test_candidate_commit_rejects_unactivated_token_without_consuming_it(db_path: Path) -> None:
    _reset_candidate(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = _preflight(client)
        blocked = _commit(client, preflight["token"])
        assert blocked.status_code == 409
        assert "confirmation_required" in blocked.json()["detail"]

        # 未激活的拒绝不消费 token：UI 在有效期内激活后仍可写入。
        activated = _activate(client, preflight["token"])
        assert activated.status_code == 200, activated.text
        assert activated.json()["activated"] is True
        committed = _commit(client, preflight["token"])
        assert committed.status_code == 200, committed.text
        assert committed.json()["stage"] == "S2 复核通过/待联系"


def test_activate_endpoint_is_gated_by_app_user_agent(db_path: Path) -> None:
    _reset_candidate(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = _preflight(client)
        # 模型通道（DSH 工具面 fetch UA，无 ASAApp/ 前缀）拿不到激活能力。
        forbidden = _activate(client, preflight["token"], headers={"User-Agent": "asa-dsh-tools/1.0"})
        assert forbidden.status_code == 403
        allowed = _activate(client, preflight["token"])
        assert allowed.status_code == 200, allowed.text


def test_preflight_token_is_single_use_after_activation(db_path: Path) -> None:
    _reset_candidate(db_path)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = _preflight(client, action="contact")
        assert _activate(client, preflight["token"]).status_code == 200
        assert _commit(client, preflight["token"], action="contact").status_code == 200
        # 一次性：commit 消费后不能再激活、不能再提交。
        assert _activate(client, preflight["token"]).status_code == 409
        replayed = _commit(client, preflight["token"], action="contact")
        assert replayed.status_code == 409


def _create_workflow(app) -> tuple[str, str, int]:
    created = app.state.core.agent_service.create_goal(
        "给士兰微技术市场经理再找些候选人",
        {"type": "job", "id": 111},
    )
    return (
        created["goal"]["goal_id"],
        created["workflow"]["workflow_id"],
        int(created["steps"][0]["id"]),
    )


def _insert_pending_approval(db_path: Path, goal_id: str, workflow_id: str, step_id: int) -> str:
    approval_id = f"approval_test_{secrets.token_hex(4)}"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO agent_approvals
            (approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,preflight_json,
             status,token_hash,expires_at)
            VALUES (?,?,?,?,?,?,?,?,'pending',?,?)
            """,
            (
                approval_id, goal_id, workflow_id, step_id, "multi_channel_sourcing", "R3",
                "外部寻访审批", json.dumps({"action": "外部寻访"}, ensure_ascii=False),
                hashlib.sha256(b"test").hexdigest(),
                (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return approval_id


def _decision(client: TestClient, approval_id: str, decision: str, token: str = ""):
    request_id = f"wc-decision-{uuid.uuid4().hex[:8]}"
    payload = {"request_id": request_id, "decision": decision, "note": "测试决定"}
    if token:
        payload["preflight_token"] = token
    return client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        headers={"Idempotency-Key": request_id},
        json=payload,
    )


def test_approval_decision_requires_activated_token(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    goal_id, workflow_id, step_id = _create_workflow(app)
    approval_id = _insert_pending_approval(db_path, goal_id, workflow_id, step_id)
    with TestClient(app) as client:
        # 无 token → 400。
        assert _decision(client, approval_id, "reject").status_code == 400

        # 预检发 token；未激活 → 409 confirmation_required。
        preflight = client.post(
            f"/api/v1/approvals/{approval_id}/decision/preflight",
            json={"request_id": f"wc-ap-preflight-{uuid.uuid4().hex[:8]}", "decision": "reject"},
        )
        assert preflight.status_code == 200, preflight.text
        token = preflight.json()["token"]
        assert preflight.json()["approval"]["approval_id"] == approval_id
        blocked = _decision(client, approval_id, "reject", token)
        assert blocked.status_code == 409
        assert "confirmation_required" in blocked.json()["detail"]

        # 激活后写入成功；审批进入终态后再次预检 → 409。
        assert _activate(client, token).status_code == 200
        decided = _decision(client, approval_id, "reject", token)
        assert decided.status_code == 200, decided.text
        again = client.post(
            f"/api/v1/approvals/{approval_id}/decision/preflight",
            json={"request_id": f"wc-ap-preflight2-{uuid.uuid4().hex[:8]}", "decision": "approve"},
        )
        assert again.status_code == 409


def test_approval_decision_preflight_validation(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        bad = client.post(
            "/api/v1/approvals/approval_missing/decision/preflight",
            json={"request_id": f"wc-ap-missing-{uuid.uuid4().hex[:8]}", "decision": "approve"},
        )
        assert bad.status_code == 404
        app = create_app(db_path=db_path, start_legacy=False)
        goal_id, workflow_id, step_id = _create_workflow(app)
        approval_id = _insert_pending_approval(db_path, goal_id, workflow_id, step_id)
        invalid = client.post(
            f"/api/v1/approvals/{approval_id}/decision/preflight",
            json={"request_id": f"wc-ap-invalid-{uuid.uuid4().hex[:8]}", "decision": "maybe"},
        )
        assert invalid.status_code == 409


def test_workflow_action_requires_activated_token(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    _, workflow_id, _ = _create_workflow(app)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE agent_workflows SET status='running' WHERE workflow_id=?", (workflow_id,))
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as client:
        # 无 token → 400；预检校验动作枚举与 note 必填。
        bare = client.post(
            f"/api/v1/workflows/{workflow_id}/pause",
            headers={"Idempotency-Key": f"wc-pause-{uuid.uuid4().hex[:8]}"},
            json={"request_id": f"wc-pause-{uuid.uuid4().hex[:8]}", "note": "暂停"},
        )
        assert bare.status_code == 400
        bad_action = client.post(
            f"/api/v1/workflows/{workflow_id}/actions/preflight",
            json={"request_id": f"wc-wf-bad-{uuid.uuid4().hex[:8]}", "action": "start", "note": "x"},
        )
        assert bad_action.status_code == 409
        no_note = client.post(
            f"/api/v1/workflows/{workflow_id}/actions/preflight",
            json={"request_id": f"wc-wf-nonote-{uuid.uuid4().hex[:8]}", "action": "pause", "note": " "},
        )
        assert no_note.status_code == 409

        # 未激活 → 409 confirmation_required；激活后动作生效。
        preflight = client.post(
            f"/api/v1/workflows/{workflow_id}/actions/preflight",
            json={"request_id": f"wc-wf-preflight-{uuid.uuid4().hex[:8]}", "action": "pause", "note": "测试暂停"},
        )
        assert preflight.status_code == 200, preflight.text
        token = preflight.json()["token"]
        blocked = client.post(
            f"/api/v1/workflows/{workflow_id}/pause",
            headers={"Idempotency-Key": f"wc-pause2-{uuid.uuid4().hex[:8]}"},
            json={"request_id": f"wc-pause2-{uuid.uuid4().hex[:8]}", "note": "测试暂停", "preflight_token": token},
        )
        assert blocked.status_code == 409
        assert "confirmation_required" in blocked.json()["detail"]
        assert _activate(client, token).status_code == 200
        paused = client.post(
            f"/api/v1/workflows/{workflow_id}/pause",
            headers={"Idempotency-Key": f"wc-pause3-{uuid.uuid4().hex[:8]}"},
            json={"request_id": f"wc-pause3-{uuid.uuid4().hex[:8]}", "note": "测试暂停", "preflight_token": token},
        )
        assert paused.status_code == 200, paused.text
        missing = client.post(
            "/api/v1/workflows/workflow_missing/actions/preflight",
            json={"request_id": f"wc-wf-missing-{uuid.uuid4().hex[:8]}", "action": "pause", "note": "x"},
        )
        assert missing.status_code == 404


def test_copilot_intent_confirm_legacy_path_bypasses_activation(db_path: Path) -> None:
    """Python 脑 pending_intent 签名确认链路（intents/confirm 内部走服务层 commit）
    自带人确认（签名 pending_intent + 确认卡点击），不要求激活，行为不回归。"""
    _reset_candidate(db_path)
    app = create_app(db_path=db_path, start_legacy=False)
    core: CoreService = app.state.core
    message = "张桂芳复核通过"
    preflight = core.candidate_preflight(CANDIDATE_ID, "advance")
    result = core.confirm_copilot_intent(
        {"kind": "candidate_action", "action": "advance", "message": message},
        intent_hash=intent_signature("candidate_action", "advance", CANDIDATE_ID, message),
        candidate_id=CANDIDATE_ID,
        preflight_token=preflight["token"],
        message=message,
        session_id=f"wc-legacy-{uuid.uuid4().hex[:8]}",
    )
    assert result["ok"] is True
    assert result["candidate_action"]["stage"] == "S2 复核通过/待联系"


def test_record_turn_stores_confirm_request_and_applies_terminal_state(db_path: Path) -> None:
    service = AgentService(db_path, FakeLLM(fake_assessment()))
    session_id = f"wc-session-{uuid.uuid4().hex[:8]}"
    request_id = f"wc-turn-{uuid.uuid4().hex[:8]}"
    confirm_request = {
        "kind": "candidate_action",
        "preflight_token": "tok-1",
        "expires_at": "2026-08-19T12:00:00",
        "action": "advance",
        "candidate": {"id": CANDIDATE_ID, "name": "张桂芳"},
    }
    recorded = service.record_external_copilot_turn(
        session_id=session_id,
        request_id=request_id,
        message="把张桂芳复核通过",
        answer="已在界面发起确认",
        context={"type": "candidate", "id": CANDIDATE_ID},
        source="dsh",
        confirm_request=confirm_request,
    )
    assert recorded["recorded"] is True
    detail = service.get_copilot_session(session_id)
    stored = detail["messages"][1]["confirm_request"]
    assert stored["state"] == "pending"
    assert stored["preflight_token"] == "tok-1"

    receipt = {"version": "execution_receipt_v1", "state": "已完成", "summary": "已确认并同步", "verified": True}
    updated = service.record_external_copilot_turn(
        session_id=session_id,
        request_id=request_id,
        message="把张桂芳复核通过",
        answer="已在界面发起确认",
        context={"type": "candidate", "id": CANDIDATE_ID},
        source="dsh",
        confirm_result={"state": "confirmed", "summary": "已确认并同步", "execution_receipt": receipt},
    )
    assert updated["ok"] is True
    assert updated["updated"] is True
    detail = service.get_copilot_session(session_id)
    assistant = detail["messages"][1]
    assert assistant["confirm_request"]["state"] == "confirmed"
    assert assistant["confirm_request"]["result_summary"] == "已确认并同步"
    assert assistant["execution_receipt"]["verified"] is True
    assert len(detail["messages"]) == 2

    cancelled = service.record_external_copilot_turn(
        session_id=session_id,
        request_id=request_id,
        message="m",
        answer="a",
        context={},
        confirm_result={"state": "cancelled"},
    )
    assert cancelled["updated"] is True
    detail = service.get_copilot_session(session_id)
    assert detail["messages"][1]["confirm_request"]["state"] == "cancelled"
    service.close()
