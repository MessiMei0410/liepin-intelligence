"""S6-1：判人评估器 —— candidate_assessment 模型 / 证据校验层 / 敏感扫描 / 路由 / 回放测试。

口径：docs/TASKCARD_S6-1_判人评估器_轨迹与跳槽史_20260724.md（验收标准 + 红线）。
全部使用临时库 + 临时 KB fixture + FakeLLM，绝不触碰生产 DB、真实知识库与外网 LLM。

覆盖：
1. 模型：schema 校验（必备键/维度结构/枚举，含 S6-3 risks 结构校验）、幂等 upsert（同人同岗一行、as_of 刷新、version 自增）；
2. 证据契约：20 条逐字引用 100% 通过；编造简历引用/编造图谱引用必被拦；证据归零强制 inferred；
   图谱未命中的 tier_source 强制 inferred；
3. 敏感属性负向扫描：诱导输入（"已婚已育稳定"类）不得成为因子——拒写 + 扫描日志；
   决策禁语（"建议淘汰"类）拒写；简历逐字引用含敏感词只剥离不拒写；
4. 路由：POST/GET 200、404（人选不存在/不属于该岗位/尚无评估）、409（模型不可用）、幂等；
5. 回放：FakeLLM 下 markdown 结构完整，每段四要素（职业轨迹/跳槽质量史/证据/置信度）齐全。
"""

from __future__ import annotations

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

from a_system_agent import candidate_assessment  # noqa: E402
from a_system_agent.llm import FakeLLM, UnavailableLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.app import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

RESUME_TEXT = (
    "张** 求职期望：杭州 技术市场经理\n"
    "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源多相控制器产品线市场推广\n"
    "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持\n"
    "2013.07-2017.05 立讯精密 · 硬件工程师 负责消费电子硬件电路设计\n"
    "浙江大学 · 电子科学与技术 · 本科 2009.09-2013.06"
)

GRAPH_FIXTURE = {
    "companies": {
        "杰华特微电子股份有限公司": {"track": "模拟芯片", "business": "电源管理芯片 多相控制器", "categories": ["设计公司"]},
        "晶丰明源半导体（上海）股份有限公司": {"track": "模拟芯片", "business": "电源管理芯片", "categories": ["设计公司"]},
    }
}

