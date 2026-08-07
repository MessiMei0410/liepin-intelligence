#!/usr/bin/env python3
"""S6-1：判人评估回放工具 —— 按岗位抽历史人选，批量生成判人评估，导出顾问盲评 markdown。

口径（TASKCARD_S6-1 验收 1）：
- 三个 case 岗位各抽 5 个历史人选，成单/未成单混合。本库无成单/入职字段，"成单"按阶段推断：
  推进组 = clean_stage 含 已触达/已联系/已沟通/已推荐/面试/Offer/谈薪/入职/已申请加微信
           或 raw_status ∈ contacted/replied/recommended/interview/offer/onboarded/wechat_requested；
  未推进组 = 其余（待复核/初筛不通过/已停止等）。两半互补凑满名额，口径在导出头部注明。
- 生成走 candidate_assessment 同一写入通道（agent_artifacts + candidate_events，幂等）；
  业务表一律只读。生产库回放 artifact 落库（系统资产）；简历原文只进 work/ 导出。
- 导出 markdown 到 work/assessment_replay/（已 gitignore）：每人一段——职业轨迹结论 +
  跳槽质量史 + 证据 + 置信度（推测=inferred），业务语言（UX-1），供顾问盲评。

--metrics 指标模式（二期扩展，2026-08-05，可选，additive）：
- 在盲评 markdown 之外输出可确定性计算的指标 JSON（不依赖人工阅读），口径：
  ① 五维覆盖率 dimension_coverage：生成的 dimensions 五维（DIMENSIONS_IMPLEMENTED）中
     「有非空 verdict」的维度占比（逐人 → 均值；另给逐维命中计数）；
  ② 证据条数分布 evidence_distribution：逐人 evidence_stats.kept 的 min/max/avg + 逐人明细
     + 被剥离证据总数（stripped）；
  ③ unknown 占比 unknown_ratio：五维产物中枚举字段取值 == "unknown" 的比例，计数字段为
     trajectory.promotion_pace / trajectory.tech_evolution / 各 segment.tier /
     move_history.current_move / 各 move 的 direction/platform/title_direction/responsibility_direction /
     percentile.band；
  ④ 推测维度占比 inferred_ratio：confidence == "inferred" 的维度占比（沿用原汇总口径）。
- 指标只读 dimensions/evidence_stats 产物字段，markdown 导出内容与流程完全不变。

用法：
  PYTHONPATH=scripts python3 scripts/assessment_replay.py --job-id 154 --limit 5
  PYTHONPATH=scripts python3 scripts/assessment_replay.py --db <备份库副本> --job-id 38 --limit 5
  PYTHONPATH=scripts python3 scripts/assessment_replay.py --job-id 154 --limit 5 --metrics
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from a_system_agent import candidate_assessment  # noqa: E402
from a_system_agent.llm import BaseLLM, create_default_llm  # noqa: E402
from a_system_agent.workflow import _mask_candidate_name  # noqa: E402

DEFAULT_DB = "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
DEFAULT_OUT_DIR = REPO_DIR / "work" / "assessment_replay"

ADVANCED_STAGE_TOKENS = ("已触达", "触达待核验", "已联系", "已沟通", "已推荐", "面试", "Offer", "谈薪", "入职", "已申请加微信")
ADVANCED_STATUSES = {"contacted", "replied", "recommended", "interview", "offer", "onboarded", "wechat_requested"}

OUTCOME_NOTE = (
    "成单口径说明：本库无成单/入职字段，按阶段推进推断——阶段≥已触达/触达待核验/已联系/已推荐/面试/Offer "
    "或状态 contacted/replied/recommended/interview/offer/onboarded 记「推进组」（当年更可能成了或走得深），"
    "待复核/初筛不通过/已停止等记「未推进组」。两组混合抽样，供盲评对照。"
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _is_advanced(stage: Any, status: Any) -> bool:
    stage_text = str(stage or "")
    return any(token in stage_text for token in ADVANCED_STAGE_TOKENS) or str(status or "").lower() in ADVANCED_STATUSES


def sample_candidates(conn: sqlite3.Connection, job_id: int, *, limit: int = 5, min_corpus: int = 200) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按岗位抽历史人选：推进/未推进混合，优先有简历语料的。返回 (人选, 抽样留痕)。"""
    rows = conn.execute(
        """
        SELECT jc.id,jc.clean_stage,jc.raw_status,p.display_name,p.current_company,p.current_title
          FROM job_candidates jc JOIN people p ON p.id=jc.person_id
         WHERE jc.job_id=? ORDER BY jc.id DESC
        """,
        (int(job_id),),
    ).fetchall()
    advanced: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    skipped_no_resume = 0
    for row in rows:
        item = dict(row)
        profile = candidate_assessment.load_candidate_resume(conn, int(item["id"]))
        corpus = candidate_assessment.build_corpus((profile or {}).get("resume") or {})
        if len(corpus.strip()) < min_corpus:
            skipped_no_resume += 1
            continue
        item["corpus_len"] = len(corpus)
        (advanced if _is_advanced(item.get("clean_stage"), item.get("raw_status")) else rest).append(item)
    quota = (limit + 1) // 2
    picked = advanced[:quota]
    picked += rest[: max(0, limit - len(picked))]
    if len(picked) < limit:
        picked += advanced[quota : quota + (limit - len(picked))]
    picked = picked[:limit]
    trace = {
        "pool": len(rows),
        "advanced_pool": len(advanced),
        "rest_pool": len(rest),
        "skipped_no_resume": skipped_no_resume,
        "picked_advanced": sum(1 for item in picked if _is_advanced(item.get("clean_stage"), item.get("raw_status"))),
        "picked_rest": sum(1 for item in picked if not _is_advanced(item.get("clean_stage"), item.get("raw_status"))),
    }
    for item in picked:
        item["group"] = "推进组" if _is_advanced(item.get("clean_stage"), item.get("raw_status")) else "未推进组"
    return picked, trace


