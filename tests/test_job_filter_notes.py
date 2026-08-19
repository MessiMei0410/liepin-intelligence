"""岗位级筛选口径便签（dogfood R2-3）服务级 + API 级回归守护。

"以后筛选用六自由度作为大加分项"这类跨会话口径偏好此前无持久化通道，模型空口
承诺"已记录"。job_filter_notes（migration 15）每岗位一条口径便签：
- 写入走写确认链（preflight 未激活 token → UI activate → commit 消费）；
- 便签是口径声明，由名单卡口径声明携带展示，不参与确定性筛选逻辑；
- 同 request_id 重放幂等（already_saved）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.database import MIGRATIONS
from asa_core.service import CoreService


def _migration_sql(version: int) -> str:
    return next(sql for v, _name, sql in MIGRATIONS if v == version)


class JobFilterNotesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "asa.db"
        conn = sqlite3.connect(self.db_path)
        # migration 1 的治理表（api_idempotency/audit_events）供 API 级 idem 链路；
        # migration 15 即被测的 job_filter_notes。
        conn.executescript(_migration_sql(1))
        conn.executescript(_migration_sql(15))
        # migrate() 的幂等修补段（ensure_idempotency_recovery_schema）补充的列，
        # 合成库不走 migrate，这里手动补齐。
        conn.executescript(
            """
            ALTER TABLE api_idempotency ADD COLUMN error_json TEXT;
            ALTER TABLE api_idempotency ADD COLUMN updated_at TEXT;
            """
        )
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT);
            INSERT INTO clients VALUES (1, '长越科技');
            INSERT INTO jobs VALUES (137, 1, '机械高级工程师');
            """
        )
        conn.commit()
        conn.close()
        self.core = CoreService(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _activate(self, token: str) -> None:
        self.core.activate_preflight_token(token)

    def test_get_without_note_returns_null(self) -> None:
        result = self.core.get_job_filter_note(137)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["note"])
        self.assertEqual(result["job"]["title"], "机械高级工程师")

    def test_unknown_job_raises_lookup(self) -> None:
        with self.assertRaises(LookupError):
            self.core.get_job_filter_note(999)
        with self.assertRaises(LookupError):
            self.core.filter_note_preflight(999, "六自由度运动台作为大加分项")

    def test_preflight_mints_inactive_token_and_echoes_previous(self) -> None:
        result = self.core.filter_note_preflight(137, "  六自由度运动台（6-DOF）作为大加分项  ")
        self.assertTrue(result["ok"])
        self.assertTrue(result["token"])
        self.assertEqual(result["note"], "六自由度运动台（6-DOF）作为大加分项")
        self.assertEqual(result["previous_note"], "")
        # 预检绝不写库
        self.assertIsNone(self.core.get_job_filter_note(137)["note"])

    def test_commit_requires_ui_activation(self) -> None:
        token = self.core.filter_note_preflight(137, "六自由度运动台作为大加分项")["token"]
        with self.assertRaisesRegex(ValueError, "confirmation_required"):
            self.core.filter_note_commit(137, "六自由度运动台作为大加分项", token, request_id="req-fn-1")
        self.assertIsNone(self.core.get_job_filter_note(137)["note"])

    def test_commit_after_activation_upserts_and_replays(self) -> None:
        token = self.core.filter_note_preflight(137, "六自由度运动台作为大加分项")["token"]
        self._activate(token)
        result = self.core.filter_note_commit(137, "六自由度运动台作为大加分项", token, request_id="req-fn-1")
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_saved"])
        stored = self.core.get_job_filter_note(137)["note"]
        self.assertEqual(stored["note"], "六自由度运动台作为大加分项")

        # 同 request_id 重放（新 token）：幂等返回已保存，不重复写入
        token2 = self.core.filter_note_preflight(137, "六自由度运动台作为大加分项")["token"]
        self.assertNotEqual(token2, "")  # 预检回显当前便签
        self._activate(token2)
        replay = self.core.filter_note_commit(137, "六自由度运动台作为大加分项", token2, request_id="req-fn-1")
        self.assertTrue(replay["already_saved"])

        # 新 request_id 覆盖更新（upsert 每岗位一条）
        token3 = self.core.filter_note_preflight(137, "3-5 自由度为次优先")["token"]
        preflight = self.core.filter_note_preflight(137, "3-5 自由度为次优先")
        self.assertEqual(preflight["previous_note"], "六自由度运动台作为大加分项")
        self._activate(token3)
        updated = self.core.filter_note_commit(137, "3-5 自由度为次优先", token3, request_id="req-fn-2")
        self.assertFalse(updated["already_saved"])
        self.assertEqual(self.core.get_job_filter_note(137)["note"]["note"], "3-5 自由度为次优先")
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM job_filter_notes WHERE job_id=137").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_token_bound_to_job_and_action(self) -> None:
        token = self.core.filter_note_preflight(137, "口径 A")["token"]
        self._activate(token)
        with self.assertRaises(ValueError):
            # token 绑定 job_id=137：打别的岗位直接拒绝
            self.core.filter_note_commit(138, "口径 A", token, request_id="req-fn-x")

    def test_note_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.core.filter_note_preflight(137, "   ")
        with self.assertRaises(ValueError):
            self.core.filter_note_preflight(137, "x" * 501)

    def test_attach_filter_note_to_candidate_list_card(self) -> None:
        token = self.core.filter_note_preflight(137, "六自由度运动台作为大加分项")["token"]
        self._activate(token)
        self.core.filter_note_commit(137, "六自由度运动台作为大加分项", token, request_id="req-fn-1")

        answer, card = self.core._attach_filter_note(137, "名单如下", {"type": "candidate_list", "title": "名单"})
        self.assertIn("口径便签", answer)
        self.assertIn("六自由度运动台", answer)
        self.assertEqual(card["filter_note"]["note"], "六自由度运动台作为大加分项")
        # 无便签岗位：answer/card 原样
        answer2, card2 = self.core._attach_filter_note(138, "名单如下", {"type": "candidate_list"})
        self.assertEqual(answer2, "名单如下")
        self.assertNotIn("filter_note", card2)


