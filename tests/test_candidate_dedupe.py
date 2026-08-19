"""候选人去重（护栏第 6 条机制）测试：合成库，CI 可运行。

覆盖：
- dedupe_scan（只读）：§6.4 口径聚类（姓氏+公司+职位三证据；公司括号后缀
  变体判同）、已停止成员标记与建议保留方、job_id 过滤、空结果。
- merge preflight：三证据通过与拒绝（409 中文 detail）、winner 已停止 409、
  winner==loser 409、关系不存在 404、diff 载荷。
- merge commit（走 #61 写确认链路）：token 未激活 409 且不消费、激活后写入
  （loser 按既有停止口径停止，stop_reason=duplicate_candidate，note 指向
  winner，事件落原行）、幂等 already_applied、审计落库、commit 时 winner
  已停止 409。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app

APP_HEADERS = {"User-Agent": "ASAApp/test-suite"}

SCHEMA = """
CREATE TABLE clients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, created_at TEXT);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL, title TEXT NOT NULL,
    status TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT, display_name TEXT NOT NULL, current_company TEXT,
    current_title TEXT, city TEXT, education TEXT, experience TEXT,
    fingerprint TEXT NOT NULL UNIQUE, created_at TEXT
);
CREATE TABLE job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, person_id INTEGER NOT NULL,
    raw_client TEXT, raw_position TEXT, raw_status TEXT, raw_stage TEXT, clean_stage TEXT,
    flow_bucket TEXT, clean_reason TEXT, recent_hunting INTEGER DEFAULT 0, search_date TEXT,
    updated_at TEXT, source_candidate_id TEXT, stop_reason TEXT
);
CREATE TABLE candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_candidate_id INTEGER, person_id INTEGER,
    job_id INTEGER, event_type TEXT NOT NULL, event_status TEXT, event_time TEXT,
    summary TEXT, raw_json TEXT DEFAULT '{}', source_table TEXT, source_id TEXT
);
CREATE TABLE source_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL, source_type TEXT NOT NULL,
    source_candidate_id TEXT, source_date TEXT, raw_status TEXT, raw_client TEXT,
    raw_position TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE candidates(
  id INT, name TEXT, company TEXT, title TEXT, education TEXT, experience TEXT,
  skills TEXT, level TEXT, city TEXT, client TEXT, position TEXT, search_date TEXT,
  status TEXT, notes TEXT, iteration INT, recommended_to_client TEXT, client_feedback TEXT,
  elimination_reason TEXT, anchor_candidate INT, created_at TEXT, updated_at TEXT,
  source TEXT, xsaas_id TEXT, talent_pool TEXT
);
CREATE TABLE positions(id INT, client TEXT, title TEXT);
"""

ACTIVE_STAGE = ("S1 新增寻访/待复核", "待复核", "search_shortlisted")
STOPPED_STAGE = ("H5 最近寻访/初筛不通过", "最近寻访", "screen_rejected")


def _person(conn: sqlite3.Connection, pid: int, name: str, company: str, title: str) -> None:
    conn.execute(
        "INSERT INTO people(id,display_name,current_company,current_title,fingerprint) VALUES (?,?,?,?,?)",
        (pid, name, company, title, f"{name}|{company}|{title}"),
    )


def _relation(
    conn: sqlite3.Connection, rid: int, person_id: int, job_id: int = 1,
    stage: tuple[str, str, str] = ACTIVE_STAGE, source_candidate_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO job_candidates(id,job_id,person_id,clean_stage,flow_bucket,raw_status,raw_stage,updated_at,source_candidate_id)
           VALUES (?,?,?,?,?,?,?,datetime('now','localtime'),?)""",
        (rid, job_id, person_id, stage[0], stage[1], stage[2], stage[0], source_candidate_id),
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "dedupe.db"
    conn = sqlite3.connect(target)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO clients(id,name) VALUES (1,'晶盛客户')")
    conn.execute("INSERT INTO jobs(id,client_id,title,status) VALUES (1,1,'机械工程师','open')")
    conn.execute("INSERT INTO jobs(id,client_id,title,status) VALUES (2,1,'工艺工程师','open')")
    # 疑似重复组：同姓 + 公司括号后缀变体 + 同职位（真实案例 969/546/1045 的复刻）。
    _person(conn, 960, "武先生", "晶盛机电（半导体、光伏设备）", "机械工程师")
    _person(conn, 540, "武斌", "晶盛机电", "机械工程师")
    _person(conn, 508, "武**", "晶盛机电（半导体、光伏设备）", "机械工程师")
    # 两个不同全名（同姓同公司同职位）：不可互证，不得聚为一组。
    _person(conn, 700, "陈航", "华虹半导体", "设备工程师")
    _person(conn, 701, "陈立", "华虹半导体", "设备工程师")
    # 同姓不同公司：不得聚组（job 2）。
    _person(conn, 800, "周明", "中芯国际", "工艺工程师")
    _person(conn, 801, "周华", "华虹半导体", "工艺工程师")
    _relation(conn, 969, 960, job_id=1)
    _relation(conn, 546, 540, job_id=1, source_candidate_id="41")
    _relation(conn, 1045, 508, job_id=1)
    _relation(conn, 2001, 700, job_id=1)
    _relation(conn, 2002, 701, job_id=1)
    _relation(conn, 3001, 800, job_id=2)
    _relation(conn, 3002, 801, job_id=2)
    conn.execute(
        "INSERT INTO candidates(id,name,company,title,status) VALUES (41,'武斌','晶盛机电','机械工程师','new')"
    )
    conn.execute(
        """INSERT INTO source_profiles(person_id,source_type,source_candidate_id,raw_json)
           VALUES (540,'liepin','res-540',?)""",
        (json.dumps({"full_text": "武斌 晶盛机电 机械工程师 五年经验 " * 30}, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()
    return target


def _client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path, start_legacy=False))


def _preflight(client: TestClient, winner_id: int, loser_id: int | None, action: str = "merge"):
    return client.post(
        "/api/v1/candidate-actions/preflight",
        json={
            "request_id": f"dd-preflight-{uuid.uuid4().hex[:8]}",
            "candidate_id": winner_id,
            "action": action,
            **({"loser_id": loser_id} if loser_id is not None else {}),
        },
    )


def _activate(client: TestClient, token: str):
    request_id = f"dd-activate-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/write-confirmations/activate",
        headers={"Idempotency-Key": request_id, **APP_HEADERS},
        json={"request_id": request_id, "preflight_token": token},
    )


