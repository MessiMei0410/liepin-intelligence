#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CKB 数据治理：L2 候选合并组 LLM 确认 + 合并执行。

背景：M1 的公司名变体合并（L1 剥法律后缀）已把同一家的常见写法并掉，
但 L2 弱合并组（去通用词/地域词后相同）里仍可能残留同一家的多个实体
（如“中微公司/中微半导体/中微半导体设备上海”都是 AMEC）。

本脚本：
1. 对组内最大 total>=10 的 L2 组，调 DeepSeek 确认“是否为同一企业主体”
   （规则：同一集团但不同法人实体如母子公司 → 不算同一家，不合并）
2. same=true → 合并：evidence 全部改挂主 key；knowledge 行 aliases 合并后删除
3. 执行前备份受影响行到 outputs/company_kb_merge_backup.json（可回滚）

用法: python3 scripts/company_kb/merge_l2_groups.py [--min-total 10] [--limit N] [--apply]
     默认只生成合并计划(--apply 才写生产库)
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_DB = PROJECT_ROOT / "outputs" / "company_kb_dev.db"
PROD_DB = os.path.expanduser(
    os.environ.get(
        "A_SYSTEM_DB",
        "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db",
    )
)
BACKUP = PROJECT_ROOT / "outputs" / "company_kb_merge_backup.json"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


def load_api_key():
    env = Path(os.path.expanduser("~/.hermes/.env"))
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("DEEPSEEK_API_KEY 未找到")


def confirm_same(api_key, members):
    """调 LLM 确认一组公司名是否同一企业主体。返回 (same, main_name, note)"""
    names = "\n".join(f"- {m[0]}" for m in members)
    prompt = (
        "以下是同一组候选的公司名称变体，请判断它们是否指向【同一家企业主体】。\n"
        "规则：\n"
        "1. 名称变体/简称/加地域写法指向同一法人实体 → same=true，main 填正式名\n"
        "2. 同一集团下不同法人实体（母公司/子公司/异地独立法人）→ same=false（不合并）\n"
        "3. 完全不同的公司 → same=false\n"
        f"公司名列表：\n{names}\n"
        '只输出 JSON：{"same": true/false, "main": "主名(同族时)", "note": "一句话理由"}'
    )
    r = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
            "max_tokens": 200,
        },
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()["choices"][0]["message"]["content"]
    try:
        d = json.loads(data)
    except Exception:
        m = re.search(r"\{.*\}", data, re.S)
        d = json.loads(m.group(0)) if m else {}
    return bool(d.get("same")), str(d.get("main") or ""), str(d.get("note") or "")[:120]


