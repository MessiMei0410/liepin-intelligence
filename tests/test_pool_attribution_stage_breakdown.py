"""「为什么 X 还没动」归因口径回归（dogfood 剧本 11 / P6）。

修复前：评估队列（assessment_queue.assessed_items）只带评估快照，
归因只能看到工作流状态 + verify_first，看不到人选当前阶段——
11 个 C 档里 7 个已 H5 初筛淘汰被答成「一个都没触发」。
修复后：队列注入 current_stage/raw_status + stage_breakdown/stage_summary，
已停止人选显式计入「已分流」，与名单卡/周报漏斗同一套计数。
"""

from __future__ import annotations

import sqlite3
import unittest

from test_a_system_agent_v1 import AgentDbCase, fake_assessment
from a_system_agent import AgentService, FakeLLM
from a_system_agent.stage_breakdown import (
    assessed_stage_breakdown,
    assessed_stage_summary,
)


def _item(stage: str, recommendation: str = "", raw_status: str = "") -> dict:
    return {"current_stage": stage, "recommendation": recommendation, "raw_status": raw_status}


class AssessedStageBreakdownTest(unittest.TestCase):
    def test_mixed_pool_breakdown_counts_every_bucket(self) -> None:
        items = (
            [_item("H5 最近寻访/初筛不通过")] * 7
            + [_item("", recommendation="verify_first"), _item("触达待核验")]
            + [_item("已触达"), _item("H3 已沟通待回")]
            + [_item("X1 待复核")]
            + [_item("S2 待触达")]
        )
        breakdown = assessed_stage_breakdown(items)
        assert breakdown == {
            "stopped": 7,
            "verification": 2,
            "contacted": 2,
            "pending_review": 1,
            "other_active": 1,
        }
        # 桶互斥且穷尽：计数之和恒等于总人数（与名单/漏斗同口径，不造第二套计数）。
        assert sum(breakdown.values()) == len(items)

    def test_stopped_bucket_matches_funnel_and_list_tokens(self) -> None:
        # 停止口径 = 名单卡 _STOP_TOKENS ∪ 周报漏斗 _is_stopped：
        # 阶段词（初筛不通过/停止/淘汰/关闭）或 raw_status 停用值，均计入已分流。
        stopped_items = [
            _item("H5 初筛不通过"),
            _item("已停止推进"),
            _item("客户淘汰"),
            _item("S2 待触达", raw_status="screen_rejected"),
            _item("S2 待触达", raw_status="rejected"),
        ]
        breakdown = assessed_stage_breakdown(stopped_items)
        assert breakdown["stopped"] == 5
        assert sum(breakdown.values()) == 5

    def test_stage_summary_marks_stopped_as_diverted_not_idle(self) -> None:
        # 剧本 11 场景：11 个 C 档（verify_first）里 7 个已 H5 停止。
        items = [_item("H5 初筛不通过", recommendation="verify_first")] * 7 + [
            _item("", recommendation="verify_first")
        ] * 4
        summary = assessed_stage_summary(items)
        assert "共 11 人" in summary
        assert "已分流停止 7" in summary
        assert "触达待核验 4" in summary
        # 已淘汰必须显式计为「已分流」，不能落入「没动」。
        assert "已分流" in summary
        assert "而非未推进" in summary

    def test_empty_pool(self) -> None:
        assert assessed_stage_summary([]) == ""
        assert sum(assessed_stage_breakdown([]).values()) == 0


class PoolAttributionStageTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试回答"))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO people VALUES (21,'李四','北方华创','机械工程师','北京','硕士','6年')"
            )
            conn.execute(
                "INSERT INTO job_candidates VALUES (31,10,21,'长越科技','机械高级工程师','new','','触达待核验','正式流程','2026-07-14','')"
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.service.close()
        super().tearDown()

    def _set_stage(self, job_candidate_id: int, stage: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE job_candidates SET clean_stage=? WHERE id=?", (stage, int(job_candidate_id)))
            conn.commit()
        finally:
            conn.close()

    def test_assessment_queue_carries_stage_breakdown(self) -> None:
        # 混合阶段池：30 已 H5 停止（评估后淘汰分流），31 触达待核验。
        assert self.service.submit_assessment(30, wait=True)["status"] == "completed"
        assert self.service.submit_assessment(31, wait=True)["status"] == "completed"
        self._set_stage(30, "H5 最近寻访/初筛不通过")

        result = self.service._execute_workflow_capability(
            "candidate_batch_assessment",
            {"type": "job", "id": 10},
            {},
        )
        queue = result["assessment_queue"]
        # 每个人选带回当前阶段，归因不再只看评估快照。
        stages = {item["job_candidate_id"]: item["current_stage"] for item in queue["assessed_items"]}
        assert stages[30] == "H5 最近寻访/初筛不通过"
        assert stages[31] == "触达待核验"
        # 阶段分解：已停止计入已分流，而非「没动」。
        assert queue["stage_breakdown"]["stopped"] == 1
        assert queue["stage_breakdown"]["verification"] == 1
        assert sum(queue["stage_breakdown"].values()) == queue["completed"] == 2
        assert "已分流停止 1" in queue["stage_summary"]
        assert "已分流" in queue["stage_summary"]

    def test_workflow_read_path_hydrates_stage_breakdown(self) -> None:
        # get_workflow 读取路径（Copilot 实际消费的那份）同样带阶段分解。
        assert self.service.submit_assessment(30, wait=True)["status"] == "completed"
        self._set_stage(30, "H5 最近寻访/初筛不通过")

        created = self.service.create_goal(
            "给长越科技机械高级工程师补充10位合适人选",
            {"type": "job", "id": 10, "page": "positions"},
        )
        workflow_id = created["workflow"]["workflow_id"]
        hydrated = self.service.get_workflow(workflow_id)
        step = next(s for s in hydrated["steps"] if s["capability_id"] == "candidate_batch_assessment")
        queue = step["output"]["assessment_queue"]
        assert queue["assessed_items"][0]["current_stage"] == "H5 最近寻访/初筛不通过"
        assert queue["stage_breakdown"]["stopped"] == 1
        assert "已分流停止" in queue["stage_summary"]


if __name__ == "__main__":
    unittest.main()