GOOD_LLM = {
    "trajectory": {
        "verdict": "从消费电子硬件转到模拟芯片原厂，技术市场线一路上行",
        "segments": [
            {"company": "杰华特微电子股份有限公司", "title": "技术市场经理", "period": "2021.03-至今",
             "tier": "T1", "tier_source": "graph", "team": "", "report_line": "", "note": "PC电源产品线"},
            {"company": "晶丰明源半导体（上海）股份有限公司", "title": "FAE工程师", "period": "2017.06-2021.02",
             "tier": "T1", "tier_source": "graph", "team": "", "report_line": "", "note": "AC-DC 客户支持"},
            {"company": "立讯精密", "title": "硬件工程师", "period": "2013.07-2017.05",
             "tier": "T2", "tier_source": "graph", "team": "", "report_line": "", "note": "消费电子"},
        ],
        "promotion_pace": "fast",
        "tech_evolution": "rising",
        "evidence": [
            {"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"},
            {"type": "图谱", "ref": "杰华特微电子股份有限公司"},
        ],
        "confidence": "certain",
    },
    "move_history": {
        "verdict": "两次跳槽均为上升，平台与职责同步抬升",
        "moves": [
            {"from": "立讯精密", "to": "晶丰明源", "direction": "up", "platform": "up",
             "title_direction": "up", "responsibility_direction": "up", "reason": "消费电子整机转芯片原厂"},
            {"from": "晶丰明源", "to": "杰华特", "direction": "up", "platform": "lateral",
             "title_direction": "up", "responsibility_direction": "up", "reason": "FAE 转技术市场管理"},
        ],
        "current_move": "lateral",
        "evidence": [
            {"type": "简历", "ref": "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师"},
        ],
        "confidence": "certain",
    },
    "consultant_summary": "技术市场线轨迹清晰，从整机硬件切到原厂后两次跳槽均上行，当前这单对他偏平移。",
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

STRATEGY_V2 = {
    "schema_version": "strategy_v2",
    "step1_job_essence": {"statement": "计算电源方向技术市场岗", "confirmed_by": "consultant"},
    "step2_target_pool": [
        {"path": "same_layer", "tier": "T1",
         "companies": [{"name": "杰华特微电子股份有限公司", "source": "client_doc", "confidence": "high"}],
         "rationale": "同层友商"}
    ],
    "step3_level_mapping": {"accepted_levels": ["经理", "总监"], "calibration_rule": "按职责定档"},
    "step4_keyword_groups": [],
    "step5_expectation": {},
    "negative_rules": [],
}


def _create_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(API_SCHEMA)
        ensure_schema(conn)
        conn.execute("INSERT INTO clients VALUES (1,'士兰微'),(2,'长越科技')")
        conn.execute("INSERT INTO jobs VALUES (154,1,'技术市场经理/总监（PC电源）','PC电源技术市场','5年以上电源芯片经验')")
        conn.execute("INSERT INTO jobs VALUES (137,2,'机械高级工程师','精密机械设计','8年以上精密设备经验')")
        # 岗位 154 的 strategy_v2 artifact
        conn.execute(
            "INSERT INTO agent_goals (goal_id,objective,title,context_type,context_id,context_json,status)"
            " VALUES ('goal_154','寻访','寻访','job',154,'{}','blocked')"
        )
        conn.execute("INSERT INTO agent_workflows (workflow_id,goal_id,status) VALUES ('wf-154','goal_154','blocked')")
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,content,metadata_json,validation_status)
            VALUES ('artifact_strategy_154','goal_154','wf-154',NULL,'search_strategy','策略','text/markdown','# 策略',?,'passed')
            """,
            (json.dumps({"strategy_v2": STRATEGY_V2}, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_person(
    db_path: Path,
    *,
    candidate_id: int,
    job_id: int,
    person_id: int,
    name: str = "张三",
    stage: str = "S1 新增寻访/待复核",
    status: str = "search_shortlisted",
    resume: str = RESUME_TEXT,
    company: str = "杰华特微电子股份有限公司",
    title: str = "技术市场经理",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO people(id,display_name,current_company,current_title,city,education,experience)"
            " VALUES (?,?,?,?,'杭州','本科','10年')",
            (person_id, name, company, title),
        )
        conn.execute(
            "INSERT INTO job_candidates(id,job_id,person_id,clean_stage,raw_status,updated_at)"
            " VALUES (?,?,?,?,?,datetime('now','localtime'))",
            (candidate_id, job_id, person_id, stage, status),
        )
        conn.execute(
            "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,source_date,raw_json)"
            " VALUES (?,?,?,'2026-07-20',?)",
            (
                person_id,
                "liepin",
                f"res_{person_id}",
                json.dumps(
                    {"full_text": resume, "work_text": resume, "project_text": "", "education_text": ""},
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()


class KbCase(unittest.TestCase):
    """临时 KB 目录（图谱 fixture）+ ASA_KNOWLEDGE_BASE_DIR 环境变量隔离。"""

    def setUp(self) -> None:
        self.kb_temp = tempfile.TemporaryDirectory()
        kb_dir = Path(self.kb_temp.name) / "kb"
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "kb_company_graph_jsj_v1.json").write_text(
            json.dumps(GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        self.kb_dir = kb_dir
        self._old_kb_env = os.environ.get("ASA_KNOWLEDGE_BASE_DIR")
        os.environ["ASA_KNOWLEDGE_BASE_DIR"] = str(kb_dir)

    def tearDown(self) -> None:
        if self._old_kb_env is None:
            os.environ.pop("ASA_KNOWLEDGE_BASE_DIR", None)
        else:
            os.environ["ASA_KNOWLEDGE_BASE_DIR"] = self._old_kb_env
        self.kb_temp.cleanup()


class DbCase(KbCase):
    def setUp(self) -> None:
        super().setUp()
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        _create_db(self.db_path)
        # company_kb 默认直连生产库（A_SYSTEM_DB 优先），测试必须隔离：
        # 指向不存在的路径 → company_kb.get_profile 静默跳过，不读生产画像。
        self._old_asm_env = os.environ.get("A_SYSTEM_DB")
        os.environ["A_SYSTEM_DB"] = str(Path(self.db_temp.name) / "nonexistent.db")

    def tearDown(self) -> None:
        if self._old_asm_env is None:
            os.environ.pop("A_SYSTEM_DB", None)
        else:
            os.environ["A_SYSTEM_DB"] = self._old_asm_env
        self.db_temp.cleanup()
        super().tearDown()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _fake_llm(result: dict | None = None) -> FakeLLM:
    return FakeLLM({}, trajectory=result if result is not None else GOOD_LLM)


def _stub_fetcher(url: str, timeout: float) -> tuple[int, str, str]:
    """S6-2 公司近况采集 stub：测试绝不真实外呼网络。"""
    return (0, "", "network_error")


# ---------------------------------------------------------------------------
# 1. schema 校验 + 幂等 upsert
# ---------------------------------------------------------------------------

def _valid_doc(**overrides) -> dict:
    doc = {
        "schema_version": "assessment_v1",
        "candidate_id": 1,
        "job_id": 154,
        "as_of": "2026-07-24 10:00:00",
        "assessor_version": "s6-3-v1",
        "model": "fake-agent-v1",
        "strategy_ref": "artifact_strategy_154",
        "dimensions": {
            "trajectory": {
                "verdict": "一路上行",
                "evidence": [{"type": "简历", "ref": "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理"}],
                "confidence": "certain",
                "segments": [
                    {"company": "杰华特微电子股份有限公司", "title": "技术市场经理", "period": "2021.03-至今",
                     "tier": "T1", "tier_source": "graph", "team": "", "report_line": "", "note": ""}
                ],
                "promotion_pace": "fast",
                "tech_evolution": "rising",
            },
            "move_history": {
                "verdict": "两次均上升",
                "evidence": [{"type": "图谱", "ref": "杰华特微电子股份有限公司"}],
                "confidence": "certain",
                "moves": [
                    {"from": "立讯精密", "to": "晶丰明源", "direction": "up", "platform": "up",
                     "title_direction": "up", "responsibility_direction": "up", "reason": "整机转原厂"}
                ],
                "current_move": "lateral",
            },
            "percentile": {
                "verdict": "同方向参照人群 N=10，该人选落位前 25%",
                "band": "top25",
                "basis": "fit_score",
                "score": 88,
                "percentile_rank": 0.8,
                "reference": {"n": 10, "direction": "技术市场", "years_window": 3, "median": 67.5,
                              "q25": 56.2, "q75": 78.8, "min": 45, "max": 95,
                              "sample_sufficient": True, "min_n": 8, "note": ""},
                "evidence": [{"type": "知识库", "ref": "历史人选库参照系：同方向（技术市场）±3年 样本N=10，既有评估中位分67.5（P25=56.2，P75=78.8）"}],
                "confidence": "certain",
            },
            "motivation": {
                "verdict": "在职时长已超其历史平均任期，存在变动的可能",
                "signals": [
                    {"kind": "tenure_over_avg", "source": "简历工况",
                     "summary": "当前任职已 65 个月，明显超过其历史平均任期 46.0 个月", "as_of": "2026-07-24"},
                    {"kind": "funding", "source": "公开信息",
                     "summary": "杰华特微电子股份有限公司：公司完成新一轮融资", "url": "https://www.joulwatt.com/news", "as_of": "2026-07-24"},
                ],
                "evidence": [{"type": "公开信息", "ref": "https://www.joulwatt.com/news"}],
                "confidence": "certain",
            },
            "risks": {
                "verdict": "未见需核实的问题（已按简历时间线、任期节奏与岗位硬条件逐项核对）。",
                "items": [],
                "evidence": [],
                "confidence": "certain",
                "stats": {"deterministic_items": 0, "llm_items_kept": 0, "llm_items_dropped": 0,
                          "checks_run": ["gap", "frequent_hop", "over_packaging", "hard_requirement"]},
            },
        },
        "consultant_summary": "轨迹清晰，跳槽质量高。",
        "advisor_action": "pending",
        "advisor_note": "",
        "evidence_stats": {"kept": 2, "stripped": 0, "stripped_detail": []},
    }
    doc.update(overrides)
    return doc


class SchemaValidationTest(unittest.TestCase):
    def test_valid_doc_passes(self) -> None:
        assert candidate_assessment.validate_assessment(_valid_doc()) == []

    def test_required_keys_and_version(self) -> None:
        doc = _valid_doc(schema_version="assessment_v0")
        assert any("schema_version" in error for error in candidate_assessment.validate_assessment(doc))
        doc = _valid_doc()
        del doc["as_of"]
        assert any("as_of" in error for error in candidate_assessment.validate_assessment(doc))

    def test_risks_dimension_structure_validated(self) -> None:
        """S6-3 起 risks 必须填充且结构合法：severity 枚举 / items 结构 / 空态置信度规则。"""
        doc = _valid_doc()
        doc["dimensions"]["risks"] = {"verdict": "有风险"}
        errors = candidate_assessment.validate_assessment(doc)
        assert any("risks" in error for error in errors), "risks 缺 items/confidence 必须被拦"
        doc = _valid_doc()
        doc["dimensions"]["risks"]["items"] = [
            {"kind": "gap", "risk": "有空窗需核实", "severity": "致命", "evidence": []}
        ]
        errors = candidate_assessment.validate_assessment(doc)
        assert any("severity" in error for error in errors), "severity 枚举非法必须被拦"
        # 空态且未执行核对 → certain 非法
        doc = _valid_doc()
        doc["dimensions"]["risks"]["stats"]["checks_run"] = []
        doc["dimensions"]["risks"]["confidence"] = "certain"
        errors = candidate_assessment.validate_assessment(doc)
        assert any("checks_run" in error for error in errors)

    def test_two_dimensions_structure(self) -> None:
        doc = _valid_doc()
        doc["dimensions"]["trajectory"]["verdict"] = ""
        assert any("verdict" in error for error in candidate_assessment.validate_assessment(doc))
        doc = _valid_doc()
        doc["dimensions"]["move_history"]["moves"][0]["direction"] = "upup"
        assert any("direction" in error for error in candidate_assessment.validate_assessment(doc))
        doc = _valid_doc()
        doc["dimensions"]["trajectory"]["promotion_pace"] = "超音速"
        assert any("promotion_pace" in error for error in candidate_assessment.validate_assessment(doc))

    def test_certain_requires_evidence(self) -> None:
        doc = _valid_doc()
        doc["dimensions"]["trajectory"]["evidence"] = []
        doc["dimensions"]["trajectory"]["confidence"] = "certain"
        errors = candidate_assessment.validate_assessment(doc)
        assert any("无证据" in error for error in errors)
        # 无证据但标 inferred → 合法
        doc["dimensions"]["trajectory"]["confidence"] = "inferred"
        assert candidate_assessment.validate_assessment(doc) == []


class UpsertIdempotencyTest(DbCase):
    def test_upsert_same_pair_updates_in_place(self) -> None:
        conn = self.connect()
        try:
            first = _valid_doc(as_of="2026-07-24 10:00:00")
            artifact_id = candidate_assessment.upsert_assessment(conn, first)
            conn.commit()
            second = _valid_doc(as_of="2026-07-24 11:30:00")
            again = candidate_assessment.upsert_assessment(conn, second)
            conn.commit()
            assert again == artifact_id
            rows = conn.execute(
                "SELECT metadata_json FROM agent_artifacts WHERE artifact_type=?",
                (candidate_assessment.ARTIFACT_TYPE,),
            ).fetchall()
            assert len(rows) == 1, "同人同岗重复写入不得新增行"
            stored = json.loads(rows[0]["metadata_json"])
            assert stored["version"] == 2
            assert stored["as_of"] == "2026-07-24 11:30:00", "重复生成必须刷新 as_of"
            assert stored["history"] and stored["history"][0]["as_of"] == "2026-07-24 10:00:00"
        finally:
            conn.close()

    def test_upsert_refuses_invalid_doc(self) -> None:
        conn = self.connect()
        try:
            doc = _valid_doc()
            doc["dimensions"]["percentile"] = {"verdict": "x"}
            with self.assertRaises(ValueError):
                candidate_assessment.upsert_assessment(conn, doc)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. 证据校验层（契约：逐字通过 / 编造被拦 / 降级 inferred / tier_source 闸）
# ---------------------------------------------------------------------------

class EvidenceGateTest(unittest.TestCase):
    def test_verbatim_evidence_passes_20_samples(self) -> None:
        corpus = RESUME_TEXT
        fragments = [
            "张** 求职期望：杭州 技术市场经理",
            "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理",
            "杰华特微电子股份有限公司 · 技术市场经理 负责PC电源多相控制器产品线市场推广",
            "负责PC电源多相控制器产品线市场推广",
            "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师",
            "晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持",
            "负责AC-DC电源芯片客户支持",
            "2013.07-2017.05 立讯精密 · 硬件工程师",
            "立讯精密 · 硬件工程师 负责消费电子硬件电路设计",
            "负责消费电子硬件电路设计",
            "浙江大学 · 电子科学与技术 · 本科 2009.09-2013.06",
            "浙江大学 · 电子科学与技术 · 本科",
            "2021.03-至今",
            "2017.06-2021.02",
            "2013.07-2017.05",
            "技术市场经理 负责PC电源多相控制器产品线市场推广",
            "FAE工程师 负责AC-DC电源芯片客户支持",
            "硬件工程师 负责消费电子硬件电路设计",
            "电子科学与技术 · 本科 2009.09-2013.06",
            "求职期望：杭州 技术市场经理",
        ]
        assert len(fragments) == 20
        evidence = [{"type": "简历", "ref": fragment} for fragment in fragments]
        kept, stripped = candidate_assessment.verify_evidence(evidence, corpus=corpus, graph_names=[])
        assert len(kept) == 20, f"逐字引用必须 100% 通过，被剥离：{stripped}"
        assert stripped == []

    def test_fabricated_resume_ref_stripped(self) -> None:
        evidence = [{"type": "简历", "ref": "2020.01-至今 中芯国际 · 技术市场总监"}]
        kept, stripped = candidate_assessment.verify_evidence(
            evidence, corpus=RESUME_TEXT, graph_names=["杰华特微电子股份有限公司"]
        )
        assert kept == [] and len(stripped) == 1
        assert "逐字" in stripped[0]["reason"]

    def test_short_ref_stripped(self) -> None:
        kept, stripped = candidate_assessment.verify_evidence(
            [{"type": "简历", "ref": "本科"}], corpus=RESUME_TEXT, graph_names=[]
        )
        assert kept == [] and stripped, "过短引用不可核验，必须剥离"

    def test_graph_ref_must_resolve_real_entry(self) -> None:
        graph_names = ["杰华特微电子股份有限公司", "晶丰明源半导体（上海）股份有限公司"]
        kept, stripped = candidate_assessment.verify_evidence(
            [{"type": "图谱", "ref": "杰华特"}], corpus=RESUME_TEXT, graph_names=graph_names
        )
        assert len(kept) == 1 and kept[0]["ref"] == "杰华特微电子股份有限公司"
        kept, stripped = candidate_assessment.verify_evidence(
            [{"type": "图谱", "ref": "华虹半导体有限公司"}], corpus=RESUME_TEXT, graph_names=graph_names
        )
        assert kept == [] and len(stripped) == 1, "编造图谱引用必须被拦"

    def test_zero_evidence_downgrades_to_inferred(self) -> None:
        raw = json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))
        raw["trajectory"]["evidence"] = [{"type": "简历", "ref": "编造的简历片段根本不存在"}]
        raw["trajectory"]["confidence"] = "certain"
        raw["move_history"]["evidence"] = [{"type": "图谱", "ref": "编造的图谱公司"}]
        raw["move_history"]["confidence"] = "certain"
        graph_hits = [{"company": "杰华特微电子股份有限公司", "graph_name": "杰华特微电子股份有限公司",
                       "track": "模拟芯片", "business": "电源", "categories": []}]
        dimensions, _summary, stats = candidate_assessment.normalize_llm_result(
            raw, corpus=RESUME_TEXT, graph_hits=graph_hits
        )
        assert dimensions["trajectory"]["evidence"] == []
        assert dimensions["trajectory"]["confidence"] == "inferred", "证据归零必须降级 inferred"
        assert dimensions["move_history"]["confidence"] == "inferred"
        assert stats["stripped"] == 2 and len(stats["stripped_detail"]) == 2

    def test_tier_source_downgrade_without_graph_hit(self) -> None:
        graph_hits = [{"company": "杰华特微电子股份有限公司", "graph_name": "杰华特微电子股份有限公司",
                       "track": "模拟芯片", "business": "电源", "categories": []}]
        dimensions, _summary, _stats = candidate_assessment.normalize_llm_result(
            json.loads(json.dumps(GOOD_LLM, ensure_ascii=False)),
            corpus=RESUME_TEXT,
            graph_hits=graph_hits,
        )
        segments = {segment["company"]: segment for segment in dimensions["trajectory"]["segments"]}
        assert segments["杰华特微电子股份有限公司"]["tier_source"] == "graph", "图谱命中保留 graph"
        assert segments["立讯精密"]["tier_source"] == "inferred", "图谱未命中强制 inferred，不瞎编"


# ---------------------------------------------------------------------------
# 3. 敏感属性负向扫描 + 决策禁语（红线）
# ---------------------------------------------------------------------------

class SensitiveScanTest(DbCase):
    def test_scan_wordlist(self) -> None:
        hits = candidate_assessment.scan_sensitive(["已婚已育，家庭稳定，可全身心投入"])
        assert any(hit["category"] == "婚育" for hit in hits)
        assert any(hit["category"] == "年龄" for hit in candidate_assessment.scan_sensitive(["35岁正是当打之年"]))
        assert any(hit["category"] == "性别" for hit in candidate_assessment.scan_sensitive(["女性候选人沟通亲和"]))
        assert any(hit["category"] == "户籍" for hit in candidate_assessment.scan_sensitive(["本地户口稳定性好"]))
        assert candidate_assessment.scan_sensitive(["两次跳槽均为上升"]) == []

    def test_inducing_verdict_rejected_and_logged(self) -> None:
        """契约：构造"已婚已育稳定"类诱导 LLM 输出 → 不得成为因子（拒写 + 扫描日志）。"""
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        induced = json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))
        induced["trajectory"]["verdict"] = "已婚已育，家庭稳定，跳槽动力一般"
        conn = self.connect()
        try:
            with self.assertRaises(ValueError) as ctx:
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=_fake_llm(induced), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
                )
            assert "敏感" in str(ctx.exception)
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "命中敏感因子必须拒写，artifact 不得落库"
            log = conn.execute(
                "SELECT event_status,raw_json FROM candidate_events WHERE event_type='assessment_sensitive_scan_blocked'"
            ).fetchone()
            assert log is not None, "拒写必须记扫描日志"
            assert log["event_status"] == "blocked"
            assert "婚育" in str(log["raw_json"])
        finally:
            conn.close()

    def test_banned_decision_words_rejected(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        banned = json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))
        banned["consultant_summary"] = "综合判断建议淘汰，不再推进。"
        conn = self.connect()
        try:
            with self.assertRaises(ValueError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=154, llm=_fake_llm(banned), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
                )
            row = conn.execute(
                "SELECT 1 FROM agent_artifacts WHERE artifact_type=?", (candidate_assessment.ARTIFACT_TYPE,)
            ).fetchone()
            assert row is None, "决策禁语必须拒写"
        finally:
            conn.close()

    def test_sensitive_resume_evidence_stripped_not_rejected(self) -> None:
        """简历逐字引用里带敏感词（原文事实）→ 只剥离该条证据，不拒写整份评估。"""
        resume = RESUME_TEXT + "\n个人情况：41岁 已婚已育 家庭稳定"
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1, resume=resume)
        raw = json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))
        raw["trajectory"]["evidence"].append({"type": "简历", "ref": "个人情况：41岁 已婚已育 家庭稳定"})
        conn = self.connect()
        try:
            doc = candidate_assessment.run_assessment(
                conn, candidate_id=1, job_id=154, llm=_fake_llm(raw), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
            )
            refs = [item["ref"] for item in doc["dimensions"]["trajectory"]["evidence"]]
            assert "个人情况：41岁 已婚已育 家庭稳定" not in refs, "含敏感词的简历引用必须剥离"
            assert any("敏感" in item["reason"] for item in doc["evidence_stats"]["stripped_detail"])
            assert doc["dimensions"]["trajectory"]["confidence"] == "certain", "干净证据仍在，不降级"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. 评估主流程：两维字段完整 + 占位 null + 幂等（service 层）
# ---------------------------------------------------------------------------

class RunAssessmentTest(DbCase):
    def test_run_assessment_full_shape(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        conn = self.connect()
        try:
            doc = candidate_assessment.run_assessment(
                conn,
                candidate_id=1,
                job_id=154,
                llm=_fake_llm(),
                kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher,
                mask_name=lambda value: str(value or "")[:1] + "**",
            )
        finally:
            conn.close()
        assert doc["schema_version"] == "assessment_v1"
        assert doc["candidate_id"] == 1 and doc["job_id"] == 154
        assert doc["strategy_ref"] == "artifact_strategy_154"
        assert doc["candidate_name_masked"] == "张**"
        assert doc["advisor_action"] == "pending"
        trajectory = doc["dimensions"]["trajectory"]
        assert trajectory["verdict"] and trajectory["confidence"] == "certain"
        assert trajectory["promotion_pace"] == "fast" and trajectory["tech_evolution"] == "rising"
        assert len(trajectory["segments"]) == 3
        assert len(trajectory["evidence"]) == 2
        move_history = doc["dimensions"]["move_history"]
        assert len(move_history["moves"]) == 2
        move = move_history["moves"][0]
        for key in ("direction", "platform", "title_direction", "responsibility_direction"):
            assert move[key] in {"up", "lateral", "down"}
        assert move_history["current_move"] in {"up", "lateral", "down", "unknown"}
        for name in ("percentile", "motivation"):
            dim = doc["dimensions"][name]
            assert isinstance(dim, dict) and dim["verdict"], f"{name} 本期必须填充（S6-2）"
            assert dim["confidence"] in {"certain", "inferred"}
        assert doc["dimensions"]["percentile"]["band"] in {None, "top10", "top25", "median", "below"}
        assert isinstance(doc["dimensions"]["motivation"]["signals"], list)
        risks = doc["dimensions"]["risks"]
        assert isinstance(risks, dict) and isinstance(risks["items"], list), "S6-3 起 risks 必须填充"
        assert risks["confidence"] in {"certain", "inferred"}
        for item in risks["items"]:
            assert item["severity"] in {"high", "medium", "low"}
        assert candidate_assessment.validate_assessment(doc) == []

    def test_run_assessment_mismatch_and_no_resume(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        conn = self.connect()
        try:
            with self.assertRaises(LookupError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=1, job_id=137, llm=_fake_llm(), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
                )
            with self.assertRaises(LookupError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=999, job_id=154, llm=_fake_llm(), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
                )
        finally:
            conn.close()
        # 无简历语料 → ValueError（409 语义）
        conn = self.connect()
        try:
            conn.execute("INSERT INTO people(id,display_name) VALUES (9,'王五')")
            conn.execute("INSERT INTO job_candidates(id,job_id,person_id) VALUES (9,154,9)")
            conn.commit()
            with self.assertRaises(ValueError):
                candidate_assessment.run_assessment(
                    conn, candidate_id=9, job_id=154, llm=_fake_llm(), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 5. 路由：POST/GET /api/v1/candidates/{id}/assessments
# ---------------------------------------------------------------------------

class AssessmentApiTest(DbCase):
    def test_post_get_404_409_idempotent(self) -> None:
        _seed_person(self.db_path, candidate_id=1, job_id=154, person_id=1)
        _seed_person(
            self.db_path, candidate_id=2, job_id=137, person_id=2,
            name="李四", resume="李** 求职期望：苏州 机械高级工程师\n2018.01-至今 迅芯微精密有限公司 · 机械工程师 负责精密结构设计",
        )
        app = create_app(db_path=self.db_path, start_legacy=False)
        app.state.core.agent_service.llm = _fake_llm()
        # S6-2 公司近况采集走网络：测试一律 stub，绝不真实外呼
        app.state.core.agent_service.assessment_signal_fetcher = lambda url, timeout: (0, "", "network_error")
        with TestClient(app) as client:
            # 404：人选不存在 / 人选不属于该岗位 / 尚无评估
            missing = client.post(
                "/api/v1/candidates/999/assessments?job_id=154",
                json={"request_id": "req-a0"}, headers={"Idempotency-Key": "k-a0"},
            )
            assert missing.status_code == 404, missing.text
            mismatch = client.post(
                "/api/v1/candidates/2/assessments?job_id=154",
                json={"request_id": "req-a1"}, headers={"Idempotency-Key": "k-a1"},
            )
            assert mismatch.status_code == 404, mismatch.text
            not_yet = client.get("/api/v1/candidates/1/assessments?job_id=154")
            assert not_yet.status_code == 404, not_yet.text

            # POST 200：生成
            first = client.post(
                "/api/v1/candidates/1/assessments?job_id=154",
                json={"request_id": "req-a2"}, headers={"Idempotency-Key": "k-a2"},
            )
            assert first.status_code == 200, first.text
            payload = first.json()
            assert payload["ok"] is True and payload["artifact_id"] == "candidate_assessment_1_154"
            assert payload["receipt"]["idempotent_replay"] is False
            doc = payload["assessment"]
            assert isinstance(doc["dimensions"]["percentile"], dict), "S6-2 起 percentile 必须填充"
            assert isinstance(doc["dimensions"]["motivation"], dict), "S6-2 起 motivation 必须填充"
            assert doc["dimensions"]["trajectory"]["confidence"] == "certain"
            assert "*" in doc["candidate_name_masked"], "评估输出姓名必须遮罩"

            # GET 200
            fetched = client.get("/api/v1/candidates/1/assessments?job_id=154")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["artifact_id"] == "candidate_assessment_1_154"

            # 同幂等键重放 → 首次响应，不重复生成
            replay = client.post(
                "/api/v1/candidates/1/assessments?job_id=154",
                json={"request_id": "req-a2"}, headers={"Idempotency-Key": "k-a2"},
            )
            assert replay.status_code == 200
            assert replay.json()["receipt"]["idempotent_replay"] is True

            # 显式 force 再 POST → 更新同一行，不重复建行，as_of 刷新
            again = client.post(
                "/api/v1/candidates/1/assessments?job_id=154&force=true",
                json={"request_id": "req-a3"}, headers={"Idempotency-Key": "k-a3"},
            )
            assert again.status_code == 200, again.text
            conn = self.connect()
            try:
                rows = conn.execute(
                    "SELECT metadata_json FROM agent_artifacts WHERE artifact_type=?",
                    (candidate_assessment.ARTIFACT_TYPE,),
                ).fetchall()
                assert len(rows) == 1, "同人同岗重复 POST 不得新增 artifact 行"
                stored = json.loads(rows[0]["metadata_json"])
                assert stored["version"] == 2
            finally:
                conn.close()

            # 409：模型不可用
            app.state.core.agent_service.llm = UnavailableLLM()
            unavailable = client.post(
                "/api/v1/candidates/2/assessments?job_id=137",
                json={"request_id": "req-a4"}, headers={"Idempotency-Key": "k-a4"},
            )
            assert unavailable.status_code == 409, unavailable.text

            # 409：敏感扫描命中拒写（诱导输入）
            induced = json.loads(json.dumps(GOOD_LLM, ensure_ascii=False))
            induced["trajectory"]["verdict"] = "年纪偏大，学习能力存疑"
            app.state.core.agent_service.llm = _fake_llm(induced)
            blocked = client.post(
                "/api/v1/candidates/2/assessments?job_id=137",
                json={"request_id": "req-a5"}, headers={"Idempotency-Key": "k-a5"},
            )
            assert blocked.status_code == 409, blocked.text
            conn = self.connect()
            try:
                row = conn.execute(
                    "SELECT 1 FROM agent_artifacts WHERE artifact_id='candidate_assessment_2_137'"
                ).fetchone()
                assert row is None, "敏感命中 409 后 artifact 不得落库"
                log = conn.execute(
                    "SELECT 1 FROM candidate_events WHERE event_type='assessment_sensitive_scan_blocked'"
                ).fetchone()
                assert log is not None, "敏感拒写必须留扫描日志"
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# 6. 回放：FakeLLM 下 markdown 结构完整 + 每段四要素齐
# ---------------------------------------------------------------------------

class ReplayTest(DbCase):
    def _seed_pool(self) -> None:
        resumes = [
            ("候选人A", "已触达", "job_chat_verified"),
            ("候选人B", "已触达", "contacted"),
            ("候选人C", "S7 已推荐客户/待反馈", "recommended"),
            ("候选人D", "H5 最近寻访/初筛不通过", "screen_rejected"),
            ("候选人E", "S1 新增寻访/待复核", "search_shortlisted"),
            ("候选人F", "H5 最近寻访/初筛不通过", "screen_rejected"),
        ]
        for index, (name, stage, status) in enumerate(resumes, 1):
            resume = (
                f"人** 求职期望：杭州 技术市场经理（人选{index}）\n"
                "2020.05-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源产品线\n"
                "2015.04-2020.04 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责客户支持\n"
                "2011.07-2015.03 立讯精密 · 硬件工程师 负责硬件设计\n"
                "杭州电子科技大学 · 电子信息工程 · 本科 2007.09-2011.06"
            )
            _seed_person(
                self.db_path, candidate_id=100 + index, job_id=154, person_id=100 + index,
                name=name, stage=stage, status=status, resume=resume,
            )

    def test_replay_markdown_structure(self) -> None:
        import assessment_replay

        self._seed_pool()
        out_dir = Path(self.db_temp.name) / "work" / "assessment_replay"
        summary = assessment_replay.run_replay(
            self.db_path, 154, limit=5, out_dir=out_dir, llm=_fake_llm(), kb_dir=str(self.kb_dir), signal_fetcher=_stub_fetcher
        )
        assert summary["attempted"] == 5 and summary["generated"] == 5, summary
        trace = summary["sample_trace"]
        assert trace["picked_advanced"] >= 2 and trace["picked_rest"] >= 2, "推进/未推进必须混合"
        markdown_path = Path(summary["markdown"])
        assert markdown_path.is_file()
        assert str(out_dir) in str(markdown_path), "回放导出只进 work/assessment_replay/"
        text = markdown_path.read_text(encoding="utf-8")
        for token in ("职业轨迹", "跳槽质量史", "证据", "置信度"):
            assert text.count(token) >= 5, f"每人一段必须含「{token}」（{text.count(token)} < 5）"
        assert "顾问口径摘要" in text
        assert "成单口径说明" in text, "成单/未成单口径必须在导出里注明"
        assert "推测" in text or "确定" in text, "置信度必须业务语言"
        assert "候选人" not in text.split("人选 1")[1][:40], "导出姓名必须遮罩"
        assert "**" in text, "导出姓名必须遮罩"
        # 回放生成走同一落库通道：artifact 幂等落库
        conn = self.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_artifacts WHERE artifact_type=?",
                (candidate_assessment.ARTIFACT_TYPE,),
            ).fetchone()[0]
            assert count == 5
        finally:
            conn.close()
        assert summary["evidence_avg"] > 0
        assert 0.0 <= summary["inferred_ratio"] <= 1.0


if __name__ == "__main__":
    unittest.main()
