"""工作台五分组（待判断/运行中/待客户/风险逾期/最近交付）的分组规则与空态。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from a_system_agent.service import AgentService
from asa_core.analytics import AnalyticsService, _flow_item_lane
from asa_core.app import create_app

SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")
LANES = ("decision", "running", "waiting_client", "risk", "delivered")


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("workbench-lanes") / "asa-workbench-lanes.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    # 生产库副本里全部人岗关系都有评估记录，_pick_plain_candidate 需要"无评估、
    # 未停止"的人岗关系来改造：拷贝后清空评估表，保证可改造候选确定存在。
    conn = sqlite3.connect(target)
    try:
        conn.execute("DELETE FROM agent_candidate_assessments")
        conn.commit()
    finally:
        conn.close()
    return target


def _pick_plain_candidate(conn: sqlite3.Connection, exclude: set[int]) -> int:
    """选一个无评估记录、未停止的人岗关系；其历史事件由调用方清除后再改造。"""
    marks = ",".join(str(value) for value in exclude) or "-1"
    row = conn.execute(
        f"""SELECT jc.id FROM job_candidates jc
            WHERE jc.id NOT IN ({marks})
              AND COALESCE(jc.clean_stage,'') NOT LIKE 'H5 %'
              AND NOT EXISTS(SELECT 1 FROM agent_candidate_assessments a WHERE a.job_candidate_id=jc.id)
            ORDER BY jc.id LIMIT 1"""
    ).fetchone()
    assert row, "fixture 库缺少可改造的人岗关系"
    return int(row[0])


def test_flow_item_lane_mapping_rules() -> None:
    for queue in ("待复核", "待核验", "待联系", "已回复"):
        assert _flow_item_lane({"queue": queue}) == "decision"
    for queue in ("超时", "异常"):
        assert _flow_item_lane({"queue": queue}) == "risk"
    assert _flow_item_lane({"queue": "进行中", "clean_stage": "S4 已推荐/待客户反馈"}) == "waiting_client"
    assert _flow_item_lane({"queue": "进行中", "last_event_summary": "客户报告已发，待客户确认"}) == "waiting_client"
    # 普通进行中、无客户等待信号的人选对不进工作台。
    assert _flow_item_lane({"queue": "进行中", "clean_stage": "S3 已联系/待回复"}) == ""
    # 风险优先于客户等待信号。
    assert _flow_item_lane({"queue": "超时", "clean_stage": "S4 已推荐/待客户反馈"}) == "risk"


def test_workbench_groups_into_five_lanes(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        used: set[int] = set()
        waiting_id = _pick_plain_candidate(conn, used)
        used.add(waiting_id)
        risk_id = _pick_plain_candidate(conn, used)
        conn.execute(
            f"DELETE FROM candidate_events WHERE job_candidate_id IN ({waiting_id},{risk_id})"
        )
        # 待客户：已推荐待客户反馈，且无其他事件 → queue 推导为「进行中」，由阶段信号进 waiting_client。
        conn.execute("UPDATE job_candidates SET clean_stage='S4 已推荐/待客户反馈' WHERE id=?", (waiting_id,))
        # 风险/逾期：最近事件含异常信号 → queue「异常」。
        conn.execute(
            """INSERT INTO candidate_events(job_candidate_id,event_type,event_status,event_time,summary)
               VALUES (?,'candidate_locate_failed','failed',datetime('now','localtime'),'候选人未唯一定位，存在异常')""",
            (risk_id,),
        )
        # 待判断（审批）+ 运行中（工作流）。注意：AgentService 启动会把 running 工作流
        # 恢复性置为 paused（workflow._recover_interrupted），所以这里用 waiting_external。
        conn.execute("INSERT INTO agent_goals(goal_id,objective,title,status) VALUES ('g-wb-1','推进电源专家寻访','电源专家寻访','active')")
        conn.execute(
            "INSERT INTO agent_workflows(workflow_id,goal_id,current_stage,status) VALUES ('wf-wb-1','g-wb-1','执行多渠道寻访','waiting_external')"
        )
        conn.execute(
            """INSERT INTO agent_approvals(approval_id,goal_id,workflow_id,step_id,action_type,risk_level,title,status)
               VALUES ('ap-wb-1','g-wb-1','wf-wb-1',1,'liepin_outreach','R3','批准多渠道寻访','pending')"""
        )
        conn.commit()
    finally:
        conn.close()

    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = client.get("/api/v1/workbench?limit=300")
        assert response.status_code == 200, response.json()
        data = response.json()

    summary = data["summary"]
    for lane in LANES:
        assert lane in summary, f"summary 缺少 lane：{lane}"
    assert summary["total"] >= len(data["items"])
    # 兼容别名：既有调用方读的 pending 即「待判断」。
    assert summary["pending"] == summary["decision"]
    assert {item["lane"] for item in data["items"]} <= set(LANES)

    by_key = {item["item_key"]: item for item in data["items"]}
    assert by_key[f"candidate:{waiting_id}"]["lane"] == "waiting_client"
    assert by_key[f"candidate:{risk_id}"]["lane"] == "risk"
    assert by_key["approval:ap-wb-1"]["lane"] == "decision"
    assert by_key["workflow:wf-wb-1"]["lane"] == "running"
    # 待复核类 inbox 项进入待判断。
    assert any(item["lane"] == "decision" and item["status_label"] == "待复核" for item in data["items"])
    assert summary["waiting_client"] >= 1
    assert summary["risk"] >= 1
    assert summary["decision"] >= 2


def test_workbench_empty_inbox_returns_empty_lanes_not_error(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    AgentService(db)  # 初始化即建 schema
    result = AnalyticsService(db).workbench({"ok": True, "items": []})
    assert result["ok"] is True
    assert result["items"] == []
    assert result["returned_count"] == 0
    assert result["truncated"] is False
    assert result["summary"] == {
        "decision": 0, "running": 0, "waiting_client": 0, "risk": 0, "delivered": 0,
        "pending": 0, "total": 0,
    }
