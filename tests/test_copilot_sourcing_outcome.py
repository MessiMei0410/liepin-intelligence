"""ROUND2 T4：Copilot 消费业务终态与寻访漏斗回归。

覆盖：
- business_outcome 四终态中文语义与 0 召回归因中文映射的完整性（单一来源）；
- 问"这轮寻访什么结果"时注入上下文含业务终态中文语义与漏斗数字（DB 实读）；
- 历史无漏斗轮次明确标注"该轮未记录渠道明细"，不编造数字；
- 指定轮次（"第 N 轮"）定位；
- completed_needs_review 的回答语义不得出现"执行失败/系统故障"表述；
- 询问句不建立新工作流（R9 询问 vs 写入区分不回归）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.capability_runtime import (  # noqa: E402
    ZERO_RESULT_ATTRIBUTION_LABELS,
    ZERO_RESULT_ATTRIBUTIONS,
)
from a_system_agent.workflow import BUSINESS_OUTCOME_LABELS, BUSINESS_OUTCOMES  # noqa: E402


class OutcomeEchoLLM(FakeLLM):
    """捕获 copilot payload，并像遵守语义的 LLM 一样基于注入的轮次上下文作答。"""

    def __init__(self, assessment: dict | None = None) -> None:
        super().__init__(assessment or fake_assessment(), chat_text="测试回答")
        self.copilot_payloads: list[dict] = []

    def copilot(self, payload: dict) -> str:
        self.copilot_payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        outcome = payload.get("workflow_outcome") or {}
        rounds = outcome.get("rounds") or []
        if not rounds:
            return "没有可用的寻访轮次上下文。"
        asked = outcome.get("asked_round")
        round_item = next((item for item in rounds if item["round_index"] == asked), rounds[-1])
        label = round_item.get("business_outcome_label") or "仍在进行中"
        parts = [f"结论：第 {round_item['round_index']} 轮{label}。"]
        if round_item.get("funnel_note"):
            parts.append(f"依据：{round_item['funnel_note']}。")
        for channel in round_item.get("channels") or []:
            segment = (
                f"{channel['channel']} 召回 {channel['recall_count']}，"
                f"入库新增 {channel['intake_new_count']}，评估 {channel['assessed_count']}"
            )
            if channel.get("zero_attribution_label"):
                segment += f"；0 召回原因：{channel['zero_attribution_label']}"
            parts.append(f"依据：{segment}。")
        return "\n".join(parts)


class LabelMappingTest(unittest.TestCase):
    def test_outcome_labels_cover_all_enums(self) -> None:
        assert set(BUSINESS_OUTCOME_LABELS) == set(BUSINESS_OUTCOMES)
        assert "本轮完成" in BUSINESS_OUTCOME_LABELS["completed_target_met"]
        assert "本轮完成" in BUSINESS_OUTCOME_LABELS["completed_needs_review"]
        assert "有待复核" in BUSINESS_OUTCOME_LABELS["completed_needs_review"]
        assert "本轮完成" in BUSINESS_OUTCOME_LABELS["completed_pool_insufficient"]
        assert "技术失败" in BUSINESS_OUTCOME_LABELS["failed_technical"]
        for key in ("completed_target_met", "completed_needs_review", "completed_pool_insufficient"):
            assert "失败" not in BUSINESS_OUTCOME_LABELS[key]

    def test_attribution_labels_cover_all_enums(self) -> None:
        assert set(ZERO_RESULT_ATTRIBUTION_LABELS) == set(ZERO_RESULT_ATTRIBUTIONS)
        assert "登录态失效" in ZERO_RESULT_ATTRIBUTION_LABELS["session_expired"]
        assert "加载" in ZERO_RESULT_ATTRIBUTION_LABELS["loading_incomplete"]
        assert "页面结构" in ZERO_RESULT_ATTRIBUTION_LABELS["page_structure_changed"]
        assert "解析" in ZERO_RESULT_ATTRIBUTION_LABELS["parse_failure"]
        assert "无匹配结果" in ZERO_RESULT_ATTRIBUTION_LABELS["no_results"]
        assert "待排查" in ZERO_RESULT_ATTRIBUTION_LABELS["unknown"]


class CopilotSourcingOutcomeTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.llm = OutcomeEchoLLM(fake_assessment("unknown"))
        self.service = AgentService(self.db_path, self.llm)

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

    def drive_sourcing_to_terminal(self, objective: str = "给长越科技机械高级工程师补充10位合适人选") -> str:
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
                "run_id": f"source-{workflow_id[-6:]}",
                "channel_runs": [{"channel": "liepin", "status": "blocked"}],
                "intake": {"accepted_count": 0},
                "audit": {"ok": True},
            },
        )
        self.wait_for(workflow_id, {"blocked", "completed", "failed"})
        return workflow_id

    def insert_funnel(self, workflow_id: str, *, run_id: str = "run-t4") -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_sourcing_funnel(
                    run_id,workflow_id,job_id,client,job,channel,status,query_count,
                    recall_count,extracted_count,dedupe_count,unique_count,
                    detail_complete,detail_partial,detail_failed,
                    intake_duplicate_count,intake_new_count,assessed_count,high_score_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, workflow_id, 10, "长越科技", "机械高级工程师", "liepin", "completed", 6,
                    198, 24, 20, 20,
                    4, 1, 0,
                    16, 4, 5, 2,
                ),
            )
            conn.execute(
                """
                INSERT INTO agent_sourcing_funnel(
                    run_id,workflow_id,job_id,client,job,channel,status,query_count,
                    recall_count,zero_attribution,error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, workflow_id, 10, "长越科技", "机械高级工程师", "xsaas", "blocked", 3,
                    0, "session_expired", "X-SAAS_LOGIN_REQUIRED: 未找到已登录的 X-SaaS 页面",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def workflow_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM agent_workflows").fetchone()[0])
        finally:
            conn.close()

    def test_answer_carries_outcome_semantics_and_funnel_numbers(self) -> None:
        # verify_first>0 → completed_needs_review（与今晚士兰微两轮的真实终态一致）
        assessed = self.service.submit_assessment(30, wait=True)
        assert assessed["status"] == "completed"
        workflow_id = self.drive_sourcing_to_terminal()
        state = self.service.get_workflow(workflow_id)
        assert state["workflow"]["business_outcome"] == "completed_needs_review"
        self.insert_funnel(workflow_id)

        before = self.workflow_count()
        result = self.service.copilot(
            "这轮寻访什么结果", session_id="t4-outcome", context={"type": "job", "id": 10}
        )
        assert self.workflow_count() == before, "询问句不得建立新工作流"

        payload = self.llm.copilot_payloads[-1]
        outcome = payload["workflow_outcome"]
        assert outcome["job_id"] == 10
        round_item = outcome["rounds"][-1]
        assert round_item["workflow_id"] == workflow_id
        assert round_item["is_sourcing"] is True
        assert round_item["business_outcome"] == "completed_needs_review"
        assert round_item["business_outcome_label"] == "本轮完成，合格人数不足，有待复核人选"
        assert "召回 198" in round_item["summary_text"]
        assert "入库新增 4" in round_item["summary_text"]
        assert "登录态失效" in round_item["summary_text"]
        channels = {item["channel"]: item for item in round_item["channels"]}
        assert channels["liepin"]["recall_count"] == 198
        assert channels["liepin"]["intake_new_count"] == 4
        assert channels["liepin"]["assessed_count"] == 5
        assert channels["xsaas"]["recall_count"] == 0
        assert channels["xsaas"]["zero_attribution"] == "session_expired"
        assert channels["xsaas"]["zero_attribution_label"] == "登录态失效，需重新登录该渠道"

        answer = result["answer"]
        assert "本轮完成" in answer
        assert "合格人数不足" in answer
        assert "有待复核" in answer
        assert "198" in answer and "4" in answer and "5" in answer
        assert "登录态失效" in answer
        assert "执行失败" not in answer
        assert "系统故障" not in answer
        assert "技术失败" not in answer

    def test_historical_round_notes_missing_funnel(self) -> None:
        workflow_id = self.drive_sourcing_to_terminal()
        result = self.service.copilot(
            "这轮寻访什么结果", session_id="t4-history", context={"type": "job", "id": 10}
        )
        payload = self.llm.copilot_payloads[-1]
        round_item = payload["workflow_outcome"]["rounds"][-1]
        assert round_item["workflow_id"] == workflow_id
        assert round_item["channels"] == []
        assert round_item["funnel_note"] == "该轮未记录渠道明细"
        # 本用例 LLM 评估硬门槛为 unknown → verify_first>0 → needs_review
        assert round_item["business_outcome"] == "completed_needs_review"
        assert "该轮未记录渠道明细" in result["answer"]

    def test_specific_round_lookup(self) -> None:
        first_id = self.drive_sourcing_to_terminal()
        second_id = self.drive_sourcing_to_terminal()
        assert first_id != second_id
        self.insert_funnel(second_id)

        result = self.service.copilot(
            "第 1 轮寻访什么结果", session_id="t4-round", context={"type": "job", "id": 10}
        )
        payload = self.llm.copilot_payloads[-1]
        outcome = payload["workflow_outcome"]
        assert outcome["asked_round"] == 1
        assert [item["round_index"] for item in outcome["rounds"]] == [1, 2]
        first, second = outcome["rounds"]
        assert first["workflow_id"] == first_id
        assert first["funnel_note"] == "该轮未记录渠道明细"
        assert second["workflow_id"] == second_id
        assert {item["channel"] for item in second["channels"]} == {"liepin", "xsaas"}
        assert "第 1 轮" in result["answer"]
        assert "该轮未记录渠道明细" in result["answer"]

    def test_cancelled_workflow_not_counted_as_round(self) -> None:
        first_id = self.drive_sourcing_to_terminal()
        cancelled = self.service.create_goal("给长越科技机械高级工程师再补充5位合适人选", {"type": "job", "id": 10})
        cancelled_id = cancelled["workflow"]["workflow_id"]
        self.service.cancel_workflow(cancelled_id, "测试取消")
        second_id = self.drive_sourcing_to_terminal("给长越科技机械高级工程师继续补充8位合适人选")

        self.service.copilot(
            "第 2 轮寻访什么结果", session_id="t4-cancel", context={"type": "job", "id": 10}
        )
        payload = self.llm.copilot_payloads[-1]
        rounds = payload["workflow_outcome"]["rounds"]
        assert [item["workflow_id"] for item in rounds] == [first_id, second_id]
        assert [item["round_index"] for item in rounds] == [1, 2]
        assert cancelled_id not in {item["workflow_id"] for item in rounds}

    def test_loose_wording_round_keeps_user_numbering(self) -> None:
        # "再多找些人选"不含引擎寻访判定词（补充/补池/寻访/找人/搜索），
        # 但在用户视角它仍是第 1 轮寻访，轮次编号不得跳过它（#154 第 1 轮的真实情况）。
        loose = self.service.create_goal("给长越科技机械高级工程师再多找些人选", {"type": "job", "id": 10})
        loose_id = loose["workflow"]["workflow_id"]
        driven_id = self.drive_sourcing_to_terminal()

        self.service.copilot(
            "第 2 轮寻访什么结果", session_id="t4-loose", context={"type": "job", "id": 10}
        )
        payload = self.llm.copilot_payloads[-1]
        outcome = payload["workflow_outcome"]
        assert outcome["asked_round"] == 2
        rounds = outcome["rounds"]
        assert [item["workflow_id"] for item in rounds] == [loose_id, driven_id]
        assert [item["round_index"] for item in rounds] == [1, 2]
        assert rounds[0]["is_sourcing"] is False
        assert rounds[0]["business_outcome"] is None
        assert rounds[1]["is_sourcing"] is True

    def test_no_job_context_injects_nothing(self) -> None:
        self.drive_sourcing_to_terminal()
        self.service.copilot("今天天气怎么样", session_id="t4-global", context={"type": "global"})
        payload = self.llm.copilot_payloads[-1]
        assert "workflow_outcome" not in payload


if __name__ == "__main__":
    unittest.main()
