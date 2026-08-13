"""ASA Copilot 策略同步二期回归守护：撤回闭环 + 埋点 + agent 路径 strategy_patch。

覆盖：
1. 撤回闭环——revise_workflow 在 supersede 前抓 undo 快照写入 workflow_revised 事件；
   revert_workflow_revision 单事务恢复 source 工作流/步骤/goal/审批、取消修订版、
   写 workflow_revision_reverted 事件，返回 source 的 get_workflow 结果。
2. 守卫——二次撤回（源工作流已非 superseded）、修订版已启动（非 planned/步骤非全
   pending）、无 undo 快照的旧修订数据，均抛 ValueError。
3. 埋点——record_copilot_event 写入 agent_copilot_events，payload JSON 完整。
4. agent 路径——copilot_agent（任务书中的 run_copilot_agent_turn 实际入口）同样产出
   strategy_patch：返回值 + 落库的 assistant_structured。

工作流链路用真实 AgentService + AgentDbCase fixture（同 test_a_system_agent_workflow
口径），因为 revise/revert 触及 goals/workflows/steps/approvals/events 全表；
埋点用例复用一期 test_copilot_strategy_patch 的 _PATCH_SCHEMA + _StubService/_StubLLM 桩。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.copilot_handler import record_copilot_event  # noqa: E402


# ---------------------------------------------------------------------------
# 一期同款桩：埋点用例只需要 _connect，不需要真实 AgentService
# ---------------------------------------------------------------------------


class _StubLLM:
    def __init__(self, patch):
        self._patch = patch
        self.calls = 0

    def extract_strategy_patch(self, payload):
        self.calls += 1
        return self._patch


class _StubService:
    def __init__(self, db_path: Path, llm) -> None:
        self._db_path = db_path
        self.llm = llm

    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


_PATCH_SCHEMA = """
CREATE TABLE agent_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT, title TEXT, objective TEXT,
    context_type TEXT, context_id INTEGER, context_json TEXT DEFAULT '{}'
);
CREATE TABLE agent_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT, goal_id TEXT, status TEXT, created_at TEXT
);
CREATE TABLE agent_workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT, capability_id TEXT, status TEXT, sequence INTEGER,
    output_json TEXT DEFAULT '{}'
);
CREATE TABLE agent_copilot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, context_type TEXT, context_id TEXT, role TEXT, content TEXT,
    structured_json TEXT DEFAULT '{}', created_at TEXT
);
"""


# ---------------------------------------------------------------------------
# 撤回闭环 + 守卫
# ---------------------------------------------------------------------------


class RevisionRevertTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))
        self.addCleanup(self.service.close)

    def wait_for(self, workflow_id: str, statuses: set[str], timeout: float = 5) -> dict:
        deadline = time.time() + timeout
        state = self.service.get_workflow(workflow_id)
        while time.time() < deadline and state["workflow"]["status"] not in statuses:
            time.sleep(0.03)
            state = self.service.get_workflow(workflow_id)
        return state

    def _create_waiting_workflow(self) -> dict:
        """推进到 waiting_approval：存在 pending 审批，undo 快照覆盖审批恢复分支。"""
        created = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        workflow_id = created["workflow"]["workflow_id"]
        self.service.start_workflow(workflow_id)
        waiting = self.wait_for(workflow_id, {"waiting_approval", "failed"})
        self.assertEqual(waiting["workflow"]["status"], "waiting_approval")
        return waiting

    def test_revert_restores_source_and_writes_event(self) -> None:
        before = self._create_waiting_workflow()
        original_id = before["workflow"]["workflow_id"]
        before_steps = {step["capability_id"]: step["status"] for step in before["steps"]}
        before_goal_status = before["goal"]["status"]
        pending_approval = next(item for item in before["approvals"] if item["status"] == "pending")

        revised = self.service.revise_workflow(
            original_id,
            "必须有精密设备量产经验；预研背景可看，但量产项目经验优先",
        )
        revised_id = revised["workflow"]["workflow_id"]
        superseded = self.service.get_workflow(original_id)
        assert superseded["workflow"]["status"] == "superseded"

        reverted = self.service.revert_workflow_revision(revised_id)

        # 返回 source 的 get_workflow 结果，状态/步骤/goal/审批全部恢复
        assert reverted["workflow"]["workflow_id"] == original_id
        assert reverted["workflow"]["status"] == before["workflow"]["status"]
        assert reverted["goal"]["status"] == before_goal_status
        after_steps = {step["capability_id"]: step["status"] for step in reverted["steps"]}
        assert after_steps == before_steps
        restored_approval = next(
            item for item in reverted["approvals"] if item["approval_id"] == pending_approval["approval_id"]
        )
        assert restored_approval["status"] == "pending"

        # 修订版已取消
        revised_state = self.service.get_workflow(revised_id)
        assert revised_state["workflow"]["status"] == "cancelled"
        assert revised_state["goal"]["status"] == "cancelled"
        assert all(step["status"] == "cancelled" for step in revised_state["steps"])

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 事件表双侧写入 workflow_revision_reverted
            rows = conn.execute(
                "SELECT workflow_id FROM agent_step_events WHERE event_type='workflow_revision_reverted'"
            ).fetchall()
            assert {original_id, revised_id} <= {row["workflow_id"] for row in rows}
            # revise 时已在 workflow_revised 事件里留下 undo 快照
            revised_event = conn.execute(
                "SELECT detail_json FROM agent_step_events WHERE workflow_id=? AND event_type='workflow_revised'",
                (revised_id,),
            ).fetchone()
            undo = json.loads(revised_event["detail_json"])["undo"]
            assert undo["source_workflow_id"] == original_id
            assert undo["source_status"] == before["workflow"]["status"]
            assert undo["source_goal_status"] == before_goal_status
            assert pending_approval["approval_id"] in undo["pending_approvals"]
        finally:
            conn.close()

    def test_second_revert_raises(self) -> None:
        before = self._create_waiting_workflow()
        revised = self.service.revise_workflow(before["workflow"]["workflow_id"], "量产项目经验优先")
        revised_id = revised["workflow"]["workflow_id"]
        self.service.revert_workflow_revision(revised_id)

        with self.assertRaises(ValueError) as ctx:
            self.service.revert_workflow_revision(revised_id)
        assert "撤回" in str(ctx.exception)

    def test_started_revision_cannot_revert(self) -> None:
        created = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        revised = self.service.revise_workflow(
            created["workflow"]["workflow_id"],
            "量产项目经验优先",
        )
        revised_id = revised["workflow"]["workflow_id"]
        # 模拟已启动：修订版非 planned 且步骤非全 pending
        conn = self.service._connect()
        try:
            conn.execute("UPDATE agent_workflows SET status='running' WHERE workflow_id=?", (revised_id,))
            conn.execute(
                "UPDATE agent_workflow_steps SET status='running' WHERE workflow_id=? AND sequence=1",
                (revised_id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(ValueError) as ctx:
            self.service.revert_workflow_revision(revised_id)
        assert "不能撤回" in str(ctx.exception)

    def test_legacy_revision_without_snapshot_cannot_revert(self) -> None:
        source = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        source_id = source["workflow"]["workflow_id"]
        legacy = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选；本轮寻访条件调整：量产经验优先",
            {"type": "job", "id": 10},
        )
        legacy_id = legacy["workflow"]["workflow_id"]
        legacy_goal_id = legacy["goal"]["goal_id"]
        # 手工造旧版修订关联：有 revision_of_workflow_id + source 已 superseded，
        # 但 workflow_revised 事件不带 undo 快照
        context = dict(legacy["goal"]["context"] or {})
        context.update({"revision_of_workflow_id": source_id, "revision_number": 1})
        conn = self.service._connect()
        try:
            conn.execute(
                "UPDATE agent_goals SET context_json=? WHERE goal_id=?",
                (json.dumps(context, ensure_ascii=False), legacy_goal_id),
            )
            conn.execute("UPDATE agent_workflows SET status='superseded' WHERE workflow_id=?", (source_id,))
            conn.execute("UPDATE agent_goals SET status='superseded' WHERE goal_id=?", (source["goal"]["goal_id"],))
            conn.execute(
                """
                INSERT INTO agent_step_events (workflow_id,step_id,event_type,status,summary,detail_json)
                VALUES (?,NULL,'workflow_revised','planned','由旧版修订生成',?)
                """,
                (legacy_id, json.dumps({"source_workflow_id": source_id, "revision_number": 1}, ensure_ascii=False)),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(ValueError) as ctx:
            self.service.revert_workflow_revision(legacy_id)
        assert "快照" in str(ctx.exception)


# ---------------------------------------------------------------------------
# 埋点：record_copilot_event → agent_copilot_events
# ---------------------------------------------------------------------------


class CopilotEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def test_record_copilot_event_persists_payload(self) -> None:
        db_path = self.tmp / "agent.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_PATCH_SCHEMA)
        conn.commit()
        conn.close()
        service = _StubService(db_path, _StubLLM(None))
        payload = {
            "workflow_id": "workflow_abc123",
            "revised_workflow_id": "workflow_def456",
            "applied": 2,
            "total": 3,
        }

        result = record_copilot_event(service, "sess-1", "strategy_patch_applied", payload)

        assert result == {"ok": True, "idempotent_replay": False}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT session_id,event,payload_json,created_at FROM agent_copilot_events"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == "sess-1"
        assert row["event"] == "strategy_patch_applied"
        assert json.loads(row["payload_json"]) == payload
        assert row["created_at"]

    def test_strategy_events_persist_patch_terminal_state_for_session_restore(self) -> None:
        db_path = self.tmp / "agent.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_PATCH_SCHEMA)
        conn.execute(
            """
            INSERT INTO agent_copilot_messages
            (session_id,context_type,context_id,role,content,structured_json)
            VALUES ('sess-1','workflow','workflow_abc123','assistant','策略建议',?)
            """,
            (json.dumps({"strategy_patch": {"workflow_id": "workflow_abc123", "changes": [{"type": "add_keyword", "value": "通信电源"}]}}),),
        )
        conn.commit()
        conn.close()
        service = _StubService(db_path, _StubLLM(None))

        record_copilot_event(service, "sess-1", "copilot_strategy_applied", {
            "workflow_id": "workflow_abc123", "revised_workflow_id": "workflow_def456",
        })
        record_copilot_event(service, "sess-1", "copilot_strategy_reverted", {
            "workflow_id": "workflow_def456", "restored_workflow_id": "workflow_abc123",
        })

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT structured_json FROM agent_copilot_messages WHERE session_id='sess-1'").fetchone()
        finally:
            conn.close()
        structured = json.loads(row["structured_json"])
        assert structured["strategy_patch_applied"] is True
        assert structured["strategy_patch_revised_workflow_id"] == "workflow_def456"
        assert structured["strategy_patch_reverted"] is True
        assert structured["strategy_patch_restored_workflow_id"] == "workflow_abc123"

    def test_strategy_apply_receipt_retry_is_idempotent_and_still_repairs_session_state(self) -> None:
        db_path = self.tmp / "agent.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_PATCH_SCHEMA)
        conn.execute(
            """
            INSERT INTO agent_copilot_messages
            (session_id,context_type,context_id,role,content,structured_json)
            VALUES ('sess-1','workflow','workflow_abc123','assistant','策略建议',?)
            """,
            (json.dumps({"strategy_patch": {"workflow_id": "workflow_abc123", "changes": []}}),),
        )
        conn.commit()
        conn.close()
        service = _StubService(db_path, _StubLLM(None))
        payload = {
            "workflow_id": "workflow_abc123", "revision": 3,
            "artifact_id": "artifact_3", "applied": 2,
        }

        first = record_copilot_event(service, "sess-1", "copilot_strategy_applied", payload)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE agent_copilot_messages SET structured_json=?", (json.dumps({"strategy_patch": {"workflow_id": "workflow_abc123", "changes": []}}),))
        conn.commit()
        conn.close()
        replay = record_copilot_event(service, "sess-1", "copilot_strategy_applied", payload)

        assert first == {"ok": True, "idempotent_replay": False}
        assert replay == {"ok": True, "idempotent_replay": True}
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_copilot_events WHERE session_id='sess-1' AND event='copilot_strategy_applied'"
            ).fetchone()[0]
            structured = json.loads(conn.execute(
                "SELECT structured_json FROM agent_copilot_messages WHERE session_id='sess-1'"
            ).fetchone()[0])
        finally:
            conn.close()
        assert count == 1
        assert structured["strategy_patch_applied"] is True
        assert structured["strategy_patch_revision"] == 3
        assert structured["strategy_patch_artifact_id"] == "artifact_3"


# ---------------------------------------------------------------------------
# agent 路径（copilot_agent）也产出 strategy_patch
# ---------------------------------------------------------------------------

_AGENT_ANSWER = "建议补充关键词「通信电源」「基站电源」，并扩展对标公司台达、维谛；也可以加过滤条件排除销售岗。"


class _AgentTurnLLM(FakeLLM):
    """copilot_agent 工具循环桩：首轮直接返回最终回答（无工具调用），patch 提取走 FakeLLM。"""

    def __init__(self, patch) -> None:
        super().__init__(fake_assessment(), strategy_patch=patch)
        self.tool_turns = 0

    def copilot_with_tools(self, payload, tools, messages=None, allow_tools=True):
        self.tool_turns += 1
        return {"content": _AGENT_ANSWER}


class CopilotAgentPatchTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.llm = _AgentTurnLLM({
            "changes": [
                {"type": "add_keyword", "value": "通信电源", "confidence": 0.9},
                {"type": "add_company", "value": "维谛", "confidence": 0.8},
            ]
        })
        self.service = AgentService(self.db_path, self.llm)
        self.addCleanup(self.service.close)

    def test_agent_turn_returns_strategy_patch_in_result_and_structured(self) -> None:
        created = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10},
        )
        workflow_id = created["workflow"]["workflow_id"]

        result = self.service.copilot_agent(
            "策略上还有什么建议",
            session_id="sess-agent-patch",
            context={"type": "job", "id": 10},
        )

        patch = result["strategy_patch"]
        assert patch is not None
        assert patch["workflow_id"] == workflow_id
        assert [change["value"] for change in patch["changes"]] == ["通信电源", "维谛"]
        assert self.llm.tool_turns == 1

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT structured_json FROM agent_copilot_messages
                WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1
                """,
                ("sess-agent-patch",),
            ).fetchone()
        finally:
            conn.close()
        structured = json.loads(row["structured_json"])
        assert structured["strategy_patch"]["workflow_id"] == workflow_id
        assert [change["value"] for change in structured["strategy_patch"]["changes"]] == ["通信电源", "维谛"]


if __name__ == "__main__":
    unittest.main()
