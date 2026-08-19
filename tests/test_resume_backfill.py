"""简历回填（#61 写确认链路 + 护栏第 9/12 条）测试：合成库，CI 可运行。

场景：用户在猎聘打开候选人详情页 → 扩展直推简历快照到桥接存储 →
asa_resume_backfill preflight（定位本地候选人 + 完整性守卫 + 新旧 diff）→
界面确认卡激活 token → commit 落库（复用 resume_persist 写入口径）。

覆盖：
- 定位：candidate_id 直给（身份互证）、resume_id 反查 source_profiles/
  entity_source_links；匹配不到 → 409「不在 ASA 库中」；多人 → 409 歧义；
  已登记猎聘档案 ID 冲突 → 409 禁止跨人回填；身份证据不匹配 → 409。
- 完整性守卫：partial 快照（全文过短/缺姓名/缺工作经历段）→ 409，不得回填。
- diff：新增/更新/无变化口径正确；完全一致 → unchanged 不发 token。
- 写确认链路：未激活 token commit → 409 confirmation_required（不消费 token）；
  激活后落库（source_profiles upsert、people 空字段回填、candidate_events
  resume_profile_captured、统一审计）；快照漂移（hash 变化）→ 409；
  同人同档案同内容重复 commit → already_applied，不重复写事件。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import liepin_workbench_server as bridge  # noqa: E402
from asa_core.app import create_app  # noqa: E402

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
CREATE TABLE candidate_profiles (
  id INTEGER PRIMARY KEY, candidate_id INTEGER, candidate_name TEXT, candidate_company TEXT,
  client TEXT, position TEXT, education_level TEXT, seniority TEXT,
  industry_tags_json TEXT, function_tags_json TEXT, risk_tags_json TEXT,
  profile_summary TEXT, updated_at TEXT
);
"""

ACTIVE_STAGE = ("S1 新增寻访/待复核", "待复核", "search_shortlisted")

FULL_TEXT = (
    "杜明 华虹半导体 设备工程师 在职考虑机会 "
    "工作经历 华虹半导体 设备工程师 负责刻蚀设备维护与工艺改进，"
    "主导 12 吋产线设备搬入与量产爬坡，熟悉 Lam/TEL 刻蚀平台 "
) * 12
WORK_TEXT = "华虹半导体 设备工程师 负责刻蚀设备维护与工艺改进，主导 12 吋产线设备搬入与量产爬坡 " * 6


def _snapshot(
    resume_id: str = "res-du-1",
    name: str = "杜明",
    company: str = "华虹半导体",
    title: str = "设备工程师",
    full_text: str = FULL_TEXT,
    work_text: str = WORK_TEXT,
    project_text: str = "",
    education_text: str = "",
    city: str = "上海",
    education: str = "本科",
    experience: str = "8年",
) -> dict:
    return {
        "surface": "liepin",
        "url": f"https://h.liepin.com/resume/showresumedetail/?res_id_encode={resume_id}",
        "captured_at": "2026-08-19T10:00:00",
        "instance_id": "tab-test",
        "resume": {
            "resume_id": resume_id,
            "name": name,
            "status": "在职，考虑机会",
            "company": company,
            "title": title,
            "city": city,
            "education": education,
            "experience": experience,
            "work_text": work_text,
            "project_text": project_text,
            "education_text": education_text,
            "full_text": full_text,
        },
    }


def _seed_snapshot(snapshot: dict | None = None) -> dict:
    payload = snapshot or _snapshot()
    result = bridge.update_resume_snapshot(payload)
    assert result["ok"]
    return payload


def _person(conn: sqlite3.Connection, pid: int, name: str, company: str, title: str, **extra: str) -> None:
    conn.execute(
        "INSERT INTO people(id,display_name,current_company,current_title,city,education,experience,fingerprint) VALUES (?,?,?,?,?,?,?,?)",
        (pid, name, company, title, extra.get("city", ""), extra.get("education", ""), extra.get("experience", ""), f"{pid}|{name}|{company}|{title}"),
    )


