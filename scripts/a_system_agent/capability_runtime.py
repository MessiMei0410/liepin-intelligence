from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import query_builders
from .capability_runtime_base import (  # noqa: F401 模块级兼容 re-export（既有测试/调用方不变）
    MULTICHANNEL,
    LIEPIN_SEARCH,
    RESUME_BACKFILL,
    XSAAS_SEARCH,
    OPENCLI_SHADOW,
    LIEPIN_OUTREACH,
    LIEPIN_PUBLISH,
    MATCHING_REPORT,
    JIASHI_REPORT,
    JIASHI_AUDIT,
    SALARY_REPORT,
    JIASHI_TEMPLATE,
    DEFAULT_SOURCING_CELL_BATCH_SIZE,
    MAX_SOURCING_CELL_BATCH_SIZE,
    DEFAULT_PAGINATION_CONTINUATION_HEADROOM,
    MAX_SOURCING_CONTINUATION_BATCHES,
    SERVICE_HANDLED_CAPABILITY_IDS,
    EXTERNAL_EXECUTION_CAPABILITY_IDS,
    assert_workflow_capabilities_resolvable,
    _loads,
    _row,
    ZERO_RESULT_ATTRIBUTIONS,
    ZERO_RESULT_ATTRIBUTION_LABELS,
    _round_int,
    _trim_error,
    _revision_consultant_evidence,
    _consultant_constraint_items,
    _lock_consultant_constraints,
    _locked_constraint_conflicts,
    CommandExecutionError,
    ExternalPhaseError,
    ExternalExecutionCancelled,
    _json_object,
    _command_failure_summary,
    XSAAS_QUERY_MAX_TERMS,
    XSAAS_QUERY_MAX_COUNT,
    LIEPIN_QUERY_MAX_TERMS,
    LIEPIN_QUERY_MAX_COUNT,
    adapt_channel_queries,
    POOL_SATURATED_DEDUPE_RATE,
    _has_query_build_error,
    classify_zero_result,
    _slug,
    _list_text,
    RunnerBaseMixin,
)
from .capability_runtime_assessment import RunnerAssessmentMixin
from .capability_runtime_delivery import RunnerDeliveryMixin
from .capability_runtime_jobs import RunnerJobsMixin
from .capability_runtime_search import RunnerSearchMixin


