"""S7-2：雷达联动 —— 一键发起 Mapping（trigger=radar）/ 激活存量清单 / 信号注入动机维度。

口径：docs/TASKCARD_S7-2_雷达联动_20260724.md（三件事/红线/验收）+ PRD S7 §2/§5。
全部使用临时库 + 临时 KB fixture + FakeLLM + stub 采集器，绝不触碰生产 DB、真实知识库与外网。

覆盖（对应任务卡验收 1/2/3/4）：
1. trigger=radar 合法性：枚举校验；strategy_ref 空仅 radar 放行；bogus trigger 拒收。
2. start-mapping 路由：200、trigger=radar、目标团队≥1、stats.radar_context 标记；
   同日重复发起幂等（already_exists，不重复建 task）；Idempotency-Key 重放返回首次响应；
   信号正文/来源链接不进 artifact 对外字段。
3. 激活清单路由：现职/曾任职清单字段齐全（id/遮罩名/职务/入库阶段/最近动作日期），
   别名公司命中（MPS ↔ 美国芯源系统有限公司 (MPS)），无榜单 404，只读。
4. 动机注入：现职公司有未过期信号 → motivation evidence 出现 type="雷达信号" 条目
   （带 as_of + 来源链接 + "推测"标注），confidence 封顶 inferred；
   无信号/信号过期/无榜单 → 不注入，行为与之前一致（回归）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import candidate_assessment, mapping_task, radar_scan  # noqa: E402
from a_system_agent.llm import FakeLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.app import create_app  # noqa: E402

TODAY = date(2026, 7, 24)

STRATEGY_V2 = {
    "schema_version": "strategy_v2",
    "input_level": "L1",
    "step1_job_essence": {"statement": "计算电源方向技术市场岗", "value_chain_role": "TME", "confirmed_by": "consultant"},
    "step2_target_pool": [
        {
            "path": "same_layer", "tier": "T1",
            "companies": [
                {"name": "晶丰明源", "source": "client_doc", "confidence": "high"},
                {"name": "MPS", "source": "client_doc", "confidence": "high"},
            ],
            "rationale": "同层友商",
        },
    ],
    "step3_level_mapping": {"accepted_levels": ["经理", "总监"], "calibration_rule": "按职责定档"},
    "step4_keyword_groups": [{"group": "core", "targets": "T1 友商", "terms": ["多相控制器", "DrMOS"]}],
    "step5_expectation": {"expected_recall_per_tier": {"T1": 10}, "fallback_plan": ""},
    "negative_rules": [],
    "consultant_edits": [],
    "archetype_id": "tme_s72",
}

SEED_FIXTURE = {
    "meta": {"created": "2026-07-01"},
    "job_archetype": {
        "archetype_id": "tme_s72",
        "title": "技术市场经理（计算电源）",
        "client": "士兰微",
        "essence": "计算电源方向技术市场岗",
        "directions": [{"name": "PC", "products": ["多相控制器", "DrMOS"], "competitors": ["MPS"]}],
        "target_functions": ["TME", "FAE"],
        "location_policy": "杭州优先",
    },
    "target_company_pool": {
        "T1_competitor_device": {
            "rationale": "同层友商",
            "companies": [{"name": "MPS", "tier": "T1", "directions": ["PC", "服务器"]}],
        },
    },
    "keyword_groups": [],
    "negative_rules": [],
    "level_mapping": {},
    "source_file": "seed_s72_tme_v1.json",
}

GRAPH_FIXTURE = {"companies": {}}
CASE_FIXTURE = {
    "meta": {"case_id": "case_silan_s72"},
    "client_profile": {"name": "士兰微"},
    "restricted": {"banned_companies": [], "consultant_phone": "13912345678", "fee_rate": "费率23%"},
}
FORBIDDEN_LITERALS = ["13912345678", "费率23%"]

RESUME_TEXT = (
    "刘** 求职期望：上海 技术市场经理\n"
    "2019.03-至今 美国芯源系统有限公司 (MPS) · 资深技术销售工程师 负责PC电源多相控制器客户导入\n"
    "2015.06-2019.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持\n"
    "浙江大学 · 电子科学与技术 · 本科 2011.09-2015.06"
)

GOOD_LLM = {
    "trajectory": {
        "verdict": "从 FAE 转技术市场，轨迹上行",
        "segments": [
            {"company": "美国芯源系统有限公司 (MPS)", "title": "资深技术销售工程师", "period": "2019.03-至今",
             "tier": "T1", "tier_source": "graph", "team": "", "report_line": "", "note": "PC电源"},
        ],
        "promotion_pace": "steady",
        "tech_evolution": "stable",
        "evidence": [{"type": "简历", "ref": "2019.03-至今 美国芯源系统有限公司 (MPS) · 资深技术销售工程师"}],
        "confidence": "certain",
    },
    "move_history": {
        "verdict": "一次跳槽上行",
        "moves": [
            {"from": "晶丰明源", "to": "MPS", "direction": "up", "platform": "up",
             "title_direction": "up", "responsibility_direction": "up", "reason": "FAE 转技术市场"},
        ],
        "current_move": "lateral",
        "evidence": [{"type": "简历", "ref": "2015.06-2019.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师"}],
        "confidence": "certain",
    },
    "consultant_summary": "技术市场线轨迹清晰。",
}

GOOD_PM = {
    "percentile": {"verdict": "同方向同龄人中处于靠前位置", "evidence": [], "confidence": "certain"},
    # 刻意给 certain：验证雷达信号注入后 confidence 被确定性封顶 inferred
    "motivation": {"verdict": "在职时长已超其历史平均任期，存在变动的可能", "evidence": [], "confidence": "certain"},
}

API_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,summary TEXT,hard_requirements TEXT);
CREATE TABLE positions(id INTEGER PRIMARY KEY,client TEXT,title TEXT);
CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT,current_title TEXT,
  city TEXT,education TEXT,experience TEXT);
CREATE TABLE candidates(id INTEGER PRIMARY KEY,name TEXT,company TEXT,title TEXT,education TEXT,
  experience TEXT,skills TEXT,city TEXT,client TEXT,position TEXT,source TEXT,xsaas_id TEXT,
  search_date TEXT,status TEXT,notes TEXT,updated_at TEXT);
CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,raw_client TEXT,
  raw_position TEXT,raw_status TEXT,raw_stage TEXT,clean_stage TEXT,flow_bucket TEXT,updated_at TEXT,
  source_candidate_id TEXT);
CREATE TABLE candidate_events(id INTEGER PRIMARY KEY,job_candidate_id INTEGER,person_id INTEGER,job_id INTEGER,
  event_type TEXT,event_status TEXT,event_time TEXT,summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
CREATE TABLE source_profiles(id INTEGER PRIMARY KEY,person_id INTEGER,source_type TEXT,
  source_candidate_id TEXT,source_date TEXT,raw_status TEXT,raw_client TEXT,raw_position TEXT,raw_json TEXT);
"""


