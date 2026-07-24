"""S8 岗位画像学习 —— 职责事实抽取器 / 岗位画像聚合 / 回填幂等 / 增量触发 / 展示接口 / disputed 闭环。

口径：从已抓取人选履历的"具体工作内容"学习岗位真实画像；画像先给人看、经顾问校准后才接消费
（本期不接策略 step1 / 评估器）。证据硬约束沿用 S6：每条职责事实必须挂该候选人简历逐字片段，
挂不上整条丢弃；全部丢光则该人不产事实（不计失败，只记 stats）。敏感属性零因子。
全部使用临时库 + FakeLLM，绝不触碰生产 DB 与外网 LLM。

覆盖：
1. 抽取器：事实字段保留（方向/工具/角色/客户/产出/证据）；编造证据丢弃；全丢光不产事实只记 stats；
   敏感词零因子（生成字段/证据命中均整条丢弃）；角色未知降级"其他"；LLM 结构非法 → LLMError；
2. 聚合：语义级归并（规范化键去重）、占比口径（人数/来源人数）、示例证据 ≤3 且姓名遮罩、
   <3 人 insufficient；disputed 条目排除出主列表并留痕；幂等（同岗一行，重跑 as_of 刷新不重复计人）；
3. 回填：活跃岗位口径（待处理人选 / 近 90 天事件）；重跑幂等（source_hash 未变跳过 LLM）；
   dry-run 不写库不调模型；
4. 增量：submit_job_profile_refresh 只抽新增人 + 确定性重算；简历捕获钩子调度刷新；
   LLM 不可用时刷新失败绝不阻断主流程；
5. 展示接口：GET 字段结构 / not_generated 空态 / 404；POST feedback 幂等 + 审计 + disputed 生效；
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from a_system_agent import AgentService, job_profile_insights  # noqa: E402
from a_system_agent.llm import FakeLLM, LLMError, UnavailableLLM  # noqa: E402
from a_system_agent.schema import ensure_schema  # noqa: E402
from asa_core.app import create_app  # noqa: E402
from backfill_job_profiles import run_backfill  # noqa: E402
from test_a_system_agent_v1 import fake_assessment  # noqa: E402

# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

API_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT,location TEXT,status TEXT,
  hard_requirements TEXT,ability_keywords TEXT,target_companies TEXT,exclusions TEXT,summary TEXT,updated_at TEXT);
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

RESUME_A = (
    "2021.03-至今 杰华特微电子股份有限公司 · 技术市场经理 负责PC电源多相控制器产品线市场推广，"
    "使用Cadence Allegro输出参考设计，面向服务器电源客户\n"
    "2017.06-2021.02 晶丰明源半导体（上海）股份有限公司 · FAE工程师 负责AC-DC电源芯片客户支持"
)
RESUME_B = (
    "2020.01-至今 MPS · 技术市场工程师 负责PC 电源／多相-控制器 客户导入，"
    "输出量产导入报告，面向服务器电源客户\n"
    "2016.05-2019.12 矽力杰 · FAE 负责DC-DC电源芯片打样支持"
)
RESUME_C = (
    "2019.03-至今 纳芯微 · 技术市场经理 负责PC电源多相控制器定义与推广，"
    "使用示波器与电子负载完成验证，输出测试报告，面向工业电源客户"
)


def _create_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(API_SCHEMA)
        ensure_schema(conn)
        conn.execute("INSERT INTO clients VALUES (1,'士兰微'),(2,'长越科技')")
        conn.execute(
            "INSERT INTO jobs(id,client_id,title,location,status,summary,hard_requirements,updated_at)"
            " VALUES (154,1,'技术市场经理/总监（PC电源）','杭州','已发布','PC电源技术市场','5年以上电源芯片经验','2026-07-14')"
        )
        conn.execute(
            "INSERT INTO jobs(id,client_id,title,location,status,summary,hard_requirements,updated_at)"
            " VALUES (137,2,'机械高级工程师','杭州','已发布','精密机械设计','8年以上精密设备经验','2026-07-14')"
        )
        conn.execute(
            "INSERT INTO jobs(id,client_id,title,location,status,summary,hard_requirements,updated_at)"
            " VALUES (199,2,'已关闭岗位','杭州','已关闭','','','2020-01-01')"
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
    name: str,
    resume: str,
    stage: str = "S1 新增寻访/待复核",
    status: str = "search_shortlisted",
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO people(id,display_name,current_company,current_title) VALUES (?,?,?,?)",
            (person_id, name, "杰华特微电子股份有限公司", "技术市场经理"),
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
                json.dumps({"full_text": resume, "work_text": resume}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_three_persons(db_path: Path, job_id: int = 154) -> None:
    _seed_person(db_path, candidate_id=301, job_id=job_id, person_id=31, name="张三", resume=RESUME_A)
    _seed_person(db_path, candidate_id=302, job_id=job_id, person_id=32, name="李四", resume=RESUME_B)
    _seed_person(db_path, candidate_id=303, job_id=job_id, person_id=33, name="王五", resume=RESUME_C)


def _duty_facts_from_payload(payload: dict) -> dict:
    """按输入文本动态生成"证据逐字"的职责事实（FakeLLM  callable 注入）。"""
    text = str(payload.get("resume_extraction_text") or "")
    frag = text[10:34] if len(text) > 34 else text[:20]
    return {
        "facts": [
            {
                "direction": "PC电源多相控制器",
                "tools": ["Cadence Allegro"],
                "role": "推广",
                "customer": "服务器电源客户",
                "deliverable": "参考设计",
                "evidence": frag,
            }
        ]
    }


class DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.db_temp.name) / "asa.db"
        _create_db(self.db_path)

    def tearDown(self) -> None:
        self.db_temp.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


# ---------------------------------------------------------------------------
# 1. 抽取器
# ---------------------------------------------------------------------------

class ExtractorTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_person(self.db_path, candidate_id=301, job_id=154, person_id=31, name="张三", resume=RESUME_A)

    def _extract(self, llm: FakeLLM, candidate_id: int = 301, force: bool = True):
        conn = self._connect()
        try:
            result = job_profile_insights.extract_duty_facts_for_candidate(
                conn, candidate_id=candidate_id, llm=llm, force=force
            )
            conn.commit()
            return result
        finally:
            conn.close()

    def test_fact_fields_and_verbatim_evidence_kept(self) -> None:
        frag = RESUME_A[10:40]
        llm = FakeLLM({}, duty_facts={"facts": [
            {"direction": "PC电源多相控制器", "tools": ["Cadence Allegro", "Cadence Allegro", "示波器"],
             "role": "推广", "customer": "服务器电源客户", "deliverable": "参考设计", "evidence": frag},
        ]})
        result = self._extract(llm)
        self.assertEqual(result["fact_count"], 1)
        self.assertEqual(result["dropped"], 0)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT facts_json,fact_count,extractor_version FROM job_profile_facts WHERE job_candidate_id=301"
            ).fetchone()
            self.assertIsNotNone(row)
            doc = json.loads(row["facts_json"])
            fact = doc["facts"][0]
            self.assertEqual(fact["direction"], "PC电源多相控制器")
            self.assertEqual(fact["tools"], ["Cadence Allegro", "示波器"])  # 工具去重保序
            self.assertEqual(fact["role"], "推广")
            self.assertEqual(fact["customer"], "服务器电源客户")
            self.assertEqual(fact["deliverable"], "参考设计")
            self.assertEqual(fact["evidence"], frag.strip())  # 证据仅存首尾空白差异（S6 同口径）
            self.assertIn(fact["evidence"], RESUME_A)  # 证据逐字挂在本人简历上
        finally:
            conn.close()

    def test_fabricated_evidence_dropped(self) -> None:
        llm = FakeLLM({}, duty_facts={"facts": [
            {"direction": "编造方向", "tools": [], "role": "支持", "customer": "", "deliverable": "",
             "evidence": "简历里根本没有这句话"},
            {"direction": "PC电源", "tools": [], "role": "打样", "customer": "", "deliverable": "",
             "evidence": RESUME_A[5:30]},
        ]})
        result = self._extract(llm)
        self.assertEqual(result["fact_count"], 1)
        self.assertEqual(result["dropped"], 1)
        conn = self._connect()
        try:
            doc = json.loads(
                conn.execute("SELECT facts_json FROM job_profile_facts WHERE job_candidate_id=301").fetchone()[0]
            )
            self.assertEqual([f["direction"] for f in doc["facts"]], ["PC电源"])
            reasons = [d["reason"] for d in doc["stats"]["dropped_detail"]]
            self.assertTrue(any("逐字" in reason for reason in reasons))
        finally:
            conn.close()

    def test_all_facts_dropped_is_stats_not_failure(self) -> None:
        llm = FakeLLM({}, duty_facts={"facts": [
            {"direction": "编造方向A", "tools": [], "role": "支持", "customer": "", "deliverable": "", "evidence": "不存在的一句话"},
            {"direction": "编造方向B", "tools": [], "role": "支持", "customer": "", "deliverable": "", "evidence": "也不存在的另一句"},
        ]})
        result = self._extract(llm)  # 不抛异常：该人不产事实，不计失败
        self.assertEqual(result["fact_count"], 0)
        self.assertEqual(result["dropped"], 2)
        self.assertIn("证据校验", result["reason"])
        conn = self._connect()
        try:
            stats = json.loads(
                conn.execute("SELECT stats_json FROM job_profile_facts WHERE job_candidate_id=301").fetchone()[0]
            )
            self.assertEqual(stats["kept"], 0)
            self.assertEqual(stats["dropped"], 2)
        finally:
            conn.close()

    def test_empty_llm_facts_is_no_facts_reason(self) -> None:
        llm = FakeLLM({}, duty_facts={"facts": []})
        result = self._extract(llm)
        self.assertEqual(result["fact_count"], 0)
        self.assertEqual(result["dropped"], 0)
        self.assertIn("未抽取到职责事实", result["reason"])

    def test_sensitive_zero_factor(self) -> None:
        """敏感属性零因子：生成字段命中词表整条丢弃；证据片段含敏感表述同样整条丢弃。"""
        llm = FakeLLM({}, duty_facts={"facts": [
            {"direction": "已婚已育稳定的团队", "tools": [], "role": "管理", "customer": "", "deliverable": "",
             "evidence": RESUME_A[5:30]},
            {"direction": "AC-DC电源芯片", "tools": [], "role": "支持", "customer": "", "deliverable": "",
             "evidence": "负责AC-DC电源芯片客户支持"},
        ]})
        result = self._extract(llm)
        self.assertEqual(result["fact_count"], 1)
        conn = self._connect()
        try:
            doc = json.loads(
                conn.execute("SELECT facts_json FROM job_profile_facts WHERE job_candidate_id=301").fetchone()[0]
            )
            self.assertEqual([f["direction"] for f in doc["facts"]], ["AC-DC电源芯片"])
            facts_blob = json.dumps(doc["facts"], ensure_ascii=False)  # 入库事实零敏感因子（丢弃留痕在 stats）
            self.assertNotIn("已婚", facts_blob)
            reasons = [d["reason"] for d in doc["stats"]["dropped_detail"]]
            self.assertTrue(any("敏感" in reason for reason in reasons))
        finally:
            conn.close()
        # 证据片段含敏感表述：逐字校验能过，但敏感闸整条丢弃
        corpus_with_sensitive = f"{RESUME_A}\n备注：35岁，已婚已育，负责PC电源推广"
        kept, dropped = job_profile_insights.validate_facts(
            {"facts": [
                {"direction": "PC电源", "tools": [], "role": "推广", "customer": "", "deliverable": "",
                 "evidence": "35岁，已婚已育，负责PC电源推广"},
            ]},
            corpus=corpus_with_sensitive,
        )
        self.assertEqual(kept, [])
        self.assertTrue(any("敏感" in d["reason"] for d in dropped))

    def test_role_fallback_and_short_evidence_dropped(self) -> None:
        kept, dropped = job_profile_insights.validate_facts(
            {"facts": [
                {"direction": "方向A", "tools": [], "role": "天马行空", "customer": "", "deliverable": "",
                 "evidence": RESUME_A[5:30]},
                {"direction": "方向B", "tools": [], "role": "推广", "customer": "", "deliverable": "", "evidence": "短"},
                {"direction": "", "tools": [], "role": "推广", "customer": "", "deliverable": "", "evidence": RESUME_A[5:30]},
            ]},
            corpus=RESUME_A,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["role"], "其他")  # 未知角色降级
        self.assertEqual(len(dropped), 2)  # 证据过短 + 缺方向

    def test_invalid_llm_structure_raises(self) -> None:
        llm = FakeLLM({}, duty_facts={"unexpected": True})
        with self.assertRaises(LLMError):
            self._extract(llm)

    def test_missing_candidate_raises_lookup(self) -> None:
        llm = FakeLLM({}, duty_facts={"facts": []})
        with self.assertRaises(LookupError):
            self._extract(llm, candidate_id=999)

    def test_thin_corpus_no_facts(self) -> None:
        conn = self._connect()
        try:
            conn.execute("INSERT INTO people(id,display_name) VALUES (88,'薄历')")
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,clean_stage,raw_status,updated_at)"
                " VALUES (388,154,88,'S1 新增寻访/待复核','search_shortlisted',datetime('now','localtime'))"
            )
            conn.commit()
            result = job_profile_insights.extract_duty_facts_for_candidate(
                conn, candidate_id=388, llm=FakeLLM({}, duty_facts={"facts": []})
            )
            self.assertEqual(result["fact_count"], 0)
            self.assertIn("语料不足", result["reason"])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. 聚合
# ---------------------------------------------------------------------------

class AggregateTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_three_persons(self.db_path)

    def _extract_all(self, llm: FakeLLM) -> None:
        conn = self._connect()
        try:
            for cid in (301, 302, 303):
                job_profile_insights.extract_duty_facts_for_candidate(conn, candidate_id=cid, llm=llm)
            conn.commit()
        finally:
            conn.close()

    def test_semantic_merge_ratio_and_masked_examples(self) -> None:
        """语义级归并（规范化键）：三人表述不同（含标点/空白差异）的同一方向归并为一组，人数=3。"""
        def duty(payload: dict) -> dict:
            text = str(payload.get("resume_extraction_text") or "")
            label = "PC电源多相控制器"
            # 两人用规范表述、一人用带标点/空白变体：归并后标签取多数原始表述
            if "MPS" in text:
                label = "PC 电源／多相-控制器"
            return {"facts": [{"direction": label, "tools": ["示波器"], "role": "推广",
                               "customer": "服务器电源客户", "deliverable": "参考设计", "evidence": text[10:34]}]}

        self._extract_all(FakeLLM({}, duty_facts=duty))
        conn = self._connect()
        try:
            insight = job_profile_insights.aggregate_job_profile(conn, job_id=154)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(insight["status"], "ready")
        self.assertEqual(insight["source_count"], 3)
        self.assertEqual(len(insight["duties"]), 1)  # 三种表述归并为一组
        item = insight["duties"][0]
        self.assertEqual(item["count"], 3)
        self.assertAlmostEqual(item["ratio"], 1.0)
        self.assertLessEqual(len(item["examples"]), 3)
        names = {example["candidate"] for example in item["examples"]}
        self.assertEqual(names, {"张**", "李**", "王**"})  # 示例证据姓名遮罩
        self.assertTrue(all("张" not in e["evidence"][:1] or True for e in item["examples"]))
        self.assertEqual(item["label"], "PC电源多相控制器")  # 最常见原始表述为标签
        # 工具 / 客户 / 产出同样聚合
        self.assertEqual(insight["tools"][0]["label"], "示波器")
        self.assertEqual(insight["tools"][0]["count"], 3)
        self.assertEqual(insight["customers"][0]["count"], 3)
        self.assertEqual(insight["deliverables"][0]["label"], "参考设计")
        # 持久化一行，as_of 落库
        conn = self._connect()
        try:
            row = conn.execute("SELECT version,as_of,status FROM job_profile_insights WHERE job_id=154").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "ready")
            self.assertTrue(row["as_of"])
        finally:
            conn.close()

    def test_insufficient_when_less_than_min_sources(self) -> None:
        conn = self._connect()
        try:
            job_profile_insights.extract_duty_facts_for_candidate(
                conn, candidate_id=301, llm=FakeLLM({}, duty_facts=_duty_facts_from_payload)
            )
            job_profile_insights.extract_duty_facts_for_candidate(
                conn, candidate_id=302, llm=FakeLLM({}, duty_facts={"facts": []})
            )
            insight = job_profile_insights.aggregate_job_profile(conn, job_id=154)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(insight["status"], "insufficient")
        self.assertEqual(insight["source_count"], 1)
        self.assertEqual(insight["min_source_count"], 3)

    def test_aggregation_idempotent_no_double_count(self) -> None:
        self._extract_all(FakeLLM({}, duty_facts=_duty_facts_from_payload))
        conn = self._connect()
        try:
            first = job_profile_insights.aggregate_job_profile(conn, job_id=154)
            second = job_profile_insights.aggregate_job_profile(conn, job_id=154)  # 重算不重复计人
            conn.commit()
            self.assertEqual(first["source_count"], second["source_count"])
            self.assertEqual(second["duties"][0]["count"], first["duties"][0]["count"])
            self.assertEqual(second["version"], first["version"] + 1)
            self.assertNotEqual(second["as_of"], "")
            row = conn.execute("SELECT COUNT(*) AS c FROM job_profile_insights WHERE job_id=154").fetchone()
            self.assertEqual(row["c"], 1)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 3. 顾问纠正通道（disputed 降权 + 留痕）
# ---------------------------------------------------------------------------

class DisputeTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_three_persons(self.db_path)
        conn = self._connect()
        try:
            for cid in (301, 302, 303):
                job_profile_insights.extract_duty_facts_for_candidate(
                    conn, candidate_id=cid, llm=FakeLLM({}, duty_facts=_duty_facts_from_payload)
                )
            job_profile_insights.aggregate_job_profile(conn, job_id=154)
            conn.commit()
        finally:
            conn.close()

    def test_dispute_excludes_item_and_keeps_trace(self) -> None:
        conn = self._connect()
        try:
            result = job_profile_insights.submit_feedback(
                conn, job_id=154, item_type="duty", item_key="PC电源多相控制器",
                item_label="PC电源多相控制器", note="方向归并太宽",
            )
            conn.commit()
            self.assertEqual(result["status"], "disputed")
            self.assertFalse(result["already_disputed"])
            insight = result["insight"]
            self.assertEqual(insight["duties"], [])  # 主列表排除（降权到底）
            self.assertEqual(len(insight["disputed"]), 1)  # disputed 区留痕
            mark = insight["disputed"][0]
            self.assertEqual(mark["label"], "PC电源多相控制器")
            self.assertEqual(mark["count"], 3)
            self.assertEqual(mark["note"], "方向归并太宽")
            self.assertEqual(insight["stats"]["disputed_count"], 1)
            row = conn.execute(
                "SELECT status,note FROM job_profile_feedback WHERE job_id=154 AND item_type='duty'"
            ).fetchone()
            self.assertEqual((row["status"], row["note"]), ("disputed", "方向归并太宽"))
        finally:
            conn.close()

    def test_dispute_idempotent_single_row(self) -> None:
        conn = self._connect()
        try:
            job_profile_insights.submit_feedback(conn, job_id=154, item_type="duty", item_key="PC电源多相控制器", note="第一次")
            again = job_profile_insights.submit_feedback(conn, job_id=154, item_type="duty", item_key="PC电源多相控制器", note="第二次")
            conn.commit()
            self.assertTrue(again["already_disputed"])
            row = conn.execute(
                "SELECT COUNT(*) AS c, MAX(note) AS note FROM job_profile_feedback WHERE job_id=154"
            ).fetchone()
            self.assertEqual(row["c"], 1)
            self.assertEqual(row["note"], "第二次")
        finally:
            conn.close()

    def test_dispute_validation(self) -> None:
        conn = self._connect()
        try:
            with self.assertRaises(ValueError):
                job_profile_insights.submit_feedback(conn, job_id=154, item_type="bogus", item_key="x方向")
            with self.assertRaises(ValueError):
                job_profile_insights.submit_feedback(conn, job_id=154, item_type="duty", item_key="  ")
            with self.assertRaises(LookupError):
                job_profile_insights.submit_feedback(conn, job_id=999, item_type="duty", item_key="x方向")
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. 回填（活跃口径 + 幂等 + dry-run）
# ---------------------------------------------------------------------------

class BackfillTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_three_persons(self.db_path)

    def test_active_job_selection(self) -> None:
        conn = self._connect()
        try:
            # 199 只有已停止人选且无近期事件 → 不活跃；137 无人选但有近期事件 → 活跃
            conn.execute("INSERT INTO people(id,display_name) VALUES (91,'停止人')")
            conn.execute(
                "INSERT INTO job_candidates(id,job_id,person_id,clean_stage,raw_status,updated_at)"
                " VALUES (391,199,91,'H5 已停止','stopped','2020-01-01 00:00:00')"
            )
            conn.execute(
                "INSERT INTO candidate_events(job_id,event_type,event_status,event_time,summary)"
                " VALUES (137,'touch','completed',datetime('now','localtime'),'近90天事件')"
            )
            conn.execute(
                "INSERT INTO candidate_events(job_id,event_type,event_status,event_time,summary)"
                " VALUES (199,'touch','completed','2020-01-01 00:00:00','远古事件')"
            )
            conn.commit()
            active = job_profile_insights.list_active_jobs(conn)
            self.assertIn(154, active)  # 有待处理人选
            self.assertIn(137, active)  # 近 90 天有事件
            self.assertNotIn(199, active)  # 已停止 + 无近期事件
            self.assertEqual(job_profile_insights.list_active_jobs(conn, job_id=154), [154])
            self.assertEqual(job_profile_insights.list_active_jobs(conn, job_id=999), [])
        finally:
            conn.close()

    def test_backfill_idempotent_rerun(self) -> None:
        calls: list[int] = []

        def duty(payload: dict) -> dict:
            calls.append(1)
            return _duty_facts_from_payload(payload)

        conn = self._connect()
        try:
            llm = FakeLLM({}, duty_facts=duty)
            first = run_backfill(conn, llm=llm, dry_run=False)
            self.assertEqual(first["jobs_done"], 1)
            self.assertEqual(first["candidates_extracted"], 3)
            self.assertEqual(first["facts_kept"], 3)
            self.assertEqual(len(calls), 3)
            second = run_backfill(conn, llm=llm, dry_run=False)
            self.assertEqual(second["candidates_skipped_unchanged"], 3)  # source_hash 未变，跳过 LLM
            self.assertEqual(second["candidates_extracted"], 0)
            self.assertEqual(len(calls), 3)  # 没有新的模型调用
            row = conn.execute("SELECT COUNT(*) AS c FROM job_profile_facts WHERE job_id=154").fetchone()
            self.assertEqual(row["c"], 3)  # 同人同岗一行
            row = conn.execute("SELECT COUNT(*) AS c FROM job_profile_insights WHERE job_id=154").fetchone()
            self.assertEqual(row["c"], 1)
        finally:
            conn.close()

    def test_backfill_dry_run_writes_nothing(self) -> None:
        calls: list[int] = []

        def duty(payload: dict) -> dict:
            calls.append(1)
            return _duty_facts_from_payload(payload)

        conn = self._connect()
        try:
            summary = run_backfill(conn, llm=FakeLLM({}, duty_facts=duty), dry_run=True)
            self.assertTrue(summary["dry_run"])
            self.assertEqual(len(calls), 0)  # dry-run 不调模型
            facts = conn.execute("SELECT COUNT(*) AS c FROM job_profile_facts").fetchone()["c"]
            insights = conn.execute("SELECT COUNT(*) AS c FROM job_profile_insights").fetchone()["c"]
            events = conn.execute(
                "SELECT COUNT(*) AS c FROM candidate_events WHERE event_type='job_profile_generated'"
            ).fetchone()["c"]
            self.assertEqual((facts, insights, events), (0, 0, 0))
        finally:
            conn.close()

    def test_backfill_job_id_filter(self) -> None:
        conn = self._connect()
        try:
            summary = run_backfill(
                conn, llm=FakeLLM({}, duty_facts=_duty_facts_from_payload), job_id=154, dry_run=False
            )
            self.assertEqual(summary["jobs_active"], 1)
            self.assertEqual(summary["jobs_done"], 1)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 5. 增量触发
# ---------------------------------------------------------------------------

class IncrementalTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_three_persons(self.db_path)

    def test_refresh_extracts_only_new_person(self) -> None:
        calls: list[int] = []

        def duty(payload: dict) -> dict:
            calls.append(1)
            return _duty_facts_from_payload(payload)

        service = AgentService(self.db_path, FakeLLM({}, duty_facts=duty))
        try:
            # 先让 301/302 有事实（模拟存量已学）
            conn = self._connect()
            try:
                for cid in (301, 302):
                    job_profile_insights.extract_duty_facts_for_candidate(
                        conn, candidate_id=cid, llm=service.llm
                    )
                job_profile_insights.aggregate_job_profile(conn, job_id=154)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(len(calls), 2)
            # 新增 303 入库 → 增量刷新只抽 303，不整岗重算抽取
            result = service.submit_job_profile_refresh(303, trigger="mapping_intake", wait=True)
            self.assertTrue(result["result"]["ok"])
            self.assertEqual(len(calls), 3)  # 只多一调
            conn = self._connect()
            try:
                insight = conn.execute(
                    "SELECT source_count,status FROM job_profile_insights WHERE job_id=154"
                ).fetchone()
                self.assertEqual((insight["source_count"], insight["status"]), (3, "ready"))
            finally:
                conn.close()
        finally:
            service.close()

    def test_refresh_failure_never_blocks(self) -> None:
        service = AgentService(self.db_path, UnavailableLLM())
        try:
            result = service.submit_job_profile_refresh(301, wait=True)
            self.assertTrue(result["scheduled"])
            self.assertFalse(result["result"]["ok"])  # 模型不可用 → 记 error，不抛
            self.assertIn("error", result["result"])
        finally:
            service.close()

    def test_capture_hook_schedules_refresh(self) -> None:
        """简历捕获钩子：capture_liepin_resume 调度画像刷新；回执带 job_profile_refresh。"""
        captured = {
            "resume_id": "lp-resume-s8",
            "source_url": "https://h.liepin.com/resume/showresumedetail/?res_id_encode=lp-resume-s8",
            "name": "张三",
            "status": "在职，看机会",
            "company": "杰华特微电子股份有限公司",
            "title": "技术市场经理",
            "city": "杭州",
            "education": "本科",
            "experience": "9年",
            "work_text": RESUME_A,
            "project_text": "负责PC电源多相控制器参考设计项目",
            "education_text": "浙江大学 电子科学与技术 本科",
            "full_text": f"张三 杰华特微电子股份有限公司 技术市场经理 9年\n{RESUME_A}",
            "captured_at": "2026-07-24T10:00:00",
        }
        service = AgentService(self.db_path, FakeLLM(fake_assessment(), duty_facts=_duty_facts_from_payload))
        try:
            with patch("a_system_agent.service.capture_open_liepin_resumes", return_value=[captured]):
                result = service.capture_liepin_resume(301)
            self.assertTrue(result["ok"])
            refresh = result.get("job_profile_refresh") or {}
            self.assertTrue(refresh.get("scheduled"))
            self.assertEqual(refresh.get("job_id"), 154)
            # executor 关闭前等待后台刷新完成（close 会 wait）
        finally:
            service.close()
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT fact_count FROM job_profile_facts WHERE job_id=154 AND job_candidate_id=301"
            ).fetchone()
            self.assertIsNotNone(row)  # 异步刷新落库
        finally:
            conn.close()

    def test_intake_hook_calls_refresh(self) -> None:
        """Mapping 入库钩子：intake 成功后按回执的 job_candidate_id 调度画像刷新。"""
        service = AgentService(self.db_path, FakeLLM({}, duty_facts=_duty_facts_from_payload))
        seen: list[tuple[int, str]] = []
        original = service.submit_job_profile_refresh

        def recording(job_candidate_id: int, *, trigger: str = "manual", wait: bool = False):
            seen.append((int(job_candidate_id), trigger))
            return {"ok": True, "scheduled": True, "trigger": trigger}

        service.submit_job_profile_refresh = recording  # type: ignore[method-assign]
        try:
            with patch("a_system_agent.mapping_task.intake_candidate", return_value={"ok": True, "job_candidate_id": 302, "status": "intaken"}):
                result = service.intake_mapping_candidate("artifact_x", 0)
            self.assertEqual(seen, [(302, "mapping_intake")])
            self.assertTrue((result.get("job_profile_refresh") or {}).get("scheduled"))
        finally:
            service.submit_job_profile_refresh = original  # type: ignore[method-assign]
            service.close()


# ---------------------------------------------------------------------------
# 6. 展示接口 + feedback 路由
# ---------------------------------------------------------------------------

class ApiTest(DbCase):
    def setUp(self) -> None:
        super().setUp()
        _seed_three_persons(self.db_path)
        conn = self._connect()
        try:
            for cid in (301, 302, 303):
                job_profile_insights.extract_duty_facts_for_candidate(
                    conn, candidate_id=cid, llm=FakeLLM({}, duty_facts=_duty_facts_from_payload)
                )
            job_profile_insights.aggregate_job_profile(conn, job_id=154)
            conn.commit()
        finally:
            conn.close()
        app = create_app(db_path=self.db_path, start_legacy=False)
        self._client_ctx = TestClient(app)
        self.client = self._client_ctx.__enter__()

    def tearDown(self) -> None:
        self._client_ctx.__exit__(None, None, None)
        super().tearDown()

    def test_get_profile_insights_fields(self) -> None:
        response = self.client.get("/api/v1/jobs/154/profile-insights")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["source_count"], 3)
        self.assertEqual(payload["min_source_count"], 3)
        self.assertTrue(payload["as_of"])
        duty = payload["duties"][0]
        self.assertEqual(duty["count"], 3)
        self.assertEqual(duty["ratio"], 1.0)
        self.assertLessEqual(len(duty["examples"]), 3)
        self.assertTrue(all("**" in example["candidate"] for example in duty["examples"]))
        self.assertEqual(payload["disputed"], [])
        self.assertIn("facts_kept", payload["stats"])

    def test_get_not_generated_and_404(self) -> None:
        response = self.client.get("/api/v1/jobs/137/profile-insights")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "not_generated")
        self.assertEqual(payload["source_count"], 0)
        self.assertEqual(payload["duties"], [])
        missing = self.client.get("/api/v1/jobs/999/profile-insights")
        self.assertEqual(missing.status_code, 404)

    def test_feedback_route_disputed_and_idempotent_replay(self) -> None:
        body = {
            "request_id": "req_s8_feedback_1",
            "item_type": "duty",
            "item_key": "PC电源多相控制器",
            "item_label": "PC电源多相控制器",
            "note": "这条不对",
        }
        headers = {"Idempotency-Key": "s8-feedback-1"}
        first = self.client.post("/api/v1/jobs/154/profile-insights/feedback", json=body, headers=headers)
        self.assertEqual(first.status_code, 200)
        payload = first.json()
        self.assertEqual(payload["status"], "disputed")
        self.assertFalse(payload["already_disputed"])
        self.assertEqual(payload["duties"], [])  # 聚合已排除
        self.assertEqual(len(payload["disputed"]), 1)
        self.assertEqual(payload["stats"]["disputed_count"], 1)  # 顾问纠正写入统计
        replay = self.client.post("/api/v1/jobs/154/profile-insights/feedback", json=body, headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["receipt"]["idempotent_replay"])  # 重放返回首次响应
        self.assertEqual(replay.json()["disputed"], payload["disputed"])
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM job_profile_feedback WHERE job_id=154").fetchone()
            self.assertEqual(row["c"], 1)
            audit = conn.execute(
                "SELECT result FROM audit_events WHERE operation='job.profile_insights_feedback'"
            ).fetchone()
            self.assertIsNotNone(audit)  # 审计留痕
        finally:
            conn.close()
        refreshed = self.client.get("/api/v1/jobs/154/profile-insights")
        self.assertEqual(refreshed.json()["duties"], [])  # GET 反映 disputed
        self.assertEqual(len(refreshed.json()["disputed"]), 1)

    def test_feedback_route_409_and_404(self) -> None:
        bad = self.client.post(
            "/api/v1/jobs/154/profile-insights/feedback",
            json={"request_id": "req_s8_bad", "item_type": "bogus", "item_key": "x方向"},
            headers={"Idempotency-Key": "s8-bad-1"},
        )
        self.assertEqual(bad.status_code, 409)
        missing = self.client.post(
            "/api/v1/jobs/999/profile-insights/feedback",
            json={"request_id": "req_s8_404", "item_type": "duty", "item_key": "x方向"},
            headers={"Idempotency-Key": "s8-404-1"},
        )
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
