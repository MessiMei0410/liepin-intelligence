"""S8 岗位画像学习：从已抓取人选履历的"具体工作内容"学习岗位真实画像。

口径（本期边界：先给人看、经顾问校准后才接消费；不接策略 step1 / 评估器）：

1. 职责事实抽取器（extract_duty_facts_for_candidate）：
   - 输入单个候选人履历 work/project 段（为空时回退全文本），LLM 抽取职责事实
     （产品/技术方向、工具方法、承担角色、面向客户/场景、典型产出）；
   - 确定性证据校验：每条事实的 evidence 必须是该候选人简历语料的逐字连续子串，
     挂不上整条丢弃；全部丢光则该人不产事实——不计失败，只记 stats；
   - 敏感属性零因子：生成字段命中敏感词表（年龄/性别/婚育/户籍）整条丢弃；
     证据片段含敏感表述同样整条丢弃（证据是必备字段，剥无可剥）。
2. 岗位画像聚合（aggregate_job_profile）：按客户+岗位（job_id）归并多人事实：
   职责分布（方向+人数/占比）、常用工具栈、典型产出、面向客户、来源人数与 as_of；
   归并采用确定性规则（规范化键：去空白/标点/拉丁大小写），不引入第二调 LLM——
   可复算、可审计；示例证据每条最多 3 条，候选人姓名一律遮罩。
3. 顾问纠正通道：feedback 把某条目标记 disputed（不删除），聚合时排除出主列表并
   留痕在 disputed 区；这是画像质量闭环，不构成策略/评估消费。

红线沿用 S6：restricted 不破（本模块不读 restricted 层）；姓名遮罩外发；
LLM 不可用 → LLMError，由调用方决定降级。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from . import candidate_assessment
from .candidate_assessment import scan_sensitive
from .llm import BaseLLM, LLMError
from .workflow import _mask_candidate_name

EXTRACTOR_VERSION = "job-profile-extractor-v1"
PROFILE_SCHEMA_VERSION = "job_profile_v1"
FACTS_SCHEMA_VERSION = "duty_facts_v1"
MIN_SOURCE_COUNT = 3  # 少于 3 份人选履历学不出画像（展示空态）
FACT_LIMIT_PER_PERSON = 8
EXAMPLE_LIMIT = 3
ACTIVE_EVENT_DAYS = 90  # 活跃岗位口径：近 90 天有事件

ITEM_TYPES = ("duty", "tool", "deliverable", "customer")
ROLE_SET = {"打样", "定义", "推广", "支持", "交付", "管理", "研发", "其他"}

# 活跃口径与 asa_core.service 的 _is_stopped 同词表（此处本地实现，避免 asa_core 反向依赖）。
_STOP_STAGE_TOKENS = ("初筛不通过", "停止推进", "已停止", "淘汰", "关闭")
_STOP_STATUSES = {"screen_rejected", "xsaas_review_stop", "rejected", "stopped", "closed"}

# 归并键规范化：去首尾空白、折叠内部空白、去常见标点、拉丁小写（确定性去重归并）。
_NORM_STRIP_RE = re.compile(r"[\s，。、；;：:（）()\[\]【】/／\\\-—_·.,<>《》\"'“”‘’]+")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def norm_key(value: Any) -> str:
    """归并键：同一岗位多人事实的语义级去重归并采用确定性规则（说明见模块 docstring）。"""
    return _NORM_STRIP_RE.sub("", str(value or "")).lower()


def _verbatim_hit(ref: str, corpus: str) -> bool:
    """逐字包含校验（与 S6 candidate_assessment._verbatim_hit 同口径）：仅容忍首尾空白差异。"""
    needle = str(ref or "").strip()
    if len(needle) < 4:  # 太短无法作为可核验证据
        return False
    return needle in corpus


def _source_hash(corpus: str) -> str:
    return hashlib.sha256(corpus.encode("utf-8")).hexdigest()


def _is_stopped(stage: Any, raw_status: Any) -> bool:
    stage_text = str(stage or "")
    return any(token in stage_text for token in _STOP_STAGE_TOKENS) or str(raw_status or "").strip().lower() in _STOP_STATUSES


# ---------------------------------------------------------------------------
# 职责事实抽取器（单人）：LLM 抽取 + 确定性证据校验 + 敏感扫描
# ---------------------------------------------------------------------------

def _extraction_text(resume: dict[str, Any]) -> str:
    """抽取输入：work/project 具体工作内容；两段皆空时回退全文本（同一语料，证据校验不受影响）。"""
    work = str(resume.get("work_text") or "").strip()
    project = str(resume.get("project_text") or "").strip()
    if work or project:
        return "\n".join(part for part in (work, project) if part)
    return str(resume.get("full_text") or resume.get("profile_text") or "").strip()


def validate_facts(raw: Any, *, corpus: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """确定性校验 LLM 返回的职责事实；返回 (保留, 丢弃含原因)，全部留痕。

    丢弃即不进库：证据非逐字 / 证据或生成字段命中敏感词 / 结构非法。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    items = raw.get("facts") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return kept, dropped
    for item in items:
        if len(kept) >= FACT_LIMIT_PER_PERSON:
            dropped.append({"direction": "", "reason": f"超过单人事实上限 {FACT_LIMIT_PER_PERSON} 条，丢弃"})
            continue
        if not isinstance(item, dict):
            dropped.append({"direction": str(item)[:60], "reason": "fact 结构非法"})
            continue
        direction = _clean(item.get("direction"), 60)
        if not direction:
            dropped.append({"direction": "", "reason": "缺产品/技术方向，丢弃"})
            continue
        tools_raw = item.get("tools")
        tools = []
        for tool in (tools_raw if isinstance(tools_raw, list) else []):
            text = _clean(tool, 30)
            if text and text not in tools:
                tools.append(text)
        role = _clean(item.get("role"), 12) or "其他"
        if role not in ROLE_SET:
            role = "其他"
        customer = _clean(item.get("customer"), 40)
        deliverable = _clean(item.get("deliverable"), 60)
        evidence = str(item.get("evidence") or "").strip()
        if not _verbatim_hit(evidence, corpus):
            dropped.append({"direction": direction[:60], "reason": "证据非简历逐字片段，整条丢弃"})
            continue
        sensitive_hits = scan_sensitive([direction, *tools, role, customer, deliverable])
        if sensitive_hits:
            dropped.append({"direction": direction[:60], "reason": "生成字段命中敏感属性词表，整条丢弃（零因子）"})
            continue
        if scan_sensitive([evidence]):
            dropped.append({"direction": direction[:60], "reason": "证据片段含敏感属性表述，整条丢弃"})
            continue
        kept.append(
            {
                "direction": direction,
                "tools": tools,
                "role": role,
                "customer": customer,
                "deliverable": deliverable,
                "evidence": evidence[:200],
            }
        )
    return kept, dropped


