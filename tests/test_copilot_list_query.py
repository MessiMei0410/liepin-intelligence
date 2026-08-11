"""Copilot 查询型名单直答（candidate list query）回归守护。

背景（2026-08-10）：顾问在 copilot_3c47cce30f3a 会话里说
"把长越的机械人选名单给我"，copilot 只建了 candidate_review 计划（create_plan）
却不返回名单，因为 turn_decision 把查询型请求当成建计划请求处理。

修复：_copilot_impl 在建计划分支前识别查询型名单请求（_is_candidate_list_query），
直接查候选池生成名单（_format_candidate_list_answer）作为 forced_answer，
不再创建等待确认的 workflow。

覆盖：
1. _is_candidate_list_query 判定——"名单/筛出/优先评估" 命中，"寻访/补池/触达/计划" 排除。
2. _format_candidate_list_answer 名单生成——岗位上下文、阶段分组、
   固晶/共晶/键合优先分组、不把"补搜"伪命中当真实经历。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from a_system_agent.copilot_handler import (  # noqa: E402
    _format_candidate_list_answer,
    _is_candidate_list_query,
)
from asa_core.service import CoreService  # noqa: E402


def _make_db() -> Path:
    """临时 DB：一个岗位 + 客户 + 候选人（含固晶背景与非固晶、停止者、补搜伪命中）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, status TEXT, summary TEXT);
        CREATE TABLE people (
            id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
            current_title TEXT, city TEXT, education TEXT, experience TEXT, fingerprint TEXT
        );
        CREATE TABLE job_candidates (
            id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
            raw_client TEXT, raw_position TEXT, raw_status TEXT, raw_stage TEXT,
            clean_stage TEXT, flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER,
            search_date TEXT, updated_at TEXT, source_candidate_id TEXT, stop_reason TEXT
        );
        CREATE TABLE candidate_profiles (
            id INTEGER PRIMARY KEY, candidate_id INTEGER, candidate_name TEXT,
            candidate_company TEXT, client TEXT, position TEXT, education_level TEXT,
            seniority TEXT, industry_tags_json TEXT, function_tags_json TEXT,
            risk_tags_json TEXT, profile_summary TEXT, updated_at TEXT
        );
        INSERT INTO clients (id, name) VALUES (1, '长越科技');
        INSERT INTO jobs (id, client_id, title, status) VALUES (137, 1, '机械高级工程师', '已发布');
        INSERT INTO people (id, display_name, current_company, current_title, fingerprint) VALUES
            (1, '张航', 'ASM中国集团公司', '高级机械设计工程师', 'a'),
            (2, '陈**', '先导科技集团有限公司', '结构设计工程师', 'b'),
            (3, '王先生', '华为', '机械技术专家', 'c'),
            (4, '刘先生', '上海泽丰半导体科技有限公司', '机械工程师', 'd'),
            (5, '补搜先生', '某软件公司', '软件开发', 'e');
        INSERT INTO job_candidates (id, job_id, person_id, clean_stage, flow_bucket) VALUES
            (522, 137, 1, '已触达', '猎聘触达'),
            (519, 137, 2, 'S1 新增寻访/待复核', '待复核'),
            (529, 137, 3, 'S1 新增寻访/待复核', '待复核'),
            (511, 137, 4, 'H5 最近寻访/初筛不通过', '最近寻访'),
            (993, 137, 5, 'S1 新增寻访/待复核', '待复核');
        INSERT INTO candidate_profiles (id, candidate_id, candidate_name, candidate_company, profile_summary) VALUES
            (1140, 1, '张航', 'ASM中国集团公司',
             '一直从事高速高精固晶机设计八年，当前 31K*16 薪。'),
            (1137, 2, '陈**', '先导科技集团有限公司',
             '曾在华卓精科参与12寸晶圆临时键合设备，负责键合真空室和气浮运动平台升级设计。'),
            (1086, 5, '补搜先生', '某软件公司',
             '键合类设备补搜：query=半导体设备 控制软件 EtherCAT；命中=运动控制,EtherCAT。');
        """
    )
    conn.commit()
    conn.close()
    return Path(path)


class CandidateListQueryTest(unittest.TestCase):
    def test_list_query_markers(self) -> None:
        self.assertTrue(_is_candidate_list_query("把长越的机械人选名单给我"))
        self.assertTrue(_is_candidate_list_query("从当前候选池里先筛出做过固晶机/共晶机/键合机的人，给出优先评估名单"))
        self.assertTrue(_is_candidate_list_query("把岗位 137 的候选人列表给我"))
        self.assertTrue(_is_candidate_list_query("整理一份长越机械岗的核验名单"))

    def test_list_query_false_positives_excluded(self) -> None:
        # kimi review #7：宽泛 marker 与混合意图不应误判为名单查询
        self.assertFalse(_is_candidate_list_query("长越这个岗位有哪些风险"))
        self.assertFalse(_is_candidate_list_query("把名单发给客户"))
        self.assertFalse(_is_candidate_list_query("重新评估这批名单"))
        self.assertFalse(_is_candidate_list_query("把名单整理成推荐报告发给客户"))

    def test_execution_requests_excluded(self) -> None:
        self.assertFalse(_is_candidate_list_query("为长越科技机械高级工程师启动一轮多渠道寻访，猎聘和 X-SaaS 都要跑，目标 10 人"))
        self.assertFalse(_is_candidate_list_query("重新建立一轮候选人寻访计划"))
        self.assertFalse(_is_candidate_list_query("把候选人完整履历抓回来，然后批量评估"))
        self.assertFalse(_is_candidate_list_query("触达这批候选人，把名单发出去"))
        self.assertFalse(_is_candidate_list_query("机械岗位最新要求尽量找做固晶机/共晶机/键合机的"))

    def test_format_answer_groups_and_prioritizes(self) -> None:
        db = _make_db()
        try:
            answer = _format_candidate_list_answer(str(db), 137, "筛出做过固晶机/共晶机/键合机的人，给出优先评估名单")
            self.assertIn("长越科技｜机械高级工程师", answer)
            self.assertIn("固晶机/共晶机/键合机背景", answer)
            # 真实固晶/键合经历进优先组
            self.assertIn("张航", answer)
            self.assertIn("陈**", answer)
            # 补搜伪命中不冒充真实经历
            prio_section = answer.split("### ⭐ 固晶机/共晶机/键合机背景")[1].split("###")[0]
            self.assertNotIn("补搜先生", prio_section)
            # 停止者不进可推进组
            self.assertNotIn("上海泽丰", answer.split("### 其余可推进候选")[1].split("###")[0])
        finally:
            db.unlink()

    def test_format_answer_without_bonder_message(self) -> None:
        db = _make_db()
        try:
            answer = _format_candidate_list_answer(str(db), 137, "把名单给我")
            self.assertIn("长越科技｜机械高级工程师", answer)
            self.assertNotIn("固晶机/共晶机/键合机背景", answer)
        finally:
            db.unlink()

    def test_format_answer_empty_pool(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, status TEXT, summary TEXT);
            CREATE TABLE people (
                id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
                current_title TEXT, city TEXT, education TEXT, experience TEXT, fingerprint TEXT
            );
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
                raw_client TEXT, raw_position TEXT, raw_status TEXT, raw_stage TEXT,
                clean_stage TEXT, flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER,
                search_date TEXT, updated_at TEXT, source_candidate_id TEXT, stop_reason TEXT
            );
            INSERT INTO clients (id, name) VALUES (1, '长越科技');
            INSERT INTO jobs (id, client_id, title, status) VALUES (137, 1, '机械高级工程师', '已发布');
            """
        )
        conn.commit()
        conn.close()
        try:
            answer = _format_candidate_list_answer(path, 137, "把名单给我")
            self.assertIn("候选池为空", answer)
        finally:
            Path(path).unlink()

    def test_format_answer_stop_stages_include_eliminated_closed(self) -> None:
        # kimi review #3：淘汰/关闭 与 初筛不通过/停止 一样视为已停止
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, status TEXT, summary TEXT);
            CREATE TABLE people (
                id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
                current_title TEXT, city TEXT, education TEXT, experience TEXT, fingerprint TEXT
            );
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
                raw_client TEXT, raw_position TEXT, raw_status TEXT, raw_stage TEXT,
                clean_stage TEXT, flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER,
                search_date TEXT, updated_at TEXT, source_candidate_id TEXT, stop_reason TEXT
            );
            INSERT INTO clients (id, name) VALUES (1, '长越科技');
            INSERT INTO jobs (id, client_id, title, status) VALUES (137, 1, '机械高级工程师', '已发布');
            INSERT INTO people (id, display_name, current_company, current_title, fingerprint) VALUES
                (1, '甲**', 'A公司', '机械工程师', 'a'),
                (2, '乙**', 'B公司', '机械工程师', 'b'),
                (3, '丙**', 'C公司', '机械工程师', 'c');
            INSERT INTO job_candidates (id, job_id, person_id, clean_stage, flow_bucket) VALUES
                (1, 137, 1, '已触达', '猎聘触达'),
                (2, 137, 2, '已淘汰', '最近寻访'),
                (3, 137, 3, '岗位关闭', '最近寻访');
            """
        )
        conn.commit()
        conn.close()
        try:
            answer = _format_candidate_list_answer(path, 137, "把名单给我")
            self.assertIn("可推进 1 人", answer)
            self.assertIn("已停止 2 人", answer)
            self.assertIn("甲**", answer.split("### 其余可推进候选")[1])
        finally:
            Path(path).unlink()

    def test_format_answer_missing_candidate_profiles_table(self) -> None:
        # kimi review #8：缺 candidate_profiles 表时固晶路径降级为普通名单，不抛错
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, status TEXT, summary TEXT);
            CREATE TABLE people (
                id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
                current_title TEXT, city TEXT, education TEXT, experience TEXT, fingerprint TEXT
            );
            CREATE TABLE job_candidates (
                id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
                raw_client TEXT, raw_position TEXT, raw_status TEXT, raw_stage TEXT,
                clean_stage TEXT, flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER,
                search_date TEXT, updated_at TEXT, source_candidate_id TEXT, stop_reason TEXT
            );
            INSERT INTO clients (id, name) VALUES (1, '长越科技');
            INSERT INTO jobs (id, client_id, title, status) VALUES (137, 1, '机械高级工程师', '已发布');
            INSERT INTO people (id, display_name, current_company, current_title, fingerprint) VALUES
                (1, '张航', 'ASM中国集团公司', '高级机械设计工程师', 'a');
            INSERT INTO job_candidates (id, job_id, person_id, clean_stage, flow_bucket) VALUES
                (522, 137, 1, '已触达', '猎聘触达');
            """
        )
        conn.commit()
        conn.close()
        try:
            answer = _format_candidate_list_answer(path, 137, "筛出做过固晶机的人，给出优先评估名单")
            self.assertIn("长越科技｜机械高级工程师", answer)
            self.assertIn("张航", answer)
        finally:
            Path(path).unlink()

    def test_format_answer_job_not_found(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT, status TEXT, summary TEXT);
            CREATE TABLE job_candidates (id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER);
            CREATE TABLE people (id INTEGER PRIMARY KEY, display_name TEXT);
            """
        )
        conn.commit()
        conn.close()
        try:
            self.assertEqual(_format_candidate_list_answer(path, 999, "把名单给我"), "")
        finally:
            Path(path).unlink()

    def test_candidate_list_card_refresh_reflects_latest_stage(self) -> None:
        """名单卡刷新 API（CoreService.candidate_list_card）：重建快照反映库内最新状态。

        场景（2026-08-11）：薛傲复核通过（S1 待复核 → S2 复核通过/待联系），
        原名单卡是生成时的静态快照仍显示 S1；刷新后必须读到 S2。
        """
        db = _make_db()
        try:
            service = CoreService(db_path=db)
            # 初始：张航（id=522）已触达
            result = service.candidate_list_card(137)
            card = result["card"]
            by_id = {c["id"]: c for g in card["groups"] for c in g["candidates"]}
            self.assertEqual(by_id[522]["stage"], "已触达")
            self.assertEqual(by_id[519]["stage"], "S1 新增寻访/待复核")
            # 模拟复核通过：S1 → S2
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE job_candidates SET clean_stage='S2 复核通过/待联系', flow_bucket='待联系', updated_at=datetime('now','localtime') WHERE id=519"
            )
            conn.commit()
            conn.close()
            refreshed = service.candidate_list_card(137)
            refreshed_by_id = {c["id"]: c for g in refreshed["card"]["groups"] for c in g["candidates"]}
            self.assertEqual(refreshed_by_id[519]["stage"], "S2 复核通过/待联系")
            self.assertEqual(refreshed_by_id[519]["flow_bucket"], "待联系")
            # bonder=True 时固晶优先组出现
            bonder = service.candidate_list_card(137, bonder=True)
            keys = [g["key"] for g in bonder["card"]["groups"]]
            self.assertIn("bonder", keys)
        finally:
            db.unlink()

    def test_candidate_list_card_refresh_job_not_found(self) -> None:
        db = _make_db()
        try:
            service = CoreService(db_path=db)
            with self.assertRaises(LookupError):
                service.candidate_list_card(999)
        finally:
            db.unlink()


if __name__ == "__main__":
    unittest.main()
