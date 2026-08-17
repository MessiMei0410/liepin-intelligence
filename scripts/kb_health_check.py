#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASA 知识库体检/lint 脚本（只读，绝不写任何 KB 文件）。

用途：给知识库装一个「能自己发现问题」的机制——schema 合法性、孤立/重复文件、
三源（图谱↔画像↔CKB）交叉、CKB 空洞字段与低置信度、客户画像字段覆盖，一次跑完，
输出人类可读报告 + 可选 JSON，退出码非 0 表示存在问题（可进 CI）。

消费方事实源（本脚本只核对「谁在读」，不读这些文件的业务语义）：
- seed_*.json（根目录）              → strategy_v2.load_job_archetypes
- kb_seed_*.json                     → negative_rules.load_negative_rule_typology
- kb_client_profiles_v1.json         → knowledge_base.load_client_profiles
- kb_company_graph_jsj_v1.json       → knowledge_base.load_company_graph
- kb_skill_ontology_semiconductor_v1.json → knowledge_base.load_skill_ontology
- kb_level_mapping_v1.json           → knowledge_base.load_level_mapping
- kb_agent_confirmed_rules_v1.json   → knowledge_base.load_confirmed_rules（写入方 asa_core.knowledge_proposals）
- cases/case_*.json                  → knowledge_base.load_restricted_constraints
- cases/seed_silan_tme_v1.json       → strategy_replay_eval（回放 golden，frozen 基线，非孤立）
- client_profiles_public_v1/*_客户档案_v*.md → radar_scan（33 家客户档案公司池）
- cases/*.xlsx、*.md                 → 源数据/参考文档（不参与运行时，但非孤立，属 provenance）

三源统一键：knowledge_base.normalize_client_name（去括号/去后缀/小写/去空白）。
同键即视为同一公司实体，用于交叉链接与「补全源」一致性核对。

用法：
  PYTHONPATH=scripts python3 scripts/kb_health_check.py
  PYTHONPATH=scripts python3 scripts/kb_health_check.py --json
  PYTHONPATH=scripts python3 scripts/kb_health_check.py --kb-dir /tmp/kb --db /tmp/x.db
  PYTHONPATH=scripts python3 scripts/kb_health_check.py --crosslink          # 额外产出交叉链接表

退出码：0 全部健康；1 存在 warning；2 存在 error（schema 坏 / JSON 坏 / 关键文件缺失）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

# scripts/ 在 pythonpath（pytest.ini 已含），可直接从 a_system_agent 导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_system_agent.knowledge_base import (  # noqa: E402
    normalize_client_name,
)
from a_system_agent.strategy_v2 import knowledge_base_dir  # noqa: E402

# ---------------------------------------------------------------------------
# 常量：消费方 glob / 文件名映射（与各模块实际 glob 保持一致）
# ---------------------------------------------------------------------------
CONSUMED_PATTERNS: dict[str, str] = {
    "seed_*.json": "strategy_v2.load_job_archetypes（岗位原型）",
    "kb_seed_*.json": "negative_rules.load_negative_rule_typology（五类负向规则）",
    "kb_client_profiles_v1.json": "knowledge_base.load_client_profiles（客户画像）",
    "kb_company_graph_jsj_v1.json": "knowledge_base.load_company_graph（公司图谱）",
    "kb_skill_ontology_semiconductor_v1.json": "knowledge_base.load_skill_ontology（技能本体）",
    "kb_level_mapping_v1.json": "knowledge_base.load_level_mapping（职级映射）",
    "kb_agent_confirmed_rules_v1.json": "knowledge_base.load_confirmed_rules（顾问确认规则）",
    "cases/case_*.json": "knowledge_base.load_restricted_constraints（restricted 层）",
    "cases/seed_silan_tme_v1.json": "strategy_replay_eval（回放 golden，frozen）",
    "client_profiles_public_v1/*_客户档案_v*.md": "radar_scan（33 家客户档案公司池）",
}

# 源数据/参考文档（不参与运行时，但非孤立）：*.md / *.xlsx 均属 provenance
REFERENCE_SUFFIXES = (".md", ".xlsx", ".xls")

# CKB 表与字段
CKB_TABLE = "company_knowledge"
CKB_EMPTY_FIELDS = (
    "business_desc", "product_lines_json", "tech_stack_json", "org_clues_json",
    "scale", "salary_clues_json", "risk_signals_json", "headhunt_clues_json",
)
_DEFAULT_DB_PATH = Path(
    "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
).expanduser()

# 近重复阈值：归一化后相似度 ≥ 0.9 且字节不同，视为近重复对（可能漂移）
_DUP_RATIO = 0.9


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> tuple[Any, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def _normalize_text(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def _connect_ckb_ro(db_path: Path) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (CKB_TABLE,)
        ).fetchone()
        if not has:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# 1. schema 校验
# ---------------------------------------------------------------------------
def check_schema(kb_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    # seed_*.json 必须带 job_archetype（kb_seed_* 除外）
    for path in sorted(kb_dir.glob("seed_*.json")):
        doc, err = _read_json(path)
        if err:
            errors.append({"file": path.name, "issue": f"JSON 解析失败: {err}"})
            continue
        if not isinstance(doc, dict):
            errors.append({"file": path.name, "issue": "顶层非对象"})
            continue
        ar = doc.get("job_archetype")
        if not isinstance(ar, dict) or not ar.get("archetype_id"):
            errors.append({"file": path.name, "issue": "缺 job_archetype.archetype_id"})
    # kb_seed_*.json 必须带 negative_rule_typology
    for path in sorted(kb_dir.glob("kb_seed_*.json")):
        doc, err = _read_json(path)
        if err:
            errors.append({"file": path.name, "issue": f"JSON 解析失败: {err}"})
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("negative_rule_typology"), dict):
            warnings.append({"file": path.name, "issue": "缺 negative_rule_typology（按 PRD §4 默认五类降级）"})
    # 核心 kb_*.json 结构
    core_checks = {
        "kb_client_profiles_v1.json": "profiles",
        "kb_company_graph_jsj_v1.json": "companies",
        "kb_skill_ontology_semiconductor_v1.json": "families",
        "kb_level_mapping_v1.json": "level_bands",
    }
    for fname, key in core_checks.items():
        path = kb_dir / fname
        if not path.is_file():
            errors.append({"file": fname, "issue": f"缺失（消费方会降级，但数据不可用）"})
            continue
        doc, err = _read_json(path)
        if err:
            errors.append({"file": fname, "issue": f"JSON 解析失败: {err}"})
            continue
        if not isinstance(doc, dict) or key not in doc:
            errors.append({"file": fname, "issue": f"缺顶层键 {key}"})
    # cases/case_*.json 必须带 client_profile + restricted
    for path in sorted((kb_dir / "cases").glob("case_*.json")) if (kb_dir / "cases").is_dir() else []:
        doc, err = _read_json(path)
        if err:
            errors.append({"file": f"cases/{path.name}", "issue": f"JSON 解析失败: {err}"})
            continue
        if not isinstance(doc, dict):
            errors.append({"file": f"cases/{path.name}", "issue": "顶层非对象"})
            continue
        cp = doc.get("client_profile") or {}
        case_client = str(cp.get("name") or cp.get("client") or "").strip() if isinstance(cp, dict) else ""
        if not case_client:
            warnings.append({"file": f"cases/{path.name}", "issue": "client_profile 缺 name/client，restricted 层永不命中"})
        if not isinstance(doc.get("restricted"), dict):
            warnings.append({"file": f"cases/{path.name}", "issue": "缺 restricted 层"})
    return errors, warnings


# ---------------------------------------------------------------------------
# 2. 孤立文件检测
# ---------------------------------------------------------------------------
def check_orphans(kb_dir: Path) -> list[dict[str, Any]]:
    orphans: list[dict[str, Any]] = []
    for path in sorted(kb_dir.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        rel = path.relative_to(kb_dir).as_posix()
        if path.suffix in REFERENCE_SUFFIXES:
            continue  # md/xlsx 属 provenance
        # 匹配任一消费 glob（glob 语义：非递归，但 cases/ 与 client_profiles_public_v1/ 有前缀）
        consumed = False
        for pattern in CONSUMED_PATTERNS:
            if rel in {pattern}:
                consumed = True
                break
            # 用 fnmatch 处理带目录前缀的 glob
            if Path(rel).match(pattern):
                consumed = True
                break
        # 显式前缀匹配（glob 的 * 不跨目录）
        if rel.startswith("cases/case_") and rel.endswith(".json"):
            consumed = True
        if rel.startswith("client_profiles_public_v1/") and "_客户档案_v" in rel and rel.endswith(".md"):
            consumed = True
        if not consumed:
            orphans.append({"file": rel, "issue": "无消费方引用（可能孤立/漂移副本）"})
    return orphans


# ---------------------------------------------------------------------------
# 3. 近重复检测（字节不同但内容高度重合）
# ---------------------------------------------------------------------------
def check_near_duplicates(kb_dir: Path) -> list[dict[str, Any]]:
    json_files = [p for p in kb_dir.rglob("*.json") if "__pycache__" not in p.parts]
    texts: list[tuple[str, str]] = []
    for p in json_files:
        try:
            texts.append((p.relative_to(kb_dir).as_posix(), _normalize_text(p.read_text(encoding="utf-8"))))
        except OSError:
            continue
    dups: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a_name, a_text = texts[i]
            b_name, b_text = texts[j]
            if a_name == b_name:
                continue
            pair = tuple(sorted((a_name, b_name)))
            if pair in seen:
                continue
            seen.add(pair)
            if not a_text or not b_text:
                continue
            # 简单相似度：较长的包含较短的 / 长度差占比
            shorter, longer = sorted((a_text, b_text), key=len)
            if longer and len(shorter) / len(longer) >= _DUP_RATIO:
                if shorter in longer or longer[: len(shorter)] == shorter:
                    dups.append({
                        "files": [a_name, b_name],
                        "issue": "内容近重复（≥90% 重合），疑似漂移副本，需核对是否 frozen golden",
                    })
    return dups


# ---------------------------------------------------------------------------
# 4. 三源交叉（图谱 ↔ 画像 ↔ CKB）
# ---------------------------------------------------------------------------
def load_company_names(kb_dir: Path) -> dict[str, dict[str, set[str]]]:
    """返回 {源名: {归一键: {原名...}}}，用于交叉。"""
    result: dict[str, dict[str, set[str]]] = {}
    # 图谱
    graph_names: dict[str, set[str]] = {}
    doc, _ = _read_json(kb_dir / "kb_company_graph_jsj_v1.json")
    if isinstance(doc, dict) and isinstance(doc.get("companies"), dict):
        for name in doc["companies"]:
            graph_names.setdefault(normalize_client_name(name), set()).add(str(name))
    result["graph"] = graph_names
    # 画像
    prof_names: dict[str, set[str]] = {}
    doc, _ = _read_json(kb_dir / "kb_client_profiles_v1.json")
    if isinstance(doc, dict) and isinstance(doc.get("profiles"), list):
        for p in doc["profiles"]:
            if isinstance(p, dict) and str(p.get("client") or "").strip():
                prof_names.setdefault(normalize_client_name(p["client"]), set()).add(str(p["client"]))
    result["profiles"] = prof_names
    return result


def _ckb_company_keys(db_path: Path) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    conn = _connect_ckb_ro(db_path)
    if conn is None:
        return keys
    try:
        rows = conn.execute(
            f"SELECT company_key, name, aliases_json FROM {CKB_TABLE} "
            "WHERE company_key IS NOT NULL AND trim(company_key)<>''"
        ).fetchall()
        for row in rows:
            for raw in (row["company_key"], row["name"]):
                if str(raw or "").strip():
                    keys.setdefault(normalize_client_name(raw), set()).add(str(raw))
            for alias in _loads_list(row["aliases_json"]):
                keys.setdefault(normalize_client_name(alias), set()).add(alias)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return keys


def _loads_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or ""))
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x or "").strip()]


def check_crosslink(kb_dir: Path, db_path: Path) -> dict[str, Any]:
    """三源交叉链接：返回摘要 + 链接表（同键公司实体）。"""
    sources = load_company_names(kb_dir)
    graph = sources.get("graph", {})
    profiles = sources.get("profiles", {})
    ckb = _ckb_company_keys(db_path)

    gkeys, pkeys, ckeys = set(graph), set(profiles), set(ckb)
    overlap = {
        "graph_intersect_profiles": len(gkeys & pkeys),
        "graph_intersect_ckb": len(gkeys & ckeys),
        "profiles_intersect_ckb": len(pkeys & ckeys),
        "graph_only": len(gkeys - pkeys - ckeys),
        "profiles_only": len(pkeys - gkeys - ckeys),
        "ckb_only": len(ckeys - gkeys - pkeys),
    }
    # 链接表：出现在 ≥2 源的实体（统一键命中的跨源公司），按键排序。
    all_keys = gkeys | pkeys | ckeys
    link_table: list[dict[str, Any]] = []
    multi = 0
    for key in sorted(all_keys):
        in_graph = key in gkeys
        in_prof = key in pkeys
        in_ckb = key in ckeys
        if sum((in_graph, in_prof, in_ckb)) < 2:
            continue
        multi += 1
        link_table.append({
            "key": key,
            "graph_names": sorted(graph[key]) if in_graph else [],
            "profile_names": sorted(profiles[key]) if in_prof else [],
            "ckb_names": sorted(ckb[key]) if in_ckb else [],
            "sources": [s for s, flag in (("graph", in_graph), ("profiles", in_prof), ("ckb", in_ckb)) if flag],
        })
    return {
        "counts": {"graph": len(gkeys), "profiles": len(pkeys), "ckb": len(ckeys)},
        "overlap": overlap,
        "multi_source_count": multi,
        "link_table": link_table,
    }


# ---------------------------------------------------------------------------
# 5. CKB 空洞 / 低置信度
# ---------------------------------------------------------------------------
def check_ckb_gaps(db_path: Path) -> dict[str, Any]:
    conn = _connect_ckb_ro(db_path)
    if conn is None:
        return {"available": False, "reason": "CKB 不可用（无库/无表）"}
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {CKB_TABLE}").fetchone()[0]
        low = conn.execute(f"SELECT COUNT(*) FROM {CKB_TABLE} WHERE confidence < 0.5").fetchone()[0]
        empty: dict[str, int] = {}
        for f in CKB_EMPTY_FIELDS:
            empty[f] = conn.execute(
                f"SELECT COUNT(*) FROM {CKB_TABLE} WHERE {f} IS NULL OR trim({f})='' OR trim({f})='[]'"
            ).fetchone()[0]
        # 待补清单：低置信度或核心字段空的公司
        rows = conn.execute(
            f"""SELECT company_key, name, confidence, evidence_count,
                       (CASE WHEN salary_clues_json IS NULL OR trim(salary_clues_json) IN ('','[]') THEN 1 ELSE 0 END
                        + CASE WHEN risk_signals_json IS NULL OR trim(risk_signals_json) IN ('','[]') THEN 1 ELSE 0 END) AS gap_score
                  FROM {CKB_TABLE}
                 WHERE confidence < 0.5 OR salary_clues_json IS NULL OR trim(salary_clues_json) IN ('','[]')
                    OR risk_signals_json IS NULL OR trim(risk_signals_json) IN ('','[]')
                 ORDER BY confidence ASC, gap_score DESC"""
        ).fetchall()
        todo = [
            {
                "company_key": r["company_key"], "name": r["name"],
                "confidence": round(float(r["confidence"] or 0), 3),
                "evidence_count": int(r["evidence_count"] or 0), "gap_score": int(r["gap_score"] or 0),
            }
            for r in rows
        ]
    except sqlite3.Error as exc:
        return {"available": False, "reason": f"查询失败: {exc}"}
    finally:
        conn.close()
    return {
        "available": True, "total": total, "low_confidence": low,
        "empty_fields": empty, "todo_count": len(todo), "todo": todo,
    }


# ---------------------------------------------------------------------------
# 6. 客户画像字段覆盖
# ---------------------------------------------------------------------------
def check_profile_coverage(kb_dir: Path) -> dict[str, Any]:
    doc, err = _read_json(kb_dir / "kb_client_profiles_v1.json")
    if err or not isinstance(doc, dict):
        return {"available": False, "reason": err or "画像库不可用"}
    profiles = [p for p in doc.get("profiles") or [] if isinstance(p, dict)]
    stats = doc.get("stats") or {}
    field_coverage = stats.get("field_coverage") or {}
    return {
        "available": True, "profiles": len(profiles),
        "declared": {
            "total_clients": stats.get("total_clients"),
            "profiled": stats.get("profiled"),
            "rich_profiles": stats.get("rich_profiles"),
        },
        "field_coverage": field_coverage,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run(kb_dir: Path, db_path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "kb_dir": str(kb_dir),
        "db_path": str(db_path),
        "schema": {}, "orphans": [], "near_duplicates": [],
        "crosslink": {}, "ckb": {}, "profile_coverage": {},
    }
    errors, warnings = check_schema(kb_dir)
    report["schema"] = {"errors": errors, "warnings": warnings}
    report["orphans"] = check_orphans(kb_dir)
    report["near_duplicates"] = check_near_duplicates(kb_dir)
    report["crosslink"] = check_crosslink(kb_dir, db_path)
    report["ckb"] = check_ckb_gaps(db_path)
    report["profile_coverage"] = check_profile_coverage(kb_dir)
    return report


def _render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"知识库目录：{report['kb_dir']}")
    lines.append(f"CKB 库：{report['db_path']}")
    lines.append("")

    schema = report["schema"]
    lines.append("=" * 70)
    lines.append(f"1. schema 校验：{len(schema['errors'])} error / {len(schema['warnings'])} warning")
    for e in schema["errors"]:
        lines.append(f"  [ERR] {e['file']}: {e['issue']}")
    for w in schema["warnings"]:
        lines.append(f"  [WARN] {w['file']}: {w['issue']}")

    lines.append("")
    lines.append("=" * 70)
    lines.append(f"2. 孤立文件（无消费方引用）：{len(report['orphans'])} 个")
    for o in report["orphans"]:
        lines.append(f"  [ORPHAN] {o['file']} — {o['issue']}")

    lines.append("")
    lines.append("=" * 70)
    lines.append(f"3. 近重复（≥90% 重合，可能漂移）：{len(report['near_duplicates'])} 对")
    for d in report["near_duplicates"]:
        lines.append(f"  [DUP] {' ↔ '.join(d['files'])} — {d['issue']}")

    cl = report["crosslink"]
    if cl.get("counts"):
        lines.append("")
        lines.append("=" * 70)
        lines.append("4. 三源交叉（图谱↔画像↔CKB，统一键 = normalize_client_name）")
        lines.append(f"  图谱 {cl['counts']['graph']} / 画像 {cl['counts']['profiles']} / CKB {cl['counts']['ckb']}")
        ov = cl["overlap"]
        lines.append(f"  图谱∩画像 = {ov['graph_intersect_profiles']} | 图谱∩CKB = {ov['graph_intersect_ckb']} | 画像∩CKB = {ov['profiles_intersect_ckb']}")
        lines.append(f"  图谱独有 {ov['graph_only']} | 画像独有 {ov['profiles_only']} | CKB 独有 {ov['ckb_only']}")
        lines.append(f"  跨 ≥2 源的实体 {cl.get('multi_source_count')} 家")

    ckb = report["ckb"]
    lines.append("")
    lines.append("=" * 70)
    if ckb.get("available"):
        lines.append(f"5. CKB 空洞/低置信度：总 {ckb['total']} 家，低置信度(<0.5) {ckb['low_confidence']} 家")
        for f, n in ckb["empty_fields"].items():
            pct = f"{n / ckb['total'] * 100:.0f}%" if ckb["total"] else "n/a"
            lines.append(f"  {f}: 空 {n}/{ckb['total']}（{pct}）")
        lines.append(f"  待补清单（低置信度或 salary/risk 空）：{ckb['todo_count']} 家")
    else:
        lines.append(f"5. CKB：不可用（{ckb.get('reason')}）")

    pc = report["profile_coverage"]
    lines.append("")
    lines.append("=" * 70)
    if pc.get("available"):
        lines.append(f"6. 客户画像：{pc['profiles']} 条 | 声明 {pc['declared']}")
        fc = pc.get("field_coverage") or {}
        if fc:
            lines.append(f"  字段覆盖 top10：{dict(list(sorted(fc.items(), key=lambda x: -x[1]))[:10])}")
    else:
        lines.append(f"6. 客户画像：不可用（{pc.get('reason')}）")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASA 知识库体检/lint（只读）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--kb-dir", default="", help="覆盖知识库目录（缺省 ASA_KNOWLEDGE_BASE_DIR/默认）")
    parser.add_argument("--db", default="", help="覆盖 CKB 库路径（缺省 A_SYSTEM_DB/默认生产库）")
    parser.add_argument("--crosslink", default="", help="额外把交叉链接表写到该 JSON 文件")
    args = parser.parse_args(argv)

    kb_dir = Path(args.kb_dir) if args.kb_dir else knowledge_base_dir()
    raw_db = args.db or os.environ.get("A_SYSTEM_DB", "") or str(_DEFAULT_DB_PATH)
    db_path = Path(raw_db).expanduser()

    report = run(kb_dir, db_path)

    if args.crosslink:
        out = Path(args.crosslink)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report["crosslink"], ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render(report))

    has_error = bool(report["schema"]["errors"]) or (report["ckb"].get("available") is False)
    has_warning = bool(
        report["schema"]["warnings"] or report["orphans"]
        or report["near_duplicates"] or (report["ckb"].get("low_confidence", 0) > 0)
    )
    return 2 if has_error else 1 if has_warning else 0


if __name__ == "__main__":
    raise SystemExit(main())
