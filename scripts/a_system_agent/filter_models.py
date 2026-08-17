"""岗位筛选模型桥（position_filter_models）。

把"寻访策略（position_profiles，LLM 生成）"与"确定性筛选器（candidate_pool_filter）"
两个子系统打通：筛选模型的证据词表以数据形式落库，筛选器运行时读取已确认模型，
未确认的域依旧失败关闭（UnsupportedFilterDomainError）。

模型 JSON 结构（model_json）：
{
  "layers": {"direct": [...], "support": [...]},   // 命名证据层 → 关键词
  "layer_weights": {"direct": 20, "support": 8},   // 每层命中计分权重（缺省 10）
  "excl_title": [...],              // 当前职位排除词 → X-排除
  "review_only_title": [...],       // 当前职位命中 → 固定 review_only_grade（如 FAE 只进 C）
  "review_only_grade": "C-弱",
  "review_only_reason": "...",
  "rules": [                        // 自上而下首个命中的规则生效
    {"grade": "A-核心", "min": {"direct": 2, "support": 1}, "reason": "..."},
    {"grade": "B-中",   "min": {"direct": 1}, "reason": "..."},
    {"grade": "D-无证据", "min": {}, "reason": "..."}   // 必须兜底
  ]
}
规则可选字段：min（每层至少命中数）、max（每层至多命中数）、
title_any（当前职位含任一词才生效，用于"纯销售排除"这类裁定）。

生命周期：draft（草稿，不参与筛选）→ confirmed（人工确认后生效）→ disabled。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

MODEL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS position_filter_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    client TEXT DEFAULT '',
    position TEXT DEFAULT '',
    domain TEXT NOT NULL,
    model_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    source TEXT DEFAULT 'manual',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    confirmed_by TEXT DEFAULT '',
    confirmed_at TEXT DEFAULT ''
)
"""

VALID_STATUS = {"draft", "confirmed", "disabled"}
DEFAULT_LAYER_WEIGHT = 10


class FilterModelError(ValueError):
    """筛选模型数据不合法。"""


