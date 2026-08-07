from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent.capability_runtime import (  # noqa: E402
    EXTERNAL_EXECUTION_CAPABILITY_IDS,
    SERVICE_HANDLED_CAPABILITY_IDS,
    RecruitingCapabilityRuntime,
    assert_workflow_capabilities_resolvable,
)


def fake_assessment() -> dict[str, Any]:
    return {
        "confidence": 0.9,
        "criteria": {
            "hard_requirements": [
                {"criterion": "半导体设备经验", "status": "met", "evidence": ["履历显示经验"]}
            ],
            "core_abilities": [{"criterion": "结构设计", "status": "met", "evidence": ["项目经历"]}],
        },
        "risks": [],
        "strengths": [],
        "gaps": [],
        "verification_questions": [],
        "fit_score": 90,
        "recommendation": "review_pass",
        "next_action": "推进",
        "summary": "测试",
        "career_trajectory": [],
        "percentile_motivation": {},
    }


# 服务层本地处理器（talent_pool_search / reply_triage / communication_draft_batch）依赖的
# v3 基础表；agent_* 表由 AgentService 启动时的 ensure_schema 补齐。
BASE_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,location TEXT,status TEXT,
  hard_requirements TEXT,ability_keywords TEXT,target_companies TEXT,exclusions TEXT,summary TEXT,updated_at TEXT);
CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT,current_title TEXT,
  city TEXT,education TEXT,experience TEXT);
CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,raw_client TEXT,
  raw_position TEXT,raw_status TEXT,raw_stage TEXT,clean_stage TEXT,flow_bucket TEXT,updated_at TEXT,
  source_candidate_id TEXT);
CREATE TABLE candidates(id INTEGER,name TEXT,company TEXT,title TEXT,education TEXT,experience TEXT,
  skills TEXT,city TEXT,client TEXT,position TEXT,status TEXT,notes TEXT,updated_at TEXT);
