from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .capability_runtime_base import (
    DEFAULT_PAGINATION_CONTINUATION_HEADROOM,
    DEFAULT_SOURCING_CELL_BATCH_SIZE,
    MAX_SOURCING_CELL_BATCH_SIZE,
    MAX_SOURCING_CONTINUATION_BATCHES,
    _loads,
    _round_int,
    _row,
    _trim_error,
    classify_zero_result,
)


class RunnerSearchMixin:
    """寻访执行支撑：查询单元续跑、漏斗/召回/覆盖持久化与 0 召回归因。"""

    def _resume_query_cells(
        self,
        run_id: str,
        query_plan: dict[str, Any],
        *,
        max_retries: int = 3,
        max_platform_capped_retries: int = 0,
    ) -> list[dict[str, Any]]:
        """Select only unfinished/retryable cells for an explicitly resumed run."""
        conn = self.service._connect()
        try:
            rows = conn.execute(
                "SELECT cell_id,plan_hash,status,retry_count,cursor_json,pages_fetched,terminal_reason,"
                "extracted_count,unique_count,updated_at FROM agent_sourcing_query_cells WHERE run_id=?",
                (run_id,),
            ).fetchall()
            recall_rows = conn.execute(
                "SELECT query_cell_id,source_candidate_id FROM agent_candidate_recalls "
                "WHERE run_id=? AND query_cell_id<>'' AND source_candidate_id<>''",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return [cell for cell in query_plan.get("cells") or [] if isinstance(cell, dict)]
        plan_hash = str(query_plan.get("plan_hash") or "")
        if any(str(row["plan_hash"] or "") != plan_hash for row in rows):
            raise ValueError("断点续跑的 query_plan_v1 与原 run_id 不一致")
        states = {
            str(row["cell_id"]): (
                str(row["status"]), int(row["retry_count"] or 0), _loads(row["cursor_json"], {}),
                int(row["pages_fetched"] or 0), int(row["extracted_count"] or 0),
                int(row["unique_count"] or 0), str(row["terminal_reason"] or ""),
                str(row["updated_at"] or ""),
            )
            for row in rows
        }
        seen_keys_by_cell: dict[str, list[str]] = {}
        for row in recall_rows:
            cell_id = str(row["query_cell_id"] or "")
            source_id = str(row["source_candidate_id"] or "").strip()
            if cell_id and source_id and not source_id.startswith("anon_"):
                seen_keys_by_cell.setdefault(cell_id, []).append(source_id)
        blocked_families_by_channel: dict[str, set[str]] = {"liepin": set(), "xsaas": set()}
        for cell in query_plan.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            cell_id = str(cell.get("cell_id") or "")
            status, _retries, cursor, _pages, _extracted, _unique, terminal_reason, _updated = states.get(
                cell_id, ("pending", 0, {}, 0, 0, 0, "", ""),
            )
            if status not in {"failed", "blocked"}:
                continue
            if status == "blocked" and cursor:
                continue
            if status == "blocked" and terminal_reason not in {
                "channel_blocked_before_query", "approved_cell_not_executed",
            }:
                continue
            channel = str(cell.get("channel") or "")
            blocked_families_by_channel.setdefault(channel, set()).update(
                str(value) for value in cell.get("query_family_ids") or [] if str(value).strip()
            )
        pending: list[dict[str, Any]] = []
        fallback_pending: list[dict[str, Any]] = []
        retryable: list[tuple[str, int, dict[str, Any]]] = []
        for plan_index, cell in enumerate(query_plan.get("cells") or []):
            if not isinstance(cell, dict):
                continue
            status, retries, cursor, pages_fetched, extracted_count, unique_count, terminal_reason, updated_at = states.get(
                str(cell.get("cell_id") or ""), ("pending", 0, {}, 0, 0, 0, "", ""),
            )
            if status == "pending":
                channel = str(cell.get("channel") or "")
                other_channel = "xsaas" if channel == "liepin" else "liepin"
                families = {str(value) for value in cell.get("query_family_ids") or [] if str(value).strip()}
                if families and families & blocked_families_by_channel.get(other_channel, set()):
                    fallback_pending.append({
                        **cell,
                        "execution_fallback_relay": {
                            "from_channel": other_channel,
                            "reason": "same_query_family_blocked",
                            "query_family_ids": sorted(families & blocked_families_by_channel[other_channel]),
                        },
                    })
                else:
                    pending.append(cell)
            elif (
                (status == "failed" and retries < max(1, max_retries))
                or (
                    status == "blocked"
                    and retries < max(1, max_retries)
                    and not cursor
                    and terminal_reason in {"channel_blocked_before_query", "approved_cell_not_executed"}
                )
            ):
                retryable.append((updated_at, plan_index, cell))
            elif (
                status in {"platform_capped", "blocked"}
                and (
                    (status == "platform_capped" and retries < max(0, max_platform_capped_retries))
                    or (status == "blocked" and retries < max(1, max_retries))
                )
                and isinstance(cursor, dict)
                and int(cursor.get("page") or 0) > 1
            ):
                retryable.append((
                    updated_at,
                    plan_index,
                    {
                        **cell,
                        "execution_cursor": {"page": int(cursor["page"])},
                        "execution_progress": {
                            "pages_fetched": pages_fetched,
                            "extracted_count": extracted_count,
                            "unique_count": unique_count,
                            "seen_candidate_keys": list(dict.fromkeys(seen_keys_by_cell.get(str(cell.get("cell_id") or ""), []))),
                        },
                    },
                ))
        retryable.sort(key=lambda item: (item[0], item[1]))
        return [*fallback_pending, *pending, *(item[2] for item in retryable)]

    def _sourcing_continuation(
        self,
        *,
        request: dict[str, Any],
        run_id: str,
        query_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a hash-bound next batch for retryable/cursor-bearing query cells."""
        try:
            index = max(0, int(request.get("_continuation_index") or 0))
        except (TypeError, ValueError):
            index = 0
        try:
            cell_batch_size = max(
                1,
                min(
                    int(request.get("max_query_cells_per_batch") or DEFAULT_SOURCING_CELL_BATCH_SIZE),
                    MAX_SOURCING_CELL_BATCH_SIZE,
                ),
            )
        except (TypeError, ValueError):
            cell_batch_size = DEFAULT_SOURCING_CELL_BATCH_SIZE
        approved_cell_count = sum(
            1 for cell in query_plan.get("cells") or [] if isinstance(cell, dict)
        )
        minimum_plan_continuations = max(
            0,
            (approved_cell_count + cell_batch_size - 1) // cell_batch_size - 1,
        )
        default_budget = min(
            MAX_SOURCING_CONTINUATION_BATCHES,
            minimum_plan_continuations + DEFAULT_PAGINATION_CONTINUATION_HEADROOM,
        )
        raw_budget = request.get("max_continuation_batches")
        if raw_budget in (None, ""):
            max_batches = default_budget
        else:
            try:
                requested_budget = max(
                    0,
                    min(int(raw_budget), MAX_SOURCING_CONTINUATION_BATCHES),
                )
            except (TypeError, ValueError):
                requested_budget = default_budget
            # A legacy/manual cap must not prevent every approved cell from receiving
            # its first execution attempt. Pagination still uses the bounded remainder.
            max_batches = max(minimum_plan_continuations, requested_budget)
        runnable = self._resume_query_cells(
            run_id,
            query_plan,
            max_retries=int(request.get("max_query_retries") or 3),
            max_platform_capped_retries=self._platform_capped_continuation_limit(request),
        )
        summary = {
            "scheduled": bool(runnable and index < max_batches),
            "run_id": run_id,
            "completed_batches": index + 1,
            "remaining_cells": len(runnable),
            "limit_reached": bool(runnable and index >= max_batches),
            "continuation_budget": max_batches,
            "minimum_plan_continuations": minimum_plan_continuations,
            "pagination_headroom": max(0, max_batches - minimum_plan_continuations),
        }
        if not summary["scheduled"]:
            return {"request": None, "summary": summary}
        next_request = {
            key: value
            for key, value in request.items()
            if key not in {"_audit_only_result", "resume_run_id", "_continuation_index"}
        }
        next_request.update({
            "resume_run_id": run_id,
            "_continuation_index": index + 1,
        })
        return {"request": next_request, "summary": summary}

    def _persist_query_cell_states(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        query_plan: dict[str, Any],
        channel_runs: list[dict[str, Any]],
        executed_cell_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Persist per-query progress without turning truncation or unknown totals into completion."""
        def normalized(value: Any) -> str:
            return " ".join(str(value or "").split()).casefold()

        def integer(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        rounds_by_query: dict[tuple[str, str], tuple[dict[str, Any], str, dict[str, Any]]] = {}
        channel_status: dict[str, tuple[str, dict[str, Any]]] = {}
        for run in channel_runs:
            if not isinstance(run, dict):
                continue
            channel = str(run.get("channel") or "").lower()
            result = run.get("result") if isinstance(run.get("result"), dict) else {}
            channel_status[channel] = (str(run.get("status") or ""), result)
            for round_item in result.get("rounds") or []:
                if isinstance(round_item, dict):
                    rounds_by_query[(channel, normalized(round_item.get("query")))] = (
                        round_item, str(run.get("status") or ""), result,
                    )

        job_id = self._job_id(client, job)
        terminal_counts: dict[str, int] = {}
        conn = self.service._connect()
        try:
            for cell in query_plan.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_query_cells
                    (run_id,workflow_id,job_id,plan_hash,cell_id,channel,query,priority,status)
                    VALUES (?,?,?,?,?,?,?,?, 'pending')
                    ON CONFLICT(run_id,cell_id) DO NOTHING
                    """,
                    (
                        run_id, workflow_id or None, job_id, str(query_plan.get("plan_hash") or ""),
                        str(cell.get("cell_id") or ""), str(cell.get("channel") or ""),
                        str(cell.get("query") or ""), integer(cell.get("priority")),
                    ),
                )

            for cell in query_plan.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                channel = str(cell.get("channel") or "").lower()
                cell_id = str(cell.get("cell_id") or "")
                match = rounds_by_query.get((channel, normalized(cell.get("query"))))
                existing = conn.execute(
                    "SELECT status,reported_total,pages_fetched,extracted_count,unique_count,cursor_json "
                    "FROM agent_sourcing_query_cells WHERE run_id=? AND cell_id=?",
                    (run_id, cell_id),
                ).fetchone()
                existing_status = str(existing["status"] or "") if existing else ""
                if executed_cell_ids is not None and cell_id not in executed_cell_ids:
                    if existing_status in {"exhausted", "platform_capped", "blocked", "failed"}:
                        terminal_counts[existing_status] = terminal_counts.get(existing_status, 0) + 1
                    continue
                reported_total: int | None = None
                extracted = 0
                unique_count = 0
                pages_fetched = 0
                cursor: Any = {}
                last_error = None
                if match:
                    round_item, run_status, channel_result = match
                    raw_total = round_item.get("result_count")
                    if raw_total is not None and str(raw_total).strip() != "":
                        reported_total = max(0, integer(raw_total))
                    extracted = max(0, integer(round_item.get("extracted_count")))
                    unique_count = max(0, integer(round_item.get("unique_count"), extracted))
                    pages_fetched = max(0, integer(round_item.get("pages_fetched"), 1))
                    cursor = round_item.get("cursor") or {}
                    round_status = str(round_item.get("status") or run_status or "completed")
                    explicit_terminal = str(round_item.get("terminal_state") or "")
                    if explicit_terminal in {"exhausted", "platform_capped", "blocked", "failed"}:
                        status = explicit_terminal
                        reason = str(round_item.get("terminal_reason") or explicit_terminal)
                    elif round_status == "failed":
                        status, reason = "failed", str(round_item.get("reason") or "query_failed")
                    elif round_status in {"blocked", "skipped", "stale_query"}:
                        status, reason = "blocked", str(round_item.get("reason") or round_status)
                    elif reported_total is None:
                        status, reason = "platform_capped", "reported_total_unknown"
                    elif extracted >= reported_total:
                        status, reason = "exhausted", "reported_total_exhausted"
                    else:
                        status, reason = "platform_capped", "reported_total_not_exhausted"
                    last_error = str(round_item.get("error") or channel_result.get("error") or "") or None
                    if existing_status in {"platform_capped", "blocked", "failed"}:
                        pages_fetched += max(0, integer(existing["pages_fetched"]))
                        extracted += max(0, integer(existing["extracted_count"]))
                        unique_count += max(0, integer(existing["unique_count"]))
                        if reported_total is None and existing["reported_total"] is not None:
                            reported_total = max(0, integer(existing["reported_total"]))
                    ledger = conn.execute(
                        "SELECT COUNT(*) AS occurrences,"
                        "COUNT(DISTINCT COALESCE(NULLIF(source_candidate_id,''),identity_key)) AS unique_count "
                        "FROM agent_candidate_recalls WHERE run_id=? AND query_cell_id=?",
                        (run_id, cell_id),
                    ).fetchone()
                    ledger_occurrences = int(ledger["occurrences"] or 0) if ledger else 0
                    ledger_unique = int(ledger["unique_count"] or 0) if ledger else 0
                    if ledger_occurrences or extracted == 0:
                        extracted = ledger_occurrences
                        unique_count = ledger_unique
                    if status == "exhausted" and reported_total is not None and unique_count < reported_total:
                        status = "platform_capped" if isinstance(cursor, dict) and cursor else "blocked"
                        reason = "duplicate_candidates_before_reported_total"
                else:
                    run_status, channel_result = channel_status.get(channel, ("", {}))
                    if existing is not None:
                        reported_total = (
                            max(0, integer(existing["reported_total"]))
                            if existing["reported_total"] is not None
                            else None
                        )
                        pages_fetched = max(0, integer(existing["pages_fetched"]))
                        extracted = max(0, integer(existing["extracted_count"]))
                        unique_count = max(0, integer(existing["unique_count"]))
                        cursor = _loads(existing["cursor_json"], {})
                    if run_status == "failed":
                        status, reason = "failed", "channel_failed_before_query"
                    elif run_status == "blocked":
                        status, reason = "blocked", "channel_blocked_before_query"
                    else:
                        status, reason = "blocked", "approved_cell_not_executed"
                    last_error = str(channel_result.get("error") or "") or None
                conn.execute(
                    """
                    UPDATE agent_sourcing_query_cells
                       SET status=?,reported_total=?,pages_fetched=?,extracted_count=?,unique_count=?,
                           cursor_json=?,retry_count=retry_count+?,
                           terminal_reason=?,last_error=?,started_at=COALESCE(started_at,datetime('now','localtime')),
                           finished_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                     WHERE run_id=? AND cell_id=?
                    """,
                    (
                        status, reported_total, pages_fetched, extracted, unique_count,
                        json.dumps(cursor, ensure_ascii=False),
                        1 if status in {"failed", "blocked"} or (
                            status == "platform_capped" and existing_status == "platform_capped"
                        ) else 0,
                        reason, last_error,
                        run_id, str(cell.get("cell_id") or ""),
                    ),
                )
                terminal_counts[status] = terminal_counts.get(status, 0) + 1
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "run_id": run_id, "stored": sum(terminal_counts.values()), "terminal_counts": terminal_counts}

    def _persist_candidate_recalls(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        query_plan: dict[str, Any],
        strategy_snapshot: dict[str, Any] | None = None,
        raw_candidates: dict[str, list[Any]],
        applied: dict[str, Any],
        min_score: int,
    ) -> dict[str, Any]:
        """Persist every extracted card before formal candidate intake or score filtering."""
        def normalized(value: Any) -> str:
            return re.sub(r"\s+", "", str(value or "")).casefold()

        def source_identifier(item: dict[str, Any]) -> str:
            return str(
                item.get("source_candidate_id") or item.get("candidate_id") or item.get("resume_id")
                or item.get("res_id_encode") or item.get("xsaas_id") or ""
            ).strip()

        def identity(item: dict[str, Any], channel: str, query: str) -> tuple[str, ...]:
            source_id = source_identifier(item)
            if source_id:
                return ("source", normalized(channel), normalized(query), normalized(source_id))
            return (
                "profile", normalized(channel), normalized(query), normalized(item.get("name")),
                normalized(item.get("company") or item.get("current_company")),
                normalized(item.get("title") or item.get("current_title")),
            )

        strategy_snapshot = strategy_snapshot if isinstance(strategy_snapshot, dict) else {}
        cells_by_query = {
            (str(cell.get("channel") or ""), normalized(cell.get("query"))): cell
            for cell in query_plan.get("cells") or []
            if isinstance(cell, dict)
        }
        strategy_hash = str(strategy_snapshot.get("strategy_hash") or "")
        strategy_artifact_id = str(strategy_snapshot.get("strategy_artifact_id") or "")
        strategy_revision = int(strategy_snapshot.get("strategy_revision") or 0) or None
        query_plan_hash = str(query_plan.get("plan_hash") or strategy_snapshot.get("query_plan_hash") or "")
        staged = applied.get("staged") if isinstance(applied.get("staged"), dict) else {}
        disposition: dict[tuple[str, ...], str] = {}
        staged_by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
        for key, state in (("accepted", "accepted"), ("existing", "existing"), ("batch_duplicates", "batch_duplicate")):
            for raw in staged.get(key) or []:
                if not isinstance(raw, dict):
                    continue
                channel = str(raw.get("channel") or raw.get("source") or "").lower()
                query = str(raw.get("source_query") or raw.get("query") or "")
                item_identity = identity(raw, channel, query)
                disposition[item_identity] = state
                staged_by_identity[item_identity] = raw
        for error in staged.get("errors") or []:
            raw = error.get("raw") if isinstance(error, dict) and isinstance(error.get("raw"), dict) else {}
            channel = str(raw.get("channel") or raw.get("source") or "").lower()
            query = str(raw.get("source_query") or raw.get("query") or "")
            item_identity = identity(raw, channel, query)
            disposition[item_identity] = "invalid"
            staged_by_identity[item_identity] = raw

        receipts = (
            applied.get("intake", {}).get("receipts") or []
            if isinstance(applied.get("intake"), dict)
            else []
        )
        accepted = [item for item in staged.get("accepted") or [] if isinstance(item, dict)]
        receipt_by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
        for accepted_item, receipt in zip(accepted, receipts, strict=False):
            if not isinstance(receipt, dict):
                continue
            accepted_channel = str(accepted_item.get("channel") or accepted_item.get("source") or "").lower()
            accepted_query = str(accepted_item.get("source_query") or accepted_item.get("query") or "")
            receipt_by_identity[identity(accepted_item, accepted_channel, accepted_query)] = receipt
        job_id = self._job_id(client, job)
        stored = 0
        by_state: dict[str, int] = {}
        conn = self.service._connect()
        try:
            for channel, values in raw_candidates.items():
                normalized_channel = str(channel or "unknown").lower()
                for index, raw in enumerate(values if isinstance(values, list) else [], 1):
                    if not isinstance(raw, dict):
                        continue
                    source_query = " ".join(str(raw.get("source_query") or raw.get("query") or "").split())
                    source_id = str(
                        raw.get("source_candidate_id") or raw.get("candidate_id") or raw.get("resume_id")
                        or raw.get("res_id_encode")
                        or raw.get("xsaas_id") or raw.get("resume_url") or raw.get("source_url") or ""
                    ).strip()
                    name = str(raw.get("name") or "").strip()
                    company = str(raw.get("company") or raw.get("current_company") or "").strip()
                    title = str(raw.get("title") or raw.get("current_title") or "").strip()
                    identity_key = "|".join((normalized(name), normalized(company), normalized(title)))
                    if not source_id:
                        source_id = "anon_" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:20]
                    try:
                        score = int(raw["fit_score"]) if raw.get("fit_score") is not None else None
                    except (TypeError, ValueError):
                        score = None
                    item_identity = identity(raw, normalized_channel, source_query)
                    state = disposition.get(item_identity, "not_intaked")
                    staged_item = staged_by_identity.get(item_identity, {})
                    exclusion_reason = None
                    if score is not None and score < min_score:
                        exclusion_reason = "score_below_threshold"
                    elif state == "existing":
                        exclusion_reason = "existing_candidate"
                    elif state == "batch_duplicate":
                        exclusion_reason = "same_batch_duplicate"
                    elif state == "invalid":
                        exclusion_reason = "normalization_error"
                    elif state == "not_intaked":
                        exclusion_reason = "not_in_intake_output"
                    receipt: dict[str, Any] = {}
                    if state == "accepted":
                        receipt = receipt_by_identity.get(
                            identity(raw, normalized_channel, source_query),
                            {},
                        )
                    receipt_status = str(receipt.get("status") or "")
                    if receipt_status in {"existing", "existing_relation"}:
                        state = receipt_status
                        exclusion_reason = "existing_candidate" if receipt_status == "existing" else "existing_relation"
                    page_number = max(1, int(raw.get("page_number") or raw.get("page") or 1))
                    position_index = max(0, int(raw.get("position_index") or index))
                    query_cell = cells_by_query.get((normalized_channel, normalized(source_query)), {})
                    query_cell_id = str(query_cell.get("cell_id") or "")
                    query_family_ids = query_cell.get("query_family_ids") if isinstance(query_cell.get("query_family_ids"), list) else []
                    query_provenance = query_cell.get("provenance") if isinstance(query_cell.get("provenance"), list) else []
                    recall_identity = "|".join((
                        run_id, normalized_channel, query_cell_id, source_id,
                        str(page_number), str(position_index), normalized(source_query),
                    ))
                    recall_id = "recall_" + hashlib.sha256(recall_identity.encode("utf-8")).hexdigest()[:24]
                    conn.execute(
                        """
                        INSERT INTO agent_candidate_recalls
                        (recall_id,run_id,workflow_id,job_id,strategy_hash,strategy_artifact_id,
                         strategy_revision,query_plan_hash,query_cell_id,query_family_ids_json,
                         query_provenance_json,channel,source_candidate_id,
                         source_query,source_url,page_number,position_index,identity_key,candidate_name,
                         company,title,fit_score,fit_level,duplicate_state,exclusion_reason,detail_status,
                         candidate_id,job_candidate_id,raw_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(recall_id) DO UPDATE SET
                          strategy_hash=excluded.strategy_hash,
                          strategy_artifact_id=excluded.strategy_artifact_id,
                          strategy_revision=excluded.strategy_revision,
                          query_plan_hash=excluded.query_plan_hash,
                          query_family_ids_json=excluded.query_family_ids_json,
                          query_provenance_json=excluded.query_provenance_json,
                          fit_score=excluded.fit_score,fit_level=excluded.fit_level,
                          duplicate_state=excluded.duplicate_state,exclusion_reason=excluded.exclusion_reason,
                          detail_status=excluded.detail_status,candidate_id=excluded.candidate_id,
                          job_candidate_id=excluded.job_candidate_id,raw_json=excluded.raw_json,
                          updated_at=datetime('now','localtime')
                        """,
                        (
                            recall_id, run_id, workflow_id or None, job_id, strategy_hash,
                            strategy_artifact_id or None, strategy_revision, query_plan_hash,
                            query_cell_id, json.dumps(query_family_ids, ensure_ascii=False),
                            json.dumps(query_provenance, ensure_ascii=False), normalized_channel,
                            source_id, source_query,
                            str(raw.get("resume_url") or raw.get("source_url") or raw.get("url") or ""),
                            page_number, position_index, identity_key, name, company, title, score,
                            str(raw.get("fit_level") or "") or None, state, exclusion_reason,
                            str(
                                staged_item.get("resume_capture_status")
                                or staged_item.get("detail_status")
                                or raw.get("resume_capture_status")
                                or raw.get("detail_status")
                                or "not_requested"
                            ),
                            int(receipt.get("candidate_id") or 0) or None,
                            int(receipt.get("job_candidate_id") or 0) or None,
                            json.dumps(raw, ensure_ascii=False),
                        ),
                    )
                    stored += 1
                    by_state[state] = by_state.get(state, 0) + 1
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "stored": stored, "run_id": run_id, "by_state": by_state}

    def _build_coverage_certificate(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        query_plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Issue an auditable coverage certificate without claiming a hidden platform population."""
        job_id = self._job_id(client, job)
        conn = self.service._connect()
        try:
            cell_rows = conn.execute(
                "SELECT * FROM agent_sourcing_query_cells WHERE run_id=? ORDER BY priority,cell_id",
                (run_id,),
            ).fetchall()
            recall_row = conn.execute(
                """
                SELECT COUNT(*) AS raw_occurrences,
                       COUNT(DISTINCT channel || ':' || COALESCE(NULLIF(source_candidate_id,''),identity_key)) AS channel_unique_identities,
                       COUNT(DISTINCT CASE
                           WHEN REPLACE(identity_key,'|','')<>'' THEN identity_key
                           ELSE channel || ':' || source_candidate_id END) AS global_unique_identities,
                       SUM(CASE WHEN duplicate_state IN ('existing','existing_relation','batch_duplicate') THEN 1 ELSE 0 END) AS duplicate_occurrences,
                       SUM(CASE WHEN exclusion_reason='score_below_threshold' THEN 1 ELSE 0 END) AS below_threshold,
                       SUM(CASE WHEN query_cell_id='' THEN 1 ELSE 0 END) AS unmapped_occurrences,
                       COUNT(DISTINCT CASE WHEN job_candidate_id IS NOT NULL THEN job_candidate_id END) AS formally_intaked,
                       SUM(CASE WHEN detail_status='complete' THEN 1 ELSE 0 END) AS detail_complete,
                       SUM(CASE WHEN detail_status='partial' THEN 1 ELSE 0 END) AS detail_partial,
                       SUM(CASE WHEN detail_status='failed' THEN 1 ELSE 0 END) AS detail_failed
                FROM agent_candidate_recalls WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
            ledger_rows = conn.execute(
                """
                SELECT query_cell_id,COUNT(*) AS occurrences
                FROM agent_candidate_recalls
                WHERE run_id=? AND query_cell_id<>''
                GROUP BY query_cell_id
                """,
                (run_id,),
            ).fetchall()
            assessment_count = int(conn.execute(
                """
                SELECT COUNT(DISTINCT a.job_candidate_id)
                FROM agent_candidate_recalls r
                JOIN agent_candidate_assessments a ON a.job_candidate_id=r.job_candidate_id AND a.is_current=1
                WHERE r.run_id=?
                """,
                (run_id,),
            ).fetchone()[0])

            status_counts = {
                status: 0 for status in ("pending", "exhausted", "platform_capped", "blocked", "failed")
            }
            state_by_cell: dict[str, dict[str, Any]] = {}
            ledger_by_cell = {str(row["query_cell_id"]): int(row["occurrences"] or 0) for row in ledger_rows}
            executed = 0
            platform_totals: dict[str, dict[str, int | None]] = {}
            for row in cell_rows:
                item = _row(row)
                state_by_cell[str(item.get("cell_id") or "")] = item
                status = str(item.get("status") or "")
                if status in status_counts:
                    status_counts[status] += 1
                if status in {"exhausted", "platform_capped", "failed"} or int(item.get("pages_fetched") or 0) > 0:
                    executed += 1
                channel = str(item.get("channel") or "unknown")
                totals = platform_totals.setdefault(
                    channel,
                    {"reported_query_total": 0, "reported_total_known_cells": 0, "extracted_occurrences": 0},
                )
                if item.get("reported_total") is not None:
                    totals["reported_query_total"] = int(totals["reported_query_total"] or 0) + int(item["reported_total"])
                    totals["reported_total_known_cells"] = int(totals["reported_total_known_cells"] or 0) + 1
                totals["extracted_occurrences"] = int(totals["extracted_occurrences"] or 0) + int(item.get("extracted_count") or 0)

            expected_extracted = sum(int(item.get("extracted_count") or 0) for item in state_by_cell.values())
            mapped_occurrences = sum(ledger_by_cell.values())
            unmapped_occurrences = int(_row(recall_row).get("unmapped_occurrences") or 0)
            mismatched_cells = sum(
                int(item.get("extracted_count") or 0) != int(ledger_by_cell.get(cell_id, 0))
                for cell_id, item in state_by_cell.items()
            )
            evidence_integrity_passed = bool(
                unmapped_occurrences == 0
                and expected_extracted == mapped_occurrences
                and mismatched_cells == 0
            )

            all_companies: set[str] = set()
            all_groups: set[str] = set()
            executed_companies: set[str] = set()
            executed_groups: set[str] = set()
            for cell in query_plan.get("cells") or []:
                if not isinstance(cell, dict):
                    continue
                state = state_by_cell.get(str(cell.get("cell_id") or ""), {})
                was_executed = str(state.get("status") or "") in {"exhausted", "platform_capped", "failed"} or int(state.get("pages_fetched") or 0) > 0
                for ref in cell.get("provenance") or []:
                    if not isinstance(ref, dict):
                        continue
                    company = str(ref.get("company") or "").strip()
                    group = str(ref.get("group") or "").strip()
                    if company:
                        all_companies.add(company)
                        if was_executed:
                            executed_companies.add(company)
                    if group:
                        all_groups.add(group)
                        if was_executed:
                            executed_groups.add(group)

            approved = len([cell for cell in query_plan.get("cells") or [] if isinstance(cell, dict)])
            dimensions = query_plan.get("dimensions") if isinstance(query_plan.get("dimensions"), dict) else {}
            semantics = (
                query_plan.get("execution_semantics")
                if isinstance(query_plan.get("execution_semantics"), dict)
                else {}
            )
            evaluation_modes = (
                semantics.get("evaluation_constraints")
                if isinstance(semantics.get("evaluation_constraints"), dict)
                else {}
            )
            dimension_execution = {
                "retrieval_axes": semantics.get("retrieval_axes") or ["channel", "query"],
                "platform_filters_applied": semantics.get("platform_filters") or [],
                "dimensions": {
                    key: {
                        "approved_values": [str(value) for value in dimensions.get(key) or []],
                        "retrieval_filter_applied": False,
                        "evaluation_mode": str(evaluation_modes.get(key) or "post_recall_evaluation"),
                    }
                    for key in ("locations", "levels", "scenarios")
                },
            }
            if not evidence_integrity_passed:
                coverage_status = "coverage_unknown"
                defensible_claim = "查询执行记录与原始召回台账不一致，候选人覆盖未知"
            elif approved > 0 and status_counts["exhausted"] == approved:
                coverage_status = "approved_query_cells_exhausted"
                defensible_claim = "已穷尽批准的渠道关键词查询单元；地点、职级、场景未作为平台筛选执行"
            elif status_counts["platform_capped"]:
                coverage_status = "platform_truncated"
                defensible_claim = "已执行部分批准查询单元，但平台截断导致候选人总体覆盖未知"
            else:
                coverage_status = "coverage_unknown"
                defensible_claim = "批准的渠道关键词查询单元尚未完全执行，候选人总体覆盖未知"
            unknown_reasons: list[str] = []
            if status_counts["platform_capped"]:
                unknown_reasons.append("platform_truncated")
            if status_counts["blocked"]:
                unknown_reasons.append("blocked_query_cells")
            if status_counts["failed"]:
                unknown_reasons.append("failed_query_cells")
            if status_counts["pending"]:
                unknown_reasons.append("pending_query_cells")
            if not evidence_integrity_passed:
                unknown_reasons.append("recall_ledger_mismatch")
            unknown_reasons.append("platform_candidate_population_denominator_unavailable")

            recall = _row(recall_row)
            certificate_id = "coverage_" + hashlib.sha256(
                f"{run_id}|{query_plan.get('plan_hash') or ''}".encode("utf-8")
            ).hexdigest()[:24]
            certificate = {
                "schema_version": "coverage_certificate_v1",
                "certificate_id": certificate_id,
                "run_id": run_id,
                "workflow_id": workflow_id,
                "job_id": job_id,
                "plan_hash": str(query_plan.get("plan_hash") or ""),
                "issued_at": datetime.now().isoformat(timespec="seconds"),
                "coverage_status": coverage_status,
                "strategy_elements": {
                    "companies_approved": len(all_companies),
                    "companies_executed": len(executed_companies),
                    "keyword_groups_approved": len(all_groups),
                    "keyword_groups_executed": len(executed_groups),
                },
                "dimension_execution": dimension_execution,
                "query_cells": {
                    "approved": approved,
                    "executed": executed,
                    **status_counts,
                },
                "platform_query_totals": platform_totals,
                "candidate_recall": {
                    "raw_occurrences": int(recall.get("raw_occurrences") or 0),
                    "unique_identities": int(recall.get("global_unique_identities") or 0),
                    "global_unique_identities": int(recall.get("global_unique_identities") or 0),
                    "channel_unique_identities": int(recall.get("channel_unique_identities") or 0),
                    "duplicate_occurrences": int(recall.get("duplicate_occurrences") or 0),
                    "below_threshold": int(recall.get("below_threshold") or 0),
                    "formally_intaked": int(recall.get("formally_intaked") or 0),
                },
                "evidence_integrity": {
                    "passed": evidence_integrity_passed,
                    "expected_extracted_occurrences": expected_extracted,
                    "mapped_recall_occurrences": mapped_occurrences,
                    "unmapped_recall_occurrences": unmapped_occurrences,
                    "mismatched_query_cells": mismatched_cells,
                },
                "detail_completeness": {
                    "complete": int(recall.get("detail_complete") or 0),
                    "partial": int(recall.get("detail_partial") or 0),
                    "failed": int(recall.get("detail_failed") or 0),
                },
                "assessment": {"completed_unique_candidates": assessment_count},
                "claims": {
                    "all_candidates_covered": False,
                    "defensible_claim": defensible_claim,
                    "coverage_unknown_reasons": list(dict.fromkeys(unknown_reasons)),
                },
            }
            conn.execute(
                """
                INSERT INTO agent_sourcing_coverage_certificates
                (certificate_id,run_id,workflow_id,job_id,plan_hash,coverage_status,certificate_json)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                  certificate_id=excluded.certificate_id,workflow_id=excluded.workflow_id,
                  job_id=excluded.job_id,plan_hash=excluded.plan_hash,
                  coverage_status=excluded.coverage_status,certificate_json=excluded.certificate_json,
                  issued_at=datetime('now','localtime')
                """,
                (
                    certificate_id, run_id, workflow_id or None, job_id,
                    str(query_plan.get("plan_hash") or ""), coverage_status,
                    json.dumps(certificate, ensure_ascii=False),
                ),
            )
            conn.commit()
            return certificate
        finally:
            conn.close()

    def _persist_sourcing_attributions(
        self, applied: dict[str, Any], strategy: dict[str, Any], workflow_id: str, client: str, job: str,
        *, run_id: str = "", query_plan: dict[str, Any] | None = None,
        strategy_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        staged = applied.get("staged") if isinstance(applied.get("staged"), dict) else {}
        accepted = staged.get("accepted") if isinstance(staged.get("accepted"), list) else []
        intake = applied.get("intake") if isinstance(applied.get("intake"), dict) else {}
        receipts = intake.get("receipts") if isinstance(intake.get("receipts"), list) else []
        strategy_channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
        query_meta: dict[tuple[str, str], dict[str, Any]] = {}
        for channel, entries in strategy_channels.items():
            for entry in entries if isinstance(entries, list) else []:
                item = entry if isinstance(entry, dict) else {"query": entry}
                query = " ".join(str(item.get("query") or "").split())
                if query:
                    query_meta[(str(channel), query)] = item
        query_plan = query_plan if isinstance(query_plan, dict) else {}
        strategy_snapshot = strategy_snapshot if isinstance(strategy_snapshot, dict) else {}
        approved_strategy_hash = str(strategy_snapshot.get("strategy_hash") or "")
        strategy_hash = approved_strategy_hash or (
            hashlib.sha256(json.dumps(strategy, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if strategy else ""
        )
        strategy_model = str((strategy.get("generation") or {}).get("model") or "") if isinstance(strategy.get("generation"), dict) else ""
        stored = 0
        channel_new: dict[str, int] = {}
        conn = self.service._connect()
        try:
            for index, candidate in enumerate(accepted):
                item = candidate if isinstance(candidate, dict) else {}
                receipt = (
                    receipts[index]
                    if index < len(receipts) and isinstance(receipts[index], dict)
                    else {}
                )
                job_candidate_id = int(receipt.get("job_candidate_id") or 0)
                if not job_candidate_id:
                    continue
                channel = str(item.get("channel") or item.get("source") or "unknown").lower()
                query = " ".join(str(item.get("source_query") or "").split()) or "未记录关键词"
                meta = query_meta.get((channel, query), {})
                recall = conn.execute(
                    """
                    SELECT query_cell_id,query_family_ids_json,query_provenance_json
                    FROM agent_candidate_recalls
                    WHERE run_id=? AND job_candidate_id=? AND channel=? AND source_query=?
                    ORDER BY id LIMIT 1
                    """,
                    (run_id, job_candidate_id, channel, query),
                ).fetchone() if run_id else None
                provenance = _loads(recall["query_provenance_json"], []) if recall else []
                primary = provenance[0] if provenance and isinstance(provenance[0], dict) else {}
                source_round = str(
                    meta.get("round") or primary.get("tier") or primary.get("group")
                    or primary.get("kind") or ""
                )
                source_purpose = str(
                    meta.get("purpose") or primary.get("targets") or primary.get("path")
                    or primary.get("company") or ""
                )
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_attributions
                    (job_candidate_id,candidate_id,job_id,workflow_id,strategy_hash,strategy_model,
                     channel,source_query,source_round,source_purpose)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_candidate_id,channel,source_query) DO UPDATE SET
                      workflow_id=COALESCE(excluded.workflow_id,workflow_id),
                      strategy_hash=COALESCE(NULLIF(excluded.strategy_hash,''),strategy_hash),
                      strategy_model=COALESCE(NULLIF(excluded.strategy_model,''),strategy_model),
                      source_round=COALESCE(NULLIF(excluded.source_round,''),source_round),
                      source_purpose=COALESCE(NULLIF(excluded.source_purpose,''),source_purpose),
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        job_candidate_id, int(receipt.get("candidate_id") or 0) or None,
                        self._job_id(client, job), workflow_id or None, strategy_hash or None, strategy_model or None,
                        channel, query, source_round, source_purpose,
                    ),
                )
                stored += 1
                channel_new[channel] = channel_new.get(channel, 0) + 1
            conn.commit()
        finally:
            conn.close()
        return {"stored": stored, "strategy_hash": strategy_hash, "workflow_id": workflow_id, "channel_new": channel_new}

    def _persist_sourcing_funnel(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        channel_runs: list[dict[str, Any]],
        channel_candidates: dict[str, list[Any]],
        applied: dict[str, Any],
        attributions: dict[str, Any],
        company_vocab: set[str] | None = None,
    ) -> dict[str, Any]:
        """每个 run×channel 落一行寻访漏斗，并把 0 结果归因回写到 channel_runs 条目上。"""
        staged = applied.get("staged") if isinstance(applied.get("staged"), dict) else {}
        intake_dups: dict[str, int] = {}
        for key in ("existing", "batch_duplicates"):
            entries = staged.get(key) if isinstance(staged.get(key), list) else []
            for entry in entries:
                item = entry if isinstance(entry, dict) else {}
                dup_channel = str(item.get("channel") or item.get("source") or "unknown").lower()
                intake_dups[dup_channel] = intake_dups.get(dup_channel, 0) + 1
        channel_new = attributions.get("channel_new") if isinstance(attributions.get("channel_new"), dict) else {}
        job_id = self._job_id(client, job)
        rows: list[dict[str, Any]] = []
        for run in channel_runs:
            channel = str(run.get("channel") or "unknown").lower()
            status = str(run.get("status") or "completed")
            result = run.get("result") if isinstance(run.get("result"), dict) else {}
            candidates = [item for item in channel_candidates.get(channel) or [] if isinstance(item, dict)]
            rounds = [entry for entry in result.get("rounds") or [] if isinstance(entry, dict)]
            detail = result.get("detail_capture") if isinstance(result.get("detail_capture"), dict) else {}
            recall = sum(_round_int(entry, "result_count") for entry in rounds)
            extracted = sum(_round_int(entry, "extracted_count") for entry in rounds)
            if extracted <= 0 and not rounds:
                extracted = len(candidates)
            scored = [item for item in candidates if item.get("fit_score") is not None]
            high_score = 0
            for item in scored:
                try:
                    if int(item.get("fit_score") or 0) >= 65:
                        high_score += 1
                except (TypeError, ValueError):
                    continue
            zero_attribution = ""
            if not candidates:
                zero_attribution = classify_zero_result(
                    channel,
                    status,
                    result,
                    dedupe_count=max(0, extracted - len(candidates)),
                    company_vocab=company_vocab,
                )
                run["zero_attribution"] = zero_attribution
            rows.append(
                {
                    "run_id": run_id,
                    "workflow_id": workflow_id or None,
                    "job_id": job_id,
                    "client": client,
                    "job": job,
                    "channel": channel,
                    "status": status,
                    "query_count": len(rounds),
                    "queries_json": json.dumps(rounds, ensure_ascii=False),
                    "recall_count": recall,
                    "extracted_count": extracted,
                    "dedupe_count": max(0, extracted - len(candidates)),
                    "unique_count": len(candidates),
                    "detail_complete": _round_int(detail, "complete"),
                    "detail_partial": _round_int(detail, "partial"),
                    "detail_failed": _round_int(detail, "failed"),
                    "intake_duplicate_count": int(intake_dups.get(channel, 0)),
                    "intake_new_count": int(channel_new.get(channel, 0) or 0),
                    "assessed_count": len(scored),
                    "high_score_count": high_score,
                    "zero_attribution": zero_attribution or None,
                    "error": _trim_error(result.get("error")) or None,
                }
            )
        conn = self.service._connect()
        try:
            for row in rows:
                cell_rows = conn.execute(
                    """
                    SELECT cell_id,query,status,reported_total,pages_fetched,extracted_count,
                           unique_count,terminal_reason,last_error
                    FROM agent_sourcing_query_cells
                    WHERE run_id=? AND channel=?
                    ORDER BY priority,cell_id
                    """,
                    (run_id, row["channel"]),
                ).fetchall()
                recall_stats = _row(conn.execute(
                    """
                    SELECT COUNT(*) AS raw_occurrences,
                           COUNT(DISTINCT CASE
                               WHEN REPLACE(identity_key,'|','')<>'' THEN identity_key
                               ELSE channel || ':' || source_candidate_id END) AS unique_identities,
                           SUM(CASE WHEN detail_status='complete' THEN 1 ELSE 0 END) AS detail_complete,
                           SUM(CASE WHEN detail_status='partial' THEN 1 ELSE 0 END) AS detail_partial,
                           SUM(CASE WHEN detail_status='failed' THEN 1 ELSE 0 END) AS detail_failed,
                           SUM(CASE WHEN duplicate_state IN ('existing','existing_relation','batch_duplicate') THEN 1 ELSE 0 END) AS intake_duplicates,
                           COUNT(DISTINCT CASE WHEN duplicate_state='accepted' AND job_candidate_id IS NOT NULL THEN job_candidate_id END) AS intake_new,
                           COUNT(DISTINCT CASE WHEN fit_score IS NOT NULL
                               THEN COALESCE(NULLIF(source_candidate_id,''),identity_key) END) AS assessed,
                           COUNT(DISTINCT CASE WHEN fit_score>=65
                               THEN COALESCE(NULLIF(source_candidate_id,''),identity_key) END) AS high_score
                    FROM agent_candidate_recalls
                    WHERE run_id=? AND channel=?
                    """,
                    (run_id, row["channel"]),
                ).fetchone())
                if cell_rows:
                    cell_items = [_row(item) for item in cell_rows]
                    statuses = {str(item.get("status") or "") for item in cell_items}
                    if "failed" in statuses:
                        row["status"] = "failed"
                    elif "blocked" in statuses:
                        row["status"] = "blocked"
                    elif "platform_capped" in statuses:
                        row["status"] = "platform_capped"
                    elif statuses == {"exhausted"}:
                        row["status"] = "completed"
                    row["query_count"] = len(cell_items)
                    row["queries_json"] = json.dumps(cell_items, ensure_ascii=False)
                    row["recall_count"] = sum(
                        int(item.get("reported_total") or 0)
                        for item in cell_items if item.get("reported_total") is not None
                    )
                    row["extracted_count"] = sum(int(item.get("extracted_count") or 0) for item in cell_items)
                raw_occurrences = int(recall_stats.get("raw_occurrences") or 0)
                unique_identities = int(recall_stats.get("unique_identities") or 0)
                row["dedupe_count"] = max(0, int(row["extracted_count"]) - unique_identities)
                row["unique_count"] = unique_identities
                row["detail_complete"] = max(
                    int(row["detail_complete"]), int(recall_stats.get("detail_complete") or 0),
                )
                row["detail_partial"] = max(
                    int(row["detail_partial"]), int(recall_stats.get("detail_partial") or 0),
                )
                row["detail_failed"] = max(
                    int(row["detail_failed"]), int(recall_stats.get("detail_failed") or 0),
                )
                row["intake_duplicate_count"] = max(
                    int(row["intake_duplicate_count"]), int(recall_stats.get("intake_duplicates") or 0),
                )
                row["intake_new_count"] = int(recall_stats.get("intake_new") or 0)
                row["assessed_count"] = int(recall_stats.get("assessed") or 0)
                row["high_score_count"] = int(recall_stats.get("high_score") or 0)
                if raw_occurrences > 0:
                    row["zero_attribution"] = None
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_funnel
                    (run_id,workflow_id,job_id,client,job,channel,status,query_count,queries_json,
                     recall_count,extracted_count,dedupe_count,unique_count,
                     detail_complete,detail_partial,detail_failed,
                     intake_duplicate_count,intake_new_count,assessed_count,high_score_count,
                     zero_attribution,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,channel) DO UPDATE SET
                      workflow_id=COALESCE(excluded.workflow_id,workflow_id),
                      job_id=excluded.job_id,
                      status=excluded.status,
                      query_count=excluded.query_count,
                      queries_json=excluded.queries_json,
                      recall_count=excluded.recall_count,
                      extracted_count=excluded.extracted_count,
                      dedupe_count=excluded.dedupe_count,
                      unique_count=excluded.unique_count,
                      detail_complete=excluded.detail_complete,
                      detail_partial=excluded.detail_partial,
                      detail_failed=excluded.detail_failed,
                      intake_duplicate_count=excluded.intake_duplicate_count,
                      intake_new_count=excluded.intake_new_count,
                      assessed_count=excluded.assessed_count,
                      high_score_count=excluded.high_score_count,
                      zero_attribution=excluded.zero_attribution,
                      error=excluded.error,
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        row["run_id"], row["workflow_id"], row["job_id"], row["client"], row["job"],
                        row["channel"], row["status"], row["query_count"], row["queries_json"],
                        row["recall_count"], row["extracted_count"], row["dedupe_count"], row["unique_count"],
                        row["detail_complete"], row["detail_partial"], row["detail_failed"],
                        row["intake_duplicate_count"], row["intake_new_count"],
                        row["assessed_count"], row["high_score_count"],
                        row["zero_attribution"], row["error"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "stored": len(rows), "run_id": run_id}

    def _record_sourcing_funnel_failure(
        self,
        *,
        run_id: str,
        workflow_id: str,
        client: str,
        job: str,
        channel: str,
        error: str,
    ) -> None:
        """渠道 runner 在合并前直接失败时，尽力留下一行失败漏斗（绝不掩盖原始异常）。"""
        try:
            conn = self.service._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO agent_sourcing_funnel
                    (run_id,workflow_id,job_id,client,job,channel,status,zero_attribution,error)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id,channel) DO UPDATE SET
                      status=excluded.status,
                      zero_attribution=excluded.zero_attribution,
                      error=excluded.error,
                      updated_at=datetime('now','localtime')
                    """,
                    (
                        run_id, workflow_id or None, self._job_id(client, job), client, job,
                        channel, "failed", classify_zero_result(channel, "failed", {"error": _trim_error(error)}), _trim_error(error),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
