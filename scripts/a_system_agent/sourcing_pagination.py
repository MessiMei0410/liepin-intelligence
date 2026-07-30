from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class PageResult:
    items: list[dict[str, Any]]
    reported_total: int | None
    has_next: bool


@dataclass(frozen=True)
class PaginationResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    reported_total: int | None = None
    pages_fetched: int = 0
    terminal_state: str = "blocked"
    terminal_reason: str = "not_started"
    cursor: dict[str, Any] | None = None


def seek_to_page(
    *,
    fetch_page: Callable[[int], PageResult],
    advance_page: Callable[[int], bool],
    start_page: int,
) -> PaginationResult | None:
    """Position an already-open query at a saved page without recollecting prior pages."""
    target_page = max(1, int(start_page))
    if target_page == 1:
        return None
    # Seed channel-specific page signatures once; advance_page keeps them current.
    fetch_page(1)
    for next_page in range(2, target_page + 1):
        if not advance_page(next_page):
            return PaginationResult(
                pages_fetched=0,
                terminal_state="blocked",
                terminal_reason="resume_page_unavailable",
                cursor={"page": next_page},
            )
    return None


def collect_pages(
    *,
    fetch_page: Callable[[int], PageResult],
    advance_page: Callable[[int], bool],
    start_page: int = 1,
    max_pages: int = 50,
    collected_before: int = 0,
    item_key: Callable[[dict[str, Any]], str] | None = None,
    seen_before_keys: set[str] | None = None,
) -> PaginationResult:
    """Collect a bounded page sequence while preserving a resumable next-page cursor."""
    current_page = max(1, int(start_page))
    page_budget = max(1, int(max_pages))
    prior_count = max(0, int(collected_before))
    items: list[dict[str, Any]] = []
    reported_total: int | None = None
    pages_fetched = 0
    prior_keys = {str(key).strip() for key in (seen_before_keys or set()) if str(key).strip()}
    seen_item_keys: set[str] = set(prior_keys)
    while pages_fetched < page_budget:
        page = fetch_page(current_page)
        page_items = [item for item in page.items if isinstance(item, dict)]
        pages_fetched += 1
        new_item_keys = 0
        if item_key is not None:
            new_page_items: list[dict[str, Any]] = []
            for item in page_items:
                key = str(item_key(item) or "").strip()
                if key and key not in seen_item_keys:
                    seen_item_keys.add(key)
                    new_item_keys += 1
                    new_page_items.append(item)
            items.extend(new_page_items)
        else:
            items.extend(page_items)
        if page.reported_total is not None:
            reported_total = max(0, int(page.reported_total))
        if item_key is not None and page_items and new_item_keys == 0:
            return PaginationResult(
                items=items,
                reported_total=reported_total,
                pages_fetched=pages_fetched,
                terminal_state="blocked",
                terminal_reason="repeated_page_results",
                cursor={"page": current_page},
            )
        current_count = len(seen_item_keys - prior_keys) if item_key is not None else len(items)
        collected_count = max(prior_count, len(prior_keys)) + current_count
        if reported_total is not None and collected_count >= reported_total:
            return PaginationResult(
                items=items,
                reported_total=reported_total,
                pages_fetched=pages_fetched,
                terminal_state="exhausted",
                terminal_reason="reported_total_exhausted",
            )
        if not page.has_next:
            if reported_total is not None and len(items) < reported_total:
                return PaginationResult(
                    items=items,
                    reported_total=reported_total,
                    pages_fetched=pages_fetched,
                    terminal_state="platform_capped",
                    terminal_reason="pagination_ended_before_total",
                )
            return PaginationResult(
                items=items,
                reported_total=reported_total,
                pages_fetched=pages_fetched,
                terminal_state="exhausted",
                terminal_reason="pagination_end",
            )
        next_page = current_page + 1
        if pages_fetched >= page_budget:
            return PaginationResult(
                items=items,
                reported_total=reported_total,
                pages_fetched=pages_fetched,
                terminal_state="platform_capped",
                terminal_reason="page_safety_limit",
                cursor={"page": next_page},
            )
        if not advance_page(next_page):
            return PaginationResult(
                items=items,
                reported_total=reported_total,
                pages_fetched=pages_fetched,
                terminal_state="blocked",
                terminal_reason="next_page_unavailable",
                cursor={"page": next_page},
            )
        current_page = next_page
    raise AssertionError("pagination loop exceeded its page budget")