class JobFilterNotesApiTest(JobFilterNotesCase):
    """API 级：路由接线 + idem 幂等 + UA 门控激活（TestClient 不进 lifespan，不跑 migrate）。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(db_path=self.db_path, start_legacy=False))

    def _preflight(self, note: str = "六自由度运动台作为大加分项") -> str:
        response = self.client.post(
            "/api/v1/jobs/137/filter-notes/preflight",
            json={"request_id": "req-preflight-1", "note": note},
        )
        assert response.status_code == 200, response.text
        return response.json()["token"]

    def _activate_via_api(self, token: str) -> None:
        response = self.client.post(
            "/api/v1/write-confirmations/activate",
            headers={"User-Agent": "ASAApp/test", "Idempotency-Key": "req-activate-1"},
            json={"request_id": "req-activate-1", "preflight_token": token},
        )
        assert response.status_code == 200, response.text

    def test_full_chain_via_http(self) -> None:
        response = self.client.get("/api/v1/jobs/137/filter-notes")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["note"])

        token = self._preflight()
        # 未激活 commit → 409 confirmation_required
        blocked = self.client.post(
            "/api/v1/jobs/137/filter-notes",
            headers={"Idempotency-Key": "key-fn-1"},
            json={"request_id": "req-fn-1", "note": "六自由度运动台作为大加分项", "preflight_token": token},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("confirmation_required", blocked.text)

        self._activate_via_api(token)
        # 首次 409 已在该 Idempotency-Key 下落 failed 账（既有 idem 语义：失败后
        # 必须换新 key 重提），这里换新 key 提交。
        committed = self.client.post(
            "/api/v1/jobs/137/filter-notes",
            headers={"Idempotency-Key": "key-fn-2"},
            json={"request_id": "req-fn-1", "note": "六自由度运动台作为大加分项", "preflight_token": token},
        )
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertTrue(committed.json()["ok"])

        stored = self.client.get("/api/v1/jobs/137/filter-notes").json()["note"]
        self.assertEqual(stored["note"], "六自由度运动台作为大加分项")

        # 幂等重放（同 Idempotency-Key + request_id）：返回首次响应，不重复写
        replayed = self.client.post(
            "/api/v1/jobs/137/filter-notes",
            headers={"Idempotency-Key": "key-fn-2"},
            json={"request_id": "req-fn-1", "note": "六自由度运动台作为大加分项", "preflight_token": token},
        )
        self.assertEqual(replayed.status_code, 200)
        # 重放返回首次响应（receipt.idempotent_replay 标记位除外）
        self.assertEqual(replayed.json()["note"], committed.json()["note"])
        self.assertTrue(replayed.json()["receipt"]["idempotent_replay"])

    def test_unknown_job_404(self) -> None:
        self.assertEqual(self.client.get("/api/v1/jobs/999/filter-notes").status_code, 404)
        response = self.client.post(
            "/api/v1/jobs/999/filter-notes/preflight",
            json={"request_id": "req-preflight-404", "note": "口径"},
        )
        self.assertEqual(response.status_code, 404)

    def test_empty_note_409(self) -> None:
        response = self.client.post(
            "/api/v1/jobs/137/filter-notes/preflight",
            json={"request_id": "req-preflight-empty", "note": "  "},
        )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
