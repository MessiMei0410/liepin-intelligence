"""S6-1b：判人评估顾问动作写回 —— PATCH /candidates/{id}/assessments/{job_id}/advisor-action 测试。

口径：PRD S6 顾问动作回流（采纳/改判/否决）。全部临时库 + 临时 KB fixture + FakeLLM，
绝不触碰生产 DB、真实知识库与外网 LLM。fixture 复用 S6-1 测试模块（tests 跨文件引用是既有模式）。

覆盖：
1. 采纳写回：advisor_action/advisor_note/updated_at 落 artifact，version 不 bump、as_of 不动，时间线留痕；
2. 改判附 note + 已 action 可再改；同幂等键重放返回首次响应、不重复写时间线；
3. 404：无评估 / 人选不存在 / 人选不属于该岗位；
4. 409：非法 action。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import candidate_assessment  # noqa: E402
from asa_core.app import create_app  # noqa: E402
from test_candidate_assessment_s6 import DbCase, _fake_llm, _seed_person  # noqa: E402


class AdvisorActionApiTest(DbCase):
    def _prepare(self) -> TestClient:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        app = create_app(db_path=self.db_path, start_legacy=False)
        app.state.core.agent_service.llm = _fake_llm()
        return TestClient(app)

    @staticmethod
    def _generate(client: TestClient) -> dict:
        response = client.post(
            "/api/v1/candidates/1/assessments?job_id=154",
            json={"request_id": "req-gen"}, headers={"Idempotency-Key": "k-gen"},
        )
        assert response.status_code == 200, response.text
        return response.json()["assessment"]

    @staticmethod
    def _patch(client: TestClient, key: str, body: dict):
        return client.patch(
            "/api/v1/candidates/1/assessments/154/advisor-action",
            json=body, headers={"Idempotency-Key": key},
        )

    def _stored_doc(self) -> dict:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT metadata_json FROM agent_artifacts WHERE artifact_id='candidate_assessment_1_154'"
            ).fetchone()
            assert row is not None
            return json.loads(row["metadata_json"])
        finally:
            conn.close()

    def test_accepted_writes_back_without_version_bump(self) -> None:
        with self._prepare() as client:
            generated = self._generate(client)
            assert generated["advisor_action"] == "pending"
            response = self._patch(client, "k-b1", {"request_id": "req-b1", "action": "accepted", "note": "口径与我的判断一致"})
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is True
            assert payload["artifact_id"] == "candidate_assessment_1_154"
            assert payload["advisor_action"] == "accepted"
            assert payload["advisor_note"] == "口径与我的判断一致"
            assert payload["receipt"]["idempotent_replay"] is False

            stored = self._stored_doc()
            assert stored["advisor_action"] == "accepted"
            assert stored["advisor_note"] == "口径与我的判断一致"
            assert stored["updated_at"], "写回必须刷新 updated_at"
            assert int(stored["version"]) == int(generated["version"]), "顾问动作写回不得 bump version"
            assert stored["as_of"] == generated["as_of"], "评估内容未变，as_of 不动"

            conn = self.connect()
            try:
                event = conn.execute(
                    "SELECT event_status,summary FROM candidate_events"
                    " WHERE event_type='candidate_assessment_advisor_action'"
                ).fetchone()
                assert event is not None, "顾问动作必须写岗位时间线留痕"
                assert event["event_status"] == "completed"
                assert "已采纳" in event["summary"]
            finally:
                conn.close()

    def test_modified_with_note_then_reject_and_replay(self) -> None:
        with self._prepare() as client:
            self._generate(client)
            modified = self._patch(client, "k-b2", {"request_id": "req-b2", "action": "modified", "note": "当前这单我判上升，不是平移"})
            assert modified.status_code == 200, modified.text
            assert modified.json()["assessment"]["advisor_action"] == "modified"

            # 已 action 的可再改（顾问改主意是正常业务流）
            rejected = self._patch(client, "k-b3", {"request_id": "req-b3", "action": "rejected"})
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["advisor_action"] == "rejected"
            stored = self._stored_doc()
            assert stored["advisor_action"] == "rejected"
            assert stored["advisor_note"] == "", "再改不带 note 时备注清空"
            assert int(stored["version"]) == 1

            # 同幂等键 + 同 body 重放 → 首次响应，不重复写时间线
            replay = self._patch(client, "k-b2", {"request_id": "req-b2", "action": "modified", "note": "当前这单我判上升，不是平移"})
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True
            assert replay.json()["advisor_action"] == "modified"
            conn = self.connect()
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM candidate_events WHERE event_type='candidate_assessment_advisor_action'"
                ).fetchone()[0]
                assert count == 2, "重放不得重复写时间线（modified 一次 + rejected 一次）"
            finally:
                conn.close()

    def test_404_without_assessment_or_relation(self) -> None:
        with self._prepare() as client:
            # 人选存在、岗位匹配，但尚未生成评估 → 404
            missing = self._patch(client, "k-c1", {"request_id": "req-c1", "action": "accepted"})
            assert missing.status_code == 404, missing.text
            # 人选不存在 → 404
            ghost = client.patch(
                "/api/v1/candidates/999/assessments/154/advisor-action",
                json={"request_id": "req-c2", "action": "accepted"}, headers={"Idempotency-Key": "k-c2"},
            )
            assert ghost.status_code == 404, ghost.text
            # 人选不属于该岗位 → 404
            mismatch = client.patch(
                "/api/v1/candidates/1/assessments/137/advisor-action",
                json={"request_id": "req-c3", "action": "accepted"}, headers={"Idempotency-Key": "k-c3"},
            )
            assert mismatch.status_code == 404, mismatch.text

    def test_409_invalid_action(self) -> None:
        with self._prepare() as client:
            self._generate(client)
            response = self._patch(client, "k-d1", {"request_id": "req-d1", "action": "maybe"})
            assert response.status_code == 409, response.text
            assert "action" in response.json()["detail"]
            stored = self._stored_doc()
            assert stored["advisor_action"] == "pending", "非法 action 不得改动 artifact"
            # service 层同样抛 ValueError（409 语义）
            conn = self.connect()
            try:
                with self.assertRaises(ValueError):
                    candidate_assessment.apply_advisor_action(
                        conn, candidate_id=1, job_id=154, action=" endorsed "
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
