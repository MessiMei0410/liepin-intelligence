#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公司知识库 CKB 定向重提取（补可提取字段 / 低置信度复核）。

背景（kb_health_check.py 的体检结论）：
- salary_clues / risk_signals 的空洞是【源限】——简历聚合来源基本不含薪资/风险信息
  （company_evidence 里 salary 仅 2.9%、risk 仅 1.1%），重提取同源简历不会改善，故本脚本默认跳过。
- product_lines / tech_stack / business_desc / org_clues / headhunt_clues 的空洞是【可重提取】；
- confidence < 0.5 的是【提取不确定】，重提取/复核最有价值。

本脚本只针对「可重提取」空洞 + 低置信度公司，复用 extract_company_profiles 的
简历聚合 + DeepSeek 提取 + 幂等 upsert 逻辑；写入 outputs/company_kb_dev.db（开发库），
绝不写生产库（生产库只读）。跑完由顾问复核后走 migrate_to_prod.py 上生产。

安全约束：
- 生产库一律 URI mode=ro 只读；
- 默认 --dry-run（只列目标不调 LLM、不写库），真正执行必须显式 --run；
- salary/risk 默认跳过（--include-source-limited 才带上）。

用法：
  PYTHONPATH=scripts python3 scripts/company_kb/reextract_gaps.py --dry-run
  PYTHONPATH=scripts python3 scripts/company_kb/reextract_gaps.py --run --limit 20
  PYTHONPATH=scripts python3 scripts/company_kb/reextract_gaps.py --run --min-confidence 0.5 --fields product,tech
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from company_kb.extract_company_profiles import (  # noqa: E402
    DEV_DB,
    MAX_RESUMES_PER_COMPANY,
    call_deepseek,
    connect_prod_ro,
    fetch_resume_texts,
    init_dev_db,
    load_api_key,
    normalize_evidence,
    save_error,
    save_profile,
)

# 可重提取字段（空才补）→ 对应 LLM 输出键
FILLABLE_FIELDS = {
    "business_desc": "business_desc",
    "product_lines": "product_lines_json",
    "tech_stack": "tech_stack_json",
    "org_clues": "org_clues_json",
    "headhunt_clues": "headhunt_clues_json",
}


def is_empty(value: str | None) -> bool:
    return value is None or str(value).strip() in ("", "[]")


def select_targets(min_confidence: float, fields: list[str], include_zero_evidence: bool) -> list[dict[str, object]]:
    """从生产库（只读）选出重提取目标：低置信度 或 指定可重提取字段为空。

    默认排除零证据公司（evidence_count=0：无简历来源，重提取无意义）。
    """
    prod_con = connect_prod_ro()
    try:
        conds: list[str] = ["confidence < ?"]
        params: list[object] = [min_confidence]
        for f in fields:
            col = FILLABLE_FIELDS[f]
            conds.append(f"({col} IS NULL OR trim({col})='' OR trim({col})='[]')")
        where = " OR ".join(conds)
        if not include_zero_evidence:
            where = f"evidence_count > 0 AND ({where})"
        sql = (
            "SELECT company_key, name, aliases_json, confidence, evidence_count, source_count "
            f"FROM company_knowledge WHERE {where}"
            " ORDER BY confidence ASC, evidence_count ASC"
        )
        rows = prod_con.execute(sql, params).fetchall()
        targets = []
        for r in rows:
            try:
                aliases = json.loads(r["aliases_json"] or "[]")
            except ValueError:
                aliases = []
            targets.append({
                "company_key": r["company_key"],
                "name": r["name"],
                "aliases": aliases if isinstance(aliases, list) else [],
                "confidence": float(r["confidence"] or 0),
                "evidence_count": int(r["evidence_count"] or 0),
            })
        return targets
    finally:
        prod_con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CKB 定向重提取（补可提取字段/低置信度复核）")
    parser.add_argument("--dry-run", action="store_true", help="只列目标，不调 LLM、不写库（默认）")
    parser.add_argument("--run", action="store_true", help="真正执行（调 LLM + 写开发库）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 家（0=全部）")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="低于该置信度纳入目标")
    parser.add_argument("--fields", default="product_lines,tech_stack,business_desc,org_clues,headhunt_clues",
                        help="可重提取字段（逗号分隔），空则补；salary/risk 默认不含（源限）")
    parser.add_argument("--include-source-limited", action="store_true", help="也补 salary/risk（不推荐，源限）")
    parser.add_argument("--include-zero-evidence", action="store_true", help="也含零证据公司（不推荐，无简历来源）")
    parser.add_argument("--workers", type=int, default=4, help="并发数")
    args = parser.parse_args(argv)

    fields = [f.strip() for f in args.fields.split(",") if f.strip() in FILLABLE_FIELDS]
    if args.include_source_limited:
        fields.extend(["salary_clues", "risk_signals"])
    targets = select_targets(args.min_confidence, fields, args.include_zero_evidence)
    if args.limit:
        targets = targets[: args.limit]

    print(f"重提取目标 {len(targets)} 家（min_confidence<{args.min_confidence} 或字段空）")
    print(f"字段：{fields}")
    if not targets:
        print("无目标，退出。")
        return 0

    if args.dry_run or not args.run:
        print("\n[dry-run] 将重提取（不调 LLM）：")
        for t in targets[:100]:
            print(f"  {t['company_key']}（conf={t['confidence']:.2f}, ev={t['evidence_count']}）")
        if len(targets) > 100:
            print(f"  … 共 {len(targets)} 家")
        print("\n提示：确认后加 --run 真正执行（写入 outputs/company_kb_dev.db，复核后 migrate_to_prod.py）。")
        return 0

    api_key = load_api_key()
    dev_con = init_dev_db(DEV_DB)
    ok = no_source = err = 0
    for i, company in enumerate(targets, 1):
        key = str(company["company_key"])
        prod_con = connect_prod_ro()
        try:
            text, source_count = fetch_resume_texts(prod_con, key, company.get("aliases") or [])
        finally:
            prod_con.close()
        if not text.strip():
            no_source += 1
            print(f"[{i}/{len(targets)}] {key}: 无简历文本，跳过", flush=True)
            continue
        profile, error = call_deepseek(api_key, company, text, source_count)
        if profile is None:
            save_error(dev_con, company, error or "LLM 失败", source_count)
            err += 1
            print(f"[{i}/{len(targets)}] {key}: 失败 {error}", flush=True)
            continue
        evidence = normalize_evidence(profile.get("evidence"))
        save_profile(dev_con, company, profile, evidence, source_count)
        ok += 1
        print(f"[{i}/{len(targets)}] {key}: ok（{len(evidence)} 证据 / {source_count} 简历）", flush=True)

    dev_con.close()
    print(f"\n完成：ok={ok} 无源={no_source} 失败={err}（已写 {DEV_DB}）")
    print("下一步：复核后 python3 scripts/company_kb/migrate_to_prod.py 上生产。")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