def build_duty_payload(candidate: dict[str, Any], job: dict[str, Any], resume: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "岗位画像学习：从单个候选人履历的具体工作内容抽取职责事实",
        "job": {"client": str(job.get("client") or ""), "title": str(job.get("title") or "")},
        "candidate": {
            "current_company": str(candidate.get("current_company") or ""),
            "current_title": str(candidate.get("current_title") or ""),
        },
        "resume_work_text": str(resume.get("work_text") or "")[:4000],
        "resume_project_text": str(resume.get("project_text") or "")[:3000],
        "resume_extraction_text": _extraction_text(resume)[:6000],
    }


def extract_duty_facts_for_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: int,
    llm: BaseLLM,
    force: bool = False,
    as_of: str = "",
) -> dict[str, Any]:
    """抽取单个 job_candidate 的职责事实并 upsert job_profile_facts（幂等：同人同岗一行）。

    返回 {candidate_id, job_id, fact_count, kept, dropped, skipped, reason}。
    LookupError：人选不存在；LLMError：模型不可用或输出非法（调用方决定降级）。
    履历不足 / 证据全丢 → fact_count=0，不计失败，只记 stats。
    """
    candidate = candidate_assessment.load_candidate_resume(conn, int(candidate_id))
    if candidate is None:
        raise LookupError(f"人选不存在：{candidate_id}")
    job_id = int(candidate.get("job_id") or 0)
    person_id = int(candidate.get("person_id") or 0)
    resume = candidate.get("resume") if isinstance(candidate.get("resume"), dict) else {}
    corpus = candidate_assessment.build_corpus(resume)
    source_hash = _source_hash(corpus)
    as_of = as_of or _now()

    existing = conn.execute(
        "SELECT id,source_hash,fact_count FROM job_profile_facts WHERE job_id=? AND job_candidate_id=?",
        (job_id, int(candidate_id)),
    ).fetchone()
    if existing and not force and existing["source_hash"] == source_hash:
        return {
            "candidate_id": int(candidate_id),
            "job_id": job_id,
            "person_id": person_id,
            "fact_count": int(existing["fact_count"] or 0),
            "kept": int(existing["fact_count"] or 0),
            "dropped": 0,
            "skipped": True,
            "reason": "履历未变化，沿用已抽取事实",
        }

    stats: dict[str, Any] = {"corpus_chars": len(corpus), "dropped_detail": []}
    kept: list[dict[str, Any]] = []
    reason = ""
    if len(corpus.strip()) < 50:
        reason = "履历语料不足，未抽取"
    else:
        job = conn.execute(
            "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise LookupError(f"岗位不存在：{job_id}")
        raw = llm.extract_duty_facts(build_duty_payload(candidate, dict(job), resume))
        if not isinstance(raw, dict) or not isinstance(raw.get("facts"), list):
            raise LLMError("岗位画像模型未返回有效结构")
        kept, dropped = validate_facts(raw, corpus=corpus)
        stats["dropped_detail"] = dropped
        if not kept:
            reason = "证据校验后无可入库事实" if dropped else "履历中未抽取到职责事实"

    stats["kept"] = len(kept)
    stats["dropped"] = len(stats["dropped_detail"])
    stats["reason"] = reason
    doc = {"schema_version": FACTS_SCHEMA_VERSION, "facts": kept, "stats": stats, "as_of": as_of}
    if existing:
        conn.execute(
            """
            UPDATE job_profile_facts SET person_id=?,facts_json=?,fact_count=?,source_hash=?,
                model=?,extractor_version=?,stats_json=?,as_of=?,updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (
                person_id, _dumps(doc), len(kept), source_hash, llm.model, EXTRACTOR_VERSION,
                _dumps(stats), as_of, int(existing["id"]),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO job_profile_facts
            (job_id,job_candidate_id,person_id,facts_json,fact_count,source_hash,model,extractor_version,stats_json,as_of)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id, int(candidate_id), person_id, _dumps(doc), len(kept), source_hash,
                llm.model, EXTRACTOR_VERSION, _dumps(stats), as_of,
            ),
        )
    return {
        "candidate_id": int(candidate_id),
        "job_id": job_id,
        "person_id": person_id,
        "fact_count": len(kept),
        "kept": len(kept),
        "dropped": len(stats["dropped_detail"]),
        "skipped": False,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 岗位画像聚合：多人事实归并 → 职责分布 / 工具栈 / 典型产出 / 面向客户
# ---------------------------------------------------------------------------

def _load_disputed(conn: sqlite3.Connection, job_id: int) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        "SELECT item_type,item_key,item_label,note,updated_at FROM job_profile_feedback WHERE job_id=? AND status='disputed'",
        (int(job_id),),
    ).fetchall()
    return {(str(r["item_type"]), str(r["item_key"])): dict(r) for r in rows}


def _group_items(
    entries: list[tuple[str, str, str, str, str]],
    *,
    persons_with_facts: int,
) -> dict[str, dict[str, Any]]:
    """entries=(norm_key, label, person_key, masked_name, evidence) → 按归并键分组，人数按 person_key 去重。"""
    groups: dict[str, dict[str, Any]] = {}
    for key, label, person_key, masked, evidence in entries:
        if not key:
            continue
        group = groups.setdefault(key, {"labels": {}, "persons": {}})
        group["labels"][label] = group["labels"].get(label, 0) + 1
        bucket = group["persons"].setdefault(person_key, {"candidate": masked, "evidence": evidence})
        if not bucket.get("evidence") and evidence:
            bucket["evidence"] = evidence
    result: dict[str, dict[str, Any]] = {}
    for key, group in groups.items():
        label = sorted(group["labels"].items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
        persons = [group["persons"][pk] for pk in sorted(group["persons"])]
        examples = [
            {"candidate": str(person["candidate"]), "evidence": str(person.get("evidence") or "")[:160]}
            for person in persons
            if str(person.get("evidence") or "").strip()
        ][:EXAMPLE_LIMIT]
        count = len(persons)
        result[key] = {
            "key": key,
            "label": label,
            "count": count,
            "ratio": round(count / persons_with_facts, 3) if persons_with_facts else 0,
            "examples": examples,
        }
    return result


def aggregate_job_profile(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    as_of: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    """按客户+岗位聚合岗位画像；persist=True 时 upsert job_profile_insights 并写岗位时间线事件。

    幂等：同岗一行，重跑刷新 as_of / version+1，不重复计人（人数来自 job_profile_facts 去重）。
    disputed 条目排除出主列表并留痕在 disputed 区（聚合时排除=降权到底，不删除数据）。
    LookupError：岗位不存在。
    """
    job = conn.execute(
        "SELECT j.id,j.title,c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
        (int(job_id),),
    ).fetchone()
    if job is None:
        raise LookupError(f"岗位不存在：{job_id}")
    as_of = as_of or _now()

    rows = conn.execute(
        """
        SELECT f.job_candidate_id,f.person_id,f.facts_json,p.display_name
          FROM job_profile_facts f
          LEFT JOIN people p ON p.id=f.person_id
         WHERE f.job_id=? ORDER BY f.job_candidate_id
        """,
        (int(job_id),),
    ).fetchall()

    duty_entries: list[tuple[str, str, str, str, str]] = []
    tool_entries: list[tuple[str, str, str, str, str]] = []
    deliverable_entries: list[tuple[str, str, str, str, str]] = []
    customer_entries: list[tuple[str, str, str, str, str]] = []
    persons_with_facts_set: set[str] = set()
    facts_kept_total = 0
    facts_dropped_total = 0
    model = ""

    for row in rows:
        doc = _loads(row["facts_json"], {})
        facts = doc.get("facts") if isinstance(doc, dict) else []
        if not isinstance(facts, list):
            continue
        stats = doc.get("stats") if isinstance(doc, dict) else {}
        facts_dropped_total += int((stats or {}).get("dropped") or 0)
        person_id = int(row["person_id"] or 0)
        person_key = f"person:{person_id}" if person_id else f"jc:{int(row['job_candidate_id'])}"
        masked = _mask_candidate_name(row["display_name"]) or f"人选{int(row['job_candidate_id'])}"
        person_facts = [fact for fact in facts if isinstance(fact, dict) and str(fact.get("direction") or "").strip()]
        if not person_facts:
            continue
        persons_with_facts_set.add(person_key)
        facts_kept_total += len(person_facts)
        for fact in person_facts:
            evidence = str(fact.get("evidence") or "")
            direction = str(fact.get("direction") or "")
            duty_entries.append((norm_key(direction), direction, person_key, masked, evidence))
            for tool in fact.get("tools") or []:
                tool_text = str(tool or "").strip()
                if tool_text:
                    tool_entries.append((norm_key(tool_text), tool_text, person_key, masked, evidence))
            deliverable = str(fact.get("deliverable") or "").strip()
            if deliverable:
                deliverable_entries.append((norm_key(deliverable), deliverable, person_key, masked, evidence))
            customer = str(fact.get("customer") or "").strip()
            if customer:
                customer_entries.append((norm_key(customer), customer, person_key, masked, evidence))
        if not model:
            model = str(doc.get("model") or "")

    persons_with_facts = len(persons_with_facts_set)
    disputed = _load_disputed(conn, int(job_id))

    def _build(entries: list[tuple[str, str, str, str, str]], item_type: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        grouped = _group_items(entries, persons_with_facts=persons_with_facts)
        active: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for key in sorted(grouped, key=lambda k: (-grouped[k]["count"], grouped[k]["label"])):
            item = grouped[key]
            mark = disputed.get((item_type, key))
            if mark:
                blocked.append(
                    {
                        "item_type": item_type,
                        "key": key,
                        "label": item["label"],
                        "count": item["count"],
                        "note": str(mark.get("note") or ""),
                        "disputed_at": str(mark.get("updated_at") or ""),
                    }
                )
            else:
                active.append(item)
        return active, blocked

    duties, disputed_duties = _build(duty_entries, "duty")
    tools, disputed_tools = _build(tool_entries, "tool")
    deliverables, disputed_deliverables = _build(deliverable_entries, "deliverable")
    customers, disputed_customers = _build(customer_entries, "customer")
    disputed_items = disputed_duties + disputed_tools + disputed_deliverables + disputed_customers

    status = "ready" if persons_with_facts >= MIN_SOURCE_COUNT else "insufficient"
    insight = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "job_id": int(job_id),
        "client": str(job["client"] or ""),
        "job_title": str(job["title"] or ""),
        "status": status,
        "source_count": persons_with_facts,
        "min_source_count": MIN_SOURCE_COUNT,
        "as_of": as_of,
        "duties": duties,
        "tools": tools,
        "deliverables": deliverables,
        "customers": customers,
        "disputed": disputed_items,
        "stats": {
            "persons_processed": len(rows),
            "persons_with_facts": persons_with_facts,
            "persons_no_facts": len(rows) - persons_with_facts,
            "facts_kept": facts_kept_total,
            "facts_dropped": facts_dropped_total,
            "disputed_count": len(disputed_items),
            "feedback_count": len(disputed),
        },
    }
    if persist:
        existing = conn.execute(
            "SELECT id,version FROM job_profile_insights WHERE job_id=?", (int(job_id),)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE job_profile_insights SET client=?,job_title=?,status=?,source_count=?,insight_json=?,
                    model=?,version=?,as_of=?,updated_at=datetime('now','localtime')
                WHERE id=?
                """,
                (
                    insight["client"], insight["job_title"], status, persons_with_facts, _dumps(insight),
                    model, int(existing["version"] or 1) + 1, as_of, int(existing["id"]),
                ),
            )
            version = int(existing["version"] or 1) + 1
        else:
            conn.execute(
                """
                INSERT INTO job_profile_insights
                (job_id,client,job_title,status,source_count,insight_json,model,version,as_of)
                VALUES (?,?,?,?,?,?,?,1,?)
                """,
                (
                    int(job_id), insight["client"], insight["job_title"], status, persons_with_facts,
                    _dumps(insight), model, as_of,
                ),
            )
            version = 1
        insight["version"] = version
        conn.execute(
            """
            INSERT INTO candidate_events
            (job_candidate_id,person_id,job_id,event_type,event_status,event_time,summary,raw_json,source_table,source_id)
            VALUES (NULL,NULL,?,?,?,datetime('now','localtime'),?,?,'job_profile_insights',?)
            """,
            (
                int(job_id),
                "job_profile_generated",
                "completed",
                (
                    f"更新岗位画像（这个岗位实际在干什么）：来源 {persons_with_facts} 份人选履历，"
                    f"职责方向 {len(duties)} 个、常用工具 {len(tools)} 项、典型产出 {len(deliverables)} 项；"
                    f"事实入库 {facts_kept_total} 条（丢弃 {facts_dropped_total} 条），"
                    f"顾问已标记 {len(disputed_items)} 条不对。"
                ),
                _dumps({"job_id": int(job_id), "status": status, "source_count": persons_with_facts, "version": version}),
                str(job_id),
            ),
        )
    return insight


# ---------------------------------------------------------------------------
# 活跃岗位口径（回填范围）
# ---------------------------------------------------------------------------

def list_active_jobs(conn: sqlite3.Connection, *, job_id: int | None = None, days: int = ACTIVE_EVENT_DAYS) -> list[int]:
    """活跃岗位：有待处理人选（未停止的 job_candidates）或近 N 天有事件的岗位。"""
    if job_id:
        exists = conn.execute("SELECT id FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        return [int(job_id)] if exists else []
    rows = conn.execute("SELECT id,job_id,clean_stage,raw_status FROM job_candidates").fetchall()
    active: set[int] = set()
    all_jobs: set[int] = set()
    for row in rows:
        jid = int(row["job_id"] or 0)
        if not jid:
            continue
        all_jobs.add(jid)
        if not _is_stopped(row["clean_stage"], row["raw_status"]):
            active.add(jid)
    recent = conn.execute(
        """
        SELECT DISTINCT job_id FROM candidate_events
         WHERE job_id IS NOT NULL AND COALESCE(event_time,'') >= datetime('now','localtime', ?)
        """,
        (f"-{int(days)} days",),
    ).fetchall()
    for row in recent:
        jid = int(row["job_id"] or 0)
        if jid:
            active.add(jid)
            all_jobs.add(jid)
    return sorted(active & all_jobs)


# ---------------------------------------------------------------------------
# 顾问纠正通道（disputed 标记；不删除，留痕 + 统计）
# ---------------------------------------------------------------------------

def submit_feedback(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    item_type: str,
    item_key: str,
    item_label: str = "",
    note: str = "",
    actor: str = "consultant",
) -> dict[str, Any]:
    """把画像某条目标记为 disputed（幂等：同 job+type+key 一行，重复标记更新备注不重复建行）。

    ValueError：item_type 非法 / item_key 为空；LookupError：岗位不存在。
    标记后立即重算画像（确定性聚合，无 LLM），disputed 条目从主列表排除并留痕。
    """
    if item_type not in ITEM_TYPES:
        raise ValueError(f"item_type 仅支持 {'/'.join(ITEM_TYPES)}")
    key = norm_key(item_key)
    if not key:
        raise ValueError("item_key 不能为空")
    job = conn.execute("SELECT id FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if job is None:
        raise LookupError(f"岗位不存在：{job_id}")

    existing = conn.execute(
        "SELECT id,note FROM job_profile_feedback WHERE job_id=? AND item_type=? AND item_key=?",
        (int(job_id), item_type, key),
    ).fetchone()
    already = existing is not None
    if existing:
        conn.execute(
            """
            UPDATE job_profile_feedback SET item_label=?,note=?,actor=?,status='disputed',
                updated_at=datetime('now','localtime')
            WHERE id=?
            """,
            (_clean(item_label, 60), _clean(note, 200), _clean(actor, 40) or "consultant", int(existing["id"])),
        )
    else:
        conn.execute(
            """
            INSERT INTO job_profile_feedback (job_id,item_type,item_key,item_label,status,note,actor)
            VALUES (?,?,?,?,'disputed',?,?)
            """,
            (int(job_id), item_type, key, _clean(item_label, 60), _clean(note, 200), _clean(actor, 40) or "consultant"),
        )
    insight = aggregate_job_profile(conn, job_id=int(job_id), persist=True)
    return {
        "ok": True,
        "job_id": int(job_id),
        "item_type": item_type,
        "item_key": key,
        "status": "disputed",
        "already_disputed": already,
        "insight": insight,
    }
