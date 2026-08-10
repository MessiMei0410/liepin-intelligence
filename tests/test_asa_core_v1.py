from __future__ import annotations

import base64
import io
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
import openpyxl
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.database import migrate
from asa_core.service import (
    _explicit_candidate_update,
    _public_effective_strategy,
    _resume_overview_summary,
    _workflow_action_card,
)
from a_system_agent.context import build_candidate_context
import liepin_workbench_server as legacy


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


def test_copilot_attachment_reads_xlsx_without_exposing_a_local_path(db_path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "新增需求"
    sheet.append(["客户", "岗位方向", "优先级"])
    sheet.append(["长川科技", "探针台 CPO", "急"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    content = buffer.getvalue()

    app = create_app(db_path=db_path, start_legacy=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/copilot/attachments",
            json={
                "request_id": "attachment-test-xlsx",
                "file_name": "探针台CPO方向新增需求.xlsx",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )

    assert response.status_code == 200
    attachment = response.json()["attachment"]
    assert attachment["content_available"] is True
    assert "asa-attachment-" not in json.dumps(attachment, ensure_ascii=False)
    assert "extracted_text" not in attachment
    conn = sqlite3.connect(db_path)
    stored = conn.execute(
        "SELECT extracted_text,session_id FROM agent_copilot_attachments WHERE attachment_id=?",
        (attachment["attachment_id"],),
    ).fetchone()
    conn.close()
    assert "[工作表] 新增需求" in stored[0]
    assert "长川科技\t探针台 CPO\t急" in stored[0]
    assert stored[1] == ""


def test_copilot_attachment_rejects_unsupported_and_invalid_content(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    with TestClient(app) as client:
        unsupported = client.post(
            "/api/v1/copilot/attachments",
            json={
                "request_id": "attachment-test-bin",
                "file_name": "payload.bin",
                "size_bytes": 4,
                "content_base64": "dGVzdA==",
            },
        )
        invalid = client.post(
            "/api/v1/copilot/attachments",
            json={
                "request_id": "attachment-test-invalid",
                "file_name": "需求.txt",
                "size_bytes": 4,
                "content_base64": "not-base64",
            },
        )

    assert unsupported.status_code == 415
    assert invalid.status_code == 400


def test_copilot_session_restores_attachment_metadata_without_document_text(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO agent_copilot_messages
        (session_id,context_type,context_id,role,content,structured_json)
        VALUES (?,?,?,?,?,?)
        """,
        (
            "attachment-history", "page", None, "user", "请分析附件",
            json.dumps({
                "uploaded_attachment_evidence": {
                    "items": [{
                        "attachment_id": "att-history", "file_name": "需求.xlsx",
                        "file_type": "xlsx", "size_bytes": 128, "content_available": True,
                        "extracted_text": "这是仅供模型使用的岗位正文", "truncated": False,
                        "status": "已读取附件正文。",
                    }]
                }
            }, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        response = client.get("/api/v1/copilot/sessions/attachment-history")

    assert response.status_code == 200
    attachment = response.json()["messages"][0]["context"]["uploaded_attachments"][0]
    assert attachment["file_name"] == "需求.xlsx"
    assert attachment["content_available"] is True
    assert "extracted_text" not in attachment


def test_read_no_reply_update_requires_a_statement_or_write_intent() -> None:
    assert _explicit_candidate_update("记录一下，这个人选消息已读未回") == "read_no_reply"
    assert _explicit_candidate_update("这个人选已读不回") == "read_no_reply"
    assert _explicit_candidate_update("这个人选已读不回怎么办") == ""


def test_copilot_session_read_endpoints_expose_task_history(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    app.state.core.agent_service.copilot(
        "继续推进这个岗位",
        session_id="agent-task-1",
        context={"type": "job", "id": 154, "client": "士兰微", "job": "电源专家"},
    )

    with TestClient(app) as client:
        sessions = client.get("/api/v1/copilot/sessions", params={"limit": 30})
        restored = client.get("/api/v1/copilot/sessions/agent-task-1", params={"limit": 100})
        unknown = client.get("/api/v1/copilot/sessions/missing-task")
        invalid_list_limit = client.get("/api/v1/copilot/sessions", params={"limit": 0})
        invalid_detail_limit = client.get("/api/v1/copilot/sessions/agent-task-1", params={"limit": 201})
        openapi = client.get("/openapi.json").json()

    assert sessions.status_code == 200
    assert sessions.json()["sessions"][0]["session_id"] == "agent-task-1"
    assert sessions.json()["sessions"][0]["title"] == "继续推进这个岗位"
    assert restored.status_code == 200
    assert [item["role"] for item in restored.json()["messages"]] == ["user", "assistant"]
    assert restored.json()["business_focus"]["context"]["type"] == "job"
    assert sessions.json()["sessions"][0]["business_focus"]["context"]["id"] == 154
    assert unknown.status_code == 404
    assert invalid_list_limit.status_code == 422
    assert invalid_detail_limit.status_code == 422
    assert "/api/v1/copilot/sessions" in openapi["paths"]
    assert "/api/v1/copilot/sessions/{session_id}" in openapi["paths"]


def test_copilot_session_metadata_can_be_renamed_archived_and_clear_focus(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    app.state.core.agent_service.copilot(
        "继续推进这个岗位",
        session_id="agent-task-manage",
        context={"type": "job", "id": 154, "client": "士兰微", "job": "电源专家"},
    )
    app.state.core.agent_service.get_copilot_focus = lambda _: (_ for _ in ()).throw(AssertionError("list must not perform N+1 focus reads"))

    with TestClient(app) as client:
        listed = client.get("/api/v1/copilot/sessions", params={"q": "继续推进"})
        renamed = client.patch(
            "/api/v1/copilot/sessions/agent-task-manage",
            headers={"Idempotency-Key": "rename-agent-task-manage"},
            json={"request_id": "rename-task-1", "title": "士兰微电源寻访"},
        )
        cleared = client.patch(
            "/api/v1/copilot/sessions/agent-task-manage",
            headers={"Idempotency-Key": "clear-agent-task-manage"},
            json={"request_id": "clear-task-1", "clear_focus": True},
        )
        archived = client.patch(
            "/api/v1/copilot/sessions/agent-task-manage",
            headers={"Idempotency-Key": "archive-agent-task-manage"},
            json={"request_id": "archive-task-1", "archived": True},
        )
        hidden = client.get("/api/v1/copilot/sessions")
        included = client.get("/api/v1/copilot/sessions", params={"include_archived": "true", "q": "士兰微电源"})
        openapi = client.get("/openapi.json").json()

    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["business_focus"]["context"]["type"] == "job"
    assert renamed.json()["title"] == "士兰微电源寻访"
    assert cleared.json()["business_focus"] is None
    assert archived.json()["archived"] is True
    assert "agent-task-manage" not in {item["session_id"] for item in hidden.json()["sessions"]}
    assert included.json()["sessions"][0]["session_id"] == "agent-task-manage"
    assert openapi["paths"]["/api/v1/copilot/sessions"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("CopilotSessionListResponse")


def test_copilot_sessions_can_be_archived_in_one_idempotent_action(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO agent_copilot_messages(session_id,role,content) VALUES (?,?,?)",
            [
                ("bulk-task-1", "user", "任务一"),
                ("bulk-task-2", "user", "任务二"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        before = client.get("/api/v1/copilot/sessions", params={"limit": 100})
        response = client.post(
            "/api/v1/copilot/sessions/archive-all",
            headers={"Idempotency-Key": "archive-all-agent-tasks"},
            json={"request_id": "archive-all-1"},
        )
        replay = client.post(
            "/api/v1/copilot/sessions/archive-all",
            headers={"Idempotency-Key": "archive-all-agent-tasks"},
            json={"request_id": "archive-all-1"},
        )
        visible = client.get("/api/v1/copilot/sessions", params={"limit": 100})
        archived = client.get(
            "/api/v1/copilot/sessions",
            params={"limit": 100, "include_archived": "true"},
        )

    assert response.status_code == 200
    before_ids = {item["session_id"] for item in before.json()["sessions"]}
    assert {"bulk-task-1", "bulk-task-2"}.issubset(before_ids)
    assert response.json()["archived_count"] == len(before_ids)
    assert set(response.json()["session_ids"]) == before_ids
    assert replay.json() == response.json()
    assert visible.json()["sessions"] == []
    assert {"bulk-task-1", "bulk-task-2"}.issubset(
        {item["session_id"] for item in archived.json()["sessions"]}
    )


def test_copilot_session_patch_requires_visible_messages(db_path: Path) -> None:
    # 仅存在于 metadata 或 focus、没有任何消息的会话在列表里永远不可见，
    # PATCH 必须按不存在处理（与 GET detail 的 404 语义一致）。
    app = create_app(db_path=db_path, start_legacy=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO agent_copilot_sessions(session_id,title) VALUES ('ghost-metadata-task','幽灵会话')")
        conn.execute("INSERT INTO agent_copilot_focus(session_id) VALUES ('ghost-focus-task')")
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        metadata_ghost = client.patch(
            "/api/v1/copilot/sessions/ghost-metadata-task",
            headers={"Idempotency-Key": "ghost-metadata-patch"},
            json={"request_id": "ghost-metadata-1", "title": "改名"},
        )
        focus_ghost = client.patch(
            "/api/v1/copilot/sessions/ghost-focus-task",
            headers={"Idempotency-Key": "ghost-focus-patch"},
            json={"request_id": "ghost-focus-1", "clear_focus": True},
        )
        listed = client.get("/api/v1/copilot/sessions")

    assert metadata_ghost.status_code == 404
    assert focus_ghost.status_code == 404
    visible = {item["session_id"] for item in listed.json()["sessions"]}
    assert "ghost-metadata-task" not in visible
    assert "ghost-focus-task" not in visible


def test_orphan_copilot_session_metadata_is_collected_at_init(db_path: Path) -> None:
    # 消息被删后残留的 metadata 是孤儿：初始化（ensure_schema）时清理；
    # 有消息的会话 metadata 不受影响，且清理幂等可重复。
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO agent_copilot_sessions(session_id,title) VALUES ('orphan-task','孤儿会话')")
        conn.execute(
            """INSERT INTO agent_copilot_messages
               (session_id,context_type,context_id,role,content,structured_json)
               VALUES ('alive-task','job',154,'user','继续推进这个岗位','{}')"""
        )
        conn.execute("INSERT INTO agent_copilot_sessions(session_id,title) VALUES ('alive-task','活跃会话')")
        conn.commit()
    finally:
        conn.close()

    create_app(db_path=db_path, start_legacy=False)
    conn = sqlite3.connect(db_path)
    try:
        remaining = {
            row[0] for row in conn.execute(
                "SELECT session_id FROM agent_copilot_sessions WHERE session_id IN ('orphan-task','alive-task')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert remaining == {"alive-task"}


def test_copilot_session_patch_idempotency_key_is_scoped_to_target(db_path: Path) -> None:
    # 幂等查找键不含 target 时，同 key+body 打不同 session 会静默重放第一个 target 的响应；
    # target 纳入 request_hash 后必须判 409，同 target 的重放仍然正常。
    app = create_app(db_path=db_path, start_legacy=False)
    app.state.core.agent_service.copilot(
        "继续推进这个岗位",
        session_id="idem-task-a",
        context={"type": "job", "id": 154, "client": "士兰微", "job": "电源专家"},
    )
    app.state.core.agent_service.copilot(
        "继续推进这个岗位",
        session_id="idem-task-b",
        context={"type": "job", "id": 154, "client": "士兰微", "job": "电源专家"},
    )

    body = {"request_id": "idem-target-rename", "title": "统一标题"}
    headers = {"Idempotency-Key": "idem-target-rename-key"}
    with TestClient(app) as client:
        first = client.patch("/api/v1/copilot/sessions/idem-task-a", headers=headers, json=body)
        cross_target = client.patch("/api/v1/copilot/sessions/idem-task-b", headers=headers, json=body)
        replay = client.patch("/api/v1/copilot/sessions/idem-task-a", headers=headers, json=body)

    assert first.status_code == 200
    assert first.json()["title"] == "统一标题"
    assert cross_target.status_code == 409
    assert replay.status_code == 200
    assert replay.json()["title"] == "统一标题"
    conn = sqlite3.connect(db_path)
    try:
        other_title = conn.execute(
            "SELECT title FROM agent_copilot_sessions WHERE session_id='idem-task-b'"
        ).fetchone()
    finally:
        conn.close()
    assert other_title is None or other_title[0] is None


def test_copilot_session_search_escapes_like_wildcards(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """INSERT INTO agent_copilot_messages(session_id,context_type,context_id,role,content,structured_json)
               VALUES (?,?,?,?,?,?)""",
            [
                ("wild-task-power", "global", None, "user", "电源专家寻访进展", "{}"),
                ("wild-task-power", "global", None, "assistant", "好的", "{}"),
                ("wild-task-plain", "global", None, "user", "整理本周进展", "{}"),
                ("wild-task-plain", "global", None, "assistant", "收到", "{}"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        percent = client.get("/api/v1/copilot/sessions", params={"q": "%"})
        underscore = client.get("/api/v1/copilot/sessions", params={"q": "_"})
        backslash = client.get("/api/v1/copilot/sessions", params={"q": "\\"})
        exact = client.get("/api/v1/copilot/sessions", params={"q": "电源"})

    assert percent.status_code == 200
    for wildcard in (percent, underscore, backslash):
        wildcard_ids = {item["session_id"] for item in wildcard.json()["sessions"]}
        assert "wild-task-power" not in wildcard_ids
        assert "wild-task-plain" not in wildcard_ids
    exact_ids = {item["session_id"] for item in exact.json()["sessions"]}
    assert "wild-task-power" in exact_ids
    assert "wild-task-plain" not in exact_ids


def test_copilot_analysis_shortcut_tolerates_non_numeric_context_id(db_path: Path) -> None:
    # 非数字 context.id 不能抛 ValueError 变 500：降级为不带 scope，消息仍正常落库。
    app = create_app(db_path=db_path, start_legacy=False)
    core = app.state.core

    class StubAnalytics:
        def create_run(self, catalog_id: str, question: str, scope: dict) -> dict:
            return {
                "ok": True,
                "result": {
                    "run_id": "run-stub",
                    "catalog_id": catalog_id,
                    "status": "ok",
                    "headline": "渠道转化正常",
                    "metrics": [],
                    "data_as_of": "2026-08-01",
                    "scope": scope,
                    "references": [],
                },
            }

    core.analytics_service = StubAnalytics()
    intent, params = core._copilot_analysis_request("哪个渠道效果好", {"type": "job", "id": "JD-非数字"})
    assert intent == "channel_performance"
    assert "job_id" not in params

    result = core._run_copilot_analysis(
        "岗位健康度怎么样",
        session_id="analysis-non-numeric",
        context={"type": "job", "id": "JD-非数字"},
        catalog_id="job_health",
        scope={},
    )
    assert result["ok"] is True
    assert result["context"]["id"] == "JD-非数字"
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT DISTINCT context_id FROM agent_copilot_messages WHERE session_id='analysis-non-numeric'"
        ).fetchall()
    finally:
        conn.close()
    assert stored == [(None,)]


def test_resume_summary_excludes_work_and_project_body() -> None:
    summary = _resume_overview_summary(
        {
            "profile_text": "张三\n在职，看看新机会\n求职意向\n算法工程师\n工作经历\n示例科技\n职责业绩：不应显示\n项目经历\n保密项目"
        }
    )
    assert summary == "张三\n在职，看看新机会\n求职意向\n算法工程师"
    assert "职责业绩" not in summary
    assert "保密项目" not in summary


def test_workflow_action_card_requires_a_real_workflow_and_preserves_r3_approval() -> None:
    assert _workflow_action_card({}) is None
    card = _workflow_action_card({
        "workflow_id": "workflow_real",
        "goal": {"title": "士兰微寻访"},
        "workflow": {"status": "waiting_approval", "current_stage": "多渠道寻访"},
        "plan_summary": [{"capability_id": "multi_channel_sourcing", "label": "执行多渠道寻访", "status": "waiting_approval"}],
        "approvals": [{"approval_id": "approval_r3", "risk_level": "R3", "status": "pending"}],
    })
    assert card is not None
    assert card["context"]["id"] == "workflow_real"
    assert card["risk_level"] == "R3"
    assert card["next_actions"][-1] == {
        "type": "workflow_approval", "id": "approval_r3", "label": "批准本次外部寻访"
    }
    assert card["blocked_reasons"] == ["外部动作仍需 R3 单次审批"]
    assert card["business_summary"]["deliverable"] == "渠道结果、完整履历获取情况和可复核候选人名单"


def test_public_effective_strategy_excludes_restricted_rules_and_companies() -> None:
    strategy = _public_effective_strategy(
        {
            "schema_version": "strategy_v2",
            "plan": {"consultant_constraints": [{"type": "must", "rule": "必须有服务器电源经验"}]},
            "strategy_v2": {
                "input_level": "L2",
                "step1_job_essence": {"statement": "服务器电源核心岗"},
                "step2_target_pool": [{
                    "tier": "T1",
                    "companies": [
                        {"name": "公开目标公司", "source": "kb_profile"},
                        {"name": "禁挖公司", "source": "restricted_client"},
                    ],
                }],
                "step4_keyword_groups": [{"group": "core", "terms": ["服务器电源"]}],
                "negative_rules": [{"type": "禁挖名单", "rule": "禁挖公司", "source": "restricted_client"}],
            },
        },
        {
            "workflow_status": "waiting_approval",
            "context_json": json.dumps({"revision_number": 1}),
            "created_at": "2026-07-30 09:00:00",
            "workflow_id": "workflow_1",
            "artifact_id": "artifact_1",
        },
    )

    assert strategy["plan_version"] == 2
    assert strategy["company_tiers"][0]["companies"] == ["公开目标公司"]
    assert "negative_rules" not in strategy
    assert "禁挖公司" not in json.dumps(strategy, ensure_ascii=False)


def test_agent_mode_hides_executable_suggestions_without_a_real_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        client.app.state.core.agent_service.copilot = lambda *_args, **_kwargs: {
            "ok": True,
            "session_id": "agent_without_workflow",
            "answer": "候选人寻访已启动。",
            "suggested_actions": [
                {"type": "start_sourcing", "id": 111, "label": "开始寻访"},
                {"type": "open_job", "id": 111, "label": "打开岗位"},
            ],
        }
        response = client.post("/api/v1/copilot/agent", json={
            "request_id": "agent-without-workflow",
            "session_id": "agent_without_workflow",
            "message": "查看这个岗位的候选人覆盖情况",
            "context": {"type": "job", "id": 111},
        })
        assert response.status_code == 200, response.json()
        body = response.json()
        assert "尚未创建寻访工作流" in body["answer"]
        assert "action_card" not in body
        assert body["suggested_actions"] == [{"type": "open_job", "id": 111, "label": "打开岗位"}]


def test_agent_mode_returns_r3_action_card_only_after_real_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        client.app.state.core.agent_service.copilot = lambda *_args, **_kwargs: {
            "ok": True,
            "session_id": "agent_with_workflow",
            "answer": "已生成寻访执行计划。",
            "workflow_id": "workflow_agent_real",
            "goal": {"title": "士兰微电源专家寻访"},
            "workflow": {"status": "waiting_approval", "current_stage": "多渠道寻访"},
            "plan_summary": [{"capability_id": "multi_channel_sourcing", "label": "多渠道寻访", "status": "pending"}],
            "approvals": [{"approval_id": "approval_agent_r3", "risk_level": "R3", "status": "pending"}],
            "suggested_actions": [{"type": "start_sourcing", "id": 111, "label": "开始寻访"}],
        }
        response = client.post("/api/v1/copilot/agent", json={
            "request_id": "agent-with-workflow",
            "session_id": "agent_with_workflow",
            "message": "给士兰微电源专家补充人选",
            "context": {"type": "job", "id": 111},
        })
        assert response.status_code == 200
        body = response.json()
        assert body["suggested_actions"] == []
        assert body["action_card"]["context"]["id"] == "workflow_agent_real"
        assert body["action_card"]["risk_level"] == "R3"
        assert body["action_card"]["next_actions"][-1] == {
            "type": "workflow_approval", "id": "approval_agent_r3", "label": "批准本次外部寻访"
        }


def test_stream_replays_the_canonical_copilot_result_idempotently(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        calls = 0

        def canonical(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "ok": True,
                "session_id": "stream-parity",
                "answer": "统一决策结果",
                "context": {"type": "job", "id": 111},
                "references": [],
                "suggested_actions": [],
                "turn_decision": {"version": "turn_decision_v2", "effect": "answer"},
            }

        client.app.state.core.copilot = canonical
        payload = {
            "request_id": "stream-parity-request",
            "session_id": "stream-parity",
            "message": "这个岗位要不要继续寻访？",
            "context": {"type": "job", "id": 111},
        }
        headers = {"Idempotency-Key": "stream-parity-key"}
        first = client.post("/api/v1/copilot/stream", json=payload, headers=headers)
        replay = client.post("/api/v1/copilot/stream", json=payload, headers=headers)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert calls == 1
        assert "event: done" in first.text
        assert "turn_decision_v2" in replay.text
        assert '"idempotent_replay": true' in replay.text


def test_stream_emits_progress_first_and_skips_it_on_replay(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        client.app.state.core.copilot = lambda *_args, **_kwargs: {
            "ok": True,
            "session_id": "stream-progress",
            "answer": "处理完成",
            "context": {},
            "references": [],
            "suggested_actions": [],
        }
        payload = {
            "request_id": "stream-progress-request",
            "session_id": "stream-progress",
            "message": "今天先处理什么？",
            "context": {"type": "page", "page": "overview"},
        }
        headers = {"Idempotency-Key": "stream-progress-key"}
        first = client.post("/api/v1/copilot/stream", json=payload, headers=headers)
        replay = client.post("/api/v1/copilot/stream", json=payload, headers=headers)

        assert first.status_code == 200
        first_frame = first.text.split("\n\n", 1)[0]
        assert first_frame.startswith("event: progress")
        assert '"message"' in first_frame
        assert "event: done" in first.text
        # 重放路径直接放已登记结果，不重复 progress。
        assert replay.status_code == 200
        assert "event: progress" not in replay.text
        assert "event: done" in replay.text
        assert '"idempotent_replay": true' in replay.text


def test_stream_failure_ends_with_error_event_and_keeps_failed_bookkeeping(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        def failing(*_args, **_kwargs):
            raise RuntimeError("decision pipeline exploded")

        client.app.state.core.copilot = failing
        payload = {
            "request_id": "stream-error-request",
            "session_id": "stream-error",
            "message": "触发一次失败",
            "context": {"type": "page", "page": "overview"},
        }
        headers = {"Idempotency-Key": "stream-error-key"}
        response = client.post("/api/v1/copilot/stream", json=payload, headers=headers)

        # 流已开始 → 以 error 事件收尾，不裸断连、不补 done。
        assert response.status_code == 200
        assert "event: progress" in response.text
        assert "event: error" in response.text
        assert "decision pipeline exploded" in response.text
        assert "event: done" not in response.text

        # failed 落账语义不变：同 key 重试在流开始前判 409 冲突（HTTP 通道）。
        retry = client.post("/api/v1/copilot/stream", json=payload, headers=headers)
        assert retry.status_code == 409
        assert "previous request failed" in retry.json()["detail"]


def test_legacy_agent_endpoint_is_a_canonical_idempotent_alias(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        calls = 0

        def canonical(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "ok": True,
                "session_id": "agent-alias",
                "answer": "统一入口",
                "suggested_actions": [],
                "turn_decision": {"version": "turn_decision_v2", "effect": "answer"},
            }

        client.app.state.core.copilot = canonical
        payload = {
            "request_id": "agent-alias-request",
            "session_id": "agent-alias",
            "message": "查看岗位覆盖",
            "context": {"type": "job", "id": 111},
        }
        headers = {"Idempotency-Key": "agent-alias-key"}
        first = client.post("/api/v1/copilot/agent", json=payload, headers=headers)
        replay = client.post("/api/v1/copilot/agent", json=payload, headers=headers)

        assert first.status_code == 200
        assert replay.status_code == 200
        assert calls == 1
        assert replay.json()["receipt"]["idempotent_replay"] is True
        assert replay.json()["turn_decision"]["version"] == "turn_decision_v2"


def test_agent_mode_action_card_persistence_preserves_strategy_patch(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO agent_copilot_messages
                   (session_id,context_type,context_id,role,content,structured_json)
                   VALUES ('agent-persist','workflow','workflow-1','assistant','原回答',?)""",
                (json.dumps({"strategy_patch": {"workflow_id": "workflow-1", "changes": [{"field": "query"}]}}),),
            )
            conn.commit()
        finally:
            conn.close()
        card = {"risk_level": "R3", "context": {"type": "workflow", "id": "workflow-1"}}
        client.app.state.core._persist_agent_action_cards({
            "session_id": "agent-persist",
            "action_card": card,
            "action_cards": [card],
            "suggested_actions": [],
        })
        conn = sqlite3.connect(db_path)
        try:
            stored = json.loads(conn.execute(
                "SELECT structured_json FROM agent_copilot_messages WHERE session_id='agent-persist'"
            ).fetchone()[0])
        finally:
            conn.close()
        assert stored["strategy_patch"]["workflow_id"] == "workflow-1"
        assert stored["action_card"] == card


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def test_migrations_are_idempotent(db_path: Path) -> None:
    first = migrate(db_path, backup=False)
    second = migrate(db_path, backup=False)
    assert first["ok"] is True
    assert second["applied"] == []
    assert second["foreign_key_issues"] == []


def test_workflow_artifact_detail_download_and_path_boundary(db_path: Path) -> None:
    migrate(db_path, backup=False)
    artifact_root = db_path.parent / "asa_artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    safe_file = artifact_root / "matching-report.docx"
    safe_file.write_bytes(b"docx-content")

    conn = sqlite3.connect(db_path)
    try:
        goal_id, workflow_id = conn.execute(
            "SELECT goal_id,workflow_id FROM agent_workflows ORDER BY id LIMIT 1"
        ).fetchone()
        rows = [
            (
                "artifact_content_test", goal_id, workflow_id, None, "search_strategy", "多渠道寻访策略",
                "text/markdown", "", "# 结论\n\n可执行策略", "{}", "passed",
            ),
            (
                "artifact_file_test", goal_id, workflow_id, None, "matching_report", "人岗匹配报告",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                str(safe_file), "", "{}", "passed",
            ),
            (
                "artifact_external_path_test", goal_id, workflow_id, None, "matching_report", "越界文件",
                "text/plain", "/etc/hosts", "", "{}", "passed",
            ),
        ]
        conn.executemany(
            """
            INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        detail = client.get("/api/v1/artifacts/artifact_content_test")
        assert detail.status_code == 200
        artifact = detail.json()["artifact"]
        assert artifact["content"] == "# 结论\n\n可执行策略"
        assert artifact["downloadable"] is True
        assert artifact["download_kind"] == "content"
        assert "file_path" not in artifact

        content_download = client.get("/api/v1/artifacts/artifact_content_test/file")
        assert content_download.status_code == 200
        assert content_download.content.decode("utf-8") == "# 结论\n\n可执行策略"
        assert "attachment" in content_download.headers["content-disposition"]

        file_download = client.get("/api/v1/artifacts/artifact_file_test/file")
        assert file_download.status_code == 200
        assert file_download.content == b"docx-content"

        blocked = client.get("/api/v1/artifacts/artifact_external_path_test")
        assert blocked.status_code == 200
        assert blocked.json()["artifact"]["downloadable"] is False
        assert blocked.json()["artifact"]["download_url"] == ""
        assert client.get("/api/v1/artifacts/artifact_external_path_test/file").status_code == 404

        workflow = client.get(f"/api/v1/workflows/{workflow_id}")
        assert workflow.status_code == 200
        listed = {item["artifact_id"]: item for item in workflow.json()["artifacts"]}
        assert listed["artifact_content_test"]["has_content"] is True
        assert "content" not in listed["artifact_content_test"]
        assert "file_path" not in listed["artifact_file_test"]

        denied = client.get(
            "/api/v1/artifacts/artifact_content_test",
            headers={"Origin": "https://example.invalid"},
        )
        assert denied.status_code == 403


def test_migration_recovers_abandoned_idempotency_processing_records(db_path: Path) -> None:
    migrate(db_path, backup=False)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO api_idempotency
                (idempotency_key,operation,request_id,request_hash,status,created_at)
            VALUES ('abandoned-key','candidate.commit','abandoned-request','hash','processing',datetime('now','localtime','-10 minutes'))
            """
        )
        conn.commit()
    finally:
        conn.close()

    migrate(db_path, backup=False)
    conn = sqlite3.connect(db_path)
    try:
        recovered = conn.execute(
            "SELECT status,response_status,error_json FROM api_idempotency WHERE idempotency_key='abandoned-key'"
        ).fetchone()
        audit = conn.execute(
            "SELECT result FROM audit_events WHERE request_id='abandoned-request' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert recovered is not None
    assert recovered[0] == "failed"
    assert recovered[1] == 500
    assert json.loads(recovered[2])["outcome"] == "unknown"
    assert audit == ("failed",)


def test_core_v1_proposal_card_preflight_decision_and_audit(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        agent = client.app.state.core.agent_service
        conn = sqlite3.connect(db_path)
        try:
            candidate_id = conn.execute(
                """
                SELECT id FROM job_candidates
                WHERE COALESCE(clean_stage,'') NOT LIKE '%初筛不通过%'
                  AND lower(COALESCE(raw_status,'')) NOT IN ('screen_rejected','rejected','stopped','closed')
                ORDER BY id LIMIT 1
                """
            ).fetchone()[0]
        finally:
            conn.close()
        context = build_candidate_context(db_path, candidate_id)
        snapshot_hash = agent._snapshot_key(context)
        proposal_id = f"core_proposal_{uuid.uuid4().hex[:10]}"
        dedupe_key = f"core-proposal-{uuid.uuid4().hex}"
        request = {
            "job_candidate_id": candidate_id,
            "task_type": "agent_verification",
            "reason": "核验当前关键证据",
            "priority": 2,
            "write": True,
        }
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_action_proposals
                (proposal_id,job_candidate_id,snapshot_hash,dedupe_key,action_type,risk_level,title,rationale,request_json,expires_at)
                VALUES (?,?,?,?,?,'R1',?,?,?,datetime('now','+1 day'))
                """,
                (
                    proposal_id, candidate_id, snapshot_hash, dedupe_key, "create_task",
                    "创建内部核验任务", "需要人工补充当前证据", json.dumps(request, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        listed = client.get("/api/v1/agent/proposals?status=pending").json()
        card = next(item["action_card"] for item in listed["proposals"] if item["proposal_id"] == proposal_id)
        assert card["proposal_id"] == proposal_id
        assert card["capability_id"] == "verification_plan"
        assert card["risk_level"] == "R1"
        assert card["next_actions"][0]["type"] == "preflight"
        preflight = client.post(
            f"/api/v1/agent/proposals/{proposal_id}/preflight",
            json={"request_id": "core-proposal-preflight"},
        )
        assert preflight.status_code == 200
        token = preflight.json()["confirmation_token"]
        decision = client.post(
            f"/api/v1/agent/proposals/{proposal_id}/decision",
            headers={"Idempotency-Key": "core-proposal-decision"},
            json={"request_id": "core-proposal-decision", "confirmation_token": token, "decision": "approve"},
        )
        assert decision.status_code == 200
        assert decision.json()["status"] == "executed"
        assert decision.json()["post_check"]["status"] == "verified"
        assert decision.json()["receipt"]["idempotent_replay"] is False
        conn = sqlite3.connect(db_path)
        try:
            proposal = conn.execute(
                "SELECT status,action_id FROM agent_action_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            action = conn.execute("SELECT action_type,status FROM agent_actions WHERE idempotency_key=?", (dedupe_key,)).fetchone()
        finally:
            conn.close()
        assert proposal[0] == "executed"
        assert proposal[1] is not None
        assert action == ("create_task", "executed")


def test_agent_action_metrics_expose_the_seven_day_action_card_loop(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        agent = client.app.state.core.agent_service
        conn = sqlite3.connect(db_path)
        try:
            candidate_id = conn.execute("SELECT id FROM job_candidates ORDER BY id LIMIT 1").fetchone()[0]
            snapshot_hash = agent._snapshot_key(build_candidate_context(db_path, candidate_id))
            for status in ("pending", "rejected", "executed", "failed"):
                suffix = uuid.uuid4().hex[:10]
                conn.execute(
                    """INSERT INTO agent_action_proposals
                       (proposal_id,job_candidate_id,snapshot_hash,dedupe_key,action_type,risk_level,title,status,created_at,updated_at,reviewed_at)
                       VALUES (?,?,?,?,?,'R1','指标测试',?,datetime('now','localtime'),datetime('now','localtime'),datetime('now','localtime'))""",
                    (f"metric_{status}_{suffix}", candidate_id, snapshot_hash, f"metric-{status}-{suffix}", "create_task", status),
                )
            conn.commit()
        finally:
            conn.close()
        response = client.get("/api/v1/agent/metrics?days=7")
        assert response.status_code == 200
        metrics = response.json()["metrics"]
        assert metrics["action_cards_generated"] >= 4
        assert metrics["confirmed"] >= 2
        assert metrics["rejected"] >= 1
        assert metrics["executed"] >= 1
        assert metrics["failed"] >= 1
        assert set(metrics) >= {"confirmation_rate", "rejection_rate", "execution_failure_rate", "needs_clarification", "r3_approvals"}


def test_read_contract_uses_real_v3_data(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        assert client.get("/api/v1/health").status_code == 200
        jobs = client.get("/api/v1/jobs?limit=5").json()
        candidates = client.get("/api/v1/candidates?limit=5").json()
        assert jobs["total"] >= 100
        assert candidates["total"] >= 100
        job_detail = client.get(f"/api/v1/jobs/{jobs['items'][0]['id']}").json()["job"]
        assert job_detail["id"] == jobs["items"][0]["id"]
        assert "position" in job_detail
        assert "profile" in job_detail
        assert "funnel" in job_detail
        assert "stages" in job_detail
        assert "candidates" in job_detail
        assert "search_experiments" in job_detail
        assert "events" in job_detail
        assert "followups" in job_detail
        detail = client.get(f"/api/v1/candidates/{candidates['items'][0]['id']}").json()["candidate"]
        assert "resume" in detail
        assert "source_links" in detail
        assert "job_relations" in detail
        assert "events" in detail


def test_workbench_ui_is_app_only(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        assert client.get("/workbench").status_code == 403
        assert client.get("/asa-app").status_code == 403
        assert client.get("/asa-app/").status_code == 403
        response = client.get("/asa-app", headers={"User-Agent": "ASAApp/0.2.16"})
        assert response.status_code in {200, 503}
        slash_response = client.get("/asa-app/", headers={"User-Agent": "ASAApp/0.2.16"})
        assert slash_response.status_code in {200, 503}
        assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://aihpahceageafhjhedhmeikhcfbfoffn",
        "chrome-extension://cecifklpjckkbclegnmapegnedelapjh",
    ],
)
def test_candidate_extensions_can_preflight_context_bridge(origin: str, db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.options(
            "/api/asa/floating/context",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_app_origin_tracks_the_configured_local_core_port(db_path: Path) -> None:
    origin = "http://127.0.0.1:8876"
    with TestClient(create_app(db_path=db_path, port=8876, start_legacy=False)) as client:
        response = client.options(
            "/api/asa/floating/context",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin


def test_context_bridge_rejects_lookalike_origins(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.options(
            "/api/asa/floating/context",
            headers={
                "Origin": "https://h.liepin.com.evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("origin", ["https://h.liepin.com", "https://headhunt.x-saas.com.cn"])
def test_recruiting_pages_cannot_call_local_api_directly(origin: str, db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.post(
            "/api/v1/copilot/events",
            headers={"Origin": origin},
            json={"request_id": "blocked-browser-write", "session_id": "blocked", "event": "test"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "browser origin is not authorized for the local ASA API"


def test_dashboard_and_jobs_exclude_stopped_and_empty_left_join_rows(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        dashboard = client.get("/api/v1/dashboard").json()
        first_page = client.get("/api/v1/candidates?limit=200").json()
        candidates = list(first_page["items"])
        while len(candidates) < first_page["total"]:
            page = client.get(
                "/api/v1/candidates",
                params={"limit": 200, "offset": len(candidates)},
            ).json()
            candidates.extend(page["items"])
        expected = sum(
            "初筛不通过" not in (item.get("clean_stage") or "")
            and "停止" not in (item.get("clean_stage") or "")
            and (item.get("raw_status") or "").lower()
            not in {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}
            for item in candidates
        )
        assert dashboard["counts"]["pending_candidates"] == expected
        jobs = client.get("/api/v1/jobs?limit=200").json()["items"]
        assert all(item["active_candidate_count"] == 0 for item in jobs if item["candidate_count"] == 0)
        assert all(item.get("lifecycle_stage") != "archived" and item.get("status") != "只读快照" for item in jobs)


def test_stopped_candidate_cannot_be_reactivated_by_preflight(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        candidates = []
        first_page = client.get("/api/v1/candidates?limit=200").json()
        candidates.extend(first_page["items"])
        while len(candidates) < first_page["total"]:
            page = client.get(
                "/api/v1/candidates",
                params={"limit": 200, "offset": len(candidates)},
            ).json()
            candidates.extend(page["items"])
        stopped = next(
            item
            for item in candidates
            if "初筛不通过" in (item.get("clean_stage") or "")
        )
        detail = client.get(f"/api/v1/candidates/{stopped['id']}").json()["candidate"]
        assert detail["is_stopped"] is True
        response = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "stopped-test", "candidate_id": stopped["id"], "action": "advance"},
        )
        assert response.status_code == 409


def test_candidate_event_source_urls_are_available_in_detail(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        detail = client.get("/api/v1/candidates/549").json()["candidate"]
        urls = [item.get("source_url") for item in detail["source_links"] if item.get("source_url")]
        assert any("liepin.com/resume/" in url for url in urls)


def test_candidate_detail_lists_report_artifact_versions_without_internal_paths(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        candidate_id = int(conn.execute("SELECT id FROM job_candidates ORDER BY id LIMIT 1").fetchone()[0])
        metadata = json.dumps({"job_candidate_id": candidate_id}, ensure_ascii=False)
        conn.executemany(
            """
            INSERT INTO agent_artifacts
                (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                ("report-version-1", "goal-report", "workflow-report", None, "recommendation_report", "嘉驰推荐报告 v1", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "/private/report-v1.docx", "secret-v1", metadata, "passed", "2026-08-04 10:00:00"),
                ("report-version-2", "goal-report", "workflow-report", None, "recommendation_report", "嘉驰推荐报告 v2", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "/private/report-v2.docx", "secret-v2", metadata, "passed", "2026-08-04 11:00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get(f"/api/v1/candidates/{candidate_id}")
        assert response.status_code == 200
        reports = response.json()["candidate"]["report_artifacts"]
        selected = [item for item in reports if item["artifact_id"].startswith("report-version-")]
        assert [item["artifact_id"] for item in selected] == ["report-version-2", "report-version-1"]
        assert [item["version"] for item in selected] == [2, 1]
        assert all("file_path" not in item and "content" not in item for item in selected)


def test_candidate_detail_falls_back_to_sourcing_card_profile(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=563").fetchone()[0]
    conn.execute("DELETE FROM source_profiles WHERE person_id=?", (person_id,))
    conn.execute(
        "DELETE FROM entity_source_links WHERE canonical_type='person' AND canonical_id=?",
        (str(person_id),),
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        detail = client.get("/api/v1/candidates/563").json()["candidate"]
        assert "矽力杰" in detail["resume"]["summary"]
        assert "区域销售经理" in detail["resume"]["full_text"]


def test_migration_backfills_sourcing_card_source_profiles(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=563").fetchone()[0]
    conn.execute("DELETE FROM source_profiles WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM schema_migrations WHERE version=4")
    conn.commit()
    conn.close()

    result = migrate(db_path, backup=False)

    assert 4 in result["applied"]
    conn = sqlite3.connect(db_path)
    profile = conn.execute(
        "SELECT source_type,raw_json FROM source_profiles WHERE person_id=? ORDER BY id DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    conn.close()
    assert profile[0] == "liepin"
    assert "矽力杰" in profile[1]


def test_explicit_copilot_review_failure_updates_candidate_and_audit(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stop-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-review-stop-test",
                "message": "这个人选复核不通过",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == "stop"
        assert body["candidate_action"]["stage"] == "H5 最近寻访/初筛不通过"
        assert "已同步到 ASA" in body["answer"]
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["is_stopped"] is True
        assert detail["clean_stage"] == "H5 最近寻访/初筛不通过"

    conn = sqlite3.connect(db_path)
    event = conn.execute(
        "SELECT event_status,summary FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    audit = conn.execute(
        "SELECT operation,surface,result FROM audit_events WHERE target_type='job_candidate' AND target_id='558' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    feedback = conn.execute(
        "SELECT signal_type,weight FROM agent_sourcing_feedback WHERE job_candidate_id=558 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert event[0] == "stop"
    assert "复核不通过" in event[1]
    assert audit == ("candidate.commit", "asa_copilot", "success")
    assert feedback == ("stopped", -2.0)


@pytest.mark.parametrize(
    ("message", "action", "expected_stage", "expected_status", "signal_type", "weight"),
    [
        ("这个人选复核通过", "advance", "S2 复核通过/待联系", "new", "review_pass", 1.0),
        ("这个人选已联系", "contact", "S3 已联系/待回复", "contacted", "contacted", 2.0),
        ("这个人选已推荐给客户", "recommend", "S7 已推荐客户/待反馈", "recommended", "recommended", 3.0),
    ],
)
def test_explicit_copilot_candidate_actions_write_real_business_state(
    db_path: Path,
    message: str,
    action: str,
    expected_stage: str,
    expected_status: str,
    signal_type: str,
    weight: float,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted',raw_stage='S1 新增寻访/待复核' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='new',notes='原始寻访｜query=电气/硬件' WHERE id=1176")
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type IN ('resume_review_completed','candidate_contact_update','candidate_recommended')")
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-{action}-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": f"floating-{action}-test",
                "message": message,
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == action
        assert body["candidate_action"]["stage"] == expected_stage
        assert "已同步到 ASA" in body["answer"]
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == expected_stage

    conn = sqlite3.connect(db_path)
    candidate_status, candidate_notes = conn.execute("SELECT status,notes FROM candidates WHERE id=1176").fetchone()
    feedback = conn.execute(
        "SELECT signal_type,weight FROM agent_sourcing_feedback WHERE job_candidate_id=558 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored_answer = conn.execute(
        "SELECT content FROM agent_copilot_messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
        (f"floating-{action}-test",),
    ).fetchone()[0]
    conn.close()
    assert candidate_status == expected_status
    assert "query=电气/硬件" in candidate_notes
    assert "ASA Copilot 指令" in candidate_notes
    assert feedback == (signal_type, weight)
    assert "已同步到 ASA" in stored_answer


def test_copilot_read_no_reply_updates_note_and_timeline_without_reconfirmation(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S3 已联系/待回复',flow_bucket='联系推进',raw_status='contacted' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='contacted',notes='原始寻访记录' WHERE id=1176")
    conn.execute(
        "DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update' AND event_status='read_no_reply'"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-read-no-reply-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-read-no-reply-test",
                "message": "这个人选合适，但是发消息已读不回。这个情况你可以在人选备注页记录下",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_update"]["update_type"] == "read_no_reply"
        assert body["candidate_update"]["stage"] == "S3 已联系/待回复"
        assert "已更新" in body["answer"]
        assert "已读未回复" in body["answer"]
        assert "请确认" not in body["answer"]
        assert "打开人选备注" not in body["answer"]
        assert body["suggested_actions"] == []

    conn = sqlite3.connect(db_path)
    candidate = conn.execute("SELECT status,notes FROM candidates WHERE id=1176").fetchone()
    relation = conn.execute("SELECT clean_stage,raw_status FROM job_candidates WHERE id=558").fetchone()
    event = conn.execute(
        "SELECT event_status,summary FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    audit = conn.execute(
        "SELECT operation,surface,result FROM audit_events WHERE target_type='job_candidate' AND target_id='558' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    stored = conn.execute(
        "SELECT content,structured_json FROM agent_copilot_messages WHERE session_id='floating-read-no-reply-test' AND role='assistant' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert candidate[0] == "contacted"
    assert "已读未回复" in candidate[1]
    assert relation == ("S3 已联系/待回复", "contacted")
    assert event[0] == "read_no_reply"
    assert "已读未回复" in event[1]
    assert audit == ("candidate.note", "asa_copilot", "success")
    assert "请确认" not in stored[0]
    assert json.loads(stored[1])["suggested_actions"] == []


def test_copilot_read_no_reply_note_never_downgrades_later_stage(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S7 已推荐客户/待反馈',flow_bucket='客户推荐',raw_status='recommended' WHERE id=558"
    )
    conn.execute("UPDATE candidates SET status='recommended' WHERE id=1176")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-read-no-reply-later-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-read-no-reply-later-stage-test",
                "message": "记录一下，这个人选消息已读未回",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        assert response.json()["candidate_update"]["stage"] == "S7 已推荐客户/待反馈"

    conn = sqlite3.connect(db_path)
    relation = conn.execute("SELECT clean_stage,raw_status FROM job_candidates WHERE id=558").fetchone()
    status = conn.execute("SELECT status FROM candidates WHERE id=1176").fetchone()[0]
    conn.close()
    assert relation == ("S7 已推荐客户/待反馈", "recommended")
    assert status == "recommended"


def test_candidate_action_never_downgrades_a_later_stage(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S7 已推荐客户/待反馈',flow_bucket='客户推荐',raw_status='recommended' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update'")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "no-downgrade-preflight", "candidate_id": 558, "action": "contact"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "no-downgrade-commit"},
            json={
                "request_id": "no-downgrade-commit",
                "candidate_id": 558,
                "action": "contact",
                "preflight_token": preflight["token"],
            },
        )
        assert response.status_code == 200
        assert response.json()["already_applied"] is True
        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == "S7 已推荐客户/待反馈"

    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM candidate_events WHERE job_candidate_id=558 AND event_type='candidate_contact_update'"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_xsaas_review_pass_uses_x2_waiting_manual_contact(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='X1 X-SaaS新增/待复核',flow_bucket='待复核',raw_status='xsaas_search_shortlisted' WHERE id=558"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "xsaas-pass-preflight", "candidate_id": 558, "action": "advance"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "xsaas-pass-commit"},
            json={
                "request_id": "xsaas-pass-commit",
                "candidate_id": 558,
                "action": "advance",
                "preflight_token": preflight["token"],
            },
        )
        assert response.status_code == 200
        assert response.json()["stage"] == "X2 X-SaaS复核通过/待人工联系"

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT clean_stage,flow_bucket,raw_status FROM job_candidates WHERE id=558"
    ).fetchone()
    conn.close()
    assert row == (
        "X2 X-SaaS复核通过/待人工联系",
        "待人工联系/转猎聘或微信",
        "xsaas_review_continue",
    )


def test_copilot_cannot_reactivate_stopped_candidate(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='H5 最近寻访/初筛不通过',flow_bucket='最近寻访',raw_status='screen_rejected' WHERE id=558"
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stopped-contact-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-stopped-contact-test",
                "message": "这个人选已联系",
                "context": {"type": "candidate", "id": 558, "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["write_blocked"] is True
        assert "未写入 ASA" in body["answer"]
        assert client.get("/api/v1/candidates/558").json()["candidate"]["is_stopped"] is True


def test_copilot_review_failure_without_candidate_context_never_claims_write(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-stop-unresolved-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "session_id": "floating-review-stop-unresolved-test",
                "message": "这个人选复核不通过",
                "context": {"type": "global", "source": "asa_floating"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["write_blocked"] is True
        assert "尚未写入 ASA" in body["answer"]
        assert "已标记" not in body["answer"]


def test_floating_compat_copilot_review_failure_uses_core_write_path(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
    )
    conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
    conn.commit()
    conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=True)) as client:
        response = client.post(
            "/api/agent/copilot",
            json={
                "session_id": "floating-compat-review-stop-test",
                "message": "这个人选复核不通过",
                "context": {
                    "type": "candidate",
                    "id": 558,
                    "source": "asa_floating",
                    "display_mode": "floating_compact",
                },
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["candidate_action"]["action"] == "stop"
        assert "已同步到 ASA" in body["answer"]
        assert client.get("/api/v1/candidates/558").json()["candidate"]["is_stopped"] is True


def test_legacy_runtime_reuses_core_agent_service(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "a_system_agent.workflow.WorkflowEngine.recover_external_continuations",
        lambda self: 0,
    )
    app = create_app(db_path=db_path, start_legacy=True)
    try:
        assert app.state.legacy.state.agent_service is app.state.core.agent_service
    finally:
        app.state.legacy.close()
        app.state.core.agent_service.close()


def test_candidate_detail_resolves_source_url_written_after_startup(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        person_id = conn.execute("SELECT person_id FROM job_candidates WHERE id=558").fetchone()[0]
        conn.execute(
            "DELETE FROM entity_source_links WHERE canonical_type='person' AND canonical_id=?",
            (str(person_id),),
        )
        resume_url = "https://h.liepin.com/resume/showresumedetail/?res_id_encode=late-event"
        conn.execute(
            """
            INSERT INTO candidate_events
                (job_candidate_id,person_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (558,?,'search_shortlisted','pending_review',datetime('now','localtime'),
                    'late source link',?,'liepin_search',?)
            """,
            (person_id, '{"resume_url":"' + resume_url + '"}', resume_url),
        )
        conn.commit()
        conn.close()

        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        urls = [item.get("source_url") for item in detail["source_links"]]
        assert resume_url in urls


def test_copilot_candidate_synonym_creates_sourcing_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-candidate-synonym-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"]
        assert all(item["type"] != "start_workflow" for item in body["suggested_actions"])
        assert any(item["type"] == "start_workflow" for item in body["action_card"]["next_actions"])
        assert any(step["capability_id"] == "multi_channel_sourcing" for step in body["plan_summary"])


def test_workflow_archive_hides_current_card_but_preserves_detail(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"archive-create-{uuid.uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        ).json()
        workflow_id = created["workflow_id"]
        assert any(item["workflow_id"] == workflow_id for item in client.get("/api/v1/dashboard").json()["workflows"])

        archive_id = f"archive-{uuid.uuid4().hex[:8]}"
        response = client.post(
            f"/api/v1/workflows/{workflow_id}/archive",
            headers={"Idempotency-Key": archive_id},
            json={"request_id": archive_id},
        )
        assert response.status_code == 200, response.json()
        assert response.json()["workflow"]["archived_at"]
        assert all(item["workflow_id"] != workflow_id for item in client.get("/api/v1/dashboard").json()["workflows"])
        detail = client.get(f"/api/v1/workflows/{workflow_id}")
        assert detail.status_code == 200
        assert detail.json()["workflow"]["archived_at"]


def test_workflow_pause_and_resume_actions_use_the_core_route(db_path: Path) -> None:
    app = create_app(db_path=db_path, start_legacy=False)
    created = app.state.core.agent_service.create_goal(
        "给士兰微技术市场经理再找些候选人",
        {"type": "job", "id": 111},
    )
    workflow_id = created["workflow"]["workflow_id"]
    step_id = next(step["id"] for step in created["steps"] if step["capability_id"] == "multi_channel_sourcing")
    request = {"client": "士兰微", "job": "技术市场经理", "query_plan_v1": {"plan_hash": "test"}}
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE agent_workflows SET status='waiting_external' WHERE workflow_id=?", (workflow_id,))
    conn.execute("UPDATE agent_goals SET status='waiting_external' WHERE goal_id=?", (created["goal"]["goal_id"],))
    conn.execute(
        "UPDATE agent_workflow_steps SET status='waiting_external',recovery_json=? WHERE id=?",
        (json.dumps({"retry_mode": "sourcing_continuation", "request": request}), step_id),
    )
    conn.commit()
    conn.close()
    scheduled: list[tuple[int, str, dict]] = []
    app.state.core.agent_service.schedule_external_workflow_step = (  # type: ignore[method-assign]
        lambda resumed_step_id, capability_id, resumed_request: scheduled.append((resumed_step_id, capability_id, resumed_request))
    )

    with TestClient(app) as client:
        paused = client.post(
            f"/api/v1/workflows/{workflow_id}/pause",
            headers={"Idempotency-Key": "workflow-pause-route"},
            json={"request_id": "workflow-pause-route", "note": "暂停"},
        )
        resumed = client.post(
            f"/api/v1/workflows/{workflow_id}/resume",
            headers={"Idempotency-Key": "workflow-resume-route"},
            json={"request_id": "workflow-resume-route", "note": "继续"},
        )

    assert paused.status_code == 200, paused.json()
    assert paused.json()["workflow"]["status"] == "paused"
    assert resumed.status_code == 200, resumed.json()
    assert resumed.json()["workflow"]["status"] == "waiting_external"
    assert scheduled == [(step_id, "multi_channel_sourcing", request)]


def test_dashboard_workflows_expose_business_outcome_matching_summary(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"dashboard-outcome-{uuid.uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "给士兰微技术市场经理再找些候选人",
                "context": {"type": "job", "id": 111},
            },
        )
        workflow_id = created.json()["workflow_id"]
        # 业务终态生产上由引擎 _finish 写入；此处直接写临时库验证透传链
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE agent_workflows SET business_outcome='completed_needs_review' WHERE workflow_id=?",
            (workflow_id,),
        )
        conn.commit()
        conn.close()

        workflows = client.get("/api/v1/dashboard").json()["workflows"]
        assert all("business_outcome" in item for item in workflows)
        item = next(i for i in workflows if i["workflow_id"] == workflow_id)
        assert item["business_outcome"] == "completed_needs_review"
        summary = client.get(f"/api/v1/workflows/{workflow_id}/summary").json()
        assert item["business_outcome"] == summary["business_outcome"]


def test_copilot_job_split_creates_job_library_update_workflow(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        request_id = f"copilot-job-library-update-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/copilot/messages",
            headers={"Idempotency-Key": request_id},
            json={
                "request_id": request_id,
                "message": "把士兰微技术市场经理/总监分成 PC、服务器、ADAS 三个岗位",
                "context": {"type": "page", "page": "jobs"},
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["workflow_id"]
        assert all(item["type"] != "start_workflow" for item in body["suggested_actions"])
        assert any(item["type"] == "start_workflow" for item in body["action_card"]["next_actions"])
        assert any(step["capability_id"] == "job_library_update" for step in body["plan_summary"])


def test_job_library_update_uses_position_profiles_and_archives_legacy_split_job(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        runtime = client.app.state.core.agent_service.capability_runtime
        result = runtime.run_job_library_update(
            {"type": "page", "page": "jobs"},
            {
                "objective": "把士兰微技术市场经理/总监分成 PC、服务器、ADAS 三个岗位",
                "skip_sync": True,
            },
        )
        update = result["job_library_update"]
        titles = {item["title"] for item in update["changes"]}
        assert "技术市场经理/总监（PC电源）" in titles
        assert "技术市场经理/总监（服务器三次电源）" in titles
        assert "技术市场经理/总监（ADAS电源）" in titles
        assert update["archived_legacy"]

    conn = sqlite3.connect(db_path)
    try:
        job_rows = conn.execute(
            """
            SELECT j.title,j.status,j.lifecycle_stage,m.priority
            FROM jobs j JOIN clients c ON c.id=j.client_id
            LEFT JOIN job_pipeline_metrics m ON m.job_id=j.id
            WHERE c.name='士兰微' AND j.title IN (
              '技术市场经理/总监（PC电源）',
              '技术市场经理/总监（服务器三次电源）',
              '技术市场经理/总监（ADAS电源）'
            )
            """
        ).fetchall()
        assert len(job_rows) == 3
        assert all("P0-最急" in (row[3] or "") for row in job_rows)
        legacy = conn.execute(
            """
            SELECT status,lifecycle_stage FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE c.name='士兰微' AND j.title LIKE '%服务器或PC市场%'
            """
        ).fetchall()
        assert legacy
        assert all(row[1] == "archived" for row in legacy)
    finally:
        conn.close()


def test_candidate_write_is_idempotent_and_preflight_is_single_use(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        candidate_id = next(
            item["id"]
            for item in client.get("/api/v1/candidates?limit=200").json()["items"]
            if "初筛不通过" not in (item.get("clean_stage") or "")
            and (item.get("raw_status") or "").lower() not in {"screen_rejected", "xsaas_review_stop"}
        )
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "test-preflight", "candidate_id": candidate_id, "action": "review"},
        ).json()
        body = {
            "request_id": "test-commit",
            "candidate_id": candidate_id,
            "action": "review",
            "preflight_token": preflight["token"],
        }
        headers = {"Idempotency-Key": f"review-{candidate_id}"}
        first = client.post("/api/v1/candidate-actions/commit", json=body, headers=headers)
        replay = client.post("/api/v1/candidate-actions/commit", json=body, headers=headers)
        reused = client.post(
            "/api/v1/candidate-actions/commit",
            json={**body, "request_id": "test-reused"},
            headers={"Idempotency-Key": f"review-{candidate_id}-reused"},
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["receipt"]["idempotent_replay"] is True
        assert reused.status_code == 409


def test_candidate_score_review_records_event_without_regressing_stage(db_path: Path) -> None:
    candidate_id = 558
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE job_candidates SET clean_stage='S7 已推荐客户/待反馈',flow_bucket='客户推荐',raw_status='recommended' WHERE id=?",
            (candidate_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "score-review-preflight", "candidate_id": candidate_id, "action": "review"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "score-review-commit"},
            json={
                "request_id": "score-review-commit",
                "candidate_id": candidate_id,
                "action": "review",
                "preflight_token": preflight["token"],
                "note": "【评分复核】结论：尺太严",
            },
        )
        detail = client.get(f"/api/v1/candidates/{candidate_id}").json()["candidate"]

    assert response.status_code == 200
    assert response.json()["stage"] == "S7 已推荐客户/待反馈"
    assert detail["clean_stage"] == "S7 已推荐客户/待反馈"
    assert any(
        item["event_type"] == "candidate_review_requested"
        and "尺太严" in (item.get("summary") or "")
        for item in detail["events"]
    )


def test_failed_idempotent_action_is_audited_and_never_left_processing(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        core = client.app.state.core
        attempts = 0

        def fail_after_claim() -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("simulated action failure")

        kwargs = {
            "operation": "test.failure",
            "request_id": "test-failure-request",
            "idempotency_key": "test-failure-key",
            "payload": {"value": 1},
            "target_type": "test",
            "target_id": "1",
            "action": fail_after_claim,
        }
        with pytest.raises(RuntimeError, match="simulated action failure"):
            core.execute_idempotent(**kwargs)
        with pytest.raises(ValueError, match="previous request failed"):
            core.execute_idempotent(**kwargs)

        conn = sqlite3.connect(db_path)
        try:
            record = conn.execute(
                "SELECT status,response_status,error_json FROM api_idempotency WHERE idempotency_key='test-failure-key'"
            ).fetchone()
            audit = conn.execute(
                "SELECT result FROM audit_events WHERE operation='test.failure' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert attempts == 1
        assert record is not None
        assert record[0] == "failed"
        assert record[1] == 500
        assert json.loads(record[2])["type"] == "RuntimeError"
        assert audit == ("failed",)


def test_candidate_learning_failure_preserves_committed_business_write(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
            )
            conn.commit()
        finally:
            conn.close()

        def fail_learning(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("simulated learning failure")

        monkeypatch.setattr(
            client.app.state.core.agent_service,
            "record_sourcing_business_signal",
            fail_learning,
        )
        preflight = client.post(
            "/api/v1/candidate-actions/preflight",
            json={"request_id": "learning-failure-preflight", "candidate_id": 558, "action": "advance"},
        ).json()
        response = client.post(
            "/api/v1/candidate-actions/commit",
            headers={"Idempotency-Key": "learning-failure-commit"},
            json={
                "request_id": "learning-failure-commit",
                "candidate_id": 558,
                "action": "advance",
                "preflight_token": preflight["token"],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["stage"] == "S2 复核通过/待联系"
        assert payload["sourcing_learning"]["recorded"] is False
        assert payload["sourcing_learning"]["error_type"] == "RuntimeError"

        conn = sqlite3.connect(db_path)
        try:
            stage = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=558").fetchone()[0]
            idem_status = conn.execute(
                "SELECT status FROM api_idempotency WHERE idempotency_key='learning-failure-commit'"
            ).fetchone()[0]
            learning_audit = conn.execute(
                "SELECT result FROM audit_events WHERE operation='candidate.learning_backflow' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert stage == "S2 复核通过/待联系"
        assert idem_status == "completed"
        assert learning_audit == ("failed",)


def test_user_business_actions_update_sourcing_keyword_memory(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE job_candidates SET clean_stage='S1 新增寻访/待复核',flow_bucket='待复核',raw_status='search_shortlisted' WHERE id=558"
        )
        conn.execute("DELETE FROM candidate_events WHERE job_candidate_id=558 AND event_type='resume_review_completed'")
        conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
        conn.execute("DELETE FROM agent_sourcing_attributions WHERE job_candidate_id=558")
        conn.execute("DELETE FROM agent_memories WHERE source_type='sourcing_performance'")
        conn.commit()
        conn.close()

        for action in ("advance", "contact", "recommend"):
            preflight = client.post(
                "/api/v1/candidate-actions/preflight",
                json={"request_id": f"preflight-{action}", "candidate_id": 558, "action": action},
            ).json()
            request_id = f"commit-{action}-{uuid.uuid4().hex[:6]}"
            response = client.post(
                "/api/v1/candidate-actions/commit",
                headers={"Idempotency-Key": request_id},
                json={
                    "request_id": request_id,
                    "candidate_id": 558,
                    "action": action,
                    "preflight_token": preflight["token"],
                },
            )
            assert response.status_code == 200
            assert response.json()["sourcing_learning"]["recorded"] is True

        detail = client.get("/api/v1/candidates/558").json()["candidate"]
        assert detail["clean_stage"] == "S7 已推荐客户/待反馈"
        assert detail["sourcing_attributions"][0]["source_query"] == "电气/硬件"
        assert detail["sourcing_attributions"][0]["learning_score"] == 6.0
        assert detail["sourcing_attributions"][0]["review_pass_count"] == 1
        assert detail["sourcing_attributions"][0]["contacted_count"] == 1
        assert detail["sourcing_attributions"][0]["recommended_count"] == 1

        agent = client.app.state.core.agent_service
        client_signal = agent.record_sourcing_business_signal(
            558, "client_rejected", actor_type="client", note="方向不符",
            source_type="client_feedback_event", source_id="test-client-feedback",
        )
        assert client_signal["recorded"] is True
        updated = client.get("/api/v1/candidates/558").json()["candidate"]["sourcing_attributions"][0]
        assert updated["learning_score"] == 3.0
        assert updated["client_rejected_count"] == 1

        conn = sqlite3.connect(db_path)
        memory = conn.execute(
            "SELECT content FROM agent_memories WHERE source_type='sourcing_performance'"
        ).fetchone()
        conn.close()
        assert memory is not None
        assert "联系 1" in memory[0]
        assert "推荐 1" in memory[0]
        assert "客户否决 1" in memory[0]


def test_client_feedback_updates_sourcing_learning(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM agent_sourcing_feedback WHERE job_candidate_id=558")
    conn.commit()
    conn.close()
    state = legacy.WorkbenchState(db_path, tmp_path, "127.0.0.1", 8765)
    state.run_refresh = lambda: {"ok": True}  # type: ignore[method-assign]
    monkeypatch.setattr(legacy, "refresh_a_system_workbench", lambda: {"ok": True})
    try:
        result = legacy.write_client_feedback(
            state,
            {
                "write": True,
                "candidate_id": 1176,
                "job_candidate_id": 558,
                "candidate_name": "黄**",
                "candidate_company": "奇瑞汽车股份有限公司",
                "client": "士兰微",
                "position": "技术市场经理（三次电源/服务器或PC市场）",
                "feedback_type": "approved",
                "feedback_detail": "客户认可技术背景",
            },
        )
        assert result["sourcing_learning"]["recorded"] is True
        assert result["sourcing_learning"]["signal_type"] == "client_approved"
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            """
            SELECT SUM(sf.weight),SUM(sf.signal_type='client_approved')
            FROM agent_sourcing_feedback sf
            WHERE sf.job_candidate_id=558
            """
        ).fetchone()
        conn.close()
        assert row == (4.0, 1)
    finally:
        state.close()