def _commit(client: TestClient, winner_id: int, loser_id: int, token: str, note: str = ""):
    request_id = f"dd-commit-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/candidate-actions/commit",
        headers={"Idempotency-Key": request_id},
        json={
            "request_id": request_id,
            "candidate_id": winner_id,
            "action": "merge",
            "loser_id": loser_id,
            "preflight_token": token,
            "note": note,
        },
    )


def _merge_once(client: TestClient, winner_id: int, loser_id: int, note: str = ""):
    preflight = _preflight(client, winner_id, loser_id)
    assert preflight.status_code == 200, preflight.text
    token = preflight.json()["token"]
    assert _activate(client, token).status_code == 200
    return _commit(client, winner_id, loser_id, token, note=note), preflight.json()


# ------------------------------------------------------------------
# 只读扫描
# ------------------------------------------------------------------

def test_dedupe_scan_clusters_three_evidence_match(db_path: Path) -> None:
    with _client(db_path) as client:
        response = client.get("/api/v1/candidates/dedupe-scan")
        assert response.status_code == 200, response.text
        payload = response.json()
    assert payload["group_count"] == 1
    group = payload["groups"][0]
    assert {m["relation_id"] for m in group["members"]} == {969, 546, 1045}
    assert group["surname"] == "武"
    member = next(m for m in group["members"] if m["relation_id"] == 546)
    assert member["person_id"] == 540
    assert member["source_type"] == "liepin"
    assert member["is_stopped"] is False
    # 建议保留方：未停止的关系（本例全部活跃，取最近事件/id 最大者）。
    assert group["suggested_winner_id"] in {969, 546, 1045}