def _valid_signal(**overrides) -> dict:
    signal = {
        "company": "MPS",
        "type": "org_change",
        "summary": "公开报道显示MPS高管变动",
        "implication": "骨干观望期，可关注",
        "source_urls": ["https://example.com/news/mps-1"],
        "as_of": "2026-07-20",
        "confidence": "medium",
        "linked_action": "mapping",
    }
    signal.update(overrides)
    return signal


def _valid_radar_doc(**overrides) -> dict:
    doc = {
        "schema_version": "radar_v1",
        "scan_date": "2026-07-24",
        "generated_at": "2026-07-24 10:00:00",
        "company_pool": [{"company": "MPS", "origin": "client_profile"}],
        "signals": [_valid_signal()],
        "ranking": [
            {"company": "MPS", "score": 3.0, "reason": "信号 1 条（组织/高管变动×1）", "suggested_action": "mapping"}
        ],
        "stats": {"companies_scanned": 1, "companies_with_signals": 1, "signals_found": 1, "sources_failed": 0},
    }
    doc.update(overrides)
    return doc


def _write_kb(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    (base / "seed_s72_tme_v1.json").write_text(json.dumps(SEED_FIXTURE, ensure_ascii=False), encoding="utf-8")
    (base / "kb_company_graph_jsj_v1.json").write_text(json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8")
    (base / "cases").mkdir(parents=True, exist_ok=True)
    (base / "cases" / "case_silan_s72_v1.json").write_text(json.dumps(CASE_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return base


class DbCase(unittest.TestCase):
    """临时 KB + 临时库（ensure_schema 全表），运行时只读 fixture，绝不碰生产。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = _write_kb(Path(self.kb_temp.name) / "kb")
        self.kb_dir = kb_dir
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(API_SCHEMA)
            ensure_schema(conn)
            conn.execute("INSERT INTO clients VALUES (1,'士兰微')")
            conn.execute(
                "INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）','PC电源技术市场','5年以上电源芯片经验')"
            )
            conn.execute("INSERT INTO jobs VALUES (155,1,'无策略岗位','','')")
            conn.commit()
        finally:
            conn.close()
        self.radar_dir = Path(self.db_temp.name) / "radar_out"

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()
        self.db_temp.cleanup()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed_strategy(self, job_id: int = 154, with_strategy: bool = True) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)
                VALUES (?,?,?,?,?,?,'blocked')
                """,
                (f"goal_{job_id}", "寻访", "寻访", "job", job_id, json.dumps({"type": "job", "id": job_id})),
            )
            conn.execute(
                "INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES (?,?,'blocked')",
                (f"wf-{job_id}", f"goal_{job_id}"),
            )
            if with_strategy:
                conn.execute(
                    """
                    INSERT INTO agent_artifacts
                    (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
                    VALUES (?,?,?,NULL,'search_strategy','多渠道寻访策略','text/markdown','# 策略',?,'passed')
                    """,
                    (
                        f"artifact_strategy_{job_id}",
                        f"goal_{job_id}",
                        f"wf-{job_id}",
                        json.dumps({"strategy_v2": STRATEGY_V2}, ensure_ascii=False),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def seed_radar(self, signals: list[dict] | None = None, scan_date: str = "2026-07-24") -> str:
        conn = sqlite3.connect(self.db_path)
        try:
            doc = _valid_radar_doc(scan_date=scan_date)
            if signals is not None:
                doc["signals"] = signals
            artifact_id = radar_scan.upsert_radar_scan(conn, doc, radar_dir=self.radar_dir)
            conn.commit()
            return artifact_id
        finally:
            conn.close()


def _valid_mapping_doc(**overrides) -> dict:
    doc = {
        "schema_version": "mapping_v1",
        "trigger": "radar",
        "job_id": 154,
        "strategy_ref": "artifact_strategy_154",
        "target_teams": [
            {
                "company": "MPS",
                "team": "PC/服务器 方向 TME/FAE 团队",
                "location": "杭州",
                "evidence": [{"type": "图谱", "ref": "seed#target_company_pool:MPS", "as_of": "2026-07-24"}],
                "confidence": "medium",
            }
        ],
        "candidates": [
            {
                "name": "李**",
                "current_role": "MPS 研发（专利发明人）",
                "team_ref": 0,
                "source_urls": ["https://patents.google.com/patent/CN111/zh"],
                "confidence": "medium",
                "reason": "公开专利发明人",
                "status": "pending",
                "consultant_note": "",
            }
        ],
        "stats": {"teams": 1, "candidates": 1, "confirmed": 0, "intaken": 0},
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# 1. trigger=radar 合法性（契约）
# ---------------------------------------------------------------------------

class TriggerContractTest(unittest.TestCase):
    def test_radar_trigger_accepted(self) -> None:
        assert mapping_task.validate_mapping_task(_valid_mapping_doc()) == []
        assert "radar" in mapping_task.TRIGGERS

    def test_bogus_trigger_rejected(self) -> None:
        doc = _valid_mapping_doc(trigger="auto")
        assert any("trigger" in error for error in mapping_task.validate_mapping_task(doc))

    def test_empty_strategy_ref_only_allowed_for_radar(self) -> None:
        radar_doc = _valid_mapping_doc(strategy_ref="")
        assert mapping_task.validate_mapping_task(radar_doc) == [], "radar 触发允许 strategy_ref 为空"
        manual_doc = _valid_mapping_doc(trigger="manual", strategy_ref="")
        assert any("strategy_ref" in error for error in mapping_task.validate_mapping_task(manual_doc))


# ---------------------------------------------------------------------------
# 2. build 层：radar_context 注入定位（stats 标记 + 信号内容不进对外字段）
# ---------------------------------------------------------------------------

class BuildRadarContextTest(unittest.TestCase):
    def test_radar_context_marker_and_no_signal_leak(self) -> None:
        radar_context = {
            "mps": [
                {
                    "type": "org_change",
                    "summary": "公开报道显示MPS高管变动",
                    "implication": "骨干观望期，可关注",
                    "as_of": "2026-07-20",
                    "confidence": "medium",
                    "linked_action": "mapping",
                }
            ]
        }

        class _EmptyCollector:
            def collect_company(self, company, **_kwargs):
                return {"evidence": [], "clues": [], "failures": [], "pages_fetched": 0, "location": ""}

        doc = mapping_task.build_mapping_task(
            job_id=154,
            trigger="radar",
            strategy_ref="artifact_strategy_154",
            strategy_doc=STRATEGY_V2,
            client="士兰微",
            job_title="技术市场经理/总监（PC电源）",
            collector=_EmptyCollector(),
            banned=[],
            as_of="2026-07-24",
            radar_context=radar_context,
            radar_company="MPS",
            radar_scan_ref="radar_scan_2026-07-24",
        )
        marker = doc["stats"].get("radar_context") or {}
        assert marker.get("applied") is True
        assert marker.get("company") == "MPS"
        assert marker.get("company_in_pool") is True
        assert marker.get("scan_artifact") == "radar_scan_2026-07-24"
        assert marker.get("pool_companies_with_signals") >= 1
        assert doc["stats"]["teams"] >= 1
        # 窗口期公司排最前
        assert doc["target_teams"][0]["company"] == "MPS"
        # 信号正文/链接不进对外字段（target_teams/candidates/content）
        outward = json.dumps(
            {"target_teams": doc["target_teams"], "candidates": doc["candidates"]}, ensure_ascii=False
        )
        assert "公开报道显示MPS高管变动" not in outward
        assert "https://example.com/news/mps-1" not in outward
        assert mapping_task.validate_mapping_task(doc) == []

    def test_radar_context_empty_when_no_signals(self) -> None:
        class _EmptyCollector:
            def collect_company(self, company, **_kwargs):
                return {"evidence": [], "clues": [], "failures": [], "pages_fetched": 0, "location": ""}

        doc = mapping_task.build_mapping_task(
            job_id=154,
            trigger="radar",
            strategy_ref="",
            strategy_doc=None,
            client="士兰微",
            job_title="无策略岗位",
            collector=_EmptyCollector(),
            banned=[],
            as_of="2026-07-24",
            radar_company="MPS",
            radar_scan_ref="radar_scan_2026-07-24",
        )
        marker = doc["stats"]["radar_context"]
        assert marker["applied"] is False
        assert "strategy_ref_missing" in marker, "无 strategy_v2 必须在 stats 注明"
        assert mapping_task.validate_mapping_task(doc) == [], "radar 触发 + 空 strategy_ref 校验必须通过"


# ---------------------------------------------------------------------------
# 3. start-mapping 路由（端到端契约 + 同日幂等）
# ---------------------------------------------------------------------------

class _ClueCollector:
    def collect_company(self, company, **_kwargs):
        return {
            "evidence": [{"type": "官网", "ref": "https://example.com/careers", "as_of": "2026-07-24"}],
            "clues": [
                {"kind": "专利", "company": company, "name": "李四",
                 "current_role": f"{company} 研发（专利发明人）",
                 "source_url": "https://patents.google.com/patent/CN111/zh",
                 "confidence": "medium", "reason": "测试线索"},
            ],
            "failures": [],
            "pages_fetched": 1,
            "location": "杭州",
        }


class StartMappingApiTest(DbCase):
    def test_start_mapping_end_to_end_and_idempotent(self) -> None:
        self.seed_strategy(154)
        self.seed_strategy(155, with_strategy=False)  # 有工作流、无 strategy_v2：strategy_ref 按 null
        self.seed_radar()
        app = create_app(db_path=self.db_path, start_legacy=False)
        with TestClient(app) as client, mock.patch.object(mapping_task, "MappingCollector", _ClueCollector):
            missing_job = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r0", "company": "MPS", "job_id": 999},
                headers={"Idempotency-Key": "k-r0"},
            )
            assert missing_job.status_code == 404, missing_job.text

            first = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r1", "company": "MPS", "job_id": 154},
                headers={"Idempotency-Key": "k-r1"},
            )
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["ok"] is True and payload["already_exists"] is False
            doc = payload["mapping_task"]
            assert doc["trigger"] == "radar"
            assert doc["strategy_ref"] == "artifact_strategy_154"
            assert doc["stats"]["teams"] >= 1 and len(doc["target_teams"]) >= 1
            marker = doc["stats"].get("radar_context") or {}
            assert marker.get("applied") is True and marker.get("company") == "MPS"
            assert marker.get("scan_artifact") == "radar_scan_2026-07-24"
            artifact_id = payload["artifact_id"]
            # 对外字段不带信号正文/链接；restricted 不回泄
            encoded = json.dumps(payload, ensure_ascii=False)
            assert "公开报道显示MPS高管变动" not in json.dumps(
                {"target_teams": doc["target_teams"], "candidates": doc["candidates"]}, ensure_ascii=False
            )
            for literal in FORBIDDEN_LITERALS:
                assert literal not in encoded, literal

            # 同日重复发起（不同幂等键）→ 返回已存在，不重复建 task（version 不变）
            second = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r2", "company": "MPS", "job_id": 154},
                headers={"Idempotency-Key": "k-r2"},
            )
            assert second.status_code == 200, second.text
            replay = second.json()
            assert replay["already_exists"] is True
            assert replay["artifact_id"] == artifact_id
            assert replay["mapping_task"].get("version") == doc.get("version")

            # 同一 Idempotency-Key 重放 → 返回首次响应
            again = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r1", "company": "MPS", "job_id": 154},
                headers={"Idempotency-Key": "k-r1"},
            )
            assert again.status_code == 200
            assert again.json()["receipt"]["idempotent_replay"] is True

            # 无策略岗位：允许发起（strategy_ref 按 null），stats 注明
            no_strategy = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r3", "company": "MPS", "job_id": 155},
                headers={"Idempotency-Key": "k-r3"},
            )
            assert no_strategy.status_code == 200, no_strategy.text
            doc155 = no_strategy.json()["mapping_task"]
            assert doc155["trigger"] == "radar" and not doc155["strategy_ref"]
            assert "strategy_ref_missing" in (doc155["stats"].get("radar_context") or {})

    def test_start_mapping_requires_scan(self) -> None:
        self.seed_strategy(154)
        app = create_app(db_path=self.db_path, start_legacy=False)
        with TestClient(app) as client, mock.patch.object(mapping_task, "MappingCollector", _ClueCollector):
            response = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r9", "company": "MPS", "job_id": 154},
                headers={"Idempotency-Key": "k-r9"},
            )
            assert response.status_code == 404, "无雷达榜单不得发起 radar Mapping"

    def test_start_mapping_requires_company(self) -> None:
        self.seed_strategy(154)
        self.seed_radar()
        app = create_app(db_path=self.db_path, start_legacy=False)
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/radar/scans/latest/actions/start-mapping",
                json={"request_id": "req-r8", "company": "", "job_id": 154},
                headers={"Idempotency-Key": "k-r8"},
            )
            assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4. 激活清单路由（现职/曾任职，字段齐全，别名命中，只读）
