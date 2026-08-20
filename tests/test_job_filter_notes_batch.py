"""批量岗位筛选口径便签（多岗位一张确认卡）服务级 + API 级回归守护。

背景：DSH 对多岗位并发发起 N 张单岗位确认卡时，管道一轮只递最后一张
（confirm_request 单值槽），其余 N-1 张 token 白过期。批量链路：
- batch-preflight 铸**一个**未激活 token，绑定整批 items（job_id+note）规范化哈希；
- batch commit 原子落库：任一岗位不存在 → 409 全不写；
- 逐项幂等 request_id 派生（{request_id}:{job_id}），重放项 already_saved。
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

ITEMS = [
    {"job_id": 137, "note": "六自由度运动台作为大加分项"},
    {"job_id": 138, "note": "3-5 自由度为次优先"},
]


def _migration_sql(version: int) -> str:
    return next(sql for v, _name, sql in MIGRATIONS if v == version)


class JobFilterNotesBatchCase(unittest.TestCase):
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
            INSERT INTO jobs VALUES (138, 1, '软件高级工程师');
            """
        )
        conn.commit()
        conn.close()
        self.core = CoreService(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _activate(self, token: str) -> None:
        self.core.activate_preflight_token(token)

    def _note_of(self, job_id: int):
        return self.core.get_job_filter_note(job_id)["note"]

    def test_preflight_mints_single_token_and_echoes_items(self) -> None:
        result = self.core.filter_notes_batch_preflight([
            {"job_id": 137, "note": "  六自由度运动台（6-DOF）作为大加分项  "},
            {"job_id": 138, "note": "3-5 自由度为次优先"},
        ])
        self.assertTrue(result["ok"])
        self.assertTrue(result["token"])
        self.assertEqual(result["action"], "job_filter_note_batch")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["note"], "六自由度运动台（6-DOF）作为大加分项")
        self.assertEqual(result["items"][0]["job"]["client"], "长越科技")
        self.assertEqual(result["items"][1]["job"]["title"], "软件高级工程师")
        # 预检绝不写库
        self.assertIsNone(self._note_of(137))
        self.assertIsNone(self._note_of(138))

    def test_preflight_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.core.filter_notes_batch_preflight([])
        with self.assertRaises(ValueError):
            self.core.filter_notes_batch_preflight([{"job_id": 137, "note": "  "}])
        with self.assertRaises(ValueError):
            self.core.filter_notes_batch_preflight([{"job_id": 137, "note": "x" * 501}])
        with self.assertRaises(ValueError):
            # 同一岗位一批里重复 → 409
            self.core.filter_notes_batch_preflight([
                {"job_id": 137, "note": "口径 A"},
                {"job_id": 137, "note": "口径 B"},
            ])
        with self.assertRaises(LookupError):
            self.core.filter_notes_batch_preflight([{"job_id": 999, "note": "口径"}])

    def test_commit_requires_ui_activation(self) -> None:
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        with self.assertRaisesRegex(ValueError, "confirmation_required"):
            self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-1")
        self.assertIsNone(self._note_of(137))
        self.assertIsNone(self._note_of(138))

    def test_commit_after_activation_writes_all_jobs(self) -> None:
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token)
        result = self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["saved"], 2)
        self.assertEqual(result["already_saved"], 0)
        self.assertEqual(self._note_of(137)["note"], "六自由度运动台作为大加分项")
        self.assertEqual(self._note_of(138)["note"], "3-5 自由度为次优先")

    def test_commit_is_atomic_when_job_missing(self) -> None:
        items = ITEMS + [{"job_id": 999, "note": "口径 C"}]
        # 预检阶段即 404（岗位 999 不存在），用存在的两岗先铸 token 再绕过预检
        # 直接验证 commit 原子性：伪造缺失岗位需 token 与 items 绑定，故改走
        # 「预检时存在、提交前删除」的真实漂移路径。
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM jobs WHERE id=138")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "未写入任何便签"):
            self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-atomic")
        # 原子语义：一个都不写（含存在的 137）
        self.assertIsNone(self._note_of(137))

    def test_token_bound_to_items_hash(self) -> None:
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token)
        # 改任何一项（便签内容/岗位集）即与 token 绑定目标不符 → 拒绝
        with self.assertRaises(ValueError):
            self.core.filter_notes_batch_commit(
                [{"job_id": 137, "note": "六自由度运动台作为大加分项"},
                 {"job_id": 138, "note": "被篡改的便签"}],
                token, request_id="req-b-tamper",
            )
        with self.assertRaises(ValueError):
            self.core.filter_notes_batch_commit(
                [{"job_id": 137, "note": "六自由度运动台作为大加分项"}],
                token, request_id="req-b-tamper",
            )
        self.assertIsNone(self._note_of(137))
        self.assertIsNone(self._note_of(138))

    def test_token_single_use(self) -> None:
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token)
        self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-1")
        with self.assertRaises(ValueError):
            # token 一次性：第二次消费直接拒绝
            self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-2")

    def test_batch_commit_applies_gender_bridge_per_item(self) -> None:
        # 与单岗位链路同口径的性别桥：便签命中性别排除词时，同事务把对应岗位
        # gender_requirement 置 male_only（migration 16 的列存在才置位）。
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE jobs ADD COLUMN gender_requirement TEXT")
        conn.commit()
        conn.close()
        items = [
            {"job_id": 137, "note": "不看女，限男候选人"},
            {"job_id": 138, "note": "3-5 自由度为次优先"},
        ]
        preflight = self.core.filter_notes_batch_preflight(items)
        self.assertTrue(preflight["items"][0]["gender_requirement_detected"])
        self.assertFalse(preflight["items"][1]["gender_requirement_detected"])
        self.assertIn("male_only", preflight["impact"])
        self._activate(preflight["token"])
        result = self.core.filter_notes_batch_commit(items, preflight["token"], request_id="req-b-gender")
        self.assertTrue(result["ok"])
        self.assertIn("male_only", result["notice"])
        self.assertTrue(result["results"][0]["gender_requirement_detected"])
        self.assertEqual(result["results"][0]["gender_requirement"], "male_only")
        self.assertEqual(result["results"][1]["gender_requirement"], "")
        conn = sqlite3.connect(self.db_path)
        rows = dict(conn.execute("SELECT id, gender_requirement FROM jobs").fetchall())
        conn.close()
        self.assertEqual(rows[137], "male_only")
        self.assertIsNone(rows[138])

    def test_item_level_idempotent_replay(self) -> None:
        token = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token)
        first = self.core.filter_notes_batch_commit(ITEMS, token, request_id="req-b-1")
        self.assertEqual(first["saved"], 2)

        # 同 request_id 重放（新 token，绑定同一批 items）：逐项 already_saved，不重复写
        token2 = self.core.filter_notes_batch_preflight(ITEMS)["token"]
        self._activate(token2)
        replay = self.core.filter_notes_batch_commit(ITEMS, token2, request_id="req-b-1")
        self.assertEqual(replay["saved"], 0)
        self.assertEqual(replay["already_saved"], 2)
        self.assertTrue(all(r["already_saved"] for r in replay["results"]))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM job_filter_notes").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

        # 新 request_id 覆盖更新（upsert 每岗位一条）
        updated_items = [
            {"job_id": 137, "note": "新口径 A"},
            {"job_id": 138, "note": "新口径 B"},
        ]
        token3 = self.core.filter_notes_batch_preflight(updated_items)["token"]
        self._activate(token3)
        updated = self.core.filter_notes_batch_commit(updated_items, token3, request_id="req-b-3")
        self.assertEqual(updated["saved"], 2)
        self.assertEqual(self._note_of(137)["note"], "新口径 A")


