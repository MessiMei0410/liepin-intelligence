"""浮窗候选人名单弹窗跨窗口同步的回归测试。

覆盖场景：React 端（/asa-app）执行停止/复核/推荐等操作后，
通过 /api/asa/floating/candidate-update 写入变更，
浮窗端（/asa-floating）轮询 /api/asa/floating/candidate-updates 刷新名单。

被测函数：
- liepin_workbench_server.record_floating_candidate_update
- liepin_workbench_server.drain_floating_candidate_updates
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import liepin_workbench_server as server  # noqa: E402


def test_record_and_drain_candidate_update():
    job_id = 137
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "stage": "H5 初筛不通过", "is_stopped": True})
    result = server.drain_floating_candidate_updates(job_id)
    assert result["ok"] is True
    assert result["job_id"] == job_id
    assert len(result["changes"]) == 1
    assert result["changes"][0]["job_candidate_id"] == 1
    assert result["changes"][0]["is_stopped"] is True
    assert result["changes"][0]["stage"] == "H5 初筛不通过"


def test_drain_with_since_filters_old_changes():
    job_id = 138
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": True})
    time.sleep(0.01)
    later = server.ASA_FLOATING_CANDIDATE_UPDATES[job_id][-1]["updated_at"]
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 2, "is_stopped": False})
    result = server.drain_floating_candidate_updates(job_id, since=later)
    assert len(result["changes"]) == 1
    assert result["changes"][0]["job_candidate_id"] == 2


def test_record_deduplicates_same_candidate_same_direction():
    job_id = 139
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": True, "stage": "H5 初筛不通过"})
    time.sleep(0.01)
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": True, "stage": "H5 方向不符"})
    result = server.drain_floating_candidate_updates(job_id)
    assert len(result["changes"]) == 1
    assert result["changes"][0]["stage"] == "H5 方向不符"


def test_drain_keeps_both_directions_for_same_candidate():
    job_id = 140
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": True})
    time.sleep(0.01)
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": False})
    result = server.drain_floating_candidate_updates(job_id)
    assert len(result["changes"]) == 2


def test_drain_cleans_expired_updates():
    job_id = 141
    server.ASA_FLOATING_CANDIDATE_UPDATE_TTL_SECONDS = 0
    try:
        server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "is_stopped": True})
        time.sleep(0.01)
        result = server.drain_floating_candidate_updates(job_id)
        assert result["changes"] == []
        assert job_id not in server.ASA_FLOATING_CANDIDATE_UPDATES
    finally:
        server.ASA_FLOATING_CANDIDATE_UPDATE_TTL_SECONDS = 300


def test_record_requires_valid_job_id():
    try:
        server.record_floating_candidate_update("not-a-number", {"job_candidate_id": 1, "is_stopped": True})
        assert False, "应抛出 ValueError"
    except ValueError as exc:
        assert "job_id" in str(exc)


def test_record_requires_valid_candidate_id():
    try:
        server.record_floating_candidate_update(142, {"job_candidate_id": "invalid", "is_stopped": True})
        assert False, "应抛出 ValueError"
    except ValueError as exc:
        assert "job_candidate_id" in str(exc)


def test_is_stopped_inferred_from_stage_stop_tokens():
    """口径对齐：上报方漏标 is_stopped 时，阶段文本命中停止词（与名单卡
    copilot_intent._STOP_TOKENS 同口径）即视为已停止，杜绝
    “H5 初筛不通过 标签 + is_stopped=False 留在可推进组”的矛盾态。"""
    job_id = 143
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "stage": "H5 最近寻访/初筛不通过"})
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 2, "stage": "H5 淘汰", "is_stopped": False})
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 3, "stage": "H5 screen_rejected"})
    result = server.drain_floating_candidate_updates(job_id)
    by_id = {item["job_candidate_id"]: item for item in result["changes"]}
    assert by_id[1]["is_stopped"] is True
    assert by_id[2]["is_stopped"] is True
    assert by_id[3]["is_stopped"] is True


def test_is_stopped_not_inferred_from_active_stage():
    """活跃阶段（含“最近寻访”）不误判为停止。"""
    job_id = 144
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 1, "stage": "S2 复核通过/待联系"})
    server.record_floating_candidate_update(job_id, {"job_candidate_id": 2, "stage": "H5 最近寻访", "is_stopped": False})
    result = server.drain_floating_candidate_updates(job_id)
    by_id = {item["job_candidate_id"]: item for item in result["changes"]}
    assert by_id[1]["is_stopped"] is False
    assert by_id[2]["is_stopped"] is False
