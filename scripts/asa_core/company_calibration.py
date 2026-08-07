"""company_calibration 核心公司校准（二期知识飞轮缺口）。

顾问逐公司确认/修正图谱条目（行业/产品线/技能标签/职级体系/禁挖竞业标记/备注），
校准状态持久化在 company_calibrations 表（图谱 JSON 保持原始名单，校准是覆盖层）：
- 待校准队列：图谱公司合并 DB 校准记录，未校准优先（支持按名称/赛道/主营业务搜索）；
- 提交校准：按 company_key（规范化公司名）upsert，内容变化 version 自增，
  同内容重复提交不 bump version（服务层幂等；HTTP 层另有 Idempotency-Key 重放兜底）；
- 跳过/标记待复核：status='needs_review'；拒绝条目（不进消费）：status='rejected'；
- 进度：已校准 N / 目标 TARGET_COMPANY_GOAL（默认 50），供前端进度指示。

消费接入走 knowledge_base.load_calibration_overlay / apply_calibration_overlay
（仅 status='calibrated' 覆盖，标注 source=consultant_calibrated）。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from a_system_agent import knowledge_base as kb

from .database import connect, json_value, transaction

CALIBRATION_STATUSES = ("calibrated", "rejected", "needs_review")
CALIBRATION_STATUS_LABELS = {
    "calibrated": "已校准",
    "rejected": "已拒绝",
    "needs_review": "待复核",
}
QUEUE_STATUS_LABELS = {**CALIBRATION_STATUS_LABELS, "pending": "未校准"}

# 核心公司校准目标（顾问本期校准 30-50 家核心公司，进度指示按 50 计）。
TARGET_COMPANY_GOAL = 50

# 待校准队列默认口径：未校准（pending）+ 待复核（needs_review）优先。
_QUEUE_DEFAULT_STATUSES = ("pending", "needs_review")


def _clean_list(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    seen: dict[str, None] = {}
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in seen:
            seen[text] = None
    return list(seen)


class CompanyCalibrationService:
    """核心公司校准：待校准队列 + 幂等提交（版本化）+ 进度聚合。"""

    def __init__(self, db_path: Path, kb_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.kb_dir = Path(kb_dir) if kb_dir else None

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        status = str(row["status"] or "")
        return {
            "calibration_id": row["calibration_id"],
            "company_key": row["company_key"],
            "company_name": row["company_name"],
            "track": row["track"],
            "product_lines": json_value(row["product_lines_json"], []),
            "skill_tags": json_value(row["skill_tags_json"], []),
            "level_system": row["level_system"],
            "no_poach": bool(row["no_poach"]),
            "non_compete": bool(row["non_compete"]),
            "note": row["note"],
            "status": status,
            "status_label": CALIBRATION_STATUS_LABELS.get(status, status),
            "calibrated_by": row["calibrated_by"],
            "calibrated_at": row["calibrated_at"],
            "version": int(row["version"] or 1),
            "updated_at": row["updated_at"],
        }

    def _graph(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        return kb.load_company_graph(self.kb_dir)

    def _calibrations_by_key(self, conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_calibrations'"
        ).fetchone()
        if not has_table:
            return {}
        rows = conn.execute("SELECT * FROM company_calibrations").fetchall()
        return {str(row["company_key"]): row for row in rows}

    @staticmethod
    def _resolve_graph_name(graph: dict[str, dict[str, Any]], company_name: str) -> str | None:
        """把顾问输入的公司名解析到图谱条目：精确/规范化别名（不用模糊匹配，宁可 miss 不可错配）。"""
        target_raw = " ".join(str(company_name or "").split())
        target_norm = kb.normalize_client_name(company_name)
        if not target_norm:
            return None
        for candidate in graph:
            if kb.name_match_rule(target_raw, target_norm, candidate)[0]:
                return candidate
        return None

    # ------------------------------------------------------------------
    # 读：待校准队列 / 详情 / 进度
    # ------------------------------------------------------------------

    def list_queue(self, query: str = "", status: str = "", limit: int = 50) -> dict[str, Any]:
        """待校准队列：未校准优先，其次待复核/已校准/已拒绝；支持名称/赛道/主营业务搜索。

        status 过滤：空 → 未校准+待复核（默认待办口径）；pending/calibrated/rejected/
        needs_review/all 单选；非法 → ValueError。
        """
        status = str(status or "").strip().lower()
        allowed = (*_QUEUE_DEFAULT_STATUSES, "calibrated", "rejected", "all")
        if status and status not in allowed:
            raise ValueError(f"未知校准状态过滤：{status}")
        limit = max(1, min(int(limit or 50), 200))
        graph, graph_trace = self._graph()
        conn = connect(self.db_path)
        try:
            records = self._calibrations_by_key(conn)
        finally:
            conn.close()

        needle = " ".join(str(query or "").split()).casefold()
        items: list[dict[str, Any]] = []
        for name in sorted(graph):
            info = graph[name]
            key = kb.normalize_client_name(name)
            row = records.get(key)
            item_status = str(row["status"]) if row else "pending"
            haystack = f"{name} {info.get('track', '')} {info.get('business', '')}".casefold()
            if needle and needle not in haystack:
                continue
            if status == "all":
                pass
            elif status:
                if item_status != status:
                    continue
            elif item_status not in _QUEUE_DEFAULT_STATUSES:
                continue
            item: dict[str, Any] = {
                "company_key": key,
                "company_name": name,
                "track": info.get("track", ""),
                "business": info.get("business", ""),
                "categories": list(info.get("categories") or []),
                "status": item_status,
                "status_label": QUEUE_STATUS_LABELS.get(item_status, item_status),
                "calibration": self._record(row) if row else None,
            }
            items.append(item)
        # 未校准优先，其次待复核，其余按状态后排；同档按公司名稳定排序。
        rank = {"pending": 0, "needs_review": 1, "calibrated": 2, "rejected": 3}
        items.sort(key=lambda item: (rank.get(item["status"], 9), item["company_name"]))
        return {
            "ok": True,
            "status": status or "pending",
            "query": str(query or "").strip(),
            "items": items[:limit],
            "total": len(items),
            "graph_trace": graph_trace,
            "status_labels": dict(QUEUE_STATUS_LABELS),
        }

    def get_calibration(self, company_key: str) -> dict[str, Any]:
        """校准详情（图谱原始条目 + 校准记录）；公司不在图谱 → LookupError。"""
        key = str(company_key or "").strip()
        graph, _trace = self._graph()
        name = next((candidate for candidate in graph if kb.normalize_client_name(candidate) == key), None)
        if name is None:
            name = self._resolve_graph_name(graph, key)
        if name is None:
            raise LookupError(f"公司不在图谱中：{company_key}")
        conn = connect(self.db_path)
        try:
            records = self._calibrations_by_key(conn)
        finally:
            conn.close()
        row = records.get(key)
        info = graph[name]
        return {
            "ok": True,
            "company_key": key,
            "company_name": name,
            "track": info.get("track", ""),
            "business": info.get("business", ""),
            "categories": list(info.get("categories") or []),
            "status": str(row["status"]) if row else "pending",
            "status_label": QUEUE_STATUS_LABELS.get(str(row["status"]) if row else "pending", ""),
            "calibration": self._record(row) if row else None,
        }

    def get_progress(self) -> dict[str, Any]:
        """进度指示：已校准 N / 目标 TARGET_COMPANY_GOAL + 各状态计数。"""
        graph, _trace = self._graph()
        conn = connect(self.db_path)
        try:
            records = self._calibrations_by_key(conn)
        finally:
            conn.close()
        counts = {status: 0 for status in CALIBRATION_STATUSES}
        for row in records.values():
            status = str(row["status"])
            if status in counts:
                counts[status] += 1
        calibrated = counts["calibrated"]
        pending = max(0, len(graph) - sum(counts.values()))
        return {
            "ok": True,
            "target": TARGET_COMPANY_GOAL,
            "calibrated": calibrated,
            "needs_review": counts["needs_review"],
            "rejected": counts["rejected"],
            "pending": pending,
            "total": len(graph),
            "ratio": round(calibrated / TARGET_COMPANY_GOAL, 4) if TARGET_COMPANY_GOAL else None,
        }

    # ------------------------------------------------------------------
    # 写：提交校准（upsert + 版本化 + 同内容幂等）
    # ------------------------------------------------------------------

    def submit(
        self,
        company_name: str,
        *,
        status: str = "calibrated",
        track: str = "",
        product_lines: list[str] | tuple[str, ...] = (),
        skill_tags: list[str] | tuple[str, ...] = (),
        level_system: str = "",
        no_poach: bool = False,
        non_compete: bool = False,
        note: str = "",
        calibrated_by: str = "consultant",
    ) -> dict[str, Any]:
        """提交校准：公司必须在图谱中（LookupError）；status 三枚举（ValueError）。

        幂等+版本：同公司同内容（全部校准字段+status）重复提交 → changed=False、
        version 不变；内容变化 → version 自增并刷新 calibrated_by/calibrated_at。
        """
        name = " ".join(str(company_name or "").split())
        if not name:
            raise ValueError("公司名不能为空")
        status = str(status or "").strip().lower()
        if status not in CALIBRATION_STATUSES:
            raise ValueError(f"未知校准状态：{status or '（空）'}（可选：{'/'.join(CALIBRATION_STATUSES)}）")
        key = kb.normalize_client_name(name)
        if not key:
            raise ValueError("公司名规范化后为空，无法校准")
        graph, _trace = self._graph()
        graph_name = self._resolve_graph_name(graph, name)
        if graph_name is None:
            raise LookupError(f"公司不在图谱中：{name}（校准是图谱覆盖层，不新建图谱条目）")
        # company_key 一律锚定图谱条目的规范化名，与覆盖层合并钩子按键对齐。
        key = kb.normalize_client_name(graph_name)

        fields = {
            "track": " ".join(str(track or "").split()),
            "product_lines": _clean_list(product_lines),
            "skill_tags": _clean_list(skill_tags),
            "level_system": " ".join(str(level_system or "").split()),
            "no_poach": bool(no_poach),
            "non_compete": bool(non_compete),
            "note": " ".join(str(note or "").split()),
            "status": status,
        }
        calibrated_by = " ".join(str(calibrated_by or "").split()) or "consultant"

        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM company_calibrations WHERE company_key=?", (key,)
            ).fetchone()
            changed = True
            if row is not None:
                current = self._record(row)
                same = all(
                    current[field] == value
                    for field, value in fields.items()
                )
                if same:
                    changed = False
                else:
                    conn.execute(
                        """UPDATE company_calibrations
                              SET company_name=?,track=?,product_lines_json=?,skill_tags_json=?,
                                  level_system=?,no_poach=?,non_compete=?,note=?,status=?,
                                  calibrated_by=?,calibrated_at=datetime('now','localtime'),
                                  version=version+1,updated_at=datetime('now','localtime')
                            WHERE company_key=?""",
                        (
                            graph_name,
                            fields["track"],
                            json.dumps(fields["product_lines"], ensure_ascii=False),
                            json.dumps(fields["skill_tags"], ensure_ascii=False),
                            fields["level_system"],
                            1 if fields["no_poach"] else 0,
                            1 if fields["non_compete"] else 0,
                            fields["note"],
                            fields["status"],
                            calibrated_by,
                            key,
                        ),
                    )
            if row is None:
                calibration_id = f"ccal_{secrets.token_urlsafe(10)}"
                conn.execute(
                    """INSERT INTO company_calibrations
                       (calibration_id,company_key,company_name,track,product_lines_json,skill_tags_json,
                        level_system,no_poach,non_compete,note,status,calibrated_by,
                        calibrated_at,version,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),1,
                               datetime('now','localtime'),datetime('now','localtime'))""",
                    (
                        calibration_id,
                        key,
                        graph_name,
                        fields["track"],
                        json.dumps(fields["product_lines"], ensure_ascii=False),
                        json.dumps(fields["skill_tags"], ensure_ascii=False),
                        fields["level_system"],
                        1 if fields["no_poach"] else 0,
                        1 if fields["non_compete"] else 0,
                        fields["note"],
                        fields["status"],
                        calibrated_by,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM company_calibrations WHERE company_key=?", (key,)
            ).fetchone()
        record = self._record(updated)
        return {
            "ok": True,
            "changed": changed,
            "company_key": key,
            "company_name": graph_name,
            "status": record["status"],
            "status_label": record["status_label"],
            "version": record["version"],
            "calibration": record,
            "progress": self.get_progress(),
        }
