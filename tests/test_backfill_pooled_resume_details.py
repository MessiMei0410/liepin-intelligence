import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import backfill_pooled_resume_details as backfill


SCHEMA = """
CREATE TABLE clients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, title TEXT NOT NULL);
CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    current_company TEXT, current_title TEXT, city TEXT, education TEXT, experience TEXT
);
CREATE TABLE job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, person_id INTEGER NOT NULL, source_candidate_id TEXT
);
CREATE TABLE source_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL, source_type TEXT NOT NULL,
    source_candidate_id TEXT, source_date TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER, person_id INTEGER, job_id INTEGER,
    event_type TEXT NOT NULL, event_status TEXT, event_time TEXT,
    summary TEXT, raw_json TEXT DEFAULT '{}', source_table TEXT, source_id TEXT
);
"""

LONG_RESUME = "候选人完整履历，含多年工作经历与项目经历。" * 10


def _seed(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute("INSERT INTO clients (name) VALUES ('测试客户')")
    conn.execute("INSERT INTO jobs (client_id, title) VALUES (1, '算法工程师')")
    people = [
        ("张三", "甲公司", "算法工程师", "上海", "硕士", "8年"),   # 已有完整履历
        ("李四", "乙公司", "视觉算法", None, None, None),        # 履历不完整
        ("王五", "丙公司", "推荐算法", None, None, None),        # 无 source_profiles，事件里有链接
        ("赵六", "丁公司", "NLP 算法", None, None, None),        # 无任何链接
    ]
    for person in people:
        conn.execute(
            "INSERT INTO people (display_name,current_company,current_title,city,education,experience) VALUES (?,?,?,?,?,?)",
            person,
        )
    for person_id in (1, 2, 3, 4):
        conn.execute(
            "INSERT INTO job_candidates (job_id, person_id, source_candidate_id) VALUES (1, ?, ?)",
            (person_id, f"res-{person_id}"),
        )
    # 张三：完整履历，不应再抓
    conn.execute(
        "INSERT INTO source_profiles (person_id,source_type,source_candidate_id,source_date,raw_json) VALUES (1,'liepin','res-1','2026-08-01',?)",
        (json.dumps({"full_text": LONG_RESUME, "resume_capture_status": "complete",
                     "source_url": "https://h.liepin.com/resume/a"}, ensure_ascii=False),),
    )
    # 李四：只有列表摘要，履历不完整
    conn.execute(
        "INSERT INTO source_profiles (person_id,source_type,source_candidate_id,source_date,raw_json) VALUES (2,'liepin','res-2','2026-08-01',?)",
        (json.dumps({"full_text": "摘要", "resume_capture_status": "not_requested_detail_limit",
                     "source_url": "https://h.liepin.com/resume/b"}, ensure_ascii=False),),
    )
    # 王五：没有 source_profiles，但 search_shortlisted 事件里存了详情链接
    conn.execute(
        "INSERT INTO candidate_events (job_candidate_id,person_id,job_id,event_type,raw_json) VALUES (3,3,1,'search_shortlisted',?)",
        (json.dumps({"resume_url": "https://h.liepin.com/resume/c", "raw_text": "列表摘要"}, ensure_ascii=False),),
    )
    conn.commit()
    return {"job_id": 1}


def _fake_capture_complete(port, cards, limit, **kwargs):
    for card in cards:
        card.update({
            "full_text": LONG_RESUME,
            "work_text": "工作经历 " * 20,
            "project_text": "项目经历 " * 10,
            "education_text": "教育经历 本科",
            "profile_text": LONG_RESUME,
            "resume_capture_status": "complete",
            "resume_capture_missing": [],
            "resume_capture_error": "",
            "resume_captured_at": "2026-08-11T10:00:00",
            "resume_id": f"resume-{card['_person_id']}",
            "city": "杭州",
            "education": "本科",
            "experience": "6年",
        })
    return {"requested": len(cards), "attempted": len(cards), "complete": len(cards),
            "partial": 0, "failed": 0, "risk_paused": 0, "status": "completed"}


class SelectTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        _seed(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_selects_only_pooled_candidates_missing_full_resume(self) -> None:
        targets, skipped = backfill.select_targets(self.conn, "测试客户", "算法工程师", 40)
        self.assertEqual(skipped, 1)  # 赵六没有任何详情链接
        names = [card["name"] for card in targets]
        self.assertEqual(names, ["李四", "王五"])  # 张三已有完整履历被跳过
        by_name = {card["name"]: card for card in targets}
        self.assertEqual(by_name["李四"]["resume_url"], "https://h.liepin.com/resume/b")
        # 王五没有 source_profiles，链接来自 search_shortlisted 事件
        self.assertEqual(by_name["王五"]["resume_url"], "https://h.liepin.com/resume/c")
        self.assertIsNone(by_name["王五"]["_source_profile_id"])

    def test_limit_truncates(self) -> None:
        targets, skipped = backfill.select_targets(self.conn, "测试客户", "算法工程师", 1)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["name"], "李四")


class MainBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SCHEMA)
        _seed(self.conn)
        self.conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_main(self) -> dict:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = backfill.main([
                "--db", str(self.db_path), "--client", "测试客户", "--job", "算法工程师",
                "--port", "9999",
            ])
        self.assertEqual(code, 0)
        return json.loads(stdout.getvalue())

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_backfill_writes_profiles_people_and_events(self) -> None:
        with patch("backfill_pooled_resume_details.capture_resume_details", _fake_capture_complete):
            result = self._run_main()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["skipped_no_url"], 1)

        # 李四：UPDATE 已有 source_profiles 行
        rows = self._query("SELECT * FROM source_profiles WHERE person_id=2")
        self.assertEqual(len(rows), 1)
        raw = json.loads(rows[0]["raw_json"])
        self.assertEqual(raw["full_text"], LONG_RESUME)
        self.assertEqual(raw["resume_capture_status"], "complete")
        self.assertEqual(raw["capture_method"], "asa_liepin_cdp_backfill")
        self.assertTrue(rows[0]["source_date"])  # source_date 已刷新
        # 王五：INSERT 新行，source_candidate_id 用抓回的 resume_id
        rows = self._query("SELECT * FROM source_profiles WHERE person_id=3 AND source_type='liepin'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["raw_json"])["full_text"], LONG_RESUME)
        # 张三不受影响
        rows = self._query("SELECT * FROM source_profiles WHERE person_id=1")
        self.assertEqual(json.loads(rows[0]["raw_json"])["full_text"], LONG_RESUME)
        self.assertNotEqual(
            json.loads(rows[0]["raw_json"]).get("capture_method"), "asa_liepin_cdp_backfill"
        )
        # people 空字段被补上
        row = self._query("SELECT * FROM people WHERE id=2")[0]
        self.assertEqual(row["city"], "杭州")
        self.assertEqual(row["education"], "本科")
        self.assertEqual(row["experience"], "6年")
        # 各写一条 resume_profile_captured 事件
        events = self._query(
            "SELECT * FROM candidate_events WHERE event_type='resume_profile_captured' ORDER BY person_id"
        )
        self.assertEqual([int(e["person_id"]) for e in events], [2, 3])
        self.assertTrue(all(e["source_table"] == "source_profiles" for e in events))

    def test_risk_pause_keeps_completed_writes(self) -> None:
        def fake_capture(port, cards, limit, **kwargs):
            _fake_capture_complete(port, cards[:1], limit)
            for card in cards[1:]:
                card.update({"resume_capture_status": "risk_paused",
                             "resume_capture_missing": ["完整履历"],
                             "resume_capture_error": "risk_page:安全验证"})
            return {"requested": len(cards), "attempted": 1, "complete": 1, "partial": 0,
                    "failed": 0, "risk_paused": 1, "status": "risk_paused",
                    "risk_reason": "risk_page:安全验证"}

        with patch("backfill_pooled_resume_details.capture_resume_details", fake_capture):
            result = self._run_main()
        self.assertEqual(result["status"], "risk_paused")
        self.assertEqual(result["written"], 1)
        rows = self._query("SELECT * FROM source_profiles WHERE person_id=2")
        self.assertEqual(json.loads(rows[0]["raw_json"])["resume_capture_status"], "complete")
        # 王五未被抓取，不应新增 source_profiles
        rows = self._query("SELECT * FROM source_profiles WHERE person_id=3")
        self.assertEqual(len(rows), 0)

    def test_dry_run_lists_targets_without_writing(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = backfill.main([
                "--db", str(self.db_path), "--client", "测试客户", "--job", "算法工程师",
                "--dry-run",
            ])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(result["targets"]), 2)
        self.assertEqual(len(self._query("SELECT * FROM source_profiles WHERE person_id=3")), 0)


if __name__ == "__main__":
    unittest.main()
