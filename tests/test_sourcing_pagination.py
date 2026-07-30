from __future__ import annotations

from a_system_agent.sourcing_pagination import PageResult, collect_pages, seek_to_page


def test_collect_pages_exhausts_only_after_next_page_disappears() -> None:
    pages = {
        1: PageResult(items=[{"id": "a"}, {"id": "b"}], reported_total=3, has_next=True),
        2: PageResult(items=[{"id": "c"}], reported_total=3, has_next=False),
    }
    advanced: list[int] = []

    result = collect_pages(
        fetch_page=lambda page: pages[page],
        advance_page=lambda page: advanced.append(page) or True,
        start_page=1,
        max_pages=10,
    )

    assert [item["id"] for item in result.items] == ["a", "b", "c"]
    assert advanced == [2]
    assert result.pages_fetched == 2
    assert result.terminal_state == "exhausted"
    assert result.terminal_reason == "reported_total_exhausted"
    assert result.cursor is None


def test_collect_pages_keeps_cursor_when_safety_limit_truncates_platform() -> None:
    result = collect_pages(
        fetch_page=lambda page: PageResult(
            items=[{"id": f"p{page}"}], reported_total=100, has_next=True,
        ),
        advance_page=lambda page: True,
        start_page=4,
        max_pages=2,
    )

    assert result.pages_fetched == 2
    assert result.terminal_state == "platform_capped"
    assert result.terminal_reason == "page_safety_limit"
    assert result.cursor == {"page": 6}


def test_seek_to_page_replays_navigation_without_recollecting_prior_pages() -> None:
    fetched: list[int] = []
    advanced: list[int] = []

    blocked = seek_to_page(
        fetch_page=lambda page: fetched.append(page) or PageResult(
            items=[{"id": f"p{page}"}], reported_total=100, has_next=True,
        ),
        advance_page=lambda page: advanced.append(page) or True,
        start_page=3,
    )

    assert blocked is None
    assert fetched == [1]
    assert advanced == [2, 3]


def test_seek_to_page_keeps_cursor_when_navigation_cannot_reach_resume_page() -> None:
    result = seek_to_page(
        fetch_page=lambda page: PageResult(
            items=[{"id": f"p{page}"}], reported_total=100, has_next=True,
        ),
        advance_page=lambda page: page < 3,
        start_page=4,
    )

    assert result is not None
    assert result.terminal_state == "blocked"
    assert result.terminal_reason == "resume_page_unavailable"
    assert result.cursor == {"page": 3}


def test_collect_pages_does_not_count_repeated_candidates_toward_exhaustion() -> None:
    pages = {
        1: PageResult(items=[{"id": "a"}, {"id": "b"}], reported_total=3, has_next=True),
        2: PageResult(items=[{"id": "b"}, {"id": "a"}], reported_total=3, has_next=True),
    }

    result = collect_pages(
        fetch_page=lambda page: pages[page],
        advance_page=lambda page: True,
        start_page=1,
        max_pages=10,
        item_key=lambda item: str(item.get("id") or ""),
    )

    assert result.pages_fetched == 2
    assert result.terminal_state == "blocked"
    assert result.terminal_reason == "repeated_page_results"
    assert result.cursor == {"page": 2}


def test_collect_pages_resume_uses_prior_count_for_total_exhaustion() -> None:
    result = collect_pages(
        fetch_page=lambda page: PageResult(
            items=[{"id": "d"}], reported_total=4, has_next=False,
        ),
        advance_page=lambda page: True,
        start_page=4,
        max_pages=10,
        collected_before=3,
        item_key=lambda item: str(item.get("id") or ""),
    )

    assert result.pages_fetched == 1
    assert result.terminal_state == "exhausted"
    assert result.terminal_reason == "reported_total_exhausted"


def test_collect_pages_resume_rejects_page_containing_only_prior_candidates() -> None:
    result = collect_pages(
        fetch_page=lambda page: PageResult(
            items=[{"id": "a"}, {"id": "b"}], reported_total=3, has_next=True,
        ),
        advance_page=lambda page: True,
        start_page=2,
        max_pages=10,
        collected_before=2,
        seen_before_keys={"a", "b"},
        item_key=lambda item: str(item.get("id") or ""),
    )

    assert result.items == []
    assert result.terminal_state == "blocked"
    assert result.terminal_reason == "repeated_page_results"
    assert result.cursor == {"page": 2}
