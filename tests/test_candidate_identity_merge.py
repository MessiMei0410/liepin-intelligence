from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "liepin_workbench_server.py"
sys.path.insert(0, str(SERVER_PATH.parent))
spec = importlib.util.spec_from_file_location("liepin_workbench_server_identity_test", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server)


SCHEMA = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    current_company TEXT,
    current_title TEXT,
    city TEXT,
    education TEXT,
    experience TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT
);
CREATE TABLE source_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_candidate_id TEXT,
    source_date TEXT,
    raw_status TEXT,
    raw_client TEXT,
    raw_position TEXT,
    raw_json TEXT NOT NULL
);
CREATE TABLE job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    person_id INTEGER NOT NULL,
    raw_client TEXT,
    raw_position TEXT,
    raw_status TEXT,
    raw_stage TEXT,
    clean_stage TEXT,
    flow_bucket TEXT,
    clean_reason TEXT,
    recent_hunting INTEGER DEFAULT 0,
    search_date TEXT,
    updated_at TEXT,
    source_candidate_id TEXT,
    UNIQUE(job_id, person_id, raw_position, source_candidate_id)
);
CREATE TABLE candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER,
    person_id INTEGER,
    job_id INTEGER,
    event_type TEXT NOT NULL,
    event_status TEXT,
    event_time TEXT,
    summary TEXT,
    raw_json TEXT DEFAULT '{}',
    source_table TEXT,
    source_id TEXT
);
CREATE TABLE candidates (
    id INTEGER PRIMARY KEY,
    name TEXT,
    company TEXT,
    title TEXT,
    client TEXT,
    position TEXT,
    status TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT,
    source TEXT,
    xsaas_id TEXT
);
CREATE TABLE candidate_profiles (id INTEGER, candidate_id INTEGER);
CREATE TABLE candidate_intelligence (id INTEGER, candidate_id INTEGER);
CREATE TABLE candidate_replies (id INTEGER, candidate_id INTEGER);
CREATE TABLE outreach_events (id INTEGER, candidate_id INTEGER);
CREATE TABLE followup_tasks (id INTEGER, candidate_id INTEGER, job_candidate_id INTEGER);
CREATE TABLE client_feedback_events (id INTEGER, candidate_id INTEGER, job_candidate_id INTEGER);
"""


class CandidateIdentityMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        server.clear_candidate_merge_confirmations()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "talent.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def seed_people(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO people(id, display_name, current_company, current_title, fingerprint) VALUES (?, ?, ?, ?, ?)",
                [
                    (1, "张伟", "长越科技", "机械高级工程师", "张伟|长越科技|机械高级工程师"),
                    (2, "张**", "长越科技", "机械高级工程师", "张**|长越科技|机械高级工程师"),
                    (3, "张**", "另一家公司", "销售经理", "张**|另一家公司|销售经理"),
                ],
            )
            conn.execute(
                "INSERT INTO source_profiles(person_id, source_type, source_candidate_id, raw_json) VALUES (1, 'xsaas', '4681173', ?)",
                (json.dumps({"source_url": "https://headhunt.x-saas.com.cn/#/app/candidate/info/4681173"}),),
            )
            conn.execute(
                "INSERT INTO source_profiles(person_id, source_type, source_candidate_id, raw_json) VALUES (2, 'liepin', 'lp-8899', ?)",
                (json.dumps({"source_url": "https://h.liepin.com/resume/?res_id_encode=lp-8899"}),),
            )

    def test_strong_cross_source_match_is_suggested(self) -> None:
        self.seed_people()
        result = server.discover_candidate_identity_matches(
            {
                "source_type": "liepin",
                "source_candidate_id": "lp-new",
                "candidate": "张伟",
                "company": "长越科技",
                "title": "机械高级工程师",
            },
            db_path=self.db_path,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["person_id"], 1)
        self.assertEqual(result["matches"][0]["confidence"], "high")
        self.assertTrue(result["matches"][0]["merge_allowed"])

    def test_surname_honorific_matches_full_name_only_with_company_and_title(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO people(id, display_name, current_company, current_title, fingerprint) VALUES (?, ?, ?, ?, ?)",
                [
                    (506, "吴先生", "上海图双精密装备有限公司", "机械设计工程师", "吴先生|图双|机械"),
                    (512, "吴云涛", "上海图双精密装备有限公司", "机械设计工程师", "吴云涛|图双|机械"),
                ],
            )
        result = server.discover_candidate_identity_matches(
            {
                "source_type": "xsaas",
                "source_candidate_id": "3536463",
                "candidate": "吴云涛",
                "company": "上海图双精密装备有限公司",
                "title": "机械设计工程师",
                "current_person_id": 512,
            },
            db_path=self.db_path,
        )
        self.assertEqual(result["matches"][0]["person_id"], 506)
        self.assertTrue(result["matches"][0]["merge_allowed"])

        weak = server.discover_candidate_identity_matches(
            {
                "source_type": "xsaas",
                "source_candidate_id": "3536463",
                "candidate": "吴云涛",
                "company": "另一家公司",
                "title": "机械设计工程师",
                "current_person_id": 512,
            },
            db_path=self.db_path,
        )
        self.assertFalse(weak["matches"][0]["merge_allowed"])

    def test_masked_same_name_with_different_company_and_title_cannot_merge(self) -> None:
        self.seed_people()
        result = server.discover_candidate_identity_matches(
            {
                "source_type": "liepin",
                "source_candidate_id": "lp-new",
                "candidate": "张**",
                "company": "长越科技",
                "title": "机械高级工程师",
            },
            db_path=self.db_path,
        )
        wrong = next(item for item in result["matches"] if item["person_id"] == 3)
        self.assertFalse(wrong["merge_allowed"])
        self.assertNotEqual(wrong["confidence"], "high")

    def test_dry_run_returns_confirmation_token_without_mutating(self) -> None:
        self.seed_people()
        before = self.table_counts()
        result = server.merge_candidate_profiles(
            {
                "canonical_person_id": 1,
                "merged_person_id": 2,
                "source_profile": {
                    "source_type": "liepin",
                    "source_candidate_id": "lp-8899",
                    "source_url": "https://h.liepin.com/resume/?res_id_encode=lp-8899",
                    "candidate": "张**",
                    "company": "长越科技",
                    "title": "机械高级工程师",
                },
                "write": False,
            },
            db_path=self.db_path,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "ask")
        self.assertTrue(result["confirmation_token"])
        self.assertEqual(before, self.table_counts())

    def test_write_without_confirmation_token_is_denied(self) -> None:
        self.seed_people()
        result = server.merge_candidate_profiles(
            {"canonical_person_id": 1, "merged_person_id": 2, "write": True},
            db_path=self.db_path,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM people"), 3)

    def test_conflicting_same_job_review_decisions_are_denied(self) -> None:
        self.seed_people()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO job_candidates(
                       id, job_id, person_id, raw_position, clean_stage, source_candidate_id
                   ) VALUES (?, 9, ?, '机械高级工程师', '待复核', ?)""",
                [(11, 1, "101"), (12, 2, "202")],
            )
            conn.executemany(
                "INSERT INTO candidate_events(job_candidate_id, person_id, job_id, event_type, event_status) VALUES (?, ?, 9, 'resume_review', ?)",
                [(11, 1, "continue"), (12, 2, "stop")],
            )
        result = server.merge_candidate_profiles(
            {
                "canonical_person_id": 1,
                "merged_person_id": 2,
                "source_profile": {
                    "source_type": "liepin",
                    "source_candidate_id": "lp-8899",
                    "candidate": "张**",
                    "company": "长越科技",
                    "title": "机械高级工程师",
                },
                "write": False,
            },
            db_path=self.db_path,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "deny")
        self.assertIn("复核结论冲突", result["error"])

    def test_preflight_preserves_full_name_when_selected_target_is_masked(self) -> None:
        self.seed_people()
        result = server.merge_candidate_profiles(
            {
                "canonical_person_id": 2,
                "merged_person_id": 1,
                "source_profile": {
                    "source_type": "xsaas",
                    "source_candidate_id": "4681173",
                    "candidate": "张伟",
                    "company": "长越科技",
                    "title": "机械高级工程师",
                },
                "write": False,
            },
            db_path=self.db_path,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical"]["person_id"], 1)
        self.assertEqual(result["merged"]["person_id"], 2)

    def test_valid_token_merges_relations_events_local_candidates_and_audits(self) -> None:
        self.seed_people()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO job_candidates(
                       id, job_id, person_id, raw_client, raw_position, clean_stage, source_candidate_id
                   ) VALUES (?, 9, ?, '长越科技', '机械高级工程师', '待复核', ?)""",
                [(11, 1, "101"), (12, 2, "202")],
            )
            conn.execute(
                """INSERT INTO job_candidates(
                       id, job_id, person_id, raw_client, raw_position, clean_stage, source_candidate_id
                   ) VALUES (13, 10, 2, '另一客户', '另一机械岗位', '待复核', '203')"""
            )
            conn.executemany(
                "INSERT INTO candidate_events(id, job_candidate_id, person_id, job_id, event_type, event_status) VALUES (?, ?, ?, 9, 'resume_review', 'continue')",
                [(31, 11, 1), (32, 12, 2)],
            )
            conn.executemany(
                "INSERT INTO candidates(id, name, company, title, source, xsaas_id) VALUES (?, ?, '长越科技', '机械高级工程师', ?, ?)",
                [(101, "张伟", "xsaas", "4681173"), (202, "张**", "liepin", "")],
            )
            conn.execute(
                """INSERT INTO candidates(id, name, company, title, client, position, source, xsaas_id)
                   VALUES (203, '张**', '长越科技', '机械高级工程师', '另一客户', '另一机械岗位', 'liepin', '')"""
            )
            conn.execute("INSERT INTO candidate_profiles(id, candidate_id) VALUES (1, 202)")
            conn.execute("INSERT INTO followup_tasks(id, candidate_id, job_candidate_id) VALUES (1, 202, 12)")

        request = {
            "canonical_person_id": 1,
            "merged_person_id": 2,
            "source_profile": {
                "source_type": "liepin",
                "source_candidate_id": "lp-8899",
                "source_url": "https://h.liepin.com/resume/?res_id_encode=lp-8899",
                "candidate": "张**",
                "company": "长越科技",
                "title": "机械高级工程师",
            },
            "actor": "unit-test",
            "write": False,
        }
        preflight = server.merge_candidate_profiles(request, db_path=self.db_path)
        result = server.merge_candidate_profiles(
            {**request, "write": True, "confirmation_token": preflight["confirmation_token"]},
            db_path=self.db_path,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM people WHERE id = 2"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM source_profiles WHERE person_id = 1"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM job_candidates WHERE job_id = 9 AND person_id = 1"), 1)
        self.assertEqual(self.scalar("SELECT source_candidate_id FROM job_candidates WHERE id = 13"), "203")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM candidates WHERE id = 203"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM candidate_events WHERE person_id = 1"), 2)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM candidate_events WHERE job_candidate_id = 11"), 2)
        self.assertEqual(self.scalar("SELECT candidate_id FROM candidate_profiles WHERE id = 1"), 101)
        self.assertEqual(self.scalar("SELECT job_candidate_id FROM followup_tasks WHERE id = 1"), 11)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM candidates WHERE id = 202"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM candidate_merge_audit"), 1)

    def table_counts(self) -> dict[str, int]:
        return {
            table: self.scalar(f"SELECT COUNT(*) FROM {table}")
            for table in ["people", "source_profiles", "job_candidates", "candidate_events", "candidates"]
        }

    def scalar(self, query: str):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(query).fetchone()
        return row[0]


if __name__ == "__main__":
    unittest.main()
