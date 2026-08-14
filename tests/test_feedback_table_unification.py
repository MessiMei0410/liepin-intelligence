"""P3-b：客户反馈新旧表口径统一（recommendation_package_feedback 双写 client_feedback_events）。

合成库测试（CI 可跑，不依赖本机正式库）。覆盖：
1. 新反馈写入 → 旧表可见同口径记录（候选人/客户/岗位/类型/内容/时间 + 留痕链路）；
2. 同 request_id 重复提交 → 旧表不双份；
3. 旧读方口径（generate_workflow_status_report 的正/负反馈统计 SQL 原样）能读到镜像行；
4. 旧表已存在（含 governance 扩展列已/未加两种形态）时双写兼容。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from asa_core.service import CoreService


SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,client_id INTEGER,title TEXT);
CREATE TABLE people(id INTEGER PRIMARY KEY,display_name TEXT,current_company TEXT);
CREATE TABLE candidates(id INTEGER PRIMARY KEY,name TEXT,company TEXT);
CREATE TABLE job_candidates(id INTEGER PRIMARY KEY,job_id INTEGER,person_id INTEGER,
  source_candidate_id TEXT,clean_stage TEXT);
CREATE TABLE candidate_events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_candidate_id INTEGER,
  person_id INTEGER,job_id INTEGER,event_type TEXT,event_status TEXT,event_time TEXT,
  summary TEXT,raw_json TEXT,source_table TEXT,source_id TEXT);
CREATE TABLE recommendation_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL UNIQUE,
    job_candidate_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    recommendation_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated',
    summary_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    risks_json TEXT NOT NULL DEFAULT '[]',
    verification_questions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(job_candidate_id, version)
);
CREATE TABLE recommendation_package_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    package_version INTEGER NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,
    content TEXT NOT NULL,
    feedback_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    recorded_by TEXT NOT NULL DEFAULT 'consultant',
    request_id TEXT NOT NULL,
    event_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(package_id, request_id)
);

INSERT INTO clients VALUES (1,'长越科技');
INSERT INTO jobs VALUES (10,1,'机械高级工程师');
INSERT INTO people VALUES (100,'张三','苏州敏芯');
INSERT INTO candidates VALUES (5501,'张三','苏州敏芯');
INSERT INTO job_candidates VALUES (559,10,100,'5501','客户反馈');
INSERT INTO recommendation_packages
  (package_id,job_candidate_id,person_id,job_id,recommendation_id,version,status)
VALUES ('recpkg_p3b',559,100,10,7,1,'generated');
"""

# 旧读方口径原样取自 scripts/generate_workflow_status_report.py（正/负反馈统计）。
POSITIVE_SQL = """
SELECT COUNT(*) FROM client_feedback_events
WHERE feedback_type IN ('approved', 'interviewing', 'interview_passed', 'offer', 'hired')
"""
NEGATIVE_SQL = """
SELECT COUNT(*) FROM client_feedback_events
WHERE feedback_type IN ('rejected', 'interview_failed', 'eliminated')
"""


