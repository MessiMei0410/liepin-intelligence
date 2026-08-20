"""岗位建档（用户实证卡点）服务级 + API 级回归守护。

「士兰微新增市场总监岗位，没有明确 JD，负责汽车市场，base 杭州」此前无任何
建档入口（界面无新增按钮，Agent 工具面只能让用户手动建）。岗位建档走写确认链：
- preflight 解析客户（精确/模糊/新建）+ 同客户同名查重（409 冲突说明），不写库；
- commit 需 UI 激活的一次性 token（绑定客户名+岗位名），落 client（必要时新建）+
  job（初始 待启动/intake/workbench），审计由 idem 链统一落库；
- 建档只登记岗位，绝不自动启动寻访/抓取；重复 commit（同客户同名已存在）幂等返回。
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


class JobCreateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "asa.db"
        conn = sqlite3.connect(self.db_path)
        # migration 1 的治理表（api_idempotency/audit_events）供 API 级 idem + 审计链路。
        conn.executescript(_migration_sql(1))
        # migrate() 幂等修补段补充的列，合成库不走 migrate，手动补齐。
        conn.executescript(
            """
            ALTER TABLE api_idempotency ADD COLUMN error_json TEXT;
            ALTER TABLE api_idempotency ADD COLUMN updated_at TEXT;
            """
        )
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, location TEXT,
                status TEXT, lifecycle_stage TEXT, source_layer TEXT, summary TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE job_pipeline_metrics (
                id INTEGER PRIMARY KEY, job_id INTEGER, metric_date TEXT,
                a_count INTEGER, b_count INTEGER, p0_count INTEGER, p1_count INTEGER,
                published_count INTEGER, under_review_count INTEGER,
                contacted_count INTEGER, pending_followup_count INTEGER,
                priority TEXT, risk TEXT, next_keywords_json TEXT,
                target_companies_json TEXT, exclude_terms_json TEXT, data_gap INTEGER
            );
            INSERT INTO clients VALUES (1, '长越科技');
            INSERT INTO clients VALUES (2, '杭州士兰微电子有限公司');
            INSERT INTO jobs VALUES (137, 1, '机械高级工程师', '苏州', '待启动', 'intake', 'workbench', '', '', '');
            """
        )
        conn.commit()
        conn.close()
        self.core = CoreService(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _activate(self, token: str) -> None:
        self.core.activate_preflight_token(token)

    def _commit(self, client: str = "士兰微", title: str = "市场总监", token: str = "", **kwargs) -> dict:
        return self.core.job_create_commit(client, title, token, request_id="req-jc-1", **kwargs)

    def test_preflight_new_client_mints_inactive_token(self) -> None:
        result = self.core.job_create_preflight(
            "  士兰微  ", " 市场总监 ", direction="汽车市场", base="杭州"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["token"])
        job = result["job"]
        # 模糊匹配：「士兰微」命中既有客户「杭州士兰微电子有限公司」，不新建
        self.assertFalse(job["client_is_new"])
        self.assertEqual(job["client_match"], "fuzzy")
        self.assertEqual(job["client"], "杭州士兰微电子有限公司")
        self.assertEqual(job["client_id"], 2)
        self.assertEqual(job["title"], "市场总监")
        self.assertEqual(job["direction"], "汽车市场")
        self.assertEqual(job["base"], "杭州")
        self.assertTrue(any("杭州士兰微电子有限公司" in w for w in result["warnings"]))
        self.assertTrue(any("JD" in w for w in result["warnings"]))
        self.assertIn("不会自动启动任何寻访", result["impact"])
        # 预检绝不写库
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_preflight_exact_and_unknown_client(self) -> None:
        exact = self.core.job_create_preflight("长越科技", "市场总监")
        self.assertEqual(exact["job"]["client_match"], "exact")
        self.assertEqual(exact["job"]["client_id"], 1)
        fresh = self.core.job_create_preflight("全新客户", "销售总监")
        self.assertTrue(fresh["job"]["client_is_new"])
        self.assertEqual(fresh["job"]["client_match"], "new")
        self.assertIn("新建客户「全新客户」", fresh["impact"])

    def test_preflight_ambiguous_client_409(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO clients VALUES (3, '士兰明芯')")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "匹配到多个既有客户"):
            self.core.job_create_preflight("士兰", "市场总监")

    def test_preflight_duplicate_job_409(self) -> None:
        with self.assertRaisesRegex(ValueError, "已存在同名岗位（#137"):
            self.core.job_create_preflight("长越科技", "机械高级工程师")

    def test_preflight_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.core.job_create_preflight("  ", "市场总监")
        with self.assertRaises(ValueError):
            self.core.job_create_preflight("士兰微", "   ")

    def test_commit_requires_ui_activation(self) -> None:
        token = self.core.job_create_preflight("士兰微", "市场总监")["token"]
        with self.assertRaisesRegex(ValueError, "confirmation_required"):
            self._commit(token=token)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_commit_creates_job_and_reuses_fuzzy_client(self) -> None:
        token = self.core.job_create_preflight(
            "士兰微", "市场总监", direction="汽车市场", base="杭州", priority="P0-最急"
        )["token"]
        self._activate(token)
        result = self._commit(token=token, direction="汽车市场", base="杭州", priority="P0-最急")
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_created"])
        self.assertFalse(result["client_created"])
        self.assertEqual(result["client_id"], 2)
        job_id = result["job_id"]
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT client_id,title,location,status,lifecycle_stage,source_layer,summary FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        metric = conn.execute(
            "SELECT priority FROM job_pipeline_metrics WHERE job_id=?", (job_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 2)
        self.assertEqual(row[1], "市场总监")
        self.assertEqual(row[2], "杭州")
        # 初始字段按既有手工建档行口径
        self.assertEqual(row[3], "待启动")
        self.assertEqual(row[4], "intake")
        self.assertEqual(row[5], "workbench")
        self.assertIn("方向：汽车市场", row[6])
        self.assertIn("JD 待补充", row[6])
        self.assertEqual(metric[0], "P0-最急")

    def test_commit_creates_new_client(self) -> None:
        token = self.core.job_create_preflight("全新客户", "销售总监", jd_text="负责华东销售团队")["token"]
        self._activate(token)
        result = self._commit(client="全新客户", title="销售总监", token=token, jd_text="负责华东销售团队")
        self.assertTrue(result["client_created"])
        conn = sqlite3.connect(self.db_path)
        client = conn.execute("SELECT name FROM clients WHERE id=?", (result["client_id"],)).fetchone()
        job = conn.execute("SELECT summary FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
        # 未传 priority 时不落 job_pipeline_metrics
        metric = conn.execute(
            "SELECT COUNT(*) FROM job_pipeline_metrics WHERE job_id=?", (result["job_id"],)
        ).fetchone()
        conn.close()
        self.assertEqual(client[0], "全新客户")
        self.assertIn("负责华东销售团队", job[0])
        self.assertNotIn("JD 待补充", job[0])
        self.assertEqual(metric[0], 0)

    def test_commit_idempotent_replay_returns_existing(self) -> None:
        token = self.core.job_create_preflight("士兰微", "市场总监")["token"]
        self._activate(token)
        first = self._commit(token=token)
        # 重复建档（如重试/双击穿透到服务层）：同客户同名已存在 → 幂等返回，不重复建行。
        # （预检层对同客户同名已 409 拦住，见 test_preflight_duplicate_job_409。）
        token3 = self.core._mint_write_token("士兰微::市场总监", "job_create", activated=True)[0]
        replay = self._commit(token=token3)
        self.assertTrue(replay["already_created"])
        self.assertEqual(replay["job_id"], first["job_id"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE title='市场总监'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_token_bound_to_client_and_title(self) -> None:
        token = self.core.job_create_preflight("士兰微", "市场总监")["token"]
        self._activate(token)
        with self.assertRaises(ValueError):
            # token 绑定「士兰微::市场总监」：建别的岗位直接拒绝
            self._commit(title="销售总监", token=token)


class JobCreateApiTest(JobCreateCase):
    """API 级：路由接线 + idem 幂等 + UA 门控激活 + 审计落库（TestClient 不进 lifespan）。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(db_path=self.db_path, start_legacy=False))

    def _preflight(self, client_name: str = "士兰微", title: str = "市场总监", **extra) -> str:
        response = self.client.post(
            "/api/v1/jobs/preflight",
            json={"request_id": "req-preflight-1", "client_name": client_name, "title": title, **extra},
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
        token = self._preflight(direction="汽车市场", base="杭州")
        # 未激活 commit → 409 confirmation_required
        blocked = self.client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "key-jc-1"},
            json={
                "request_id": "req-jc-1", "client_name": "士兰微", "title": "市场总监",
                "preflight_token": token,
            },
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("confirmation_required", blocked.text)

        self._activate_via_api(token)
        # 首次 409 已在该 Idempotency-Key 下落 failed 账（既有 idem 语义：换新 key 重提）
        committed = self.client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "key-jc-2"},
            json={
                "request_id": "req-jc-1", "client_name": "士兰微", "title": "市场总监",
                "preflight_token": token,
            },
        )
        self.assertEqual(committed.status_code, 200, committed.text)
        payload = committed.json()
        self.assertTrue(payload["ok"])
        job_id = payload["job_id"]
        self.assertGreater(job_id, 0)

        # 审计落库：idem 链统一写 audit_events（首次未激活 409 落 failed，成功提交落 success）
        conn = sqlite3.connect(self.db_path)
        audit = conn.execute(
            "SELECT operation,target_type,result FROM audit_events WHERE operation='job.create' AND result='success'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(audit)
        self.assertEqual(audit[1], "job")
        self.assertEqual(audit[2], "success")

        # 岗位可读回（GET /api/v1/jobs/{id} 走完整 job 详情，依赖 positions 等表，这里只查列表口径）
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT title,status FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "市场总监")
        self.assertEqual(row[1], "待启动")

        # 幂等重放（同 Idempotency-Key + request_id）：返回首次响应，不重复建行
        replayed = self.client.post(
            "/api/v1/jobs",
            headers={"Idempotency-Key": "key-jc-2"},
            json={
                "request_id": "req-jc-1", "client_name": "士兰微", "title": "市场总监",
                "preflight_token": token,
            },
        )
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.json()["job_id"], job_id)
        self.assertTrue(replayed.json()["receipt"]["idempotent_replay"])
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE title='市场总监'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_duplicate_preflight_409(self) -> None:
        response = self.client.post(
            "/api/v1/jobs/preflight",
            json={"request_id": "req-preflight-dup", "client_name": "长越科技", "title": "机械高级工程师"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("已存在同名岗位", response.text)

    def test_empty_fields_422(self) -> None:
        response = self.client.post(
            "/api/v1/jobs/preflight",
            json={"request_id": "req-preflight-empty", "client_name": "", "title": "市场总监"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