def render_person_markdown(index: int, person: dict[str, Any], doc: dict[str, Any] | None, error: str = "") -> str:
    """每人一段：轨迹结论 + 跳槽史 + 证据 + 置信度（业务语言，供顾问盲评）。"""
    name = _mask_candidate_name(person.get("display_name"))
    header = (
        f"## 人选 {index}：{name}（job_candidate #{person.get('id')}｜{person.get('group')}）\n\n"
        f"- 当前：{person.get('current_company') or ''}｜{person.get('current_title') or ''}\n"
        f"- 入库阶段：{person.get('clean_stage') or ''}\n"
    )
    if doc is None:
        return header + f"\n**生成失败**：{error}\n"
    dimensions = doc.get("dimensions") or {}
    trajectory = dimensions.get("trajectory") or {}
    move_history = dimensions.get("move_history") or {}
    label = candidate_assessment.LABELS
    lines = [header, ""]
    lines.append(
        f"### 职业轨迹（置信度：{label.get(trajectory.get('confidence'), trajectory.get('confidence'))}）\n"
    )
    lines.append(f"**结论**：{trajectory.get('verdict') or ''}\n")
    lines.append(
        f"- 晋升速度：{label.get(trajectory.get('promotion_pace'), '无法判断')}；"
        f"技术栈演进：{label.get(trajectory.get('tech_evolution'), '无法判断')}"
    )
    for segment in trajectory.get("segments") or []:
        tier = label.get(segment.get("tier"), "无法判断")
        source = "图谱" if segment.get("tier_source") == "graph" else "推测"
        lines.append(
            f"- {segment.get('period') or ''}｜{segment.get('company') or ''}｜{segment.get('title') or ''}"
            f"｜平台含金量：{tier}（{source}）"
            + (f"｜{segment.get('note')}" if segment.get("note") else "")
        )
    lines.append(
        f"\n### 跳槽质量史（置信度：{label.get(move_history.get('confidence'), move_history.get('confidence'))}）\n"
    )
    lines.append(f"**结论**：{move_history.get('verdict') or ''}\n")
    for move in move_history.get("moves") or []:
        lines.append(
            f"- {move.get('from') or ''} → {move.get('to') or ''}：{label.get(move.get('direction'), '平移')}"
            f"（平台 {label.get(move.get('platform'), '平移')} / title {label.get(move.get('title_direction'), '平移')}"
            f" / 职责 {label.get(move.get('responsibility_direction'), '平移')}）— {move.get('reason') or ''}"
        )
    lines.append(f"- 当前这一单对他：{label.get(move_history.get('current_move'), '无法判断')}")
    percentile = dimensions.get("percentile") or {}
    if percentile:
        reference = percentile.get("reference") if isinstance(percentile.get("reference"), dict) else {}
        window = f"±{reference.get('years_window')}年" if reference.get("years_window") is not None else "不限年限"
        band_label = label.get(percentile.get("band"), "无法落位") if percentile.get("band") else "无法落位"
        lines.append(
            f"\n### 在同龄人里的位置（置信度：{label.get(percentile.get('confidence'), percentile.get('confidence'))}）\n"
        )
        lines.append(f"**结论**：{percentile.get('verdict') or ''}\n")
        lines.append(
            f"- 落位：{band_label}｜得分 {percentile.get('score')}（{label.get(percentile.get('basis'), percentile.get('basis'))}）"
            f"｜参照系：同方向（{reference.get('direction') or ''}）{window} N={reference.get('n')}"
            f" 中位分 {reference.get('median')}"
            + (f"｜{reference.get('note')}" if reference.get("note") else "")
        )
    motivation = dimensions.get("motivation") or {}
    if motivation:
        lines.append(
            f"\n### 动机与时机（置信度：{label.get(motivation.get('confidence'), motivation.get('confidence'))}）\n"
        )
        lines.append(f"**结论**：{motivation.get('verdict') or ''}\n")
        for signal in motivation.get("signals") or []:
            if signal.get("url"):
                suffix = f"（来源：{signal.get('url')}，{signal.get('as_of')}）"
            elif signal.get("as_of"):
                suffix = f"（{signal.get('as_of')}）"
            else:
                suffix = ""
            lines.append(f"- [{signal.get('source') or ''}] {signal.get('summary') or ''}{suffix}")
        if not (motivation.get("signals") or []):
            lines.append("- 未见明显变动信号")
    risks = dimensions.get("risks") or {}
    if risks:
        lines.append(
            f"\n### 需要核实的问题（置信度：{label.get(risks.get('confidence'), risks.get('confidence'))}）\n"
        )
        lines.append(f"**结论**：{risks.get('verdict') or ''}\n")
        for item in risks.get("items") or []:
            lines.append(
                f"- 【{label.get(item.get('severity'), item.get('severity'))}｜{label.get(item.get('kind'), item.get('kind'))}】"
                f"{item.get('risk') or ''}"
            )
        if not (risks.get("items") or []):
            lines.append("- 未见需核实的问题")
    lines.append("\n### 证据\n")
    for dim_name in candidate_assessment.DIMENSIONS_IMPLEMENTED:
        dim = dimensions.get(dim_name) or {}
        for item in dim.get("evidence") or []:
            lines.append(f"- [{label.get(dim_name, dim_name)}｜{item.get('type')}] {item.get('ref')}")
    stats = doc.get("evidence_stats") or {}
    if stats.get("stripped"):
        lines.append(f"- （{stats['stripped']} 条引用未通过逐字/真实条目校验，已自动剥离不计入）")
    lines.append("\n### 顾问口径摘要\n")
    lines.append(str(doc.get("consultant_summary") or ""))
    lines.append(
        f"\n---\n生成时间：{doc.get('as_of')}｜模型：{doc.get('model')}｜"
        f"证据 {stats.get('kept', 0)} 条\n"
    )
    return "\n".join(lines)