def main():
    apply = "--apply" in sys.argv
    min_total = 10
    if "--min-total" in sys.argv:
        min_total = int(sys.argv[sys.argv.index("--min-total") + 1])
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    dev = sqlite3.connect(f"file:{DEV_DB}?mode=ro", uri=True)
    groups = [
        (gid, json.loads(members))
        for gid, members, _ in dev.execute("SELECT group_id, members_json, size FROM l2_groups")
    ]
    dev.close()
    big = [g for g in groups if max(m[1] for m in g[1]) >= min_total]
    if limit:
        big = big[:limit]
    print(f"待确认组: {len(big)}（min_total>={min_total}）")

    api_key = load_api_key()
    plan_file = PROJECT_ROOT / "outputs" / "l2_merge_plan.json"
    # apply 模式复用已生成的计划（不重复调 LLM 确认）；--reconfirm 才强制重跑
    if apply and plan_file.exists() and "--reconfirm" not in sys.argv:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        print(f"复用已生成计划 outputs/l2_merge_plan.json（{len(plan)} 组，--reconfirm 可强制重跑）")
        merges = [p for p in plan if p["same"]]
        print(f"== 计划中建议合并: {len(merges)} 组")
    else:
        plan = []  # [{group_id, members, same, main, note}]
        for i, (gid, members) in enumerate(big, 1):
            same, main, note = False, "", ""
            for attempt in range(3):
                try:
                    same, main, note = confirm_same(api_key, members)
                    break
                except Exception as e:
                    print(f"  G{gid} 第{attempt+1}次失败: {e}")
                    time.sleep(2)
            plan.append({"group_id": gid, "members": members, "same": same, "main": main, "note": note})
            mark = "合并" if same else "保留"
            print(f"[{i}/{len(big)}] G{gid} {mark}: {note[:60]} ({len(members)}成员)")
            time.sleep(0.3)

        with open(plan_file, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)
        merges = [p for p in plan if p["same"]]
        print(f"\n== 确认结果: {len(merges)}/{len(plan)} 组建议合并，计划已存 outputs/l2_merge_plan.json")

        if not apply:
            print("（未执行 --apply，仅出计划。执行合并: python3 merge_l2_groups.py --apply）")
            return

    # ---- 执行合并（生产库 + 开发库同步，先备份） ----
    prod = sqlite3.connect(PROD_DB)
    prod.row_factory = sqlite3.Row
    # dev 库也要同步合并，否则定时刷新时 migrate 全量 upsert 会把已合并实体复活
    dev_w = sqlite3.connect(DEV_DB)
    dev_w.row_factory = sqlite3.Row
    backup = {"knowledge": [], "evidence": []}
    for p in merges:
        for m in p["members"]:
            key = m[0]
            if key == p["main"]:
                continue
            rows = prod.execute("SELECT * FROM company_knowledge WHERE company_key=?", (key,)).fetchall()
            for r in rows:
                backup["knowledge"].append(dict(r))
            evs = prod.execute("SELECT * FROM company_evidence WHERE company_key=?", (key,)).fetchall()
            for e in evs:
                backup["evidence"].append(dict(e))
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=1)
    print(f"备份 {len(backup['knowledge'])} 行画像 + {len(backup['evidence'])} 行证据 -> {BACKUP}")

    merged = 0
    for p in merges:
        # 主 key = 组内 total 最大的 member（canonical 主实体）
        main_member = max(p["members"], key=lambda m: m[1])
        main_key = main_member[0]
        for con, tag in ((prod, "生产库"), (dev_w, "开发库")):
            main_row = con.execute(
                "SELECT * FROM company_knowledge WHERE company_key=?", (main_key,)
            ).fetchone()
            if not main_row:
                print(f"  [{tag}] 跳过 G{p['group_id']}: 主实体 {main_key} 不存在")
                continue
            aliases = json.loads(main_row["aliases_json"] or "[]")
            for m in p["members"]:
                key = m[0]
                if key == main_key:
                    continue
                row = con.execute("SELECT * FROM company_knowledge WHERE company_key=?", (key,)).fetchone()
                if not row:
                    continue
                for a in json.loads(row["aliases_json"] or "[]"):
                    if a not in aliases:
                        aliases.append(a)
                if key not in aliases:
                    aliases.append(key)
                # 证据改挂主 key
                con.execute("UPDATE company_evidence SET company_key=? WHERE company_key=?", (main_key, key))
                # 删除被合并画像行
                con.execute("DELETE FROM company_knowledge WHERE company_key=?", (key,))
            con.execute(
                "UPDATE company_knowledge SET aliases_json=?, updated_at=datetime('now','localtime') WHERE company_key=?",
                (json.dumps(aliases, ensure_ascii=False), main_key),
            )
        merged += 1
        print(f"  ✓ 合并 G{p['group_id']}: {len(p['members'])} 个变体 -> {main_key} (aliases 现 {len(json.loads(prod.execute('SELECT aliases_json FROM company_knowledge WHERE company_key=?', (main_key,)).fetchone()[0]) or '[]')} 个)")
    prod.commit()
    dev_w.commit()
    prod.close()
    dev_w.close()
    print(f"[DONE] 合并 {merged} 组。生产库+开发库已同步更新。")


if __name__ == "__main__":
    main()