def _relation(conn: sqlite3.Connection, rid: int, person_id: int, job_id: int = 1, source_candidate_id: str | None = None) -> None:
    conn.execute(
        """INSERT INTO job_candidates(id,job_id,person_id,clean_stage,flow_bucket,raw_status,raw_stage,updated_at,source_candidate_id)
           VALUES (?,?,?,?,?,?,?,datetime('now','localtime'),?)""",
        (rid, job_id, person_id, ACTIVE_STAGE[0], ACTIVE_STAGE[1], ACTIVE_STAGE[2], ACTIVE_STAGE[0], source_candidate_id),
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "resume-backfill.db"
    conn = sqlite3.connect(target)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO clients(id,name) VALUES (1,'华虹客户')")
    conn.execute("INSERT INTO jobs(id,client_id,title,status) VALUES (1,1,'设备工程师','open')")
    # 主目标：杜明（关系 901），无已登记猎聘档案。
    _person(conn, 90, "杜明", "华虹半导体", "设备工程师")
    _relation(conn, 901, 90, source_candidate_id="51")
    conn.execute(
        "INSERT INTO candidates(id,name,company,title,status) VALUES (51,'杜明','华虹半导体','设备工程师','new')"
    )
    # resume_id 反查目标：已登记 source_profiles（关系 902）。
    _person(conn, 91, "钱峰", "中芯国际", "工艺工程师")
    _relation(conn, 902, 91)
    conn.execute(
        "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,raw_json) VALUES (91,'liepin','res-known-1',?)",
        (json.dumps({"name": "钱峰", "full_text": "钱峰 中芯国际 工艺工程师 旧档案 " * 40}, ensure_ascii=False),),
    )
    # 歧义组：两个 person 都登记 res-dup-1。
    _person(conn, 92, "孙一", "华虹半导体", "设备工程师")
    _person(conn, 93, "孙二", "华虹半导体", "设备工程师")
    _relation(conn, 903, 92)
    _relation(conn, 904, 93)
    for pid in (92, 93):
        conn.execute(
            "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,raw_json) VALUES (?,'liepin','res-dup-1','{}')",
            (pid,),
        )
    # 已登记冲突组：person 94 已登记 res-old-1（关系 905）。
    _person(conn, 94, "李雷", "长江存储", "设备工程师")
    _relation(conn, 905, 94)
    conn.execute(
        "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,raw_json) VALUES (94,'liepin','res-old-1','{}')"
    )
    # 档案一致组：person 95 已存与默认快照同内容的档案（关系 906），people 字段也已回填。
    _person(conn, 95, "杜明", "华虹半导体", "设备工程师", city="上海", education="本科", experience="8年")
    _relation(conn, 906, 95)
    same_as_snapshot = _snapshot()["resume"]
    conn.execute(
        "INSERT INTO source_profiles(person_id,source_type,source_candidate_id,raw_json) VALUES (95,'liepin','res-du-1',?)",
        (json.dumps(same_as_snapshot, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()
    return target


@pytest.fixture(autouse=True)
def _clear_snapshots():
    bridge.clear_resume_snapshots()
    yield
    bridge.clear_resume_snapshots()


def _preflight(client: TestClient, **payload):
    return client.post(
        "/api/v1/candidates/resume-backfill/preflight",
        json={"request_id": f"rb-preflight-{uuid.uuid4().hex[:8]}", **payload},
    )


def _activate(client: TestClient, token: str):
    request_id = f"rb-activate-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/write-confirmations/activate",
        headers={"Idempotency-Key": request_id, **APP_HEADERS},
        json={"request_id": request_id, "preflight_token": token},
    )


def _commit(client: TestClient, candidate_id: int, token: str):
    request_id = f"rb-commit-{uuid.uuid4().hex[:8]}"
    return client.post(
        "/api/v1/candidates/resume-backfill/commit",
        headers={"Idempotency-Key": request_id},
        json={"request_id": request_id, "candidate_id": candidate_id, "preflight_token": token},
    )


def test_preflight_requires_locator(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client)
        assert response.status_code == 400


def test_preflight_without_snapshot_409(db_path: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=901)
        assert response.status_code == 409
        assert "未读到当前页简历快照" in response.json()["detail"]


def test_preflight_locates_by_candidate_id_and_diffs(db_path: Path) -> None:
    _seed_snapshot()
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=901)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["token"]
        assert payload["action"] == "resume_backfill"
        assert payload["candidate"]["id"] == 901
        assert payload["candidate"]["name"] == "杜明"
        assert payload["resume"]["resume_id"] == "res-du-1"
        diff = {entry["field"]: entry for entry in payload["diff"]}
        # 本地档案为空：全文/工作经历段为新增；people 已有公司/职位 → 无变化。
        assert diff["full_text"]["change"] == "added"
        assert diff["work_text"]["change"] == "added"
        assert diff["current_company"]["change"] == "unchanged"
        assert diff["city"]["change"] == "added"


def test_preflight_locates_by_resume_id(db_path: Path) -> None:
    _seed_snapshot(_snapshot(resume_id="res-known-1", name="钱峰", company="中芯国际", title="工艺工程师",
                             full_text="钱峰 中芯国际 工艺工程师 工作经历 中芯国际 工艺工程师 负责扩散工艺 " * 30))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, resume_id="res-known-1")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["candidate"]["id"] == 902
        diff = {entry["field"]: entry for entry in payload["diff"]}
        # 已有旧档案全文：新快照更长 → updated。
        assert diff["full_text"]["change"] == "updated"


def test_preflight_unknown_resume_id_409_not_in_pool(db_path: Path) -> None:
    _seed_snapshot(_snapshot(resume_id="res-stranger-9", name="外人", company="未知公司", title="未知职位",
                             full_text="外人 未知公司 未知职位 工作经历 某处 某职 " * 40))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, resume_id="res-stranger-9")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "不在 ASA 库中" in detail
        assert "不会新建档案" in detail


def test_preflight_ambiguous_persons_409(db_path: Path) -> None:
    _seed_snapshot(_snapshot(resume_id="res-dup-1", name="孙一", company="华虹半导体", title="设备工程师"))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, resume_id="res-dup-1")
        assert response.status_code == 409
        assert "无法唯一定位" in response.json()["detail"]