class JobFilterNotesBatchApiTest(JobFilterNotesBatchCase):
    """API 级：路由接线 + idem 幂等 + UA 门控激活（TestClient 不进 lifespan，不跑 migrate）。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(db_path=self.db_path, start_legacy=False))

    def _preflight(self, items=ITEMS) -> str:
        response = self.client.post(
            "/api/v1/jobs/filter-notes/batch-preflight",
            json={"request_id": "req-bp-1", "items": items},
        )
        assert response.status_code == 200, response.text
        return response.json()["token"]

    def _activate_via_api(self, token: str) -> None:
        response = self.client.post(
            "/api/v1/write-confirmations/activate",
            headers={"User-Agent": "ASAApp/test", "Idempotency-Key": "req-activate-b1"},
            json={"request_id": "req-activate-b1", "preflight_token": token},
        )
        assert response.status_code == 200, response.text

    def test_full_chain_via_http(self) -> None:
        token = self._preflight()
        # 未激活 commit → 409 confirmation_required
        blocked = self.client.post(
            "/api/v1/jobs/filter-notes/batch",
            headers={"Idempotency-Key": "key-b-1"},
            json={"request_id": "req-b-1", "items": ITEMS, "preflight_token": token},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("confirmation_required", blocked.text)

        self._activate_via_api(token)
        # 首次 409 已在该 Idempotency-Key 下落 failed 账（既有 idem 语义：失败后
        # 必须换新 key 重提），这里换新 key 提交。
        committed = self.client.post(
            "/api/v1/jobs/filter-notes/batch",
            headers={"Idempotency-Key": "key-b-2"},
            json={"request_id": "req-b-1", "items": ITEMS, "preflight_token": token},
        )
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertTrue(committed.json()["ok"])
        self.assertEqual(committed.json()["saved"], 2)

        self.assertEqual(
            self.client.get("/api/v1/jobs/137/filter-notes").json()["note"]["note"],
            "六自由度运动台作为大加分项",
        )
        self.assertEqual(
            self.client.get("/api/v1/jobs/138/filter-notes").json()["note"]["note"],
            "3-5 自由度为次优先",
        )

        # 幂等重放（同 Idempotency-Key + request_id）：返回首次响应，不重复写
        replayed = self.client.post(
            "/api/v1/jobs/filter-notes/batch",
            headers={"Idempotency-Key": "key-b-2"},
            json={"request_id": "req-b-1", "items": ITEMS, "preflight_token": token},
        )
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.json()["saved"], committed.json()["saved"])
        self.assertTrue(replayed.json()["receipt"]["idempotent_replay"])

    def test_preflight_unknown_job_404_and_invalid_items_409(self) -> None:
        response = self.client.post(
            "/api/v1/jobs/filter-notes/batch-preflight",
            json={"request_id": "req-bp-404", "items": [{"job_id": 999, "note": "口径"}]},
        )
        self.assertEqual(response.status_code, 404)
        empty = self.client.post(
            "/api/v1/jobs/filter-notes/batch-preflight",
            json={"request_id": "req-bp-empty", "items": []},
        )
        self.assertEqual(empty.status_code, 422)  # pydantic min_length
        dup = self.client.post(
            "/api/v1/jobs/filter-notes/batch-preflight",
            json={"request_id": "req-bp-dup", "items": [
                {"job_id": 137, "note": "口径 A"}, {"job_id": 137, "note": "口径 B"},
            ]},
        )
        self.assertEqual(dup.status_code, 409)


if __name__ == "__main__":
    unittest.main()