class RecruitingCapabilityRuntime(
    RunnerBaseMixin,
    RunnerSearchMixin,
    RunnerAssessmentMixin,
    RunnerJobsMixin,
    RunnerDeliveryMixin,
):
    """Deterministic implementations behind the ASA capability registry.

    Mixin 组合 facade：生命周期/执行基元在 RunnerBaseMixin，寻访在
    RunnerSearchMixin，共享事实原语在 RunnerAssessmentMixin，岗位库在
    RunnerJobsMixin，交付在 RunnerDeliveryMixin。execute / execute_external
    与 OpenCLI 主召回簇（_query_text … _run_opencli_shadow）物理保留在本文件：
    tests/test_opencli_primary_recall.py 直接读取源文件断言。
    """

    def execute(self, capability_id: str, context: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"run_{capability_id}", None)
        if handler is None:
            spec = self.service.skills.get(capability_id) if capability_id in SERVICE_HANDLED_CAPABILITY_IDS else None
            if spec is not None:
                # 注册在服务层（workflow_handler）实现的能力：走已注册 handler，保证调用语义完整。
                return spec.handler(context, inputs)
            available = "、".join(sorted(self.deterministic_runner_ids() | set(SERVICE_HANDLED_CAPABILITY_IDS)))
            raise ValueError(f"能力没有可用的确定性 Runner 或服务层处理器：{capability_id}；可用能力：{available}")
        return handler(context, inputs)

    def execute_external(self, capability_id: str, request: dict[str, Any]) -> dict[str, Any]:
        if capability_id not in EXTERNAL_EXECUTION_CAPABILITY_IDS:
            supported = "、".join(sorted(EXTERNAL_EXECUTION_CAPABILITY_IDS))
            raise ValueError(f"能力不支持后台渠道执行：{capability_id}；仅支持：{supported}")
        client, job = str(request.get("client") or ""), str(request.get("job") or "")
        if not client or not job:
            raise ValueError("寻访任务缺少客户或岗位")
        self._ensure_external_request_active(request)
        cancel_check = (
            (lambda: self._external_request_cancelled(request))
            if request.get("_workflow_step_id")
            else None
        )
        audit_only_result = request.get("_audit_only_result")
        if isinstance(audit_only_result, dict):
            sync_script = Path("/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py")
            sync = self._run_external(
                [self.python, str(sync_script), "--client", client, "--job", job, "--no-open"],
                300,
                cancel_check=cancel_check,
            )
            return {
                **audit_only_result,
                "verified": True,
                "audit": {
                    "ok": True,
                    "summary": "A 系统收尾审计通过",
                    "returncode": sync.returncode,
                    "recovered_without_channel_rerun": True,
                },
            }
        approved_snapshot = request.get("strategy_snapshot") if isinstance(request.get("strategy_snapshot"), dict) else {}
        query_plan = request.get("query_plan_v1") if isinstance(request.get("query_plan_v1"), dict) else {}
        if not query_plan and isinstance(approved_snapshot.get("query_plan_v1"), dict):
            query_plan = approved_snapshot["query_plan_v1"]
        plan_ok, plan_errors = query_builders.validate_query_plan_v1(query_plan)
        approved_plan_hash = str(
            request.get("query_plan_hash") or approved_snapshot.get("query_plan_hash") or ""
        )
        if not plan_ok or not approved_plan_hash:
            detail = "；".join(plan_errors) if plan_errors else "缺少审批计划哈希"
            raise ValueError(f"缺少有效且批准的 query_plan_v1：{detail}")
        if not secrets.compare_digest(approved_plan_hash, str(query_plan.get("plan_hash") or "")):
            raise ValueError("批准的 query_plan_v1 哈希与执行请求不一致")
        target = max(1, min(int(request.get("target_count") or 10), 50))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = str(request.get("resume_run_id") or f"asa-source-{stamp}")
        candidates_path = self.output_dir / "sourcing" / f"{_slug(client)}-{_slug(job)}-{stamp}.json"
        liepin_path = candidates_path.with_name(candidates_path.stem + "-liepin.json")
        xsaas_path = candidates_path.with_name(candidates_path.stem + "-xsaas.json")
        liepin_raw_path = candidates_path.with_name(candidates_path.stem + "-liepin-raw.json")
        xsaas_raw_path = candidates_path.with_name(candidates_path.stem + "-xsaas-raw.json")
        liepin_queries_path = candidates_path.with_name(candidates_path.stem + "-liepin-queries.json")
        xsaas_queries_path = candidates_path.with_name(candidates_path.stem + "-xsaas-queries.json")
        candidates_path.parent.mkdir(parents=True, exist_ok=True)
        strategy = request.get("strategy") if isinstance(request.get("strategy"), dict) else {}
        query_groups = approved_snapshot.get("query_groups") if isinstance(approved_snapshot.get("query_groups"), list) else []
        ability_terms = {
            str(term).strip()
            for group in query_groups
            if isinstance(group, dict)
            for term in (group.get("terms") or [])
            if str(term).strip()
        }
        quality_min_score, quality_recommend_score = self._sourcing_score_thresholds(job, ability_terms)
        page_budget = self._channel_page_budget(request)
        resume_requested = bool(request.get("resume_run_id"))
        all_runnable_cells = (
            self._resume_query_cells(
                run_id,
                query_plan,
                max_retries=int(request.get("max_query_retries") or 3),
                max_platform_capped_retries=self._platform_capped_continuation_limit(request),
            )
            if resume_requested
            else [cell for cell in query_plan.get("cells") or [] if isinstance(cell, dict)]
        )
        # Every approved cell may already be terminal when a paused workflow is
        # resumed. In that case, run the normal empty-batch intake/audit path so
        # the workflow can advance to assessment instead of failing at the cursor.
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
        runnable_cells = all_runnable_cells[:cell_batch_size]
        executed_cell_ids = {
            str(cell.get("cell_id") or "") for cell in runnable_cells if isinstance(cell, dict)
        }
        execution_plan = {**query_plan, "cells": runnable_cells, "cell_count": len(runnable_cells)}
        liepin_queries = query_builders.query_plan_channel_entries(execution_plan, "liepin")
        xsaas_queries = query_builders.query_plan_channel_entries(execution_plan, "xsaas")
        company_terms = query_builders.query_plan_company_vocabulary(query_plan)
        liepin_queries_path.write_text(json.dumps({"queries": liepin_queries}, ensure_ascii=False, indent=2), encoding="utf-8")
        xsaas_queries_path.write_text(json.dumps({"queries": xsaas_queries}, ensure_ascii=False, indent=2), encoding="utf-8")
        # 并行跑两条渠道（OC1→production fallback），取代串行等待
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _oc1 = self._opencli_primary_enabled(request)
        _cdp = int(request.get("cdp_port") or 9223)
        _lim = max(12, target * 2)
        _det, _detail_args = self._liepin_detail_capture_options(request, target)
        _report: dict[str, Any] = {}

        def _run_liepin() -> tuple[str, dict[str, Any] | None]:
            if not liepin_queries:
                liepin_path.write_text("[]", encoding="utf-8")
                liepin_raw_path.write_text("[]", encoding="utf-8")
                return "resume_skipped", {"ok": True, "status": "resume_skipped", "rounds": []}
            eng = "production_fallback" if _oc1 else "production"
            res: dict[str, Any] | None = None
            if _oc1:
                eng = self._attempt_opencli_primary(
                    channel="liepin", client=client, job=job, port=_cdp,
                    queries_path=liepin_queries_path, output_path=liepin_path,
                    raw_output_path=liepin_raw_path,
                    limit=min(_lim, 24), detail_limit=_det, report=_report,
                    detail_args=_detail_args,
                    cancel_check=cancel_check)
                if eng == "opencli":
                    res = {**_report.get("liepin", {}), "ok": True, "recall_engine": "opencli"}
                elif eng == "opencli_partial":
                    primary_summary = _report.get("liepin", {})
                    primary_rows = _loads(liepin_path.read_text(encoding="utf-8"), [])
                    primary_raw = _loads(liepin_raw_path.read_text(encoding="utf-8"), [])
                    fallback_entries = self._opencli_fallback_entries(
                        liepin_queries, primary_summary, primary_raw,
                    )
                    fallback_queries_path = liepin_queries_path.with_name(
                        liepin_queries_path.stem + "-paginated.json"
                    )
                    fallback_path = liepin_path.with_name(liepin_path.stem + "-paginated.json")
                    fallback_raw_path = liepin_raw_path.with_name(
                        liepin_raw_path.stem + "-paginated.json"
                    )
                    fallback_queries_path.write_text(
                        json.dumps({"queries": fallback_entries}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    try:
                        fallback_result = self._run_external_json([
                            self.python, str(LIEPIN_SEARCH), "--client", client, "--position", job,
                            "--db", str(self.service.db_path), "--output-dir", str(candidates_path.parent),
                            "--port", str(_cdp), "--rounds", str(len(fallback_entries)),
                            "--max-cards", str(min(_lim, 24)), "--min-score", str(quality_min_score), "--recommend-score", str(quality_recommend_score),
                            "--max-pages", str(page_budget),
                            "--capture-links", "--capture-details", "--detail-limit", str(_det),
                            *_detail_args,
                            "--no-open-links", "--dry-run", "--json-output", str(fallback_path),
                            "--raw-json-output", str(fallback_raw_path),
                            "--queries-json", str(fallback_queries_path),
                        ], 900, cancel_check=cancel_check)
                    except ExternalExecutionCancelled:
                        raise
                    except Exception as exc:
                        fallback_result = {
                            "ok": False, "status": "blocked", "error": _trim_error(exc), "rounds": [],
                        }
                        fallback_path.write_text("[]", encoding="utf-8")
                        fallback_raw_path.write_text("[]", encoding="utf-8")
                    res, merged_rows, merged_raw = self._merge_opencli_completion(
                        channel="liepin",
                        primary_summary=primary_summary,
                        fallback_result=fallback_result,
                        primary_rows=primary_rows,
                        fallback_rows=_loads(fallback_path.read_text(encoding="utf-8"), []),
                        primary_raw=primary_raw,
                        fallback_raw=_loads(fallback_raw_path.read_text(encoding="utf-8"), []),
                    )
                    liepin_path.write_text(json.dumps(merged_rows, ensure_ascii=False, indent=2), encoding="utf-8")
                    liepin_raw_path.write_text(json.dumps(merged_raw, ensure_ascii=False, indent=2), encoding="utf-8")
                    eng = "opencli_paginated"
            if res is None:
                try:
                    res = self._run_external_json([
                        self.python, str(LIEPIN_SEARCH), "--client", client, "--position", job,
                        "--db", str(self.service.db_path), "--output-dir", str(candidates_path.parent),
                        "--port", str(_cdp), "--rounds", str(len(liepin_queries)),
                        "--max-cards", str(min(_lim, 24)), "--min-score", str(quality_min_score), "--recommend-score", str(quality_recommend_score),
                        "--max-pages", str(page_budget),
                        "--capture-links", "--capture-details", "--detail-limit", str(_det),
                        *_detail_args,
                        "--no-open-links", "--dry-run", "--json-output", str(liepin_path),
                        "--raw-json-output", str(liepin_raw_path),
                        "--queries-json", str(liepin_queries_path),
                    ], 900, cancel_check=cancel_check)
                except ExternalExecutionCancelled:
                    raise
                except Exception as exc:
                    res = {"ok": False, "status": "blocked", "error": _trim_error(exc), "rounds": []}
                    liepin_path.write_text("[]", encoding="utf-8")
                    liepin_raw_path.write_text("[]", encoding="utf-8")
            return eng, res

        def _run_xsaas() -> tuple[str, dict[str, Any] | None]:
            if not xsaas_queries:
                xsaas_path.write_text("[]", encoding="utf-8")
                xsaas_raw_path.write_text("[]", encoding="utf-8")
                return "resume_skipped", {"ok": True, "status": "resume_skipped", "rounds": []}
            eng = "production_fallback" if _oc1 else "production"
            res: dict[str, Any] | None = None
            if _oc1:
                eng = self._attempt_opencli_primary(
                    channel="xsaas", client=client, job=job, port=_cdp,
                    queries_path=xsaas_queries_path, output_path=xsaas_path,
                    raw_output_path=xsaas_raw_path,
                    limit=min(_lim, 100), detail_limit=_det, report=_report,
                    cancel_check=cancel_check)
                if eng == "opencli":
                    res = {**_report.get("xsaas", {}), "ok": True, "recall_engine": "opencli"}
                elif eng == "opencli_partial":
                    primary_summary = _report.get("xsaas", {})
                    primary_rows = _loads(xsaas_path.read_text(encoding="utf-8"), [])
                    primary_raw = _loads(xsaas_raw_path.read_text(encoding="utf-8"), [])
                    fallback_entries = self._opencli_fallback_entries(
                        xsaas_queries, primary_summary, primary_raw,
                    )
                    fallback_queries_path = xsaas_queries_path.with_name(
                        xsaas_queries_path.stem + "-paginated.json"
                    )
                    fallback_path = xsaas_path.with_name(xsaas_path.stem + "-paginated.json")
                    fallback_raw_path = xsaas_raw_path.with_name(
                        xsaas_raw_path.stem + "-paginated.json"
                    )
                    fallback_queries_path.write_text(
                        json.dumps({"queries": fallback_entries}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    try:
                        fallback_result = self._run_external_json([
                            self.python, str(XSAAS_SEARCH), "--queries", str(fallback_queries_path),
                            "--output", str(fallback_path), "--port", str(_cdp),
                            "--raw-output", str(fallback_raw_path),
                            "--max-rows", str(min(_lim, 100)), "--db", str(self.service.db_path),
                            "--max-pages", str(page_budget),
                            "--client", client, "--job", job, "--min-score", str(quality_min_score),
                        ], 300, cancel_check=cancel_check)
                    except ExternalExecutionCancelled:
                        raise
                    except Exception as exc:
                        fallback_result = {"ok": False, "status": "blocked", "error": _trim_error(exc), "rounds": []}
                        fallback_path.write_text("[]", encoding="utf-8")
                        fallback_raw_path.write_text("[]", encoding="utf-8")
                    res, merged_rows, merged_raw = self._merge_opencli_completion(
                        channel="xsaas",
                        primary_summary=primary_summary,
                        fallback_result=fallback_result,
                        primary_rows=primary_rows,
                        fallback_rows=_loads(fallback_path.read_text(encoding="utf-8"), []),
                        primary_raw=primary_raw,
                        fallback_raw=_loads(fallback_raw_path.read_text(encoding="utf-8"), []),
                    )
                    xsaas_path.write_text(json.dumps(merged_rows, ensure_ascii=False, indent=2), encoding="utf-8")
                    xsaas_raw_path.write_text(json.dumps(merged_raw, ensure_ascii=False, indent=2), encoding="utf-8")
                    eng = "opencli_paginated"
            if res is None:
                try:
                    res = self._run_external_json([
                        self.python, str(XSAAS_SEARCH), "--queries", str(xsaas_queries_path),
                        "--output", str(xsaas_path), "--port", str(_cdp),
                        "--raw-output", str(xsaas_raw_path),
                        "--max-rows", str(min(_lim, 100)), "--db", str(self.service.db_path),
                        "--max-pages", str(page_budget),
                        "--client", client, "--job", job, "--min-score", str(quality_min_score),
                    ], 300, cancel_check=cancel_check)
                except ExternalExecutionCancelled:
                    raise
                except Exception:
                    res = {"ok": False, "status": "blocked", "error": _trim_error(sys.exc_info()[1])}
                    xsaas_path.write_text("[]", encoding="utf-8")
            return eng, res

        def _run_xsaas_guarded() -> tuple[str, dict[str, Any] | None]:
            # X-SaaS runners use isolated tabs marked with asa_search_runner=1.
            # A subprocess timeout cannot execute its own finally block, so the
            # parent removes only those owned tabs both before and after the run.
            from xsaas_candidate_search import close_runner_tabs

            try:
                close_runner_tabs(_cdp)
            except Exception:
                pass
            try:
                return _run_xsaas()
            finally:
                try:
                    close_runner_tabs(_cdp)
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=2) as _pool:
            _futures = {
                _pool.submit(_run_liepin): "liepin",
                _pool.submit(_run_xsaas_guarded): "xsaas",
            }
            _results: dict[str, tuple[str, dict[str, Any] | None]] = {}
            for _fut in as_completed(_futures):
                _results[_futures[_fut]] = _fut.result()

        liepin_engine, search = _results["liepin"]
        xsaas_engine, xsaas = _results["xsaas"]
        self._ensure_external_request_active(request)
        primary_channels = _report
        risk_stop_reason = self._channel_risk_stop_reason(search)
        opencli_shadow = (
            {
                "enabled": False,
                "mode": "read_only_shadow",
                "affects_intake": False,
                "reason": "channel_risk_hard_stop",
            }
            if risk_stop_reason
            else self._run_opencli_shadow(
                request=request,
                client=client,
                job=job,
                port=int(request.get("cdp_port") or 9223),
                limit=max(12, target * 2),
                liepin_queries=liepin_queries,
                xsaas_queries=xsaas_queries,
                liepin_path=liepin_path,
                xsaas_path=xsaas_path,
                artifact_path=candidates_path.with_name(candidates_path.stem + "-opencli-shadow.json"),
                liepin_queries_path=liepin_queries_path,
                xsaas_queries_path=xsaas_queries_path,
                skip_channels={
                    channel
                    for channel, engine in (("liepin", liepin_engine), ("xsaas", xsaas_engine))
                    if engine.startswith("opencli")
                },
                cancel_check=cancel_check,
            )
        )
        liepin_candidates = _loads(liepin_path.read_text(encoding="utf-8"), [])
        xsaas_candidates = _loads(xsaas_path.read_text(encoding="utf-8"), [])
        if not liepin_raw_path.exists():
            liepin_raw_path.write_text(json.dumps(liepin_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        if not xsaas_raw_path.exists():
            xsaas_raw_path.write_text(json.dumps(xsaas_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        liepin_raw_candidates = _loads(liepin_raw_path.read_text(encoding="utf-8"), [])
        xsaas_raw_candidates = _loads(xsaas_raw_path.read_text(encoding="utf-8"), [])
        combined = liepin_candidates + xsaas_candidates
        quality_rejected: list[dict[str, Any]] = []
        if quality_min_score >= 70:
            def candidate_score(item: dict[str, Any]) -> int:
                try:
                    return int(float(item.get("fit_score") or 0))
                except (TypeError, ValueError):
                    return 0

            quality_rejected = [
                item for item in combined
                if candidate_score(item) < quality_min_score
            ]
            combined = [
                item for item in combined
                if candidate_score(item) >= quality_min_score
            ]
        candidates_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        workflow_id = str(request.get("workflow_id") or "")

        def _normalize_run_result(raw: dict[str, Any] | None) -> dict[str, Any]:
            """把 runner 返回的 result.candidates 统一为数量，避免前端把列表当 0 处理。"""
            if not isinstance(raw, dict):
                return raw or {}
            normalized = dict(raw)
            candidates = normalized.get("candidates")
            if isinstance(candidates, list):
                normalized["candidates"] = len(candidates)
            return normalized

        channel_runs = [
            {"channel": "liepin", "status": "risk_stopped" if risk_stop_reason else "completed" if search.get("ok") else "blocked", "recall_engine": liepin_engine, "result": _normalize_run_result(search)},
            {"channel": "xsaas", "status": "completed" if xsaas.get("ok") else "blocked", "recall_engine": xsaas_engine, "result": _normalize_run_result(xsaas)},
        ]

        # Persist raw channel evidence before any formal candidate intake. The later
        # upserts only enrich these immutable occurrences with disposition receipts.
        raw_candidates = {"liepin": liepin_raw_candidates, "xsaas": xsaas_raw_candidates}
        recall_ledger = self._persist_candidate_recalls(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
            strategy_snapshot=approved_snapshot,
            raw_candidates=raw_candidates,
            applied={},
            min_score=quality_min_score,
        )
        query_cell_states = self._persist_query_cell_states(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
            channel_runs=channel_runs,
            executed_cell_ids=executed_cell_ids,
        )
        coverage_certificate = self._build_coverage_certificate(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
        )

        self._ensure_external_request_active(request)
        dry = self._run_external_json([self.python, str(MULTICHANNEL), "intake", "--db", str(self.service.db_path), "--client", client, "--job", job, "--input", str(candidates_path)], 120, cancel_check=cancel_check)
        recall_ledger = self._persist_candidate_recalls(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
            strategy_snapshot=approved_snapshot,
            raw_candidates=raw_candidates,
            applied=dry,
            min_score=quality_min_score,
        )
        self._ensure_external_request_active(request)
        applied = self._run_external_json([self.python, str(MULTICHANNEL), "intake", "--db", str(self.service.db_path), "--client", client, "--job", job, "--input", str(candidates_path), "--apply"], 180, cancel_check=cancel_check)
        attributions = self._persist_sourcing_attributions(
            applied, request.get("strategy") if isinstance(request.get("strategy"), dict) else {},
            workflow_id, client, job, run_id=run_id, query_plan=query_plan,
            strategy_snapshot=approved_snapshot,
        )
        # 入池后补抓：对已入池但还没有完整猎聘履历的人选自动补抓一轮，写入 source_profiles，
        # 使人选详情页与后续 candidate_batch_assessment 能直接拿到完整履历。失败不阻断寻访。
        resume_backfill: dict[str, Any] = {"status": "skipped", "reason": ""}
        if risk_stop_reason:
            resume_backfill["reason"] = "channel_risk_stop"
        elif str(os.environ.get("ASA_RESUME_BACKFILL", "on")).strip().lower() in {"0", "off", "false"}:
            resume_backfill["reason"] = "disabled"
        else:
            self._ensure_external_request_active(request)
            _, detail_flags = self._liepin_detail_capture_options(request, target)
            try:
                backfill_limit = int(os.environ.get("ASA_RESUME_BACKFILL_LIMIT", "40") or 40)
            except (TypeError, ValueError):
                backfill_limit = 40
            backfill_limit = max(1, min(backfill_limit, 100))
            try:
                resume_backfill = self._run_external_json(
                    [
                        self.python, str(RESUME_BACKFILL),
                        "--db", str(self.service.db_path),
                        "--client", client, "--job", job,
                        "--port", str(int(request.get("cdp_port") or 9223)),
                        "--limit", str(backfill_limit),
                        *detail_flags,
                    ],
                    120 + 25 * backfill_limit,
                    cancel_check=cancel_check,
                )
            except ExternalExecutionCancelled:
                raise
            except Exception as exc:
                resume_backfill = {"status": "failed", "error": str(exc)[:300]}
        recall_ledger = self._persist_candidate_recalls(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
            strategy_snapshot=approved_snapshot,
            raw_candidates=raw_candidates,
            applied=applied,
            min_score=quality_min_score,
        )
        coverage_certificate = self._build_coverage_certificate(
            run_id=run_id,
            workflow_id=workflow_id,
            client=client,
            job=job,
            query_plan=query_plan,
        )
        try:
            funnel = self._persist_sourcing_funnel(
                run_id=run_id,
                workflow_id=workflow_id,
                client=client,
                job=job,
                channel_runs=channel_runs,
                channel_candidates={"liepin": liepin_candidates, "xsaas": xsaas_candidates},
                applied=applied,
                attributions=attributions,
                company_vocab=company_terms,
            )
        except Exception as exc:
            funnel = {"ok": False, "stored": 0, "error": str(exc)[:500]}
        partial_result = {
            "verified": True,
            "run_id": run_id,
            "channel_runs": channel_runs,
            "opencli_shadow": opencli_shadow,
            "opencli_primary": {"enabled": _oc1, "channels": primary_channels},
            "intake": {"dry_run": dry, "applied": applied, "source_file": str(candidates_path)},
            "attributions": attributions,
            "resume_backfill": resume_backfill,
            "candidate_recall_ledger": recall_ledger,
            "query_cell_states": query_cell_states,
            "coverage_certificate": coverage_certificate,
            "sourcing_funnel": funnel,
            "quality_gate": {
                "minimum_score": quality_min_score,
                "rejected_before_intake": len(quality_rejected),
                "policy": "specialized_power_minimum_score" if quality_min_score >= 70 else "channel_default",
            },
            "channel_risk_stop": {
                "active": bool(risk_stop_reason),
                "channel": "liepin" if risk_stop_reason else "",
                "signal": risk_stop_reason,
                "message": "猎聘命中安全风险提示，已停止猎聘及后续分页。" if risk_stop_reason else "",
            },
            "audit": {"ok": False, "summary": "等待 A 系统收尾审计"},
        }
        sync_script = Path("/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py")
        try:
            sync = self._run_external([self.python, str(sync_script), "--client", client, "--job", job, "--no-open"], 300, cancel_check=cancel_check)
        except CommandExecutionError as exc:
            partial_result["audit"] = {
                "ok": False,
                "phase": "audit",
                "summary": str(exc),
                "detail": exc.detail,
            }
            raise ExternalPhaseError(
                f"寻访与入库已完成，但 A 系统收尾审计未通过：{exc}",
                phase="audit",
                partial_result=partial_result,
                detail=exc.detail,
            ) from exc
        learning = self._capture_search_learning(client, job, [*liepin_queries, *xsaas_queries])
        continuation = (
            {
                "summary": {
                    "scheduled": False,
                    "reason": "channel_risk_hard_stop",
                    "risk_signal": risk_stop_reason,
                    "remaining_cells": max(0, len(all_runnable_cells) - len(runnable_cells)),
                },
                "request": None,
            }
            if risk_stop_reason
            else self._sourcing_continuation(request=request, run_id=run_id, query_plan=query_plan)
        )
        final_result = {
            **partial_result,
            "audit": {
                "ok": True,
                "summary": "A 系统收尾审计通过",
                "returncode": sync.returncode,
            },
            "learning": learning,
            "continuation": continuation["summary"],
        }
        if continuation["request"] is not None:
            final_result["_continuation_request"] = continuation["request"]
        return final_result

    @staticmethod
    def _query_text(entries: list[Any]) -> str:
        if not entries:
            return ""
        first = entries[0]
        value = first.get("query") if isinstance(first, dict) else first
        return " ".join(str(value or "").split())

    @staticmethod
    def _query_entry_text(entry: Any) -> str:
        value = entry.get("query") if isinstance(entry, dict) else entry
        return " ".join(str(value or "").split())

    @classmethod
    def _opencli_fallback_entries(
        cls,
        entries: list[Any],
        summary: dict[str, Any],
        primary_rows: list[Any] | None = None,
    ) -> list[Any]:
        """Resume only OpenCLI query cells that did not prove exhaustion."""
        rounds = {
            cls._query_entry_text(item.get("query")): item
            for item in summary.get("rounds") or []
            if isinstance(item, dict) and cls._query_entry_text(item.get("query"))
        }
        fallback: list[Any] = []
        for entry in entries:
            query = cls._query_entry_text(entry)
            if not query:
                continue
            round_item = rounds.get(query)
            if round_item and round_item.get("terminal_state") == "exhausted":
                continue
            base = dict(entry) if isinstance(entry, dict) else {"query": query}
            extracted = _round_int(round_item, "extracted_count")
            cursor = round_item.get("cursor") if isinstance(round_item, dict) else None
            if (
                round_item
                and round_item.get("terminal_state") == "platform_capped"
                and extracted > 0
                and isinstance(cursor, dict)
                and int(cursor.get("page") or 0) > 1
            ):
                base["cursor"] = {"page": int(cursor["page"])}
                base["collected_before"] = extracted
                seen_keys = [
                    key
                    for item in primary_rows or []
                    if isinstance(item, dict)
                    and cls._query_entry_text(item.get("source_query") or item.get("query")) == query
                    and (key := cls._candidate_resume_key(item))
                ]
                if seen_keys:
                    base["seen_candidate_keys"] = list(dict.fromkeys(seen_keys))
            else:
                base.pop("cursor", None)
                base.pop("collected_before", None)
                base.pop("seen_candidate_keys", None)
            fallback.append(base)
        return fallback

    @staticmethod
    def _candidate_resume_key(item: dict[str, Any]) -> str:
        source_id = str(
            item.get("candidate_id") or item.get("resume_id") or item.get("res_id_encode")
            or item.get("xsaas_id") or ""
        ).strip()
        if source_id:
            return source_id
        return "|".join(
            " ".join(str(item.get(key) or "").split()).casefold()
            for key in ("name", "company", "title")
        )

    @staticmethod
    def _candidate_artifact_key(channel: str, item: dict[str, Any]) -> str:
        source_id = str(
            item.get("candidate_id") or item.get("resume_id") or item.get("res_id_encode")
            or item.get("xsaas_id") or item.get("resume_url") or item.get("source_url") or ""
        ).strip()
        if source_id:
            return f"{channel}:id:{source_id}"
        identity = "|".join(
            " ".join(str(item.get(key) or "").split()).casefold()
            for key in ("name", "company", "current_company", "title", "current_title")
        )
        return f"{channel}:identity:{identity}"

    @classmethod
    def _merge_opencli_completion(
        cls,
        *,
        channel: str,
        primary_summary: dict[str, Any],
        fallback_result: dict[str, Any],
        primary_rows: list[Any],
        fallback_rows: list[Any],
        primary_raw: list[Any],
        fallback_raw: list[Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Merge OpenCLI page 1 with paginated completion without losing occurrence evidence."""
        accepted_seen: set[str] = set()
        accepted: list[dict[str, Any]] = []
        for raw in [*primary_rows, *fallback_rows]:
            if not isinstance(raw, dict):
                continue
            key = cls._candidate_artifact_key(channel, raw)
            if key in accepted_seen:
                continue
            accepted_seen.add(key)
            accepted.append(raw)
        raw_rows = [item for item in [*primary_raw, *fallback_raw] if isinstance(item, dict)]

        fallback_rounds = {
            cls._query_entry_text(item.get("query")): item
            for item in fallback_result.get("rounds") or []
            if isinstance(item, dict) and cls._query_entry_text(item.get("query"))
        }
        merged_rounds: list[dict[str, Any]] = []
        merged_queries: set[str] = set()
        for raw_round in primary_summary.get("rounds") or []:
            if not isinstance(raw_round, dict):
                continue
            primary_round = dict(raw_round)
            query = cls._query_entry_text(primary_round.get("query"))
            merged_queries.add(query)
            fallback_round = fallback_rounds.get(query)
            if primary_round.get("terminal_state") == "exhausted" or not fallback_round:
                merged_rounds.append(primary_round)
                continue
            merged = dict(fallback_round)
            if (
                primary_round.get("terminal_state") == "platform_capped"
                and _round_int(primary_round, "extracted_count") > 0
            ):
                query_raw = [
                    item for item in raw_rows
                    if cls._query_entry_text(item.get("source_query") or item.get("query")) == query
                ]
                merged["extracted_count"] = len(query_raw)
                merged["unique_count"] = len({cls._candidate_artifact_key(channel, item) for item in query_raw})
                merged["pages_fetched"] = (
                    _round_int(primary_round, "pages_fetched")
                    + _round_int(fallback_round, "pages_fetched")
                )
                if fallback_round.get("result_count") is None:
                    merged["result_count"] = primary_round.get("result_count")
                merged["resumed_after_opencli"] = True
            merged_rounds.append(merged)
        merged_rounds.extend(
            dict(item)
            for query, item in fallback_rounds.items()
            if query not in merged_queries
        )
        result = {
            **fallback_result,
            "mode": "opencli_primary_with_paginated_completion",
            "opencli_primary": primary_summary,
            "rounds": merged_rounds,
            "candidates": len(accepted),
            "ok": bool(fallback_result.get("ok")),
        }
        return result, accepted, raw_rows

    @staticmethod
    def _opencli_primary_enabled(request: dict[str, Any]) -> bool:
        """OpenCLI 默认主召回；请求级参数或环境变量可显式关闭并回退生产 runner。"""
        configured = request.get("opencli_primary", os.environ.get("ASA_OPENCLI_PRIMARY", "1"))
        return str(configured).strip().lower() in {"1", "true", "yes", "on"}

    def _attempt_opencli_primary(
        self,
        *,
        channel: str,
        client: str,
        job: str,
        port: int,
        queries_path: Path,
        output_path: Path,
        raw_output_path: Path,
        limit: int,
        detail_limit: int,
        report: dict[str, Any],
        detail_args: list[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """OpenCLI 主渠道召回；失败、被阻断或无完整合格行时回退生产 runner。"""
        query_payload = _loads(queries_path.read_text(encoding="utf-8"), {})
        planned_queries = query_payload.get("queries") if isinstance(query_payload, dict) else []
        if any(
            isinstance(item, dict)
            and isinstance(item.get("cursor"), dict)
            and int(item["cursor"].get("page") or 0) > 1
            for item in (planned_queries if isinstance(planned_queries, list) else [])
        ):
            report[channel] = {
                "ok": False,
                "mode": "opencli_primary_recall",
                "channel": channel,
                "status": "production_fallback",
                "reason": "resume_cursor_requires_paginated_runner",
            }
            return "production_fallback"
        try:
            summary = self._run_external_json(
                [
                    self.python, str(OPENCLI_SHADOW), "--mode", "primary",
                    "--channel", channel, "--queries-json", str(queries_path),
                    "--output", str(output_path),
                    "--raw-output", str(raw_output_path),
                    "--client", client, "--job", job,
                    "--db", str(self.service.db_path), "--port", str(port),
                    "--limit", str(limit), "--detail-limit", str(detail_limit),
                    *(detail_args or []),
                    "--max-queries", str(max(1, len(planned_queries) if isinstance(planned_queries, list) else 0)),
                ],
                600,
                cancel_check=cancel_check,
            )
        except ExternalExecutionCancelled:
            raise
        except Exception as exc:
            summary = {
                "ok": False, "mode": "opencli_primary_recall", "channel": channel,
                "error": _trim_error(exc),
            }
        report[channel] = summary
        if summary.get("coverage_complete") or summary.get("ok"):
            return "opencli"
        if any(
            isinstance(item, dict)
            and item.get("terminal_state") in {"exhausted", "platform_capped"}
            for item in summary.get("rounds") or []
        ):
            return "opencli_partial"
        return "production_fallback"

    def _run_opencli_shadow(
        self,
        *,
        request: dict[str, Any],
        client: str,
        job: str,
        port: int,
        limit: int,
        liepin_queries: list[Any],
        xsaas_queries: list[Any],
        liepin_path: Path,
        xsaas_path: Path,
        artifact_path: Path,
        liepin_queries_path: Path | None = None,
        xsaas_queries_path: Path | None = None,
        skip_channels: set[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        configured = request.get("opencli_shadow", os.environ.get("ASA_OPENCLI_SHADOW", "1"))
        enabled = str(configured).strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return {"enabled": False, "mode": "read_only_shadow", "affects_intake": False, "channels": []}
        channels = []
        for channel, entries, baseline, queries_file in (
            ("liepin", liepin_queries, liepin_path, liepin_queries_path),
            ("xsaas", xsaas_queries, xsaas_path, xsaas_queries_path),
        ):
            if skip_channels and channel in skip_channels:
                channels.append({
                    "channel": channel, "status": "skipped",
                    "reason": "recall_engine_opencli", "affects_intake": False,
                })
                continue
            query = self._query_text(entries)
            if not query:
                channels.append({"channel": channel, "status": "skipped", "reason": "missing_query"})
                continue
            channel_limit = min(limit, 24 if channel == "liepin" else 100)
            command = [
                self.python,
                str(OPENCLI_SHADOW),
                "--channel", channel,
                "--query", query,
                "--baseline", str(baseline),
                "--client", client,
                "--job", job,
                "--db", str(self.service.db_path),
                "--port", str(port),
                "--limit", str(channel_limit),
            ]
            if queries_file is not None:
                command += ["--queries-json", str(queries_file)]
            try:
                result = self._run_external_json(command, 600, cancel_check=cancel_check)
                channels.append({"channel": channel, "status": "completed", **result})
            except ExternalExecutionCancelled:
                raise
            except Exception as exc:
                channels.append({
                    "channel": channel,
                    "status": "blocked",
                    "query": query,
                    "error": _trim_error(exc),
                    "affects_intake": False,
                })
        payload = {
            "enabled": True,
            "mode": "read_only_shadow",
            "affects_intake": False,
            "affects_outreach": False,
            "sample_policy": "first_nonempty_baseline_else_first",
            "channels": channels,
            "artifact": str(artifact_path),
        }
        artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        history_path = artifact_path.parent / "opencli-shadow-history.jsonl"
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "client": client,
                "job": job,
                "workflow_id": str(request.get("workflow_id") or ""),
                "channels": channels,
            }, ensure_ascii=False) + "\n")
        payload["history"] = str(history_path)
        return payload

