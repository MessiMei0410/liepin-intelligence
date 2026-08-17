"""position_filter_models 桥：模型校验、规则引擎、草稿生成与确认链路。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.candidate_pool_filter import (  # noqa: E402
    UnsupportedFilterDomainError,
    filter_job_candidates,
)
from a_system_agent.filter_models import (  # noqa: E402
    FilterModelError,
    confirm_model,
    draft_from_position_profile,
    grade_with_model,
    load_model_for_job,
    upsert_model,
    validate_model,
)

TME_MODEL = {
    "layers": {
        "role": ["技术市场", "TME", "产品定义", "FAE"],
        "product": ["多相控制器", "DrMOS", "POL"],
        "support": ["客户导入", "技术宣讲"],
    },
    "layer_weights": {"role": 18, "product": 12, "support": 3},
    "excl_title": ["机械", "结构"],
    "rules": [
        {"grade": "X-排除", "title_any": ["销售"], "max": {"role": 0}, "reason": "纯销售角色"},
        {"grade": "A-核心", "min": {"role": 2, "product": 2, "support": 1}, "reason": "双轨证据充分"},
        {"grade": "A-强", "min": {"role": 1, "product": 2}, "reason": "双轨证据"},
        {"grade": "B-中", "min": {"role": 1, "product": 1}, "reason": "双轨各一项"},
        {"grade": "C-弱", "min": {"support": 1}, "reason": "仅支撑证据"},
        {"grade": "D-无证据", "min": {}, "reason": "无证据"},
    ],
}


def test_validate_model_normalizes_and_rejects_bad_models() -> None:
    clean = validate_model(TME_MODEL)
    assert clean["review_only_grade"] == "C-弱"
    assert clean["rules"][-1]["min"] == {}

    with pytest.raises(FilterModelError, match="layers"):
        validate_model({"rules": [{"grade": "D-无证据", "min": {}}]})
    with pytest.raises(FilterModelError, match="未定义层"):
        validate_model({
            "layers": {"direct": ["x"]},
            "rules": [{"grade": "A-核心", "min": {"ghost": 1}}, {"grade": "D-无证据", "min": {}}],
        })
    with pytest.raises(FilterModelError, match="兜底"):
        validate_model({
            "layers": {"direct": ["x"]},
            "rules": [{"grade": "A-核心", "min": {"direct": 1}}],
        })


def test_grade_with_model_rules_first_match_and_title_verdicts() -> None:
    model = validate_model(TME_MODEL)
    strong = grade_with_model(
        model,
        title_text="技术市场经理",
        txt="技术市场 tme 产品定义，多相控制器 drmos pol，客户导入",
        edu="硕士", exp_n=10,
    )
    assert strong["grade"] == "A-核心"
    assert set(strong["layer_hits"]) == {"role", "product", "support"}

    sales = grade_with_model(
        model, title_text="销售主管", txt="多相控制器 drmos 销售目标", edu="本科", exp_n=8,
    )
    assert sales["grade"] == "X-排除"
    assert "纯销售" in sales["reason"]

    excl = grade_with_model(model, title_text="机械工程师", txt="多相控制器", edu="", exp_n=None)
    assert excl["grade"] == "X-排除"

    none = grade_with_model(model, title_text="电源工程师", txt="常规硬件开发", edu="", exp_n=None)
    assert none["grade"] == "D-无证据"

    review_only = validate_model({
        "layers": {"direct": ["vpd"]},
        "review_only_title": ["FAE"],
        "review_only_grade": "C-弱",
        "review_only_reason": "FAE 需核验",
        "rules": [{"grade": "B-中", "min": {"direct": 1}}, {"grade": "D-无证据", "min": {}}],
    })
    fae = grade_with_model(review_only, title_text="fae 工程师", txt="vpd 支持", edu="", exp_n=None)
    assert fae["grade"] == "C-弱" and fae["reason"] == "FAE 需核验"


def _bridge_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "bridge.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, client_id INTEGER, title TEXT);
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
        CREATE TABLE position_profiles (
            id INTEGER PRIMARY KEY, client TEXT, position TEXT,
            hard_requirements_json TEXT, ability_keywords_json TEXT,
            exclusion_tags_json TEXT, updated_at TEXT
        );
        INSERT INTO clients VALUES (1, '士兰微');
        INSERT INTO jobs VALUES (201, 1, '磁集成工艺专家');
        INSERT INTO people VALUES
            (1, '工艺甲', '某磁件厂', '磁集成工艺专家', '杭州', '硕士', '10年'),
            (2, '路人乙', '某公司', '行政主管', '杭州', '本科', '8年');
        INSERT INTO job_candidates VALUES
            (301, 201, 1, 'S1 新增寻访/待复核', '待复核', '501', '2026-08-17'),
            (302, 201, 2, 'S1 新增寻访/待复核', '待复核', '502', '2026-08-17');
        INSERT INTO candidate_profiles VALUES
            (1, 501, '工艺甲', '某磁件厂', '磁集成工艺专家', '硕士', '10年',
             '负责磁集成与一体成型电感工艺开发，主导磁件可靠性验证'),
            (2, 502, '路人乙', '某公司', '行政主管', '本科', '8年', '负责行政后勤管理');
        INSERT INTO position_profiles VALUES
            (1, '士兰微', '磁集成工艺专家',
             '["磁集成", "一体成型电感", "具备十年以上磁件工艺开发与量产导入经验"]',
             '["磁件可靠性", "量产导入"]', '["销售", "行政"]', '2026-08-17');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_filter_fails_closed_until_model_confirmed(tmp_path: Path) -> None:
    db_path = _bridge_db(tmp_path)
    with pytest.raises(UnsupportedFilterDomainError):
        filter_job_candidates(str(db_path), 201, client="士兰微")

    # draft 不参与筛选：依旧失败关闭
    model_id = upsert_model(
        str(db_path), job_id=201, client="士兰微", position="磁集成工艺专家",
        domain="magnetics_process",
        model={
            "layers": {"direct": ["磁集成", "一体成型电感"], "support": ["磁件可靠性", "量产导入"]},
            "layer_weights": {"direct": 20, "support": 8},
            "excl_title": ["行政"],
            "rules": [
                {"grade": "A-核心", "min": {"direct": 2, "support": 1}},
                {"grade": "B-中", "min": {"direct": 1}},
                {"grade": "D-无证据", "min": {}},
            ],
        },
        status="draft", source="profile_draft",
    )
    with pytest.raises(UnsupportedFilterDomainError):
        filter_job_candidates(str(db_path), 201, client="士兰微")

    confirm_model(str(db_path), model_id, confirmed_by="kimi")
    result = filter_job_candidates(str(db_path), 201, client="士兰微")
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["工艺甲"]["grade"] == "A-核心"
    assert "磁集成" in by_name["工艺甲"]["hard_hits"]
    assert by_name["路人乙"]["grade"] == "X-排除"


def test_job_level_model_beats_domain_level(tmp_path: Path) -> None:
    db_path = _bridge_db(tmp_path)
    base = {
        "layers": {"direct": ["磁集成"]},
        "rules": [{"grade": "B-中", "min": {"direct": 1}}, {"grade": "D-无证据", "min": {}}],
    }
    upsert_model(str(db_path), job_id=None, client="", position="", domain="magnetics_process",
                 model=base, status="confirmed", source="manual")
    upsert_model(str(db_path), job_id=201, client="士兰微", position="磁集成工艺专家",
                 domain="magnetics_process",
                 model={**base, "layers": {"direct": ["一体成型电感"]}},
                 status="confirmed", source="manual")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        entry = load_model_for_job(conn, 201, None)
    finally:
        conn.close()
    assert entry is not None
    assert entry["model"]["layers"]["direct"] == ["一体成型电感"]


def test_draft_from_position_profile_maps_layers(tmp_path: Path) -> None:
    db_path = _bridge_db(tmp_path)
    draft = draft_from_position_profile(str(db_path), 201)
    assert draft["client"] == "士兰微"
    assert draft["model"]["layers"]["direct"] == ["磁集成", "一体成型电感"]
    assert draft["model"]["layers"]["support"] == ["磁件可靠性", "量产导入"]
    assert draft["model"]["excl_title"] == ["销售", "行政"]
    assert "具备十年以上磁件工艺开发与量产导入经验" in draft["note"]
    assert draft["model"]["rules"][-1]["grade"] == "D-无证据"

    model_id = upsert_model(
        str(db_path), job_id=draft["job_id"], client=draft["client"], position=draft["position"],
        domain=draft["domain"], model=draft["model"], status="draft", source="profile_draft",
        note=draft["note"],
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert load_model_for_job(conn, 201, None, status="draft") is not None
        assert load_model_for_job(conn, 201, None) is None  # draft 不进 confirmed 通道
    finally:
        conn.close()
    confirm_model(str(db_path), model_id, confirmed_by="reviewer")
    result = filter_job_candidates(str(db_path), 201, client="士兰微")
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["工艺甲"]["grade"] in ("A-核心", "B-中")