def compute_assessment_metrics(succeeded: list[dict[str, Any]]) -> dict[str, Any]:
    """--metrics 模式：对已成功生成的评估 doc 计算确定性指标（口径见模块 docstring）。

    succeeded：run_replay 中 ok=True 的条目（含 doc）。不依赖人工阅读，纯字段统计。
    """
    dims = candidate_assessment.DIMENSIONS_IMPLEMENTED
    per_dimension_hits = {name: 0 for name in dims}
    coverage_per_person: list[float] = []
    evidence_counts: list[int] = []
    stripped_total = 0
    unknown_fields = 0
    total_fields = 0
    inferred_dims = 0
    total_dims = 0

    def _count_unknown(value: Any) -> None:
        nonlocal unknown_fields, total_fields
        total_fields += 1
        if str(value or "") == "unknown":
            unknown_fields += 1

    for entry in succeeded:
        doc = entry["doc"]
        dimensions = doc.get("dimensions") or {}
        covered = 0
        for name in dims:
            dim = dimensions.get(name) or {}
            total_dims += 1
            if dim.get("confidence") == "inferred":
                inferred_dims += 1
            if str(dim.get("verdict") or "").strip():
                covered += 1
                per_dimension_hits[name] += 1
        coverage_per_person.append(round(covered / len(dims), 4) if dims else 0.0)

        stats = doc.get("evidence_stats") or {}
        evidence_counts.append(int(stats.get("kept", 0)))
        stripped_total += int(stats.get("stripped", 0) or 0)

        trajectory = dimensions.get("trajectory") or {}
        _count_unknown(trajectory.get("promotion_pace"))
        _count_unknown(trajectory.get("tech_evolution"))
        for segment in trajectory.get("segments") or []:
            if isinstance(segment, dict):
                _count_unknown(segment.get("tier"))
        move_history = dimensions.get("move_history") or {}
        _count_unknown(move_history.get("current_move"))
        for move in move_history.get("moves") or []:
            if isinstance(move, dict):
                for field in ("direction", "platform", "title_direction", "responsibility_direction"):
                    _count_unknown(move.get(field))
        percentile = dimensions.get("percentile") or {}
        if percentile:
            _count_unknown(percentile.get("band"))

    persons = len(succeeded)
    return {
        "persons": persons,
        "dimension_coverage": {
            "mean": round(sum(coverage_per_person) / persons, 4) if persons else 0.0,
            "per_person": coverage_per_person,
            "per_dimension_hits": per_dimension_hits,
            "dimensions": list(dims),
        },
        "evidence_distribution": {
            "kept_per_person": evidence_counts,
            "min": min(evidence_counts) if evidence_counts else 0,
            "max": max(evidence_counts) if evidence_counts else 0,
            "avg": round(sum(evidence_counts) / len(evidence_counts), 2) if evidence_counts else 0.0,
            "stripped_total": stripped_total,
        },
        "unknown_ratio": round(unknown_fields / total_fields, 4) if total_fields else 0.0,
        "unknown_fields": unknown_fields,
        "total_enum_fields": total_fields,
        "inferred_ratio": round(inferred_dims / total_dims, 4) if total_dims else 0.0,
    }


