"""候选人去重：只读扫描 + 合并动作（DSH 护栏第 6 条的机制落地）。

护栏口径（dsh/asa-profile/AGENTS.md §6 / a_system_agent.mapping_task §6.4）：
遮罩名合并需「姓氏 + 公司 + 职位」证据同时匹配，走只读预检 + 一次性确认。
此前全系统只有护栏文字、没有任何合并机制，本模块补齐两端：

- dedupe_scan（只读）：按 §6.4 既有归一化/互证函数（mapping_task 模块复用，
  不另造规则）对 job_candidates 聚类，返回疑似重复组供顾问/Agent 审查。
- candidate_merge_preflight / candidate_merge_commit：合并走 #61 写确认链路
  （preflight 铸造未激活一次性 token → UI 激活 → commit）。合并不物理合并行：
  commit 把 loser 关系按既有停止口径标记停止（stop_reason=duplicate_candidate
  「重复人选」），note 指向 winner 关系 id；事件/跟进记录保留原行不动。
  幂等：loser 已停止 → already_applied 返回；winner 已停止 → 409。

合并停止不回灌寻访学习信号（duplicate 不是寻访查询失败的证据），
也不触发停止备注寻访调整分析。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

# §6.4 既有口径函数（遮罩名归一/互证、公司职位文本匹配），与 MULTICHANNEL intake 同源。
from a_system_agent.mapping_task import (
    _identity_text_matches,
    _is_masked_name,
    _names_can_correspond,
    _normalize_person_name,
)

from .database import connect, json_value, transaction
from .service_candidate_actions import _is_stopped, _row
from .stop_reasons import STOP_REASON_LABELS

MERGE_STOP_REASON = "duplicate_candidate"
RESUME_EXCERPT_CHARS = 200


def _surname(name: Any) -> str:
    """姓氏：与 _names_can_correspond 同口径的归一化（去 老师/先生/女士）后取首字。"""
    text = _normalize_person_name(name).replace("先生", "").replace("女士", "").strip()
    return text[:1]


def merge_evidence(
    name_a: Any, company_a: Any, title_a: Any,
    name_b: Any, company_b: Any, title_b: Any,
) -> dict[str, Any]:
    """三证据校验（§6.4）：姓氏相同 + 遮罩名可互证 + 公司互含 + 职位互含。

    公司/职位用 _identity_text_matches（去空白后互为子串），因此
    「晶盛机电」与「晶盛机电（半导体、光伏设备）」这类括号后缀变体判同。
    """
    surname_a = _surname(name_a)
    surname_b = _surname(name_b)
    evidence = {
        "surname": bool(surname_a and surname_a == surname_b),
        "name_correspond": _names_can_correspond(name_a, name_b),
        "company": _identity_text_matches(company_a, company_b),
        "title": _identity_text_matches(title_a, title_b),
    }
    evidence["matched"] = all(evidence.values())
    return evidence


def _relation_row(conn: Any, relation_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT jc.id, jc.job_id, jc.person_id, jc.clean_stage, jc.raw_status, jc.stop_reason,
               jc.source_candidate_id, jc.updated_at,
               p.display_name, p.current_company, p.current_title,
               j.title AS job_title, c.name AS client
          FROM job_candidates jc
          JOIN people p ON p.id=jc.person_id
          LEFT JOIN jobs j ON j.id=jc.job_id
          LEFT JOIN clients c ON c.id=j.client_id
         WHERE jc.id=?
        """,
        (relation_id,),
    ).fetchone()
    return _row(row)


def _resume_excerpt(conn: Any, person_id: Any) -> str:
    """简历摘要前 N 字：取该 person 最新 source_profiles 里的 full_text/profile_text。"""
    if not person_id:
        return ""
    rows = conn.execute(
        "SELECT raw_json FROM source_profiles WHERE person_id=? ORDER BY id DESC LIMIT 5",
        (person_id,),
    ).fetchall()
    best = ""
    for row in rows:
        raw = json_value(row[0], {})
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("full_text") or raw.get("profile_text") or "").strip()
        if len(text) > len(best):
            best = text
    return re.sub(r"\s+", " ", best)[:RESUME_EXCERPT_CHARS]


