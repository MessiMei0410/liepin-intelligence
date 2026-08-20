"""性别维度进确定性筛选引擎（客户硬性口径：长越/长川"不推进女性人选"）回归守护。

- gender_inference：简历结构化性别字段 > display_name 称呼 > unknown（防误杀）；
- 引擎：jobs.gender_requirement='male_only'（migration 16）时铁证 female 判 X-排除，
  unknown 一律保留并标注「性别待核验」；开关关闭/缺列时完全不参与分级；
- 便签桥：口径便签含性别排除词时 commit 同事务把开关置 male_only（同一确认链）。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.candidate_pool_filter import (  # noqa: E402
    filter_job_candidates,
    format_grade_card,
    format_grade_list,
)
from a_system_agent.gender_inference import (  # noqa: E402
    detect_male_only_note,
    infer_gender,
)
from asa_core.database import MIGRATIONS  # noqa: E402
from asa_core.service import CoreService  # noqa: E402

_STRONG_MECH_SUMMARY = (
    "主导六自由度精密运动台（工件台）整机设计，微米级定位，半导体设备装配线，"
    "使用 Ansys 做有限元、模态与热变形分析，直线电机与光栅尺选型"
)


def _migration_sql(version: int) -> str:
    return next(sql for v, _name, sql in MIGRATIONS if v == version)


def _gender_db(tmp: str) -> Path:
    """合成库：male_only 岗位 137 + 不限岗位 138，覆盖称呼/简历字段/未知三种证据形态。"""
    db_path = Path(tmp) / "gender-filter.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        f"""
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT,
                           gender_requirement TEXT NOT NULL DEFAULT '');
        CREATE TABLE people (
            id INTEGER PRIMARY KEY, display_name TEXT, current_company TEXT,
            current_title TEXT, city TEXT, education TEXT, experience TEXT
        );
        CREATE TABLE job_candidates (
            id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER,
            clean_stage TEXT, flow_bucket TEXT, source_candidate_id TEXT, updated_at TEXT
        );
        CREATE TABLE candidate_profiles (
            id INTEGER PRIMARY KEY, candidate_id INTEGER, candidate_name TEXT,
            candidate_company TEXT, position TEXT, education_level TEXT,
            seniority TEXT, profile_summary TEXT
        );

        INSERT INTO clients VALUES (1, '长越科技');
        INSERT INTO jobs (id, client_id, title, gender_requirement) VALUES
            (137, 1, '机械高级工程师', 'male_only'),
            (138, 1, '机械高级工程师', '');
        INSERT INTO people VALUES
            (1, '孙女士', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年'),
            (2, '武先生', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年'),
            (3, '遮罩丙', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年'),
            (4, '李工', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年');
        INSERT INTO job_candidates VALUES
            (201, 137, 1, 'S1 新增寻访/待复核', '待复核', '301', '2026-08-20'),
            (202, 137, 2, 'S1 新增寻访/待复核', '待复核', '302', '2026-08-20'),
            (203, 137, 3, 'S1 新增寻访/待复核', '待复核', '303', '2026-08-20'),
            (204, 137, 4, 'S1 新增寻访/待复核', '待复核', '304', '2026-08-20'),
            (205, 138, 1, 'S1 新增寻访/待复核', '待复核', '301', '2026-08-20');
        INSERT INTO candidate_profiles VALUES
            (1, 301, '孙女士', '上海微电子装备', '机械设计工程师', '硕士', '10年', '{_STRONG_MECH_SUMMARY}'),
            (2, 302, '武先生', '上海微电子装备', '机械设计工程师', '硕士', '10年', '{_STRONG_MECH_SUMMARY}'),
            (3, 303, '遮罩丙', '上海微电子装备', '机械设计工程师', '硕士', '10年', '{_STRONG_MECH_SUMMARY}'),
            (4, 304, '李工', '上海微电子装备', '机械设计工程师', '硕士', '10年',
             '性别：女 | 求职意向 | 机械工程师 | 上海\n{_STRONG_MECH_SUMMARY}');
        """
    )
    conn.commit()
    conn.close()
    return db_path


class GenderInferenceCase(unittest.TestCase):
    def test_honorific_inference(self) -> None:
        self.assertEqual(infer_gender("武先生")["gender"], "male")
        hit = infer_gender("孙女士")
        self.assertEqual(hit["gender"], "female")
        self.assertEqual(hit["evidence"]["source"], "display_name_honorific")

    def test_resume_field_beats_display_name(self) -> None:
        # 优先级：简历结构化性别字段 > display_name 称呼
        hit = infer_gender("孙女士", "性别：男 | 求职意向 | 机械工程师")
        self.assertEqual(hit["gender"], "male")
        self.assertEqual(hit["evidence"]["source"], "resume_gender_field")
        self.assertIn("性别：男", hit["evidence"]["snippet"])

    def test_resume_header_and_paren_patterns(self) -> None:
        self.assertEqual(infer_gender("", "", "王小明\n男 | 28岁 | 本科")["gender"], "male")
        self.assertEqual(infer_gender("", "", "张三（女）\n求职意向")["gender"], "female")

    def test_unknown_when_no_hard_evidence(self) -> None:
        self.assertEqual(infer_gender("遮罩丙", "负责精密机械设计十年")["gender"], "unknown")
        # 误杀守护：正文出现"女"字（非结构化证据）不得误判
        self.assertEqual(infer_gender("", "", "她育有一女，家庭幸福，长期稳定")["gender"], "unknown")
        self.assertIsNone(infer_gender("张三")["evidence"])

    def test_male_only_note_detection(self) -> None:
        for text in ("该岗位不看女生", "客户说不要女性候选人", "限男", "仅男性", "男性优先"):
            self.assertEqual(detect_male_only_note(text), "male_only", text)
        self.assertEqual(detect_male_only_note("六自由度运动台作为大加分项"), "")
        self.assertEqual(detect_male_only_note(""), "")


class GenderFilterEngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = _gender_db(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _by_name(self, result: dict) -> dict:
        return {item["name"]: item for item in result["candidates"]}

    def test_male_only_excludes_female_keeps_male_flags_unknown(self) -> None:
        result = filter_job_candidates(str(self.db_path), 137, client="长越科技", domain="mechanical")
        rows = self._by_name(result)
        # 女士称呼 → X-排除，reason 带正式表述与证据片段
        self.assertEqual(rows["孙女士"]["grade"], "X-排除")
        self.assertIn("客户口径：不推进女性人选", rows["孙女士"]["reason"])
        self.assertIn("孙女士", rows["孙女士"]["reason"])
        # 先生称呼 → 正常分级保留
        self.assertEqual(rows["武先生"]["grade"], "A-核心")
        self.assertNotIn("性别待核验", rows["武先生"]["reason"])
        # 无称呼无性别字段 → 保留 + 待核验标注
        self.assertEqual(rows["遮罩丙"]["grade"], "A-核心")
        self.assertIn("性别待核验", rows["遮罩丙"]["reason"])
        # 遮罩名无称呼但简历「性别：女」→ 铁证排除
        self.assertEqual(rows["李工"]["grade"], "X-排除")
        self.assertIn("性别：女", rows["李工"]["reason"])
        # 汇总计数
        self.assertEqual(result["gender_requirement"], "male_only")
        self.assertEqual(result["gender_excluded"], 2)
        self.assertEqual(result["gender_unknown"], 1)

    def test_switch_off_leaves_everything_untouched(self) -> None:
        result = filter_job_candidates(str(self.db_path), 138, client="长越科技", domain="mechanical")
        rows = self._by_name(result)
        self.assertEqual(rows["孙女士"]["grade"], "A-核心")
        self.assertEqual(result["gender_requirement"], "")
        self.assertEqual(result["gender_excluded"], 0)
        for item in result["candidates"]:
            self.assertEqual(item["gender"], "")
            self.assertNotIn("性别待核验", item["reason"])

    def test_grade_list_and_card_summary_carry_gender_line(self) -> None:
        result = filter_job_candidates(str(self.db_path), 137, client="长越科技", domain="mechanical")
        answer = format_grade_list(result)
        self.assertIn("性别口径", answer)
        self.assertIn("已凭铁证排除 2 人", answer)
        self.assertIn("1 人性别待核验", answer)
        self.assertIn("遮罩丙", answer)
        self.assertIn("｜性别待核验", answer)
        _, card = format_grade_card(result, client="长越科技", job_title="机械高级工程师", job_id=137)
        self.assertEqual(card["summary"]["gender_excluded"], 2)
        self.assertEqual(card["summary"]["gender_unknown"], 1)
        self.assertEqual(card["gender_requirement"], "male_only")


class GenderNoteBridgeCase(unittest.TestCase):
    """便签→开关的桥：含性别排除词的口径便签 commit 同事务置 male_only。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "asa.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(_migration_sql(1))
        conn.executescript(_migration_sql(15))
        conn.executescript(
            """
            ALTER TABLE api_idempotency ADD COLUMN error_json TEXT;
            ALTER TABLE api_idempotency ADD COLUMN updated_at TEXT;
            CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT);
            INSERT INTO clients VALUES (1, '长越科技');
            INSERT INTO jobs VALUES (137, 1, '机械高级工程师');
            INSERT INTO jobs VALUES (138, 1, '机械高级工程师');
            """
        )
        conn.executescript(_migration_sql(16))
        conn.commit()
        conn.close()
        self.core = CoreService(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _commit(self, job_id: int, note: str, request_id: str) -> dict:
        token = self.core.filter_note_preflight(job_id, note)["token"]
        self.core.activate_preflight_token(token)
        return self.core.filter_note_commit(job_id, note, token, request_id=request_id)

    def test_preflight_flags_gender_note_without_writing(self) -> None:
        preflight = self.core.filter_note_preflight(137, "该岗位不看女生，不推进女性人选")
        self.assertTrue(preflight["gender_requirement_detected"])
        self.assertIn("检测到性别限制口径", preflight["impact"])
        self.assertEqual(preflight["gender_requirement"], "")
        # 预检绝不写库
        conn = sqlite3.connect(self.db_path)
        value = conn.execute("SELECT gender_requirement FROM jobs WHERE id=137").fetchone()[0]
        conn.close()
        self.assertEqual(value, "")

    def test_commit_sets_male_only_in_same_confirmation_chain(self) -> None:
        result = self._commit(137, "该岗位不看女生，不推进女性人选", "req-g-1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["gender_requirement_detected"])
        self.assertEqual(result["gender_requirement"], "male_only")
        self.assertIn("检测到性别限制口径", result["notice"])
        # GET 响应携带开关
        stored = self.core.get_job_filter_note(137)
        self.assertEqual(stored["gender_requirement"], "male_only")
        self.assertIn("不看女", stored["note"]["note"])

    def test_plain_note_does_not_touch_switch(self) -> None:
        result = self._commit(138, "六自由度运动台作为大加分项", "req-g-2")
        self.assertFalse(result["gender_requirement_detected"])
        self.assertEqual(result["gender_requirement"], "")
        self.assertNotIn("notice", result)
        self.assertEqual(self.core.get_job_filter_note(138)["gender_requirement"], "")


if __name__ == "__main__":
    unittest.main()