# ---------------------------------------------------------------------------

class ActivateApiTest(DbCase):
    def _seed_talent(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO candidates(id,name,company,title,status,updated_at,search_date)"
                " VALUES (1189,'刘一楠','美国芯源系统有限公司 (MPS)','资深技术销售工程师','new','2026-07-01 10:00:00','2026-07-01')"
            )
            conn.execute(
                "INSERT INTO candidates(id,name,company,title,status,updated_at,search_date)"
                " VALUES (1182,'邓先生','矽力杰半导体技术(杭州)有限公司','产品市场经理','contacted','2026-07-02 10:00:00','2026-07-02')"
            )
            conn.execute(
                "INSERT INTO candidates(id,name,company,title,status,updated_at,search_date)"
                " VALUES (2001,'王某某','杰华特微电子股份有限公司','工程师','new','2026-07-03 10:00:00','2026-07-03')"
            )
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,raw_status,clean_stage,updated_at,source_candidate_id)"
                " VALUES (571,154,565,'search_shortlisted','S1 新增寻访/待复核','2026-07-10 09:00:00','1189')"
            )
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,raw_status,clean_stage,updated_at,source_candidate_id)"
                " VALUES (572,154,566,'search_shortlisted','S2 已接触','2026-07-11 09:00:00','1182')"
            )
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,raw_status,clean_stage,updated_at,source_candidate_id)"
                " VALUES (573,154,567,'search_shortlisted','S1 新增寻访/待复核','2026-07-12 09:00:00','2001')"
            )
            conn.execute(
                "INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary)"
                " VALUES (571,565,154,'note_added','completed','2026-07-20 15:00:00','顾问备注')"
            )
            # 曾任职：王某某简历文本里出现过 MPS（现职不是 MPS）
            conn.execute(
                "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,source_date,raw_json)"
                " VALUES (567,'liepin','res_567','2026-07-01',?)",
                (json.dumps({"full_text": "2013-2018 MPS FAE工程师；2018-至今 杰华特"}, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()

    def test_activate_list_fields_and_alias(self) -> None:
        self.seed_radar()
        self._seed_talent()
        app = create_app(db_path=self.db_path, start_legacy=False)
        with TestClient(app) as client:
            response = client.get("/api/v1/radar/scans/latest/actions/activate", params={"company": "MPS"})
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is True and payload["total"] >= 1
            items = payload["candidates"]
            current = [item for item in items if item["tenure"] == "现职"]
            assert current, "别名命中：MPS 应命中『美国芯源系统有限公司 (MPS)』现职人选"
            first = current[0]
            assert first["id"] == 1189
            assert first["name_masked"] and "刘一楠" not in first["name_masked"], "姓名必须遮罩"
            assert first["current_title"] == "资深技术销售工程师"
            assert first["stage"] == "S1 新增寻访/待复核"
            assert first["last_action_at"].startswith("2026-07-20")
            # 曾任职文本命中（王某某现职杰华特，简历里有 MPS）
            history = [item for item in items if item["tenure"] == "曾任职"]
            assert any(item["id"] == 2001 for item in history), f"曾任职命中缺失：{items}"
            # 现职排在曾任职前面
            assert items[0]["tenure"] == "现职"
            # 只读：candidates 行数不变
            conn = sqlite3.connect(self.db_path)
            try:
                assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
            finally:
                conn.close()

    def test_activate_empty_and_requires_scan(self) -> None:
        self.seed_radar()
        app = create_app(db_path=self.db_path, start_legacy=False)
        with TestClient(app) as client:
            empty = client.get("/api/v1/radar/scans/latest/actions/activate", params={"company": "不存在半导体"})
            assert empty.status_code == 200
            assert empty.json()["total"] == 0
        # 无榜单 → 404
        bare_temp = tempfile.TemporaryDirectory()
        self.addCleanup(bare_temp.cleanup)
        bare_db = Path(bare_temp.name) / "bare.db"
        conn = sqlite3.connect(bare_db)
        try:
            conn.executescript(API_SCHEMA)
            ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
        bare_app = create_app(db_path=bare_db, start_legacy=False)
        with TestClient(bare_app) as client:
            response = client.get("/api/v1/radar/scans/latest/actions/activate", params={"company": "MPS"})
            assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5. 动机维度注入（有信号注入 + 无信号/过期回归）
# ---------------------------------------------------------------------------

class MotivationRadarInjectionTest(DbCase):
    def _seed_person(self, company: str = "美国芯源系统有限公司 (MPS)") -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO people(id,display_name,current_company,current_title,city,education,experience)"
                " VALUES (1,'刘一楠',?,'资深技术销售工程师','上海','本科','10年')",
                (company,),
            )
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,raw_status,updated_at)"
                " VALUES (1,154,1,'search_shortlisted',datetime('now','localtime'))"
            )
            conn.execute(
                "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,source_date,raw_json)"
                " VALUES (1,'liepin','res_1','2026-07-20',?)",
                (json.dumps({"full_text": RESUME_TEXT, "work_text": RESUME_TEXT}, ensure_ascii=False),),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _stub_fetcher(url: str, timeout: float):
        return (0, "", "network_error")

    def _fake_llm(self) -> FakeLLM:
        return FakeLLM({}, trajectory=GOOD_LLM, percentile_motivation=GOOD_PM)

    def _run(self) -> dict:
        conn = self.connect()
        try:
            return candidate_assessment.run_assessment(
                conn,
                candidate_id=1,
                job_id=154,
                llm=self._fake_llm(),
                kb_dir=str(self.kb_dir),
                signal_fetcher=self._stub_fetcher,
                today=TODAY,
            )
        finally:
            conn.close()

    def test_radar_signal_injected_into_motivation(self) -> None:
        self._seed_person()
        self.seed_radar()
        doc = self._run()
        motivation = doc["dimensions"]["motivation"]
        radar_entries = [item for item in motivation["evidence"] if item["type"] == "雷达信号"]
        assert radar_entries, f"雷达信号必须进 motivation.evidence：{motivation['evidence']}"
        entry = radar_entries[0]["ref"]
        assert "公开报道显示MPS高管变动" in entry
        assert "2026-07-20" in entry, "证据必须带 as_of"
        assert "https://example.com/news/mps-1" in entry, "证据必须带来源链接"
        assert "推测" in entry, "文案必须标注推测"
        # LLM 给了 certain，注入雷达信号（推测口径）后必须封顶 inferred
        assert motivation["confidence"] == "inferred"
        assert doc["signal_stats"]["radar_signals"] == {"matched": 1, "injected": 1}
        assert candidate_assessment.validate_assessment(doc) == []

    def test_no_signal_no_injection_regression(self) -> None:
        self._seed_person()
        # 榜单里只有别家公司的信号 → 不注入，行为与之前一致
        self.seed_radar(signals=[_valid_signal(company="芯源微")])
        doc = self._run()
        motivation = doc["dimensions"]["motivation"]
        assert not [item for item in motivation["evidence"] if item["type"] == "雷达信号"]
        assert doc["signal_stats"]["radar_signals"]["injected"] == 0

    def test_expired_signal_not_injected(self) -> None:
        self._seed_person()
        self.seed_radar(signals=[_valid_signal(as_of="2026-05-01")])  # 距 today 84 天 > 60 天有效期
        doc = self._run()
        motivation = doc["dimensions"]["motivation"]
        assert not [item for item in motivation["evidence"] if item["type"] == "雷达信号"], "过期信号不得注入"
        assert doc["signal_stats"]["radar_signals"] == {"matched": 0, "injected": 0}

    def test_no_radar_scan_at_all_regression(self) -> None:
        self._seed_person()
        doc = self._run()  # 库里完全没有 radar_scan artifact
        motivation = doc["dimensions"]["motivation"]
        assert not [item for item in motivation["evidence"] if item["type"] == "雷达信号"]
        assert doc["signal_stats"]["radar_signals"] == {"matched": 0, "injected": 0}
        assert candidate_assessment.validate_assessment(doc) == []


if __name__ == "__main__":
    unittest.main()
