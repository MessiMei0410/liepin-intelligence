"""S5-3：知识回流（图谱 teams 扩展层 + 评测指标）契约测试。

口径：PRD docs/ASA_PRD_S5_mapping_direct_sourcing_2026-07-23.md §5（知识回流）/§8（度量）/§7（硬性约束）。
全部临时库 + 临时 KB 图谱 JSON，绝不触碰生产 DB 与真实知识库；不打外网、不接 LLM。

覆盖：
- 回流写入：teams 层结构（name/headcount_hint/key_roles/as_of/source_artifact）、as_of 日期、
  幂等（同 artifact 重复回流更新 as_of 不重复条目）、跨 artifact 同团队合并、
  未知公司进 teams_external（不污染原底图）、除 teams 相关键外原文件逐字节保留（diff 断言）、
  禁挖公司跳过 + restricted/候选人名不回泄、无确认团队/全部禁挖拒写、干跑不落盘；
- 触发路由：POST /api/v1/mapping-tasks/{artifact_id}/backflow 的 200/404/409、
  幂等重放、审计恰好一条、业务时间线留痕；
- 指标：GET /api/v1/mapping-tasks/metrics 四项口径（线索有效率/确认→入库转化率/
  Mapping 覆盖率/来源高分率对照）与 null 降级。
"""

from __future__ import annotations

import difflib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import graph_teams_backflow, mapping_metrics, mapping_task  # noqa: E402
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

GRAPH_FIXTURE = {
    "meta": {"version": "v1", "created": "2026-07-23"},
    "stats": {"companies": 2},
    "companies": {
        "晶丰明源": {
            "business": "电源管理芯片",
            "track": "模拟芯片｜电源管理",
            "categories": ["半导体设备"],
        },
        "MPS": {
            "business": "高性能电源方案",
            "track": "模拟芯片｜电源管理",
            "categories": ["精密设备"],
        },
    },
}


def _graph_text() -> str:
    # 图谱文件序列化口径（indent=1/ensure_ascii=False/无尾换行），与真实 kb 文件一致
    return json.dumps(GRAPH_FIXTURE, ensure_ascii=False, indent=1)


