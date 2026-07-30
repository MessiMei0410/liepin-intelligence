from __future__ import annotations

import time
import unittest

from test_a_system_agent_v1 import (
    AgentDbCase,
    AgentHttpApiTest,
    fake_assessment,
    workbench_server,
)

from a_system_agent import AgentService, FakeLLM


class AgentV15ServiceTest(AgentDbCase):
    def test_v16_config_and_skill_registry_are_public_and_allowlisted(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        config = service.get_public_config()["config"]
        self.assertEqual(config["model"]["model"], "deepseek-v4-pro")
        self.assertNotIn("api_key", config["model"])
        skills = {item["id"]: item for item in service.list_skills()["skills"]}
        self.assertTrue({"job_diagnosis", "candidate_assessment", "verification_plan", "communication_draft", "liepin_resume_capture"}.issubset(skills))
        self.assertTrue({"search_strategy", "multi_channel_sourcing", "recommendation_report", "outreach_prepare", "outreach_execute", "salary_verification", "offer_confirmation", "project_retrospective"}.issubset(skills))
        self.assertEqual(skills["multi_channel_sourcing"]["adapter_type"], "browser")
        self.assertEqual(skills["multi_channel_sourcing"]["risk_level"], "R3")
        self.assertEqual(skills["job_publish_prepare"]["risk_level"], "R1")
        self.assertEqual(skills["outreach_prepare"]["risk_level"], "R1")
        self.assertIn("single_action_confirmation", skills["outreach_execute"]["required_permissions"])
        self.assertEqual(skills["communication_draft"]["risk_level"], "R1")
        self.assertEqual(skills["multi_channel_sourcing"]["action_kind"], "external_write")
        self.assertEqual(skills["multi_channel_sourcing"]["preflight_mode"], "required")
        self.assertEqual(skills["multi_channel_sourcing"]["confirmation_surface"], "workflow_approval")
        self.assertEqual(skills["multi_channel_sourcing"]["post_check"], "external_evidence")
        self.assertEqual(skills["job_diagnosis"]["action_kind"], "read")
        self.assertEqual(skills["job_diagnosis"]["audit_event_type"], "capability.job_diagnosis")
        with self.assertRaisesRegex(ValueError, "人工预检|单次审批"):
            service.execute_skill("identity_merge_preflight", context={"type": "candidate"}, inputs={})
        with self.assertRaisesRegex(ValueError, "人工预检|单次审批"):
            service.execute_skill("multi_channel_sourcing", context={"type": "job"}, inputs={})
        with self.assertRaisesRegex(ValueError, "未注册"):
            service.execute_skill("shell", context={"type": "global"}, inputs={})
        service.close()

    def test_v18_memory_is_deduplicated_scoped_and_revocable(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        first = service.store_memory(
            scope_type="candidate", scope_id=30, memory_type="verification_result",
            content="候选人确认两周可以到岗", source_type="test", source_id=1,
        )
        second = service.store_memory(
            scope_type="candidate", scope_id=30, memory_type="verification_result",
            content="候选人确认两周可以到岗", source_type="test", source_id=1,
        )
        self.assertEqual(first["memory_id"], second["memory_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(service.search_memories("两周到岗", context_type="global")["memories"], [])
        scoped = service.search_memories("两周到岗", context_type="candidate", context_id=30)
        self.assertEqual(scoped["memories"][0]["id"], first["memory_id"])
        self.assertTrue(scoped["semantic_reranked"])
        service.revoke_memory(first["memory_id"])
        self.assertEqual(service.search_memories("两周到岗", context_type="candidate", context_id=30)["memories"], [])
        service.close()

    def test_v16_copilot_session_restores_structured_messages(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="先核验关键项目。"))
        service.submit_assessment(30, wait=True)
        service.copilot("这个人选缺什么？", session_id="restore-test", context={"type": "candidate", "id": 30})
        history = service.get_copilot_session("restore-test")
        self.assertEqual([item["role"] for item in history["messages"]], ["user", "assistant"])
        self.assertTrue(history["messages"][1]["references"])
        self.assertTrue(history["messages"][1]["skill_runs"])
        sessions = service.list_copilot_sessions()
        self.assertEqual(sessions["sessions"][0]["session_id"], "restore-test")
        self.assertEqual(sessions["sessions"][0]["title"], "这个人选缺什么？")
        service.close()

    def test_dashboard_and_global_copilot_support_contextual_queries(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="当前应优先处理待复核人选。"))
        service.submit_assessment(30, wait=True)
        dashboard = service.get_dashboard()
        self.assertIn("funnel", dashboard["analytics"])
        self.assertIn("channels", dashboard["analytics"])
        self.assertIn("feedback", dashboard["analytics"])
        self.assertIn("agent_quality", dashboard["analytics"])
        self.assertIn("rates", dashboard["analytics"]["funnel"])
        self.assertIn("jobs", dashboard["analytics"]["funnel"])
        self.assertIn("candidate_reply_avg_hours", dashboard["analytics"]["feedback"])
        self.assertIn("stalled_jobs", dashboard["analytics"]["feedback"])
        self.assertEqual(len(dashboard["analytics"]["agent_quality"]["score_distribution"]), 3)
        self.assertEqual(len(dashboard["analytics"]["agent_quality"]["coverage_distribution"]), 3)
        self.assertIn("latest_failure_rate", dashboard["analytics"]["agent_quality"])
        reply = service.copilot(
            "现在优先处理什么？",
            session_id="global-test",
            context={"type": "candidate", "id": 30},
        )
        self.assertEqual(reply["session_id"], "global-test")
        self.assertEqual(reply["context"]["type"], "candidate")
        self.assertTrue(reply["references"])
        self.assertIn("优先", reply["answer"])
        service.close()

    def test_channel_efficiency_tracks_attribution_and_conversion_funnel(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        conn = service._connect()
        conn.execute("ALTER TABLE candidates ADD COLUMN source TEXT")
        conn.execute("UPDATE candidates SET source='liepin' WHERE id=40")
        conn.execute(
            "INSERT INTO people VALUES (21,'李明','测试公司','控制工程师','苏州','本科','6年')"
        )
        conn.execute(
            """
            INSERT INTO candidates
            (id,name,company,title,education,experience,skills,city,client,position,status,notes,updated_at,source)
            VALUES (41,'李明','测试公司','控制工程师','本科','6年','PLC','苏州','长越科技',
                    '机械高级工程师','contacted','测试人选','2026-07-15','xsaas')
            """
        )
        conn.execute(
            """
            INSERT INTO job_candidates
            VALUES (31,10,21,'长越科技','机械高级工程师','contacted','','S3 已回复','正式流程','2026-07-15','41')
            """
        )
        conn.executemany(
            """
            INSERT INTO candidate_events
            (id,job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (81, 31, 21, 10, "resume_review_completed", "continue", "2026-07-15", "通过", "{}", "test", "1"),
                (82, 31, 21, 10, "candidate_outreach", "contacted", "2026-07-15", "已联系", "{}", "test", "2"),
                (83, 31, 21, 10, "candidate_message_received", "done", "2026-07-15", "已回复", "{}", "test", "3"),
            ],
        )
        conn.commit()
        conn.close()

        channels = service.get_dashboard()["analytics"]["channels"]
        by_channel = {item["channel"]: item for item in channels["rows"]}
        self.assertEqual(channels["summary"]["total_intake"], 2)
        self.assertEqual(channels["summary"]["coverage_rate"], 1.0)
        self.assertEqual(by_channel["liepin"]["intake"], 1)
        self.assertEqual(by_channel["xsaas"]["review_pass_rate"], 1.0)
        self.assertEqual(by_channel["xsaas"]["contact_rate"], 1.0)
        self.assertEqual(by_channel["xsaas"]["reply_rate"], 1.0)
        service.close()

    def test_stage_shadow_rules_never_execute_external_actions(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        high = service.stage_shadow_decision(
            {"fit_score": 82, "confidence": 0.9, "evidence_coverage": 0.84, "recommendation": "priority_review", "criteria": {"hard_requirements": []}}
        )
        verify = service.stage_shadow_decision(
            {"fit_score": 70, "confidence": 0.78, "evidence_coverage": 0.62, "recommendation": "verify_first", "criteria": {"hard_requirements": []}}
        )
        low = service.stage_shadow_decision(
            {"fit_score": 48, "confidence": 0.82, "evidence_coverage": 0.8, "recommendation": "not_recommended", "criteria": {"hard_requirements": []}}
        )
        blocked = service.stage_shadow_decision(
            {"fit_score": 88, "confidence": 0.9, "evidence_coverage": 0.9, "recommendation": "priority_review", "criteria": {"hard_requirements": [{"status": "not_met", "critical": True}]}}
        )
        self.assertEqual(high["proposed_stage"], "复核通过待联系")
        self.assertEqual(verify["proposed_stage"], "待核验")
        self.assertEqual(low["proposed_stage"], "待人工复核")
        self.assertEqual(blocked["proposed_stage"], "待人工复核")
        for result in [high, verify, low, blocked]:
            self.assertEqual(result["mode"], "shadow")
            self.assertFalse(result["executed"])
            self.assertNotIn(result["action_type"], {"outreach", "candidate_merge", "stop"})
        service.close()

    def test_workbench_surfaces_unassessed_and_human_review(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        first = service.get_workbench()
        self.assertEqual(first["summary"]["unassessed"], 1)
        self.assertEqual(first["items"][0]["job_candidate_id"], 30)
        self.assertEqual(first["items"][0]["kind"], "unassessed")
        self.assertEqual(first["runtime"]["model"], "fake-agent-v1")
        self.assertEqual(first["recent_runs"], [])

        service.submit_assessment(30, wait=True)
        second = service.get_workbench()
        self.assertEqual(second["summary"]["human_review"], 1)
        self.assertEqual(second["items"][0]["kind"], "human_review")
        self.assertEqual(second["recent_runs"][0]["status"], "completed")
        self.assertEqual(second["recent_runs"][0]["job_candidate_id"], 30)
        service.close()

    def test_batch_assess_is_bounded_and_uses_current_queue(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.batch_assess(limit=5)
        self.assertEqual(len(result["started"]), 1)
        run_id = result["started"][0]["run_id"]
        for _ in range(100):
            run = service.get_run(run_id)
            if run["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(run["status"], "completed")
        repeated = service.batch_assess([30])
        self.assertEqual(repeated["started"], [])
        self.assertEqual(repeated["skipped"][0]["reason"], "当前判断仍有效")
        service.close()

    def test_auto_assess_all_submits_the_complete_open_queue(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        result = service.auto_assess_all(limit=50)
        self.assertEqual(result["queued_total"], 1)
        self.assertEqual(result["started"][0]["job_candidate_id"], 30)
        run_id = result["started"][0]["run_id"]
        for _ in range(100):
            run = service.get_run(run_id)
            if run["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(run["trigger"], "overview_auto_queue")
        service.close()

    def test_failed_item_returns_to_retry_queue_after_cooldown(self) -> None:
        def fail(_context: dict) -> dict:
            raise RuntimeError("rate limited")

        service = AgentService(self.db_path, FakeLLM(fail))
        service.submit_assessment(30, wait=True)
        self.assertEqual(service.get_workbench()["items"][0]["kind"], "failed")
        conn = service._connect()
        conn.execute(
            "UPDATE agent_runs SET updated_at=datetime('now','localtime','-11 minutes')"
        )
        conn.commit()
        conn.close()
        self.assertEqual(service.get_workbench()["items"][0]["kind"], "unassessed")
        service.close()

    def test_internal_followup_task_does_not_stale_candidate_assessment(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        self.assertFalse(service.get_candidate_state(30)["stale"])
        conn = service._connect()
        conn.execute(
            """
            INSERT INTO candidate_events(
                job_candidate_id,person_id,job_id,event_type,event_status,event_time,
                summary,raw_json,source_table,source_id
            ) VALUES (30,20,10,'followup_task','open',datetime('now','localtime'),
                      '核验内部证据','{}','followup_tasks','1')
            """
        )
        conn.commit()
        conn.close()
        self.assertFalse(service.get_candidate_state(30)["stale"])
        service.close()

    def test_candidate_state_surfaces_attached_agent_reports(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        conn = service._connect()
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
            VALUES ('artifact_report','goal_report','workflow_report',1,'recommendation_report','嘉驰推荐报告',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document','/tmp/report.docx',
                    '审计通过',?,'passed')
            """,
            ('{"job_candidate_id":30,"candidate_id":"40","attached_to_candidate":true}',),
        )
        conn.commit()
        conn.close()
        state = service.get_candidate_state(30)
        self.assertEqual(state["artifacts"][0]["artifact_id"], "artifact_report")
        self.assertEqual(state["artifacts"][0]["artifact_type"], "recommendation_report")
        service.close()

    def test_proposals_are_idempotent_and_require_one_time_confirmation(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        first = service.generate_proposals([30])
        second = service.generate_proposals([30])
        self.assertEqual(len(first["proposals"]), 1)
        self.assertEqual(first["proposals"][0]["proposal_id"], second["proposals"][0]["proposal_id"])

        proposal_id = first["proposals"][0]["proposal_id"]
        preflight = service.proposal_preflight(proposal_id)
        approved = service.decide_proposal(
            proposal_id,
            preflight["confirmation_token"],
            "approve",
            "人工确认创建核验任务",
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["action_type"], "create_task")
        with self.assertRaisesRegex(ValueError, "无效或已过期"):
            service.decide_proposal(proposal_id, preflight["confirmation_token"], "approve")

        finished = service.finish_proposal(proposal_id, success=True)
        self.assertEqual(finished["status"], "executed")
        proposals = service.list_proposals("executed")
        self.assertEqual(proposals["proposals"][0]["proposal_id"], proposal_id)
        service.close()

    def test_expired_proposal_confirmation_token_is_rejected(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        proposal = service.generate_proposals([30])["proposals"][0]
        preflight = service.proposal_preflight(proposal["proposal_id"])
        with service._lock:
            service._proposal_confirmations[preflight["confirmation_token"]]["expires_at"] = time.time() - 1
        with self.assertRaisesRegex(ValueError, "无效或已过期"):
            service.decide_proposal(
                proposal["proposal_id"], preflight["confirmation_token"], "approve"
            )
        self.assertEqual(service.list_proposals("pending")["proposals"][0]["proposal_id"], proposal["proposal_id"])
        service.close()

    def test_proposal_reject_does_not_execute(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        proposal = service.generate_proposals([30])["proposals"][0]
        preflight = service.proposal_preflight(proposal["proposal_id"])
        rejected = service.decide_proposal(
            proposal["proposal_id"], preflight["confirmation_token"], "reject", "暂不创建"
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(service.list_proposals("pending")["proposals"], [])
        service.close()

    def test_unknown_proposal_action_is_failed_without_agent_action(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        proposal = service.generate_proposals([30])["proposals"][0]
        conn = service._connect()
        conn.execute(
            "UPDATE agent_action_proposals SET action_type='external_unknown',status='approved' WHERE proposal_id=?",
            (proposal["proposal_id"],),
        )
        conn.commit()
        conn.close()
        proposal["action_type"] = "external_unknown"
        with self.assertRaisesRegex(ValueError, "不支持自动执行动作"):
            service.execute_proposal(proposal)
        failed = service.list_proposals("failed")["proposals"]
        self.assertEqual(failed[0]["proposal_id"], proposal["proposal_id"])
        conn = service._connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0], 0)
        conn.close()
        service.close()

    def test_failed_execution_can_be_regenerated_on_same_snapshot(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        proposal = service.generate_proposals([30])["proposals"][0]
        preflight = service.proposal_preflight(proposal["proposal_id"])
        service.decide_proposal(
            proposal["proposal_id"], preflight["confirmation_token"], "approve"
        )
        service.finish_proposal(proposal["proposal_id"], success=False, note="temporary")
        regenerated = service.generate_proposals([30])["proposals"][0]
        self.assertEqual(regenerated["proposal_id"], proposal["proposal_id"])
        self.assertEqual(regenerated["status"], "pending")
        service.close()

    def test_stale_proposal_cannot_pass_preflight(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        service.submit_assessment(30, wait=True)
        proposal = service.generate_proposals([30])["proposals"][0]
        conn = service._connect()
        conn.execute("UPDATE people SET current_title='机械研发经理' WHERE id=20")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "依据已变化"):
            service.proposal_preflight(proposal["proposal_id"])
        service.close()


class AgentV15HttpApiTest(AgentHttpApiTest):
    def test_hidden_candidate_open_cannot_start_assessment(self) -> None:
        conn = self.service._connect()
        before = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        conn.close()
        status, result = self.request(
            "POST",
            "/api/agent/candidate-assess",
            {"job_candidate_id": 30, "trigger": "candidate_open", "page_active": False},
        )
        self.assertEqual(status, 409)
        self.assertFalse(result["ok"])
        conn = self.service._connect()
        after = conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        conn.close()
        self.assertEqual(after, before)

    def test_workbench_batch_and_rejected_proposal_routes(self) -> None:
        status, workbench = self.request(
            "GET", "/api/agent/workbench?limit=8", origin="file://"
        )
        self.assertEqual(status, 200)
        self.assertEqual(workbench["items"][0]["kind"], "unassessed")

        status, batch = self.request(
            "POST", "/api/agent/batch-assess", {"job_candidate_ids": [30], "limit": 5}
        )
        self.assertEqual(status, 202)
        run_id = batch["started"][0]["run_id"]
        for _ in range(100):
            _, run = self.request("GET", f"/api/agent/run?run_id={run_id}")
            if run["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(run["status"], "completed")

        status, generated = self.request(
            "POST", "/api/agent/proposals-generate", {"job_candidate_ids": [30]}
        )
        self.assertEqual(status, 200)
        proposal_id = generated["proposals"][0]["proposal_id"]
        status, preflight = self.request(
            "POST", "/api/agent/proposal-preflight", {"proposal_id": proposal_id}
        )
        self.assertEqual(status, 200)
        status, rejected = self.request(
            "POST",
            "/api/agent/proposal-decide",
            {
                "proposal_id": proposal_id,
                "confirmation_token": preflight["confirmation_token"],
                "decision": "reject",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(rejected["status"], "rejected")

    def test_approved_proposal_creates_task_and_action_audit(self) -> None:
        conn = self.service._connect()
        conn.execute(
            """
            CREATE TABLE followup_tasks(
                id INTEGER,candidate_id INTEGER,candidate_name TEXT,candidate_company TEXT,
                client TEXT,position TEXT,task_type TEXT,priority INTEGER,due_at TEXT,
                status TEXT,reason TEXT,source_table TEXT,source_id INTEGER,
                created_at TEXT,updated_at TEXT,job_candidate_id INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        self.service.submit_assessment(30, wait=True)
        _, generated = self.request(
            "POST", "/api/agent/proposals-generate", {"job_candidate_ids": [30]}
        )
        proposal_id = generated["proposals"][0]["proposal_id"]
        _, preflight = self.request(
            "POST", "/api/agent/proposal-preflight", {"proposal_id": proposal_id}
        )
        original_refresh = workbench_server.refresh_a_system_workbench
        workbench_server.refresh_a_system_workbench = lambda: {"ok": True, "test": True}
        try:
            status, executed = self.request(
                "POST",
                "/api/agent/proposal-decide",
                {
                    "proposal_id": proposal_id,
                    "confirmation_token": preflight["confirmation_token"],
                    "decision": "approve",
                },
            )
        finally:
            workbench_server.refresh_a_system_workbench = original_refresh
        self.assertEqual(status, 200)
        self.assertEqual(executed["status"], "executed")
        conn = self.service._connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM followup_tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM agent_action_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()[0],
                "executed",
            )
        finally:
            conn.close()

    def test_verification_tasks_sync_is_automatic_idempotent_and_internal_only(self) -> None:
        conn = self.service._connect()
        conn.execute(
            """
            CREATE TABLE followup_tasks(
                id INTEGER,candidate_id INTEGER,candidate_name TEXT,candidate_company TEXT,
                client TEXT,position TEXT,task_type TEXT,priority INTEGER,due_at TEXT,
                status TEXT,reason TEXT,source_table TEXT,source_id INTEGER,
                created_at TEXT,updated_at TEXT,job_candidate_id INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        self.service.submit_assessment(30, wait=True)
        original_refresh = workbench_server.refresh_a_system_workbench
        workbench_server.refresh_a_system_workbench = lambda: {"ok": True, "test": True}
        try:
            status, first = self.request(
                "POST",
                "/api/agent/verification-tasks-sync",
                {"job_candidate_ids": [30], "limit": 50},
            )
            second_status, second = self.request(
                "POST",
                "/api/agent/verification-tasks-sync",
                {"job_candidate_ids": [30], "limit": 50},
            )
        finally:
            workbench_server.refresh_a_system_workbench = original_refresh
        self.assertEqual(status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["executed_total"], 1)
        self.assertEqual(second["executed_total"], 0)
        conn = self.service._connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM followup_tasks").fetchone()[0], 1)
            action = conn.execute("SELECT action_type,risk_level FROM agent_actions").fetchone()
            self.assertEqual(tuple(action), ("create_task", "R1"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_actions WHERE action_type IN ('outreach','candidate_merge','stop')").fetchone()[0],
                0,
            )
            workbench = self.service.get_workbench()
            self.assertEqual(workbench["summary"]["open_verification_tasks"], 1)
            self.assertEqual(workbench["verification_tasks"][0]["job_candidate_id"], 30)
        finally:
            conn.close()

    def test_verification_completion_closes_task_writes_evidence_and_reassesses(self) -> None:
        conn = self.service._connect()
        conn.execute(
            """
            CREATE TABLE followup_tasks(
                id INTEGER,candidate_id INTEGER,candidate_name TEXT,candidate_company TEXT,
                client TEXT,position TEXT,task_type TEXT,priority INTEGER,due_at TEXT,
                status TEXT,reason TEXT,source_table TEXT,source_id INTEGER,
                created_at TEXT,updated_at TEXT,job_candidate_id INTEGER
            )
            """
        )
        conn.commit()
        conn.close()
        self.service.submit_assessment(30, wait=True)
        original_refresh = workbench_server.refresh_a_system_workbench
        workbench_server.refresh_a_system_workbench = lambda: {"ok": True, "test": True}
        try:
            _, synced = self.request(
                "POST",
                "/api/agent/verification-tasks-sync",
                {"job_candidate_ids": [30], "limit": 50},
            )
            task_id = synced["executed"][0]["task_id"]
            status, completed = self.request(
                "POST",
                "/api/agent/verification-complete",
                {
                    "task_id": task_id,
                    "job_candidate_id": 30,
                    "answers": [
                        {
                            "question": "确认年龄和到岗时间",
                            "status": "met",
                            "note": "年龄符合，两周到岗",
                        }
                    ],
                    "note": "电话核验",
                },
            )
        finally:
            workbench_server.refresh_a_system_workbench = original_refresh
        self.assertEqual(status, 202)
        self.assertEqual(completed["status"], "done")
        for _ in range(100):
            run = self.service.get_run(completed["run_id"])
            if run["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(run["status"], "completed")
        conn = self.service._connect()
        try:
            task = conn.execute(
                "SELECT status,resolution_note FROM followup_tasks WHERE id=?", (task_id,)
            ).fetchone()
            self.assertEqual(task["status"], "done")
            self.assertIn("两周到岗", task["resolution_note"])
            event = conn.execute(
                "SELECT summary,raw_json FROM candidate_events WHERE event_type='agent_verification_completed'"
            ).fetchone()
            self.assertIn("确认年龄和到岗时间=满足", event["summary"])
            self.assertIn("电话核验", event["raw_json"])
            action = conn.execute(
                "SELECT risk_level,status FROM agent_actions WHERE action_type='complete_task'"
            ).fetchone()
            self.assertEqual(tuple(action), ("R1", "executed"))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent_candidate_assessments").fetchone()[0],
                2,
            )
        finally:
            conn.close()


class AgentV15IntegrationContractTest(unittest.TestCase):
    def test_server_and_generator_expose_v15_workbench_contract(self) -> None:
        server = open("scripts/liepin_workbench_server.py", encoding="utf-8").read()
        schema = open("scripts/a_system_agent/schema.py", encoding="utf-8").read()
        self.assertIn("agent_copilot_messages", schema)
        self.assertIn("agent_stage_recommendations", schema)
        for route in [
            "/api/agent/workbench",
            "/api/agent/dashboard",
            "/api/agent/copilot",
            "/api/agent/batch-assess",
            "/api/agent/auto-assess-all",
            "/api/agent/proposals",
            "/api/agent/proposals-generate",
            "/api/agent/verification-tasks-sync",
            "/api/agent/verification-complete",
            "/api/agent/proposal-preflight",
            "/api/agent/proposal-decide",
            "/api/agent/config/public",
            "/api/agent/skills",
            "/api/agent/skills/execute",
            "/api/agent/copilot/session",
            "/api/agent/copilot/sessions",
            "/api/agent/goals",
            "/api/agent/goal-templates",
            "/api/agent/workflows/",
            "/api/agent/steps/",
            "/api/agent/approvals/",
            "/api/agent/artifacts/",
            "/api/agent/events",
            "/api/agent/memories",
            "/api/agent/memory/revoke",
            "/api/flow/inbox",
            "/api/flow/item",
            "/asa-floating",
            "/api/asa/floating/state",
            "/api/asa/floating/context",
            "/api/asa/floating/command",
            "/api/asa/floating/commands",
            'parsed.path in {"/asa", "/a-system"}',
        ]:
            self.assertIn(route, server)
        builder = open(
            "/Users/messi/Documents/Codex/2026-06-26/re/work/build_talent_workbench.py",
            encoding="utf-8",
        ).read()
        for marker in [
            'id="agentWorkbenchSlot"',
            "hydrateAgentWorkbench()",
            "scheduleAgentVerificationTaskSync",
            "开放核验任务",
            "自动创建内部核验任务",
            "data-agent-wb-refresh",
            "agent-auto-progress",
            "待自动评估",
            "运行记录",
            "deepseek-v4-pro",
            'id="agentExtensionRail"',
            'id="agentFutureWorkspaceSlot"',
            "a-system-agent-surfaces-ready",
            'id="agentGoalWorkspace"',
            "hydrateAgentGoals()",
            "renderAgentGoalWorkspace()",
            "data-approval-approve",
            "等待渠道结果",
            "AGENT_AUTO_ASSESS_LIMIT = 50",
            "runAgentWorkbenchQueue",
            "setAgentInboxCardState",
            "agent-card-spinner",
            'id="agentCopilotPanel"',
            "data-agent-analytics-tab",
            "岗位漏斗",
            "渠道效率",
            "channel-efficiency-table",
            "渠道归因",
            "review_pass_rate",
            "AGENT_DASHBOARD_POLL_MS = 30000",
            "refreshVisibleAgentDashboard",
            "Promise.all([hydrateAgentWorkbench(), hydrateAgentDashboard()])",
            "analysis-stage-strip",
            "candidate_reply_avg_hours",
            "score_distribution",
            "latest_failure_rate",
            "反馈效率",
            "ASA 质量",
            "data-agent-verification",
            "提交核验结果",
            "verification-complete",
            "开放内部任务",
            "candidate-detail-tabs",
            "candidate-compact-list",
            "renderCandidateResume",
            "renderCandidateRecords",
            "data-candidate-ask-asa",
            "$('#candidates')?.classList.contains('active')",
            "tabId === 'candidates' && wechatState.selectedTalent",
            "page_active:$('#candidates')?.classList.contains('active')",
        ]:
            self.assertIn(marker, builder)
        self.assertNotIn("data-agent-chat", builder)
        self.assertNotIn('<strong>匹配与核验</strong>', builder)
        self.assertNotIn("setTimeout(hydrateAgentWorkbench, 900)", builder)
        self.assertNotIn("<b>Agent 收件箱</b>", builder)


if __name__ == "__main__":
    unittest.main()