def test_preflight_incomplete_snapshot_409(db_path: Path) -> None:
    _seed_snapshot(_snapshot(full_text="杜明 华虹半导体 设备工程师", work_text=""))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=901)
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "抓取不完整" in detail
        assert "partial" in detail


def test_preflight_identity_mismatch_409(db_path: Path) -> None:
    _seed_snapshot(_snapshot(name="完全不同的人", company="别的公司", title="别的职位",
                             full_text="完全不同的人 别的公司 别的职位 工作经历 某地 某岗 " * 35))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=901)
        assert response.status_code == 409
        assert "身份证据不匹配" in response.json()["detail"]


def test_preflight_known_liepin_id_conflict_409(db_path: Path) -> None:
    # person 94 已登记 res-old-1；当前页是 res-du-1 → 禁止跨人回填。
    _seed_snapshot(_snapshot(name="李雷", company="长江存储", title="设备工程师",
                             full_text="李雷 长江存储 设备工程师 工作经历 长江存储 设备工程师 " * 30))
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=905)
        assert response.status_code == 409
        assert "禁止跨人回填" in response.json()["detail"]


def test_preflight_unchanged_returns_no_token(db_path: Path) -> None:
    _seed_snapshot()
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        response = _preflight(client, candidate_id=906)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["unchanged"] is True
        assert "token" not in payload


def test_commit_rejects_unactivated_token_then_writes(db_path: Path) -> None:
    _seed_snapshot()
    app = create_app(db_path=db_path, start_legacy=False)
    with TestClient(app) as client:
        preflight = _preflight(client, candidate_id=901).json()
        blocked = _commit(client, 901, preflight["token"])
        assert blocked.status_code == 409
        assert "confirmation_required" in blocked.json()["detail"]

        # 未激活的拒绝不消费 token：激活后仍可写入。
        assert _activate(client, preflight["token"]).status_code == 200
        committed = _commit(client, 901, preflight["token"])
        assert committed.status_code == 200, committed.text
        result = committed.json()
        assert result["already_applied"] is False
        assert result["source_profile_id"]
        assert result["receipt"]["audit_event_id"]

    conn = sqlite3.connect(db_path)
    try:
        profile = conn.execute(
            "SELECT raw_json FROM source_profiles WHERE person_id=90 AND source_type='liepin' AND source_candidate_id='res-du-1'"
        ).fetchone()
        assert profile, "回填应写入 source_profiles"
        raw = json.loads(profile[0])
        assert "12 吋产线" in raw["full_text"]
        assert raw["capture_method"] == "asa_bridge_extension"
        event = conn.execute(
            "SELECT event_type,event_status FROM candidate_events WHERE job_candidate_id=901 AND event_type='resume_profile_captured'"
        ).fetchone()
        assert event == ("resume_profile_captured", "completed")
        person = conn.execute("SELECT city,education,experience FROM people WHERE id=90").fetchone()
        assert person == ("上海", "本科", "8年")
        summary_row = conn.execute(
            "SELECT profile_summary FROM candidate_profiles WHERE candidate_id=51"
        ).fetchone()
        assert summary_row and "12 吋产线" in summary_row[0]
        audit = conn.execute(
            "SELECT result FROM audit_events WHERE operation='resume_backfill.commit' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit and audit[0] == "success"
    finally:
        conn.close()


def test_commit_snapshot_drift_409(db_path: Path) -> None:
    _seed_snapshot()
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        preflight = _preflight(client, candidate_id=901).json()
        # 预检后页面内容变化（hash 漂移）→ commit 拒绝。
        _seed_snapshot(_snapshot(full_text=FULL_TEXT + "新增一段完全不同的经历。", work_text=WORK_TEXT + "新增段落。"))
        assert _activate(client, preflight["token"]).status_code == 200
        drifted = _commit(client, 901, preflight["token"])
        assert drifted.status_code == 409
        assert "已变化" in drifted.json()["detail"]


def test_commit_already_applied_is_idempotent(db_path: Path) -> None:
    snapshot = _seed_snapshot()
    app = create_app(db_path=db_path, start_legacy=False)
    with TestClient(app) as client:
        core = app.state.core
        from asa_core.service_resume_backfill import snapshot_content_hash

        content_hash = snapshot_content_hash(snapshot)
        token1, _ = core._mint_write_token((901, content_hash), "resume_backfill", activated=True)
        first = core.resume_backfill_commit(901, token1, snapshot=snapshot)
        assert first["already_applied"] is False
        token2, _ = core._mint_write_token((901, content_hash), "resume_backfill", activated=True)
        second = core.resume_backfill_commit(901, token2, snapshot=snapshot)
        assert second["already_applied"] is True
    conn = sqlite3.connect(db_path)
    try:
        events = conn.execute(
            "SELECT count(*) FROM candidate_events WHERE job_candidate_id=901 AND event_type='resume_profile_captured'"
        ).fetchone()
        assert events[0] == 1, "幂等命中不得重复写业务事件"
    finally:
        conn.close()