def validate_model(model: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化模型 JSON；不合法抛 FilterModelError。"""
    if not isinstance(model, dict):
        raise FilterModelError("模型必须是 JSON 对象")
    layers = model.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise FilterModelError("layers 必须是非空对象")
    clean_layers: dict[str, list[str]] = {}
    for name, keys in layers.items():
        if not isinstance(name, str) or not name.strip():
            raise FilterModelError("层名必须是非空字符串")
        if not isinstance(keys, list):
            raise FilterModelError(f"层 {name} 的关键词必须是列表")
        clean_layers[name.strip()] = [str(k).strip() for k in keys if str(k or "").strip()]
    weights_raw = model.get("layer_weights") or {}
    if not isinstance(weights_raw, dict):
        raise FilterModelError("layer_weights 必须是对象")
    weights = {str(k): int(v) for k, v in weights_raw.items() if str(k) in clean_layers}
    rules = model.get("rules")
    if not isinstance(rules, list) or not rules:
        raise FilterModelError("rules 必须是非空列表")
    clean_rules: list[dict[str, Any]] = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict) or not str(rule.get("grade") or "").strip():
            raise FilterModelError(f"rules[{i}] 缺少 grade")
        for field in ("min", "max"):
            value = rule.get(field) or {}
            if not isinstance(value, dict):
                raise FilterModelError(f"rules[{i}].{field} 必须是对象")
            unknown = set(value) - set(clean_layers)
            if unknown:
                raise FilterModelError(f"rules[{i}].{field} 引用了未定义层: {sorted(unknown)}")
        title_any = rule.get("title_any") or []
        if not isinstance(title_any, list):
            raise FilterModelError(f"rules[{i}].title_any 必须是列表")
        clean_rules.append({
            "grade": str(rule["grade"]).strip(),
            "min": {str(k): int(v) for k, v in (rule.get("min") or {}).items()},
            "max": {str(k): int(v) for k, v in (rule.get("max") or {}).items()},
            "title_any": [str(t) for t in title_any],
            "reason": str(rule.get("reason") or ""),
        })
    if clean_rules[-1]["min"] or clean_rules[-1]["max"] or clean_rules[-1]["title_any"]:
        raise FilterModelError("最后一条规则必须是无条件兜底规则")
    return {
        "layers": clean_layers,
        "layer_weights": weights,
        "excl_title": [str(k).strip() for k in (model.get("excl_title") or []) if str(k or "").strip()],
        "review_only_title": [str(k).strip() for k in (model.get("review_only_title") or []) if str(k or "").strip()],
        "review_only_grade": str(model.get("review_only_grade") or "C-弱"),
        "review_only_reason": str(model.get("review_only_reason") or "该角色类型需人工核验实际责任"),
        "rules": clean_rules,
    }


def grade_with_model(
    model: dict[str, Any],
    *,
    title_text: str,
    txt: str,
    edu: str = "",
    exp_n: int | None = None,
) -> dict[str, Any]:
    """通用规则引擎：对单个候选执行数据驱动的分层证据分级。

    title_text / txt 调用方需已转小写。返回 grade/reason/score/layer_hits/hard_hits。
    """
    layer_hits = {
        name: [k for k in keys if k.lower() in txt]
        for name, keys in model["layers"].items()
    }
    score = sum(
        len(hits) * int(model["layer_weights"].get(name, DEFAULT_LAYER_WEIGHT))
        for name, hits in layer_hits.items()
    )
    if edu in ("硕士", "博士"):
        score += 8
    elif edu == "本科":
        score += 5
    if exp_n is not None and exp_n >= 7:
        score += 8
    elif exp_n is not None and exp_n >= 4:
        score += 3

    excl = [k for k in model["excl_title"] if k.lower() in title_text]
    if excl:
        grade, reason = "X-排除", f"当前/原职位不匹配:{'、'.join(excl)}"
    elif any(k.lower() in title_text for k in model["review_only_title"]):
        grade, reason = model["review_only_grade"], model["review_only_reason"]
    else:
        grade, reason = "D-无证据", "未命中任何分级规则"
        for rule in model["rules"]:
            if rule["title_any"] and not any(t.lower() in title_text for t in rule["title_any"]):
                continue
            if any(len(layer_hits[layer]) < n for layer, n in rule["min"].items()):
                continue
            if any(len(layer_hits[layer]) > n for layer, n in rule["max"].items()):
                continue
            grade = rule["grade"]
            reason = rule["reason"] or "命中分级规则"
            break
    hard_hits = [k for name in model["layers"] for k in layer_hits[name]]
    return {"grade": grade, "reason": reason, "score": score, "layer_hits": layer_hits, "hard_hits": hard_hits}


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(MODEL_TABLE_DDL)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_filter_models'"
    ).fetchone()
    return row is not None


def load_model_for_job(
    conn: sqlite3.Connection,
    job_id: int,
    domain: str | None = None,
    *,
    status: str = "confirmed",
) -> dict[str, Any] | None:
    """读取岗位的筛选模型：岗位级优先，域级（job_id IS NULL）其次。

    只读连接上表不存在时返回 None，由调用方失败关闭。
    """
    if not _table_exists(conn):
        return None
    rows = conn.execute(
        "SELECT * FROM position_filter_models WHERE status=? AND (job_id=? OR (job_id IS NULL AND domain=?)) "
        "ORDER BY CASE WHEN job_id IS NULL THEN 1 ELSE 0 END, id DESC",
        (status, int(job_id), str(domain or "")),
    ).fetchall()
    for row in rows:
        try:
            model = validate_model(json.loads(row["model_json"]))
        except (FilterModelError, json.JSONDecodeError):
            continue
        return {
            "model_id": row["id"], "domain": row["domain"], "model": model,
            "source": row["source"], "status": row["status"],
        }
    return None


def model_status_map(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str]:
    """批量返回 job_id → 'confirmed'/'draft'（无记录则不出现）。"""
    if not job_ids or not _table_exists(conn):
        return {}
    marks = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"SELECT job_id, status FROM position_filter_models WHERE job_id IN ({marks}) AND status != 'disabled'",
        [int(j) for j in job_ids],
    ).fetchall()
    result: dict[int, str] = {}
    for row in rows:  # confirmed 优先于 draft
        jid = int(row["job_id"])
        if row["status"] == "confirmed" or jid not in result:
            result[jid] = row["status"]
    return result


def upsert_model(
    db_path: str,
    *,
    job_id: int | None,
    client: str,
    position: str,
    domain: str,
    model: dict[str, Any],
    status: str = "draft",
    source: str = "manual",
    note: str = "",
    confirmed_by: str = "",
) -> int:
    """写入/更新筛选模型，返回模型 id。同 (job_id, domain) 复用已有行。"""
    if status not in VALID_STATUS:
        raise FilterModelError(f"非法状态: {status}")
    clean = validate_model(model)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_table(conn)
        existing = conn.execute(
            "SELECT id FROM position_filter_models WHERE COALESCE(job_id,-1)=COALESCE(?,-1) AND domain=? ORDER BY id DESC LIMIT 1",
            (job_id, domain),
        ).fetchone()
        confirmed_at = "datetime('now','localtime')" if status == "confirmed" else "''"
        if existing:
            conn.execute(
                "UPDATE position_filter_models SET client=?, position=?, model_json=?, status=?, source=?, note=?, "
                "updated_at=datetime('now','localtime'), confirmed_by=?, "
                f"confirmed_at={confirmed_at} WHERE id=?",
                (client, position, json.dumps(clean, ensure_ascii=False), status, source, note, confirmed_by, existing["id"]),
            )
            model_id = int(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO position_filter_models (job_id, client, position, domain, model_json, status, source, note, confirmed_by, confirmed_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,{confirmed_at})",
                (job_id, client, position, domain, json.dumps(clean, ensure_ascii=False), status, source, note, confirmed_by),
            )
            model_id = int(cur.lastrowid)
        conn.commit()
        return model_id
    finally:
        conn.close()


def confirm_model(db_path: str, model_id: int, *, confirmed_by: str = "") -> None:
    """人工确认：draft → confirmed。确认前重新校验模型合法性。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM position_filter_models WHERE id=?", (int(model_id),)).fetchone()
        if not row:
            raise FilterModelError(f"模型 {model_id} 不存在")
        validate_model(json.loads(row["model_json"]))
        conn.execute(
            "UPDATE position_filter_models SET status='confirmed', confirmed_by=?, "
            "confirmed_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
            (confirmed_by, int(model_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _short_tokens(items: list[Any], *, max_len: int = 12) -> tuple[list[str], list[str]]:
    """把画像字段拆成可直接匹配的词（≤max_len）与需要人工提炼的长句。"""
    tokens, long_items = [], []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        (tokens if len(text) <= max_len else long_items).append(text)
    return tokens, long_items


def draft_from_position_profile(db_path: str, job_id: int) -> dict[str, Any]:
    """从 position_profiles 生成筛选模型草稿（status=draft，不参与筛选）。

    映射规则：hard_requirements → direct 层；ability_keywords → support 层；
    exclusion_tags → excl_title。超过 12 字的长句不进关键词表，写入 note 供人工提炼。
    草稿使用保守兜底规则：A-核心 direct≥2 且 support≥1；B direct≥1；C support≥2；D 兜底。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            "SELECT j.id, j.title, c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (int(job_id),),
        ).fetchone()
        if not job:
            raise FilterModelError(f"岗位 {job_id} 不存在")
        profile = conn.execute(
            "SELECT * FROM position_profiles WHERE client=? AND position=? ORDER BY COALESCE(updated_at,'') DESC, id DESC LIMIT 1",
            (job["client"], job["title"]),
        ).fetchone()
        if not profile:
            raise FilterModelError(f"岗位 {job_id}（{job['client']}·{job['title']}）无 position_profiles 画像，无法生成草稿")
        hard = json.loads(profile["hard_requirements_json"] or "[]")
        ability = json.loads(profile["ability_keywords_json"] or "[]")
        exclusion = json.loads(profile["exclusion_tags_json"] or "[]")
    finally:
        conn.close()

    direct, direct_long = _short_tokens(hard if isinstance(hard, list) else [])
    support, support_long = _short_tokens(ability if isinstance(ability, list) else [])
    excl, _ = _short_tokens(exclusion if isinstance(exclusion, list) else [])
    if not direct:
        raise FilterModelError("画像 hard_requirements 无可用短词，需人工建模")
    model = {
        "layers": {"direct": direct, "support": support},
        "layer_weights": {"direct": 20, "support": 8},
        "excl_title": excl,
        "rules": [
            {"grade": "A-核心", "min": {"direct": 2, "support": 1}, "reason": "硬性要求与能力关键词同时命中"},
            {"grade": "B-中", "min": {"direct": 1}, "reason": "命中硬性要求，支撑证据待补"},
            {"grade": "C-弱", "min": {"support": 2}, "reason": "仅有能力关键词命中"},
            {"grade": "D-无证据", "min": {}, "reason": "简历无岗位硬证据"},
        ],
    }
    long_notes = [f"direct 待提炼: {x}" for x in direct_long] + [f"support 待提炼: {x}" for x in support_long]
    return {
        "job_id": int(job_id),
        "client": job["client"],
        "position": job["title"],
        "domain": f"profile_{int(job_id)}",
        "model": model,
        "note": "；".join(long_notes),
        "profile_id": int(profile["id"]),
    }
