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
import unittest
from pathlib import Path

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

    def test_copilot_batch_stop_writes_and_receipts(self) -> None:
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

        self.assertEqual(rows[31]["clean_stage"], "H5 最近寻访/初筛不通过")
        self.assertNotIn("初筛不通过", str(rows[30]["clean_stage"] or ""))
        self.assertIn("已执行批量停止推进", str(result.get("answer") or ""))
        receipt = result.get("batch_stop_receipt") or {}
        self.assertEqual(int(receipt.get("applied") or 0), 1)


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
        self.assertEqual(rows[33]["clean_stage"], "H5 最近寻访/初筛不通过")
        self.assertEqual(int((result.get("batch_stop_receipt") or {}).get("applied") or 0), 2)


if __name__ == "__main__":
    unittest.main()
