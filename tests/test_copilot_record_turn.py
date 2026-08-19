from __future__ import annotations

import unittest

from test_a_system_agent_v1 import AgentDbCase, fake_assessment

from a_system_agent import AgentService, FakeLLM


class CopilotRecordTurnTest(AgentDbCase):
    """DSH 等外部编排层轮次回填：会话列表可见、详情可恢复、按 request_id 幂等。"""

    def test_record_turn_appears_in_session_list_and_detail(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-1",
            request_id="req-1",
            message="士兰微电源专家名单给我",
            answer="名单如下……",
            context={"type": "job", "id": 142},
            source="dsh",
            model="deepseek-v4-flash",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])

        sessions = service.list_copilot_sessions()
        entry = next(s for s in sessions["sessions"] if s["session_id"] == "asa-test-1")
        self.assertEqual(entry["title"], "士兰微电源专家名单给我")
        self.assertEqual(entry["message_count"], 2)
        self.assertEqual(entry["context_type"], "job")
        self.assertEqual(entry["context_id"], 142)

        detail = service.get_copilot_session("asa-test-1")
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["messages"][1]["content"], "名单如下……")
        self.assertEqual(detail["messages"][1]["model_participation"]["model"], "deepseek-v4-flash")
        service.close()

    def test_record_turn_idempotent_by_request_id(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        kwargs = dict(
            session_id="asa-test-2", request_id="req-1",
            message="你好", answer="你好！有什么可以帮你？",
            context={"type": "page"}, source="dsh",
        )
        first = service.record_external_copilot_turn(**kwargs)
        second = service.record_external_copilot_turn(**kwargs)
        self.assertTrue(first["recorded"])
        self.assertTrue(second["ok"])
        self.assertFalse(second["recorded"])
        detail = service.get_copilot_session("asa-test-2")
        self.assertEqual(len(detail["messages"]), 2)
        service.close()

    def test_record_turn_requires_session_and_request_id(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="", request_id="", message="m", answer="a", context={},
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["recorded"])
        self.assertEqual(service.list_copilot_sessions()["sessions"], [])
        service.close()

    def test_record_turn_persists_action_card_for_session_restore(self) -> None:
        """DSH 透传的名单卡回填后落 structured_json，恢复会话时详情带出 action_card。"""
        card = {
            "type": "candidate_list",
            "context": {"type": "job", "id": 142},
            "summary": {"total": 7},
            "filter_mode": "strict",
        }
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-card",
            request_id="req-card-1",
            message="士兰微电源专家名单给我",
            answer="名单如下……",
            context={"type": "job", "id": 142},
            source="dsh",
            action_card=card,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])

        detail = service.get_copilot_session("asa-test-card")
        assistant = detail["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["action_card"], card)
        self.assertEqual(assistant["action_cards"], [card])
        service.close()

    def test_record_turn_without_action_card_keeps_detail_clean(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-nocard",
            request_id="req-nocard-1",
            message="你好",
            answer="你好！",
            context={"type": "page"},
            source="dsh",
        )
        self.assertTrue(result["recorded"])
        detail = service.get_copilot_session("asa-test-nocard")
        self.assertIsNone(detail["messages"][1]["action_card"])
        self.assertEqual(detail["messages"][1]["action_cards"], [])
        self.assertEqual(detail["messages"][1]["suggested_actions"], [])
        self.assertEqual(detail["messages"][1]["references"], [])
        service.close()

    def test_record_turn_persists_suggested_actions_and_references(self) -> None:
        """DSH 轮末聚合的对象操作入口回填后落 structured_json，恢复会话时操作芯片仍可用。"""
        actions = [
            {"type": "open_workflow", "id": "workflow_aaa", "label": "查看并审批"},
            {"type": "open_candidate", "id": 531, "label": "打开人选"},
        ]
        references = [
            {"type": "workflow", "id": "workflow_aaa", "label": "R3 外部寻访审批"},
            {"type": "candidate", "id": 531, "label": "张三", "subtitle": "某半导体"},
        ]
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-actions",
            request_id="req-actions-1",
            message="查一下现在有哪些待审批",
            answer="有 2 条待审批……",
            context={"type": "page"},
            source="dsh",
            suggested_actions=actions,
            references=references,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])

        detail = service.get_copilot_session("asa-test-actions")
        assistant = detail["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["suggested_actions"], actions)
        self.assertEqual(assistant["references"], references)
        service.close()

    def test_record_turn_persists_delegate_payload_for_session_restore(self) -> None:
        """asa_copilot_ask 委托载荷（经 DSH done）回填后落 structured_json，
        恢复会话时理解卡/执行回执/焦点/模型参与/工作流进度卡仍透出。"""
        understanding = {"show": True, "summary": "我理解为要士兰微电源专家名单"}
        receipt = {"state": "已生成建议", "verified": False}
        analysis = {"headline": "候选人分档", "metrics": [{"label": "已确认", "value": 3}], "next_step": "核验 2 人"}
        focus = {"client": "士兰微", "action": "寻访"}
        participation = {"mode": "model_tools", "label": "模型生成 + 工具证据", "model": "deepseek-v4"}
        workflow_progress = {
            "workflow_id": "workflow_aaa", "status": "running",
            "completed": 1, "total": 4, "label": "寻访中", "pending_approvals": [],
        }
        cards = [{"type": "candidate_list", "summary": {"total": 7}}]
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-delegate",
            request_id="req-delegate-1",
            message="士兰微寻访进展如何",
            answer="当前在寻访阶段……",
            context={"type": "job", "id": 142},
            source="dsh",
            understanding_card=understanding,
            execution_receipt=receipt,
            analysis_card=analysis,
            business_focus=focus,
            model_participation=participation,
            workflow_progress=workflow_progress,
            workflow_id="workflow_aaa",
            action_cards=cards,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])

        detail = service.get_copilot_session("asa-test-delegate")
        assistant = detail["messages"][1]
        self.assertEqual(assistant["understanding_card"], understanding)
        self.assertEqual(assistant["execution_receipt"], receipt)
        self.assertEqual(assistant["analysis_card"], analysis)
        self.assertEqual(assistant["business_focus"], focus)
        self.assertEqual(assistant["model_participation"], participation)
        self.assertEqual(assistant["workflow_progress"], workflow_progress)
        self.assertEqual(assistant["workflow_id"], "workflow_aaa")
        self.assertEqual(assistant["action_cards"], cards)
        # 复数卡片传入时单卡取首卡（前端渲染路径消费单卡）
        self.assertEqual(assistant["action_card"], cards[0])
        service.close()

    def test_record_turn_delegate_payload_defaults_when_absent(self) -> None:
        """不带委托载荷时保持既有默认：模型参与 badge 落 DSH 编排层，卡片字段干净。"""
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-plain",
            request_id="req-plain-1",
            message="你好", answer="你好！",
            context={"type": "page"}, source="dsh", model="deepseek-v4-flash",
        )
        self.assertTrue(result["recorded"])
        detail = service.get_copilot_session("asa-test-plain")
        assistant = detail["messages"][1]
        self.assertEqual(
            assistant["model_participation"],
            {"mode": "dsh", "label": "DSH 编排层", "model": "deepseek-v4-flash"},
        )
        self.assertIsNone(assistant["understanding_card"])
        self.assertIsNone(assistant["execution_receipt"])
        self.assertIsNone(assistant["workflow_progress"])
        self.assertIsNone(assistant["workflow_id"])
        service.close()

    def test_record_turn_persists_subagents_for_session_restore(self) -> None:
        """DSH 子代理运行终态（SSE subagent 事件聚合）回填后落 structured_json，
        恢复会话时详情带出 subagents，前端可重渲染「子代理执行」卡片终态。"""
        subagents = [
            {"id": "run-1", "label": "背调候选人甲", "status": "done", "summary": "甲已核实，在职。"},
            {"id": "run-2", "label": "背调候选人乙", "status": "failed"},
            {"id": "run-3", "label": "调研竞品岗位", "status": "running"},
        ]
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-subagents",
            request_id="req-subagents-1",
            message="背调这两个人",
            answer="背调结果如下……",
            context={"type": "page"},
            source="dsh",
            subagents=subagents,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])

        detail = service.get_copilot_session("asa-test-subagents")
        assistant = detail["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["subagents"], subagents)
        service.close()

    def test_record_turn_without_subagents_keeps_detail_clean(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.record_external_copilot_turn(
            session_id="asa-test-nosubagents",
            request_id="req-nosubagents-1",
            message="你好", answer="你好！",
            context={"type": "page"}, source="dsh",
        )
        self.assertTrue(result["recorded"])
        detail = service.get_copilot_session("asa-test-nosubagents")
        self.assertEqual(detail["messages"][1]["subagents"], [])
        service.close()

    def test_delegate_sessions_hidden_from_session_list_but_auditable(self) -> None:
        """孤儿会话治理：::dsh-delegate 派生 session 与遗留 dsh- 前缀 session
        不进会话列表 rollup，但消息仍在库中可审计（详情可按 id 取回）。"""
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        for session_id, request_id in (
            ("asa-777::dsh-delegate", "req-del-1"),
            ("dsh-0f3c9a2b-legacy", "req-legacy-1"),
            ("asa-visible", "req-visible-1"),
        ):
            result = service.record_external_copilot_turn(
                session_id=session_id, request_id=request_id,
                message="委托问题", answer="委托回答",
                context={"type": "page"}, source="dsh",
            )
            self.assertTrue(result["recorded"])

        sessions = service.list_copilot_sessions()
        session_ids = [s["session_id"] for s in sessions["sessions"]]
        self.assertEqual(session_ids, ["asa-visible"])
        # 委托会话仍可按 id 取回（审计轨迹不丢）
        detail = service.get_copilot_session("asa-777::dsh-delegate")
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        service.close()


if __name__ == "__main__":
    unittest.main()