def test_dedupe_scan_marks_stopped_and_suggests_active_winner(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
        (*STOPPED_STAGE, 969),
    )
    conn.commit()
    conn.close()
    with _client(db_path) as client:
        payload = client.get("/api/v1/candidates/dedupe-scan").json()
    group = payload["groups"][0]
    stopped = {m["relation_id"] for m in group["members"] if m["is_stopped"]}
    assert stopped == {969}
    assert group["suggested_winner_id"] in {546, 1045}


def test_dedupe_scan_job_filter_and_empty_result(db_path: Path) -> None:
    with _client(db_path) as client:
        # job 2 的两条关系同姓不同公司 → 空结果。
        payload = client.get("/api/v1/candidates/dedupe-scan", params={"job_id": 2}).json()
        assert payload["group_count"] == 0
        assert payload["groups"] == []
        assert payload["job_id"] == 2
        # job 1 只剩武氏一组；两个不同全名（陈航/陈立）不聚组。
        payload = client.get("/api/v1/candidates/dedupe-scan", params={"job_id": 1}).json()
        assert payload["group_count"] == 1


# ------------------------------------------------------------------
# merge preflight（三证据 + diff）
# ------------------------------------------------------------------

def test_merge_preflight_returns_diff_and_token(db_path: Path) -> None:
    with _client(db_path) as client:
        response = _preflight(client, 969, 546)
        assert response.status_code == 200, response.text
        payload = response.json()
    assert payload["action"] == "merge"
    assert payload["token"]
    assert payload["candidate"]["id"] == 969
    assert payload["winner"]["name"] == "武先生"
    assert payload["loser"]["name"] == "武斌"
    fields = {item["field"] for item in payload["diff"]}
    assert {"name", "current_company", "current_title", "stage", "source_type", "person_id", "resume_excerpt"} <= fields
    resume = next(item for item in payload["diff"] if item["field"] == "resume_excerpt")
    assert resume["loser"].startswith("武斌 晶盛机电")
    assert len(resume["loser"]) <= 200
    assert payload["loser_already_stopped"] is False


def test_merge_preflight_rejects_insufficient_evidence(db_path: Path) -> None:
    with _client(db_path) as client:
        # 姓氏不匹配。
        response = _preflight(client, 969, 2001)
        assert response.status_code == 409
        assert "合并证据不足" in response.json()["detail"]
        # 同姓不同公司不同职位。
        response = _preflight(client, 3001, 3002)
        assert response.status_code == 409
        assert "公司" in response.json()["detail"]
        # 同姓同公司同职位但两个不同全名（不可互证）。
        response = _preflight(client, 2001, 2002)
        assert response.status_code == 409
        assert "姓名互证" in response.json()["detail"]


def test_merge_preflight_guards(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
        (*STOPPED_STAGE, 969),
    )
    conn.commit()
    conn.close()
    with _client(db_path) as client:
        # winner 已停止 → 409，不得往停止人选上合并。
        response = _preflight(client, 969, 546)
        assert response.status_code == 409
        assert "已停止" in response.json()["detail"]
        # winner == loser → 409。
        assert _preflight(client, 546, 546).status_code == 409
        # 缺 loser_id → 409。
        assert _preflight(client, 546, None).status_code == 409
        # 关系不存在 → 404。
        assert _preflight(client, 546, 999999).status_code == 404
        # loser 已停止 → preflight 放行（commit 幂等返回），并标注。
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
            (*STOPPED_STAGE, 1045),
        )
        conn.commit()
        conn.close()
        response = _preflight(client, 546, 1045)
        assert response.status_code == 200, response.text
        assert response.json()["loser_already_stopped"] is True


# ------------------------------------------------------------------
# merge commit（写确认链路 + 停止语义 + 幂等 + 审计）
# ------------------------------------------------------------------

