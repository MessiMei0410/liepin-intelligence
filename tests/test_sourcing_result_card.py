from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from test_a_system_agent_v1 import AgentDbCase, fake_assessment  # noqa: E402
from a_system_agent import AgentService, FakeLLM  # noqa: E402
from a_system_agent import sourcing_result_card  # noqa: E402


class SourcingResultCardTest(AgentDbCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = AgentService(self.db_path, FakeLLM(fake_assessment(), chat_text="测试"))

    def tearDown(self) -> None:
        self.service.close()
        super().tearDown()

    def _seed_assessment(self, job_candidate_id: int, fit_score: int, recommendation: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_runs (run_id, kind, context_type, context_id, snapshot_hash, status, model, prompt_version)
                VALUES (?, 'assessment', 'candidate', ?, 'hash', 'completed', 'test', 'test')
                """,
                (f"run_{job_candidate_id}", job_candidate_id),
            )
            conn.execute(
                """
                INSERT INTO agent_candidate_assessments
                (run_id, job_candidate_id, candidate_id, person_id, snapshot_hash, assessment_version, fit_score, fit_level, recommendation, confidence, evidence_coverage, is_current)
                VALUES (?, ?, ?, ?, 'hash', 'v1', ?, ?, ?, 0.8, 0.7, 1)
                """,
                (f"run_{job_candidate_id}", job_candidate_id, job_candidate_id, job_candidate_id, fit_score, "A", recommendation),
            )
            conn.commit()
        finally:
            conn.close()

    def test_non_sourcing_objective_returns_none(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = self.service.create_goal("分析某岗位画像", {"type": "job", "id": 10})
            workflow_id = result["workflow"]["workflow_id"]
            card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
            self.assertIsNone(card)
        finally:
            conn.close()

    def test_sourcing_workflow_generates_card(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = self.service.create_goal("给长越科技机械高级工程师补充5位合适人选", {"type": "job", "id": 10})
            workflow_id = result["workflow"]["workflow_id"]
            self._seed_assessment(30, 88, "recommended")
            card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
            self.assertIsNotNone(card)
            self.assertEqual(card["type"], "sourcing_result")
            self.assertEqual(card["summary"]["client"], "长越科技")
            self.assertEqual(card["summary"]["job"], "机械高级工程师")
            self.assertEqual(card["summary"]["recommendation_breakdown"]["recommended"], 1)
        finally:
            conn.close()

    def test_top_candidates_masking(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            result = self.service.create_goal("给长越科技机械高级工程师补充5位合适人选", {"type": "job", "id": 10})
            workflow_id = result["workflow"]["workflow_id"]
            self._seed_assessment(30, 92, "recommended")
            card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
            self.assertIsNotNone(card)
            top = card["summary"]["top_candidates"]
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["fit_score"], 92)
            self.assertTrue(top[0]["name"].startswith("张"))
            self.assertTrue("*" in top[0]["name"])
        finally:
            conn.close()
