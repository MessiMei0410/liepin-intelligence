#!/usr/bin/env python3
"""M1: 公司实体识别与规范化（公司知识库 CKB 第一步）

数据源（只读打开 ASA 生产库）:
  - agent_candidate_recalls.company        当前公司（猎聘搜索直接给, 最干净）
  - agent_candidate_recalls.raw_json.work_text  历史雇主（正则解析简历工作经历段落）
  - people.current_company                 人才库主表
  - source_profiles.company                寻访来源档案

流程:
  1. 提取原始公司名 + 计数
  2. 清洗占位名/噪音
  3. 规范化: 去法律后缀/地域括号/全半角统一 -> 短名
  4. L1 强合并(短名完全相同) / L2 弱合并候选(通用词剥离后前缀包含)
  5. 输出 companies 清单到开发中间库 company_kb_dev.db (不动生产库)

用法:
  python3 scripts/company_kb/build_company_kb.py [--out outputs/company_kb_dev.db]
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

SRC_DB = os.path.expanduser(
    os.environ.get(
        "A_SYSTEM_DB",
        "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db",
    )
)

# ---------------- 占位名 / 噪音 ----------------
PLACEHOLDER_PATTERNS = [
    "候选人目前没有工作",
    "我还不知道候选人",
    "公司待确认",
    "待确认",
    "未知",
    "无公司",
    "自由职业",
    "个体",
    "保密",
    "其他",
    "无",
]
CLEANUP_PATTERNS = [
    r"（.*?）",  # 全角括号内容
    r"\(.*?\)",  # 半角括号内容(如 上海)
    r"\s*-\s*.*$",  # "- 至今" 残留
    r"\d{4}年?.*$",  # 年份残留
]

# ---------------- 法律后缀（L1 规范化用） ----------------
LEGAL_SUFFIXES = [
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "股份公司",
    "集团公司",
    "集团",
    "股份有限公司(上市)",
    "股份有限公司（上市）",
]

# ---------------- 通用业务词（L2 弱合并时才剥离, 防止误并） ----------------
GENERIC_WORDS = [
    "科技", "技术", "电子", "精密", "智能", "自动化", "半导体", "装备",
    "工业", "产业", "国际", "股份",
]
# 地域词: 前缀和后缀都可剥离（上海精测 -> 精测; 瑞萨半导体北京 -> 瑞萨半导体）
REGION_WORDS = [
    "中国", "上海", "北京", "苏州", "深圳", "杭州", "武汉", "无锡", "常州",
    "南京", "沈阳", "合肥", "宁波", "东莞", "广州", "成都", "西安", "天津",
    "重庆", "珠海", "厦门", "长沙", "青岛", "大连", "嘉兴", "南通", "佛山",
    "中山", "泉州", "温州", "台州", "绍兴", "湖州", "扬州", "镇江", "泰州",
    "徐州", "烟台", "潍坊", "济南", "郑州", "太原", "石家庄", "哈尔滨",
    "长春", "兰州", "昆明", "贵阳", "南昌", "福州", "泉州",
]


def clean_raw(name: str) -> str | None:
    """清洗原始公司名, 返回 None 表示占位/噪音"""
    s = (name or "").strip()
    if not s:
        return None
    # 去引号/特殊符号
    s = s.replace("“", "").replace("”", "").replace('"', "").strip()
    if len(s) < 2 or len(s) > 40:
        return None
    for p in PLACEHOLDER_PATTERNS:
        if p in s:
            return None
    # 去括号内容（地域标注等）但保留公司核心名
    s = re.sub(r"[（(][^（()）]*[)）]", "", s).strip()
    if not s:
        return None
    # 残留未配对的括号/特殊符号 -> 残渣, 丢弃
    if any(ch in s for ch in "（）()·|、。"):
        return None
    for p in PLACEHOLDER_PATTERNS:
        if p in s:
            return None
    return s


def to_short_name(name: str) -> str:
    """规范化: 去法律后缀 -> 短名 (L1 合并键)"""
    s = name.strip()
    # 全半角/大小写统一
    s = s.replace("（", "(").replace("）", ")")
    # 迭代剥法律后缀
    changed = True
    while changed:
        changed = False
        for suf in sorted(LEGAL_SUFFIXES, key=len, reverse=True):
            if s.endswith(suf) and len(s) > len(suf) + 1:
                s = s[: -len(suf)].strip()
                changed = True
    return s.strip()


def to_minimal_name(short: str) -> str:
    """剥离通用业务词 + 地域前缀/后缀 -> 最小名 (L2 弱合并键)"""
    s = short
    changed = True
    while changed:
        changed = False
        for w in sorted(GENERIC_WORDS, key=len, reverse=True):
            if s.endswith(w) and len(s) > len(w) + 1:
                s = s[: -len(w)].strip()
                changed = True
        for w in sorted(REGION_WORDS, key=len, reverse=True):
            if s.endswith(w) and len(s) > len(w) + 1:
                s = s[: -len(w)].strip()
                changed = True
            elif s.startswith(w) and len(s) > len(w) + 1:
                s = s[len(w):].strip()
                changed = True
    return s


# ---------------- 从 work_text 解析历史雇主 ----------------
# 详细格式: 公司名\n（2008.06 - 至今, 18年1个月）\n行业...
WORK_TIME_RE = re.compile(r"\n([^\n（(]{1,60}?)\s*\n[（(]\s*\d{4}\s*[.\-–~]")
# 紧凑格式: 公司名 · 职位 · 2024.10-至今(1年9个月)
COMPACT_WORK_RE = re.compile(r"([^\n·（(]{2,40}?)\s*·\s*[^\n·]{1,40}?\s*·\s*\d{4}\.\d{2}\s*[-–~]")

def parse_work_history(work_text: str) -> list[str]:
    """简历工作经历段落 -> 公司名列表。格式:
    公司名
    （2008.06 - 至今, 18年1个月）
    行业[规模]
    职位...
    """
    if not work_text:
        return []
    found = []
    for m in WORK_TIME_RE.finditer(work_text):
        name = m.group(1).strip().strip("·,，;；")
        # 去掉可能的行业尾注（如 "工业自动化100-499人" 不会出现在公司名行, 但保险）
        name = re.split(r"\d+-\d+人|/\d+", name)[0].strip()
        c = clean_raw(name)
        if c:
            found.append(c)
    for m in COMPACT_WORK_RE.finditer(work_text):
        c = clean_raw(m.group(1).strip())
        if c:
            found.append(c)
    return found


# ---------------- 主流程 ----------------
def extract_all():
    con = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    raw_counts = Counter()      # 原始名 -> 出现次数

    # 1. recalls.company
    rows = con.execute(
        "SELECT company FROM agent_candidate_recalls WHERE company IS NOT NULL AND company != ''"
    ).fetchall()
    print(f"[1] agent_candidate_recalls.company 非空 {len(rows)} 行")
    for r in rows:
        c = clean_raw(r["company"])
        if c:
            raw_counts[c] += 1

    # 2. recalls.raw_json.work_text 历史雇主
    rows = con.execute(
        "SELECT raw_json FROM agent_candidate_recalls WHERE raw_json LIKE '%work_text%'"
    ).fetchall()
    n_hist = 0
    hist_counts = Counter()
    for r in rows:
        try:
            d = json.loads(r["raw_json"])
        except Exception:
            continue
        wt = d.get("work_text") or ""
        for c in parse_work_history(wt):
            hist_counts[c] += 1
            n_hist += 1
    print(f"[2] work_text 解析历史雇主 {n_hist} 次, 去重 {len(hist_counts)} 家")
    raw_counts.update(hist_counts)

    # 3. people.current_company
    rows = con.execute(
        "SELECT current_company FROM people WHERE current_company IS NOT NULL AND current_company != ''"
    ).fetchall()
    print(f"[3] people.current_company 非空 {len(rows)} 行")
    for r in rows:
        c = clean_raw(r["current_company"])
        if c:
            raw_counts[c] += 1

    # 4. source_profiles.raw_json.company
    rows = con.execute("SELECT raw_json FROM source_profiles").fetchall()
    print(f"[4] source_profiles 共 {len(rows)} 行")
    for r in rows:
        try:
            d = json.loads(r["raw_json"])
        except Exception:
            continue
        comp = d.get("company") or d.get("candidate_company") or ""
        c = clean_raw(comp)
        if c:
            raw_counts[c] += 1

    # 合并历史雇主计数
    con.close()
    return raw_counts


def build_entities(raw_counts: Counter):
    """L1 强合并 + L2 弱合并候选"""
    short_map = defaultdict(list)   # short_name -> [raw names]
    for raw, cnt in raw_counts.items():
        short_map[to_short_name(raw)].append((raw, cnt))

    entities = []  # {key, name, aliases, total, raws}
    for short, raws in sorted(short_map.items(), key=lambda kv: -sum(c for _, c in kv[1])):
        raws.sort(key=lambda x: -x[1])
        total = sum(c for _, c in raws)
        entities.append(
            {
                "key": short,
                "name": raws[0][0],  # 最高频原始名作展示名
                "aliases": [r for r, _ in raws[1:]],
                "total": total,
                "raws": raws,
            }
        )

    # L2: 最小名相同的实体 -> 候选合并组
    minimal_map = defaultdict(list)
    for e in entities:
        minimal_map[to_minimal_name(e["key"])].append(e)
    l2_groups = []
    for mkey, group in minimal_map.items():
        if len(group) > 1:
            l2_groups.append(sorted(group, key=lambda e: -e["total"]))

    return entities, l2_groups


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "outputs/company_kb_dev.db"
    out = os.path.expanduser(out)
    Path(out).parent.mkdir(parents=True, exist_ok=True)

    print(f"== 源库: {SRC_DB}")
    raw_counts = extract_all()
    print(f"== 原始公司名去重: {len(raw_counts)} 家")

    entities, l2_groups = build_entities(raw_counts)
    print(f"== L1 规范化后实体: {len(entities)} 家")
    print(f"== L2 候选合并组: {len(l2_groups)} 组")

    # 写入中间库
    con = sqlite3.connect(out)
    con.execute("DROP TABLE IF EXISTS companies")
    con.execute(
        """CREATE TABLE companies (
            company_key TEXT PRIMARY KEY,
            name TEXT, aliases_json TEXT,
            total INTEGER, raw_count INTEGER,
            l2_group_id INTEGER, is_l2_rep INTEGER DEFAULT 0
        )"""
    )
    # L2 组代表: 组内 total 最大的实体
    l2_rep = {}
    for gi, g in enumerate(l2_groups):
        rep_key = max(g, key=lambda e: e["total"])["key"]
        l2_rep[rep_key] = gi
    for i, e in enumerate(entities):
        gid = None
        for gi, g in enumerate(l2_groups):
            if any(m["key"] == e["key"] for m in g):
                gid = gi
                break
        con.execute(
            "INSERT INTO companies VALUES (?,?,?,?,?,?,?)",
            (e["key"], e["name"], json.dumps(e["aliases"], ensure_ascii=False),
             e["total"], len(e["raws"]), gid, 1 if e["key"] in l2_rep else 0),
        )
    con.execute("DROP TABLE IF EXISTS l2_groups")
    con.execute("CREATE TABLE l2_groups (group_id INTEGER, members_json TEXT, size INTEGER)")
    for gi, g in enumerate(l2_groups):
        con.execute("INSERT INTO l2_groups VALUES (?,?,?)",
                    (gi, json.dumps([(e["key"], e["total"]) for e in g], ensure_ascii=False), len(g)))
    con.commit()
    con.close()
    print(f"== 已写入: {out}")

    # 导出 M2 目标清单（高频公司, 含别名与 L2 组信息）
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    targets = []
    for r in con.execute(
        "SELECT company_key, name, aliases_json, total, l2_group_id FROM companies WHERE total>=5 ORDER BY total DESC"
    ):
        targets.append(
            {
                "company_key": r["company_key"],
                "name": r["name"],
                "aliases": json.loads(r["aliases_json"]),
                "total": r["total"],
                "l2_group_id": r["l2_group_id"],
            }
        )
    # 排除已被合并的实体：company_knowledge.aliases 里出现过的 key/name 视为已并入
    # 其他主实体（L2 合并执行后 dev/prod 两库同步；不排除则定时刷新会把已合并实体重新拆出）
    merged_aliases = set()
    try:
        kcon = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        for row in kcon.execute("SELECT aliases_json FROM company_knowledge"):
            merged_aliases.update(json.loads(row[0] or "[]"))
        kcon.close()
    except Exception:
        pass
    before = len(targets)
    targets = [
        t
        for t in targets
        if t["company_key"] not in merged_aliases and t["name"] not in merged_aliases
    ]
    if len(targets) != before:
        print(f"== 已排除 {before - len(targets)} 个已合并实体（L2 合并状态感知）")
    tg = "outputs/company_kb_m2_targets.json"
    with open(tg, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=1)
    print(f"== M2 目标清单({len(targets)} 家, total>=5): {tg}")

    # 摘要输出
    print("\n-- Top 20 高频公司(L1实体) --")
    for e in entities[:20]:
        extra = f"  aliases={e['aliases'][:3]}" if e["aliases"] else ""
        print(f"  {e['total']:>5}  {e['key']}{extra}")
    print("\n-- L2 候选合并组(可能需人工确认) 前15组 --")
    for g in l2_groups[:15]:
        print("  ", " || ".join(f"{e['key']}({e['total']})" for e in g))


if __name__ == "__main__":
    main()
