#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公司知识库 CKB M3-1：把开发库中的 company_knowledge / company_evidence
迁移到 ASA 生产库（talent_system_v3_20260629.db）。

安全设计：
- 目标表在生产库不存在时创建；已存在则跳过（幂等）。
- 数据源是 dev 库，生产库写坏可随时 DROP 重来，无需全库备份。
- 量小（~1500 行 + ~1 万行证据），事务内完成。

用法:
  python3 scripts/company_kb/migrate_to_prod.py
"""

import json
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEV_DB = os.path.join(PROJECT_ROOT, "outputs", "company_kb_dev.db")
PROD_DB = os.path.expanduser(
    os.environ.get(
        "A_SYSTEM_DB",
        "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db",
    )
)

DDL_KNOWLEDGE = """
CREATE TABLE IF NOT EXISTS company_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    aliases_json TEXT DEFAULT '[]',
    industry TEXT DEFAULT '',
    business_desc TEXT DEFAULT '',
    product_lines_json TEXT DEFAULT '[]',
    tech_stack_json TEXT DEFAULT '[]',
    org_clues_json TEXT DEFAULT '[]',
    scale TEXT DEFAULT '',
    salary_clues_json TEXT DEFAULT '[]',
    risk_signals_json TEXT DEFAULT '[]',
    headhunt_clues_json TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'auto',
    error_message TEXT,
    last_extracted_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_company_knowledge_industry ON company_knowledge(industry);
CREATE INDEX IF NOT EXISTS idx_company_knowledge_status ON company_knowledge(status);
"""

DDL_EVIDENCE = """
CREATE TABLE IF NOT EXISTS company_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_value TEXT DEFAULT '',
    quote TEXT DEFAULT '',
    source_ref TEXT DEFAULT '',
    confidence REAL DEFAULT 0.8,
    model_version TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_company_evidence_key ON company_evidence(company_key);
"""


def main():
    if not os.path.exists(DEV_DB):
        print(f"[ERR] 开发库不存在: {DEV_DB}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(PROD_DB):
        print(f"[ERR] 生产库不存在: {PROD_DB}", file=sys.stderr)
        sys.exit(1)

    dev = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    prod = sqlite3.connect(PROD_DB)

    # 1. 建表（幂等）
    prod.executescript(DDL_KNOWLEDGE)
    prod.executescript(DDL_EVIDENCE)
    prod.commit()
    print("[OK] 生产库表结构就绪（company_knowledge / company_evidence）")

    # 2. 数据同步：开发库 -> 生产库（增量 upsert：已有行更新、新行插入）
    #    注意：company_knowledge 是自动层，人工结论在 company_calibrations（不同表），
    #    覆盖自动层安全；status='error' 的行也同步过去（保留错误标记）。
    cols = [
        "company_key", "name", "aliases_json", "industry", "business_desc",
        "product_lines_json", "tech_stack_json", "org_clues_json", "scale",
        "salary_clues_json", "risk_signals_json", "headhunt_clues_json",
        "confidence", "evidence_count", "source_count", "status",
        "error_message", "last_extracted_at", "created_at", "updated_at",
    ]
    rows = dev.execute(
        f"SELECT {', '.join(cols)} FROM company_knowledge ORDER BY company_key"
    ).fetchall()
    ph = ",".join("?" * len(cols))
    prod.executemany(
        f"INSERT OR REPLACE INTO company_knowledge ({', '.join(cols)}) VALUES ({ph})",
        rows,
    )
    print(f"[OK] 画像导入 {len(rows)} 条")

    # 4. 搬运证据（先清后插，保证增量同步不重复）
    ecols = ["company_key", "fact_type", "fact_value", "quote",
             "source_ref", "confidence", "model_version", "created_at"]
    erows = dev.execute(
        f"SELECT {', '.join(ecols)} FROM company_evidence ORDER BY company_key"
    ).fetchall()
    # 生产库中将被更新的公司集合（先清旧证据）
    touched = {r[0] for r in erows}
    prod.executemany("DELETE FROM company_evidence WHERE company_key=?", [(k,) for k in touched])
    eph = ",".join("?" * len(ecols))
    prod.executemany(
        f"INSERT INTO company_evidence ({', '.join(ecols)}) VALUES ({eph})",
        erows,
    )
    print(f"[OK] 证据同步 {len(erows)} 条（{len(touched)} 家公司）")

    prod.commit()
    prod.close()
    dev.close()
    print(f"[DONE] 迁移完成 -> {PROD_DB}")


if __name__ == "__main__":
    main()