CREATE TABLE candidate_events(id INTEGER PRIMARY KEY,job_candidate_id INTEGER,person_id INTEGER,job_id INTEGER,
  event_type TEXT,event_status TEXT,event_time TEXT,summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
"""


class CapabilityCompletenessTest(unittest.TestCase):
    """注册能力 × 确定性 Runner 可用性 × 调用语义的一致性审计。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "agent.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(BASE_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,'长越科技')")
        conn.execute(
            "INSERT INTO jobs VALUES (10,1,'机械高级工程师','杭州','已发布','','','','','','2026-07-14')"
        )
        conn.execute(
            "INSERT INTO people VALUES (20,'张航','ASM中国集团公司','高级机械设计工程师','上海','本科','8年')"
        )
        conn.execute(
            "INSERT INTO job_candidates VALUES (30,10,20,'长越科技','机械高级工程师','new','','X1 待复核','正式流程','2026-07-14','40')"
        )
        conn.commit()
        conn.close()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment()))

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def _workflow_capability_ids(self) -> set[str]:
        """工作流能力以 lambda handler 注册；内置技能走绑定的 _skill_* 方法。"""
        workflow_ids: set[str] = set()
        for item in self.service.skills.list():
            spec = self.service.skills.get(item["id"])
            if isinstance(spec.handler, type(lambda: None)):
                workflow_ids.add(spec.id)
        return workflow_ids

    def test_registered_workflow_capabilities_all_resolve(self) -> None:
        runner_ids = RecruitingCapabilityRuntime.deterministic_runner_ids()
        workflow_ids = self._workflow_capability_ids()
        self.assertEqual(len(workflow_ids), 26)
        # 注册能力 = 确定性 Runner ∪ 服务层处理器，集合互斥且无遗漏。
        self.assertEqual(workflow_ids - runner_ids, set(SERVICE_HANDLED_CAPABILITY_IDS))
        self.assertEqual(runner_ids & set(SERVICE_HANDLED_CAPABILITY_IDS), set())
        availability = self.service.capability_runtime.availability()
        by_id = {row["capability_id"]: row for row in availability["capabilities"]}
        self.assertEqual(set(by_id), workflow_ids)
        for capability_id, row in by_id.items():
            self.assertTrue(row["registered"], capability_id)
            self.assertIn(
                row["execution_path"], {"deterministic_runner", "service_handler"}, capability_id
            )

    def test_registration_invariant_rejects_unresolvable_capability(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ghost_capability"):
            assert_workflow_capabilities_resolvable(
                [("ghost_capability", "幽灵能力")], {"job_intake"}
            )
        # 注册到服务层处理器集合后同一能力可通过门禁。
        assert_workflow_capabilities_resolvable(
            [("ghost_capability", "幽灵能力")],
            {"job_intake"},
            {"ghost_capability"},
        )

    def test_service_startup_fails_fast_when_runner_missing(self) -> None:
        # 模拟注册了一个既无 run_* Runner 也不在服务层集合的能力 → 启动即失败而不是运行时爆炸。
        with mock.patch.object(
            RecruitingCapabilityRuntime,
            "deterministic_runner_ids",
            return_value=frozenset({"job_intake"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "job_library_update"):
                AgentService(self.db_path, FakeLLM(fake_assessment()))

    def test_execute_delegates_service_handled_registered_capability(self) -> None:
        # 服务层实现的能力通过运行时入口执行：调用语义完整，不再抛“尚未实现”原始异常。
        result = self.service.capability_runtime.execute(
            "talent_pool_search", {"type": "job", "id": 10}, {}
        )
        self.assertIsInstance(result.get("candidates"), list)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIn("检索完成", result["summary"])
        # 注册表路径同样可执行，四个服务层能力都有可用 handler。
        for capability_id in ("reply_triage", "communication_draft_batch"):
            executed = self.service.skills.execute(capability_id, {"type": "global"}, {})
            self.assertIn("summary", executed["result"])

    def test_execute_unknown_capability_raises_controlled_availability_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.service.capability_runtime.execute("ghost_capability", {"type": "global"}, {})
        message = str(ctx.exception)
        self.assertIn("可用能力", message)
        self.assertIn("job_intake", message)

    def test_external_execution_restricted_to_supported_set(self) -> None:
        runner_ids = RecruitingCapabilityRuntime.deterministic_runner_ids()
        known = runner_ids | set(SERVICE_HANDLED_CAPABILITY_IDS)
        self.assertEqual(EXTERNAL_EXECUTION_CAPABILITY_IDS, frozenset({"multi_channel_sourcing"}))
        for capability_id in sorted(known - set(EXTERNAL_EXECUTION_CAPABILITY_IDS)):
            with self.assertRaises(ValueError) as ctx:
                self.service.capability_runtime.execute_external(capability_id, {})
            self.assertIn("仅支持", str(ctx.exception), capability_id)
            self.assertIn("multi_channel_sourcing", str(ctx.exception), capability_id)
        # 唯一支持后台渠道执行的能力通过支持门禁（随后进入参数校验）。
        with self.assertRaises(ValueError) as ctx:
            self.service.capability_runtime.execute_external("multi_channel_sourcing", {})
        self.assertNotIn("仅支持", str(ctx.exception))
        self.assertIn("寻访任务缺少客户或岗位", str(ctx.exception))

    def test_availability_metadata_reports_external_and_single_capability(self) -> None:
        availability = self.service.capability_runtime.availability()
        external = [
            row["capability_id"]
            for row in availability["capabilities"]
            if row["external_execution_supported"]
        ]
        self.assertEqual(external, ["multi_channel_sourcing"])
        single = self.service.capability_runtime.availability("job_intake")
        self.assertEqual(single["capabilities"]["capability_id"], "job_intake")
        self.assertEqual(single["capabilities"]["execution_path"], "deterministic_runner")
        self.assertTrue(single["capabilities"]["registered"])


if __name__ == "__main__":
    unittest.main()
