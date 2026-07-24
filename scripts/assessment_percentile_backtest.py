#!/usr/bin/env python3
"""S6-2：水平分位真实回测 —— 20 个有实际结果的历史人选 × 分位 band × 实际推进对照。

口径（PRD §5 S6-2 验收：分位判断在 20 个历史人选上与实际面试结果一致性 ≥ 可解释）：
- 抽样：agent_candidate_assessments(is_current=1) × job_candidates，只取有明确结果的人——
  推进组（clean_stage 含 已触达/触达待核验/已联系/已沟通/已推荐/面试/Offer/谈薪/入职/已申请加微信）
  与 初筛未过组（H5 最近寻访/初筛不通过）；S1 待复核无结果，不入样。两组混合凑满名额。
- 逐人走 candidate_assessment 同一生成通道（artifact 落 --db 指定库；生产库回放请传副本），
  取 percentile band / 参照 N / basis。
- 对照表 band × 实际推进率（推进深=高分位应占多）；错例逐条一句规则化归因：
  参照样本不足 / 简历过薄 / 无既有评分轨迹落位 / 参照系偏差（分位外的推进因素）。
- 导出 markdown（数字+错例）+ JSON 明细到 work/assessment_replay/（gitignored，不外发）。

用法：
  python3 scripts/assessment_percentile_backtest.py --db <副本> --limit 20 [--no-company-signals]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import assessment_replay  # noqa: E402
from a_system_agent import candidate_assessment  # noqa: E402
from a_system_agent.llm import BaseLLM, create_default_llm  # noqa: E402
from a_system_agent.workflow import _mask_candidate_name  # noqa: E402

DEFAULT_DB = "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
DEFAULT_OUT_DIR = REPO_DIR / "work" / "assessment_replay"

REJECTED_STAGE_TOKENS = ("初筛不通过",)
BAND_ORDER = ("top10", "top25", "median", "below", None)
THIN_CORPUS_LEN = 600  # 简历语料 < 600 字 → 简历过薄归因

OUTCOME_NOTE = (
    "结果口径：本库无面试/成单字段，按入库阶段取「有实际结果」的人——"
    "推进组 = 阶段≥已触达/触达待核验/已联系/已推荐/面试/Offer/已申请加微信（当年走得更远）；"
    "初筛未过组 = H5 最近寻访/初筛不通过（明确未推进）；S1 待复核无结果不入样。"
    "一致性读法：推进组的实际推进率应随 band 升高（推进深=高分位应占多）。"
    "注意：分位分布基于既有评估 fit_score，而当初初筛/推进可能部分参考过该分，"
    "本对照是「一致性体检」而非完全独立的预测验证（偏差在错例归因中如实讨论）。"
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _group_of(stage: Any) -> str:
    text = str(stage or "")
    if any(token in text for token in REJECTED_STAGE_TOKENS):
        return "rejected"
    if assessment_replay._is_advanced(text, None):
        return "advanced"
    return "pending"


def sample_backtest_candidates(conn: sqlite3.Connection, *, limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """抽有实际结果的历史人选：推进组/初筛未过组混合，优先有简历语料的。"""
    rows = conn.execute(
        """
        SELECT a.job_candidate_id AS id,a.job_id AS job_id,a.fit_score AS fit_score,
               j.clean_stage AS clean_stage,jb.title AS job_title,c.name AS client,
               p.display_name AS display_name,p.current_company AS current_company,p.experience AS experience
          FROM agent_candidate_assessments a
          JOIN job_candidates j ON j.id=a.job_candidate_id
          JOIN jobs jb ON jb.id=a.job_id
          JOIN clients c ON c.id=jb.client_id
          JOIN people p ON p.id=a.person_id
         WHERE a.is_current=1
         ORDER BY a.job_candidate_id
        """,
    ).fetchall()
    advanced: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped_pending = 0
    skipped_no_resume = 0
    for row in rows:
        item = dict(row)
        group = _group_of(item.get("clean_stage"))
        if group == "pending":
            skipped_pending += 1
            continue
        profile = candidate_assessment.load_candidate_resume(conn, int(item["id"]))
        corpus = candidate_assessment.build_corpus((profile or {}).get("resume") or {})
        if len(corpus.strip()) < 200:
            skipped_no_resume += 1
            continue
        item["corpus_len"] = len(corpus)
        (advanced if group == "advanced" else rejected).append(item)
    quota = (limit + 1) // 2
    picked = advanced[:quota] + rejected[: max(0, limit - min(quota, len(advanced)))]
    if len(picked) < limit:
        picked += advanced[quota : quota + (limit - len(picked))]
    picked = picked[:limit]
    for item in picked:
        item["group"] = "advanced" if _group_of(item.get("clean_stage")) == "advanced" else "rejected"
    trace = {
        "assessed_pool": len(rows),
        "advanced_pool": len(advanced),
        "rejected_pool": len(rejected),
        "skipped_pending": skipped_pending,
        "skipped_no_resume": skipped_no_resume,
        "picked_advanced": sum(1 for item in picked if item["group"] == "advanced"),
        "picked_rejected": sum(1 for item in picked if item["group"] == "rejected"),
    }
    return picked, trace


def attribute_mismatch(person: dict[str, Any], doc: dict[str, Any] | None, *, error: str = "") -> str:
    """错例一句归因（规则化）：为什么分位与实际结果对不上。"""
    if doc is None:
        return f"评估生成失败（{error}），无法落位"
    percentile = (doc.get("dimensions") or {}).get("percentile") or {}
    reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
    reasons: list[str] = []
    if not reference.get("sample_sufficient"):
        reasons.append(f"参照样本不足（N={reference.get('n')}），落位按推测口径")
    if percentile.get("basis") == "trajectory_features":
        reasons.append("无既有评估分，用轨迹特征落位，与参照分布存在口径偏差")
    if int(person.get("corpus_len") or 0) < THIN_CORPUS_LEN:
        reasons.append("简历过薄，轨迹与分位依据不足")
    if not reasons:
        if person["group"] == "advanced":
            reasons.append("参照系偏差：分位只反映相对水平，当初推进还含分位外因素（岗位特殊性/顾问判断）")
        else:
            reasons.append("岗位特殊性：分位靠前但当初未推进，可能存在分位外的否决因素")
    return "；".join(reasons)


def is_mismatch(person: dict[str, Any], band: str | None) -> bool:
    """错例口径：推进组落 median/below/无法落位，或初筛未过组落 top10/top25/无法落位。"""
    if band is None:
        return True
    if person["group"] == "advanced":
        return band in ("median", "below")
    return band in ("top10", "top25")


def run_backtest(
    db_path: Path,
    *,
    limit: int = 20,
    out_dir: Path = DEFAULT_OUT_DIR,
    llm: BaseLLM | None = None,
    kb_dir: str | None = None,
    signal_fetcher: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """回测主流程：抽样 → 逐人生成 → band×推进率对照 + 错例归因 → 导出 markdown/JSON。"""
    llm = llm or create_default_llm()
    conn = _connect(db_path)
    try:
        picked, trace = sample_backtest_candidates(conn, limit=limit)
        rows: list[dict[str, Any]] = []
        for person in picked:
            entry: dict[str, Any] = {
                "candidate_id": int(person["id"]),
                "job_id": int(person["job_id"]),
                "client": person.get("client"),
                "job_title": person.get("job_title"),
                "group": person["group"],
                "clean_stage": person.get("clean_stage"),
                "name_masked": _mask_candidate_name(person.get("display_name")),
            }
            try:
                doc = candidate_assessment.run_assessment(
                    conn,
                    candidate_id=int(person["id"]),
                    job_id=int(person["job_id"]),
                    llm=llm,
                    kb_dir=kb_dir,
                    mask_name=_mask_candidate_name,
                    signal_fetcher=signal_fetcher,
                    today=today,
                )
                candidate_assessment.persist_assessment(conn, doc)
                conn.commit()
                percentile = (doc.get("dimensions") or {}).get("percentile") or {}
                reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
                entry.update(
                    ok=True,
                    band=percentile.get("band"),
                    basis=percentile.get("basis"),
                    score=percentile.get("score"),
                    rank=percentile.get("percentile_rank"),
                    ref_n=reference.get("n"),
                    sample_sufficient=bool(reference.get("sample_sufficient")),
                    confidence=percentile.get("confidence"),
                    verdict=percentile.get("verdict"),
                    mismatch=is_mismatch(person, percentile.get("band")),
                    attribution="",
                )
            except Exception as exc:  # 单人失败不阻断；失败即错例，归因=生成失败
                conn.rollback()
                entry.update(ok=False, error=f"{exc.__class__.__name__}: {exc}", band=None, mismatch=True, attribution="")
            if entry.get("mismatch"):
                entry["attribution"] = attribute_mismatch(
                    person,
                    None if not entry.get("ok") else {"dimensions": {"percentile": {
                        "basis": entry.get("basis"),
                        "reference": {"n": entry.get("ref_n"), "sample_sufficient": entry.get("sample_sufficient")},
                    }}},
                    error=str(entry.get("error") or ""),
                )
            rows.append(entry)

        # band × 推进率对照表
        table: list[dict[str, Any]] = []
        for band in BAND_ORDER:
            members = [row for row in rows if row.get("band") == band and row.get("ok")]
            if not members:
                continue
            advanced_n = sum(1 for row in members if row["group"] == "advanced")
            table.append(
                {
                    "band": band or "无法落位",
                    "n": len(members),
                    "advanced": advanced_n,
                    "rejected": len(members) - advanced_n,
                    "advanced_rate": round(advanced_n / len(members), 3),
                }
            )
        mismatches = [row for row in rows if row.get("mismatch")]

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        md_path = out_dir / f"percentile_backtest_{stamp}.md"
        json_path = out_dir / f"percentile_backtest_{stamp}.json"
        label = candidate_assessment.LABELS
        lines = [
            "# S6-2 水平分位回测：band × 实际推进对照",
            "",
            f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜模型：{getattr(llm, 'model', 'unknown')}｜库：{db_path}",
            f"- 抽样：有结果池 推进 {trace['advanced_pool']} / 初筛未过 {trace['rejected_pool']}"
            f"（无结果跳过 {trace['skipped_pending']}，无简历跳过 {trace['skipped_no_resume']}）"
            f" → 抽 {len(picked)} 人（推进 {trace['picked_advanced']} / 未过 {trace['picked_rejected']}）",
            f"- {OUTCOME_NOTE}",
            "",
            "## 对照表（band × 实际推进率）",
            "",
            "| band | 人数 | 推进 | 初筛未过 | 实际推进率 |",
            "|---|---|---|---|---|",
        ]
        for row in table:
            band_label = label.get(row["band"], row["band"])
            lines.append(f"| {band_label} | {row['n']} | {row['advanced']} | {row['rejected']} | {row['advanced_rate'] * 100:.0f}% |")
        lines += [
            "",
            f"成功 {sum(1 for row in rows if row.get('ok'))}/{len(rows)}｜错例 {len(mismatches)} 人"
            f"（口径：推进组落 median/below/无法落位，或初筛未过组落 top10/top25/无法落位）",
            "",
            "## 错例逐条归因",
            "",
        ]
        if not mismatches:
            lines.append("无错例。")
        for row in mismatches:
            band_label = label.get(row.get("band"), row.get("band") or "无法落位")
            group_label = "推进" if row["group"] == "advanced" else "初筛未过"
            lines.append(
                f"- {row['name_masked']}（job_candidate #{row['candidate_id']}｜{row['client']}·{row['job_title']}｜{group_label}｜band={band_label}）："
                f"{row.get('attribution') or ''}"
            )
        lines += ["", "## 全员明细", "", "| 人选 | 岗位 | 实际 | band | 分 | rank | 参照N | 置信 | 依据 |", "|---|---|---|---|---|---|---|---|---|"]
        for row in rows:
            if not row.get("ok"):
                lines.append(f"| {row['name_masked']} | {row['job_title']} | {'推进' if row['group'] == 'advanced' else '未过'} | 生成失败 | - | - | - | - | {row.get('error', '')[:40]} |")
                continue
            band_label = label.get(row.get("band"), row.get("band") or "无法落位")
            rank = row.get("rank")
            lines.append(
                f"| {row['name_masked']} | {row['job_title']} | {'推进' if row['group'] == 'advanced' else '未过'}"
                f" | {band_label} | {row.get('score')} | {rank if rank is not None else '-'} | {row.get('ref_n')}"
                f" | {label.get(row.get('confidence'), row.get('confidence'))} | {label.get(row.get('basis'), row.get('basis'))} |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        json_path.write_text(
            json.dumps({"trace": trace, "table": table, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "db": str(db_path),
            "markdown": str(md_path),
            "json": str(json_path),
            "sample_trace": trace,
            "attempted": len(rows),
            "generated": sum(1 for row in rows if row.get("ok")),
            "table": table,
            "mismatch_count": len(mismatches),
            "mismatches": [
                {"candidate_id": row["candidate_id"], "group": row["group"], "band": row.get("band"), "attribution": row.get("attribution")}
                for row in mismatches
            ],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="S6-2 水平分位回测：20 个历史人选 band × 实际推进对照 + 错例归因")
    parser.add_argument("--db", default=DEFAULT_DB, help="v3 库路径（生产库请传副本，artifact 落该库）")
    parser.add_argument("--limit", type=int, default=20, help="抽样人数（默认 20）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="导出目录（默认 work/assessment_replay/）")
    parser.add_argument("--no-company-signals", action="store_true", help="跳过公司近况公开采集")
    args = parser.parse_args()
    fetcher = (lambda url, timeout: (0, "", "skipped_by_flag")) if args.no_company_signals else None
    summary = run_backtest(
        Path(args.db).expanduser(),
        limit=max(1, args.limit),
        out_dir=Path(args.out_dir).expanduser(),
        signal_fetcher=fetcher,
    )
    print(json.dumps({key: value for key, value in summary.items() if key != "mismatches"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
