#!/usr/bin/env python3
"""S8 岗位画像存量回填：对库内活跃岗位逐岗学习"这个岗位实际在干什么"。

活跃口径：有待处理人选（未停止的 job_candidates）或近 90 天有 candidate_events 的岗位。
幂等：同人同岗事实一行（source_hash 未变跳过 LLM 重抽），同岗画像一行（重跑刷新 as_of /
version，不重复计人）。只写本任务的新表（job_profile_facts / job_profile_insights）与
岗位时间线事件（job_profile_generated），不动业务表其余部分。

默认 dry-run：只打印逐岗/逐人决策明细，不写库、不调 LLM；加 --apply 才实际抽取 + 聚合。

用法：
    python3 scripts/backfill_job_profiles.py --db /path/to/talent_system.db                 # dry-run
    python3 scripts/backfill_job_profiles.py --db /path/to/talent_system.db --apply         # 实际写库
    python3 scripts/backfill_job_profiles.py --db /path/to/talent_system.db --job-id 154 --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_system_agent import candidate_assessment, job_profile_insights
from a_system_agent.llm import BaseLLM, LLMError, create_default_llm
from a_system_agent.schema import ensure_schema


def collect_job_candidates(conn: sqlite3.Connection, job_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM job_candidates WHERE job_id=? ORDER BY id", (int(job_id),)
    ).fetchall()
    return [int(row["id"]) for row in rows]


def corpus_chars(conn: sqlite3.Connection, candidate_id: int) -> int:
    candidate = candidate_assessment.load_candidate_resume(conn, int(candidate_id))
    if candidate is None:
        return 0
    resume = candidate.get("resume") if isinstance(candidate.get("resume"), dict) else {}
    return len(candidate_assessment.build_corpus(resume).strip())


def run_backfill(
    conn: sqlite3.Connection,
    *,
    llm: BaseLLM | None = None,
    job_id: int | None = None,
    dry_run: bool = True,
    force: bool = False,
) -> dict[str, object]:
    """回填主流程（测试可直接注入临时库连接 + FakeLLM）。返回统计明细 dict。"""
    ensure_schema(conn)
    jobs = job_profile_insights.list_active_jobs(conn, job_id=job_id)
    summary: dict[str, object] = {
        "dry_run": dry_run,
        "jobs_active": len(jobs),
        "jobs_done": 0,
        "jobs_ready": 0,
        "jobs_insufficient": 0,
        "candidates_total": 0,
        "candidates_extracted": 0,
        "candidates_skipped_unchanged": 0,
        "candidates_no_facts": 0,
        "candidates_failed": 0,
        "facts_kept": 0,
        "facts_dropped": 0,
        "details": [],
    }
    reasons: Counter[str] = Counter()
    for jid in jobs:
        candidate_ids = collect_job_candidates(conn, jid)
        detail: dict[str, object] = {"job_id": jid, "candidates": len(candidate_ids)}
        if dry_run:
            extractable = sum(1 for cid in candidate_ids if corpus_chars(conn, cid) >= 50)
            detail["extractable"] = extractable
            detail["action"] = "would_extract" if extractable else "skip_no_corpus"
            details = summary["details"]
            assert isinstance(details, list)
            details.append(detail)
            continue
        assert llm is not None  # --apply 必须有可用模型
        job_kept = 0
        job_failed = 0
        for cid in candidate_ids:
            summary["candidates_total"] = int(summary["candidates_total"]) + 1
            try:
                result = job_profile_insights.extract_duty_facts_for_candidate(
                    conn, candidate_id=cid, llm=llm, force=force
                )
            except LLMError:
                raise  # 模型整体不可用：整批终止，避免逐人空转
            except Exception as exc:  # 单人失败不阻断整岗（记 stats）
                job_failed += 1
                reasons[f"failed:{type(exc).__name__}"] += 1
                continue
            job_kept += int(result.get("kept") or 0)
            summary["facts_dropped"] = int(summary["facts_dropped"]) + int(result.get("dropped") or 0)
            if result.get("skipped"):
                summary["candidates_skipped_unchanged"] = int(summary["candidates_skipped_unchanged"]) + 1
            elif int(result.get("fact_count") or 0) > 0:
                summary["candidates_extracted"] = int(summary["candidates_extracted"]) + 1
            else:
                summary["candidates_no_facts"] = int(summary["candidates_no_facts"]) + 1
                reasons[f"no_facts:{result.get('reason') or '未说明'}"] += 1
        insight = job_profile_insights.aggregate_job_profile(conn, job_id=jid, persist=True)
        conn.commit()
        summary["facts_kept"] = int(summary["facts_kept"]) + job_kept
        summary["candidates_failed"] = int(summary["candidates_failed"]) + job_failed
        summary["jobs_done"] = int(summary["jobs_done"]) + 1
        if insight.get("status") == "ready":
            summary["jobs_ready"] = int(summary["jobs_ready"]) + 1
        else:
            summary["jobs_insufficient"] = int(summary["jobs_insufficient"]) + 1
        detail.update(
            {
                "action": "aggregated",
                "status": insight.get("status"),
                "source_count": insight.get("source_count"),
                "facts_kept": job_kept,
                "failed": job_failed,
            }
        )
        details = summary["details"]
        assert isinstance(details, list)
        details.append(detail)
    summary["reasons"] = dict(reasons)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="S8 岗位画像存量回填（活跃岗位逐岗学习职责事实并聚合画像）")
    parser.add_argument("--db", required=True, help="目标 SQLite 数据库路径")
    parser.add_argument("--job-id", type=int, default=None, help="只回填指定岗位（默认全部活跃岗位）")
    parser.add_argument("--force", action="store_true", help="履历未变也强制重抽（默认 source_hash 未变跳过）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="只打印决策明细，不写库不调模型（默认）")
    mode.add_argument("--apply", dest="apply", action="store_true", help="实际抽取 + 聚合写库")
    parser.set_defaults(apply=False)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"数据库不存在：{db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        conn.commit()
        llm = None
        if args.apply:
            llm = create_default_llm()
            if llm.model == "unavailable":
                print("模型不可用（未配置 Keychain/API Key），--apply 终止；可先 dry-run 查看范围。", file=sys.stderr)
                return 2
        summary = run_backfill(
            conn, llm=llm, job_id=args.job_id, dry_run=not args.apply, force=bool(args.force)
        )
    finally:
        conn.close()

    print(f"模式：{'APPLY（写库）' if args.apply else 'DRY-RUN（只读）'}；活跃岗位 {summary['jobs_active']} 个")
    for detail in summary["details"]:  # type: ignore[union-attr]
        print(f"  岗位 #{detail['job_id']}：人选 {detail['candidates']} 人 → {detail}")
    if args.apply:
        print(
            "完成：岗位 {jobs_done}（画像就绪 {jobs_ready} / 履历不足 {jobs_insufficient}），"
            "人选 {candidates_total}（抽取 {candidates_extracted} / 沿用 {candidates_skipped_unchanged} / "
            "无事实 {candidates_no_facts} / 失败 {candidates_failed}），"
            "事实入库 {facts_kept} 条、丢弃 {facts_dropped} 条".format(**summary)
        )
    else:
        print("（dry-run 未写库、未调模型；加 --apply 实际执行）")
    print(json.dumps({"reasons": summary["reasons"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
