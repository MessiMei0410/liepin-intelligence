#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司知识库 M2：LLM 批量提取公司画像

流程：
1. 读取 outputs/company_kb_m2_targets.json（1742 家目标公司）
2. 只读连接生产库 talent_system_v3_20260629.db，按 company_key/aliases 聚合候选人简历文本
3. 调用 DeepSeek API 提取结构化公司画像（行业/业务/产品线/技术栈/组织/规模/薪资/风险/猎头线索 + 证据）
4. 写入 outputs/company_kb_dev.db（company_knowledge + company_evidence 两张表）

安全约束：绝不写生产库（URI mode=ro 只读），只写 outputs/company_kb_dev.db。
依赖：Python3 标准库 + requests。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGETS_JSON = os.path.join(PROJECT_ROOT, "outputs", "company_kb_m2_targets.json")
DEV_DB = os.path.join(PROJECT_ROOT, "outputs", "company_kb_dev.db")
PROD_DB = os.path.expanduser(
    "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
)
HERMES_ENV = os.path.expanduser("~/.hermes/.env")

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
MODEL_VERSION = "deepseek-chat-m2"  # 证据表记录用，便于后续重提/比对

MAX_RESUMES_PER_COMPANY = 8      # 每家公司最多聚合简历份数
MAX_INPUT_CHARS = 6000           # 拼接后输入文本截断长度
LLM_MAX_RETRIES = 2              # LLM 失败额外重试次数（共 1+2 次）
PROGRESS_EVERY = 50              # 每处理多少家打印一次进度
REQUEST_TIMEOUT = 120            # API 请求超时（秒）

