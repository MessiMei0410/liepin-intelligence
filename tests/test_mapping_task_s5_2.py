"""S5-2：Mapping 任务卡 —— 候选状态机 PATCH / 逐人破冰素材 / 入库动作 契约测试。

口径：docs/TASKCARD_S5-2_任务卡视图与破冰素材_20260723.md（①状态机 ②破冰素材 ③入库）
+ PRD §4/§7。全部临时库 + 临时 KB fixture，绝不触碰生产 DB；不打外网、不接 LLM（规则版生成）。

覆盖：
- PATCH：七态迁移合法/非法（intaken 倒退、rejected 终态、直接置 intaken、未知态）、note 编辑、
  幂等重放、审计落库、404/409/422 语义、version 不 bump、stats 同步；
- 破冰：confirmed 触发自动生成且引用真实线索词、反模板（不含线索关键词的通用句判不合格拒写）、
  重新生成幂等、费率/红线词不出现；
- 入库：confirmed 放行/非 confirmed 拒绝、不写第二条 job_candidates（预置关系 → 复用原 id）、
  遮罩名合并（§6.4 同姓+公司+职位双匹配）、禁挖拦截、无来源拒入、已停止关系不重复入库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import mapping_task  # noqa: E402
from asa_core.app import create_app  # noqa: E402

FORBIDDEN_LITERALS = ["13912345678", "费率23%", "MARKER_REDLINE_S5", "话术红线"]

CASE_FIXTURE = {
    "meta": {"case_id": "case_silan_s5"},
    "client_profile": {"name": "士兰微"},
    "restricted": {
        "banned_companies": ["杰华特"],
        "consultant_phone": "13912345678",
        "fee_rate": "费率23%",
        "scripts_redline": "话术红线MARKER_REDLINE_S5",
    },
}

PAPER_REASON = (
    "晶丰明源 相关公开论文《Conducted EMI Mitigation Schemes in Isolated Switching-Mode "
    "Power Supply Without the Need of a Y-Capacitor》作者（单位标注：Bright Power Semiconductor, Shanghai, China）"
)
PAPER_URL = "https://doi.org/10.1109/tpel.2016.2579679"
MPS_REASON = (
    "MPS 相关公开论文《Analytical loss model of power MOSFET》作者"
    "（单位标注：Monolithic Power Systems, Los Gatos, CA, USA）"
)
MPS_URL = "https://doi.org/10.1109/tpel.2005.869743"
PATENT_REASON = "杰华特 多相控制器方向公开专利《一种多相控制器》（CN111000111A）发明人"
PATENT_URL = "https://patents.google.com/patent/CN111000111A/zh"


def _teams() -> list[dict]:
    return [
        {
            "company": "晶丰明源",
            "team": "服务器 方向 TME 团队",
            "location": "杭州",
            "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:晶丰明源", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
        {
            "company": "MPS",
            "team": "PC/服务器/ADAS 方向 TME/FAE/AE 团队",
            "location": "",
            "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:MPS", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
        {
            "company": "杰华特",
            "team": "PC/服务器 方向 TME/FAE 团队",
            "location": "",
            "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:杰华特", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
    ]


def _candidate(
    name: str,
    team_ref: int,
    role: str,
    reason: str,
    url: str,
    *,
    status: str = "pending",
    note: str = "",
    extra: dict | None = None,
) -> dict:
    candidate = {
        "name": name,
        "current_role": role,
        "team_ref": team_ref,
        "source_urls": [url],
        "confidence": "medium",
        "reason": reason,
        "status": status,
        "consultant_note": note,
    }
    if extra:
        candidate.update(extra)
    return candidate


def _default_candidates() -> list[dict]:
    return [
        # 0 待确认（PATCH 确认主路径）
        _candidate("陈**", 0, "晶丰明源 技术论文作者", PAPER_REASON, PAPER_URL),
        # 1 待确认（非法迁移用例）
        _candidate("Y**", 1, "MPS 技术论文作者", MPS_REASON, MPS_URL),
        # 2 已确认（入库主路径）
        _candidate("沈**", 0, "晶丰明源 技术论文作者", PAPER_REASON, PAPER_URL, status="confirmed"),
        # 3 已确认（禁挖公司，入库拦截用例）
        _candidate("李**", 2, "杰华特 研发（专利发明人）", PATENT_REASON, PATENT_URL, status="confirmed"),
        # 4 已确认（遮罩名合并用例：预置全名 王五 同公司同职位）
        _candidate("王**", 0, "晶丰明源 技术论文作者", PAPER_REASON, PAPER_URL, status="confirmed"),
        # 5 搁置（parked 恢复/淘汰用例）
        _candidate("赵**", 1, "MPS 技术论文作者", MPS_REASON, MPS_URL, status="parked"),
        # 6 已淘汰（软删终态用例）
        _candidate("钱**", 1, "MPS 技术论文作者", MPS_REASON, MPS_URL, status="rejected"),
        # 7 已入库（intaken 终态 + 业务幂等用例，带入库回执）
        _candidate(
            "孙**", 1, "MPS 技术论文作者", MPS_REASON, MPS_URL, status="intaken",
            extra={"intake": {"job_candidate_id": 999, "candidate_id": 888, "person_id": 777,
                              "intaken_at": "2026-07-23 09:00:00", "relation_existed": False}},
        ),
    ]


class S52Case(unittest.TestCase):
    """临时库 + 临时 KB（restricted 禁挖白名单）+ 种子 mapping_task artifact。"""

    API_SCHEMA = """
    CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
    CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT);
    CREATE TABLE positions(id INTEGER PRIMARY KEY,client TEXT,title TEXT);
    CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT,current_title TEXT,
      city TEXT,education TEXT,experience TEXT,fingerprint TEXT,created_at TEXT);
    CREATE TABLE candidates(id INTEGER PRIMARY KEY,name TEXT,company TEXT,title TEXT,education TEXT,
      experience TEXT,skills TEXT,level TEXT,city TEXT,client TEXT,position TEXT,source TEXT,xsaas_id TEXT,
      search_date TEXT,status TEXT,notes TEXT,iteration INTEGER,created_at TEXT,updated_at TEXT);
    CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,raw_client TEXT,
      raw_position TEXT,raw_status TEXT,raw_stage TEXT,clean_stage TEXT,flow_bucket TEXT,clean_reason TEXT,
      recent_hunting INTEGER,search_date TEXT,updated_at TEXT,source_candidate_id TEXT);
    CREATE TABLE candidate_events(id INTEGER PRIMARY KEY,job_candidate_id INTEGER,person_id INTEGER,job_id INTEGER,
      event_type TEXT,event_status TEXT,event_time TEXT,summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
    CREATE TABLE source_profiles(id INTEGER PRIMARY KEY,person_id INTEGER,source_type TEXT,
      source_candidate_id TEXT,source_date TEXT,raw_status TEXT,raw_client TEXT,raw_position TEXT,raw_json TEXT);
    CREATE TABLE candidate_clients(id INTEGER PRIMARY KEY,candidate_name TEXT,candidate_company TEXT,
      client TEXT,source TEXT,position_tag TEXT,created_at TEXT);
    CREATE TABLE candidate_profiles(id INTEGER PRIMARY KEY,candidate_id INTEGER,candidate_name TEXT,
      candidate_company TEXT,client TEXT,position TEXT,industry_tags_json TEXT,function_tags_json TEXT,
      risk_tags_json TEXT,profile_summary TEXT,updated_at TEXT);
    CREATE TABLE candidate_intelligence(id INTEGER PRIMARY KEY,candidate_id INTEGER,candidate_name TEXT,
      candidate_company TEXT,client TEXT,position TEXT,fit_score INTEGER,fit_level TEXT,evidence_json TEXT,
      risk_json TEXT,next_action TEXT,last_evaluated_at TEXT,model_version TEXT,created_at TEXT,updated_at TEXT,
      strong_matches_json TEXT,weak_matches_json TEXT,verification_questions_json TEXT,recommendation_decision TEXT);
    """

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = Path(self.kb_temp.name) / "kb"
        (kb_dir / "cases").mkdir(parents=True, exist_ok=True)
        (kb_dir / "cases" / "case_silan_s5_v1.json").write_text(
            json.dumps(CASE_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        self._create_db()
        self.app = create_app(db_path=self.db_path, start_legacy=False)
        self._seed_workflows()

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        self.db_temp.cleanup()

    def _create_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.API_SCHEMA)
        conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
        conn.execute("INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）')")
        conn.execute("INSERT INTO positions VALUES (1,'士兰微','技术市场经理/总监（PC电源）')")
        conn.commit()
        conn.close()

    def _seed_workflows(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)
            VALUES ('goal_154','给士兰微补充人选','寻访','job',154,'{"type":"job","id":154}','blocked')
            """
        )
        conn.execute(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-154','goal_154','blocked')"
        )
        conn.commit()
        conn.close()

    def _seed_artifact(self, candidates: list[dict] | None = None) -> str:
        doc = {
            "schema_version": "mapping_v1",
            "trigger": "manual",
            "job_id": 154,
            "strategy_ref": "artifact_strategy_154",
            "client": "士兰微",
            "job_title": "技术市场经理/总监（PC电源）",
            "generated_at": "2026-07-23 10:00:00",
            "workflow_id": "wf-154",
            "goal_id": "goal_154",
            "target_teams": _teams(),
            "candidates": candidates if candidates is not None else _default_candidates(),
            "stats": {"teams": 3, "candidates": 0, "confirmed": 0, "intaken": 0},
        }
        doc["stats"]["candidates"] = len(doc["candidates"])
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            artifact_id = mapping_task.upsert_mapping_task(conn, doc)
            conn.commit()
        finally:
            conn.close()
        return artifact_id

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"Idempotency-Key": key}

    def _patch(self, artifact_id: str, index: int, body: dict, key: str):
        with TestClient(self.app) as client:
            return client.patch(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/{index}",
                json=body,
                headers=self._headers(key),
            )

    def _doc(self, artifact_id: str) -> dict:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            payload = mapping_task.get_mapping_task(conn, artifact_id)
        finally:
            conn.close()
        return payload["mapping_task"]

    def _count(self, sql: str, params: tuple = ()) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute(sql, params).fetchone()[0])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 1. 状态机 PATCH（合法/非法迁移、note、幂等、审计、404/409/422、version 不 bump）