def run_replay(
    db_path: Path,
    job_id: int,
    *,
    limit: int = 5,
    out_dir: Path = DEFAULT_OUT_DIR,
    llm: BaseLLM | None = None,
    kb_dir: str | None = None,
    signal_fetcher: Any = None,
    with_metrics: bool = False,
) -> dict[str, Any]:
    """回放主流程：抽样 → 逐人生成（写 artifact）→ 导出 markdown → 返回统计。

    signal_fetcher：S6-2 公司近况采集器（默认真实只读采集；测试注入 stub 防真实网络）。
    with_metrics=True 时在返回统计中附带 "metrics" 键（compute_assessment_metrics，口径见模块 docstring）；
    默认 False，返回结构与盲评 markdown 完全不变。
    """
    llm = llm or create_default_llm()
    conn = _connect(db_path)
    try:
        job = conn.execute(
            "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()
        if job is None:
            raise LookupError(f"岗位不存在：{job_id}")
        picked, trace = sample_candidates(conn, int(job_id), limit=limit)
        results: list[dict[str, Any]] = []
        for index, person in enumerate(picked, 1):
            entry: dict[str, Any] = {"candidate_id": int(person["id"]), "group": person["group"]}
            try:
                doc = candidate_assessment.run_assessment(
                    conn,
                    candidate_id=int(person["id"]),
                    job_id=int(job_id),
                    llm=llm,
                    kb_dir=kb_dir,
                    mask_name=_mask_candidate_name,
                    signal_fetcher=signal_fetcher,
                )
                artifact_id = candidate_assessment.persist_assessment(conn, doc)
                conn.commit()
                entry.update(ok=True, artifact_id=artifact_id, doc=doc)
            except Exception as exc:  # 单人失败不阻断整批；失败原因进导出与统计
                conn.rollback()
                entry.update(ok=False, error=f"{exc.__class__.__name__}: {exc}")
            results.append(entry)

        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"assessment_replay_{job['client']}_job{job_id}_{stamp}.md"
        succeeded = [entry for entry in results if entry.get("ok")]
        evidence_counts = [
            int((entry["doc"].get("evidence_stats") or {}).get("kept", 0)) for entry in succeeded
        ]
        inferred_dims = 0
        total_dims = 0
        for entry in succeeded:
            for name in candidate_assessment.DIMENSIONS_IMPLEMENTED:
                total_dims += 1
                if ((entry["doc"].get("dimensions") or {}).get(name) or {}).get("confidence") == "inferred":
                    inferred_dims += 1
        header = [
            f"# 判人评估回放：{job['client']}｜{job['title']}（job #{job_id}）",
            "",
            f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}｜模型：{getattr(llm, 'model', 'unknown')}",
            f"- 抽样：库内 {trace['pool']} 人（推进 {trace['advanced_pool']} / 未推进 {trace['rest_pool']}，"
            f"无简历跳过 {trace['skipped_no_resume']}）→ 抽 {len(picked)} 人"
            f"（推进 {trace['picked_advanced']} / 未推进 {trace['picked_rest']}）",
            f"- {OUTCOME_NOTE}",
            "- 红线：评估只辅助判断，不构成决策建议；「推测」= 证据不足需人工核验。",
            "",
            f"成功 {len(succeeded)}/{len(results)}｜证据均条数 "
            f"{(sum(evidence_counts) / len(evidence_counts)) if evidence_counts else 0:.1f}｜"
            f"推测维度占比 {(inferred_dims / total_dims * 100) if total_dims else 0:.0f}%",
            "",
            "---",
            "",
        ]
        sections: list[str] = []
        for index, (person, entry) in enumerate(zip(picked, results), 1):
            sections.append(
                render_person_markdown(index, person, entry.get("doc") if entry.get("ok") else None, str(entry.get("error") or ""))
            )
            sections.append("")
        out_path.write_text("\n".join(header + sections), encoding="utf-8")
        summary: dict[str, Any] = {
            "ok": True,
            "db": str(db_path),
            "job_id": int(job_id),
            "client": job["client"],
            "title": job["title"],
            "markdown": str(out_path),
            "sample_trace": trace,
            "generated": len(succeeded),
            "attempted": len(results),
            "evidence_avg": round(sum(evidence_counts) / len(evidence_counts), 2) if evidence_counts else 0.0,
            "evidence_counts": evidence_counts,
            "inferred_ratio": round(inferred_dims / total_dims, 4) if total_dims else 0.0,
            "results": [
                {key: value for key, value in entry.items() if key != "doc"}
                for entry in results
            ],
        }
        if with_metrics:
            summary["metrics"] = compute_assessment_metrics(succeeded)
        return summary
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="S6-1/6-2 判人评估回放：抽样 → 真实生成 → 导出盲评 markdown")
    parser.add_argument("--db", default=DEFAULT_DB, help="v3 库路径（生产库回放 artifact 落库；可用备份库副本）")
    parser.add_argument("--job-id", type=int, action="append", required=True, help="岗位 id（可多次指定）")
    parser.add_argument("--limit", type=int, default=5, help="每岗位抽样人数（默认 5）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="导出目录（默认 work/assessment_replay/）")
    parser.add_argument("--no-company-signals", action="store_true", help="跳过公司近况公开采集（工况信号仍计算）")
    parser.add_argument("--metrics", action="store_true",
                        help="附加输出确定性指标 JSON（五维覆盖率/证据条数分布/unknown 占比/推测占比；盲评 markdown 不变）")
    args = parser.parse_args()
    fetcher = (lambda url, timeout: (0, "", "skipped_by_flag")) if args.no_company_signals else None
    summaries = []
    for job_id in args.job_id:
        summary = run_replay(
            Path(args.db).expanduser(),
            job_id,
            limit=max(1, args.limit),
            out_dir=Path(args.out_dir).expanduser(),
            signal_fetcher=fetcher,
            with_metrics=args.metrics,
        )
        summaries.append(summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False, indent=2))
    print(json.dumps({"ok": True, "jobs": len(summaries)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
