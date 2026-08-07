"""交付记分卡（delivery_scorecard）口径测试：合成最小库，逐指标断言计算口径、空态与样本量。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from asa_core.analytics import AnalyticsService


SCHEMA = """
CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, title TEXT NOT NULL,
    created_at TEXT, closed_at TEXT
);
CREATE TABLE job_candidates (id INTEGER PRIMARY KEY, job_id INTEGER, person_id INTEGER);
CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE agent_candidate_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, job_candidate_id INTEGER NOT NULL, is_current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE consultant_confirmed_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL, job_id INTEGER NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE TABLE recommendation_package_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL, feedback_type TEXT NOT NULL, feedback_time TEXT NOT NULL
);
CREATE TABLE candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL, event_type TEXT NOT NULL, event_status TEXT, event_time TEXT
);
CREATE TABLE agent_sourcing_funnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL DEFAULT 0, channel TEXT NOT NULL,
    query_count INTEGER NOT NULL DEFAULT 0, recall_count INTEGER NOT NULL DEFAULT 0,
    intake_new_count INTEGER NOT NULL DEFAULT 0, assessed_count INTEGER NOT NULL DEFAULT 0,
    high_score_count INTEGER NOT NULL DEFAULT 0,
    workflow_id TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE agent_goals (
    goal_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    context_type TEXT NOT NULL DEFAULT 'global', context_id INTEGER
);
CREATE TABLE agent_workflows (
    workflow_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL,
    status TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE agent_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL, artifact_type TEXT NOT NULL
);
CREATE TABLE agent_analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE, catalog_id TEXT NOT NULL, catalog_version TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '', scope_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
    supersedes_run_id TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT, export_path TEXT, expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "scorecard.db"
    conn = sqlite3.connect(target)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return target