def _service(db_path: Path, schema: str = SCHEMA) -> CoreService:
    conn = sqlite3.connect(db_path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    service = CoreService.__new__(CoreService)
    service.db_path = db_path
    return service


def _legacy_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM client_feedback_events ORDER BY id").fetchall()
    finally:
        conn.close()


def test_feedback_mirrored_to_legacy_table() -> None:
    """新反馈写入 → 旧表出现同口径镜像行（字段映射 + 留痕链路完整）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p3b.db"
        service = _service(db_path)
        result = service.record_package_feedback(
            "recpkg_p3b",
            "interview",
            "客户反馈：约下周二一面，重点看固晶键合经验",
            feedback_time="2026-08-14 10:00:00",
            request_id="fb-1",
        )

        assert result["ok"] is True
        assert result["already_recorded"] is False
        assert result["client_feedback_event_id"]

        rows = _legacy_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == result["client_feedback_event_id"]
        # 候选人/客户/岗位口径：来自 job_candidates → people/jobs/clients 解析。
        assert row["job_candidate_id"] == 559
        assert row["candidate_id"] == 5501  # legacy candidates.id（source_candidate_id）
        assert row["candidate_name"] == "张三"
        assert row["candidate_company"] == "苏州敏芯"
        assert row["client"] == "长越科技"
        assert row["position"] == "机械高级工程师"
        # 反馈类型映射到旧写方口径：interview → interviewing。
        assert row["feedback_type"] == "interviewing"
        assert row["status_after"] == "interviewing"
        assert row["feedback_detail"] == "客户反馈：约下周二一面，重点看固晶键合经验"
        assert row["feedback_time"] == "2026-08-14 10:00:00"
        # 新表没有的字段留空 + 来源留痕；event_id 链回 candidate_events（raw_json 含 package_id）。
        assert row["reason_tags_json"] == "[]"
        assert row["next_action"] == ""
        assert row["source"] == "recommendation_package"
        assert row["event_id"] == result["event_id"]

        conn = sqlite3.connect(db_path)
        try:
            event = conn.execute(
                "SELECT raw_json FROM candidate_events WHERE id=?", (row["event_id"],)
            ).fetchone()
        finally:
            conn.close()
        assert "recpkg_p3b" in event[0]


def test_duplicate_request_not_double_written() -> None:
    """同 request_id 重复提交 → already_recorded，旧表仍只有一行。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p3b.db"
        service = _service(db_path)
        first = service.record_package_feedback(
            "recpkg_p3b", "approved", "客户认可，推进面试", request_id="fb-dup"
        )
        replay = service.record_package_feedback(
            "recpkg_p3b", "approved", "客户认可，推进面试", request_id="fb-dup"
        )

        assert first["already_recorded"] is False
        assert replay["already_recorded"] is True
        assert replay["feedback_id"] == first["feedback_id"]
        assert replay["client_feedback_event_id"] == first["client_feedback_event_id"]

        rows = _legacy_rows(db_path)
        assert len(rows) == 1


def test_legacy_report_queries_see_mirrored_feedback() -> None:
    """旧读方口径（generate_workflow_status_report 正/负反馈统计）直接读到镜像行。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p3b.db"
        service = _service(db_path)
        service.record_package_feedback("recpkg_p3b", "approved", "客户认可", request_id="fb-pos")
        service.record_package_feedback("recpkg_p3b", "rejected", "客户否决：经验不匹配", request_id="fb-neg")
        service.record_package_feedback("recpkg_p3b", "other", "客户随口一聊", request_id="fb-other")

        conn = sqlite3.connect(db_path)
        try:
            positive = conn.execute(POSITIVE_SQL).fetchone()[0]
            negative = conn.execute(NEGATIVE_SQL).fetchone()[0]
        finally:
            conn.close()

        # approved 计入正向、rejected 计入负向；other 不属于旧口径正/负集合，不计入。
        assert positive == 1
        assert negative == 1
        assert len(_legacy_rows(db_path)) == 3


def test_mirror_into_preexisting_legacy_table_without_governance_columns() -> None:
    """旧表已存在但没有 governance 扩展列（未跑过 a_system_workflow_governance）时，
    双写先幂等补列再写入；存量手工行（record_client_feedback 路径）不受影响。"""
    legacy_schema = SCHEMA + """
    CREATE TABLE client_feedback_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER,
        candidate_name TEXT NOT NULL,
        candidate_company TEXT,
        client TEXT,
        position TEXT,
        feedback_type TEXT NOT NULL,
        status_after TEXT,
        reason_tags_json TEXT DEFAULT '[]',
        feedback_detail TEXT,
        next_action TEXT,
        source TEXT DEFAULT 'manual',
        feedback_time TEXT DEFAULT (datetime('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    INSERT INTO client_feedback_events
      (candidate_id,candidate_name,client,position,feedback_type,status_after,source)
    VALUES (5501,'张三','长越科技','机械高级工程师','approved','client_approved','manual');
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p3b.db"
        service = _service(db_path, schema=legacy_schema)
        result = service.record_package_feedback(
            "recpkg_p3b", "hold", "客户暂缓，月底再议", request_id="fb-hold"
        )

        rows = _legacy_rows(db_path)
        assert len(rows) == 2
        manual, mirrored = rows
        assert manual["source"] == "manual"
        assert manual["job_candidate_id"] is None  # 存量行不回填
        assert mirrored["source"] == "recommendation_package"
        assert mirrored["job_candidate_id"] == 559
        assert mirrored["feedback_type"] == "hold"
        assert mirrored["status_after"] == "hold"
        assert mirrored["id"] == result["client_feedback_event_id"]
