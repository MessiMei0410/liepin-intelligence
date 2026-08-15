"""批量停止推进（candidate_batch_stop）回归守护。

覆盖：
1. _requests_batch_stop 意图判定——分级过滤 + 明确停止措辞才触发；
   普通名单/分级名单不触发写库。
2. apply_batch_stop 落库口径——H5 初筛不通过、X-SaaS raw_status 区分、
   candidates.status 同步、candidate_events 审计、幂等跳过已停止。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.batch_stop import (  # noqa: E402
    apply_batch_stop,
    batch_stop_summary,
    build_batch_stop_items,
)
from a_system_agent.copilot_intent import _requests_batch_stop  # noqa: E402
from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from asa_core.app import create_app  # noqa: E402
from asa_core.database import MIGRATIONS, ensure_idempotency_recovery_schema  # noqa: E402


def _make_db() -> Path:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE job_candidates (
            id INTEGER PRIMARY KEY, person_id INTEGER, job_id INTEGER,
            clean_stage TEXT, flow_bucket TEXT, raw_status TEXT, raw_stage TEXT,
            clean_reason TEXT, stop_reason TEXT, source_candidate_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, status TEXT, notes TEXT, updated_at TEXT);
        CREATE TABLE candidate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER,
            person_id INTEGER, job_id INTEGER, event_type TEXT, event_status TEXT,
            event_time TEXT, summary TEXT, raw_json TEXT, source_table TEXT
        );
        INSERT INTO job_candidates (id, person_id, job_id, clean_stage, flow_bucket, source_candidate_id) VALUES
            (1, 10, 137, 'S1 新增寻访/待复核', '待复核', '100'),
            (2, 11, 137, 'X1 X-SaaS新增/待复核', '待复核', '101'),
            (3, 12, 137, 'H5 最近寻访/初筛不通过', '最近寻访', '102');
        INSERT INTO candidates (id, status, notes) VALUES (100, 'new', ''), (101, 'new', '');
        """
    )
    conn.commit()
    conn.close()
    return Path(path)


class BatchStopTest(unittest.TestCase):
    def test_requests_batch_stop_classification(self) -> None:
        self.assertTrue(_requests_batch_stop("把所有候选人过滤一下，再看下匹配度，不匹配的就停止推进，然后再给我名单"))
        self.assertTrue(_requests_batch_stop("把不匹配的停掉，再给我名单"))
        self.assertFalse(_requests_batch_stop("过滤一下候选人，按匹配度给名单"))
        self.assertFalse(_requests_batch_stop("把岗位 137 的名单给我"))

    def test_build_items_and_summary(self) -> None:
        items = build_batch_stop_items(
            {
                "candidates": [
                    {"id": 1, "name": "甲", "company": "A", "title": "电气工程师", "grade": "X-排除", "reason": "方向不符"},
                    {"id": 2, "name": "乙", "company": "B", "title": "研发经理", "grade": "X-排除", "reason": "经理"},
                    {"id": 3, "name": "丙", "company": "C", "title": "机械工程师", "grade": "D-无证据", "reason": "无证据"},
                    {"id": 4, "name": "丁", "company": "D", "title": "机械工程师", "grade": "A-强", "reason": "硬证据"},
                ]
            }
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["stop_reason"], "direction_mismatch")
        self.assertEqual(items[1]["stop_reason"], "too_senior")
        self.assertEqual(items[2]["stop_reason"], "other")
        self.assertIn("资历过高", batch_stop_summary(items))

    def test_apply_batch_stop_is_idempotent_and_audited(self) -> None:
        db = _make_db()
        items = [
            {"jc_id": 1, "name": "甲", "title": "电气工程师", "grade": "X-排除", "stop_reason": "direction_mismatch", "stop_reason_label": "方向不符", "note": "方向不符"},
            {"jc_id": 2, "name": "乙", "title": "软件工程师", "grade": "X-排除", "stop_reason": "direction_mismatch", "stop_reason_label": "方向不符", "note": "方向不符"},
            {"jc_id": 3, "name": "丙", "title": "机械工程师", "grade": "D-无证据", "stop_reason": "other", "stop_reason_label": "其他", "note": "无证据"},
        ]
        try:
            first = apply_batch_stop(str(db), 137, items)
            self.assertEqual(first["applied"], 2)
            self.assertEqual(first["skipped"], 1)
            self.assertEqual(first["events"], 2)

            second = apply_batch_stop(str(db), 137, items)
            self.assertEqual(second["applied"], 0)
            self.assertEqual(second["skipped"], 3)
            self.assertEqual(second["events"], 0)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            rows = {row["id"]: row for row in conn.execute("SELECT id, clean_stage, raw_status, stop_reason FROM job_candidates")}
            self.assertEqual(rows[1]["clean_stage"], "H5 最近寻访/初筛不通过")
            self.assertEqual(rows[1]["raw_status"], "screen_rejected")
            self.assertEqual(rows[2]["raw_status"], "xsaas_review_stop")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM candidate_events WHERE source_table='copilot_batch_stop'").fetchone()[0], 2)
            conn.close()
        finally:
            db.unlink()