def _teams() -> list[dict]:
    return [
        {
            "company": "晶丰明源",
            "team": "服务器 方向 TME 团队",
            "location": "杭州",
            "evidence": [{"type": "图谱", "ref": "kb_company_graph:晶丰明源", "as_of": "2026-07-23"}],
            "confidence": "high",
        },
        {
            "company": "MPS",
            "team": "PC/服务器/ADAS 方向 TME/FAE/AE 团队",
            "location": "",
            "evidence": [{"type": "图谱", "ref": "kb_company_graph:MPS", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
        {
            "company": "杰华特",
            "team": "PC/服务器 方向 TME/FAE 团队",
            "location": "",
            "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:杰华特", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
        {
            "company": "星曜半导体（上海）有限公司",
            "team": "计算电源 方向 TME 团队",
            "location": "上海",
            "evidence": [{"type": "官网", "ref": "https://example.com/team", "as_of": "2026-07-23"}],
            "confidence": "medium",
        },
    ]


def _candidate(name: str, team_ref: int, role: str, *, status: str = "pending", extra: dict | None = None) -> dict:
    candidate = {
        "name": name,
        "current_role": role,
        "team_ref": team_ref,
        "source_urls": [f"https://example.com/clue/{name}"],
        "confidence": "medium",
        "reason": f"公开线索：{role}",
        "status": status,
        "consultant_note": "",
    }
    if extra:
        candidate.update(extra)
    return candidate


def _backflow_candidates() -> list[dict]:
    return [
        # 0 团队0 confirmed（角色 TME）
        _candidate("回测甲", 0, "晶丰明源 TME", status="confirmed"),
        # 1 团队0 contacted（角色 FAE → 同团队第二个关键角色）
        _candidate("回测乙", 0, "晶丰明源 FAE", status="contacted"),
        # 2 团队1 intaken（带入库回执）
        _candidate(
            "回测丙", 1, "MPS 技术论文作者", status="intaken",
            extra={"intake": {"job_candidate_id": 501, "candidate_id": 601, "person_id": 701,
                              "intaken_at": "2026-07-23 09:00:00", "relation_existed": False}},
        ),
        # 3 团队2 confirmed（杰华特：禁挖，回流须跳过）
        _candidate("回测丁", 2, "杰华特 研发（专利发明人）", status="confirmed"),
        # 4 团队3 replied（图谱未覆盖公司 → teams_external）
        _candidate("回测戊", 3, "星曜半导体 TME", status="replied"),
        # 5 团队0 pending（不计入确认）
        _candidate("回测己", 0, "晶丰明源 页面公开联系人", status="pending"),
        # 6 团队1 rejected（不计入确认）
        _candidate("回测庚", 1, "MPS 技术论文作者", status="rejected"),
    ]


def _mapping_doc(workflow_id: str, candidates: list[dict], *, clues: int = 12) -> dict:
    doc = {
        "schema_version": "mapping_v1",
        "trigger": "manual",
        "job_id": 154,
        "strategy_ref": "artifact_strategy_154",
        "client": "士兰微",
        "job_title": "技术市场经理/总监（PC电源）",
        "generated_at": "2026-07-23 10:00:00",
        "workflow_id": workflow_id,
        "goal_id": "goal_154",
        "target_teams": _teams(),
        "candidates": candidates,
        "stats": {"teams": 4, "candidates": len(candidates), "confirmed": 0, "intaken": 0, "clues": clues},
    }
    return doc


class S53Case(unittest.TestCase):
    """临时库 + 临时 KB（图谱 JSON + restricted 禁挖白名单）。"""

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
    """

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        self.kb_dir = Path(self.kb_temp.name) / "kb"
        (self.kb_dir / "cases").mkdir(parents=True, exist_ok=True)
        (self.kb_dir / "cases" / "case_silan_s5_v1.json").write_text(
            json.dumps(CASE_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        self.graph_path = self.kb_dir / "kb_company_graph_jsj_v1.json"
        self.graph_path.write_text(_graph_text(), encoding="utf-8")
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(self.kb_dir)
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        self._create_db()
        self.app = create_app(db_path=self.db_path, start_legacy=False)

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

    def _seed_artifact(self, workflow_id: str = "wf-154", candidates: list[dict] | None = None, clues: int = 12) -> str:
        doc = _mapping_doc(
            workflow_id,
            candidates if candidates is not None else _backflow_candidates(),
            clues=clues,
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            artifact_id = mapping_task.upsert_mapping_task(conn, doc)
            conn.commit()
        finally:
            conn.close()
        return artifact_id

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

    def _graph(self) -> dict:
        return json.loads(self.graph_path.read_text(encoding="utf-8"))

    @staticmethod
    def _post(client: TestClient, url: str, body: dict, key: str):
        return client.post(url, json=body, headers={"Idempotency-Key": key})


# ---------------------------------------------------------------------------
# 1. 回流写入（teams 层结构 / as_of / 幂等 / teams_external / 字节保留 / 红线）
# ---------------------------------------------------------------------------

class BackflowWriteTest(S53Case):
    def test_teams_layer_structure_and_as_of(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        summary = graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24",
            banned=["杰华特"],
        )
        assert summary["ok"] is True and summary["changed"] is True
        assert summary["as_of"] == "2026-07-24"
        assert summary["companies_written"] == 2, summary
        assert summary["teams_written"] == 3  # 晶丰明源 + MPS + 外部公司
        assert summary["teams_inserted"] == 3 and summary["teams_updated"] == 0
        assert summary["external_companies_written"] == 1
        assert summary["skipped_banned"] == 1

        graph = self._graph()
        jfy = graph["companies"]["晶丰明源"]["teams"]
        assert len(jfy) == 1
        entry = jfy[0]
        assert entry["name"] == "服务器 方向 TME 团队"
        assert entry["headcount_hint"] == 2, "团队0 confirmed+contacted 两人（pending 不计）"
        assert entry["key_roles"] == ["晶丰明源 TME", "晶丰明源 FAE"]
        assert entry["as_of"] == "2026-07-24"
        assert entry["source_artifact"] == "mapping_task_wf-154"
        mps = graph["companies"]["MPS"]["teams"]
        assert len(mps) == 1 and mps[0]["headcount_hint"] == 1, "rejected 候选不计入"
        assert mps[0]["key_roles"] == ["MPS 技术论文作者"]

    def test_unknown_company_goes_to_teams_external(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        graph = self._graph()
        assert set(graph["companies"].keys()) == {"晶丰明源", "MPS"}, "原底图公司集合不得新增"
        external = graph["teams_external"]
        assert list(external.keys()) == ["星曜半导体（上海）有限公司"]
        entry = external["星曜半导体（上海）有限公司"][0]
        assert entry["name"] == "计算电源 方向 TME 团队"
        assert entry["headcount_hint"] == 1
        assert entry["key_roles"] == ["星曜半导体 TME"]
        assert entry["as_of"] == "2026-07-24"
        assert entry["source_artifact"] == "mapping_task_wf-154"

    def test_byte_preservation_except_teams_regions(self) -> None:
        before = self.graph_path.read_text(encoding="utf-8")
        # 前置：fixture 本身满足序列化口径（未修改解析 ↔ 原文逐字节一致）
        assert json.dumps(json.loads(before), ensure_ascii=False, indent=1) == before
        doc = _mapping_doc("wf-154", _backflow_candidates())
        graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        after = self.graph_path.read_text(encoding="utf-8")
        assert after != before
        # 剥离 teams 相关键后必须逐字节还原原文件
        cleaned = json.loads(after)
        cleaned.pop("teams_external", None)
        for company_entry in cleaned["companies"].values():
            company_entry.pop("teams", None)
        assert json.dumps(cleaned, ensure_ascii=False, indent=1) == before, "除 teams 区外原文件逐字节保留"
        # diff 行只涉及 teams 内容（公司名/团队名/as_of/数组括号），不含原底图业务字段改动
        removed = [
            line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
            if line.startswith("-") and not line.startswith("---")
        ]
        assert removed == [], f"字节保留语义：不允许有任何删除行，实际：{removed[:5]}"

    def test_idempotent_rerun_updates_as_of_without_duplicates(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        first = self.graph_path.read_text(encoding="utf-8")
        # 同日重跑：无任何变化（不写盘）
        summary_same = graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        assert summary_same["changed"] is False and summary_same["teams_written"] == 0
        assert self.graph_path.read_text(encoding="utf-8") == first
        # 换日重跑：as_of 更新，条目不重复
        summary_next = graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-25", banned=["杰华特"],
        )
        assert summary_next["changed"] is True
        assert summary_next["teams_inserted"] == 0 and summary_next["teams_updated"] == 3
        graph = self._graph()
        assert len(graph["companies"]["晶丰明源"]["teams"]) == 1
        assert graph["companies"]["晶丰明源"]["teams"][0]["as_of"] == "2026-07-25"
        assert len(graph["teams_external"]["星曜半导体（上海）有限公司"]) == 1

    def test_cross_artifact_merge_same_team(self) -> None:
        doc1 = _mapping_doc("wf-154", _backflow_candidates())
        graph_teams_backflow.backflow_teams(
            self.graph_path, doc1, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        # 另一岗位的 artifact：同公司同团队（空白差异归一后为同一条），新增一个角色与更高人数
        candidates2 = [
            _candidate("回测辛", 0, "晶丰明源 资深AE", status="confirmed"),
            _candidate("回测壬", 0, "晶丰明源 TME", status="confirmed"),
            _candidate("回测癸", 0, "晶丰明源 FAE", status="contacted"),
        ]
        doc2 = _mapping_doc("wf-200", candidates2)
        doc2["target_teams"][0]["team"] = "服务器 方向 TME 团队"  # 与 doc1 同团队
        summary = graph_teams_backflow.backflow_teams(
            self.graph_path, doc2, artifact_id="mapping_task_wf-200", as_of="2026-07-25", banned=["杰华特"],
        )
        assert summary["teams_inserted"] == 0 and summary["teams_updated"] == 1
        entry = self._graph()["companies"]["晶丰明源"]["teams"][0]
        assert len(self._graph()["companies"]["晶丰明源"]["teams"]) == 1, "跨 artifact 同团队合并为单条"
        assert entry["headcount_hint"] == 3, "headcount_hint 取 max（跨岗位不可加和）"
        assert entry["key_roles"] == ["晶丰明源 TME", "晶丰明源 FAE", "晶丰明源 资深AE"]
        assert entry["as_of"] == "2026-07-25"
        assert entry["source_artifact"] == "mapping_task_wf-200"

    def test_banned_skipped_and_no_restricted_leak(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        summary = graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
        )
        assert summary["skipped_banned"] == 1
        text = self.graph_path.read_text(encoding="utf-8")
        assert "杰华特" not in text, "禁挖公司团队不得进图谱"
        for literal in FORBIDDEN_LITERALS:
            assert literal not in text, f"restricted 内容不得回泄：{literal}"
        # 候选人名（无论遮罩与否）一律不进图谱
        for candidate in _backflow_candidates():
            assert candidate["name"] not in text, f"候选人名不得进图谱：{candidate['name']}"
            for url in candidate["source_urls"]:
                assert url not in text, "候选来源 URL 不进图谱"

    def test_all_banned_rejected(self) -> None:
        candidates = [_candidate("回测丁", 2, "杰华特 研发", status="confirmed")]
        doc = _mapping_doc("wf-154", candidates)
        before = self.graph_path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            graph_teams_backflow.backflow_teams(
                self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=["杰华特"],
            )
        assert self.graph_path.read_text(encoding="utf-8") == before, "拒写时图谱不得被改动"

    def test_no_confirmed_teams_rejected(self) -> None:
        candidates = [
            _candidate("回测甲", 0, "晶丰明源 TME", status="pending"),
            _candidate("回测庚", 1, "MPS 技术论文作者", status="rejected"),
        ]
        doc = _mapping_doc("wf-154", candidates)
        with self.assertRaises(ValueError):
            graph_teams_backflow.backflow_teams(
                self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24", banned=[],
            )

    def test_dry_run_does_not_write(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        before = self.graph_path.read_text(encoding="utf-8")
        summary = graph_teams_backflow.backflow_teams(
            self.graph_path, doc, artifact_id="mapping_task_wf-154", as_of="2026-07-24",
            banned=["杰华特"], write=False,
        )
        assert summary["dry_run"] is True and summary["changed"] is True
        assert summary["teams_written"] == 3
        assert self.graph_path.read_text(encoding="utf-8") == before, "干跑不得落盘"

    def test_missing_or_broken_graph_rejected(self) -> None:
        doc = _mapping_doc("wf-154", _backflow_candidates())
        with self.assertRaises(ValueError):
            graph_teams_backflow.backflow_teams(
                self.kb_dir / "no_such_graph.json", doc, artifact_id="x", as_of="2026-07-24", banned=[],
            )
        broken = self.kb_dir / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            graph_teams_backflow.backflow_teams(broken, doc, artifact_id="x", as_of="2026-07-24", banned=[])


# ---------------------------------------------------------------------------
# 2. 触发路由（POST /api/v1/mapping-tasks/{artifact_id}/backflow）
# ---------------------------------------------------------------------------

class BackflowRouteTest(S53Case):
    def test_route_200_summary_and_audit_and_timeline(self) -> None:
        artifact_id = self._seed_artifact()
        with TestClient(self.app) as client:
            resp = self._post(client, f"/api/v1/mapping-tasks/{artifact_id}/backflow",
                              {"request_id": "req-b1"}, "k-b1")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["artifact_id"] == artifact_id
        assert payload["as_of"], "返回写入摘要必须带 as_of"
        assert payload["companies_written"] == 2 and payload["teams_written"] == 3
        assert payload["external_companies_written"] == 1 and payload["skipped_banned"] == 1
        assert payload["receipt"]["idempotent_replay"] is False
        # 图谱实际落盘
        graph = self._graph()
        assert graph["companies"]["晶丰明源"]["teams"][0]["as_of"] == payload["as_of"]
        # 审计恰好一条
        count = self._count(
            "SELECT COUNT(*) FROM audit_events WHERE operation='mapping_task.backflow' AND target_id=?",
            (artifact_id,),
        )
        assert count == 1
        # 业务时间线留痕
        events = self._count(
            "SELECT COUNT(*) FROM candidate_events WHERE event_type='mapping_task_backflow' AND source_id=?",
            (artifact_id,),
        )
        assert events == 1

    def test_route_404(self) -> None:
        with TestClient(self.app) as client:
            resp = self._post(client, "/api/v1/mapping-tasks/mapping_task_nope/backflow",
                              {"request_id": "req-b404"}, "k-b404")
        assert resp.status_code == 404, resp.text

    def test_route_409_no_confirmed(self) -> None:
        artifact_id = self._seed_artifact(candidates=[
            _candidate("回测甲", 0, "晶丰明源 TME", status="pending"),
        ])
        with TestClient(self.app) as client:
            resp = self._post(client, f"/api/v1/mapping-tasks/{artifact_id}/backflow",
                              {"request_id": "req-b409"}, "k-b409")
        assert resp.status_code == 409, resp.text
        assert "已确认" in resp.text

    def test_route_idempotent_replay_and_cross_key_idempotency(self) -> None:
        artifact_id = self._seed_artifact()
        body = {"request_id": "req-b2"}
        with TestClient(self.app) as client:
            first = self._post(client, f"/api/v1/mapping-tasks/{artifact_id}/backflow", body, "k-b2")
            assert first.status_code == 200
            replay = self._post(client, f"/api/v1/mapping-tasks/{artifact_id}/backflow", body, "k-b2")
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True
            assert replay.json()["receipt"]["audit_event_id"] == first.json()["receipt"]["audit_event_id"]
            # 换键再触发：业务幂等（条目不重复，同日 as_of 不变化 → 不写盘）
            second = self._post(client, f"/api/v1/mapping-tasks/{artifact_id}/backflow",
                                {"request_id": "req-b3"}, "k-b3")
            assert second.status_code == 200
            assert second.json()["changed"] is False
        graph = self._graph()
        assert len(graph["companies"]["晶丰明源"]["teams"]) == 1
        assert len(graph["teams_external"]["星曜半导体（上海）有限公司"]) == 1
        count = self._count(
            "SELECT COUNT(*) FROM audit_events WHERE operation='mapping_task.backflow' AND target_id=?",
            (artifact_id,),
        )
        assert count == 2, "两次不同键各记一条审计；重放不重复记账"
        # 换键后图谱落盘仍只有一次（第一次触发）——teams 条目唯一已在上断言


# ---------------------------------------------------------------------------
# 3. 评测指标（GET /api/v1/mapping-tasks/metrics）
# ---------------------------------------------------------------------------

class MappingMetricsTest(S53Case):
    def _seed_assessments(self) -> None:
        """来源高分率对照数据：mapping 3 人（2 高分）、liepin 3 人（2 高分）、xsaas 1 人（样本不足）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            sources = ["mapping", "mapping", "mapping", "liepin", "liepin", "liepin", "xsaas"]
            scores = [80, 70, 90, 80, 60, 90, 88]
            for index, (source, score) in enumerate(zip(sources, scores), start=1):
                conn.execute(
                    "INSERT INTO candidates (id,name,company,title,client,position,source,xsaas_id)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (index, f"候选{index}", "某公司", "TME", "士兰微", "技术市场经理/总监（PC电源）", source, ""),
                )
                conn.execute(
                    "INSERT INTO job_candidates (id,job_id,person_id,raw_client,raw_position,source_candidate_id)"
                    " VALUES (?,?,?,?,?,?)",
                    (index, 154, 1000 + index, "士兰微", "技术市场经理/总监（PC电源）", str(index)),
                )
                conn.execute(
                    """
                    INSERT INTO agent_candidate_assessments
                    (run_id,job_candidate_id,candidate_id,person_id,job_id,client,job,snapshot_hash,
                     assessment_version,fit_score,fit_level,recommendation,confidence,evidence_coverage,is_current)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                    """,
                    (f"run-{index}", index, index, 1000 + index, 154, "士兰微", "技术市场经理/总监（PC电源）",
                     f"hash-{index}", "v1", score, "high" if score >= 75 else "mid", "recommend", 0.8, 0.9),
                )
            conn.commit()
        finally:
            conn.close()

    def _seed_funnel(self) -> None:
        """池枯竭覆盖数据：wf-154（枯竭+有 mapping）、wf-300（枯竭无 mapping）、wf-400（未枯竭）。"""
        conn = sqlite3.connect(self.db_path)
        try:
            rows = [
                ("run-a", "wf-154", 10, 9),
                ("run-b", "wf-300", 10, 9),
                ("run-c", "wf-400", 10, 1),
                ("run-d", "wf-500", 0, 0),  # extracted=0 不计入分母
            ]
            for run_id, workflow_id, extracted, dedupe in rows:
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_funnel
                    (run_id,workflow_id,job_id,client,job,channel,status,extracted_count,dedupe_count)
                    VALUES (?,?,154,'士兰微','技术市场经理/总监（PC电源）','liepin','completed',?,?)
                    """,
                    (run_id, workflow_id, extracted, dedupe),
                )
            conn.commit()
        finally:
            conn.close()

    def test_metrics_four_dimensions(self) -> None:
        # 两份任务卡：wf-154（clues=12：confirmed+contacted+intaken+replied=5，intaken=1）、
        # wf-200（clues=8：confirmed=1，intaken=0）
        self._seed_artifact("wf-154", _backflow_candidates(), clues=12)
        doc2_candidates = [_candidate("回测子", 0, "晶丰明源 TME", status="confirmed")]
        artifact2 = self._seed_artifact("wf-200", doc2_candidates, clues=8)
        assert artifact2 == "mapping_task_wf-200"
        self._seed_funnel()
        self._seed_assessments()

        with TestClient(self.app) as client:
            resp = client.get("/api/v1/mapping-tasks/metrics")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True and payload["generated_at"]
        metrics = payload["metrics"]
        assert metrics["artifacts_aggregated"] == 2

        clue = metrics["clue_effectiveness"]
        assert clue["clues_total"] == 20
        assert clue["confirmed_plus_total"] == 6  # 5（wf-154）+1（wf-200）
        assert clue["rate"] == round(6 / 20, 4)

        conversion = metrics["confirm_to_intake"]
        assert conversion["intaken_total"] == 1
        assert conversion["confirmed_plus_total"] == 6
        assert conversion["rate"] == round(1 / 6, 4)

        coverage = metrics["mapping_coverage"]
        assert coverage["exhausted_workflows"] == 2, "wf-154 与 wf-300 枯竭；wf-400/500 不计"
        assert coverage["with_mapping"] == 1
        assert coverage["coverage"] == 0.5
        assert coverage["dedupe_rate_threshold"] == 0.8

        high = metrics["high_score_by_source"]
        assert high["high_score_floor"] == 75
        groups = high["groups"]
        assert groups["mapping"] == {"assessed": 3, "high": 2, "high_rate": round(2 / 3, 4)}
        assert groups["liepin"] == {"assessed": 3, "high": 2, "high_rate": round(2 / 3, 4)}
        assert groups["xsaas"] == {"assessed": 1, "high": 1, "high_rate": None}, "样本不足如实 null"
        comparison = high["comparison"]
        assert comparison["resume_assessed"] == 4 and comparison["resume_high"] == 3
        assert comparison["resume_high_rate"] == 0.75
        assert comparison["mapping_high_rate"] == round(2 / 3, 4)
        assert comparison["delta_mapping_vs_resume"] == round(round(2 / 3, 4) - 0.75, 4)

    def test_metrics_null_degradation_on_empty_db(self) -> None:
        with TestClient(self.app) as client:
            resp = client.get("/api/v1/mapping-tasks/metrics")
        assert resp.status_code == 200, resp.text
        metrics = resp.json()["metrics"]
        assert metrics["artifacts_aggregated"] == 0
        assert metrics["clue_effectiveness"]["rate"] is None
        assert metrics["confirm_to_intake"]["rate"] is None
        assert metrics["mapping_coverage"]["coverage"] is None
        assert metrics["mapping_coverage"]["exhausted_workflows"] == 0
        groups = metrics["high_score_by_source"]["groups"]
        assert all(group["high_rate"] is None and group["assessed"] == 0 for group in groups.values())
        assert metrics["high_score_by_source"]["comparison"]["delta_mapping_vs_resume"] is None

    def test_metrics_no_restricted_or_name_leak(self) -> None:
        self._seed_artifact()
        self._seed_assessments()
        with TestClient(self.app) as client:
            resp = client.get("/api/v1/mapping-tasks/metrics")
        text = resp.text
        for literal in FORBIDDEN_LITERALS:
            assert literal not in text
        for candidate in _backflow_candidates():
            assert candidate["name"] not in text, "指标输出只有聚合计数，候选人名不得出现"

    def test_compute_metrics_direct_function(self) -> None:
        # 模块级直调（临时库连接），与路由同口径
        self._seed_artifact("wf-154", _backflow_candidates(), clues=12)
        conn = sqlite3.connect(self.db_path)
        try:
            metrics = mapping_metrics.compute_mapping_metrics(conn)
        finally:
            conn.close()
        assert metrics["clue_effectiveness"]["clues_total"] == 12
        assert metrics["clue_effectiveness"]["confirmed_plus_total"] == 5
        assert metrics["clue_effectiveness"]["rate"] == round(5 / 12, 4)
        assert metrics["confirm_to_intake"]["rate"] == round(1 / 5, 4)


if __name__ == "__main__":
    unittest.main()
