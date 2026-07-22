#!/usr/bin/env python3
"""Backfill historical talk_samples strategy labels using current reply rules."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from reply_intelligence_rules import classify_reply, normalize_message


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def classify_strategy(message: str, direction: str) -> str:
    text = message or ""
    if direction == "system":
        return "system"
    if "您向对方推荐了" in text:
        return "job_recommendation"
    if "半导体" in text and "加个微信" in text:
        return "broad_semiconductor_wechat"
    classified = classify_reply(text)
    tags = set(classified.get("reply_tags") or [])
    intent = classified.get("intent") or "unclear"
    if "确认是否在招" in tags:
        return "asks_if_open"
    if "方向确认" in tags:
        return "asks_direction"
    if "自荐匹配" in tags:
        return "self_recommendation"
    if intent == "location_concern":
        return "location"
    if intent == "need_more_info":
        return "asks_company"
    if intent == "interested":
        return "positive_fit"
    if intent == "not_interested":
        return "mismatch_or_reject"
    if intent == "salary_concern":
        return "salary"
    if intent == "need_contact":
        return "contact_exchange"
    if normalize_message(text) in {"您好", "你好", "hello", "hi"}:
        return "short_ping"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill talk_samples strategy labels.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()

    conn = connect(Path(args.db).expanduser())
    rows = conn.execute(
        """
        SELECT id, message, direction_guess, strategy_guess
        FROM talk_samples
        ORDER BY id
        """
    ).fetchall()

    updated = 0
    changes = Counter()
    for row in rows:
        new_strategy = classify_strategy(row["message"] or "", row["direction_guess"] or "")
        old_strategy = row["strategy_guess"] or ""
        if new_strategy == old_strategy:
            continue
        conn.execute(
            "UPDATE talk_samples SET strategy_guess = ? WHERE id = ?",
            (new_strategy, row["id"]),
        )
        updated += 1
        changes[f"{old_strategy or 'empty'}->{new_strategy}"] += 1
    conn.commit()
    conn.close()

    print(
        json.dumps(
            {
                "ok": True,
                "updated": updated,
                "changes": dict(changes),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