# ---------------------------------------------------------------------------

class StatusPatchTest(S52Case):
    def test_confirm_legal_transition_and_stats_and_no_version_bump(self) -> None:
        artifact_id = self._seed_artifact()
        before = self._doc(artifact_id)
        assert before["version"] == 1 and before["history"] == []

        resp = self._patch(artifact_id, 0, {"request_id": "req-p1", "status": "confirmed"}, "k-p1")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True and payload["status"] == "confirmed"
        assert payload["status_label"] == "已确认"
        assert payload["receipt"]["idempotent_replay"] is False
        # stats 同步：已确认含 0/2/3/4/7（confirmed+contacted+replied+intaken 口径）
        assert payload["stats"]["confirmed"] == 5, payload["stats"]
        assert payload["stats"]["intaken"] == 1

        doc = self._doc(artifact_id)
        assert doc["candidates"][0]["status"] == "confirmed"
        assert doc["version"] == 1, "状态 upsert 不得 bump artifact version"
        assert doc["history"] == [], "状态 upsert 不动 history"

    def test_main_chain_and_parked_recovery(self) -> None:
        artifact_id = self._seed_artifact()
        seq = [
            (0, "confirmed", "k-c1"),
            (0, "contacted", "k-c2"),
            (0, "replied", "k-c3"),
            (0, "parked", "k-c4"),
            (0, "pending", "k-c5"),
            (0, "confirmed", "k-c6"),
        ]
        for n, (index, status, key) in enumerate(seq):
            resp = self._patch(artifact_id, index, {"request_id": f"req-{key}", "status": status}, key)
            assert resp.status_code == 200, f"步骤 {n} {status} 失败：{resp.text}"
        doc = self._doc(artifact_id)
        assert doc["candidates"][0]["status"] == "confirmed"

    def test_illegal_transitions_409(self) -> None:
        artifact_id = self._seed_artifact()
        cases = [
            (0, "intaken", "PATCH 不得直接置 intaken"),
            (1, "contacted", "pending 不得跳级到 contacted"),
            (5, "confirmed", "parked 只能恢复 pending 或淘汰"),
            (6, "pending", "rejected 是软删终态"),
            (7, "contacted", "intaken 禁止倒退"),
            (7, "rejected", "intaken 是终态"),
            (0, "foo", "未知态"),
        ]
        for n, (index, status, why) in enumerate(cases):
            resp = self._patch(artifact_id, index, {"request_id": f"req-x{n}", "status": status}, f"k-x{n}")
            assert resp.status_code == 409, f"{why}：期望 409，实际 {resp.status_code} {resp.text}"
        doc = self._doc(artifact_id)
        # 全部非法迁移均未落库
        statuses = [c["status"] for c in doc["candidates"]]
        assert statuses == ["pending", "pending", "confirmed", "confirmed", "confirmed", "parked", "rejected", "intaken"]

    def test_note_edit_only_and_same_status_noop(self) -> None:
        artifact_id = self._seed_artifact()
        resp = self._patch(artifact_id, 1, {"request_id": "req-n1", "consultant_note": "先论文文再电话"}, "k-n1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["candidate"]["consultant_note"] == "先论文文再电话"
        # 同态 PATCH 视为幂等无操作
        resp2 = self._patch(artifact_id, 1, {"request_id": "req-n2", "status": "pending"}, "k-n2")
        assert resp2.status_code == 200, resp2.text
        # rejected 候选也允许改备注（备注不是状态机）
        resp3 = self._patch(artifact_id, 6, {"request_id": "req-n3", "consultant_note": "方向不符"}, "k-n3")
        assert resp3.status_code == 200, resp3.text
        doc = self._doc(artifact_id)
        assert doc["candidates"][1]["consultant_note"] == "先论文文再电话"
        assert doc["candidates"][1]["status"] == "pending"
        assert doc["candidates"][6]["consultant_note"] == "方向不符"

    def test_idempotent_replay_and_audit(self) -> None:
        artifact_id = self._seed_artifact()
        body = {"request_id": "req-i1", "status": "confirmed", "consultant_note": "重点跟进"}
        first = self._patch(artifact_id, 0, body, "k-i1")
        assert first.status_code == 200
        replay = self._patch(artifact_id, 0, body, "k-i1")
        assert replay.status_code == 200
        assert replay.json()["receipt"]["idempotent_replay"] is True
        assert replay.json()["receipt"]["audit_event_id"] == first.json()["receipt"]["audit_event_id"]
        # 同键不同载荷 → 409
        conflict = self._patch(artifact_id, 0, {"request_id": "req-i1", "status": "contacted"}, "k-i1")
        assert conflict.status_code == 409, conflict.text
        # 审计：一次成功写入只落一条 audit_events
        count = self._count(
            "SELECT COUNT(*) FROM audit_events WHERE operation='mapping_task.candidate_update' AND target_id=?",
            (f"{artifact_id}#0",),
        )
        assert count == 1, "审计必须恰好一条（幂等重放不重复记账）"
        audits = self._count("SELECT COUNT(*) FROM api_idempotency WHERE operation='mapping_task.candidate_update'")
        assert audits == 1

    def test_404_and_422_semantics(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            missing = client.patch(
                "/api/v1/mapping-tasks/mapping_task_nope/candidates/0",
                json={"request_id": "req-e1", "status": "confirmed"},
                headers=self._headers("k-e1"),
            )
            assert missing.status_code == 404, missing.text
            out_of_range = client.patch(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/99",
                json={"request_id": "req-e2", "status": "confirmed"},
                headers=self._headers("k-e2"),
            )
            assert out_of_range.status_code == 404, out_of_range.text
            no_fields = client.patch(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/0",
                json={"request_id": "req-e3"},
                headers=self._headers("k-e3"),
            )
            assert no_fields.status_code == 422, no_fields.text
            no_request_id = client.patch(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/0",
                json={"status": "confirmed"},
                headers=self._headers("k-e4"),
            )
            assert no_request_id.status_code == 422, no_request_id.text


# ---------------------------------------------------------------------------
# 2. 破冰素材（confirmed 自动生成 / 反模板硬约束 / 重新生成幂等 / 红线不出现）
# ---------------------------------------------------------------------------

class IcebreakerTest(S52Case):
    def test_confirm_auto_generates_icebreaker_citing_real_clues(self) -> None:
        artifact_id = self._seed_artifact()
        resp = self._patch(artifact_id, 0, {"request_id": "req-ib1", "status": "confirmed"}, "k-ib1")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["icebreaker_generated"] is True
        assert payload["icebreaker_errors"] == []
        icebreaker = payload["candidate"]["icebreaker"]
        assert 1 <= len(icebreaker["hooks"]) <= 3
        assert icebreaker["angle"] in mapping_task.ICEBREAKER_ANGLES
        assert icebreaker["angle"] == "技术共鸣", "论文线索人选应走技术共鸣"
        assert icebreaker["generated_at"].strip() and icebreaker["source_ref"].strip()
        assert PAPER_URL in icebreaker["source_ref"], "source_ref 必须回指所用线索"
        # hooks 必须引用真实线索词（论文题实词/单位/团队名）
        joined = " ".join(icebreaker["hooks"])
        assert "Y-Capacitor" in joined or "EMI" in joined, joined
        assert "晶丰明源" in joined
        # 持久化：GET 读回同一份素材
        doc = self._doc(artifact_id)
        assert doc["candidates"][0]["icebreaker"]["hooks"] == icebreaker["hooks"]
        # 费率/红线词不出现
        encoded = json.dumps(payload, ensure_ascii=False)
        for literal in FORBIDDEN_LITERALS:
            assert literal not in encoded, literal

    def test_anti_template_quality_gate_unit(self) -> None:
        candidate = _candidate("陈**", 0, "晶丰明源 技术论文作者", PAPER_REASON, PAPER_URL)
        team = _teams()[0]
        # 通用句（不含任何线索关键词）必须被拦
        generic = {
            "hooks": ["看您背景很优秀，想认识一下", "您这背景跟我们客户很匹配"],
            "angle": "技术共鸣",
            "generated_at": "2026-07-23 10:00:00",
            "source_ref": PAPER_URL,
        }
        errors = mapping_task.icebreaker_quality_errors(generic, candidate, team)
        assert any("线索关键词" in error for error in errors), errors
        # 红线词必须被拦（即使引用了线索）
        redline = {
            "hooks": ["看到您发的《Conducted EMI Mitigation Schemes》论文，费率好商量"],
            "angle": "技术共鸣",
            "generated_at": "2026-07-23 10:00:00",
            "source_ref": PAPER_URL,
        }
        errors = mapping_task.icebreaker_quality_errors(redline, candidate, team)
        assert any("红线" in error for error in errors), errors
        # 结构不合格被拦
        for broken in (
            {"hooks": [], "angle": "技术共鸣", "generated_at": "x", "source_ref": "y"},
            {"hooks": ["a", "b", "c", "d"], "angle": "技术共鸣", "generated_at": "x", "source_ref": "y"},
            {"hooks": ["ok"], "angle": "平台跃迁", "generated_at": "x", "source_ref": "y"},
            {"hooks": ["ok"], "angle": "技术共鸣", "generated_at": "", "source_ref": "y"},
        ):
            assert mapping_task.icebreaker_quality_errors(broken, candidate, team), broken
        # 无线索关键词的候选无法生成
        with self.assertRaises(ValueError):
            mapping_task.build_icebreaker(
                _candidate("无**", 0, "", " ", PAPER_URL),
                {"company": "", "team": "", "location": ""},
            )

    def test_confirm_quality_failure_not_written(self) -> None:
        artifact_id = self._seed_artifact()
        # 质量门禁判不合格 → 状态变更放行但素材拒绝写入（契约锚点）
        with mock.patch.object(
            mapping_task,
            "icebreaker_quality_errors",
            return_value=["hooks 不含该候选任何线索关键词（论文题/单位/团队实词），判为泛泛模板，拒绝写入"],
        ):
            resp = self._patch(artifact_id, 0, {"request_id": "req-ib2", "status": "confirmed"}, "k-ib2")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["candidate"]["status"] == "confirmed"
        assert payload["icebreaker_generated"] is False
        assert payload["icebreaker_errors"], "质量不合格必须给出人话原因"
        doc = self._doc(artifact_id)
        assert "icebreaker" not in doc["candidates"][0], "判不合格的素材不得写入"

    def test_regenerate_idempotent_and_status_gate(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            # 未确认人选不得重新生成
            pending = client.post(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/0/icebreaker",
                json={"request_id": "req-rg0"},
                headers=self._headers("k-rg0"),
            )
            assert pending.status_code == 409, pending.text
            # 已确认人选重新生成
            first = client.post(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/2/icebreaker",
                json={"request_id": "req-rg1"},
                headers=self._headers("k-rg1"),
            )
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["icebreaker"]["hooks"], payload
            assert payload["candidate"]["icebreaker"]["generated_at"] == payload["icebreaker"]["generated_at"]
            replay = client.post(
                f"/api/v1/mapping-tasks/{artifact_id}/candidates/2/icebreaker",
                json={"request_id": "req-rg1"},
                headers=self._headers("k-rg1"),
            )
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True
            doc = self._doc(artifact_id)
            assert doc["candidates"][2]["icebreaker"]["hooks"] == payload["icebreaker"]["hooks"]


# ---------------------------------------------------------------------------
# 3. 入库动作（confirmed 放行 / 幂等 / 不写第二条 job_candidates / 遮罩合并 / 禁挖）
# ---------------------------------------------------------------------------

class IntakeTest(S52Case):
    JOB_TITLE = "技术市场经理/总监（PC电源）"

    def _intake(self, client: TestClient, artifact_id: str, index: int, key: str, request_id: str = ""):
        return client.post(
            f"/api/v1/mapping-tasks/{artifact_id}/candidates/{index}/intake",
            json={"request_id": request_id or f"req-{key}"},
            headers=self._headers(key),
        )

    def test_intake_success_writes_every_surface_once(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            resp = self._intake(client, artifact_id, 2, "k-in1")
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            assert payload["ok"] is True and payload["status"] == "intaken"
            assert payload["already_intaken"] is False and payload["relation_existed"] is False
            job_candidate_id = payload["job_candidate_id"]
            assert job_candidate_id > 0 and payload["candidate_id"] > 0 and payload["person_id"] > 0
            # 红线不回泄
            encoded = json.dumps(payload, ensure_ascii=False)
            for literal in FORBIDDEN_LITERALS:
                assert literal not in encoded, literal

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                relations = conn.execute(
                    "SELECT * FROM job_candidates WHERE job_id=154 AND raw_position=?", (self.JOB_TITLE,)
                ).fetchall()
                assert len(relations) == 1, "入库必须恰好写一条 job_candidates"
                relation = relations[0]
                assert relation["clean_stage"] == "S1 新增寻访/待复核"
                assert relation["raw_status"] == "mapping_intake"
                assert relation["flow_bucket"] == "待复核"
                person = conn.execute("SELECT * FROM people WHERE id=?", (payload["person_id"],)).fetchone()
                assert person["display_name"] == "沈**", "遮罩名原样存储"
                assert person["current_company"] == "晶丰明源"
                candidate_row = conn.execute(
                    "SELECT * FROM candidates WHERE id=?", (payload["candidate_id"],)
                ).fetchone()
                assert candidate_row["source"] == "mapping"
                assert candidate_row["client"] == "士兰微" and candidate_row["position"] == self.JOB_TITLE
                events = conn.execute(
                    "SELECT * FROM candidate_events WHERE job_candidate_id=? AND event_type='mapping_intake'",
                    (job_candidate_id,),
                ).fetchall()
                assert len(events) == 1, "业务时间线必须落 mapping_intake 事件"
                profiles = conn.execute(
                    "SELECT * FROM source_profiles WHERE person_id=? AND source_type='mapping'",
                    (payload["person_id"],),
                ).fetchall()
                assert len(profiles) == 1
                links = conn.execute(
                    "SELECT * FROM entity_source_links WHERE canonical_id=? AND source_system='mapping'",
                    (str(payload["person_id"]),),
                ).fetchall()
                assert len(links) == 1 and links[0]["source_url"] == PAPER_URL
            finally:
                conn.close()

            # 任务卡回写：status=intaken + 入库回执 + stats
            doc = self._doc(artifact_id)
            assert doc["candidates"][2]["status"] == "intaken"
            assert doc["candidates"][2]["intake"]["job_candidate_id"] == job_candidate_id
            assert doc["stats"]["intaken"] == 2, doc["stats"]
            assert doc["version"] == 1

            # 幂等重放（同键）：同一响应，不再写
            replay = self._intake(client, artifact_id, 2, "k-in1")
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True
            # 业务幂等（新键）：已入库直接回执，不写第二条
            again = self._intake(client, artifact_id, 2, "k-in2")
            assert again.status_code == 200, again.text
            assert again.json()["already_intaken"] is True
            assert again.json()["job_candidate_id"] == job_candidate_id
            count = self._count(
                "SELECT COUNT(*) FROM job_candidates WHERE job_id=154 AND raw_position=?", (self.JOB_TITLE,)
            )
            assert count == 1, "任何重放/重复入库都不得写第二条 job_candidates"
            audit_count = self._count(
                "SELECT COUNT(*) FROM audit_events WHERE operation='mapping_task.candidate_intake'"
            )
            assert audit_count == 2, "首次入库 + 业务幂等回执各记一条审计（重放不重复记）"

    def test_intake_requires_confirmed(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            resp = self._intake(client, artifact_id, 0, "k-in3")
            assert resp.status_code == 409, resp.text
            assert "confirmed" in resp.text
            parked = self._intake(client, artifact_id, 5, "k-in4")
            assert parked.status_code == 409, parked.text
            rejected = self._intake(client, artifact_id, 6, "k-in5")
            assert rejected.status_code == 409, rejected.text
            missing_artifact = self._intake(client, "mapping_task_nope", 0, "k-in6")
            assert missing_artifact.status_code == 404, missing_artifact.text
            out_of_range = self._intake(client, artifact_id, 99, "k-in7")
            assert out_of_range.status_code == 404, out_of_range.text
        count = self._count("SELECT COUNT(*) FROM job_candidates")
        assert count == 0, "非 confirmed 一律不得落 job_candidates"

    def test_intake_merges_masked_name_and_reuses_existing_relation(self) -> None:
        # 预置：全名 王五 的 people + candidates + job_candidates（同人选同岗位关系已存在）
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO people VALUES (1,'王五','晶丰明源半导体','晶丰明源 技术论文作者（资深）','上海','','','王五|晶丰明源半导体|x','2026-07-01')"
        )
        conn.execute(
            """INSERT INTO candidates(id,name,company,title,client,position,source,status)
               VALUES (1,'王五','晶丰明源半导体','晶丰明源 技术论文作者（资深）','士兰微',?,'liepin','new')""",
            (self.JOB_TITLE,),
        )
        conn.execute(
            """INSERT INTO job_candidates(id,job_id,person_id,raw_client,raw_position,raw_status,raw_stage,
               clean_stage,flow_bucket,updated_at,source_candidate_id)
               VALUES (1,154,1,'士兰微',?,'search_shortlisted','S1 新增寻访/待复核','S1 新增寻访/待复核','待复核','2026-07-01','1')""",
            (self.JOB_TITLE,),
        )
        conn.commit()
        conn.close()
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            resp = self._intake(client, artifact_id, 4, "k-in8")
            assert resp.status_code == 200, resp.text
            payload = resp.json()
            # §6.4 遮罩合并：王** 互证 王五（同姓遮罩 + 公司 + 职位双匹配）
            assert payload["person_id"] == 1, payload
            assert payload["person_existed"] is True
            assert payload["relation_existed"] is True
            assert payload["job_candidate_id"] == 1, "已有关系必须复用原 id"
            count = self._count("SELECT COUNT(*) FROM job_candidates")
            assert count == 1, "不写第二条 job_candidates（回归断言）"
            people_count = self._count("SELECT COUNT(*) FROM people")
            assert people_count == 1, "遮罩名互证命中后不得新建 people"
            doc = self._doc(artifact_id)
            assert doc["candidates"][4]["status"] == "intaken"
            assert doc["candidates"][4]["intake"]["relation_existed"] is True

    def test_intake_banned_company_blocked(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            resp = self._intake(client, artifact_id, 3, "k-in9")
            assert resp.status_code == 409, resp.text
            assert "禁挖" in resp.text
            encoded = resp.text
            for literal in FORBIDDEN_LITERALS:
                assert literal not in encoded, literal
        doc = self._doc(artifact_id)
        assert doc["candidates"][3]["status"] == "confirmed", "禁挖拦截不得改动任务卡状态"
        assert self._count("SELECT COUNT(*) FROM job_candidates") == 0

    def test_intake_no_source_blocked_even_if_seeded(self) -> None:
        # 绕过 upsert 校验直接落一份带无来源候选的 artifact（防御纵深用例）
        candidates = _default_candidates()
        candidates[0]["status"] = "confirmed"
        candidates[0]["source_urls"] = []
        doc = {
            "schema_version": "mapping_v1",
            "trigger": "manual",
            "job_id": 154,
            "strategy_ref": "artifact_strategy_154",
            "client": "士兰微",
            "job_title": self.JOB_TITLE,
            "generated_at": "2026-07-23 10:00:00",
            "workflow_id": "wf-154",
            "goal_id": "goal_154",
            "target_teams": _teams(),
            "candidates": candidates,
            "stats": {"teams": 3, "candidates": len(candidates), "confirmed": 1, "intaken": 0},
            "version": 1,
            "history": [],
        }
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES ('mapping_task_wf-154','goal_154','wf-154',NULL,'mapping_task','t','text/markdown','',?,'passed')
            """,
            (json.dumps(doc, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()
        with TestClient(self.app) as client:
            resp = self._intake(client, "mapping_task_wf-154", 0, "k-in10")
            assert resp.status_code == 409, resp.text
            assert "来源" in resp.text
        assert self._count("SELECT COUNT(*) FROM job_candidates") == 0, "无来源人名依旧无法入库"

    def test_intake_stopped_relation_not_resurrected(self) -> None:
        # 预置：同人选同岗位关系已停止推进（H5 初筛不通过）
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO people VALUES (1,'沈默','晶丰明源','晶丰明源 技术论文作者','杭州','','','x','2026-07-01')"
        )
        conn.execute(
            """INSERT INTO job_candidates(id,job_id,person_id,raw_client,raw_position,raw_status,raw_stage,
               clean_stage,flow_bucket,updated_at,source_candidate_id)
               VALUES (1,154,1,'士兰微',?,'screen_rejected','H5 最近寻访/初筛不通过','H5 最近寻访/初筛不通过','最近寻访','2026-07-01','1')""",
            (self.JOB_TITLE,),
        )
        conn.commit()
        conn.close()
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            # 候选 2（沈** 晶丰明源 技术论文作者）遮罩互证 沈默 → 命中已停止关系
            resp = self._intake(client, artifact_id, 2, "k-in11")
            assert resp.status_code == 409, resp.text
            assert "停止" in resp.text
        doc = self._doc(artifact_id)
        assert doc["candidates"][2]["status"] == "confirmed"
        assert self._count("SELECT COUNT(*) FROM job_candidates") == 1, "停止关系不得重复入库"


if __name__ == "__main__":
    unittest.main()