class CopilotBatchStopIntegrationTest(AgentDbCase):
    """端到端：AgentService.copilot 收到“过滤 + 停止推进”时应真正批量落库。"""

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        for column in ("clean_reason", "stop_reason"):
            try:
                conn.execute(f"ALTER TABLE job_candidates ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT INTO people (id, display_name, current_company, current_title, city, education, experience) "
            "VALUES (21, '李工', '某电气公司', '电气工程师', '上海', '本科', '8年')"
        )
        conn.execute(
            "INSERT INTO candidates (id, name, company, title, education, experience, skills, city, client, position, status, notes, updated_at) "
            "VALUES (41, '李工', '某电气公司', '电气工程师', '本科', '8年', '', '上海', '长越科技', '机械高级工程师', 'new', '', '2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id, job_id, person_id, raw_client, raw_position, raw_status, raw_stage, clean_stage, flow_bucket, updated_at, source_candidate_id) "
            "VALUES (31, 10, 21, '长越科技', '机械高级工程师', 'new', '', 'S1 新增寻访/待复核', '待复核', '2026-07-14', '41')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles "
            "(id, candidate_id, candidate_name, candidate_company, client, position, education_level, seniority, industry_tags_json, function_tags_json, risk_tags_json, profile_summary, updated_at) "
            "VALUES (51, 41, '李工', '某电气公司', '长越科技', '机械高级工程师', '本科', '8年', '[]', '[]', '[]', '电气控制柜设计', '2026-07-14')"
        )
        conn.commit()
        conn.close()

    def test_copilot_batch_stop_requires_confirmation_then_writes(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        result = service.copilot(
            "把所有候选人过滤一下，再看下匹配度，不匹配的就停止推进，然后再给我名单",
            session_id="batch-stop-test",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {row["id"]: row for row in conn.execute("SELECT id, clean_stage, stop_reason FROM job_candidates WHERE id IN (30, 31)")}
        conn.close()

        self.assertEqual(rows[31]["clean_stage"], "S1 新增寻访/待复核")
        self.assertNotIn("初筛不通过", str(rows[30]["clean_stage"] or ""))
        self.assertIn("当前尚未写入", str(result.get("answer") or ""))
        command = result.get("pending_command") or {}
        self.assertEqual(command.get("status"), "pending")
        self.assertEqual(int((command.get("impact") or {}).get("affected_count") or 0), 1)

        preflight = service.preflight_copilot_command(str(command["command_id"]))
        decision = service.decide_copilot_command(
            str(command["command_id"]),
            decision="approve",
            confirmation_token=str(preflight["confirmation_token"]),
        )
        self.assertEqual(int((decision.get("receipt") or {}).get("succeeded") or 0), 1)
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=31").fetchone()
        conn.close()
        self.assertEqual(row[0], "H5 最近寻访/初筛不通过")
        self.assertEqual(service.get_copilot_context_state("batch-stop-test").get("pending_command"), {})
        restored = service.get_copilot_session("batch-stop-test")
        assistant = next(message for message in reversed(restored["messages"]) if message["role"] == "assistant")
        self.assertIsNone(assistant.get("pending_command"))
        self.assertTrue((assistant.get("execution_receipt") or {}).get("verified"))

        replayed = service.decide_copilot_command(
            str(command["command_id"]),
            decision="approve",
            confirmation_token=str(preflight["confirmation_token"]),
        )
        self.assertTrue(replayed.get("replayed"))
        self.assertEqual(replayed.get("receipt"), decision.get("receipt"))
        conn = sqlite3.connect(self.db_path)
        duplicate_count = conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE job_candidate_id=31 AND event_type='resume_review_completed'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(duplicate_count, 1)

    def test_short_confirmation_only_executes_immediately_previous_command(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        initial = service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-short-confirm",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        self.assertTrue((initial.get("pending_command") or {}).get("command_id"))
        confirmed = service.copilot(
            "可以",
            session_id="batch-stop-short-confirm",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        self.assertTrue((confirmed.get("execution_receipt") or {}).get("verified"))
        self.assertEqual((confirmed.get("interaction_card") or {}).get("action_label"), "停止推进候选人")

        service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-intervened",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        service.copilot(
            "现在名单里有多少人？",
            session_id="batch-stop-intervened",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        after_question = service.copilot(
            "可以",
            session_id="batch-stop-intervened",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        self.assertFalse(bool((after_question.get("execution_receipt") or {}).get("verified")))

    def test_restored_expired_command_refreshes_once_without_business_write(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        created = service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-refresh",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        command = dict(created.get("pending_command") or {})
        self.assertTrue(command.get("command_id"))
        self.assertEqual((created.get("interaction_card") or {}).get("state"), "pending")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE agent_copilot_commands SET expires_at='2000-01-01 00:00:00' WHERE command_id=?", (command["command_id"],))
        conn.commit()
        conn.close()

        refreshed = service.refresh_copilot_command(
            str(command["command_id"]), request_id="refresh-command-1",
            expected_command_hash=str(command["command_hash"]),
        )
        replacement = dict(refreshed.get("command") or {})
        self.assertTrue(refreshed.get("refreshed"))
        self.assertNotEqual(replacement.get("command_id"), command.get("command_id"))
        replay = service.refresh_copilot_command(
            str(command["command_id"]), request_id="refresh-command-1",
            expected_command_hash=str(command["command_hash"]),
        )
        self.assertEqual((replay.get("command") or {}).get("command_id"), replacement.get("command_id"))
        conn = sqlite3.connect(self.db_path)
        stage = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=31").fetchone()[0]
        command_count = conn.execute("SELECT COUNT(*) FROM agent_copilot_commands WHERE session_id='batch-stop-refresh'").fetchone()[0]
        conn.close()
        self.assertEqual(stage, "S1 新增寻访/待复核")
        self.assertEqual(command_count, 2)
        restored = service.get_copilot_session("batch-stop-refresh")
        assistant = next(message for message in reversed(restored["messages"]) if message["role"] == "assistant")
        self.assertEqual((assistant.get("pending_command") or {}).get("command_id"), replacement.get("command_id"))
        self.assertEqual((assistant.get("interaction_card") or {}).get("state"), "pending")
        user = next(message for message in reversed(restored["messages"]) if message["role"] == "user")
        self.assertIsNone(user.get("pending_command"))

    def test_concurrent_refresh_reuses_one_replacement_command(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        created = service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-refresh-concurrent",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        command = dict(created.get("pending_command") or {})
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE agent_copilot_commands SET expires_at='2000-01-01 00:00:00' WHERE command_id=?",
            (command["command_id"],),
        )
        conn.commit()
        conn.close()
        barrier = threading.Barrier(2)

        def refresh() -> dict:
            barrier.wait()
            return service.refresh_copilot_command(
                str(command["command_id"]),
                request_id="refresh-command-concurrent",
                expected_command_hash=str(command["command_hash"]),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: refresh(), range(2)))
        replacement_ids = {(result.get("command") or {}).get("command_id") for result in results}
        self.assertEqual(len(replacement_ids), 1)
        conn = sqlite3.connect(self.db_path)
        command_count = conn.execute(
            "SELECT COUNT(*) FROM agent_copilot_commands WHERE session_id='batch-stop-refresh-concurrent'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(command_count, 2)

    def test_intervening_turn_supersedes_command_condition_version(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        initial = service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-version-drift",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        command_id = str((initial.get("pending_command") or {}).get("command_id") or "")
        self.assertTrue(command_id)
        service.copilot(
            "先说下现在有多少人？",
            session_id="batch-stop-version-drift",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        with self.assertRaisesRegex(ValueError, "superseded"):
            service.preflight_copilot_command(command_id)
        self.assertEqual(service.get_copilot_command(command_id)["command"]["status"], "superseded")

    def test_command_http_preflight_and_decision_routes(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        result = service.copilot(
            "把所有候选人过滤一下，不匹配的就停止推进，再给我名单",
            session_id="batch-stop-http",
            context={"type": "job", "id": 10, "page": "positions"},
        )
        command_id = str((result.get("pending_command") or {}).get("command_id") or "")
        self.assertTrue(command_id)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(MIGRATIONS[0][2])
        conn.row_factory = sqlite3.Row
        ensure_idempotency_recovery_schema(conn)
        conn.commit()
        conn.close()

        with patch("asa_core.app.migrate", return_value={"applied": []}), TestClient(
            create_app(db_path=Path(self.db_path), start_legacy=False)
        ) as client:
            loaded = client.get(f"/api/v1/copilot/commands/{command_id}")
            self.assertEqual(loaded.status_code, 200)
            preflight = client.post(
                f"/api/v1/copilot/commands/{command_id}/preflight",
                json={"request_id": "command-http-preflight"},
            )
            self.assertEqual(preflight.status_code, 200)
            decision_body = {
                "request_id": "command-http-decision",
                "decision": "approve",
                "confirmation_token": preflight.json()["confirmation_token"],
            }
            decision = client.post(
                f"/api/v1/copilot/commands/{command_id}/decision",
                headers={"Idempotency-Key": "command-http-decision-key"},
                json=decision_body,
            )
            self.assertEqual(decision.status_code, 200)
            self.assertTrue(decision.json()["receipt"]["verified"])
            replay = client.post(
                f"/api/v1/copilot/commands/{command_id}/decision",
                headers={"Idempotency-Key": "command-http-decision-key"},
                json=decision_body,
            )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json()["receipt"]["verified"])
            self.assertTrue(replay.json()["receipt"]["idempotent_replay"])


class CopilotBatchStopUnsupportedDomainGuardTest(AgentDbCase):
    """未支持的职能域（如电气）必须退化为只读名单，绝不自动批量停止整池。"""

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE jobs SET title='电气高级工程师' WHERE id=10")
        conn.execute(
            "INSERT INTO people (id, display_name, current_company, current_title, city, education, experience) "
            "VALUES (22, '王工', '某电气公司', '电气工程师', '上海', '本科', '6年')"
        )
        conn.execute(
            "INSERT INTO candidates (id, name, company, title, education, experience, skills, city, client, position, status, notes, updated_at) "
            "VALUES (42, '王工', '某电气公司', '电气工程师', '本科', '6年', '', '上海', '长越科技', '电气高级工程师', 'new', '', '2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id, job_id, person_id, raw_client, raw_position, raw_status, raw_stage, clean_stage, flow_bucket, updated_at, source_candidate_id) "
            "VALUES (32, 10, 22, '长越科技', '电气高级工程师', 'new', '', 'S1 新增寻访/待复核', '待复核', '2026-07-14', '42')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles "
            "(id, candidate_id, candidate_name, candidate_company, client, position, education_level, seniority, industry_tags_json, function_tags_json, risk_tags_json, profile_summary, updated_at) "
            "VALUES (52, 42, '王工', '某电气公司', '长越科技', '电气高级工程师', '本科', '6年', '[]', '[]', '[]', '电气控制柜设计', '2026-07-14')"
        )
        conn.commit()
        conn.close()

    def test_unsupported_domain_does_not_batch_stop(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        result = service.copilot(
            "把所有候选人过滤一下，再看下匹配度，不匹配的就停止推进，然后再给我名单",
            session_id="batch-stop-electrical-test",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT clean_stage FROM job_candidates WHERE id=32").fetchone()
        conn.close()

        self.assertNotIn("初筛不通过", str(row[0] or ""))
        self.assertIsNone(result.get("batch_stop_receipt"))
        self.assertNotIn("已执行批量停止推进", str(result.get("answer") or ""))


class CopilotBatchStopSoftwareTest(AgentDbCase):
    """软件岗：软件候选人保留，机械等职能不符候选人被批量停止。"""

    def setUp(self) -> None:
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        for column in ("clean_reason", "stop_reason"):
            try:
                conn.execute(f"ALTER TABLE job_candidates ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
        conn.execute("UPDATE jobs SET title='自动化软件高级工程师' WHERE id=10")
        # 软件匹配候选人
        conn.execute(
            "INSERT INTO people (id, display_name, current_company, current_title, city, education, experience) "
            "VALUES (22, '王工', '某软件公司', 'C++软件工程师', '上海', '本科', '6年')"
        )
        conn.execute(
            "INSERT INTO candidates (id, name, company, title, education, experience, skills, city, client, position, status, notes, updated_at) "
            "VALUES (42, '王工', '某软件公司', 'C++软件工程师', '本科', '6年', '', '上海', '长越科技', '自动化软件高级工程师', 'new', '', '2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id, job_id, person_id, raw_client, raw_position, raw_status, raw_stage, clean_stage, flow_bucket, updated_at, source_candidate_id) "
            "VALUES (32, 10, 22, '长越科技', '自动化软件高级工程师', 'new', '', 'S1 新增寻访/待复核', '待复核', '2026-07-14', '42')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles "
            "(id, candidate_id, candidate_name, candidate_company, client, position, education_level, seniority, industry_tags_json, function_tags_json, risk_tags_json, profile_summary, updated_at) "
            "VALUES (52, 42, '王工', '某软件公司', '长越科技', '自动化软件高级工程师', '本科', '6年', '[]', '[]', '[]', '运动控制 C++ 软件开发', '2026-07-14')"
        )
        # 机械职能不符候选人
        conn.execute(
            "INSERT INTO people (id, display_name, current_company, current_title, city, education, experience) "
            "VALUES (23, '李工', '某机械公司', '机械工程师', '上海', '本科', '8年')"
        )
        conn.execute(
            "INSERT INTO candidates (id, name, company, title, education, experience, skills, city, client, position, status, notes, updated_at) "
            "VALUES (43, '李工', '某机械公司', '机械工程师', '本科', '8年', '', '上海', '长越科技', '自动化软件高级工程师', 'new', '', '2026-07-14')"
        )
        conn.execute(
            "INSERT INTO job_candidates "
            "(id, job_id, person_id, raw_client, raw_position, raw_status, raw_stage, clean_stage, flow_bucket, updated_at, source_candidate_id) "
            "VALUES (33, 10, 23, '长越科技', '自动化软件高级工程师', 'new', '', 'S1 新增寻访/待复核', '待复核', '2026-07-14', '43')"
        )
        conn.execute(
            "INSERT INTO candidate_profiles "
            "(id, candidate_id, candidate_name, candidate_company, client, position, education_level, seniority, industry_tags_json, function_tags_json, risk_tags_json, profile_summary, updated_at) "
            "VALUES (53, 43, '李工', '某机械公司', '长越科技', '自动化软件高级工程师', '本科', '8年', '[]', '[]', '[]', '机械结构设计', '2026-07-14')"
        )
        conn.commit()
        conn.close()

    def test_software_job_keeps_software_and_stops_mechanical(self) -> None:
        service = AgentService(self.db_path, FakeLLM(fake_assessment()))
        self.addCleanup(service.close)
        result = service.copilot(
            "把所有候选人过滤一下，再看下匹配度，不匹配的就停止推进，然后再给我名单",
            session_id="batch-stop-software-keep-test",
            context={"type": "job", "id": 10, "page": "positions"},
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {row["id"]: row for row in conn.execute("SELECT id, clean_stage FROM job_candidates WHERE id IN (32, 33)")}
        conn.close()

        self.assertNotIn("初筛不通过", str(rows[32]["clean_stage"] or ""))
        self.assertNotIn("初筛不通过", str(rows[33]["clean_stage"] or ""))
        command = result.get("pending_command") or {}
        preflight = service.preflight_copilot_command(str(command["command_id"]))
        confirmed = service.decide_copilot_command(
            str(command["command_id"]),
            decision="approve",
            confirmation_token=str(preflight["confirmation_token"]),
        )
        self.assertEqual(int((confirmed.get("receipt") or {}).get("succeeded") or 0), 2)
        conn = sqlite3.connect(self.db_path)
        rows = {row[0]: row[1] for row in conn.execute("SELECT id, clean_stage FROM job_candidates WHERE id IN (32, 33)")}
        conn.close()
        self.assertNotIn("初筛不通过", str(rows[32] or ""))
        self.assertEqual(rows[33], "H5 最近寻访/初筛不通过")


if __name__ == "__main__":
    unittest.main()
