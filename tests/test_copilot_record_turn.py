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
        service.close()


if __name__ == "__main__":
    unittest.main()
