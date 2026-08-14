"""寻访入库预筛回归守护。

覆盖：
1. 共享裁定 intake_mismatch_verdict 的机械/软件职能判定；
2. 边界：管理层级、资历、未知职能域不预筛（留给人工复核）；
3. 独立脚本 a_system_multichannel 内联版本与共享版本保持同口径（防漂移）。
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from _local import env_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(env_path("ASA_MULTICHANNEL_SCRIPTS", Path("/Users/messi/.codex/skills/multi-channel-search/scripts"))))

from a_system_agent.candidate_pool_filter import intake_mismatch_verdict  # noqa: E402


class IntakePrescreenTest(unittest.TestCase):
    def test_mechanical_domain_flags_non_mechanical_functions(self) -> None:
        self.assertIsNotNone(intake_mismatch_verdict("机械高级工程师", "电气工程师"))
        self.assertIsNotNone(intake_mismatch_verdict("机械高级工程师", "软件工程师"))
        self.assertIsNotNone(intake_mismatch_verdict("机械高级工程师", "功能测试工程师"))
        self.assertIsNone(intake_mismatch_verdict("机械高级工程师", "机械设计工程师"))

    def test_software_domain_flags_non_software_functions(self) -> None:
        self.assertIsNotNone(intake_mismatch_verdict("自动化软件高级工程师", "机械工程师"))
        self.assertIsNotNone(intake_mismatch_verdict("自动化软件高级工程师", "电气工程师"))
        self.assertIsNotNone(intake_mismatch_verdict("自动化软件高级工程师", "工艺工程师"))
        self.assertIsNone(intake_mismatch_verdict("自动化软件高级工程师", "C++软件工程师"))

    def test_management_and_unknown_domain_not_prescreened(self) -> None:
        # 管理层级/资历过高交给人工复核，入库预筛只处理“方向不符”
        self.assertIsNone(intake_mismatch_verdict("机械高级工程师", "研发经理"))
        self.assertIsNone(intake_mismatch_verdict("自动化软件高级工程师", "技术总监"))
        # 未支持的职能域不预筛
        self.assertIsNone(intake_mismatch_verdict("电气高级工程师", "机械工程师"))
        self.assertIsNone(intake_mismatch_verdict("高级失效分析工程师", "电气工程师"))

    def test_verdict_payload(self) -> None:
        verdict = intake_mismatch_verdict("自动化软件高级工程师", "电气工程师")
        self.assertEqual(verdict["stage"], "H5 最近寻访/初筛不通过")
        self.assertEqual(verdict["flow_bucket"], "最近寻访")
        self.assertEqual(verdict["stop_reason"], "direction_mismatch")
        self.assertIn("方向不符", verdict["reason"])

    def test_skill_script_inline_version_matches_shared(self) -> None:
        try:
            module = importlib.import_module("a_system_multichannel")
        except ImportError as exc:
            self.skipTest(f"本机 skill 脚本缺失: {exc}")
        for job_title, candidate_title in [
            ("机械高级工程师", "电气工程师"),
            ("机械高级工程师", "机械设计工程师"),
            ("自动化软件高级工程师", "机械工程师"),
            ("自动化软件高级工程师", "C++软件工程师"),
            ("电气高级工程师", "机械工程师"),
        ]:
            self.assertEqual(
                intake_mismatch_verdict(job_title, candidate_title),
                module._intake_mismatch_verdict(job_title, candidate_title),
                (job_title, candidate_title),
            )


class MultiChannelIntakePrescreenIntegrationTest(unittest.TestCase):
    """a_system_multichannel.apply_intake 入库时把明确职能不符的写成 H5，而非待复核。"""

    def _make_db(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY, name TEXT, company TEXT, title TEXT,
                client TEXT, position TEXT, status TEXT, notes TEXT, xsaas_id TEXT, iteration INTEGER
            );
            CREATE TABLE people (
                id INTEGER PRIMARY KEY, fingerprint TEXT, display_name TEXT,
                current_company TEXT, current_title TEXT, city TEXT, education TEXT, experience TEXT
            );
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER, raw_position TEXT,
                raw_status TEXT, raw_stage TEXT, clean_stage TEXT, flow_bucket TEXT,
                clean_reason TEXT, stop_reason TEXT, source_candidate_id TEXT
            );
            CREATE TABLE candidate_events (
                id INTEGER PRIMARY KEY, job_candidate_id INTEGER, person_id INTEGER, job_id INTEGER,
                event_type TEXT, event_status TEXT, event_time TEXT, summary TEXT,
                raw_json TEXT, source_table TEXT, source_id TEXT
            );
            CREATE TABLE candidate_clients (id INTEGER PRIMARY KEY);
            CREATE TABLE candidate_profiles (id INTEGER PRIMARY KEY);
            CREATE TABLE candidate_intelligence (
                id INTEGER PRIMARY KEY, candidate_id INTEGER, next_action TEXT, recommendation_decision TEXT
            );
            """
        )
        conn.commit()
        conn.close()
        return path

    @staticmethod
    def _candidate(name: str, company: str, title: str, channel: str) -> dict:
        return {
            "channel": channel,
            "source": channel,
            "name": name,
            "company": company,
            "title": title,
            "education": "本科",
            "experience": "6年",
            "city": "上海",
            "profile_text": "运动控制 C++ 软件开发",
            "full_text": "",
            "work_text": "",
            "project_text": "",
            "education_text": "",
            "source_url": "https://example.com/resume",
            "source_candidate_id": "",
            "source_query": "运动控制 C++",
            "xsaas_id": "",
            "stage": "S1 新增寻访/待复核" if channel == "liepin" else "X1 X-SaaS新增/待复核",
            "raw_status": "search_shortlisted" if channel == "liepin" else "xsaas_search_shortlisted",
            "event_status": "pending_review",
            "flow_bucket": "待复核",
            "raw": {"title": title, "company": company},
        }

    def test_apply_intake_prescreens_mismatched_title(self) -> None:
        try:
            module = importlib.import_module("a_system_multichannel")
        except ImportError as exc:
            self.skipTest(f"本机 skill 脚本缺失: {exc}")
        db = self._make_db()
        context = {"job_id": 10, "client": "长越科技", "job": "自动化软件高级工程师", "ability_keywords": ["C++", "运动控制"]}
        candidates = [
            self._candidate("王工", "某软件公司", "C++软件工程师", "liepin"),
            self._candidate("李工", "某机械公司", "机械工程师", "liepin"),
        ]
        try:
            result = module.apply_intake(db, context, candidates, apply=True)
            self.assertEqual(result["inserted"], 2)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = [row for row in conn.execute("SELECT * FROM job_candidates WHERE job_id=10")]
            cands = {row["id"]: row for row in conn.execute("SELECT id, name, status, notes FROM candidates WHERE client='长越科技'")}
            conn.close()
            mechanical = next(r for r in rows if cands[int(r["source_candidate_id"])]["name"] == "李工")
            software = next(r for r in rows if cands[int(r["source_candidate_id"])]["name"] == "王工")
            mechanical_cid = int(mechanical["source_candidate_id"])
            software_cid = int(software["source_candidate_id"])
            self.assertEqual(mechanical["clean_stage"], "H5 最近寻访/初筛不通过")
            self.assertEqual(mechanical["stop_reason"], "direction_mismatch")
            self.assertEqual(software["clean_stage"], "S1 新增寻访/待复核")
            self.assertEqual(cands[mechanical_cid]["status"], "screen_rejected")
            self.assertEqual(cands[software_cid]["status"], "new")
        finally:
            Path(db).unlink()


if __name__ == "__main__":
    unittest.main()
