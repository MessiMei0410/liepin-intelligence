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
    format_grade_card,
    format_grade_list,
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
            (4, '待补丁', 'MPS', '硬件专家-电源', '上海', '硕士', '12年'),
            (5, '系统丁', '某芯片公司', '系统工程师', '上海', '硕士', '10年'),
            (6, '邻接戊', '某电源公司', '电源工程师', '上海', '本科', '8年');
        INSERT INTO job_candidates VALUES
            (101, 142, 1, 'S1 新增寻访/待复核', '待复核', '201', '2026-08-17'),
            (102, 142, 2, 'S1 新增寻访/待复核', '待复核', '202', '2026-08-17'),
            (103, 142, 3, 'S1 新增寻访/待复核', '待复核', '203', '2026-08-17'),
            (104, 142, 4, 'S1 新增寻访/待复核', '待复核', '204', '2026-08-17'),
            (105, 142, 5, 'S1 新增寻访/待复核', '待复核', '205', '2026-08-17'),
            (106, 142, 6, 'S1 新增寻访/待复核', '待复核', '206', '2026-08-17');
        INSERT INTO candidate_profiles VALUES
            (1, 201, '电源甲', '台达', '电源专家', '硕士', '10年',
             '负责 VPD 垂直供电模块，多相 Buck/TLVR 控制建模，DrMOS 选型，使用 SIMPLIS 完成负载瞬态和环路稳定验证'),
            (2, 202, '机械乙', '上海微电子装备', '电源专家', '硕士', '10年',
             '光刻机机械结构设计，使用 Ansys 做有限元、模态和振动分析'),
            (3, 203, '通用电源丙', '某电源公司', '电源专家', '本科', '8年',
             '长期负责通用 AC/DC、LLC 和 PFC 电源开发'),
            (4, 205, '系统丁', '某芯片公司', '电源专家', '硕士', '10年',
             '参与 CPU/GPU 供电项目，了解 VPD、TLVR、多相 Buck 和 DrMOS'),
            (5, 206, '邻接戊', '某电源公司', '电源专家', '本科', '8年',
             '负责电力电子、电源硬件、磁件、热设计和可靠性工作');
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
    assert by_name["系统丁"]["grade"] == "C-弱"
    assert "当前职位缺少明确" in by_name["系统丁"]["reason"]
    assert by_name["邻接戊"]["grade"] == "C-弱"
    assert not by_name["邻接戊"]["grade"].startswith(("A", "B"))

    answer = format_grade_list(result)
    assert "可推进 1 人（仅 A/B 级）" in answer
    assert "C-弱 3 人" in answer
    assert "X-排除 1 人" in answer
    assert "电源甲" in answer
    assert "系统丁" not in answer
    assert "机械乙" not in answer

    _, card = format_grade_card(result, client="士兰微", job_title="电源专家", job_id=142)
    assert card["filter_mode"] == "grade_filter"
    assert [group["key"] for group in card["groups"]] == ["A-核心"]
    assert sum(len(group["candidates"]) for group in card["groups"]) == 1


def _mechanical_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "mechanical-filter.db"
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

        INSERT INTO clients VALUES (1, '长越科技');
        INSERT INTO jobs VALUES (137, 1, '机械高级工程师');
        INSERT INTO people VALUES
            (1, '运动台甲', '上海微电子装备', '机械设计工程师', '上海', '硕士', '10年'),
            (2, '零件乙', '某机床厂', '机械工程师', '苏州', '本科', '8年');
        INSERT INTO job_candidates VALUES
            (201, 137, 1, 'S1 新增寻访/待复核', '待复核', '301', '2026-08-19'),
            (202, 137, 2, 'S1 新增寻访/待复核', '待复核', '302', '2026-08-19');
        INSERT INTO candidate_profiles VALUES
            (1, 301, '运动台甲', '上海微电子装备', '机械设计工程师', '硕士', '10年',
             '主导六自由度超精密气浮运动台（工件台）整机设计，多轴联动对准台，微米级定位，使用 Ansys 做有限元、模态与热变形分析'),
            (2, 302, '零件乙', '某机床厂', '机械工程师', '本科', '8年',
             '负责机床防护罩与钣金结构设计');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def test_mechanical_six_dof_stage_counts_as_hard_evidence(tmp_path: Path) -> None:
    db_path = _mechanical_db(tmp_path)
    result = filter_job_candidates(str(db_path), 137, domain="mechanical")
    graded = {item["id"]: item for item in result["candidates"]}
    # 六自由度运动台经历计入硬证据并满足运动部件维度：A-核心（精密+半导体设备/运动部件+仿真全占）
    assert graded[201]["grade"] == "A-核心"
    assert any("运动台" in hit or "六自由度" in hit or "工件台" in hit for hit in graded[201]["hard_hits"])
    # 无证据者维持 D 级，不被新关键词误抬
    assert graded[202]["grade"] == "D-无证据"


def test_truncation_is_declared_in_result_and_answer(tmp_path: Path) -> None:
    """dogfood P1-3：旧默认 limit=200 静默截断，分级计数被当成全池口径
    （C-弱 180 报成 134）。截断必须在结果与回答文本里显式声明。"""
    db_path = _filter_db(tmp_path)
    result = filter_job_candidates(str(db_path), 142, client="士兰微", max_candidates=3)
    assert result["total"] == 6
    assert result["returned"] == 3
    assert result["truncated"] is True
    answer = format_grade_list(result)
    assert "数据口径" in answer
    assert "截断" in answer
    assert "3/6" in answer


def test_full_pool_result_declares_freshness_without_truncation(tmp_path: Path) -> None:
    result = filter_job_candidates(str(_filter_db(tmp_path)), 142, client="士兰微")
    assert result["truncated"] is False
    assert result["returned"] == result["total"]
    answer = format_grade_list(result)
    assert "数据口径" in answer
    assert "实时计算，分级覆盖全池" in answer


def test_tool_result_covers_full_pool_by_default_and_declares_as_of(tmp_path: Path) -> None:
    db_path = _filter_db(tmp_path)
    tool_result = execute_filter_candidates(str(db_path), 142)
    assert tool_result["success"] is True
    data = tool_result["data"]
    # 默认 limit 覆盖全池：summary 计数与 total 一致，可直接引用
    assert data["truncated"] is False
    assert data["data_as_of"]
    assert sum(data["summary"].values()) == data["total"]
    # 显式调小 limit 时给调用方（LLM）不可忽略的截断警告
    limited = execute_filter_candidates(str(db_path), 142, limit=2)
    assert limited["data"]["truncated"] is True
    assert "截断口径" in limited["data"]["note"]
