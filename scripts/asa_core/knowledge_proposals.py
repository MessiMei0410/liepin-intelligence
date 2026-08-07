"""knowledge_proposal 知识增补提案链路（二期知识飞轮）。

Agent 从三类已落库事实中确定性（不依赖 LLM）提出知识增补建议：
1. 停止原因聚类：同一客户同一标准化停止原因 ≥3 次 → negative_rule 提案
   （如"方向不符"反复出现 → 建议固化为该客户的排除/前置过滤规则）；
2. 客户反馈聚类：版本化推荐包 rejected/hold 反馈同一客户 ≥2 次 → negative_rule 提案；
3. 已确认推荐聚类：同一现职公司 ≥2 次顾问确认推荐且不在公司图谱 → company_graph_entry 提案。

保守口径：证据不足（低于阈值/不可结构化的枚举值）只留候选不生成提案；
每个提案带可读证据列表；UNIQUE(proposal_type, content_key) 保证同一来源同一内容
不重复提案（含已拒绝的提案，不反复骚扰顾问）。

确认链路复用 preflight/commit 两段模式（参照 agent_learning_rules 的 300s 确认令牌
+ 内容签名）：preflight 发令牌并对提案内容签名，decision 校验令牌+签名未漂移后才执行。
accept → 写入对应知识文件（company_graph_entry 追加进图谱 companies；其余类型追加进
kb_agent_confirmed_rules_v1.json），条目带 proposed_by=consultant_confirmed 标记与版本；
reject → 落 rejected 并必须附原因。写知识文件走 临时文件+os.replace 原子替换；
asa-web/knowledge_base 与 /Users/messi/Documents/ASA/knowledge_base 是硬链接镜像，
原子替换会断链，写后重新建立硬链接（失败降级为镜像目录原子复制），保持两处内容一致。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .database import connect, json_value, transaction
from .service import STOP_STAGES, STOP_STATUSES
from .stop_reasons import STOP_REASON_LABELS
from a_system_agent import knowledge_base as kb
from a_system_agent.strategy_v2 import knowledge_base_dir

PROPOSAL_TYPES = ("company_graph_entry", "skill_alias", "level_mapping", "negative_rule")
PROPOSAL_TYPE_LABELS = {
    "company_graph_entry": "公司图谱条目",
    "skill_alias": "技能别名",
    "level_mapping": "职级映射",
    "negative_rule": "排除规则",
}
PROPOSAL_STATUSES = ("pending", "accepted", "rejected", "superseded")
PROPOSAL_STATUS_LABELS = {
    "pending": "待确认",
    "accepted": "已入库",
    "rejected": "已拒绝",
    "superseded": "已被取代",
}

# 无专属知识文件的类型，接受后统一追加到该文件（图谱条目直接写图谱文件）。
CONFIRMED_RULES_FILE = "kb_agent_confirmed_rules_v1.json"

# 生成阈值（保守）：停止原因聚类 ≥3；客户反馈聚类 ≥2；确认推荐公司聚类 ≥2。
STOP_REASON_MIN_CLUSTER = 3
FEEDBACK_MIN_CLUSTER = 2
COMPANY_MIN_CLUSTER = 2

# 只有这些标准化停止原因可结构化为排除/过滤规则；other/low_intent/duplicate_candidate
# 指向个体判断而非可固化知识，只留候选不生成提案。
STRUCTURABLE_STOP_REASONS = (
    "direction_mismatch",
    "experience_mismatch",
    "location_mismatch",
    "salary_mismatch",
    "too_senior",
)

CONFIRMATION_TTL_SECONDS = 300
_EVIDENCE_ID_CAP = 10

# 已验证：这两个 knowledge_base 目录互为硬链接镜像（同 inode）。写一处后重建镜像。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_KB_MIRROR_PAIRS: tuple[tuple[Path, Path], ...] = (
    (
        _REPO_ROOT / "asa-web" / "knowledge_base",
        Path("/Users/messi/Documents/ASA/knowledge_base"),
    ),
)


def _content_key(proposal_type: str, content: dict[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{proposal_type}|{canonical}".encode("utf-8")).hexdigest()


def _signature(row: sqlite3.Row) -> str:
    return hashlib.sha256(
        f"{row['id']}|{row['proposal_type']}|{row['content_json']}|{row['content_key']}".encode("utf-8")
    ).hexdigest()


def _mirror_dirs_for(directory: Path) -> list[Path]:
    resolved = directory.expanduser().resolve()
    mirrors: list[Path] = []
    for left, right in _KB_MIRROR_PAIRS:
        for source, target in ((left, right), (right, left)):
            if resolved == source.expanduser().resolve() and target.is_dir():
                mirrors.append(target)
    return mirrors


def _atomic_write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_kb_json(directory: Path, file_name: str, doc: Any) -> list[str]:
    """原子写知识文件并保持硬链接镜像一致；返回写入的路径列表。"""
    directory = Path(directory)
    target = directory / file_name
    _atomic_write_json(target, doc)
    written = [str(target)]
    for mirror in _mirror_dirs_for(directory):
        mirror_target = mirror / file_name
        try:
            if mirror_target.exists():
                if os.stat(mirror_target).st_ino == os.stat(target).st_ino:
                    continue
                mirror_target.unlink()
            os.link(target, mirror_target)
        except OSError:
            # 跨设备/权限等无法硬链接时降级为镜像目录原子复制，保证内容一致。
            _atomic_write_json(mirror_target, doc)
        written.append(str(mirror_target))
    return written


def _stopped_where(alias: str = "jc") -> tuple[str, list[Any]]:
    stage_clause = " OR ".join(f"{alias}.clean_stage LIKE ?" for _ in STOP_STAGES)
    status_clause = ",".join("?" for _ in STOP_STATUSES)
    where = f"({stage_clause}) OR lower(COALESCE({alias}.raw_status,'')) IN ({status_clause})"
    return where, [f"%{token}%" for token in STOP_STAGES] + sorted(STOP_STATUSES)


class KnowledgeProposalService:
    """知识增补提案：确定性生成 + 两段确认 + 知识文件原子写入。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._confirmations: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _brief(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "proposal_type": row["proposal_type"],
            "proposal_type_label": PROPOSAL_TYPE_LABELS.get(row["proposal_type"], row["proposal_type"]),
            "title": row["title"],
            "status": row["status"],
            "status_label": PROPOSAL_STATUS_LABELS.get(row["status"], row["status"]),
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
        }

    def _detail(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = self._brief(row)
        payload.update(
            {
                "ok": True,
                "content": json_value(row["content_json"], {}),
                "evidence": json_value(row["evidence_json"], []),
                "applied_to": row["applied_to"],
                "decision_note": row["decision_note"],
                "updated_at": row["updated_at"],
            }
        )
        return payload

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def list_proposals(self, status: str = "pending", proposal_type: str = "", limit: int = 50) -> dict[str, Any]:
        status = str(status or "pending").strip().lower()
        if status != "all" and status not in PROPOSAL_STATUSES:
            raise ValueError(f"未知提案状态：{status}")
        proposal_type = str(proposal_type or "").strip()
        if proposal_type and proposal_type not in PROPOSAL_TYPES:
            raise ValueError(f"未知提案类型：{proposal_type}")
        limit = max(1, min(int(limit or 50), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if proposal_type:
            clauses.append("proposal_type=?")
            params.append(proposal_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                f"""SELECT * FROM knowledge_proposals {where}
                    ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC LIMIT ?""",
                (*params, limit),
            ).fetchall()
            counts = {
                str(item["status"]): int(item["n"])
                for item in conn.execute("SELECT status, COUNT(*) AS n FROM knowledge_proposals GROUP BY status").fetchall()
            }
        finally:
            conn.close()
        return {
            "ok": True,
            "status": status,
            "items": [self._brief(row) for row in rows],
            "counts": {key: counts.get(key, 0) for key in PROPOSAL_STATUSES},
            "type_labels": dict(PROPOSAL_TYPE_LABELS),
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM knowledge_proposals WHERE proposal_id=?",
                (str(proposal_id or "").strip(),),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise LookupError("knowledge proposal not found")
        return self._detail(row)

    # ------------------------------------------------------------------
    # 生成（确定性规则，幂等：UNIQUE(proposal_type, content_key) 兜底）
    # ------------------------------------------------------------------

    def generate(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit or 50), 200))
        conn = connect(self.db_path)
        try:
            specs = [
                *self._stop_reason_specs(conn),
                *self._feedback_specs(conn),
                *self._company_graph_specs(conn),
            ]
        finally:
            conn.close()
        proposals = [spec for spec in specs if spec.get("ready")]
        candidates = [spec["candidate"] for spec in specs if not spec.get("ready")]

        created: list[dict[str, Any]] = []
        existing: list[dict[str, Any]] = []
        with transaction(self.db_path) as conn:
            for spec in proposals[:limit]:
                row = conn.execute(
                    "SELECT * FROM knowledge_proposals WHERE proposal_type=? AND content_key=?",
                    (spec["proposal_type"], spec["content_key"]),
                ).fetchone()
                if row:
                    existing.append(self._brief(row))
                    continue
                proposal_id = f"kprop_{secrets.token_urlsafe(10)}"
                conn.execute(
                    """INSERT INTO knowledge_proposals
                       (proposal_id,proposal_type,title,content_json,evidence_json,content_key,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,'pending',datetime('now','localtime'),datetime('now','localtime'))""",
                    (
                        proposal_id,
                        spec["proposal_type"],
                        spec["title"],
                        json.dumps(spec["content"], ensure_ascii=False),
                        json.dumps(spec["evidence"], ensure_ascii=False),
                        spec["content_key"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM knowledge_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                created.append(self._brief(row))
        return {
            "ok": True,
            "created": created,
            "existing": existing,
            "candidates": candidates,
            "thresholds": {
                "stop_reason_min_cluster": STOP_REASON_MIN_CLUSTER,
                "feedback_min_cluster": FEEDBACK_MIN_CLUSTER,
                "company_min_cluster": COMPANY_MIN_CLUSTER,
            },
        }

    def _spec(self, proposal_type: str, title: str, content: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ready": True,
            "proposal_type": proposal_type,
            "title": title,
            "content": content,
            "evidence": evidence,
            "content_key": _content_key(proposal_type, content),
        }

    @staticmethod
    def _candidate(kind: str, key: str, count: int, needed: int, reason: str) -> dict[str, Any]:
        return {"ready": False, "candidate": {"kind": kind, "key": key, "count": count, "needed": needed, "reason": reason}}

    def _stop_reason_specs(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        where, params = _stopped_where()
        rows = conn.execute(
            f"""SELECT jc.id, jc.stop_reason, j.title AS job_title, c.name AS client
                  FROM job_candidates jc
                  LEFT JOIN jobs j ON j.id=jc.job_id
                  LEFT JOIN clients c ON c.id=j.client_id
                 WHERE ({where}) AND jc.stop_reason IS NOT NULL AND trim(jc.stop_reason)<>''""",
            params,
        ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            client = str(row["client"] or "").strip() or "（未关联客户）"
            code = str(row["stop_reason"] or "").strip()
            groups.setdefault((client, code), []).append(row)
        specs: list[dict[str, Any]] = []
        for (client, code), items in sorted(groups.items()):
            label = STOP_REASON_LABELS.get(code, code)
            if code not in STRUCTURABLE_STOP_REASONS:
                specs.append(self._candidate(
                    "stop_reason", f"{client} × {label}", len(items), STOP_REASON_MIN_CLUSTER,
                    f"停止原因「{label}」不可结构化为排除规则，只留候选",
                ))
                continue
            if len(items) < STOP_REASON_MIN_CLUSTER:
                specs.append(self._candidate(
                    "stop_reason", f"{client} × {label}", len(items), STOP_REASON_MIN_CLUSTER,
                    f"证据不足：{len(items)} 次 < 阈值 {STOP_REASON_MIN_CLUSTER} 次，只留候选",
                ))
                continue
            ids = [int(item["id"]) for item in items[:_EVIDENCE_ID_CAP]]
            samples = [
                {"job_candidate_id": int(item["id"]), "job_title": str(item["job_title"] or "")}
                for item in items[:5]
            ]
            content = {
                "scope_type": "client",
                "scope": client,
                "rule": (
                    f"客户「{client}」人选多次因「{label}」停止推进（{len(items)} 次），"
                    f"建议在寻访策略中把该特征固化为排除/前置过滤规则"
                ),
                "trigger": "stop_reason_cluster",
                "trigger_code": code,
                "occurrences": len(items),
            }
            specs.append(self._spec(
                "negative_rule",
                f"排除规则建议：{client} × {label}",
                content,
                [{
                    "source_type": "stop_reason",
                    "source_ids": ids,
                    "summary": f"{client} 岗位停止原因「{label}」累计 {len(items)} 次",
                    "samples": samples,
                }],
            ))
        return specs

    def _feedback_specs(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recommendation_package_feedback'"
        ).fetchone()
        if not has_table:
            return []
        rows = conn.execute(
            """SELECT f.id, f.feedback_type, f.content, f.package_id, j.title AS job_title, c.name AS client
                 FROM recommendation_package_feedback f
                 LEFT JOIN jobs j ON j.id=f.job_id
                 LEFT JOIN clients c ON c.id=j.client_id
                WHERE f.feedback_type IN ('rejected','hold')"""
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            client = str(row["client"] or "").strip() or "（未关联客户）"
            groups.setdefault(client, []).append(row)
        specs: list[dict[str, Any]] = []
        for client, items in sorted(groups.items()):
            if len(items) < FEEDBACK_MIN_CLUSTER:
                specs.append(self._candidate(
                    "client_feedback", client, len(items), FEEDBACK_MIN_CLUSTER,
                    f"证据不足：{len(items)} 条 < 阈值 {FEEDBACK_MIN_CLUSTER} 条，只留候选",
                ))
                continue
            ids = [int(item["id"]) for item in items[:_EVIDENCE_ID_CAP]]
            samples = [
                {
                    "feedback_id": int(item["id"]),
                    "feedback_type": item["feedback_type"],
                    "job_title": str(item["job_title"] or ""),
                    "content": str(item["content"] or "")[:120],
                }
                for item in items[:5]
            ]
            rejected = sum(1 for item in items if item["feedback_type"] == "rejected")
            content = {
                "scope_type": "client",
                "scope": client,
                "rule": (
                    f"客户「{client}」对推荐包多次给出否决/暂缓反馈（共 {len(items)} 条，其中否决 {rejected} 条），"
                    f"建议顾问复核反馈内容后固化为该客户的排除/复核规则"
                ),
                "trigger": "client_feedback_cluster",
                "occurrences": len(items),
            }
            specs.append(self._spec(
                "negative_rule",
                f"排除规则建议：{client} 客户反馈聚类",
                content,
                [{
                    "source_type": "client_feedback",
                    "source_ids": ids,
                    "summary": f"{client} 的推荐包 rejected/hold 反馈累计 {len(items)} 条（否决 {rejected} 条）",
                    "samples": samples,
                }],
            ))
        return specs

    def _company_graph_specs(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT cr.id, cr.job_candidate_id, cr.reason, p.current_company, j.title AS job_title
                 FROM consultant_confirmed_recommendations cr
                 JOIN people p ON p.id=cr.person_id
                 LEFT JOIN jobs j ON j.id=cr.job_id"""
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        display: dict[str, str] = {}
        for row in rows:
            raw = " ".join(str(row["current_company"] or "").split())
            norm = kb.normalize_client_name(raw)
            if not norm:
                continue
            groups.setdefault(norm, []).append(row)
            display.setdefault(norm, raw)
        if not groups:
            return []
        graph, _trace = kb.load_company_graph()
        graph_norms = {kb.normalize_client_name(name) for name in graph}
        specs: list[dict[str, Any]] = []
        for norm, items in sorted(groups.items()):
            name = display[norm]
            if norm in graph_norms:
                continue  # 已在图谱，无增补必要（不提案也不留候选）
            if len(items) < COMPANY_MIN_CLUSTER:
                specs.append(self._candidate(
                    "confirmed_recommendation", name, len(items), COMPANY_MIN_CLUSTER,
                    f"证据不足：{len(items)} 次确认推荐 < 阈值 {COMPANY_MIN_CLUSTER} 次，只留候选",
                ))
                continue
            ids = [int(item["id"]) for item in items[:_EVIDENCE_ID_CAP]]
            samples = [
                {
                    "recommendation_id": int(item["id"]),
                    "job_candidate_id": int(item["job_candidate_id"]),
                    "job_title": str(item["job_title"] or ""),
                }
                for item in items[:5]
            ]
            content = {
                "name": name,
                "track": "",
                "business": "",
                "categories": [],
                "rationale": (
                    f"该公司现职人选被顾问确认推荐 {len(items)} 次（正向信号），"
                    f"建议补入公司图谱（赛道/主营业务由顾问确认后补充）"
                ),
                "trigger": "confirmed_recommendation_cluster",
                "occurrences": len(items),
            }
            specs.append(self._spec(
                "company_graph_entry",
                f"公司图谱增补：{name}",
                content,
                [{
                    "source_type": "confirmed_recommendation",
                    "source_ids": ids,
                    "summary": f"「{name}」现职人选被顾问确认推荐 {len(items)} 次，且公司不在现有图谱中",
                    "samples": samples,
                }],
            ))
        return specs

    # ------------------------------------------------------------------
    # 确认链路（preflight 令牌 + 内容签名 → decision accept/reject）
    # ------------------------------------------------------------------

    def preflight(self, proposal_id: str) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM knowledge_proposals WHERE proposal_id=?",
                (str(proposal_id or "").strip(),),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise LookupError("knowledge proposal not found")
        if row["status"] != "pending":
            raise ValueError(f"提案当前状态不可确认：{PROPOSAL_STATUS_LABELS.get(row['status'], row['status'])}")
        token = secrets.token_urlsafe(24)
        signature = _signature(row)
        with self._lock:
            now = time.time()
            self._confirmations = {
                key: value for key, value in self._confirmations.items() if value["expires_at"] > now
            }
            self._confirmations[token] = {
                "proposal_id": row["proposal_id"],
                "signature": signature,
                "expires_at": now + CONFIRMATION_TTL_SECONDS,
            }
        impact = (
            "接受后把该公司追加进公司图谱 kb_company_graph_jsj_v1.json（带 proposed_by 标记），"
            if row["proposal_type"] == "company_graph_entry"
            else f"接受后把该规则追加进 {CONFIRMED_RULES_FILE}（带 proposed_by 标记），"
        )
        impact += "写入立即生效且会同步镜像知识库目录；拒绝则提案落 rejected 并保留原因。"
        return {
            "ok": True,
            "confirmation_token": token,
            "expires_in": CONFIRMATION_TTL_SECONDS,
            "signature": signature,
            "proposal": self._detail(row),
            "impact": impact,
        }

    def decide(self, proposal_id: str, confirmation_token: str, decision: str, note: str = "") -> dict[str, Any]:
        decision = str(decision or "").strip().lower()
        if decision not in {"accept", "reject"}:
            raise ValueError(f"未知提案决策：{decision or '（空）'}")
        note = " ".join(str(note or "").split())
        if decision == "reject" and not note:
            raise ValueError("拒绝提案必须填写原因（note）")
        with self._lock:
            grant = self._confirmations.pop(str(confirmation_token or ""), None)
        if not grant or grant["expires_at"] <= time.time():
            raise ValueError("确认令牌无效或已过期，请重新预检")
        if grant["proposal_id"] != str(proposal_id or "").strip():
            raise ValueError("确认令牌与提案不匹配")

        applied_to = ""
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_proposals WHERE proposal_id=?",
                (grant["proposal_id"],),
            ).fetchone()
            if not row:
                raise LookupError("knowledge proposal not found")
            if row["status"] != "pending":
                raise ValueError("提案状态已变化，请重新预检")
            if _signature(row) != grant["signature"]:
                raise ValueError("提案内容已变化，请重新预检")
            if decision == "accept":
                applied_to, status = self._apply(row)
            else:
                status = "rejected"
            conn.execute(
                """UPDATE knowledge_proposals
                      SET status=?,confirm_token=?,applied_to=?,decided_by='consultant',decision_note=?,
                          decided_at=datetime('now','localtime'),updated_at=datetime('now','localtime')
                    WHERE id=?""",
                (status, confirmation_token, applied_to, note, int(row["id"])),
            )
            updated = conn.execute(
                "SELECT * FROM knowledge_proposals WHERE id=?", (int(row["id"]),)
            ).fetchone()
        return {
            "ok": True,
            "proposal_id": grant["proposal_id"],
            "decision": decision,
            "status": status,
            "status_label": PROPOSAL_STATUS_LABELS.get(status, status),
            "applied_to": applied_to,
            "proposal": self._detail(updated),
        }

    def _apply(self, row: sqlite3.Row) -> tuple[str, str]:
        """把提案内容写入对应知识文件；返回 (applied_to, 最终状态)。"""
        content = json_value(row["content_json"], {})
        kb_dir = knowledge_base_dir()
        if row["proposal_type"] == "company_graph_entry":
            return self._apply_company_graph(kb_dir, row, content)
        return self._apply_confirmed_rule(kb_dir, row, content)

    def _apply_company_graph(self, kb_dir: Path, row: sqlite3.Row, content: dict[str, Any]) -> tuple[str, str]:
        name = " ".join(str(content.get("name") or "").split())
        if not name:
            raise ValueError("提案内容缺公司名，无法入库")
        path = kb_dir / kb.COMPANY_GRAPH_FILE
        if not path.is_file():
            raise ValueError(f"公司图谱 {kb.COMPANY_GRAPH_FILE} 缺失（{kb_dir}），无法写入")
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"公司图谱解析失败（{exc.__class__.__name__}），未写入") from exc
        companies = doc.get("companies") if isinstance(doc, dict) else None
        if not isinstance(companies, dict):
            raise ValueError(f"公司图谱结构异常（缺 companies 对象），未写入")
        existing = next(
            (key for key in companies if kb.normalize_client_name(key) == kb.normalize_client_name(name)),
            None,
        )
        if existing is not None:
            # 图谱在提案生成后已含该公司：不重复写，提案落 superseded 如实留痕。
            return f"{kb.COMPANY_GRAPH_FILE}（已存在条目：{existing}）", "superseded"
        companies[name] = {
            "track": str(content.get("track") or ""),
            "business": str(content.get("business") or ""),
            "categories": [str(item) for item in content.get("categories") or [] if str(item or "").strip()],
            "proposed_by": "consultant_confirmed",
            "source": "knowledge_proposal",
            "proposal_id": row["proposal_id"],
            "added_at": datetime.now().strftime("%Y-%m-%d"),
        }
        meta = doc.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["updated"] = datetime.now().strftime("%Y-%m-%d")
            meta["version"] = f"{meta.get('version') or 'v1'}+kprop"
        stats = doc.get("stats")
        if isinstance(stats, dict) and isinstance(stats.get("companies"), int):
            stats["companies"] = len(companies)
        written = write_kb_json(kb_dir, kb.COMPANY_GRAPH_FILE, doc)
        return ";".join(written), "accepted"

    def _apply_confirmed_rule(self, kb_dir: Path, row: sqlite3.Row, content: dict[str, Any]) -> tuple[str, str]:
        path = kb_dir / CONFIRMED_RULES_FILE
        doc: dict[str, Any]
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"{CONFIRMED_RULES_FILE} 解析失败（{exc.__class__.__name__}），未写入") from exc
            doc = loaded if isinstance(loaded, dict) else {}
        else:
            doc = {}
        meta = doc.setdefault("meta", {})
        if not isinstance(meta, dict):
            meta = doc["meta"] = {}
        meta.setdefault("version", "v1")
        meta.setdefault("created", datetime.now().strftime("%Y-%m-%d"))
        meta["updated"] = datetime.now().strftime("%Y-%m-%d")
        rules = doc.get("rules")
        if not isinstance(rules, list):
            rules = doc["rules"] = []
        if not any(isinstance(item, dict) and item.get("proposal_id") == row["proposal_id"] for item in rules):
            rules.append(
                {
                    "rule_id": f"krule_{secrets.token_urlsafe(8)}",
                    "rule_type": row["proposal_type"],
                    "title": row["title"],
                    "content": content,
                    "evidence": json_value(row["evidence_json"], []),
                    "proposed_by": "consultant_confirmed",
                    "source": "knowledge_proposal",
                    "proposal_id": row["proposal_id"],
                    "version": meta.get("version") or "v1",
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        written = write_kb_json(kb_dir, CONFIRMED_RULES_FILE, doc)
        return ";".join(written), "accepted"