def test_merge_commit_requires_activated_token(db_path: Path) -> None:
    with _client(db_path) as client:
        preflight = _preflight(client, 969, 546).json()
        blocked = _commit(client, 969, 546, preflight["token"])
        assert blocked.status_code == 409
        assert "confirmation_required" in blocked.json()["detail"]
        # 未激活拒绝不消费 token：激活后仍可写入。
        assert _activate(client, preflight["token"]).status_code == 200
        committed = _commit(client, 969, 546, preflight["token"])
        assert committed.status_code == 200, committed.text


def test_merge_commit_stops_loser_with_audit(db_path: Path) -> None:
    with _client(db_path) as client:
        committed, _ = _merge_once(client, 969, 546, note="确认同一人")
        assert committed.status_code == 200, committed.text
        payload = committed.json()
        assert payload["already_applied"] is False
        assert payload["stop_reason"] == "duplicate_candidate"
        assert payload["stop_reason_label"] == "重复人选"
    conn = sqlite3.connect(db_path)
    loser = conn.execute(
        "SELECT clean_stage,raw_status,stop_reason,clean_reason FROM job_candidates WHERE id=546"
    ).fetchone()
    assert loser[0] == "H5 最近寻访/初筛不通过"
    assert loser[1] == "screen_rejected"
    assert loser[2] == "duplicate_candidate"
    assert "关系 #969" in loser[3] and "确认同一人" in loser[3]
    # 事件落原行（loser），事件内容指向 winner。
    event = conn.execute(
        "SELECT event_type,event_status,raw_json FROM candidate_events WHERE job_candidate_id=546 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event[0] == "resume_review_completed" and event[1] == "stop"
    raw = json.loads(event[2])
    assert raw["action"] == "merge" and raw["merged_into"] == 969
    # legacy candidates 行同步停止口径。
    legacy = conn.execute("SELECT status,notes FROM candidates WHERE id=41").fetchone()
    assert legacy[0] == "screen_rejected" and "关系 #969" in legacy[1]
    # winner 行不动。
    winner = conn.execute("SELECT clean_stage,raw_status FROM job_candidates WHERE id=969").fetchone()
    assert winner[0] == ACTIVE_STAGE[0] and winner[1] == ACTIVE_STAGE[2]
    # 审计落库（idem 链路 candidate.commit 成功 + after_json）。
    audit = conn.execute(
        "SELECT result,target_id,after_json FROM audit_events WHERE operation='candidate.commit' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert audit[0] == "success" and audit[1] == "969"
    assert "duplicate_candidate" in audit[2]
    conn.close()


def test_merge_commit_idempotent_when_loser_already_stopped(db_path: Path) -> None:
    with _client(db_path) as client:
        first, _ = _merge_once(client, 969, 546)
        assert first.status_code == 200, first.text
        events_after_first = _event_count(db_path, 546)
        # 同一 loser 重复合并：走完整 preflight→activate→commit，幂等返回。
        second, preflight = _merge_once(client, 969, 546)
        assert preflight["loser_already_stopped"] is True
        assert second.status_code == 200, second.text
        assert second.json()["already_applied"] is True
        assert _event_count(db_path, 546) == events_after_first


def test_merge_commit_rejects_when_winner_stopped(db_path: Path) -> None:
    with _client(db_path) as client:
        preflight = _preflight(client, 969, 546).json()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE job_candidates SET clean_stage=?,flow_bucket=?,raw_status=? WHERE id=?",
            (*STOPPED_STAGE, 969),
        )
        conn.commit()
        conn.close()
        assert _activate(client, preflight["token"]).status_code == 200
        committed = _commit(client, 969, 546, preflight["token"])
        assert committed.status_code == 409
        assert "已停止" in committed.json()["detail"]


def _event_count(db_path: Path, relation_id: int) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT count(*) FROM candidate_events WHERE job_candidate_id=?", (relation_id,)
    ).fetchone()[0]
    conn.close()
    return count
