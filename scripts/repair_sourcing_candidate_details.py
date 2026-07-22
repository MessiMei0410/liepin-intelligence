#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from run_published_position_search import CAPTURE_LINKS_JS, CDP, EXTRACT_JS, SEARCH_JS, evaluate


DEFAULT_DB = Path(
    "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
)


def _compact(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _history_text(items: Any, fields: tuple[str, ...]) -> str:
    if not isinstance(items, list):
        return ""
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = " · ".join(str(item.get(field) or "").strip() for field in fields if str(item.get(field) or "").strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _search_tab(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        tabs = json.loads(response.read().decode())
    tab = next(
        (
            item for item in tabs
            if item.get("type") == "page"
            and "h.liepin.com/search/getConditionItem" in str(item.get("url") or "")
        ),
        None,
    )
    if not tab:
        raise RuntimeError("Liepin search tab is not open")
    return str(tab["webSocketDebuggerUrl"])


def _targets(conn: sqlite3.Connection, workflow_ids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in workflow_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT sa.workflow_id,sa.job_candidate_id,sa.channel,sa.source_query,
               jc.person_id,p.display_name AS name,p.current_company AS company,
               p.current_title AS title
        FROM agent_sourcing_attributions sa
        JOIN job_candidates jc ON jc.id=sa.job_candidate_id
        JOIN people p ON p.id=jc.person_id
        WHERE sa.workflow_id IN ({placeholders}) AND lower(sa.channel)='liepin'
        ORDER BY sa.job_candidate_id
        """,
        workflow_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _capture(cdp: CDP, query: str) -> list[dict[str, Any]]:
    evaluate(cdp, f"({SEARCH_JS})({json.dumps(query, ensure_ascii=False)})", timeout=20)
    cards = evaluate(cdp, f"({EXTRACT_JS})()", timeout=15) or []
    links = evaluate(cdp, f"({CAPTURE_LINKS_JS})({len(cards)})", timeout=20) or []
    for index, card in enumerate(cards):
        card["resume_url"] = (links[index] if index < len(links) else "") or card.get("resume_url", "")
    return cards


def _match(target: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    name = _compact(target["name"])
    company = _compact(target["company"])
    candidates = [card for card in cards if _compact(card.get("name")) == name]
    exact = [card for card in candidates if company and _compact(card.get("current_company")) == company]
    return (exact or candidates or [None])[0]


def _write_profile(conn: sqlite3.Connection, target: dict[str, Any], card: dict[str, Any]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_url = str(card.get("resume_url") or "").strip()
    resume_id = str(card.get("res_id_encode") or "").strip()
    profile_text = str(card.get("raw_text") or "").strip()
    raw = {
        "name": card.get("name") or target["name"],
        "company": card.get("current_company") or target["company"],
        "title": card.get("current_title") or target["title"],
        "education": card.get("education") or "",
        "experience": card.get("experience") or "",
        "city": card.get("city") or "",
        "profile_text": profile_text,
        "full_text": profile_text,
        "work_text": _history_text(card.get("work"), ("company", "title", "dates")),
        "education_text": _history_text(card.get("education_history"), ("school", "major", "degree", "dates")),
        "project_text": "",
        "source_url": source_url,
        "res_id_encode": resume_id,
        "source_query": target["source_query"],
        "repaired_at": now,
    }
    existing = conn.execute(
        "SELECT id FROM source_profiles WHERE person_id=? AND lower(COALESCE(source_type,''))='liepin' ORDER BY id DESC LIMIT 1",
        (target["person_id"],),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE source_profiles
            SET source_candidate_id=?,source_date=?,raw_status='search_shortlisted',raw_json=?
            WHERE id=?
            """,
            (resume_id or None, now[:10], json.dumps(raw, ensure_ascii=False), existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO source_profiles
                (person_id,source_type,source_candidate_id,source_date,raw_status,raw_json)
            VALUES (?,'liepin',?,?,'search_shortlisted',?)
            """,
            (target["person_id"], resume_id or None, now[:10], json.dumps(raw, ensure_ascii=False)),
        )
    if source_url:
        conn.execute(
            """
            INSERT INTO entity_source_links
                (canonical_type,canonical_id,source_system,source_entity_type,
                 source_entity_id,source_url,metadata_json,updated_at)
            VALUES ('person',?,'liepin','external_profile',?,?,?,?)
            ON CONFLICT(source_system,source_entity_type,source_entity_id,canonical_type,canonical_id)
            DO UPDATE SET source_url=excluded.source_url,metadata_json=excluded.metadata_json,
                          updated_at=excluded.updated_at
            """,
            (
                str(target["person_id"]),
                resume_id or source_url,
                source_url,
                json.dumps({"repaired_from": "liepin_search", "source_query": target["source_query"]}, ensure_ascii=False),
                now,
            ),
        )
    event = conn.execute(
        """
        SELECT id,raw_json FROM candidate_events
        WHERE job_candidate_id=? AND event_type='search_shortlisted'
        ORDER BY id DESC LIMIT 1
        """,
        (target["job_candidate_id"],),
    ).fetchone()
    if event:
        try:
            event_raw = json.loads(event["raw_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            event_raw = {}
        event_raw.update(raw)
        conn.execute(
            "UPDATE candidate_events SET raw_json=?,source_id=? WHERE id=?",
            (json.dumps(event_raw, ensure_ascii=False), source_url, event["id"]),
        )


def repair(db_path: Path, port: int, workflow_ids: list[str]) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        targets = _targets(conn, workflow_ids)
        by_query: dict[str, list[dict[str, Any]]] = {}
        for target in targets:
            by_query.setdefault(str(target["source_query"] or "").strip(), []).append(target)
        cdp = CDP(_search_tab(port))
        repaired = []
        missing = []
        try:
            for query, query_targets in by_query.items():
                cards = _capture(cdp, query)
                for target in query_targets:
                    card = _match(target, cards)
                    if not card or not card.get("resume_url"):
                        missing.append({"job_candidate_id": target["job_candidate_id"], "name": target["name"], "query": query})
                        continue
                    with conn:
                        _write_profile(conn, target, card)
                    repaired.append({
                        "job_candidate_id": target["job_candidate_id"],
                        "name": target["name"],
                        "source_url": card["resume_url"],
                    })
        finally:
            cdp.close()
        return {"ok": not missing, "repaired": repaired, "missing": missing}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair Liepin card profiles and current-session source links")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--workflow-id", action="append", required=True)
    args = parser.parse_args()
    result = repair(args.db.expanduser(), args.port, args.workflow_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