def _source_type(conn: Any, person_id: Any, source_candidate_id: Any) -> str:
    row = conn.execute(
        "SELECT source_type FROM source_profiles WHERE person_id=? ORDER BY id DESC LIMIT 1",
        (person_id,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    return "liepin" if source_candidate_id is not None else "talent_pool"


def _relation_brief(conn: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "person_id": row["person_id"],
        "name": row["display_name"] or "",
        "current_company": row["current_company"] or "",
        "current_title": row["current_title"] or "",
        "job_id": row["job_id"],
        "job_title": row.get("job_title") or "",
        "client": row.get("client") or "",
        "stage": row["clean_stage"] or "",
        "is_stopped": _is_stopped(row["clean_stage"], row["raw_status"]),
        "stop_reason": row["stop_reason"] or "",
        "source_type": _source_type(conn, row["person_id"], row["source_candidate_id"]),
    }


class CandidateDedupeMixin:
    """候选人去重域：dedupe_scan 只读聚类 + merge 写确认链路。"""

    # ------------------------------------------------------------------
    # 只读扫描
    # ------------------------------------------------------------------

    def dedupe_scan(self, *, job_id: int | None = None) -> dict[str, Any]:
        """疑似重复组扫描（只读）：§6.4 口径（姓氏+公司+职位三证据）对
        job_candidates 聚类，返回组内关系数 >1 的组。"""
        clauses = ["1=1"]
        params: list[Any] = []
        if job_id:
            clauses.append("jc.job_id=?")
            params.append(int(job_id))
        conn = connect(self.db_path)
        try:
            rows = [
                _row(row)
                for row in conn.execute(
                    f"""
                    SELECT jc.id, jc.job_id, jc.person_id, jc.clean_stage, jc.raw_status, jc.stop_reason,
                           jc.source_candidate_id, jc.updated_at,
                           p.display_name, p.current_company, p.current_title,
                           j.title AS job_title,
                           (SELECT MAX(ce.event_time) FROM candidate_events ce
                             WHERE ce.job_candidate_id=jc.id) AS last_event_at,
                           COALESCE((SELECT sp.source_type FROM source_profiles sp
                                      WHERE sp.person_id=p.id ORDER BY sp.id DESC LIMIT 1),
                                    CASE WHEN jc.source_candidate_id IS NOT NULL THEN 'liepin' END,
                                    'talent_pool') AS source_type
                      FROM job_candidates jc
                      JOIN people p ON p.id=jc.person_id
                      LEFT JOIN jobs j ON j.id=jc.job_id
                     WHERE {' AND '.join(clauses)}
                    """,
                    params,
                )
            ]
        finally:
            conn.close()

        # 同姓分桶 → 桶内并查集：公司+职位互含且遮罩名可互证的关系并为一组。
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            surname = _surname(row["display_name"])
            if not surname or not str(row["current_company"] or "").strip() or not str(row["current_title"] or "").strip():
                continue
            buckets.setdefault(surname, []).append(row)

        groups: list[dict[str, Any]] = []
        for surname, members in buckets.items():
            if len(members) < 2:
                continue
            parent = list(range(len(members)))

            def find(index: int) -> int:
                while parent[index] != index:
                    parent[index] = parent[parent[index]]
                    index = parent[index]
                return index

            def union(left: int, right: int) -> None:
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[root_right] = root_left

            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    evidence = merge_evidence(
                        members[i]["display_name"], members[i]["current_company"], members[i]["current_title"],
                        members[j]["display_name"], members[j]["current_company"], members[j]["current_title"],
                    )
                    if evidence["matched"]:
                        union(i, j)

            clustered: dict[int, list[dict[str, Any]]] = {}
            for index, row in enumerate(members):
                clustered.setdefault(find(index), []).append(row)
            for cluster in clustered.values():
                if len(cluster) < 2:
                    continue
                # 稳定排序两步：先按最近事件倒序，再把未停止的提到前面（保持桶内倒序）。
                cluster.sort(key=lambda row: (str(row["last_event_at"] or row["updated_at"] or ""), int(row["id"])), reverse=True)
                cluster.sort(key=lambda row: _is_stopped(row["clean_stage"], row["raw_status"]))
                cluster_members = [
                    {
                        "relation_id": int(row["id"]),
                        "person_id": row["person_id"],
                        "name": row["display_name"] or "",
                        "name_masked": _is_masked_name(row["display_name"]),
                        "current_company": row["current_company"] or "",
                        "current_title": row["current_title"] or "",
                        "job_id": row["job_id"],
                        "job_title": row["job_title"] or "",
                        "stage": row["clean_stage"] or "",
                        "is_stopped": _is_stopped(row["clean_stage"], row["raw_status"]),
                        "stop_reason": row["stop_reason"] or "",
                        "source_type": row["source_type"] or "",
                        "updated_at": row["updated_at"] or "",
                        "last_event_at": row["last_event_at"] or "",
                    }
                    for row in cluster
                ]
                groups.append({
                    "group_id": f"dup_{len(groups) + 1}",
                    "surname": surname,
                    "company": cluster_members[0]["current_company"],
                    "title": cluster_members[0]["current_title"],
                    # 建议保留方：未停止且最近有事件的关系（排序后的首位）。
                    "suggested_winner_id": cluster_members[0]["relation_id"],
                    "members": cluster_members,
                })
        groups.sort(key=lambda group: (-len(group["members"]), group["group_id"]))
        return {
            "ok": True,
            "job_id": int(job_id) if job_id else None,
            "scanned_relations": len(rows),
            "group_count": len(groups),
            "groups": groups,
        }

    # ------------------------------------------------------------------
    # 合并：preflight（三证据 + diff）→ UI 激活 → commit（loser 停止）
    # ------------------------------------------------------------------

    def candidate_merge_preflight(self, winner_id: int, loser_id: int) -> dict[str, Any]:
        winner_id = int(winner_id)
        loser_id = int(loser_id)
        if winner_id == loser_id:
            raise ValueError("保留方（winner）与废弃方（loser）不能是同一条关系")
        conn = connect(self.db_path)
        try:
            winner_row = _relation_row(conn, winner_id)
            loser_row = _relation_row(conn, loser_id)
            if not winner_row or not loser_row:
                raise LookupError("candidate not found")
            if _is_stopped(winner_row["clean_stage"], winner_row["raw_status"]):
                raise ValueError("保留方（winner）关系已停止推进，不得向已停止人选合并")
            evidence = merge_evidence(
                winner_row["display_name"], winner_row["current_company"], winner_row["current_title"],
                loser_row["display_name"], loser_row["current_company"], loser_row["current_title"],
            )
            if not evidence["matched"]:
                missing = [
                    label
                    for key, label in (("surname", "姓氏"), ("name_correspond", "姓名互证"), ("company", "公司"), ("title", "职位"))
                    if not evidence[key]
                ]
                raise ValueError(f"合并证据不足（{'、'.join(missing)}不匹配）：遮罩名合并需姓氏+公司+职位证据同时匹配")
            winner = _relation_brief(conn, winner_row)
            loser = _relation_brief(conn, loser_row)
            winner_resume = _resume_excerpt(conn, winner_row["person_id"])
            loser_resume = _resume_excerpt(conn, loser_row["person_id"])
        finally:
            conn.close()

        diff = [
            {"field": "name", "label": "姓名", "winner": winner["name"], "loser": loser["name"], "same": winner["name"] == loser["name"]},
            {"field": "current_company", "label": "当前公司", "winner": winner["current_company"], "loser": loser["current_company"], "same": winner["current_company"] == loser["current_company"]},
            {"field": "current_title", "label": "当前职位", "winner": winner["current_title"], "loser": loser["current_title"], "same": winner["current_title"] == loser["current_title"]},
            {"field": "stage", "label": "阶段", "winner": winner["stage"], "loser": loser["stage"], "same": winner["stage"] == loser["stage"]},
            {"field": "source_type", "label": "来源", "winner": winner["source_type"], "loser": loser["source_type"], "same": winner["source_type"] == loser["source_type"]},
            {"field": "person_id", "label": "person_id", "winner": str(winner["person_id"] or ""), "loser": str(loser["person_id"] or ""), "same": winner["person_id"] == loser["person_id"]},
            {"field": "resume_excerpt", "label": f"简历摘要（前{RESUME_EXCERPT_CHARS}字）", "winner": winner_resume, "loser": loser_resume, "same": winner_resume == loser_resume},
        ]
        # 合并 token 的 target 是 (winner_id, loser_id) 二元组：动作语义绑定两条关系，
        # 单一 candidate_id 无法表达；commit 按同一二元组核销。
        token, expires = self._mint_write_token((winner_id, loser_id), "merge", activated=False)
        return {
            "ok": True,
            "token": token,
            "expires_at": expires.isoformat(timespec="seconds"),
            "action": "merge",
            # 确认卡既有投影位：candidate = 保留方（winner）。
            "candidate": {"id": winner["id"], "name": winner["name"], "stage": winner["stage"]},
            "winner": winner,
            "loser": loser,
            "diff": diff,
            "evidence": {
                "surname": _surname(winner["name"]),
                "surname_matched": evidence["surname"],
                "company_matched": evidence["company"],
                "title_matched": evidence["title"],
            },
            "loser_already_stopped": bool(loser["is_stopped"]),
            "impact": "合并不物理删行：废弃方关系将停止推进（停止原因：重复人选）并备注指向保留方；事件与跟进记录保留在原行。",
        }

    def candidate_merge_commit(self, winner_id: int, loser_id: int, note: str, preflight_token: str) -> dict[str, Any]:
        winner_id = int(winner_id)
        loser_id = int(loser_id)
        with self._preflight_lock:
            grant = self._preflight_tokens.pop(preflight_token, None)
        if not grant or grant[0] != (winner_id, loser_id) or grant[1] != "merge" or grant[2] <= datetime.now():
            raise ValueError("preflight token is invalid, expired, or already used")
        with transaction(self.db_path) as conn:
            winner_row = _relation_row(conn, winner_id)
            loser_row = _relation_row(conn, loser_id)
            if not winner_row or not loser_row:
                raise LookupError("candidate not found")
            if _is_stopped(winner_row["clean_stage"], winner_row["raw_status"]):
                raise ValueError("保留方（winner）关系已停止推进，不得向已停止人选合并")
            evidence = merge_evidence(
                winner_row["display_name"], winner_row["current_company"], winner_row["current_title"],
                loser_row["display_name"], loser_row["current_company"], loser_row["current_title"],
            )
            if not evidence["matched"]:
                raise ValueError("合并证据不足（姓氏+公司+职位需同时匹配），当前两条关系状态已变化，请重新扫描确认")
            merge_note = f"合并去重：与关系 #{winner_id}（{winner_row['display_name']}）为同一人，本条关系废弃"
            note = " ".join(str(note or "").split())
            if note:
                merge_note = f"{merge_note}｜{note}"
            if _is_stopped(loser_row["clean_stage"], loser_row["raw_status"]):
                # 幂等：loser 已停止（重复合并/此前已停止）→ already_applied，不再写事件。
                return {
                    "ok": True,
                    "action": "merge",
                    "winner_id": winner_id,
                    "loser_id": loser_id,
                    "stage": loser_row["clean_stage"] or "",
                    "stop_reason": loser_row["stop_reason"] or "",
                    "already_applied": True,
                }
            # 停止口径与 candidate_commit(stop) 完全一致（阶段/桶/状态含 X-SaaS 变体）。
            is_xsaas = str(loser_row["clean_stage"] or "").startswith("X") or str(loser_row["raw_status"] or "").startswith("xsaas_")
            stage = "H5 最近寻访/初筛不通过"
            bucket = "最近寻访"
            raw_status = "xsaas_review_stop" if is_xsaas else "screen_rejected"
            conn.execute(
                """
                UPDATE job_candidates
                   SET clean_stage=?,flow_bucket=?,raw_status=?,raw_stage=?,clean_reason=?,
                       stop_reason=?,updated_at=datetime('now','localtime')
                 WHERE id=?
                """,
                (stage, bucket, raw_status, stage, merge_note, MERGE_STOP_REASON, loser_id),
            )
            source_candidate_id = str(loser_row["source_candidate_id"] or "").strip()
            if source_candidate_id.isdigit():
                conn.execute(
                    """
                    UPDATE candidates
                       SET status=?,
                           notes=CASE
                             WHEN trim(COALESCE(notes,''))='' THEN ?
                             ELSE trim(notes) || '｜' || ?
                           END,
                           updated_at=datetime('now','localtime')
                     WHERE id=?
                    """,
                    ("screen_rejected", merge_note, merge_note, int(source_candidate_id)),
                )
            event_raw = {
                "action": "merge",
                "merged_into": winner_id,
                "merged_into_name": winner_row["display_name"] or "",
                "note": merge_note,
                "actor": "user",
                "stop_reason": MERGE_STOP_REASON,
                "stop_reason_label": STOP_REASON_LABELS[MERGE_STOP_REASON],
            }
            cursor = conn.execute(
                """INSERT INTO candidate_events(job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table)
                   VALUES (?,?,?,?,?,datetime('now','localtime'),?,?,'api_v1')""",
                (loser_id, loser_row["person_id"], loser_row["job_id"], "resume_review_completed", "stop",
                 merge_note, json.dumps(event_raw, ensure_ascii=False)),
            )
        # 合并停止不回灌寻访学习信号（duplicate 不证明寻访查询失效），
        # 也不触发停止备注寻访调整分析——与 candidate_commit(stop) 的差异点。
        return {
            "ok": True,
            "action": "merge",
            "winner_id": winner_id,
            "loser_id": loser_id,
            "stage": stage,
            "stop_reason": MERGE_STOP_REASON,
            "stop_reason_label": STOP_REASON_LABELS[MERGE_STOP_REASON],
            "business_event_id": cursor.lastrowid,
            "already_applied": False,
        }
