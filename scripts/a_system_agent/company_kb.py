"""公司知识库（CKB）消费集成：company_knowledge / company_evidence 只读查询。

数据源为生产库（A_SYSTEM_DB 环境变量优先，缺省 talent_system_v3 生产库），
一律 mode=ro 只读连接；DB 缺失/无表/读取失败一律降级返回 None/空列表，
不抛错、不影响主流程（与 knowledge_base.load_calibration_overlay 同一降级口径）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

# 缺省生产库路径（A_SYSTEM_DB 环境变量优先）
_DEFAULT_DB_PATH = Path(
    "~/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db"
).expanduser()

_KNOWLEDGE_TABLE = "company_knowledge"

# get_profile 返回的画像字段（含 company_key/name 便于调用方留痕）
_PROFILE_FIELDS = (
    "company_key",
    "name",
    "aliases_json",
    "industry",
    "business_desc",
    "product_lines_json",
    "tech_stack_json",
    "org_clues_json",
    "scale",
    "salary_clues_json",
    "risk_signals_json",
    "headhunt_clues_json",
    "confidence",
    "evidence_count",
    "source_count",
)


def _db_path(db_path: str | Path | None = None) -> Path | None:
    """解析库路径：显式参数 > A_SYSTEM_DB > 缺省生产库；文件不存在返回 None。"""
    raw = str(db_path or "").strip() or os.environ.get("A_SYSTEM_DB", "").strip()
    path = Path(raw).expanduser() if raw else _DEFAULT_DB_PATH
    return path if path.is_file() else None


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """只读连接（mode=ro）；无 company_knowledge 表或连接失败返回 None。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_KNOWLEDGE_TABLE,),
        ).fetchone()
        if not has_table:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def _row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    """行 → 画像 dict；confidence 转 float，其余按原样字符串/整数透出。"""
    profile: dict[str, Any] = {}
    for field in _PROFILE_FIELDS:
        value = row[field] if field in row.keys() else None
        if field == "confidence":
            try:
                value = float(value or 0.0)
            except (TypeError, ValueError):
                value = 0.0
        elif field in ("evidence_count", "source_count"):
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                value = 0
        else:
            value = str(value or "")
        profile[field] = value
    return profile


def get_profile(name: str, db_path: str | Path | None = None) -> dict[str, Any] | None:
    """按公司名查 CKB 画像：company_key/name/aliases_json LIKE 匹配，取置信度最高一条。

    未命中/库不可用/无表一律返回 None，不报错。
    """
    keyword = " ".join(str(name or "").split())
    if not keyword:
        return None
    path = _db_path(db_path)
    if path is None:
        return None
    conn = _connect_ro(path)
    if conn is None:
        return None
    try:
        like = f"%{keyword}%"
        row = conn.execute(
            """
            SELECT * FROM company_knowledge
            WHERE company_key LIKE ? OR name LIKE ? OR aliases_json LIKE ?
            ORDER BY confidence DESC, evidence_count DESC
            LIMIT 1
            """,
            (like, like, like),
        ).fetchone()
        return _row_to_profile(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def find_companies(kw: str, limit: int = 10, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    """按行业/主营业务/产品线 LIKE 反查公司列表（置信度降序），返回画像 dict 列表。

    库不可用/无表/读取失败返回空列表，不报错。
    """
    keyword = " ".join(str(kw or "").split())
    if not keyword:
        return []
    path = _db_path(db_path)
    if path is None:
        return []
    conn = _connect_ro(path)
    if conn is None:
        return []
    try:
        like = f"%{keyword}%"
        rows = conn.execute(
            """
            SELECT * FROM company_knowledge
            WHERE industry LIKE ? OR business_desc LIKE ? OR product_lines_json LIKE ?
            ORDER BY confidence DESC, evidence_count DESC
            LIMIT ?
            """,
            (like, like, like, max(1, int(limit))),
        ).fetchall()
        return [_row_to_profile(row) for row in rows]
    except (sqlite3.Error, ValueError):
        return []
    finally:
        conn.close()


def _loads_list(raw: str) -> list[str]:
    """JSON 数组字段容错解析；非数组/解析失败返回空列表。"""
    try:
        value = json.loads(str(raw or ""))
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def profile_summary(p: dict[str, Any] | None) -> str:
    """画像 → 中文摘要（≤300 字）；空画像返回空串。"""
    if not isinstance(p, dict) or not p:
        return ""
    parts: list[str] = []
    head = str(p.get("name") or p.get("company_key") or "").strip()
    industry = str(p.get("industry") or "").strip()
    if head:
        parts.append(f"{head}（{industry}）" if industry else head)
    business = " ".join(str(p.get("business_desc") or "").split())
    if business:
        parts.append(business)
    product_lines = _loads_list(str(p.get("product_lines_json") or ""))
    if product_lines:
        parts.append("产品线：" + "、".join(product_lines[:6]))
    tech_stack = _loads_list(str(p.get("tech_stack_json") or ""))
    if tech_stack:
        parts.append("技术栈：" + "、".join(tech_stack[:6]))
    scale = str(p.get("scale") or "").strip()
    if scale:
        parts.append(f"规模：{scale}")
    risk_signals = _loads_list(str(p.get("risk_signals_json") or ""))
    if risk_signals:
        parts.append("风险信号：" + "、".join(risk_signals[:3]))
    summary = "。".join(part.rstrip("。") for part in parts if part)
    if summary and not summary.endswith("。"):
        summary += "。"
    return summary[:300]