VALID_FACT_TYPES = {
    "business", "product", "tech", "org", "scale", "salary", "risk", "headhunt",
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def load_api_key():
    """从 ~/.hermes/.env 读取 DEEPSEEK_API_KEY。"""
    if not os.path.exists(HERMES_ENV):
        sys.exit(f"[FATAL] 找不到 {HERMES_ENV}，无法读取 DEEPSEEK_API_KEY")
    with open(HERMES_ENV, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    sys.exit(f"[FATAL] {HERMES_ENV} 中未找到 DEEPSEEK_API_KEY")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect_prod_ro():
    """以 URI mode=ro 只读方式连接生产库，杜绝任何写入。"""
    if not os.path.exists(PROD_DB):
        sys.exit(f"[FATAL] 生产库不存在: {PROD_DB}")
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def init_dev_db(path):
    """初始化开发库表结构（不存在则创建）。"""
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS company_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            industry TEXT NOT NULL DEFAULT '',
            business_desc TEXT NOT NULL DEFAULT '',
            product_lines_json TEXT NOT NULL DEFAULT '[]',
            tech_stack_json TEXT NOT NULL DEFAULT '[]',
            org_clues_json TEXT NOT NULL DEFAULT '[]',
            scale TEXT NOT NULL DEFAULT '',
            salary_clues_json TEXT NOT NULL DEFAULT '[]',
            risk_signals_json TEXT NOT NULL DEFAULT '[]',
            headhunt_clues_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'auto',
            error_message TEXT,
            last_extracted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS company_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_key TEXT NOT NULL,
            fact_type TEXT NOT NULL,
            fact_value TEXT NOT NULL DEFAULT '',
            quote TEXT NOT NULL DEFAULT '',
            source_ref TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0,
            model_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_company_evidence_key "
        "ON company_evidence(company_key)"
    )
    con.commit()
    return con


# ---------------------------------------------------------------------------
# 简历聚合
# ---------------------------------------------------------------------------
def fetch_resume_texts(prod_con, company_key, aliases):
    """
    从生产库聚合该公司相关的候选人简历文本。

    匹配规则：agent_candidate_recalls.company 或 raw_json 中的 work_text
    与 company_key / 任一 alias 做 LIKE 模糊匹配，最多取 MAX_RESUMES_PER_COMPANY 份。
    返回 (拼接文本, 命中份数)。
    """
    names = [company_key] + [a for a in (aliases or []) if a and a != company_key]
    if not names:
        return "", 0

    # 构造 WHERE: company LIKE ? OR raw_json LIKE ?（work_text 含在 raw_json 文本里）
    conds, params = [], []
    for n in names:
        like = f"%{n}%"
        conds.append("(company LIKE ? OR raw_json LIKE ?)")
        params.extend([like, like])
    sql = (
        "SELECT candidate_name, company, title, raw_json "
        f"FROM agent_candidate_recalls WHERE ({' OR '.join(conds)}) "
        # SQL 层先过滤掉 profile/work/edu 正文全空的记录（如猎聘卡片仅标题），
        # 避免近期空记录挤占取样窗口
        "AND (length(COALESCE(json_extract(raw_json,'$.profile_text'),'')) > 0 "
        "OR length(COALESCE(json_extract(raw_json,'$.work_text'),'')) > 0 "
        "OR length(COALESCE(json_extract(raw_json,'$.education_text'),'')) > 0) "
        f"ORDER BY id DESC LIMIT {MAX_RESUMES_PER_COMPANY * 4}"  # 多取一些再过滤
    )
    rows = prod_con.execute(sql, params).fetchall()

    chunks, used = [], 0
    for r in rows:
        if used >= MAX_RESUMES_PER_COMPANY:
            break
        try:
            raw = json.loads(r["raw_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        # 优先取结构化的 profile_text / work_text，其次回退到顶层字段
        profile = raw.get("profile_text") or ""
        work = raw.get("work_text") or ""
        edu = raw.get("education_text") or ""
        # 正文全空的记录（如猎聘卡片仅标题）对画像提取无价值，跳过
        if not (profile.strip() or work.strip() or edu.strip()):
            continue
        # 确认该公司确实在文本中出现（避免 LIKE 误命中 raw_json 其他字段）
        blob = f"{r['company']} {profile} {work}"
        if not any(n in blob for n in names):
            continue
        header = f"【候选人】{r['candidate_name'] or raw.get('name','')} | {r['company']} | {r['title']}"
        body = "\n".join(x for x in [header, profile, work, edu] if x)
        if body.strip():
            chunks.append(body)
            used += 1

    text = "\n\n".join(chunks)
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS] + "\n……（已截断）"
    return text, used


# ---------------------------------------------------------------------------
# LLM 提取
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是一名资深猎头研究员。根据输入的多份候选人简历文本，推断这些候选人所在公司的"
    "公司画像。只输出 JSON，不要输出任何其他内容。所有结论必须有简历原文支撑，"
    "禁止编造。证据中的 quote 必须逐字摘自输入原文。"
)

USER_PROMPT_TEMPLATE = """以下是关于公司「{name}」（可能的出现名：{names}）的 {count} 份候选人简历片段：

<resumes>
{text}
</resumes>

请提取该公司画像，输出 JSON，字段如下：
{{
  "industry": "所属行业（一句话）",
  "business_desc": "主营业务描述（2-3句话）",
  "product_lines": ["产品线/业务线1", "..."],
  "tech_stack": ["技术栈/核心技术1", "..."],
  "org_clues": ["组织架构线索，如部门/团队/岗位设置", "..."],
  "scale": "规模线索（人数/体量，无依据则写'未知'）",
  "salary_clues": ["薪资/职级线索", "..."],
  "risk_signals": ["风险信号，如裁员/流失/负面", "..."],
  "headhunt_clues": ["对猎头有价值的线索，如挖角切入点/人才流向", "..."],
  "evidence": [
    {{"fact_type": "business|product|tech|org|scale|salary|risk|headhunt 之一",
      "fact_value": "归纳出的事实",
      "quote": "支撑该事实的原文逐字摘录"}}
  ]
}}
要求：evidence 3-10 条；quote 必须来自 <resumes> 原文；无依据的字段留空数组或空字符串。"""


def call_deepseek(api_key, company, text, source_count):
    """
    调用 DeepSeek API 提取公司画像。失败重试 LLM_MAX_RETRIES 次。
    返回 (profile_dict, error_message)；成功时 error_message 为 None。
    """
    names = " / ".join([company["company_key"]] + (company.get("aliases") or []))
    user_prompt = USER_PROMPT_TEMPLATE.format(
        name=company["name"] or company["company_key"],
        names=names,
        count=source_count,
        text=text,
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1 + LLM_MAX_RETRIES):
        try:
            resp = requests.post(
                DEEPSEEK_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            profile = json.loads(content)
            if not isinstance(profile, dict):
                raise ValueError("LLM 返回的 JSON 不是对象")
            return profile, None
        except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
            last_err = f"第{attempt + 1}次尝试: {type(e).__name__}: {e}"
            print(f"  [WARN] {company['company_key']} LLM 调用失败（{last_err}）", flush=True)
            if attempt < LLM_MAX_RETRIES:
                time.sleep(2 * (attempt + 1))  # 简单退避
    return None, last_err


def normalize_str_list(value):
    """把 LLM 返回值规整为字符串数组。"""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_evidence(value):
    """规整 evidence：只保留 fact_type 合法且 quote 非空的条目，最多 10 条。"""
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        ft = str(item.get("fact_type", "")).strip()
        if ft not in VALID_FACT_TYPES:
            continue
        quote = str(item.get("quote", "")).strip()
        if not quote:
            continue
        out.append(
            {
                "fact_type": ft,
                "fact_value": str(item.get("fact_value", "")).strip(),
                "quote": quote,
            }
        )
        if len(out) >= 10:
            break
    return out


def calc_confidence(evidence_count, source_count):
    """简单启发式置信度：证据越多、来源简历越多，置信度越高。"""
    conf = 0.3 + 0.05 * evidence_count + 0.03 * source_count
    return round(min(conf, 0.95), 2)


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------
def save_profile(dev_con, company, profile, evidence, source_count):
    """写入 company_knowledge + company_evidence（status='auto'）。"""
    now = now_str()
    key = company["company_key"]
    dev_con.execute(
        """
        INSERT INTO company_knowledge (
            company_key, name, aliases_json, industry, business_desc,
            product_lines_json, tech_stack_json, org_clues_json, scale,
            salary_clues_json, risk_signals_json, headhunt_clues_json,
            confidence, evidence_count, source_count, status,
            last_extracted_at, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company_key) DO UPDATE SET
            name=excluded.name, aliases_json=excluded.aliases_json,
            industry=excluded.industry, business_desc=excluded.business_desc,
            product_lines_json=excluded.product_lines_json,
            tech_stack_json=excluded.tech_stack_json,
            org_clues_json=excluded.org_clues_json, scale=excluded.scale,
            salary_clues_json=excluded.salary_clues_json,
            risk_signals_json=excluded.risk_signals_json,
            headhunt_clues_json=excluded.headhunt_clues_json,
            confidence=excluded.confidence, evidence_count=excluded.evidence_count,
            source_count=excluded.source_count, status=excluded.status,
            error_message=NULL,
            last_extracted_at=excluded.last_extracted_at,
            updated_at=excluded.updated_at
        """,
        (
            key,
            company.get("name") or key,
            json.dumps(company.get("aliases") or [], ensure_ascii=False),
            str(profile.get("industry", "") or ""),
            str(profile.get("business_desc", "") or ""),
            json.dumps(normalize_str_list(profile.get("product_lines")), ensure_ascii=False),
            json.dumps(normalize_str_list(profile.get("tech_stack")), ensure_ascii=False),
            json.dumps(normalize_str_list(profile.get("org_clues")), ensure_ascii=False),
            str(profile.get("scale", "") or ""),
            json.dumps(normalize_str_list(profile.get("salary_clues")), ensure_ascii=False),
            json.dumps(normalize_str_list(profile.get("risk_signals")), ensure_ascii=False),
            json.dumps(normalize_str_list(profile.get("headhunt_clues")), ensure_ascii=False),
            calc_confidence(len(evidence), source_count),
            len(evidence),
            source_count,
            "auto",
            now,
            now,
            now,
        ),
    )
    # 重跑时先清掉旧证据再插入，保证幂等
    dev_con.execute("DELETE FROM company_evidence WHERE company_key = ?", (key,))
    for ev in evidence:
        dev_con.execute(
            """
            INSERT INTO company_evidence (
                company_key, fact_type, fact_value, quote,
                source_ref, confidence, model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                key,
                ev["fact_type"],
                ev["fact_value"],
                ev["quote"],
                "agent_candidate_recalls",
                0.8,  # LLM 提取证据的默认置信度
                MODEL_VERSION,
                now,
            ),
        )
    dev_con.commit()


def save_error(dev_con, company, error_message, source_count):
    """LLM 重试后仍失败：标记 status='error'，保留错误信息，便于后续重跑。"""
    now = now_str()
    dev_con.execute(
        """
        INSERT INTO company_knowledge (
            company_key, name, aliases_json, source_count, status,
            error_message, last_extracted_at, created_at, updated_at
        ) VALUES (?,?,?,?,'error',?,?,?,?)
        ON CONFLICT(company_key) DO UPDATE SET
            status='error', error_message=excluded.error_message,
            source_count=excluded.source_count,
            last_extracted_at=excluded.last_extracted_at,
            updated_at=excluded.updated_at
        """,
        (
            company["company_key"],
            company.get("name") or company["company_key"],
            json.dumps(company.get("aliases") or [], ensure_ascii=False),
            source_count,
            (error_message or "")[:500],
            now,
            now,
            now,
        ),
    )
    dev_con.commit()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process_one(company, api_key):
    """单家公司: 聚合简历 + 调 LLM（worker 线程执行, 每 worker 独立只读连接）。
    返回 (status, company, profile_or_None, err_or_None, source_count)
    任何异常都吞掉转为 error 状态，保证 fut.result() 不抛。"""
    key = company["company_key"]
    try:
        prod_con = connect_prod_ro()
        try:
            text, source_count = fetch_resume_texts(
                prod_con, key, company.get("aliases") or []
            )
        finally:
            prod_con.close()
        if not text.strip():
            return ("no_source", company, None, "生产库无匹配简历文本", 0)
        profile, err = call_deepseek(api_key, company, text, source_count)
        if profile is None:
            return ("error", company, None, err, source_count)
        return ("ok", company, profile, None, source_count)
    except Exception as e:
        return ("error", company, None, f"worker异常: {e}", 0)


def main():
    parser = argparse.ArgumentParser(description="公司知识库 M2：LLM 批量提取公司画像")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 家（试跑用），0 表示全部")
    parser.add_argument("--workers", type=int, default=8, help="并发数（默认 8）")
    args = parser.parse_args()

    # 1. 读目标公司清单
    with open(TARGETS_JSON, "r", encoding="utf-8") as f:
        targets = json.load(f)
    if args.limit > 0:
        targets = targets[: args.limit]
    print(f"[INFO] 目标公司 {len(targets)} 家（limit={args.limit or '全部'}，workers={args.workers}）", flush=True)

    # 2. 连接库：开发库可写（worker 各自连只读生产库）
    api_key = load_api_key()
    dev_con = init_dev_db(DEV_DB)

    # 幂等：已存在且 status='auto' 的 company_key 跳过；error/无源行重试
    # （数据积累后，之前无简历的公司可能已有新简历，定时刷新时自动补全）
    done_keys = {
        r[0]
        for r in dev_con.execute(
            "SELECT company_key FROM company_knowledge WHERE status='auto'"
        )
    }
    pending = [c for c in targets if c["company_key"] not in done_keys]
    retry_errors = sum(1 for c in targets if c["company_key"] not in done_keys)
    print(f"[INFO] 开发库已有 {len(done_keys)} 家(auto)，待处理/重试 {len(pending)} 家", flush=True)

    stats = {"ok": 0, "error": 0, "no_source": 0}
    t0 = time.time()
    done_cnt = len(done_keys)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process_one, c, api_key) for c in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                status, company, profile, err, sc = fut.result()
            except Exception as e:  # 理论上 process_one 已兜底，双保险
                status, company, profile, err, sc = "error", None, None, f"worker异常: {e}", 0
            if company is None:
                stats["error"] += 1
                continue
            key = company["company_key"]
            if status == "no_source":
                save_error(dev_con, company, "生产库无匹配简历文本", 0)
                stats["no_source"] += 1
            elif status == "error":
                save_error(dev_con, company, err, sc)
                stats["error"] += 1
            else:
                evidence = normalize_evidence((profile or {}).get("evidence"))
                save_profile(dev_con, company, profile, evidence, sc)
                stats["ok"] += 1

            # 进度打印
            if i % PROGRESS_EVERY == 0 or i == len(pending):
                elapsed = time.time() - t0
                rate = i / elapsed * 60 if elapsed > 0 else 0
                eta = (len(pending) - i) / rate * 60 if rate > 0 else 0
                print(
                    f"[PROGRESS] {done_cnt + i}/{len(targets)} | 成功 {stats['ok']} "
                    f"失败 {stats['error']} 无源 {stats['no_source']} "
                    f"| 速度 {rate:.0f}家/分 | ETA {eta:.0f}min",
                    flush=True,
                )

    dev_con.close()
    print(
        f"[DONE] 成功 {stats['ok']}，LLM失败 {stats['error']}，"
        f"无简历 {stats['no_source']}，"
        f"总耗时 {time.time() - t0:.0f}s（并发 {args.workers}）。输出: {DEV_DB}",
        flush=True,
    )


if __name__ == "__main__":
    main()