@pytest.fixture()
def populated_db(db_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO clients (id,name) VALUES (1,'士兰微')")
        # 岗位 10：20 天关闭；岗位 11：10 天关闭；岗位 12：进行中（无 closed_at，不计入关闭周期）。
        conn.execute("INSERT INTO jobs (id,client_id,title,created_at,closed_at) VALUES (10,1,'电源专家','2026-06-01 09:00:00','2026-06-21 09:00:00')")
        conn.execute("INSERT INTO jobs (id,client_id,title,created_at,closed_at) VALUES (11,1,'市场经理','2026-06-01 09:00:00','2026-06-11 09:00:00')")
        conn.execute("INSERT INTO jobs (id,client_id,title,created_at,closed_at) VALUES (12,1,'开放岗位','2026-07-01 09:00:00',NULL)")
        conn.executemany(
            "INSERT INTO job_candidates (id,job_id,person_id) VALUES (?,?,?)",
            [(100, 10, 1000), (101, 10, 1001), (102, 11, 1002)],
        )
        conn.executemany(
            "INSERT INTO agent_runs (run_id,status) VALUES (?,?)",
            [("run-1", "completed"), ("run-2", "failed")],
        )
        # 已完成评估：jc100/jc101（job 10）；jc102 的 run 失败，不计入分母。
        conn.executemany(
            "INSERT INTO agent_candidate_assessments (run_id,job_candidate_id,is_current) VALUES (?,?,?)",
            [("run-1", 100, 1), ("run-1", 101, 1), ("run-1", 101, 0), ("run-2", 102, 1)],
        )
        conn.executemany(
            "INSERT INTO consultant_confirmed_recommendations (job_candidate_id,job_id,confirmed_at) VALUES (?,?,?)",
            [(100, 10, "2026-07-01 10:00:00"), (101, 10, "2026-07-02 10:00:00")],
        )
        # jc100：确认后出现推荐包 interview 反馈 → 计入面试转化。
        conn.execute(
            "INSERT INTO recommendation_package_feedback (job_candidate_id,feedback_type,feedback_time) VALUES (100,'interview','2026-07-03 09:00:00')"
        )
        # jc101：面试事件发生在确认之前（不计）+ 一条 rejected 反馈（不计）。
        conn.executemany(
            "INSERT INTO candidate_events (job_candidate_id,event_type,event_status,event_time) VALUES (?,?,?,?)",
            [
                (101, "client_feedback", "interviewing", "2026-07-01 09:00:00"),
                (101, "client_feedback", "rejected", "2026-07-05 09:00:00"),
            ],
        )
        # 渠道漏斗：liepin 归 job 10，xsaas 归 job 11。
        conn.executemany(
            """INSERT INTO agent_sourcing_funnel
               (job_id,channel,query_count,recall_count,intake_new_count,assessed_count,high_score_count)
               VALUES (?,?,?,?,?,?,?)""",
            [(10, "liepin", 3, 100, 10, 20, 5), (11, "xsaas", 2, 50, 5, 10, 1)],
        )
        conn.executemany(
            "INSERT INTO agent_goals (goal_id,title,context_type,context_id) VALUES (?,?,?,?)",
            [("g1", "第1轮寻访", "job", 10), ("g2", "第2轮寻访", "job", 11), ("g3", "旧寻访", "job", 11), ("g4", "非寻访", "global", None), ("g5", "漏斗寻访", "job", 11)],
        )
        conn.executemany(
            "INSERT INTO agent_workflows (workflow_id,goal_id,status,updated_at) VALUES (?,?,?,?)",
            [
                ("wf1", "g1", "completed", "2026-07-10 09:00:00"),
                ("wf2", "g2", "blocked", "2026-07-11 09:00:00"),
                ("wf3", "g3", "cancelled", "2026-07-12 09:00:00"),
                ("wf4", "g4", "completed", "2026-07-13 09:00:00"),
                ("wf5", "g5", "failed", "2026-07-14 09:00:00"),
            ],
        )
        # wf1：有策略 + 有复盘；wf2：有策略无复盘；wf3：已取消（非终局）；wf4：非寻访；wf5：靠漏斗记录认定为寻访。
        conn.executemany(
            "INSERT INTO agent_artifacts (workflow_id,artifact_type) VALUES (?,?)",
            [("wf1", "search_strategy"), ("wf1", "strategy_review"), ("wf2", "search_strategy"), ("wf3", "search_strategy")],
        )
        conn.execute("UPDATE agent_sourcing_funnel SET workflow_id='wf5' WHERE channel='xsaas'")
        conn.commit()
    finally:
        conn.close()
    return db_path


def run_scorecard(db_path: Path, scope: dict | None = None) -> dict:
    return AnalyticsService(db_path).create_run("delivery_scorecard", "交付表现如何？", scope or {})["result"]


def metric_map(result: dict) -> dict:
    return {metric["id"]: metric for metric in result["metrics"]}


def test_catalog_registers_delivery_scorecard(db_path: Path) -> None:
    catalog = AnalyticsService(db_path).catalog()
    item = next(entry for entry in catalog["items"] if entry["catalog_id"] == "delivery_scorecard")
    assert item["label"] == "交付记分卡"
    assert item["allowed_scope_fields"] == ["days", "job_id"]


def test_scorecard_computes_five_core_metrics(populated_db: Path) -> None:
    result = run_scorecard(populated_db)
    assert result["status"] == "completed"
    metrics = metric_map(result)

    # 1. 有效推荐率 = 确认推荐 2 / 已完成评估 2（run 失败的评估不计）。
    assert metrics["effective_recommendation_rate"]["value"] == 1.0
    assert metrics["effective_recommendation_rate"]["sample_size"] == 2
    # 2. 推荐至面试转化 = 1 / 2（确认前的面试事件与 rejected 反馈均不计）。
    assert metrics["recommendation_to_interview_rate"]["value"] == 0.5
    assert metrics["recommendation_to_interview_rate"]["sample_size"] == 2
    # 3. 渠道质量：入库率 15/150，高分率 6/30。
    assert metrics["channel_intake_rate"]["value"] == 0.1
    assert metrics["channel_intake_rate"]["sample_size"] == 150
    assert metrics["channel_high_score_rate"]["value"] == 0.2
    assert metrics["channel_high_score_rate"]["sample_size"] == 30
    # 4. 关闭周期：[10, 20] 天 → 中位数/平均 15 天，进行中的岗位不计。
    assert metrics["job_closure_days_median"]["value"] == 15.0
    assert metrics["job_closure_days_avg"]["value"] == 15.0
    assert metrics["job_closure_days_median"]["sample_size"] == 2
    # 5. 复盘完成率：终局寻访 wf1/wf2/wf5，仅 wf1 有复盘。
    assert metrics["strategy_review_completion_rate"]["value"] == round(1 / 3, 4)
    assert metrics["strategy_review_completion_rate"]["sample_size"] == 3

    for metric in metrics.values():
        assert metric["note"], "每项指标必须带中文口径说明"
        assert metric["sample_size"] is not None
    assert result["caveats"] == []


def test_scorecard_per_job_drilldown_rows(populated_db: Path) -> None:
    result = run_scorecard(populated_db)
    sections = {section["title"]: section for section in result["sections"]}

    job_rows = {row["id"]: row for row in sections["岗位推荐明细"]["rows"]}
    assert set(job_rows) == {10}
    assert job_rows[10]["assessed"] == 2
    assert job_rows[10]["confirmed"] == 2
    assert job_rows[10]["interviewed"] == 1
    assert job_rows[10]["recommendation_rate"] == 1.0
    assert job_rows[10]["interview_rate"] == 0.5

    channel_rows = {row["channel"]: row for row in sections["渠道质量"]["rows"]}
    assert channel_rows["liepin"]["intake_rate"] == 0.1
    assert channel_rows["xsaas"]["high_score_rate"] == 0.1

    closure_rows = sections["岗位关闭周期"]["rows"]
    assert {row["id"] for row in closure_rows} == {10, 11}
    assert sorted(row["closure_days"] for row in closure_rows) == [10.0, 20.0]

    review_rows = {row["workflow_id"]: row for row in sections["寻访工作流复盘"]["rows"]}
    assert set(review_rows) == {"wf1", "wf2", "wf5"}
    assert review_rows["wf1"]["review_state"] == "已复盘"
    assert review_rows["wf2"]["review_state"] == "未复盘"
    assert review_rows["wf5"]["review_state"] == "未复盘"

    job_refs = {ref["id"] for ref in result["references"] if ref["type"] == "job"}
    workflow_refs = {ref["id"] for ref in result["references"] if ref["type"] == "workflow"}
    assert job_refs == {10}
    assert workflow_refs == {"wf1", "wf2", "wf5"}


def test_scorecard_empty_db_returns_honest_nulls(db_path: Path) -> None:
    result = run_scorecard(db_path)
    assert result["status"] == "completed"
    metrics = metric_map(result)
    for metric_id in (
        "effective_recommendation_rate", "recommendation_to_interview_rate",
        "channel_intake_rate", "channel_high_score_rate",
        "job_closure_days_median", "job_closure_days_avg",
        "strategy_review_completion_rate",
    ):
        assert metrics[metric_id]["value"] is None, metric_id
        assert metrics[metric_id]["sample_size"] == 0, metric_id
    caveats = " ".join(result["caveats"])
    assert "样本量 0" in caveats
    assert "没有已关闭岗位" in caveats
    assert "数据不足" in result["headline"]
    assert all(not section["rows"] for section in result["sections"])


def test_scorecard_job_scope_filters_all_metrics(populated_db: Path) -> None:
    result = run_scorecard(populated_db, {"job_id": 10, "days": 30})
    metrics = metric_map(result)
    assert metrics["effective_recommendation_rate"]["value"] == 1.0
    assert metrics["recommendation_to_interview_rate"]["value"] == 0.5
    # 渠道只剩 job 10 的 liepin 行。
    assert metrics["channel_intake_rate"]["sample_size"] == 100
    assert metrics["channel_high_score_rate"]["value"] == 0.25
    # 关闭周期只剩 job 10 的 20 天。
    assert metrics["job_closure_days_median"]["value"] == 20.0
    assert metrics["job_closure_days_median"]["sample_size"] == 1
    # 复盘只看 context 指向 job 10 的工作流（wf1）。
    assert metrics["strategy_review_completion_rate"]["value"] == 1.0
    assert metrics["strategy_review_completion_rate"]["sample_size"] == 1
    sections = {section["title"]: section for section in result["sections"]}
    assert {row["id"] for row in sections["岗位推荐明细"]["rows"]} == {10}
    assert {row["workflow_id"] for row in sections["寻访工作流复盘"]["rows"]} == {"wf1"}


def test_scorecard_rejects_unauthorized_scope_fields(db_path: Path) -> None:
    with pytest.raises(ValueError, match="未授权字段"):
        AnalyticsService(db_path).create_run("delivery_scorecard", "", {"raw_sql": "DELETE FROM jobs"})
