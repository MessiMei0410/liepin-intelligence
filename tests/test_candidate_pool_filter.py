"""候选池职能域、评估复用与覆盖口径回归测试。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.candidate_pool_filter import (  # noqa: E402
    filter_job_candidates,
    format_grade_list,
    job_filter_domain,
)
from a_system_agent.copilot_tools import execute_filter_candidates  # noqa: E402


class CandidatePoolFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = path
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT,
                summary TEXT, hard_requirements TEXT, ability_keywords TEXT,
                search_words TEXT, exclusions TEXT
            );
            CREATE TABLE people (
                id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
                current_title TEXT, city TEXT, education TEXT, experience TEXT
            );
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
                clean_stage TEXT, flow_bucket TEXT, source_candidate_id TEXT,
                updated_at TEXT
            );
            CREATE TABLE candidate_profiles (
                candidate_id INTEGER, candidate_name TEXT, candidate_company TEXT,
                position TEXT, education_level TEXT, seniority TEXT, profile_summary TEXT
            );
            CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE agent_candidate_assessments (
                id INTEGER PRIMARY KEY, run_id TEXT, job_candidate_id INTEGER,
                fit_score INTEGER, fit_level TEXT, recommendation TEXT,
                confidence REAL, evidence_coverage REAL, strengths_json TEXT,
                gaps_json TEXT, is_current INTEGER, created_at TEXT
            );

            INSERT INTO clients VALUES (1, '测试客户');
            INSERT INTO jobs VALUES (
                142, 1, '电源专家',
                '负责 VPD/VRM 垂直供电模块、多相Buck/TLVR控制与建模',
                '模块电源研发，掌握DrMOS与磁件规格', 'VPD;VRM;TLVR',
                'VPD 垂直供电 TLVR', '纯机械方向排除'
            );
            INSERT INTO jobs VALUES (144, 1, '客户成功经理', '负责客户交付', '', '', '', '');

            INSERT INTO people VALUES
                (1, '机械候选人', '精密设备公司', '高级机械工程师', '上海', '本科', '8年'),
                (2, '电源候选人', '模块电源公司', '模块电源研发工程师', '杭州', '硕士', '10年'),
                (3, '已评估A级', '服务器公司', '资深电源研发', '杭州', '本科', '12年'),
                (4, '已评估D级', '电子公司', '电源工程师', '苏州', '本科', '6年');
            INSERT INTO job_candidates VALUES
                (1001, 142, 1, 'S1 新增寻访/待复核', '待复核', '201', '2026-08-17'),
                (1002, 142, 2, 'S1 新增寻访/待复核', '待复核', '202', '2026-08-17'),
                (1003, 142, 3, 'S1 新增寻访/待复核', '待复核', '203', '2026-08-17'),
                (1004, 142, 4, 'S1 新增寻访/待复核', '待复核', '204', '2026-08-17');
            INSERT INTO candidate_profiles VALUES
                (201, '机械候选人', '精密设备公司', '机械高级工程师', '本科', '8年',
                 '精密机械，微米定位，有限元分析，模态振动，直线导轨'),
                (202, '电源候选人', '模块电源公司', '电源专家', '硕士', '10年',
                 '负责模块电源 VPD、VRM、多相Buck、TLVR、DrMOS及SIMPLIS建模'),
                (203, '已评估A级', '服务器公司', '电源专家', '本科', '12年', '画像正文待补'),
                (204, '已评估D级', '电子公司', '电源专家', '本科', '6年', '通用电源经历');

            INSERT INTO agent_runs VALUES ('run_old', 'completed'), ('run_a', 'completed'), ('run_d', 'completed');
            INSERT INTO agent_candidate_assessments VALUES
                (1, 'run_old', 1003, 20, 'D-暂缓', 'hold', 0.5, 0.5, '[]', '[]', 0, '2026-08-01'),
                (2, 'run_a', 1003, 94, 'A-优先推进', 'priority_review', 0.9, 0.9,
                 '["VPD垂直供电项目已量产", "多相VRM研发证据完整"]', '[]', 1, '2026-08-17'),
                (3, 'run_d', 1004, 42, 'D-暂缓', 'hold', 0.7, 0.7,
                 '["具备通用电源经验"]', '["缺少VPD项目"]', 1, '2026-08-17');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def test_domain_uses_title_and_jd_without_generic_development_false_positive(self) -> None:
        self.assertEqual(job_filter_domain("电源专家"), "power")
        self.assertEqual(job_filter_domain("研发专家", "负责VPD/VRM与多相Buck模块"), "power")
        self.assertEqual(job_filter_domain("自动化软件开发工程师"), "software")
        self.assertEqual(job_filter_domain("电源软件工程师"), "software")
        self.assertEqual(job_filter_domain("电源结构工程师"), "mechanical")
        self.assertIsNone(job_filter_domain("电气高级工程师", "精密机械设备经验"))
        self.assertIsNone(job_filter_domain("客户成功经理", "负责客户交付"))

    def test_power_filter_never_scores_mechanical_keywords_as_power_evidence(self) -> None:
        result = filter_job_candidates(self.db_path, 142)
        candidates = {item["id"]: item for item in result["candidates"]}

        self.assertEqual(result["domain"], "power")
        self.assertEqual(candidates[1001]["grade"], "X-排除")
        self.assertEqual(candidates[1001]["hard_hits"], [])
        self.assertEqual(candidates[1002]["grade"], "A-核心")
        self.assertIn("VPD", candidates[1002]["hard_hits"])
        self.assertNotIn("有限元", candidates[1002]["hard_hits"])

    def test_current_completed_agent_assessment_is_authoritative(self) -> None:
        result = filter_job_candidates(self.db_path, 142)
        candidates = {item["id"]: item for item in result["candidates"]}

        assessed_a = candidates[1003]
        self.assertEqual(assessed_a["grade"], "A-强")
        self.assertEqual(assessed_a["score"], 94)
        self.assertEqual(assessed_a["grade_source"], "agent_assessment")
        self.assertEqual(assessed_a["assessment_fit_level"], "A-优先推进")
        self.assertIn("VPD垂直供电项目已量产", assessed_a["hard_hits"])

        assessed_d = candidates[1004]
        self.assertEqual(assessed_d["grade"], "D-暂缓")
        self.assertNotEqual(assessed_d["grade"], "D-无证据")
        self.assertIn("42 分 / D-暂缓", assessed_d["reason"])

    def test_unknown_domain_fails_closed_instead_of_using_mechanical_defaults(self) -> None:
        with self.assertRaisesRegex(ValueError, "已停止自动分级"):
            filter_job_candidates(self.db_path, 144)
        tool_result = execute_filter_candidates(self.db_path, 144)
        self.assertFalse(tool_result["success"])
        self.assertIn("未识别或不支持岗位职能域", tool_result["error"])

    def test_full_pool_is_analyzed_while_detail_limit_is_explicit(self) -> None:
        result = filter_job_candidates(self.db_path, 142, max_candidates=2)

        self.assertEqual(result["pool_total"], 4)
        self.assertEqual(result["analyzed_total"], 4)
        self.assertEqual(result["returned_total"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(sum(result["grade_counts"].values()), 4)
        self.assertIn("已分析全部 4 条待分级关系", result["coverage_note"])
        self.assertIn("仅返回排序前 2 条明细", result["coverage_note"])

        formatted = format_grade_list(result)
        self.assertIn("待人工判断 1 人", formatted)
        self.assertIn("D-暂缓（共 1 人，本次展示 0 人）", formatted)

    def test_tool_contract_keeps_full_counts_and_marks_returned_details(self) -> None:
        result = execute_filter_candidates(self.db_path, 142, limit=2)
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(data["domain"], "power")
        self.assertEqual(data["analyzed_total"], 4)
        self.assertEqual(data["returned_total"], 2)
        self.assertTrue(data["truncated"])
        self.assertEqual(sum(data["summary"].values()), 4)
        self.assertEqual(sum(group["returned"] for group in data["groups"]), 2)
        self.assertTrue(any(group["truncated"] for group in data["groups"]))


if __name__ == "__main__":
    unittest.main()
