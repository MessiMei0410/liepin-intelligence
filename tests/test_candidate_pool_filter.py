from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from a_system_agent.candidate_pool_filter import (  # noqa: E402
    UnsupportedFilterDomainError,
    filter_job_candidates,
    job_filter_domain,
)
from a_system_agent.copilot_tools import execute_filter_candidates  # noqa: E402


def _filter_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "candidate-filter.db"
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

        INSERT INTO clients VALUES (1, '士兰微');
        INSERT INTO jobs VALUES (142, 1, '电源专家');
        INSERT INTO jobs VALUES (143, 1, '技术支持经理');
        INSERT INTO people VALUES
            (1, '电源甲', '台达', '电源工程师', '上海', '硕士', '10年'),
            (2, '机械乙', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年'),
            (3, '通用电源丙', '某电源公司', '电源研发工程师', '深圳', '本科', '8年'),
            (4, '待补丁', 'MPS', '硬件专家-电源', '上海', '硕士', '12年');
        INSERT INTO job_candidates VALUES
            (101, 142, 1, 'S1 新增寻访/待复核', '待复核', '201', '2026-08-17'),
            (102, 142, 2, 'S1 新增寻访/待复核', '待复核', '202', '2026-08-17'),
            (103, 142, 3, 'S1 新增寻访/待复核', '待复核', '203', '2026-08-17'),
            (104, 142, 4, 'S1 新增寻访/待复核', '待复核', '204', '2026-08-17');
        INSERT INTO candidate_profiles VALUES
            (1, 201, '电源甲', '台达', '电源专家', '硕士', '10年',
             '负责 VPD 垂直供电模块，多相 Buck/TLVR 控制建模，DrMOS 选型，使用 SIMPLIS 完成负载瞬态和环路稳定验证'),
            (2, 202, '机械乙', '上海微电子装备', '电源专家', '硕士', '10年',
             '光刻机机械结构设计，使用 Ansys 做有限元、模态和振动分析'),
            (3, 203, '通用电源丙', '某电源公司', '电源专家', '本科', '8年',
             '长期负责通用 AC/DC、LLC 和 PFC 电源开发');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_power_domain_is_explicit_and_unknown_domain_fails_closed(tmp_path: Path) -> None:
    db_path = _filter_db(tmp_path)

    assert job_filter_domain("电源专家") == "power"
    assert job_filter_domain("VPD/VRM 电源专家") == "power"
    assert job_filter_domain("ACDC服务器电源研发总监") is None
    assert job_filter_domain("技术市场经理（三次电源/服务器或PC市场）") is None
    assert job_filter_domain("技术支持经理") is None

    with pytest.raises(UnsupportedFilterDomainError, match="拒绝套用其他岗位规则"):
        filter_job_candidates(str(db_path), 143)

    tool_result = execute_filter_candidates(str(db_path), 143)
    assert tool_result["success"] is False
    assert "拒绝套用其他岗位规则" in tool_result["error"]


def test_power_filter_requires_direct_power_evidence_for_a_or_b(tmp_path: Path) -> None:
    result = filter_job_candidates(str(_filter_db(tmp_path)), 142, client="士兰微")
    by_name = {item["name"]: item for item in result["candidates"]}

    assert by_name["电源甲"]["grade"].startswith("A")
    assert {"VPD", "TLVR", "DrMOS"}.issubset(set(by_name["电源甲"]["hard_hits"]))
    assert by_name["机械乙"]["grade"] == "X-排除"
    assert not by_name["机械乙"]["grade"].startswith(("A", "B"))
    assert not by_name["通用电源丙"]["grade"].startswith(("A", "B"))
    assert by_name["待补丁"]["grade"] == "U-待补画像"
