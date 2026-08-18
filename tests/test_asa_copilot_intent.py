"""PRD 阶段 4 R9：Copilot 意图结构化测试。

覆盖：
- 语料清单（tests/fixtures/copilot_intent_corpus.json）命中率：直写/待确认/无意图；
- 询问句零误写（解析层 + API 层，候选人状态不变）；
- pending_intent 产生（不直写）与确认执行全链（preflight token → commit → 审计 → 幂等）；
- 状态漂移 409（已停止的再停止）、签名篡改 409；
- 现有直写行为回归（明确短句仍直写）；
- workflow_id 纪律：意图解析与确认链路不得出现"已启动寻访"语义。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.intent import parse_candidate_intent

SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))
CORPUS_PATH = Path(__file__).parent / "fixtures" / "copilot_intent_corpus.json"
CANDIDATE_ID = 558
ACTIVE_STAGE = ("S1 新增寻访/待复核", "待复核", "search_shortlisted")
STOPPED_STAGE = ("H5 最近寻访/初筛不通过", "最近寻访", "screen_rejected")


@pytest.fixture()
def corpus() -> list[dict]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["entries"]


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # 模块级共享副本：整模块只复制一次生产库（1.5GB）。测试间隔离靠各测试
    # 开头 _set_candidate_stage 先清洗候选人 558 状态再操作。
    target = tmp_path_factory.mktemp("copilot-intent") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _set_candidate_stage(db_path: Path, stage: tuple[str, str, str]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
        (*stage, CANDIDATE_ID),
    )
    conn.commit()
    conn.close()


def _candidate_row(db_path: Path) -> tuple:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT clean_stage,flow_bucket,raw_status FROM job_candidates WHERE id=?", (CANDIDATE_ID,)
    ).fetchone()
    conn.close()
    return row


def _post_message(client: TestClient, message: str, context: dict | None = None) -> dict:
    request_id = f"intent-{uuid.uuid4().hex[:12]}"
    response = client.post(
        "/api/v1/copilot/messages",
        headers={"Idempotency-Key": request_id},
        json={
            "request_id": request_id,
            "session_id": f"intent-test-{uuid.uuid4().hex[:8]}",
            "message": message,
            "context": context or {"type": "candidate", "id": CANDIDATE_ID, "source": "asa_floating"},
        },
    )
    assert response.status_code == 200
    return response.json()


def _post_confirm(
    client: TestClient,
    pending: dict,
    *,
    intent_hash: str | None = None,
    action: str | None = None,
    kind: str | None = None,
    preflight_token: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
):
    confirm_id = request_id or f"confirm-{uuid.uuid4().hex[:12]}"
    return client.post(
        "/api/v1/copilot/intents/confirm",
        headers={"Idempotency-Key": confirm_id},
        json={
            "request_id": confirm_id,
            # 共享副本：服务层幂等键由 session_id|target|action|message 派生，
            # 默认每次调用唯一 session_id 避免跨测试命中前序确认的幂等重放；
            # 幂等重放测试显式复用同一 session_id 验证真实客户端重放语义。
            "session_id": session_id or f"intent-test-confirm-{uuid.uuid4().hex[:8]}",
            "intent": {"kind": kind or pending["kind"], "action": action or pending["action"]},
            "intent_hash": intent_hash if intent_hash is not None else pending["intent_hash"],
            "candidate_id": pending["candidate"]["id"],
            "preflight_token": preflight_token if preflight_token is not None else pending["preflight_token"],
            "message": pending["message"],
        },
    )


# ---------------------------------------------------------------------------
# 语料命中率：解析层
# ---------------------------------------------------------------------------

def test_intent_corpus_parser_expectations(corpus: list[dict]) -> None:
    assert len(corpus) >= 40
    failures = []
    for entry in corpus:
        parsed = parse_candidate_intent(entry["message"])
        expected = entry["expected"]
        got = {"tier": parsed["tier"], "kind": parsed["kind"], "action": parsed["action"]}
        if got != expected:
            failures.append({"message": entry["message"], "expected": expected, "got": got})
    assert failures == []


def test_question_corpus_has_at_least_ten_entries(corpus: list[dict]) -> None:
    questions = [e for e in corpus if e["note"].startswith("询问句零误写")]
    assert len(questions) >= 10
    for entry in questions:
        parsed = parse_candidate_intent(entry["message"])
        assert parsed["kind"] == "none", entry["message"]


# ---------------------------------------------------------------------------
# 询问句零误写：API 层（候选人上下文下也不得写入、不得产生确认卡片）
# ---------------------------------------------------------------------------

def test_question_messages_never_write_nor_pend(corpus: list[dict], db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    questions = [e["message"] for e in corpus if e["note"].startswith("询问句零误写")]
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        for message in questions:
            body = _post_message(client, message)
            assert "pending_intent" not in body, message
            assert "candidate_action" not in body, message
            assert "candidate_update" not in body, message
            assert body.get("write_blocked") is not True, message
    assert _candidate_row(db_path) == ACTIVE_STAGE


# ---------------------------------------------------------------------------
# 同义语料：API 层产生 pending_intent，但确认前零写入
# ---------------------------------------------------------------------------

def test_confirm_tier_corpus_pends_without_write(corpus: list[dict], db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    confirm_entries = [e for e in corpus if e["expected"]["tier"] == "confirm"]
    assert len(confirm_entries) >= 16
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        for entry in confirm_entries:
            body = _post_message(client, entry["message"])
            pending = body.get("pending_intent")
            assert pending, entry["message"]
            assert pending["action"] == entry["expected"]["action"], entry["message"]
            assert pending["kind"] == "candidate_action"
            assert pending["target_scope"] == "current_candidate"
            assert pending["candidate"]["id"] == CANDIDATE_ID
            assert pending["confirm_text"].endswith("确认？")
            assert pending["intent_hash"]
            assert pending["preflight_token"]
            assert "未确认前不会写入 ASA" in body["answer"]
            # workflow_id 纪律：待确认链路不得建立/启动工作流
            assert not body.get("workflow_id"), entry["message"]
            assert not body.get("goal_id"), entry["message"]
            assert "已启动寻访" not in body["answer"]
            assert "已建立目标" not in body["answer"]
            assert all(item.get("type") != "start_workflow" for item in body.get("suggested_actions") or [])
            assert "candidate_action" not in body, entry["message"]
    # 全部消息过后候选人仍未被写入
    assert _candidate_row(db_path) == ACTIVE_STAGE


# ---------------------------------------------------------------------------
# pending_intent → 确认执行全链：preflight token → commit → 审计 → 幂等
# ---------------------------------------------------------------------------

def test_pending_intent_confirm_full_chain(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这人不合适，停了吧")
        pending = body["pending_intent"]
        assert pending["action"] == "stop"
        assert pending["confirm_text"] == "将停止推进 黄**，确认？"
        card = body["action_card"]
        assert card["proposal_id"] is None  # candidate writes retain their existing preflight/commit chain
        assert card["capability_id"] == "candidate_action"
        assert card["risk_level"] == "R2"
        assert card["next_actions"][0]["type"] == "confirm_candidate_intent"
        # 确认前未写入
        assert _candidate_row(db_path) == ACTIVE_STAGE
        assert client.get(f"/api/v1/candidates/{CANDIDATE_ID}").json()["candidate"]["is_stopped"] is False

        confirm_id = f"confirm-{uuid.uuid4().hex[:12]}"
        confirm_session = f"intent-test-confirm-replay-{uuid.uuid4().hex[:8]}"
        first = _post_confirm(client, pending, request_id=confirm_id, session_id=confirm_session)
        assert first.status_code == 200
        confirmed = first.json()
        assert confirmed["ok"] is True
        assert confirmed["candidate_action"]["action"] == "stop"
        assert confirmed["candidate_action"]["ok"] is True
        assert "已确认并同步到 ASA" in confirmed["answer"]
        # 确认链路不得声称寻访/工作流语义
        assert "workflow_id" not in confirmed
        assert "已启动寻访" not in confirmed["answer"]
        assert "启动寻访" not in confirmed["answer"]
        assert "已建立目标" not in confirmed["answer"]
        # 写入生效
        assert client.get(f"/api/v1/candidates/{CANDIDATE_ID}").json()["candidate"]["is_stopped"] is True
        assert _candidate_row(db_path) == STOPPED_STAGE
        # 审计落库
        audit_id = confirmed["candidate_action"]["receipt"]["audit_event_id"]
        conn = sqlite3.connect(db_path)
        audit_row = conn.execute(
            "SELECT operation,target_type,target_id,result FROM audit_events WHERE event_id=?", (audit_id,)
        ).fetchone()
        conn.close()
        assert audit_row == ("candidate.commit", "job_candidate", str(CANDIDATE_ID), "success")
        # 幂等重放：相同 request_id + Idempotency-Key + session_id 返回同一结果，不重复写入
        replay = _post_confirm(client, pending, request_id=confirm_id, session_id=confirm_session)
        assert replay.status_code == 200
        assert replay.json()["candidate_action"]["action"] == "stop"


def test_pending_intent_confirm_state_drift_returns_409(db_path: Path) -> None:
    """确认期间候选人被停止：沿用"已停止的再停止→409"语义（防状态漂移）。"""
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这人不合适，停了吧")
        pending = body["pending_intent"]
        # 状态漂移：确认到达前候选人已被停止
        _set_candidate_stage(db_path, STOPPED_STAGE)
        response = _post_confirm(client, pending)
        assert response.status_code == 409
        assert "已停止" in response.json()["detail"]


def test_confirm_rejects_tampered_hash_and_action(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这个可以，约面试吧")
        pending = body["pending_intent"]
        assert pending["action"] == "advance"
        # 篡改签名
        forged = _post_confirm(client, pending, intent_hash="0" * 64)
        assert forged.status_code == 409
        # 篡改动作（拿 advance 的签名去确认 stop）
        swapped = _post_confirm(client, pending, action="stop")
        assert swapped.status_code == 409
        # 不支持的意图类型
        bad_kind = _post_confirm(client, pending, kind="candidate_update")
        assert bad_kind.status_code == 409
        # 缺 preflight token
        missing_token = _post_confirm(client, pending, preflight_token="")
        assert missing_token.status_code == 400
        # 候选人仍未被写入
        assert _candidate_row(db_path) == ACTIVE_STAGE


# ---------------------------------------------------------------------------
# 目标指代消歧与§6 不可破坏规则
# ---------------------------------------------------------------------------

def test_extended_intent_without_candidate_context_never_pends(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这人不合适，停了吧", context={"type": "global", "source": "asa_floating"})
        assert "pending_intent" not in body
        assert "candidate_action" not in body
        assert body["write_blocked"] is True
        assert "尚未写入 ASA" in body["answer"]
    assert _candidate_row(db_path) == ACTIVE_STAGE


def test_stopped_candidate_extended_advance_intent_blocked(db_path: Path) -> None:
    """§6：停止即淘汰，已停止人选不得被新意图重新推进。"""
    _set_candidate_stage(db_path, STOPPED_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这个可以，约面试吧")
        assert "pending_intent" not in body
        assert body["write_blocked"] is True
        assert "未写入 ASA" in body["answer"]
        assert "已经停止推进" in body["answer"]
    assert _candidate_row(db_path) == STOPPED_STAGE


def test_stopped_candidate_extended_stop_intent_produces_no_card(db_path: Path) -> None:
    """已停止人选的停止类表达：目标状态已达成，不产生确认卡片，返回 already_applied 回执。"""
    _set_candidate_stage(db_path, STOPPED_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这人不合适，停了吧")
        assert "pending_intent" not in body
        assert body.get("candidate_action", {}).get("already_applied") is True
        assert "已同步到 ASA" in body["answer"]
        assert body.get("write_blocked") is not True
    assert _candidate_row(db_path) == STOPPED_STAGE


def test_extended_intent_already_applied_produces_no_card(db_path: Path) -> None:
    """已应用动作再次表达：不产生确认卡片，返回 already_applied 回执。"""
    _set_candidate_stage(db_path, ("S3 已联系/待回复", "联系推进", "contacted"))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "我跟他电话聊过了")
        assert "pending_intent" not in body
        assert body.get("candidate_action", {}).get("already_applied") is True
        assert "已同步到 ASA" in body["answer"]
        assert body.get("write_blocked") is not True
    assert _candidate_row(db_path) == ("S3 已联系/待回复", "联系推进", "contacted")


# ---------------------------------------------------------------------------
# 顾问式交互（2026-08-13 重构）：明确短句与扩展表达统一走确认层，
# 只产出 pending_intent（含签名 + preflight token），由确认端点执行。
# ---------------------------------------------------------------------------

def test_direct_short_phrases_go_through_confirmation(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这个人选复核不通过")
        assert body["pending_intent"]["action"] == "stop"
        assert body["pending_intent"]["candidate"]["id"] == CANDIDATE_ID
        assert body["pending_intent"]["preflight_token"]
        assert "未确认前不会写入 ASA" in body["answer"]
        assert body.get("write_blocked") is None
    assert _candidate_row(db_path) == ACTIVE_STAGE  # 未确认不落库


def test_direct_contact_short_phrase_goes_through_confirmation(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "这个人选已联系")
        assert body["pending_intent"]["action"] == "contact"
        assert body["pending_intent"]["candidate"]["id"] == CANDIDATE_ID
        assert body["pending_intent"]["preflight_token"]
        assert "未确认前不会写入 ASA" in body["answer"]


# ---------------------------------------------------------------------------
# workflow_id 纪律：recommend 同义句命中工作流意图词时也不得建立工作流
# ---------------------------------------------------------------------------

def test_recommend_synonym_never_creates_workflow(db_path: Path) -> None:
    _set_candidate_stage(db_path, ACTIVE_STAGE)
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = _post_message(client, "可以推给客户了")
        assert body["pending_intent"]["action"] == "recommend"
        assert not body.get("workflow_id")
        assert not body.get("goal_id")
        assert not body.get("plan_summary")
        assert "已启动寻访" not in body["answer"]
        assert "已建立目标" not in body["answer"]
    assert _candidate_row(db_path) == ACTIVE_STAGE
