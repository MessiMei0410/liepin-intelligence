"""二期知识飞轮：company_calibration 核心公司校准测试。

覆盖：校准 CRUD/版本化幂等（服务层 + HTTP Idempotency-Key 重放）、待校准队列
（未校准优先/搜索/状态过滤）、进度指示、覆盖层合并钩子（calibrated 覆盖并标注
source=consultant_calibrated）、降级（DB 不可用/无表/无记录时完全保持现状）。
全部使用临时库（SOURCE_DB 备份副本）+ 临时 KB fixture（ASA_KNOWLEDGE_BASE_DIR
覆盖），绝不触碰真实知识库目录与生产 DB。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asa_core.app import create_app
from asa_core.company_calibration import CompanyCalibrationService
from a_system_agent import knowledge_base


SOURCE_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")

KB_GRAPH_FIXTURE = {
    "meta": {"version": "test"},
    "companies": {
        "杭州鲁滨逊测试技术有限公司": {
            "track": "测试量测设备｜测试/分选/AOI/量测",
            "business": "半导体测试机、分选机与后道检测设备研发制造",
            "categories": ["半导体设备"],
        },
        "苏州刻蚀先锋科技有限公司": {
            "track": "前道设备｜刻蚀/等离子",
            "business": "等离子刻蚀装备",
            "categories": ["半导体设备"],
        },
        "上海福尔摩斯量测仪器有限公司": {
            "track": "测试量测设备｜测试/分选/AOI/量测",
            "business": "AOI 光学量测仪器",
            "categories": ["精密仪器"],
        },
    },
}


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    target = tmp_path / "asa.db"
    source = sqlite3.connect(SOURCE_DB)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


@pytest.fixture()
def kb_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "kb"
    directory.mkdir()
    (directory / "kb_company_graph_jsj_v1.json").write_text(
        json.dumps(KB_GRAPH_FIXTURE, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("ASA_KNOWLEDGE_BASE_DIR", str(directory))
    return directory


def _submit(client: TestClient, key: str, **overrides) -> dict:
    body = {
        "request_id": f"ccal-{key}",
        "company_name": "杭州鲁滨逊测试技术有限公司",
        "status": "calibrated",
        "track": "后道测试设备",
        "product_lines": ["STS8200 测试机"],
        "skill_tags": ["测试机", "分选机"],
        "level_system": "P/M 双序列",
        "no_poach": True,
        "non_compete": False,
        "note": "顾问确认赛道与产品线",
    }
    body.update(overrides)
    response = client.post(
        "/api/v1/company-calibrations",
        headers={"Idempotency-Key": f"ccal-key-{key}"},
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_calibration_submit_versioned_and_content_idempotent(db_path: Path, kb_dir: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        first = _submit(client, "v1")
        assert first["changed"] is True
        assert first["version"] == 1
        assert first["status"] == "calibrated"
        assert first["calibration"]["no_poach"] is True
        assert first["calibration"]["product_lines"] == ["STS8200 测试机"]

        # 同内容不同 Idempotency-Key：服务层幂等，不 bump version。
        replay = _submit(client, "v1-again")
        assert replay["changed"] is False
        assert replay["version"] == 1

        # 内容变化：version 自增。
        updated = _submit(client, "v2", track="后道测试设备（含分选）")
        assert updated["changed"] is True
        assert updated["version"] == 2

        detail = client.get(f"/api/v1/company-calibrations/{first['company_key']}")
        assert detail.status_code == 200
        assert detail.json()["calibration"]["track"] == "后道测试设备（含分选）"
        assert detail.json()["calibration"]["version"] == 2

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM company_calibrations").fetchone()[0]
        finally:
            conn.close()
    assert count == 1, "同一家公司只有一条校准记录（company_key 唯一）"


def test_calibration_submit_http_idempotent_replay(db_path: Path, kb_dir: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        body = {
            "request_id": "ccal-replay",
            "company_name": "苏州刻蚀先锋科技有限公司",
            "status": "needs_review",
            "note": "赛道待与顾问确认",
        }
        first = client.post(
            "/api/v1/company-calibrations",
            headers={"Idempotency-Key": "ccal-replay-key"},
            json=body,
        )
        replay = client.post(
            "/api/v1/company-calibrations",
            headers={"Idempotency-Key": "ccal-replay-key"},
            json=body,
        )
        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM company_calibrations WHERE company_name='苏州刻蚀先锋科技有限公司'"
            ).fetchone()[0]
        finally:
            conn.close()
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["receipt"]["idempotent_replay"] is True
    assert count == 1


def test_calibration_alias_submit_and_validation(db_path: Path, kb_dir: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        # 规范化别名（去城市前缀/公司后缀）锚定同一图谱条目。
        alias = _submit(client, "alias", company_name="鲁滨逊测试")
        assert alias["company_name"] == "杭州鲁滨逊测试技术有限公司"
        assert alias["company_key"] == knowledge_base.normalize_client_name("杭州鲁滨逊测试技术有限公司")

        bad_status = client.post(
            "/api/v1/company-calibrations",
            headers={"Idempotency-Key": "ccal-bad-status"},
            json={"request_id": "ccal-bad-status", "company_name": "鲁滨逊测试", "status": "done"},
        )
        unknown = client.post(
            "/api/v1/company-calibrations",
            headers={"Idempotency-Key": "ccal-unknown"},
            json={"request_id": "ccal-unknown", "company_name": "某某快消公司"},
        )
        unknown_detail = client.get("/api/v1/company-calibrations/no-such-company")
    assert bad_status.status_code == 409
    assert "校准状态" in bad_status.json()["detail"]
    assert unknown.status_code == 404
    assert unknown_detail.status_code == 404


def test_calibration_queue_search_filter_and_progress(db_path: Path, kb_dir: Path) -> None:
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        queue = client.get("/api/v1/company-calibrations")
        assert queue.status_code == 200
        payload = queue.json()
        assert payload["total"] == 3
        assert all(item["status"] == "pending" for item in payload["items"]), "默认队列全部未校准"

        _submit(client, "queue", company_name="鲁滨逊测试")
        _submit(client, "queue-review", company_name="苏州刻蚀先锋科技有限公司", status="needs_review", track="", product_lines=[], skill_tags=[], level_system="", no_poach=False, note="待复核赛道")

        # 默认待办口径：未校准 + 待复核；已校准的鲁滨逊不再出现。
        after = client.get("/api/v1/company-calibrations").json()
        statuses = {item["company_name"]: item["status"] for item in after["items"]}
        assert "杭州鲁滨逊测试技术有限公司" not in statuses
        assert statuses["苏州刻蚀先锋科技有限公司"] == "needs_review"
        assert statuses["上海福尔摩斯量测仪器有限公司"] == "pending"

        # 已校准单选过滤 + 搜索（名称/赛道/主营业务）。
        calibrated = client.get("/api/v1/company-calibrations?status=calibrated").json()
        assert [item["company_name"] for item in calibrated["items"]] == ["杭州鲁滨逊测试技术有限公司"]
        searched = client.get("/api/v1/company-calibrations?status=all&q=刻蚀").json()
        assert [item["company_name"] for item in searched["items"]] == ["苏州刻蚀先锋科技有限公司"]
        bad_filter = client.get("/api/v1/company-calibrations?status=bogus")

        progress = client.get("/api/v1/company-calibrations/progress").json()
        assert progress["target"] == 50
        assert progress["calibrated"] == 1
        assert progress["needs_review"] == 1
        assert progress["total"] == 3
        assert progress["pending"] == 1
    assert bad_filter.status_code == 409


def test_calibration_overlay_merges_into_graph(db_path: Path, kb_dir: Path) -> None:
    """覆盖层合并：calibrated 记录优先用校准值并标注 source=consultant_calibrated；原图谱不变。"""
    with TestClient(create_app(db_path=db_path, start_legacy=False)) as client:
        _submit(client, "overlay")
        _submit(client, "overlay-review", company_name="苏州刻蚀先锋科技有限公司", status="needs_review", track="", product_lines=[], skill_tags=[], level_system="", no_poach=False, note="待复核")

    overlay, load_trace = knowledge_base.load_calibration_overlay(db_path)
    assert list(overlay) == [knowledge_base.normalize_client_name("杭州鲁滨逊测试技术有限公司")], (
        "仅 status=calibrated 进入覆盖层"
    )
    assert any("校准覆盖层 1 家" in line for line in load_trace)

    graph, _ = knowledge_base.load_company_graph(kb_dir)
    merged, merge_trace = knowledge_base.apply_calibration_overlay(graph, overlay)
    entry = merged["杭州鲁滨逊测试技术有限公司"]
    assert entry["track"] == "后道测试设备"
    assert entry["categories"] == ["测试机", "分选机"]
    assert entry["product_lines"] == ["STS8200 测试机"]
    assert entry["source"] == "consultant_calibrated"
    assert entry["calibration"]["no_poach"] is True
    assert entry["calibration"]["level_system"] == "P/M 双序列"
    assert entry["calibration"]["version"] == 1
    assert any("consultant_calibrated" in line for line in merge_trace)

    # 原始图谱（入参）不被修改；未校准条目无 source 标注。
    assert graph["杭州鲁滨逊测试技术有限公司"]["track"] == "测试量测设备｜测试/分选/AOI/量测"
    assert "source" not in graph["杭州鲁滨逊测试技术有限公司"]
    assert "source" not in merged["苏州刻蚀先锋科技有限公司"]

    # 策略消费侧：合并后 derive_graph_pool 命中带 consultant_calibrated；不合并保持 kb_graph。
    pool, _ = knowledge_base.derive_graph_pool(merged, query_text="测试机 分选机 后道检测")
    calibrated_hits = [item for item in pool if item["name"] == "杭州鲁滨逊测试技术有限公司"]
    assert calibrated_hits and calibrated_hits[0]["source"] == "consultant_calibrated"
    raw_pool, _ = knowledge_base.derive_graph_pool(graph, query_text="测试机 分选机 后道检测")
    assert all(item["source"] == "kb_graph" for item in raw_pool)


def test_calibration_overlay_degrades_without_db(db_path: Path, kb_dir: Path, tmp_path: Path) -> None:
    """降级口径：无 db_path/库文件缺失/无表/无记录 → 空覆盖层，图谱完全保持现状。"""
    graph, _ = knowledge_base.load_company_graph(kb_dir)

    overlay, trace = knowledge_base.load_calibration_overlay(tmp_path / "missing.db")
    assert overlay == {}
    assert any("校准库缺失" in line for line in trace)

    no_table_db = tmp_path / "empty.db"
    conn = sqlite3.connect(no_table_db)
    conn.execute("CREATE TABLE dummy(id INTEGER)")
    conn.commit()
    conn.close()
    overlay, trace = knowledge_base.load_calibration_overlay(no_table_db)
    assert overlay == {}
    assert any("无 company_calibrations 表" in line for line in trace)

    from asa_core.database import migrate

    # 迁移后（表已建、无记录）：空覆盖层 + 留痕。
    migrate(db_path, backup=False)
    overlay, trace = knowledge_base.load_calibration_overlay(db_path)
    assert overlay == {}
    assert any("无 calibrated 记录" in line for line in trace)

    merged, merge_trace = knowledge_base.apply_calibration_overlay(graph, overlay)
    assert merged == graph
    assert any("按原始名单处理" in line for line in merge_trace)

    # 未命中图谱的孤儿校准记录：留痕不新建图谱条目。
    service = CompanyCalibrationService(db_path)
    service.submit("鲁滨逊测试", status="calibrated", track="后道测试设备")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO company_calibrations (calibration_id,company_key,company_name,status) "
            "VALUES ('ccal_orphan','ghostco','幽灵公司','calibrated')"
        )
        conn.commit()
    finally:
        conn.close()
    overlay, _ = knowledge_base.load_calibration_overlay(db_path)
    merged, merge_trace = knowledge_base.apply_calibration_overlay(graph, overlay)
    assert "幽灵公司" not in merged
    assert any("不在公司图谱中" in line for line in merge_trace)
    assert merged["杭州鲁滨逊测试技术有限公司"]["source"] == "consultant_calibrated"
