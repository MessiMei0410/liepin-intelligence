"""上下文选举与 Copilot 焦点冲突修复的回归测试。

覆盖 2026-07-22 修复的缺陷：陈旧的 a_system explicit 点击长期压住
新鲜浏览器候选人页面，以及 Copilot 焦点在页面候选人切换后仍钉住旧候选人。
被测函数：
- liepin_workbench_server.select_floating_active_context / floating_context_stale_after
- a_system_agent.service.AgentService._copilot_context_from_focus / _persist_copilot_focus
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from _local import env_path, fixture_base_db, require_local

import pytest

import liepin_workbench_server as legacy
from a_system_agent.service import AgentService


SOURCE_DB = env_path("ASA_SOURCE_DB", Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"))
NOW = datetime(2026, 7, 22, 15, 0, 0)


def _context(surface: str, *, age_seconds: float, **extra: object) -> dict:
    payload = {
        "surface": surface,
        "updated_at": (NOW - timedelta(seconds=age_seconds)).isoformat(timespec="seconds"),
    }
    payload.update(extra)
    return payload


def _browser_window_context(age_seconds: float = 5) -> dict:
    return _context(
        "native",
        age_seconds=age_seconds,
        frontmost_app={"bundle_id": "com.google.chrome", "name": "Google Chrome"},
        window={"title": "候选人主页 - 猎聘"},
    )


# ---------------------------------------------------------------------------
# 修复 1：上下文选举降权
# ---------------------------------------------------------------------------


def test_asystem_explicit_stale_after_reduced_to_120_seconds() -> None:
    explicit = _context("a_system", age_seconds=0, explicit=True, trigger="click")
    assert legacy.floating_context_stale_after(explicit) == 120
    # 其他 surface 的保鲜期逻辑保持不变
    assert legacy.floating_context_stale_after(_context("a_system", age_seconds=0)) == 20
    assert legacy.floating_context_stale_after(_context("liepin", age_seconds=0, page_visible=True)) == 20
    assert (
        legacy.floating_context_stale_after(_context("native", age_seconds=0, trigger="hotkey"))
        == 180
    )


def test_fresh_liepin_page_beats_recent_asystem_explicit_click() -> None:
    contexts = {
        "a_system:asa": _context(
            "a_system",
            age_seconds=100,  # 120 秒保鲜期内、未过期
            explicit=True,
            trigger="click",
            page_visible=True,
            context={"type": "candidate", "id": 558},
        ),
        "liepin:ext": _context(
            "liepin",
            age_seconds=5,
            page_visible=True,
            context={"type": "candidate", "id": 9001},
        ),
        "native:mac": _browser_window_context(age_seconds=5),
    }
    winner = legacy.select_floating_active_context(contexts, NOW)
    assert winner is not None
    assert legacy.normalize_bridge_surface(winner.get("surface")) == "liepin"


def test_asystem_explicit_click_older_than_120s_is_stale_and_loses() -> None:
    stale_click = _context(
        "a_system",
        age_seconds=130,
        explicit=True,
        trigger="click",
        page_visible=True,
        context={"type": "candidate", "id": 558},
    )
    assert legacy.floating_context_age(NOW, stale_click) > legacy.floating_context_stale_after(stale_click)
    contexts = {
        "a_system:asa": stale_click,
        "liepin:ext": _context("liepin", age_seconds=5, page_visible=True, context={"type": "candidate", "id": 9001}),
        "native:mac": _browser_window_context(age_seconds=5),
    }
    winner = legacy.select_floating_active_context(contexts, NOW)
    assert winner is not None
    assert legacy.normalize_bridge_surface(winner.get("surface")) == "liepin"


def test_fresh_asystem_explicit_click_still_wins_without_browser_bridge() -> None:
    # 防过度修正：没有新鲜浏览器页面时，新鲜的 a_system explicit 点击仍应获胜。
    contexts = {
        "a_system:asa": _context(
            "a_system",
            age_seconds=30,
            explicit=True,
            trigger="click",
            page_visible=True,
            context={"type": "candidate", "id": 558},
        ),
        "liepin:ext": _context("liepin", age_seconds=500, page_visible=True),  # 早已过期
    }
    winner = legacy.select_floating_active_context(contexts, NOW)
    assert winner is not None
    assert legacy.normalize_bridge_surface(winner.get("surface")) == "a_system"


# ---------------------------------------------------------------------------
# 修复 2：Copilot 焦点候选人冲突检测
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    # 模块级共享副本：每个测试用独立 session_id 写入焦点，互不冲突。
    target = tmp_path_factory.mktemp("floating-context") / "asa.db"
    require_local(SOURCE_DB, "正式库 talent_system_v3")
    source = sqlite3.connect(fixture_base_db())
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    svc = AgentService(target, max_workers=1)
    try:
        yield svc
    finally:
        svc.close()


@pytest.fixture()
def candidate_ids(service: AgentService) -> tuple[int, int, int]:
    conn = sqlite3.connect(str(service.db_path))
    try:
        rows = conn.execute("SELECT id FROM job_candidates ORDER BY id LIMIT 2").fetchall()
        max_id = conn.execute("SELECT MAX(id) FROM job_candidates").fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == 2 and rows[0][0] != rows[1][0]
    return int(rows[0][0]), int(rows[1][0]), int(max_id) + 100000


def _selected(candidate_id: int | None) -> dict:
    if candidate_id is None:
        return {"type": "global", "id": None, "page": "", "filters": {}}
    return {"type": "candidate", "id": candidate_id, "page": "", "filters": {}}


def _pin_focus(service: AgentService, session_id: str, candidate_id: int) -> dict:
    focus = service._persist_copilot_focus(session_id, "同步当前候选人页面", _selected(candidate_id))
    assert int((focus.get("candidate") or {}).get("id") or 0) == candidate_id
    assert float(focus.get("confidence") or 0) == 1.0
    return focus


def test_focus_switches_to_new_page_candidate_when_resolvable(
    service: AgentService, candidate_ids: tuple[int, int, int]
) -> None:
    candidate_a, candidate_b, _ = candidate_ids
    session_id = f"floating-focus-switch-{uuid.uuid4().hex[:8]}"
    _pin_focus(service, session_id, candidate_a)
    focus = service._persist_copilot_focus(session_id, "同步当前候选人页面", _selected(candidate_b))
    assert int((focus.get("candidate") or {}).get("id") or 0) == candidate_b
    assert int((focus.get("context") or {}).get("id") or 0) == candidate_b
    assert float(focus.get("confidence") or 0) == 1.0


def test_focus_cleared_when_new_page_candidate_not_in_db(
    service: AgentService, candidate_ids: tuple[int, int, int]
) -> None:
    candidate_a, _, missing_id = candidate_ids
    session_id = f"floating-focus-clear-{uuid.uuid4().hex[:8]}"
    _pin_focus(service, session_id, candidate_a)
    focus = service._persist_copilot_focus(session_id, "同步当前候选人页面", _selected(missing_id))
    # 新候选人未入库：清空候选人焦点并降权，绝不回退钉住旧候选人 A
    assert not (focus.get("candidate") or {}).get("id")
    assert int((focus.get("candidate") or {}).get("id") or 0) != candidate_a
    assert (focus.get("context") or {}).get("type") == "global"
    assert not (focus.get("context") or {}).get("id")
    assert float(focus.get("confidence") or 0) <= 0.4


def test_continuation_does_not_resurrect_old_candidate_when_selected_differs(
    service: AgentService, candidate_ids: tuple[int, int, int]
) -> None:
    candidate_a, candidate_b, _ = candidate_ids
    session_id = f"floating-continuation-{uuid.uuid4().hex[:8]}"
    _pin_focus(service, session_id, candidate_a)
    context, conflicts = service._copilot_context_from_focus(
        session_id, "可以，但是这个岗位先暂停一下", _selected(candidate_b)
    )
    assert conflicts == []
    assert context.get("type") == "candidate"
    assert int(context.get("id") or 0) == candidate_b


def test_continuation_still_restores_focus_when_selected_is_global(
    service: AgentService, candidate_ids: tuple[int, int, int]
) -> None:
    # 基线行为保持不变：消息未附带不同候选人上下文时，continuation 仍沿用旧焦点。
    candidate_a, _, _ = candidate_ids
    session_id = f"floating-continuation-base-{uuid.uuid4().hex[:8]}"
    _pin_focus(service, session_id, candidate_a)
    context, conflicts = service._copilot_context_from_focus(
        session_id, "可以，但是这个岗位先暂停一下", _selected(None)
    )
    assert conflicts == []
    assert context.get("type") == "candidate"
    assert int(context.get("id") or 0) == candidate_a

