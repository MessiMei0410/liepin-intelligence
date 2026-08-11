"""Handler extracted from service.py — Copilot message processing, intent confirmation, session management.

All functions receive 'self' (AgentService instance) as first parameter.
"""

from __future__ import annotations
import hashlib, json, re, textwrap, secrets, threading, sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ._shared import (
    _dumps, _loads, _row, _is_short_ack, _contains_any, _latest_event, _table_exists, _table_columns,
    DECISION_LABELS, SOURCING_SIGNAL_WEIGHTS, SOURCING_SIGNAL_LABELS,
)
from .llm import BaseLLM, LLMError
from .workflow import BUSINESS_OUTCOME_LABELS, classify_business_outcome, sourcing_target_stats
from .privacy import sanitize_payload
from .policy import is_stopped
from .context import build_candidate_context
from .job_status import job_status_intake_allowed
from .copilot_tools import generate_proactive_suggestions
from . import strategy_v2
from .native_attachments import attachment_read_requested, image_analysis_requested
from .capability_runtime import ZERO_RESULT_ATTRIBUTION_LABELS
from .turn_decision import build_turn_decision
from .conversation_state import (
    TERMINAL_WORKFLOW_STATUSES,
    build_context_state,
    deterministic_context_summary,
    enrich_turn_understanding,
)


_EXPANDED_EXPLANATION_MARKERS = (
    "详细", "展开", "完整依据", "逐条", "具体解释", "解释下", "为什么", "为何", "怎么判断", "从哪些点",
    "深入", "全面", "说清楚", "好好分析", "具体说",
)

_ASSESSMENT_CONTEXT_FIELDS = (
    "fit_score", "fit_level", "recommendation", "recommendation_label",
    "confidence", "evidence_coverage", "criteria", "strengths", "gaps",
    "risks", "verification_questions", "next_action", "outreach_angle", "citations",
)

_CRITERION_STATUS_LABELS = {
    "met": "已满足", "partial": "部分匹配", "not_met": "未满足", "unknown": "证据不足",
}


def _copilot_response_detail(message: str) -> str:
    return "expanded" if any(marker in str(message or "") for marker in _EXPANDED_EXPLANATION_MARKERS) else "standard"


def _copilot_assessment_context(assessment: dict[str, Any]) -> dict[str, Any]:
    return {key: assessment.get(key) for key in _ASSESSMENT_CONTEXT_FIELDS}


def _candidate_evidence_question(message: str) -> bool:
    text = " ".join(str(message or "").split())
    return any(
        token in text
        for token in (
            "哪些方面", "匹配", "适合", "符合", "为什么", "为何", "怎么判断",
            "从哪些点", "三次电源", "证据链", "详细解释", "具体解释",
        )
    )


def _copilot_list_value(*values: Any) -> list[str]:
    for value in values:
        parsed = value if isinstance(value, list) else _loads(value, None)
        if isinstance(parsed, list):
            result = [" ".join(str(item or "").split()) for item in parsed]
            result = [item for item in result if item]
            if result:
                return list(dict.fromkeys(result))
        text = " ".join(str(value or "").split())
        if text:
            result = [item.strip() for item in re.split(r"[\n；;]+", text) if item.strip()]
            if result:
                return list(dict.fromkeys(result))
    return []


def _copilot_job_evidence(self, job_id: int) -> dict[str, Any]:
    try:
        job = self.capability_runtime._job({"type": "job", "id": int(job_id)})
    except (sqlite3.Error, ValueError):
        return {}
    profile = job.get("profile") if isinstance(job.get("profile"), dict) else {}
    return {
        "client": job.get("client"),
        "job": job.get("title"),
        "location": job.get("position_location") or job.get("location"),
        "status": job.get("status"),
        "summary": profile.get("jd_analysis_summary") or job.get("summary"),
        "responsibilities": _copilot_list_value(job.get("responsibilities")),
        "requirements": _copilot_list_value(job.get("requirements")),
        "education_requirement": profile.get("education_requirement") or job.get("education"),
        "experience_requirement": profile.get("experience_requirement") or job.get("experience"),
        "hard_requirements": _copilot_list_value(profile.get("hard_requirements_json"), job.get("hard_requirements")),
        "ability_keywords": _copilot_list_value(profile.get("ability_keywords_json"), job.get("ability_keywords")),
        "soft_preferences": _copilot_list_value(profile.get("soft_preferences_json")),
        "target_companies": _copilot_list_value(profile.get("target_companies_json"), job.get("target_companies")),
        "exclusions": _copilot_list_value(profile.get("exclusion_tags_json"), job.get("exclusions")),
        "risk_points": _copilot_list_value(profile.get("risk_points_json")),
        "pitch_points": _copilot_list_value(profile.get("pitch_points_json")),
    }


def _format_candidate_evidence_answer(assessment: dict[str, Any], *, gaps_only: bool = False) -> str:
    gaps = [str(item).strip() for item in (assessment.get("gaps") or []) if str(item).strip()]
    questions = [str(item).strip() for item in (assessment.get("verification_questions") or []) if str(item).strip()]
    if gaps_only:
        lines = ["**结论**", assessment.get("next_action") or "当前判断仍有证据缺口。"]
        lines.extend(["", "**证据缺口**", *[f"- {item}" for item in gaps or ["当前没有额外证据缺口。"]]])
        lines.extend(["", "**需要核验**", *[f"{index}. {item}" for index, item in enumerate(questions or ["当前没有额外核验项。"], 1)]])
        return "\n".join(lines)

    criteria = assessment.get("criteria") if isinstance(assessment.get("criteria"), dict) else {}
    items: list[tuple[str, dict[str, Any]]] = []
    for group in ("hard_requirements", "core_abilities", "soft_preferences"):
        for item in (criteria.get(group) or []):
            if not isinstance(item, dict):
                continue
            criterion = str(item.get("criterion") or "").strip()
            # 评估器可能同时返回一个把整段 JD 拼在一起的总门槛和拆分门槛；
            # 总门槛通常只有学历证据，直接展示会造成“整条硬门槛已满足”的误读。
            if len(criterion) > 80 and group == "hard_requirements":
                continue
            items.append((group, item))
    status_rank = {"met": 0, "partial": 1, "unknown": 2, "not_met": 3}
    items.sort(key=lambda pair: (
        status_rank.get(str(pair[1].get("status") or "unknown"), 2),
        0 if pair[0] == "hard_requirements" else 1 if pair[0] == "core_abilities" else 2,
    ))
    matched = [item for _, item in items if str(item.get("status") or "unknown") == "met"]
    uncertain = [item for _, item in items if str(item.get("status") or "unknown") != "met"]
    lines = [
        "**结论**",
        f"当前评估为 {assessment.get('fit_level') or '待判断'}"
        + (f"（{assessment.get('fit_score')} 分）" if assessment.get("fit_score") is not None else "")
        + "。以下判断只基于现有证据，不代表未写明的能力已经满足。",
        "",
        "**逐条证据链**",
        "",
        "**直接匹配**",
    ]
    if not matched:
        lines.append("- 当前评估没有足够的直接匹配证据。")
    for index, item in enumerate(matched[:8], 1):
        criterion = str(item.get("criterion") or "判断项").strip()
        status = _CRITERION_STATUS_LABELS.get(str(item.get("status") or "unknown"), "证据不足")
        evidence = [str(value).strip() for value in (item.get("evidence") or []) if str(value).strip()]
        reason = str(item.get("reason") or "").strip()
        lines.append(f"{index}. **{criterion}｜{status}**")
        lines.append(f"   - 直接证据：{'；'.join(evidence) if evidence else '现有资料未提供直接证据'}")
        lines.append(f"   - 判断说明：{reason or '需要结合补充材料再判断'}")
    lines.extend(["", "**部分匹配或证据不足**"])
    if not uncertain:
        lines.append("- 当前没有额外的部分匹配或证据不足项。")
    for index, item in enumerate(uncertain[:10], 1):
        criterion = str(item.get("criterion") or "判断项").strip()
        status = _CRITERION_STATUS_LABELS.get(str(item.get("status") or "unknown"), "证据不足")
        evidence = [str(value).strip() for value in (item.get("evidence") or []) if str(value).strip()]
        reason = str(item.get("reason") or "").strip()
        lines.append(f"{index}. **{criterion}｜{status}**")
        lines.append(f"   - 已知材料：{'；'.join(evidence) if evidence else '简历未提供直接证据'}")
        lines.append(f"   - 不能直接下结论的原因：{reason or '需要补充项目细节'}")
    lines.extend(["", "**判断边界**", *[f"- {item}" for item in gaps or ["当前评估未记录额外缺口。"]]])
    lines.extend(["", "**需要核验**", *[f"{index}. {item}" for index, item in enumerate(questions or ["当前没有额外核验项。"], 1)]])
    lines.extend(["", "**下一步**", str(assessment.get("next_action") or "先补齐关键证据，再由顾问决定是否推进。")])
    return "\n".join(lines)


def _normalize_copilot_context(self, context: dict[str, Any]) -> dict[str, Any]:
    context_type = str(context.get("type") or "global").strip().lower()
    if context_type not in {"global", "page", "job", "candidate", "queue", "workflow"}:
        context_type = "global"
    context_id = None
    if context_type in {"job", "candidate"}:
        try:
            context_id = int(context.get("id") or 0) or None
        except (TypeError, ValueError):
            context_id = None
    elif context_type == "workflow":
        candidate = str(context.get("id") or "").strip()
        context_id = candidate if re.fullmatch(r"workflow_[0-9a-zA-Z]+", candidate) else None
        if context_id is None:
            context_type = "global"
    page = str(context.get("page") or "").strip().lower()
    page = {"jobs": "positions", "progress": "flow", "radar": "overview"}.get(page, page)
    if page not in {"overview", "positions", "flow", "candidates"}:
        page = ""
    raw_filters = context.get("filters") if isinstance(context.get("filters"), dict) else {}
    filters = {
        key: " ".join(str(raw_filters.get(key) or "").split())[:120]
        for key in ("queue", "client", "job", "search", "view")
        if raw_filters.get(key) not in (None, "")
    }
    return {"type": context_type, "id": context_id, "page": page, "filters": filters}


def _floating_bridge_evidence(self, context: dict[str, Any]) -> dict[str, Any]:
    bridge = context.get("bridge") if isinstance(context.get("bridge"), dict) else {}
    if not bridge:
        return {}
    surface = str(bridge.get("surface") or "").strip().lower()
    if surface not in {"liepin", "xsaas", "a_system", "native"}:
        return {}

    def compact(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit]

    if surface == "native":
        frontmost_app = bridge.get("frontmost_app") if isinstance(bridge.get("frontmost_app"), dict) else {}
        window = bridge.get("window") if isinstance(bridge.get("window"), dict) else {}
        wechat = bridge.get("wechat") if isinstance(bridge.get("wechat"), dict) else {}
        blocks = wechat.get("text_blocks") if isinstance(wechat.get("text_blocks"), list) else []
        visible_text = (
            wechat.get("visible_text_clean")
            or wechat.get("combined_text")
            or "\n".join(str(item) for item in blocks)
        )
        message_blocks: list[dict[str, Any]] = []
        for item in (wechat.get("message_blocks") or [])[:40]:
            if not isinstance(item, dict):
                continue
            text = compact(item.get("text"), 500)
            if not text:
                continue
            message_blocks.append({
                "text": text,
                "side": compact(item.get("side"), 20),
                "x": item.get("x"),
                "y": item.get("y"),
            })
        ocr_quality = wechat.get("ocr_quality") if isinstance(wechat.get("ocr_quality"), dict) else {}
        raw_image_analysis = wechat.get("image_analysis") if isinstance(wechat.get("image_analysis"), dict) else {}
        classifications: list[dict[str, Any]] = []
        for item in (raw_image_analysis.get("classifications") or [])[:12]:
            if not isinstance(item, dict):
                continue
            try:
                confidence = round(float(item.get("confidence") or 0), 4)
            except (TypeError, ValueError):
                confidence = 0.0
            label = compact(item.get("label"), 100)
            if label:
                classifications.append({"label": label, "confidence": confidence})
        image_analysis = {
            "source": compact(raw_image_analysis.get("source"), 80),
            "ocr_text": compact(raw_image_analysis.get("ocr_text"), 12000),
            "classifications": classifications,
        }
        image_analysis = {
            key: value for key, value in image_analysis.items()
            if value not in (None, "", [])
        }
        try:
            text_block_count = max(0, int(wechat.get("text_block_count") or len(blocks)))
        except (TypeError, ValueError):
            text_block_count = len(blocks)
        evidence = {
            "source": "native",
            "page_type": "wechat_visible_window" if wechat else "native_window",
            "label": "微信当前可见窗口" if wechat else "当前 macOS 窗口",
            "app_name": compact(frontmost_app.get("name"), 80),
            "bundle_id": compact(frontmost_app.get("bundle_id"), 160),
            "window_title": compact(window.get("title") or wechat.get("window_title"), 180),
            "capture_mode": compact(wechat.get("capture_mode"), 40),
            "visible_text": compact(visible_text, 12000),
            "message_blocks": message_blocks,
            "ocr_quality": ocr_quality,
            "text_block_count": text_block_count,
            "bridge_status": compact(wechat.get("status") or bridge.get("status"), 180),
            "evidence_scope": "current_visible_window_ocr" if wechat else "window_metadata_only",
            "attachment_content_available": False,
            "visual_understanding_available": bool(image_analysis),
            "image_analysis": image_analysis,
            "untrusted_screen_content": True,
        }
        return {key: value for key, value in evidence.items() if value not in (None, "", [])}

    candidate = bridge.get("candidate") if isinstance(bridge.get("candidate"), dict) else {}
    bridge_profile = (
        candidate.get("profile_summary")
        or bridge.get("candidate_profile_text")
        or bridge.get("profile_summary")
        or bridge.get("page_text")
        or ""
    )
    job_candidate_id = bridge.get("job_candidate_id")
    if not bridge_profile and isinstance(job_candidate_id, int) and job_candidate_id > 0:
        try:
            candidate_context = build_candidate_context(self.db_path, job_candidate_id)
            bridge_profile = str((candidate_context.get("resume") or {}).get("full_text") or "")
        except Exception:
            bridge_profile = ""
    evidence = {
        "source": surface,
        "page_type": compact(bridge.get("page_type"), 40),
        "candidate_name": compact(candidate.get("name") or bridge.get("candidate_name"), 40),
        "company": compact(candidate.get("company") or bridge.get("company"), 80),
        "title": compact(candidate.get("title") or bridge.get("candidate_title"), 120),
        "profile_summary": compact(bridge_profile, 4200),
        "bridge_status": compact(bridge.get("status"), 160),
        "source_url": compact(bridge.get("source_url") or bridge.get("url"), 260),
    }
    return {key: value for key, value in evidence.items() if value}


def _uploaded_attachment_evidence(self, context: dict[str, Any], session_id: str) -> dict[str, Any]:
    raw_items = context.get("uploaded_attachments") if isinstance(context.get("uploaded_attachments"), list) else []
    items: list[dict[str, Any]] = []
    requested: list[tuple[str, str]] = []
    for raw in raw_items[:3]:
        if not isinstance(raw, dict):
            continue
        attachment_id = " ".join(str(raw.get("attachment_id") or "").split())[:80]
        access_token = str(raw.get("access_token") or "")[:200]
        if not attachment_id or not access_token:
            continue
        requested.append((attachment_id, hashlib.sha256(access_token.encode("utf-8")).hexdigest()))

    conn = self._connect()
    try:
        rows = []
        if requested:
            for attachment_id, token_hash in requested:
                row = conn.execute(
                    """
                    SELECT * FROM agent_copilot_attachments
                    WHERE attachment_id=? AND access_token_hash=?
                      AND expires_at>datetime('now','localtime')
                      AND (session_id='' OR session_id=?)
                    """,
                    (attachment_id, token_hash, session_id),
                ).fetchone()
                if row:
                    rows.append(row)
        else:
            rows = conn.execute(
                """
                SELECT * FROM agent_copilot_attachments
                WHERE session_id=? AND expires_at>datetime('now','localtime')
                ORDER BY created_at DESC LIMIT 2
                """,
                (session_id,),
            ).fetchall()
        for row in rows:
            extracted_text = str(row["extracted_text"] or "")[:18000]
            file_name = Path(str(row["file_name"] or "")).name[:180]
            content_available = bool(extracted_text)
            items.append(
                {
                    "attachment_id": row["attachment_id"],
                    "file_name": file_name,
                    "file_type": row["file_type"],
                    "mime_type": row["mime_type"],
                    "size_bytes": int(row["size_bytes"] or 0),
                    "content_available": content_available,
                    "extracted_text": extracted_text,
                    "truncated": bool(row["truncated"]),
                    "is_image": False,
                    "status": row["status"],
                    "untrusted_document_content": True,
                }
            )
            conn.execute(
                """
                UPDATE agent_copilot_attachments
                SET session_id=CASE WHEN session_id='' THEN ? ELSE session_id END,
                    last_accessed_at=datetime('now','localtime')
                WHERE attachment_id=?
                """,
                (session_id, row["attachment_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    if not items:
        return {}
    return {
        "scope": "user_selected_local_upload",
        "items": items,
        "content_available": any(item["content_available"] for item in items),
        "local_paths_exposed": False,
    }


def _persistable_attachment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("uploaded_attachment_evidence") if isinstance(payload.get("uploaded_attachment_evidence"), dict) else {}
    if not evidence:
        return payload
    stored_items = []
    for item in (evidence.get("items") or [])[:3]:
        if not isinstance(item, dict):
            continue
        stored_items.append({
            key: item.get(key)
            for key in (
                "attachment_id", "file_name", "file_type", "mime_type", "size_bytes",
                "content_available", "truncated", "is_image", "status",
            )
        })
    return {
        **payload,
        "uploaded_attachment_evidence": {
            "scope": evidence.get("scope"),
            "items": stored_items,
            "content_available": bool(evidence.get("content_available")),
            "local_paths_exposed": False,
        },
    }


def _client_aliases(name: str) -> list[str]:
    normalized = " ".join(str(name or "").split())
    aliases = [normalized] if normalized else []
    core = normalized
    for suffix in ("有限责任公司", "股份有限公司", "有限公司", "科技", "电子", "集团", "股份"):
        if core.endswith(suffix) and len(core) - len(suffix) >= 2:
            core = core[:-len(suffix)]
            if core not in aliases:
                aliases.append(core)
    return aliases


def _mentioned_jobs_for_copilot(self, message: str, limit: int = 5) -> list[dict[str, Any]]:
    cleaned = " ".join(str(message or "").split())
    if not cleaned:
        return []
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,
                   COALESCE(m.priority,'') AS priority
            FROM jobs j
            JOIN clients c ON c.id=j.client_id
            LEFT JOIN job_pipeline_metrics m ON m.job_id=j.id
            WHERE COALESCE(j.status,'open')!='closed'
            ORDER BY CASE WHEN COALESCE(m.priority,'') LIKE 'P0%' THEN 0 ELSE 1 END, j.id DESC
            LIMIT 300
            """
        ).fetchall()
    except sqlite3.Error:
        rows = conn.execute(
            """
            SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,'' AS priority
            FROM jobs j JOIN clients c ON c.id=j.client_id
            WHERE COALESCE(j.status,'open')!='closed'
            ORDER BY j.id DESC LIMIT 300
            """
        ).fetchall()
    finally:
        conn.close()
    # 岗位状态过滤（2026-07-22）：黑名单状态（待启动/暂停/只读快照/已拆分等）
    # 的岗位不作为可推荐/可定位结果返回；名单见 a_system_agent/job_status.py。
    rows = [row for row in rows if job_status_intake_allowed(row["status"])]
    scored: list[tuple[int, bool, dict[str, Any]]] = []
    for row in rows:
        item = _row(row)
        client = str(item.get("client") or "")
        job = str(item.get("job") or "")
        score = 0
        job_id = int(item.get("id") or 0)
        specific_evidence = False
        client_matched = any(alias in cleaned for alias in _client_aliases(client))
        id_matched = bool(job_id and re.search(rf"(?:#\s*|岗位\s*#?\s*){job_id}(?!\d)", cleaned, re.I))
        title_matched = bool(job and job in cleaned)
        if id_matched:
            score += 100
            specific_evidence = True
        if client_matched:
            score += 12
        # 没有客户名、也不是精确岗位名/ID 时，不拿岗位标题里的零散词去碰全文，
        # 否则 JD/附件长文本里很容易误命中“分析”“数据”等通用词，导致无关岗位卡片。
        if not (client_matched or id_matched or title_matched):
            continue
        for token in re.split(r"[\s/（）()、,，｜|]+", job):
            token = token.strip()
            if len(token) >= 2 and token in cleaned:
                score += min(8, len(token))
                specific_evidence = True
        # Match the business core of a title, not only its full formal wording.
        # E.g. 自动化软件岗位 -> 自动化软件高级工程师, 技术市场岗 -> 技术市场经理/总监.
        core_parts = re.split(r"[\s/（）()、,，｜|]+", job)
        role_words = ("高级", "资深", "初级", "中级", "首席", "工程师", "经理", "总监", "专家", "主管", "岗位", "职位")
        for part in core_parts:
            core = part.strip()
            for role_word in role_words:
                core = core.replace(role_word, "")
            if len(core) >= 2 and core in cleaned:
                score += min(18, len(core) * 3)
                specific_evidence = True
            # A two-character domain keyword is enough when the client is explicit
            # and only one open job contains it (软件/机械/电气 etc.).
            for size in range(min(6, len(core)), 1, -1):
                match = next((core[i:i + size] for i in range(len(core) - size + 1) if core[i:i + size] in cleaned), "")
                if match:
                    score += min(12, size * 2)
                    specific_evidence = True
                    break
        if score:
            item["summary"] = str(item.get("summary") or "")[:900]
            scored.append((score, specific_evidence, item))
    scored.sort(key=lambda pair: (pair[0], int(pair[2].get("id") or 0)), reverse=True)
    # "长越的机械岗位" often matches every 长越岗位 through the client name,
    # while "机械" identifies one of them. Return that clear winner as a real
    # reference; only similarly scored jobs remain ambiguous for clarification.
    if len(scored) > 1 and scored[0][0] > scored[1][0] and scored[0][1]:
        return [scored[0][2]]
    return [item for _, _, item in scored[: max(1, min(int(limit or 5), 10))]]


def _reconcile_copilot_runtime_state(
    conn: sqlite3.Connection,
    focus: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove terminal workflow references before they can steer a later turn."""
    focus = dict(focus or {})
    state = dict(state or {})
    refs: list[str] = []
    for key in ("pending_workflow", "current_workflow"):
        value = focus.get(key) if isinstance(focus.get(key), dict) else {}
        if value.get("workflow_id"):
            refs.append(str(value["workflow_id"]))
    pending_plan = state.get("pending_plan") if isinstance(state.get("pending_plan"), dict) else {}
    active_goal = state.get("active_goal") if isinstance(state.get("active_goal"), dict) else {}
    for value in (pending_plan, active_goal):
        if value.get("workflow_id"):
            refs.append(str(value["workflow_id"]))
    statuses: dict[str, str] = {}
    if refs and _table_exists(conn, "agent_workflows"):
        placeholders = ",".join("?" for _ in set(refs))
        rows = conn.execute(
            f"SELECT workflow_id,status FROM agent_workflows WHERE workflow_id IN ({placeholders})",
            tuple(dict.fromkeys(refs)),
        ).fetchall()
        statuses = {str(row["workflow_id"]): str(row["status"] or "") for row in rows}

    active_refs: list[dict[str, Any]] = []
    terminal_actions: set[str] = set()
    for key in ("pending_workflow", "current_workflow"):
        value = focus.get(key) if isinstance(focus.get(key), dict) else {}
        workflow_id = str(value.get("workflow_id") or "")
        status = statuses.get(workflow_id, str(value.get("status") or ""))
        if workflow_id and status in TERMINAL_WORKFLOW_STATUSES:
            terminal_actions.add(str(value.get("action") or ""))
            focus[key] = {}
        elif workflow_id:
            value = dict(value)
            value["status"] = status or value.get("status")
            focus[key] = value
            active_refs.append(value)

    pending_workflow_id = str(pending_plan.get("workflow_id") or "")
    pending_status = statuses.get(pending_workflow_id, str(pending_plan.get("status") or ""))
    if pending_workflow_id and pending_status in TERMINAL_WORKFLOW_STATUSES:
        terminal_actions.add(str(pending_plan.get("action") or ""))
        state["pending_plan"] = {}
    elif pending_workflow_id:
        pending_plan = dict(pending_plan)
        pending_plan["status"] = pending_status or pending_plan.get("status")
        state["pending_plan"] = pending_plan
        active_refs.append(pending_plan)

    goal_workflow_id = str(active_goal.get("workflow_id") or "")
    goal_status = statuses.get(goal_workflow_id, str(active_goal.get("status") or ""))
    if goal_workflow_id and goal_status in TERMINAL_WORKFLOW_STATUSES:
        terminal_actions.add(str(active_goal.get("action") or ""))
        active_goal = dict(active_goal)
        active_goal["status"] = goal_status
        state["active_goal"] = active_goal
    if not active_refs and str(focus.get("action") or "") in terminal_actions:
        focus["action"] = ""
        focus["objective"] = ""
    focus["conversation_state"] = state
    return focus, state


def get_copilot_context_state(self, session_id: str) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT state_json FROM agent_copilot_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        state = _loads(row["state_json"], {}) if row else {}
        _, state = _reconcile_copilot_runtime_state(conn, {}, state)
        return state
    finally:
        conn.close()


def get_copilot_focus(self, session_id: str) -> dict[str, Any] | None:
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT focus.revision,focus.context_type,focus.context_id,focus.client,
                   focus.job_id,focus.candidate_id,focus.action,focus.confidence,
                   focus.focus_json,focus.evidence_json,focus.conflicts_json,focus.updated_at,
                   state.state_json
            FROM agent_copilot_focus focus
            LEFT JOIN agent_copilot_state state ON state.session_id=focus.session_id
            WHERE focus.session_id=?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        focus = _loads(row["focus_json"], {})
        focus.update(
            {
                "session_id": session_id,
                "revision": int(row["revision"] or 1),
                "context": focus.get("context") or {
                    "type": row["context_type"] or "global",
                    "id": row["context_id"],
                },
                "client": focus.get("client") or row["client"] or "",
                "action": focus.get("action") or row["action"] or "",
                "confidence": float(row["confidence"] or 0),
                "evidence": _loads(row["evidence_json"], []),
                "conflicts": _loads(row["conflicts_json"], []),
                "updated_at": row["updated_at"],
            }
        )
        state = _loads(row["state_json"], {})
        focus, _ = _reconcile_copilot_runtime_state(conn, focus, state)
        focus["needs_clarification"] = bool(focus.get("conflicts"))
        return focus
    finally:
        conn.close()


def _copilot_focus_from_joined_row(row: sqlite3.Row) -> dict[str, Any] | None:
    if row["focus_revision"] is None:
        return None
    focus = _loads(row["focus_json"], {})
    focus.update(
        {
            "session_id": str(row["session_id"]),
            "revision": int(row["focus_revision"] or 1),
            "context": focus.get("context") or {
                "type": row["focus_context_type"] or "global",
                "id": row["focus_context_id"],
            },
            "client": focus.get("client") or row["focus_client"] or "",
            "action": focus.get("action") or row["focus_action"] or "",
            "confidence": float(row["focus_confidence"] or 0),
            "evidence": _loads(row["focus_evidence_json"], []),
            "conflicts": _loads(row["focus_conflicts_json"], []),
            "updated_at": row["focus_updated_at"],
        }
    )
    focus["needs_clarification"] = bool(focus.get("conflicts"))
    return focus

@staticmethod
def _is_job_budget_fact_update(message: str) -> bool:
    """A standalone job-budget fact is context, not a salary workflow request."""
    text = " ".join(str(message or "").split())
    if not text or _is_explicit_question(text):
        return False
    if any(
        token in text
        for token in (
            "谈薪", "薪资谈判", "谈薪方案", "薪资核验", "薪资报告", "谈薪风险",
            "怎么谈", "如何谈", "帮我谈", "整理", "生成", "制作", "处理",
            "开始", "执行", "启动",
        )
    ):
        return False
    budget_marker = bool(
        "预算" in text
        or any(token in text for token in ("薪资范围", "薪酬范围", "年薪范围", "总包范围", "总包上限"))
    )
    if not budget_marker:
        return False
    has_amount = bool(
        re.search(r"\d+(?:\.\d+)?\s*(?:w|W|万|k|K)", text)
        or re.search(r"\d+(?:\.\d+)?\s*[-~至到]\s*\d+(?:\.\d+)?\s*(?:w|W|万|k|K)", text)
    )
    return has_amount and any(token in text for token in ("岗位", "职位", "这个岗", "这个机会", "客户"))


def _format_job_budget_fact_answer(message: str, selected_facts: dict[str, Any]) -> str:
    quote = " ".join(str(message or "").split())
    job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    client = str(selected_facts.get("client") or job.get("client") or "").strip()
    title = str(job.get("title") or job.get("job") or "").strip()
    scope = " / ".join(part for part in (client, title) if part) or "当前岗位"
    return (
        f"结论：我把「{quote}」理解为{scope}的岗位预算补充，不创建谈薪任务。\n\n"
        "下一步：我会在本轮对话里按这个预算判断匹配度和沟通口径；"
        "如果要写入岗位库，请明确说“更新岗位库预算”。"
    )


_CANDIDATE_RESULT_OBSERVATION_RE = re.compile(
    r"(?:这轮|本轮|目前|现在|截至现在).{0,30}"
    r"(?:找到|找出|筛出|筛到|召回|入库).{0,16}"
    r"(?:\d+|[一二两三四五六七八九十]+)\s*(?:位|个|名|人)?(?:人选|候选人)"
)


def _is_candidate_result_observation(
    message: str,
    understanding: dict[str, Any],
) -> bool:
    if (
        str(understanding.get("speech_act") or "") != "inform"
        or str(understanding.get("action") or "none") != "none"
        or _is_explicit_question(message)
    ):
        return False
    fact_updates = understanding.get("fact_updates")
    return bool(
        isinstance(fact_updates, list)
        and any(
            isinstance(item, dict) and item.get("kind") == "workflow_observation"
            for item in fact_updates
        )
        and _CANDIDATE_RESULT_OBSERVATION_RE.search(message)
    )


def _format_candidate_result_observation_answer(
    message: str,
    selected_facts: dict[str, Any],
    existing_focus: dict[str, Any] | None,
    *,
    floating_compact: bool,
) -> str:
    quote = " ".join(str(message or "").split())
    job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    client = str(selected_facts.get("client") or job.get("client") or "").strip()
    title = str(job.get("title") or job.get("job") or "").strip()
    scope = " / ".join(part for part in (client, title) if part) or "当前岗位"
    if floating_compact:
        return (
            f"结论：我把“{quote}”作为{scope}的结果反馈，不会新建寻访任务。\n\n"
            "下一步：需要扩池时再明确说“继续寻访”。"
        )

    state = (
        existing_focus.get("conversation_state")
        if isinstance(existing_focus, dict)
        and isinstance(existing_focus.get("conversation_state"), dict)
        else {}
    )
    active_goal = state.get("active_goal") if isinstance(state.get("active_goal"), dict) else {}
    objective = str(active_goal.get("objective") or "").strip()
    status = str(active_goal.get("status") or "").strip()
    goal_line = (
        f"当前主线仍是“{objective}”；这条反馈只更新结果，不替换目标。\n\n"
        if objective and status not in TERMINAL_WORKFLOW_STATUSES
        else ""
    )
    return (
        f"结论：我把“{quote}”理解为{scope}的本轮结果反馈，不会自动新建寻访任务。\n\n"
        f"{goal_line}"
        "下一步：先判断这批结果是否值得推进；需要继续扩池时，再明确说“继续寻访”。"
    )


@staticmethod
def _new_candidate_outreach_requested(message: str) -> bool:
    """Keep a request for new people separate from an existing-batch follow-up."""
    text = " ".join(str(message or "").split())
    if not any(token in text for token in ("候选人", "人选", "新人")):
        return False
    if any(token in text for token in ("二次跟进", "再跟一次", "催回复", "这批", "这12", "已触达的", "已联系的")):
        return False
    return any(
        token in text
        for token in (
            "再触达", "补充触达", "补充并触达", "继续联系新人",
            "联系新人", "多找些人再联系", "再找一批触达",
        )
    )


def _continued_sourcing_requested(message: str) -> bool:
    text = " ".join(str(message or "").split())
    return (
        any(token in text for token in ("候选人", "人选"))
        and any(token in text for token in ("再寻访", "继续寻访", "再搜索", "继续搜索", "补搜", "再找", "继续找", "再多找"))
    )


def _strategy_revision_requested(message: str) -> bool:
    text = " ".join(str(message or "").split())
    return (
        any(token in text for token in ("寻访策略", "搜索策略", "搜人策略", "策略部分"))
        and any(token in text for token in ("修改", "调整", "改一下", "改下", "修订", "优化"))
    )


def _pending_sourcing_refinement_mode(
    message: str,
    existing_focus: dict[str, Any] | None,
    speech_act: str,
) -> str:
    """Classify constraint discussion against a pending sourcing plan."""
    if speech_act in {"ask", "discuss"} or not isinstance(existing_focus, dict):
        return ""
    pending = existing_focus.get("pending_workflow")
    if not isinstance(pending, dict) or not pending.get("workflow_id"):
        return ""
    if str(existing_focus.get("action") or "") not in {"candidate_sourcing", "strategy_revision"}:
        return ""
    text = " ".join(str(message or "").split())
    if any(
        token in text
        for token in (
            "年限", "职级", "行业", "关键词", "公司池", "方向",
            "排除", "不要", "优先",
        )
    ):
        return "revise"
    if any(token in text for token in ("放宽", "收紧")) and any(token in text for token in ("条件", "要求")):
        return "discuss"
    return ""


def _confirmed_assistant_refinement(message: str, last_assistant_message: str) -> str:
    """Carry forward concrete assistant details only when the user confirms that dimension."""
    text = " ".join(str(message or "").split())
    assistant_text = " ".join(str(last_assistant_message or "").split())
    dimensions = ("年限", "职级", "行业", "方向", "关键词", "公司池", "条件", "要求")
    if not any(token in text for token in dimensions):
        return ""
    if not any(token in assistant_text for token in dimensions):
        return ""
    if not (
        re.search(r"\d+\s*年以上", assistant_text)
        or any(token in assistant_text for token in ("资深工程师", "主管", "经理", "总监", "不放宽"))
    ):
        return ""
    return assistant_text[:360]


def _strategy_revision_round(message: str) -> int | None:
    match = re.search(r"第\s*([0-9一二两三四五六七八九十两]+)\s*轮", str(message or ""))
    if not match:
        return None
    value = match.group(1)
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits.get(value)


def _strategy_revision_evidence(message: str, conversation_history: list[dict[str, Any]]) -> list[str]:
    signal_tokens = (
        "必须", "优先", "可看", "排除", "不要", "放宽", "经验", "量产", "预研",
        "年限", "职级", "行业", "方向", "场景", "客户", "公司池", "关键词", "技术",
    )
    user_evidence = [
        " ".join(str(item.get("content") or "").split())
        for item in conversation_history[-16:]
        if item.get("role") == "user"
        and any(token in str(item.get("content") or "") for token in signal_tokens)
        and not _strategy_revision_requested(str(item.get("content") or ""))
    ]
    current = " ".join(str(message or "").split())
    if any(token in current for token in signal_tokens):
        user_evidence.append(current)
    evidence = list(dict.fromkeys(item for item in user_evidence if item))[-6:]
    return evidence


def _strategy_revision_instruction(message: str, conversation_history: list[dict[str, Any]]) -> str:
    evidence = _strategy_revision_evidence(message, conversation_history)
    if not evidence:
        return ""
    return (
        "仅修订当前工作流的寻访策略，不改变岗位、轮次目标或外部动作范围。"
        f"顾问已确认的原始条件：{'；'.join(evidence)}。"
        "生成前必须逐项读取原 strategy_v2，只更新岗位本质、目标公司池、关键词组和排除规则；"
        "不得声称删除原策略中不存在的词，不得把助手历史回答当作顾问事实。"
    )


def _resolve_strategy_revision_workflow(
    self, message: str, selected: dict[str, Any]
) -> tuple[str | None, str]:
    explicit = re.search(r"workflow_[0-9a-zA-Z]+", str(message or ""))
    selected_workflow_id = str(selected.get("id") or "").strip() if selected.get("type") == "workflow" else ""
    if not explicit and re.fullmatch(r"workflow_[0-9a-zA-Z]+", selected_workflow_id):
        explicit = re.match(r"workflow_[0-9a-zA-Z]+", selected_workflow_id)
    job_id = int(selected.get("id") or 0) if selected.get("type") == "job" else 0
    if not explicit and not job_id:
        return None, "请先打开目标岗位，或在消息中明确客户和岗位。"
    conn = self._connect()
    try:
        params: list[Any] = []
        where = "w.workflow_id=?" if explicit else "g.context_type='job' AND g.context_id=?"
        params.append(explicit.group(0) if explicit else job_id)
        rows = conn.execute(
            f"""
            SELECT w.workflow_id,w.status,w.created_at,g.title,g.objective,g.context_id,g.context_json,
                   (SELECT s.status FROM agent_workflow_steps s
                    WHERE s.workflow_id=w.workflow_id AND s.capability_id='multi_channel_sourcing'
                    ORDER BY s.sequence LIMIT 1) AS sourcing_status
            FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE {where}
              AND EXISTS (SELECT 1 FROM agent_workflow_steps s
                          WHERE s.workflow_id=w.workflow_id AND s.capability_id='search_strategy')
              AND EXISTS (SELECT 1 FROM agent_workflow_steps s
                          WHERE s.workflow_id=w.workflow_id AND s.capability_id='multi_channel_sourcing')
            ORDER BY w.created_at,w.id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    if explicit and rows and job_id and int(rows[0]["context_id"] or 0) != job_id:
        return None, "消息中的工作流不属于当前岗位，请重新确认目标。"
    eligible = [
        row for row in rows
        if str(row["status"] or "") in {"planned", "queued", "paused", "waiting_approval", "blocked", "failed"}
        and str(row["sourcing_status"] or "") in {"pending", "waiting_approval", "blocked", "failed"}
    ]
    asked_round = _strategy_revision_round(message)
    if asked_round is not None:
        titles = {str(row["workflow_id"]): str(row["title"] or "") for row in rows}
        eligible = [
            row for row in eligible
            if f"第{asked_round}轮寻访" in str(row["title"] or "")
            or f"第{asked_round}轮寻访" in titles.get(
                str(
                    (_loads(row["context_json"], {}) or {}).get("revision_root_workflow_id")
                    or (_loads(row["context_json"], {}) or {}).get("revision_of_workflow_id")
                    or ""
                ),
                "",
            )
        ]
    if not eligible:
        scope = f"第{asked_round}轮" if asked_round is not None else "当前岗位"
        return None, f"没有找到{scope}可安全修订的待执行寻访工作流。"
    if len(eligible) > 1:
        labels = "、".join(str(row["title"] or row["workflow_id"]) for row in eligible[:3])
        return None, f"找到多个可修订工作流：{labels}。请明确第几轮。"
    return str(eligible[0]["workflow_id"]), ""


# ---- Copilot 策略建议结构化（strategy_patch）----
# Copilot 回答中的策略类建议提取为结构化补丁，浮窗渲染「应用到策略」操作栏，
# 确认后走修订链（revise_workflow）落地，不做原策略原地改写。
_STRATEGY_PATCH_TYPES = {
    "add_keyword": ("keywords", "新增关键词"),
    "add_company": ("targetCompanies", "新增对标公司"),
    "add_scene": ("sceneWords", "新增场景词"),
    "add_filter": ("exclusions", "新增过滤条件"),
}
_STRATEGY_PATCH_MAX_CHANGES = 20
_STRATEGY_PATCH_INSTRUCTION_PREFIX = "仅修订当前工作流的寻访策略，不改变岗位、轮次目标或外部动作范围。顾问已确认的原始条件："
_STRATEGY_PATCH_INSTRUCTION_SUFFIX = (
    "。生成前必须逐项读取原 strategy_v2，只更新岗位本质、目标公司池、关键词组和排除规则；"
    "不得声称删除原策略中不存在的词，不得把助手历史回答当作顾问事实。"
)


def _strategy_patch_candidate(message: str, answer: str) -> bool:
    """门控：只有"含策略要素的建议类回答"才值得多花一次 LLM 提取。"""
    text = str(answer or "")
    if not text.strip():
        return False
    if _strategy_revision_requested(str(message or "")):
        return True
    return (
        any(token in text for token in ("关键词", "公司", "场景", "排除", "过滤"))
        and any(token in text for token in ("建议", "补充", "新增", "扩展", "可以加", "加上"))
    )


def _strategy_term_key(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _strategy_v2_existing_values(v2: dict[str, Any]) -> dict[str, set[str]]:
    """从 strategy_v2 提取现有词条，用于服务端去重。"""
    terms: set[str] = set()
    companies: set[str] = set()
    rules: set[str] = set()
    for group in v2.get("step4_keyword_groups") or []:
        for term in (group or {}).get("terms") or []:
            key = _strategy_term_key(term)
            if key:
                terms.add(key)
        for target in (group or {}).get("targets") or []:
            key = _strategy_term_key(target)
            if key:
                companies.add(key)
    for pool in v2.get("step2_target_pool") or []:
        for company in (pool or {}).get("companies") or []:
            key = _strategy_term_key((company or {}).get("name"))
            if key:
                companies.add(key)
    for rule in v2.get("negative_rules") or []:
        key = _strategy_term_key((rule or {}).get("rule"))
        if key:
            rules.add(key)
    return {"terms": terms, "companies": companies, "rules": rules}


def _normalize_strategy_patch_changes(raw_changes: Any, existing: dict[str, set[str]]) -> list[dict[str, Any]]:
    """校验 + patch 内去重 + 对现有策略去重。返回按置信度降序的变更列表。"""
    changes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_changes if isinstance(raw_changes, list) else []:
        if not isinstance(item, dict):
            continue
        change_type = str(item.get("type") or "")
        if change_type not in _STRATEGY_PATCH_TYPES:
            continue
        field, label = _STRATEGY_PATCH_TYPES[change_type]
        value = " ".join(str(item.get("value") or "").split())
        if not 2 <= len(value) <= 40:
            continue
        key = _strategy_term_key(value)
        if (change_type, key) in seen:
            continue
        # 服务端去重：关键词/场景词对现有 terms+公司池，公司对现有公司池，过滤对现有排除规则
        if change_type in ("add_keyword", "add_scene") and (key in existing["terms"] or key in existing["companies"]):
            continue
        if change_type == "add_company" and key in existing["companies"]:
            continue
        if change_type == "add_filter" and key in existing["rules"]:
            continue
        seen.add((change_type, key))
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            confidence = 0.5
        changes.append({
            "type": change_type,
            "field": field,
            "value": value,
            "confidence": confidence,
            "clause": f"{label}「{value}」",
        })
    changes.sort(key=lambda change: -change["confidence"])
    return changes[:_STRATEGY_PATCH_MAX_CHANGES]


def _build_strategy_patch(
    self,
    message: str,
    answer: str,
    selected: dict[str, Any],
    conversation_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """从 copilot 回答提取可落地的策略补丁。任何失败都返回 None（不阻断主流程）。"""
    if not _strategy_patch_candidate(message, answer):
        return None
    try:
        workflow_id, _error = _resolve_strategy_revision_workflow(self, message, selected)
        if not workflow_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT s.output_json, g.title
                FROM agent_workflows w
                JOIN agent_goals g ON g.goal_id=w.goal_id
                JOIN agent_workflow_steps s ON s.workflow_id=w.workflow_id
                WHERE w.workflow_id=? AND s.capability_id='search_strategy'
                ORDER BY s.sequence DESC LIMIT 1
                """,
                (workflow_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        step_output = _loads(row["output_json"], {}) or {}
        v2 = step_output.get("strategy_v2") if isinstance(step_output.get("strategy_v2"), dict) else {}
        existing = _strategy_v2_existing_values(v2)
        extracted = self.llm.extract_strategy_patch({"message": message, "answer": answer})
        if not extracted:
            return None
        changes = _normalize_strategy_patch_changes(extracted.get("changes"), existing)
        if not changes:
            return None
        evidence = _strategy_revision_evidence(message, conversation_history or [])
        instruction_prefix = _STRATEGY_PATCH_INSTRUCTION_PREFIX
        if evidence:
            instruction_prefix += f"{'；'.join(evidence)}；"
        return {
            "version": "1.0",
            "source": "copilot",
            "workflow_id": workflow_id,
            "workflow_title": str(row["title"] or workflow_id),
            "changes": changes,
            "instruction_prefix": instruction_prefix,
            "instruction_suffix": _STRATEGY_PATCH_INSTRUCTION_SUFFIX,
            "consultant_evidence": evidence,
        }
    except Exception:
        return None


@staticmethod
def _copilot_action_kind(message: str) -> str:
    if _is_job_budget_fact_update(message):
        return ""
    if _strategy_revision_requested(message):
        return "strategy_revision"
    if _new_candidate_outreach_requested(message):
        return "new_candidate_outreach"
    if (
        any(token in message for token in ("人选", "候选人"))
        and any(token in message for token in ("补充", "补池", "找", "搜索", "搜", "寻访"))
    ):
        return "candidate_sourcing"
    rules = (
        ("job_archive", ("归档岗位", "岗位归档", "关闭岗位", "岗位关闭", "没拆分的岗位", "未拆分的岗位")),
        ("job_split", ("拆分岗位", "岗位拆分", "分成")),
        ("job_publish", ("发布岗位", "岗位发布", "上架岗位")),
        ("candidate_sourcing", ("补池", "寻访", "找人", "找些人选", "找候选人", "搜索人选")),
        ("candidate_outreach", ("触达", "开聊", "发送消息", "联系候选人", "二次跟进", "再跟一次", "催回复")),
        ("candidate_review", ("复核", "初筛", "停止推进", "继续推进")),
        ("recommendation", ("推荐报告", "推荐给客户", "提交客户")),
        ("salary", ("谈薪", "薪资")),
    )
    for action, tokens in rules:
        if any(token in message for token in tokens):
            return action
    return ""


_JOB_REQUIREMENT_MARKERS = (
    "岗位需求", "JD", "jd", "职位描述", "岗位描述", "岗位职责", "岗位职则",
    "任职要求", "任职资格", "职位要求", "岗位要求", "招聘需求", "新增岗位",
    "新岗位", "录入岗位", "接入岗位", "岗位接入",
)


@staticmethod
def _is_job_requirement_message(message: str) -> bool:
    """检测用户是否正在输入/粘贴一份岗位需求（JD）。"""
    text = str(message or "")
    return any(marker in text for marker in _JOB_REQUIREMENT_MARKERS)


_COPILOT_SPEECH_ACTS = {
    "ask", "inform", "discuss", "propose", "confirm", "execute", "correct", "cancel", "other",
}
_COPILOT_SEMANTIC_ACTIONS = {
    "none", "candidate_sourcing", "strategy_revision", "candidate_outreach",
    "candidate_review", "job_publish", "job_split", "job_archive",
    "recommendation", "salary",
}
_COPILOT_CONSTRAINT_KINDS = {"must", "prefer", "allow", "exclude", "target_count", "other"}


def _is_plan_control_instruction(value: Any) -> bool:
    text = "".join(str(value or "").split())
    if not text:
        return False
    return any(
        token in text
        for token in (
            "先生成计划", "只生成计划", "先建立计划", "只建立计划",
            "先看计划", "先不要执行", "暂时不要执行", "不要执行",
            "先别执行", "不要启动", "先别启动",
        )
    )


def _is_explicit_question(message: str) -> bool:
    text = " ".join(str(message or "").split())
    return bool(re.search(r"[?？]", text)) or any(
        token in text
        for token in (
            "请问", "要不要", "是否", "能不能", "可不可以", "为什么", "怎么", "如何",
            "我是问", "我想问", "问一下", "想了解", "是什么", "怎么样",
        )
    )


# 查询型名单请求：顾问直接要"名单/列表/筛出人选"，应当直答候选池，
# 而不是建一个等待确认的执行计划（2026-08-10 长越机械人选名单卡在 create_plan）。
_QUERY_LIST_MARKERS = (
    "名单", "列表", "列一下", "列出", "列出来", "筛出", "筛一下", "筛选",
    "有哪些人选", "有什么人选", "给一份", "整理一份", "排一下", "排个序",
    "优先评估", "优先名单", "核验名单",
)
_QUERY_LIST_EXCLUSIONS = (
    "寻访", "补池", "找人", "找候选人", "搜索", "搜人", "触达", "开聊",
    "发送", "联系候选人", "更新", "拆分", "归档", "发布", "推荐给客户",
    "提交客户", "谈薪", "复核进度", "计划", "执行", "启动", "开始",
    "发给客户", "发给", "重新评估", "评估进度", "风险", "问题",
)


def _is_candidate_list_query(message: str) -> bool:
    """判断消息是否为“直接要候选名单/筛选结果”的查询型请求。

    疑问句（“这个名单上怎么都是做光刻机的”“名单里为什么没有XX”）
    不是“再给一份名单”的指令，必须排除，避免名单直答把质疑吞掉。
    """
    text = " ".join(str(message or "").split())
    if _is_explicit_question(text):
        return False
    # “禁挖名单/目标公司名单/排除名单”是岗位策略事实，不是候选人名单请求。
    # 先移除这些政策语境，再判断是否仍有明确的候选池列表动作，避免把锚点
    # 回答提前拦截成“候选池为空”。
    candidate_list_text = re.sub(
        r"(?:禁挖|排除|竞业|目标公司|目标企业|客户|黑|白)名单",
        "",
        text,
        flags=re.IGNORECASE,
    )
    if not any(marker in candidate_list_text for marker in _QUERY_LIST_MARKERS):
        return False
    if any(token in candidate_list_text for token in _QUERY_LIST_EXCLUSIONS):
        return False
    return True


def _format_candidate_list_answer(db_path: str, job_id: int, message: str) -> str:
    """从候选池生成岗位名单文本（含岗位上下文、阶段分组、固晶/共晶/键合优先）。"""
    return _build_candidate_list_card(db_path, job_id, message)[0]


def _build_candidate_list_card(db_path: str, job_id: int, message: str) -> tuple[str, dict[str, Any]]:
    """生成名单文本 + 结构化卡片（action_card，前端渲染可点击名单弹窗）。

    返回 (answer_text, card)。card 形如：
    {
      "type": "candidate_list",
      "title": "长越科技｜机械高级工程师（岗位 137）候选名单",
      "context": {"type": "job", "id": 137},
      "summary": {"total": 329, "active": 321, "stopped": 8, "bonder_count": 37},
      "groups": [
        {"key": "bonder", "label": "固晶机/共晶机/键合机背景", "priority": true, "candidates": [...]},
        {"key": "active", "label": "其余可推进候选", "priority": false, "candidates": [...]},
        {"key": "stopped", "label": "已停止推进", "priority": false, "candidates": [...]},
      ],
    }
    每个 candidate: {id, name, company, title, stage, flow_bucket}
    """
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT j.id, c.name AS client, j.title
            FROM jobs j JOIN clients c ON c.id = j.client_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not job:
            return "", {}
        rows = conn.execute(
            """
            SELECT jc.id AS jc_id, p.display_name, p.current_company, p.current_title,
                   jc.clean_stage, jc.flow_bucket
            FROM job_candidates jc
            LEFT JOIN people p ON p.id = jc.person_id
            WHERE jc.job_id = ?
            ORDER BY jc.id DESC
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            empty_text = f"结论：岗位「{job['client']}｜{job['title']}」当前候选池为空。\n\n下一步：需要先启动一轮寻访补池。"
            card = {
                "type": "candidate_list",
                "title": f"{job['client']}｜{job['title']}（岗位 {job['id']}）候选名单",
                "context": {"type": "job", "id": job["id"]},
                "summary": {"total": 0, "active": 0, "stopped": 0, "bonder_count": 0},
                "groups": [],
            }
            return empty_text, card
        # 固晶/共晶/键合 关键词优先标记：一次性 JOIN 避免 N+1，candidate_profiles
        # 缺表时降级为普通名单（老库无此表，直接抛错会导致 copilot 500）。
        bonder = any(token in message for token in ("固晶", "共晶", "键合"))
        bonder_ids: set[int] = set()
        if bonder and _table_exists(conn, "candidate_profiles"):
            try:
                prof_rows = conn.execute(
                    """
                    SELECT DISTINCT jc.id AS jc_id
                    FROM candidate_profiles cp
                    JOIN people p ON p.display_name = cp.candidate_name
                                 AND p.current_company = cp.candidate_company
                    JOIN job_candidates jc ON jc.person_id = p.id
                    WHERE jc.job_id = ?
                      AND (cp.profile_summary LIKE '%固晶%'
                        OR cp.profile_summary LIKE '%共晶%'
                        OR cp.profile_summary LIKE '%键合%')
                      AND cp.profile_summary NOT LIKE '%补搜%'
                    """,
                    (job_id,),
                ).fetchall()
                bonder_ids = {int(r["jc_id"]) for r in prof_rows}
            except sqlite3.Error:
                bonder_ids = set()
        stage_order = {
            "待复核": 0, "新增寻访": 1, "已触达": 2, "已联系": 3,
            "初筛不通过": 4, "停止": 4, "淘汰": 4, "关闭": 4, "最近寻访": 4,
        }
        def stage_rank(stage: str) -> int:
            for key, rank in stage_order.items():
                if key in (stage or ""):
                    return rank
            return 3
        _STOP_TOKENS = ("初筛不通过", "停止", "淘汰", "关闭")
        rows = sorted(rows, key=lambda r: (stage_rank(str(r["clean_stage"])), r["jc_id"]))
        active = [r for r in rows if not any(k in (r["clean_stage"] or "") for k in _STOP_TOKENS)]
        stopped = [r for r in rows if any(k in (r["clean_stage"] or "") for k in _STOP_TOKENS)]
        # 优先名单：固晶/共晶/键合 命中者（未停止），按入库顺序排列
        prioritized = [r for r in active if r["jc_id"] in bonder_ids]
        prioritized.sort(key=lambda r: r["jc_id"])
        other_active = [r for r in active if r["jc_id"] not in bonder_ids]

        def to_candidate(r) -> dict[str, Any]:
            return {
                "id": int(r["jc_id"]),
                "name": str(r["display_name"] or "未知"),
                "company": str(r["current_company"] or ""),
                "title": str(r["current_title"] or ""),
                "stage": str(r["clean_stage"] or ""),
                "flow_bucket": str(r["flow_bucket"] or ""),
            }

        groups: list[dict[str, Any]] = []
        if prioritized:
            groups.append({
                "key": "bonder", "label": "固晶机/共晶机/键合机背景",
                "priority": True, "candidates": [to_candidate(r) for r in prioritized],
            })
        if other_active:
            groups.append({
                "key": "active", "label": "其余可推进候选",
                "priority": False, "candidates": [to_candidate(r) for r in other_active],
            })
        if stopped:
            groups.append({
                "key": "stopped", "label": "已停止推进",
                "priority": False, "candidates": [to_candidate(r) for r in stopped],
            })

        def fmt(r) -> str:
            stage = str(r["clean_stage"] or "")
            parts = [str(r["display_name"] or "未知")]
            if r["current_company"]:
                parts.append(str(r["current_company"]))
            if r["current_title"]:
                parts.append(str(r["current_title"]))
            label = " | ".join(dict.fromkeys(parts))
            return f"- {label}（{stage}）" if stage else f"- {label}"

        lines: list[str] = []
        lines.append(f"## {job['client']}｜{job['title']}（岗位 {job['id']}）候选名单")
        lines.append(f"共 {len(rows)} 人，其中可推进 {len(active)} 人、已停止 {len(stopped)} 人。\n")
        # 固晶优先组的优先级标注：A 级=直接固晶机/键合机经验，B 级=封装/精密设备相关，C 级=其余命中
        priority_notes: dict[int, tuple[str, str]] = {}
        if prioritized and bonder:
            try:
                for pr in conn.execute(
                    """
                    SELECT jc.id AS jc_id, cp.profile_summary
                    FROM job_candidates jc
                    JOIN people p ON p.id = jc.person_id
                    JOIN candidate_profiles cp ON cp.candidate_name = p.display_name
                                            AND cp.candidate_company = p.current_company
                    WHERE jc.job_id = ? AND jc.id IN (%s)
                      AND cp.profile_summary NOT LIKE '%%补搜%%'
                    """
                    % ",".join("?" * len(prioritized)),
                    (job_id, *[r["jc_id"] for r in prioritized]),
                ).fetchall():
                    summary = str(pr["profile_summary"] or "")
                    jc_id = int(pr["jc_id"])
                    if any(t in summary for t in ("固晶机", "固晶", "键合机", "die bond", "wire bond", "共晶机")):
                        level = "A"
                    elif any(t in summary for t in ("封装", "ASMPT", "先进微电子", "精密设备", "光刻", "刻蚀", "CVD", "PVD", "真空设备")):
                        level = "B"
                    else:
                        level = "C"
                    note = " ".join(summary.split())[:64]
                    priority_notes[jc_id] = (level, note)
            except sqlite3.Error:
                priority_notes = {}
        if prioritized:
            lines.append(f"### ⭐ 固晶机/共晶机/键合机背景（优先评估，{len(prioritized)} 人）")
            for r in prioritized[:12]:
                level, note = priority_notes.get(int(r["jc_id"]), ("C", ""))
                base = fmt(r)
                if level in ("A", "B"):
                    lines.append(f"- **【{level}级】** {base[2:]} — {note}")
                else:
                    lines.append(base)
            lines.append("")
        if other_active:
            lines.append(f"### 其余可推进候选（{len(other_active)} 人，列前 15）")
            lines.extend(fmt(r) for r in other_active[:15])
            lines.append("")
        if stopped:
            lines.append(f"### 已停止推进（{len(stopped)} 人，列前 5）")
            lines.extend(fmt(r) for r in stopped[:5])
        card = {
            "type": "candidate_list",
            "title": f"{job['client']}｜{job['title']}（岗位 {job['id']}）候选名单",
            "context": {"type": "job", "id": job["id"]},
            "summary": {
                "total": len(rows), "active": len(active), "stopped": len(stopped),
                "bonder_count": len(prioritized),
            },
            "groups": groups,
        }
        return "\n".join(lines), card
    finally:
        conn.close()


def _is_candidate_list_composition_question(message: str) -> bool:
    """判断消息是否为“质疑名单构成/来源”的提问（如“怎么都是做光刻机的”）。

    这类提问应回答构成分析与原因，而不是再次输出名单。
    """
    text = " ".join(str(message or "").split())
    if not _is_explicit_question(text):
        return False
    if not any(marker in text for marker in ("名单", "列表", "这些", "这批", "候选")):
        return False
    # 质疑/惊讶语气的核心特征：怎么/为什么/为何 + 都是/全是/都做/没有/找不到
    if not re.search(r"(?:怎么|为什么|为何).*(?:都是|全是|都做|都是做|都来自|都集中|没有|没看到|看不到|找不到)", text):
        return False
    return True


def _build_candidate_list_composition_answer(db_path: str, job_id: int, message: str) -> str:
    """生成“名单构成分析”回答：按公司/行业统计分布，解释为什么名单偏某类背景。"""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute(
            """
            SELECT j.id, c.name AS client, j.title
            FROM jobs j JOIN clients c ON c.id = j.client_id
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if not job:
            return ""
        rows = conn.execute(
            """
            SELECT p.current_company AS company, p.current_title AS title
            FROM job_candidates jc
            LEFT JOIN people p ON p.id = jc.person_id
            WHERE jc.job_id = ?
            """,
            (job_id,),
        ).fetchall()
        if not rows:
            return ""
        # 按公司聚合
        company_counts: dict[str, int] = {}
        for r in rows:
            company = str(r["company"] or "未知公司").strip()
            if not company or company in ("候选人目前没有工作", "我还不知道候选人在哪家公司工作"):
                company = "（未标注公司）"
            company_counts[company] = company_counts.get(company, 0) + 1
        top_companies = sorted(company_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        total = len(rows)
        # 半导体/光刻机相关关键词
        semicon_keywords = (
            "光刻", "微电子", "半导体", "精科", "北方华创", "中微", "华海清科",
            "上海微电子", "晶盛", "长川", "天准", "华兴源创", "精测", "第四十五",
            "芯", "纳", "宏", "AMAT", "ASML", "SMEE", "NAURA", "屹唐", "拓荆",
        )
        semicon_hits = sum(
            1 for r in rows if str(r["company"] or "") and any(k in str(r["company"]) for k in semicon_keywords)
        )
        semicon_ratio = semicon_hits / total * 100 if total else 0
        lines: list[str] = []
        lines.append(f"## {job['client']}｜{job['title']}（岗位 {job['id']}）名单构成分析")
        lines.append(f"共 {total} 人。这不是巧合——名单构成主要来自当前岗位的寻访策略。\n")
        lines.append(f"### 公司分布（前 {len(top_companies)}）")
        for name, count in top_companies:
            lines.append(f"- {name}：{count} 人")
        lines.append("")
        if semicon_hits:
            lines.append(
                f"### 原因\n"
                f"名单中约 {semicon_hits}/{total} 人（{semicon_ratio:.0f}%）来自半导体/光刻设备相关公司。"
                f"原因是岗位「{job['title']}」的寻访策略把目标公司池和关键词集中在了半导体设备厂商"
                f"（光刻机、量测、刻蚀、CVD 等），机械工程师背景的候选人也因此以这些公司为主。"
            )
        else:
            lines.append("### 原因\n当前名单没有明显行业集中，以上是公司分布参考。")
        lines.append(
            "\n### 下一步\n"
            "如果你想看到更分散的行业构成（例如通用机械、3C 自动化、光伏设备等），"
            "告诉我目标方向，我可以按新方向重新筛名单。"
        )
        return "\n".join(lines)
    finally:
        conn.close()


def _verbatim_constraint_candidates(messages: list[str]) -> list[dict[str, str]]:
    """Extract auditable clauses without normalizing the consultant's terminology."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        for raw_clause in re.split(r"[，,；;。\n]+", str(message or "")):
            clause = raw_clause.strip()
            if not clause or len(clause) > 180 or _is_plan_control_instruction(clause):
                continue
            kind = ""
            if any(token in clause for token in ("必须", "一定要", "硬性", "不能少")):
                kind = "must"
            elif any(token in clause for token in ("优先", "更好", "最好")):
                kind = "prefer"
            elif any(token in clause for token in ("可看", "可以看", "可接受")):
                kind = "allow"
            elif any(token in clause for token in ("排除", "不要", "不能要", "不考虑")):
                kind = "exclude"
            elif (
                re.search(r"\d+\s*(?:位|个|名|人)(?:选|候选人)?", clause)
                and not re.search(r"(?:只|已|已经|目前|现在).*找到", clause)
            ):
                kind = "target_count"
            elif any(token in clause for token in ("年经验", "年以上", "职级", "行业", "方向")):
                kind = "other"
            if not kind or clause in seen:
                continue
            seen.add(clause)
            rows.append({"quote": clause, "kind": kind})
    return rows[-12:]


def _interpret_copilot_message(
    self,
    message: str,
    selected: dict[str, Any],
    selected_facts: dict[str, Any],
    existing_focus: dict[str, Any] | None,
    conversation_history: list[dict[str, str]],
    last_assistant_message: str,
) -> dict[str, Any]:
    """Use the model for semantics, then constrain its output to verified local facts."""
    deterministic_action = self._copilot_action_kind(message) or "none"
    recent_user_messages = [
        str(item.get("content") or "")
        for item in conversation_history[-16:]
        if item.get("role") == "user"
    ]
    known_jobs = self._mentioned_jobs_for_copilot(message)
    current_job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    focus_job = existing_focus.get("job") if isinstance(existing_focus, dict) and isinstance(existing_focus.get("job"), dict) else {}
    known_targets: list[dict[str, Any]] = []
    for item in [current_job, focus_job, *known_jobs]:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        target = {
            "type": "job", "id": int(item["id"]),
            "client": str(item.get("client") or selected_facts.get("client") or (existing_focus or {}).get("client") or ""),
            "label": str(item.get("title") or item.get("job") or ""),
        }
        if target not in known_targets:
            known_targets.append(target)
    if selected.get("type") in {"candidate", "workflow"} and selected.get("id"):
        known_targets.append({
            "type": selected["type"], "id": selected["id"],
            "client": str(selected_facts.get("client") or ""),
            "label": str((selected_facts.get("candidate") or {}).get("name") or (selected_facts.get("workflow") or {}).get("title") or ""),
        })
    payload = {
        "current_message": message,
        "recent_user_messages": recent_user_messages[-8:],
        "last_assistant_message": last_assistant_message[-1200:],
        "current_context": selected,
        "known_targets": known_targets,
        "pending_action": {
            "action": str((existing_focus or {}).get("action") or "none"),
            "objective": str((existing_focus or {}).get("objective") or ""),
            "constraints": list(
                (existing_focus or {}).get("constraint_ledger")
                or (existing_focus or {}).get("constraints")
                or []
            ),
            "pending_plan": dict((existing_focus or {}).get("pending_workflow") or {}),
        },
        "conversation_state": dict((existing_focus or {}).get("conversation_state") or {}),
        "deterministic_hint": deterministic_action,
    }
    raw: dict[str, Any] = {}
    try:
        interpreted = self.llm.interpret_copilot_intent(sanitize_payload(payload))
        if isinstance(interpreted, dict):
            raw = interpreted
    except (LLMError, ValueError, TypeError):
        raw = {}

    speech_act = str(raw.get("speech_act") or "").strip().lower()
    if _is_explicit_question(message):
        speech_act = "ask"
    elif speech_act not in _COPILOT_SPEECH_ACTS:
        if _is_short_ack(message) and (existing_focus or {}).get("action"):
            speech_act = "confirm"
        elif (
            (existing_focus or {}).get("action") in {"candidate_sourcing", "strategy_revision"}
            and re.fullmatch(r"(?:可以|确认|现在)?(?:开始|继续|重新|执行)?(?:搜索|寻访)(?:吧|了|可以)?", message, re.I)
        ):
            speech_act = "confirm"
        elif re.search(r"(?:取消|算了|停止这个计划|不要执行)", message):
            speech_act = "cancel"
        elif re.search(r"(?:纠正|更正|不是.+而是|改成|改为|去掉|删除|不再要求)", message):
            speech_act = "correct"
        elif deterministic_action != "none":
            speech_act = "execute" if re.search(r"(?:立即|马上|直接|现在)?(?:开始|执行)", message) else "propose"
        else:
            speech_act = "other"
    action = str(raw.get("action") or "").strip().lower()
    if action not in _COPILOT_SEMANTIC_ACTIONS:
        action = "none"
    if action == "none" and deterministic_action != "none":
        action = deterministic_action
    if action == "new_candidate_outreach":
        action = "candidate_sourcing"
    job_budget_fact_update = _is_job_budget_fact_update(message)
    if job_budget_fact_update and action == "salary":
        action = "none"
        if speech_act in {"propose", "confirm", "execute", "correct"}:
            speech_act = "inform"
    if action == "none" and speech_act in {"confirm", "correct", "cancel"} and (existing_focus or {}).get("action"):
        previous_action = str((existing_focus or {}).get("action") or "none")
        action = previous_action if previous_action in _COPILOT_SEMANTIC_ACTIONS else "none"
    refinement_mode = _pending_sourcing_refinement_mode(message, existing_focus, speech_act)
    if refinement_mode:
        action = "strategy_revision"
        speech_act = "correct" if refinement_mode == "revise" else "discuss"

    allowed_target_keys = {(str(item.get("type")), str(item.get("id"))): item for item in known_targets}
    raw_target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    target = allowed_target_keys.get((str(raw_target.get("type") or ""), str(raw_target.get("id") or "")))
    if target is None and selected.get("type") in {"job", "candidate", "workflow"} and selected.get("id"):
        target = allowed_target_keys.get((str(selected["type"]), str(selected["id"])))
    if target is None and bool(raw.get("refers_to_previous")) and len(known_targets) == 1:
        target = known_targets[0]
    target = dict(target or {"type": "global", "id": None, "client": "", "label": ""})

    source_messages = [*recent_user_messages, message]
    source_corpus = "\n".join(source_messages)
    constraints: list[dict[str, str]] = []
    seen_quotes: set[str] = set()
    for item in raw.get("constraints") or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        kind = str(item.get("kind") or "other").strip().lower()
        if quote and quote in source_corpus and quote not in seen_quotes and not _is_plan_control_instruction(quote):
            seen_quotes.add(quote)
            constraints.append({"quote": quote, "kind": kind if kind in _COPILOT_CONSTRAINT_KINDS else "other"})
    for item in _verbatim_constraint_candidates(source_messages[-8:]):
        if item["quote"] not in seen_quotes:
            seen_quotes.add(item["quote"])
            constraints.append(item)

    try:
        confidence = max(0.0, min(float(raw.get("confidence") or 0.0), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not raw and deterministic_action != "none":
        confidence = 0.8
    elif not raw and speech_act in {"confirm", "correct", "cancel"} and (existing_focus or {}).get("action"):
        confidence = max(0.8, min(float((existing_focus or {}).get("confidence") or 0.0), 1.0))
    missing_fields = [str(item).strip()[:80] for item in (raw.get("missing_fields") or []) if str(item).strip()][:6]
    needs_clarification = bool(raw.get("needs_clarification"))
    if _is_short_ack(message) and action == "none":
        needs_clarification = True
        missing_fields = missing_fields or ["要确认的动作"]
    raw_constraint_changes = [
        item
        for item in (raw.get("constraint_changes") or [])
        if isinstance(item, dict)
        and not _is_plan_control_instruction(item.get("quote") or item.get("value"))
    ]
    understanding = {
        "version": "copilot_understanding_v1",
        "speech_act": speech_act,
        "action": action,
        "topic": str(raw.get("topic") or "").strip()[:48],
        "objective": str(raw.get("objective") or ((existing_focus or {}).get("objective") if speech_act == "confirm" else "") or "").strip()[:500],
        "target": target,
        "constraints": constraints[-12:],
        "fact_updates": list(raw.get("fact_updates") or [])[:8],
        "action_evidence": list(raw.get("action_evidence") or [])[:4],
        "refers_to_previous": bool(raw.get("refers_to_previous")) or speech_act == "confirm",
        "confidence": round(confidence, 3),
        "needs_clarification": needs_clarification,
        "missing_fields": missing_fields,
        "clarification_question": str(raw.get("clarification_question") or "").strip()[:240],
        "source_message": message,
        "raw_constraint_changes": raw_constraint_changes,
        "safe_for_action": bool(
            action != "none"
            and speech_act in {"propose", "confirm", "execute", "correct", "cancel"}
            and confidence >= 0.72
            and not needs_clarification
        ),
    }
    return enrich_turn_understanding(
        understanding,
        message=message,
        pending_plan_ref=dict((existing_focus or {}).get("pending_workflow") or {}),
    )


def _copilot_pending_plan(
    self,
    selected: dict[str, Any],
    existing_focus: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates: list[str] = []
    pending = existing_focus.get("pending_workflow") if isinstance(existing_focus, dict) else {}
    if isinstance(pending, dict) and pending.get("workflow_id"):
        candidates.append(str(pending["workflow_id"]))
    if selected.get("type") == "workflow" and selected.get("id"):
        candidates.append(str(selected["id"]))
    focus_context = existing_focus.get("context") if isinstance(existing_focus, dict) else {}
    if isinstance(focus_context, dict) and focus_context.get("type") == "workflow" and focus_context.get("id"):
        candidates.append(str(focus_context["id"]))
    for workflow_id in dict.fromkeys(candidates):
        try:
            state = self.get_workflow(workflow_id)
        except ValueError:
            continue
        status = str((state.get("workflow") or {}).get("status") or "")
        if status in TERMINAL_WORKFLOW_STATUSES:
            continue
        plan_ref = dict(state.get("plan_ref") or {})
        if plan_ref.get("workflow_id") and plan_ref.get("plan_hash"):
            return plan_ref, state
    return {}, {}


def _copilot_focus_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    context_type = str(context.get("type") or "global")
    try:
        context_id = int(context.get("id") or 0)
    except (TypeError, ValueError):
        context_id = 0
    if context_type not in {"job", "candidate"} or context_id <= 0:
        return {}
    conn = self._connect()
    try:
        if context_type == "job":
            row = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status
                FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                """,
                (context_id,),
            ).fetchone()
            if row:
                return {
                    "context": {"type": "job", "id": context_id},
                    "client": str(row["client"] or ""),
                    "job": {"id": context_id, "title": str(row["job"] or ""), "status": str(row["status"] or "")},
                    "candidate": {},
                }
            return {}
        row = conn.execute(
            """
            SELECT jc.id,p.display_name,c.name AS client,j.id AS job_id,j.title AS job
            FROM job_candidates jc JOIN people p ON p.id=jc.person_id
            LEFT JOIN jobs j ON j.id=jc.job_id LEFT JOIN clients c ON c.id=j.client_id
            WHERE jc.id=?
            """,
            (context_id,),
        ).fetchone()
        if row:
            return {
                "context": {"type": "candidate", "id": context_id},
                "client": str(row["client"] or ""),
                "job": {"id": int(row["job_id"] or 0), "title": str(row["job"] or "")},
                "candidate": {"id": context_id, "name": str(row["display_name"] or "")},
            }
    finally:
        conn.close()
    return {}


def _copilot_workflow_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(context.get("id") or "").strip()
    if not re.fullmatch(r"workflow_[0-9a-zA-Z]+", workflow_id):
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT w.workflow_id,w.status,g.title,g.objective,g.context_type,g.context_id,g.context_json
            FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE w.workflow_id=?
            """,
            (workflow_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    workflow_context = _loads(row["context_json"], {}) or {}
    job_facts: dict[str, Any] = {}
    job_id = int(row["context_id"] or 0) if str(row["context_type"] or "") == "job" else 0
    if job_id:
        try:
            job_facts = self._copilot_focus_context_facts({"type": "job", "id": job_id})
        except sqlite3.Error:
            # Strategy patch tests intentionally use a minimal workflow-only schema.
            job_facts = {}
    return {
        "context": {"type": "workflow", "id": workflow_id},
        "workflow": {
            "workflow_id": workflow_id,
            "title": str(row["title"] or workflow_id),
            "objective": str(row["objective"] or ""),
            "status": str(row["status"] or ""),
            "context": workflow_context,
        },
        "client": str(job_facts.get("client") or ""),
        "job": dict(job_facts.get("job") or {}),
        "candidate": {},
    }


def _workflow_strategy_question(message: str, context: dict[str, Any]) -> bool:
    """Return whether the user is asking to read the selected workflow strategy."""
    if str(context.get("type") or "") != "workflow" or not context.get("id"):
        return False
    normalized = "".join(str(message or "").lower().split())
    strategy_terms = ("寻访策略", "搜索策略", "搜人策略")
    if not any(term in normalized for term in strategy_terms):
        return False
    mutation_terms = (
        "修改", "调整", "优化", "新增", "增加", "补充", "删除", "去掉", "替换", "改成", "应用",
    )
    return not any(term in normalized for term in mutation_terms)


def _compact_workflow_context(workflow_payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the current workflow facts needed by Copilot without sending full artifacts."""
    workflow = dict(workflow_payload.get("workflow") or {})
    goal = dict(workflow_payload.get("goal") or {})
    steps = [
        {
            "id": step.get("id"),
            "capability_id": step.get("capability_id"),
            "label": step.get("business_label"),
            "status": step.get("status"),
            "risk_level": step.get("risk_level"),
        }
        for step in (workflow_payload.get("steps") or [])
    ]
    approvals = [
        {
            "approval_id": approval.get("approval_id"),
            "action_type": approval.get("action_type"),
            "status": approval.get("status"),
            "risk_level": approval.get("risk_level"),
            "preflight": approval.get("preflight") or {},
        }
        for approval in (workflow_payload.get("approvals") or [])
    ]
    artifacts = workflow_payload.get("artifacts") or []
    strategy_artifact = next(
        (
            artifact for artifact in artifacts
            if artifact.get("artifact_type") == "search_strategy"
            and artifact.get("validation_status") == "passed"
        ),
        None,
    )
    strategy: dict[str, Any] | None = None
    if strategy_artifact:
        metadata = strategy_artifact.get("metadata") if isinstance(strategy_artifact.get("metadata"), dict) else {}
        plan = metadata.get("plan") if isinstance(metadata.get("plan"), dict) else {}
        strategy_v2_payload = metadata.get("strategy_v2") if isinstance(metadata.get("strategy_v2"), dict) else {}
        review_gates = plan.get("review_gates") if isinstance(plan.get("review_gates"), dict) else {}
        channels = plan.get("channels") if isinstance(plan.get("channels"), dict) else {}
        strategy = {
            "artifact_id": strategy_artifact.get("artifact_id"),
            "validation_status": strategy_artifact.get("validation_status"),
            "model": ((plan.get("generation") or {}).get("model") if isinstance(plan.get("generation"), dict) else ""),
            "summary": str(plan.get("strategy_summary") or ""),
            "channels": {
                channel: [
                    {
                        "query": str(item.get("query") or ""),
                        "purpose": str(item.get("purpose") or ""),
                    }
                    for item in items if isinstance(item, dict) and item.get("query")
                ]
                for channel, items in channels.items() if isinstance(items, list)
            },
            "target_companies": list(plan.get("target_companies") or []),
            "hard_requirements": list(review_gates.get("hard_requirements") or []),
            "negative_rules": list(review_gates.get("negative_rules") or []),
            "risk_points": list(review_gates.get("risk_points") or []),
            "input_level": strategy_v2_payload.get("input_level"),
            "missing_anchors": list(strategy_v2_payload.get("missing_anchors") or []),
            "keyword_groups": list(strategy_v2_payload.get("step4_keyword_groups") or []),
        }
    return {
        "goal": {
            "goal_id": goal.get("goal_id"),
            "title": goal.get("title"),
            "objective": goal.get("objective"),
        },
        "workflow": {
            "workflow_id": workflow.get("workflow_id"),
            "status": workflow.get("status"),
            "current_stage": workflow.get("current_stage"),
        },
        "plan_ref": dict(workflow_payload.get("plan_ref") or {}),
        "progress": dict(workflow_payload.get("progress") or {}),
        "steps": steps,
        "approvals": approvals,
        "strategy": strategy,
    }


def _format_workflow_strategy_answer(workflow_context: dict[str, Any], *, expanded: bool = False) -> str:
    strategy = workflow_context.get("strategy") if isinstance(workflow_context.get("strategy"), dict) else None
    workflow = workflow_context.get("workflow") if isinstance(workflow_context.get("workflow"), dict) else {}
    if not strategy:
        return "结论：这个任务还没有通过校验的寻访策略。\n\n下一步：先完成“生成多渠道寻访策略”步骤。"

    channels = strategy.get("channels") if isinstance(strategy.get("channels"), dict) else {}
    liepin = channels.get("liepin") if isinstance(channels.get("liepin"), list) else []
    xsaas = channels.get("xsaas") if isinstance(channels.get("xsaas"), list) else []
    pending_r3 = any(
        approval.get("status") == "pending" and approval.get("risk_level") == "R3"
        for approval in (workflow_context.get("approvals") or [])
    )
    approval_text = "当前待 R3 审批，尚未执行外部搜索。" if pending_r3 else f"当前工作流状态：{workflow.get('status') or '未知'}。"

    def values(items: list[dict[str, Any]], key: str, limit: int) -> str:
        result = [str(item.get(key) or "").strip() for item in items if isinstance(item, dict)]
        result = [item for item in result if item]
        if expanded:
            limit = len(result)
        suffix = f"；另有 {len(result) - limit} 项" if len(result) > limit else ""
        return "；".join(result[:limit]) + suffix

    companies = [str(item).strip() for item in (strategy.get("target_companies") or []) if str(item).strip()]
    hard_requirements = [
        {"value": item} for item in (strategy.get("hard_requirements") or []) if str(item).strip()
    ]
    negative_rules = [
        {"value": item} for item in (strategy.get("negative_rules") or []) if str(item).strip()
    ]
    risk_points = [
        {"value": item} for item in (strategy.get("risk_points") or []) if str(item).strip()
    ]
    summary = str(strategy.get("summary") or "").strip() or "按当前通过校验的 strategy_v2 执行。"
    lines = [
        f"结论：{summary}{approval_text}",
        f"猎聘 {len(liepin)} 组：{values(liepin, 'query', 6) or '未配置'}",
        f"X-SaaS {len(xsaas)} 组：{values(xsaas, 'query', 6) or '未配置'}",
    ]
    if companies:
        company_limit = len(companies) if expanded else 10
        lines.append(f"目标公司：{'、'.join(companies[:company_limit])}" + (f"等 {len(companies)} 家" if len(companies) > company_limit else ""))
    if hard_requirements:
        lines.append(f"硬条件：{values(hard_requirements, 'value', 5)}")
    if negative_rules:
        lines.append(f"排除：{values(negative_rules, 'value', 4)}")
    if risk_points:
        lines.append(f"风险：{values(risk_points, 'value', 4)}")
    lines.append("下一步：批准后只搜索、排重并进入待复核，不发送消息。" if pending_r3 else "下一步：按工作流当前状态继续。")
    return "\n\n".join(lines)


def _format_context_mismatch_answer(
    conflicts: list[dict[str, Any]], *, floating_compact: bool = False
) -> str:
    """Ask before answering when the visible/focused object conflicts with the user's explicit client."""
    mismatch = next((item for item in conflicts if item.get("type") == "context_client_mismatch"), None)
    if not mismatch:
        return ""
    selected_client = str(mismatch.get("selected_client") or "当前上下文").strip()
    mentioned = [
        str(item or "").strip()
        for item in (mismatch.get("mentioned_clients") or [])
        if str(item or "").strip()
    ]
    mentioned_label = "、".join(mentioned) or "你提到的客户"
    if floating_compact:
        return (
            f"对象不一致：当前是{selected_client}，你提到{mentioned_label}。\n\n"
            "下一步：先确认要问哪个客户/岗位。"
        )
    return (
        f"我这里的上下文对象不一致：当前页面/会话焦点是「{selected_client}」，"
        f"但你这句提到「{mentioned_label}」。\n\n"
        "请先确认要问哪个客户或岗位，我再按对应上下文回答。"
    )


def _copilot_context_facts(self, context: dict[str, Any]) -> dict[str, Any]:
    if str(context.get("type") or "") == "workflow":
        return self._copilot_workflow_context_facts(context)
    return self._copilot_focus_context_facts(context)


def _copilot_context_from_focus(
    self, session_id: str, message: str, selected: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    focus = self.get_copilot_focus(session_id)
    current_clients = self._mentioned_client_names(message)
    current_jobs = self._mentioned_jobs_for_copilot(message)
    conflicts: list[dict[str, Any]] = []
    if len(current_clients) > 1:
        conflicts.append({"type": "ambiguous_client", "candidates": current_clients[:5]})
    if len(current_jobs) > 1:
        conflicts.append(
            {
                "type": "ambiguous_job",
                "candidates": [
                    {"id": item.get("id"), "client": item.get("client"), "job": item.get("job")}
                    for item in current_jobs[:5]
                ],
            }
        )
    if conflicts:
        return dict(selected), conflicts

    selected_facts = self._copilot_context_facts(selected)
    if selected_facts and current_clients and selected_facts.get("client") not in current_clients:
        conflicts.append({
            "type": "context_client_mismatch",
            "selected_client": selected_facts.get("client"),
            "mentioned_clients": current_clients[:5],
        })
        return {"type": "global", "id": None, "page": selected.get("page") or "overview", "filters": {}}, conflicts
    # 模糊结果追问（"寻访结果呢"类，未提及岗位/客户名）优先会话焦点岗位，
    # 避免前端残留页面 context 把主线串到其他岗位（2026-08-07 长越→电源专家串台）。
    if (
        selected_facts
        and selected.get("type") == "job"
        and not current_jobs
        and not current_clients
        and re.search(r"(?:结果|进展|情况|怎么样|如何)", message)
        and focus
    ):
        focus_context = focus.get("context") if isinstance(focus.get("context"), dict) else {}
        if (
            focus_context.get("type") == "job"
            and focus_context.get("id")
            and str(selected.get("id")) != str(focus_context.get("id"))
            and float(focus.get("confidence") or 0) >= 0.7
        ):
            return {
                "type": "job", "id": int(focus_context["id"]),
                "page": "positions", "filters": {},
            }, []
    if selected_facts:
        return dict(selected), []
    if len(current_jobs) == 1:
        return {
            "type": "job", "id": int(current_jobs[0]["id"]),
            "page": selected.get("page") or "positions", "filters": {},
        }, []
    if not focus:
        return dict(selected), []
    focus_context = focus.get("context") if isinstance(focus.get("context"), dict) else {}
    focus_client = str(focus.get("client") or "")
    if current_clients and focus_client not in current_clients:
        return dict(selected), []
    selected_candidate_id: int | None = None
    if str(selected.get("type") or "") == "candidate":
        try:
            selected_candidate_id = int(selected.get("id") or 0) or None
        except (TypeError, ValueError):
            selected_candidate_id = None
    focus_candidate = focus.get("candidate") if isinstance(focus.get("candidate"), dict) else {}
    focus_candidate_id = focus_candidate.get("id") or (
        focus_context.get("id") if focus_context.get("type") == "candidate" else None
    )
    try:
        focus_candidate_id = int(focus_candidate_id or 0) or None
    except (TypeError, ValueError):
        focus_candidate_id = None
    if (
        selected_candidate_id
        and focus_candidate_id
        and selected_candidate_id != focus_candidate_id
    ):
        # 消息明确附带了与旧焦点不同的候选人页面上下文：页面事实优先，
        # continuation 恢复让位，不再复活旧候选人焦点。
        return dict(selected), []
    continuation = _is_short_ack(message) or _new_candidate_outreach_requested(message) or _continued_sourcing_requested(message) or any(
        token in message
        for token in ("继续", "刚才", "之前", "那个", "这个", "当前", "上述", "按刚才", "按之前", "按此", "再找")
    )
    if (
        continuation
        and focus_context.get("type") in {"job", "candidate"}
        and focus_context.get("id")
        and float(focus.get("confidence") or 0) >= 0.7
    ):
        return {
            "type": str(focus_context["type"]),
            "id": int(focus_context["id"]),
            "page": "positions" if focus_context["type"] == "job" else "candidates",
            "filters": {},
        }, []
    return dict(selected), []


def _copilot_workflow_outcome_context(
    self,
    message: str,
    selected: dict[str, Any],
    mentioned_jobs: list[dict[str, Any]],
    existing_focus: dict[str, Any] | None,
) -> dict[str, Any]:
    """为 Copilot 注入所涉岗位的寻访轮次业务终态与渠道漏斗（全部 DB 实读）。

    岗位解析顺序：当前页面岗位 → 消息唯一提及岗位 → 会话焦点岗位。
    每轮给出 business_outcome 中文语义（复用 classify_business_outcome 口径）
    与 agent_sourcing_funnel 行；历史无漏斗行时标注"该轮未记录渠道明细"，
    不向 LLM 提供任何可编造数字的空间。
    """
    job_id: int | None = None
    if str(selected.get("type") or "") == "job":
        try:
            job_id = int(selected.get("id") or 0) or None
        except (TypeError, ValueError):
            job_id = None
    if job_id is None and len(mentioned_jobs) == 1:
        try:
            job_id = int(mentioned_jobs[0].get("id") or 0) or None
        except (TypeError, ValueError):
            job_id = None
    if job_id is None and existing_focus:
        focus_context = existing_focus.get("context") if isinstance(existing_focus.get("context"), dict) else {}
        if (
            focus_context.get("type") == "job"
            and float(existing_focus.get("confidence") or 0) >= 0.7
        ):
            try:
                job_id = int(focus_context.get("id") or 0) or None
            except (TypeError, ValueError):
                job_id = None
    if job_id is None:
        return {}

    asked_round: int | None = None
    round_match = re.search(r"第\s*(\d+)\s*轮", str(message or ""))
    if round_match:
        asked_round = int(round_match.group(1))

    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT w.workflow_id,w.status,w.business_outcome,w.created_at,w.updated_at,
                   g.objective,g.context_json
            FROM agent_workflows w JOIN agent_goals g ON g.goal_id=w.goal_id
            WHERE g.context_type='job' AND g.context_id=?
              AND w.status NOT IN ('cancelled','superseded')
            ORDER BY w.created_at ASC, w.id ASC
            """,
            (job_id,),
        ).fetchall()
        rounds: list[dict[str, Any]] = []
        for row in rows:
            workflow_id = str(row["workflow_id"])
            context = _loads(row["context_json"], {})
            # 轮次编号按用户可见时间线（取消/被取代不计）；classify_business_outcome
            # 的寻访判定只用于标注 is_sourcing，不用于剔除轮次——
            # 否则"再多找些人选"这类措辞的寻访轮会被跳过，编号与用户口径错位。
            is_sourcing = sourcing_target_stats(conn, row["objective"], context, workflow_id) is not None
            outcome = str(row["business_outcome"] or "") or classify_business_outcome(conn, workflow_id)
            funnel_rows = conn.execute(
                """
                SELECT channel,status,query_count,recall_count,extracted_count,dedupe_count,
                       unique_count,detail_complete,detail_partial,detail_failed,
                       intake_duplicate_count,intake_new_count,assessed_count,high_score_count,
                       zero_attribution,error
                FROM agent_sourcing_funnel WHERE workflow_id=? ORDER BY channel ASC, id ASC
                """,
                (workflow_id,),
            ).fetchall()
            channels: list[dict[str, Any]] = []
            channel_segments: list[str] = []
            for funnel in funnel_rows:
                attribution = str(funnel["zero_attribution"] or "") or None
                attribution_label = ZERO_RESULT_ATTRIBUTION_LABELS.get(attribution or "")
                error_text = str(funnel["error"] or "").strip()
                channel = {
                    "channel": str(funnel["channel"] or ""),
                    "status": str(funnel["status"] or ""),
                    "query_count": int(funnel["query_count"] or 0),
                    "recall_count": int(funnel["recall_count"] or 0),
                    "extracted_count": int(funnel["extracted_count"] or 0),
                    "dedupe_count": int(funnel["dedupe_count"] or 0),
                    "unique_count": int(funnel["unique_count"] or 0),
                    "detail_complete": int(funnel["detail_complete"] or 0),
                    "detail_partial": int(funnel["detail_partial"] or 0),
                    "detail_failed": int(funnel["detail_failed"] or 0),
                    "intake_duplicate_count": int(funnel["intake_duplicate_count"] or 0),
                    "intake_new_count": int(funnel["intake_new_count"] or 0),
                    "assessed_count": int(funnel["assessed_count"] or 0),
                    "high_score_count": int(funnel["high_score_count"] or 0),
                    "zero_attribution": attribution,
                    "zero_attribution_label": attribution_label or None,
                    "error": error_text[-160:] or None,
                }
                channels.append(channel)
                # 与前端漏斗展示同一行文格式（T2），逐轮绑定数字，防止跨轮引用
                segment = (
                    f"{channel['channel']}：查询 {channel['query_count']} 组 → 召回 {channel['recall_count']}"
                    f" → 抽取 {channel['extracted_count']} → 排重后 {channel['unique_count']}"
                    f" → 详情（完整 {channel['detail_complete']} / 部分 {channel['detail_partial']} / 失败 {channel['detail_failed']}）"
                    f" → 入库新增 {channel['intake_new_count']}（排重命中 {channel['intake_duplicate_count']}）"
                    f" → 评估 {channel['assessed_count']}（高分 {channel['high_score_count']}）"
                )
                if attribution_label:
                    segment += f"；0 召回原因：{attribution_label}"
                channel_segments.append(segment)
            round_index = len(rounds) + 1
            outcome_label = BUSINESS_OUTCOME_LABELS.get(outcome or "")
            headline = outcome_label or ("寻访轮次" if is_sourcing else "非寻访类工作流")
            detail_text = "；".join(channel_segments) if channel_segments else "该轮未记录渠道明细"
            rounds.append(
                {
                    "round_index": round_index,
                    "workflow_id": workflow_id,
                    "status": str(row["status"] or ""),
                    "is_sourcing": is_sourcing,
                    "business_outcome": outcome,
                    "business_outcome_label": outcome_label or None,
                    "updated_at": str(row["updated_at"] or ""),
                    "channels": channels,
                    "funnel_note": "" if channels else "该轮未记录渠道明细",
                    "summary_text": f"第 {round_index} 轮（{str(row['updated_at'] or '')}）：{headline}；{detail_text}",
                }
            )
    finally:
        conn.close()
    if not rounds:
        return {}
    return {
        "job_id": job_id,
        "asked_round": asked_round,
        "rounds": rounds[-8:],
        "semantics": (
            "completed_target_met/completed_needs_review/completed_pool_insufficient 均为本轮完成（仅达标情况不同），"
            "只有 failed_technical 是技术失败；每轮 summary_text 是该轮的完整事实，"
            "回答某一轮时只能用该轮的数字，funnel_note 标注未记录渠道明细的轮次不得借用其他轮次数字"
        ),
    }


def _persist_copilot_focus(
    self,
    session_id: str,
    message: str,
    selected: dict[str, Any],
    *,
    structured: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    previous = self.get_copilot_focus(session_id) or {}
    structured = dict(structured or {})
    conflicts = list(conflicts or [])
    facts = self._copilot_context_facts(selected)
    mentioned_clients = self._mentioned_client_names(message)
    mentioned_jobs = self._mentioned_jobs_for_copilot(message)
    if not facts and len(mentioned_jobs) == 1:
        facts = self._copilot_focus_context_facts({"type": "job", "id": mentioned_jobs[0].get("id")})

    context_value = dict(previous.get("context") or {"type": "global", "id": None})
    client = str(previous.get("client") or "")
    job = dict(previous.get("job") or {})
    candidate = dict(previous.get("candidate") or {})
    confidence = float(previous.get("confidence") or 0)
    selected_candidate_id: int | None = None
    if str(selected.get("type") or "") == "candidate":
        try:
            selected_candidate_id = int(selected.get("id") or 0) or None
        except (TypeError, ValueError):
            selected_candidate_id = None
    previous_candidate_id = candidate.get("id") or (
        context_value.get("id") if context_value.get("type") == "candidate" else None
    )
    try:
        previous_candidate_id = int(previous_candidate_id or 0) or None
    except (TypeError, ValueError):
        previous_candidate_id = None
    candidate_conflict = bool(
        selected_candidate_id
        and previous_candidate_id
        and selected_candidate_id != previous_candidate_id
    )
    if facts:
        # 页面事实优先：新候选人已入库时直接采用新页面候选人为焦点。
        context_value = dict(facts["context"])
        client = str(facts.get("client") or "")
        job = dict(facts.get("job") or {})
        candidate = dict(facts.get("candidate") or {})
        confidence = 1.0
    elif candidate_conflict:
        # 页面候选人已切换但新候选人未入库：清空候选人焦点并降权
        # （confidence 低于 continuation 阈值 0.7），不再钉住旧候选人。
        context_value = {"type": "global", "id": None}
        job = {}
        candidate = {}
        confidence = 0.4
    elif len(mentioned_clients) == 1 and mentioned_clients[0] != client:
        context_value = {"type": "global", "id": None}
        client = mentioned_clients[0]
        job = {}
        candidate = {}
        confidence = 0.85

    grounding = selected.get("grounding") if isinstance(selected.get("grounding"), dict) else {}
    direction_text = "\n".join([message, json.dumps(grounding, ensure_ascii=False)])
    directions = list(previous.get("directions") or [])
    for label, tokens in (
        ("PC", ("PC", "pc", "电脑")),
        ("服务器", ("服务器", "server", "Server")),
        ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
    ):
        if any(token in direction_text for token in tokens) and label not in directions:
            directions.append(label)

    attachments = list(previous.get("attachments") or [])
    uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
    for item in uploaded.get("items") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file_name") or "").strip()[:180]
        if name and name not in attachments:
            attachments.append(name)
    for name in grounding.get("attachment_names") or []:
        name = str(name or "").strip()[:180]
        if name and name not in attachments:
            attachments.append(name)

    understanding = structured.get("intent_understanding") if isinstance(structured.get("intent_understanding"), dict) else {}
    turn_decision = structured.get("turn_decision") if isinstance(structured.get("turn_decision"), dict) else {}
    effective_constraints = [
        item
        for item in (turn_decision.get("effective_constraints") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    if effective_constraints:
        constraint_ledger = effective_constraints[-24:]
        constraints = [str(item.get("quote") or "").strip() for item in constraint_ledger]
    elif turn_decision.get("constraint_changes"):
        constraint_ledger = []
        constraints = []
    else:
        constraint_ledger = list(previous.get("constraint_ledger") or [])
        constraints = list(previous.get("constraints") or [])

    evidence = list(previous.get("evidence") or [])
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if facts:
        evidence.append({"source": "current_context", "type": context_value.get("type"), "id": context_value.get("id"), "at": stamp})
    if mentioned_clients:
        evidence.append({"source": "explicit_message", "clients": mentioned_clients[:5], "at": stamp})
    if grounding:
        evidence.append({"source": grounding.get("source") or "workflow_grounding", "job_id": grounding.get("job_id"), "at": stamp})
    if attachments:
        evidence.append({"source": "session_attachment", "files": attachments[-3:], "at": stamp})

    semantic_action = str(understanding.get("action") or "")
    pending_workflow = (
        dict(previous.get("pending_workflow") or {})
        if isinstance(previous.get("pending_workflow"), dict)
        else {}
    )
    current_workflow = (
        dict(previous.get("current_workflow") or {})
        if isinstance(previous.get("current_workflow"), dict)
        else {}
    )
    workflow_intent = (
        structured.get("workflow_intent")
        if isinstance(structured.get("workflow_intent"), dict)
        else None
    )
    if workflow_intent is not None:
        pending_workflow = dict(workflow_intent) if workflow_intent.get("status") == "planned" else {}
        current_workflow = (
            dict(workflow_intent)
            if workflow_intent.get("status") not in TERMINAL_WORKFLOW_STATUSES
            else {}
        )
    elif turn_decision.get("effect") == "cancel_plan" or str(understanding.get("speech_act") or "") == "cancel":
        pending_workflow = {}
        current_workflow = {}
    active_workflow = pending_workflow or current_workflow
    action = semantic_action if semantic_action in _COPILOT_SEMANTIC_ACTIONS and semantic_action != "none" else ""
    if not action and active_workflow:
        action = str(active_workflow.get("action") or previous.get("action") or "")
    elif not action and _is_short_ack(message):
        action = str(previous.get("action") or "") if active_workflow else ""
    semantic_objective = str(understanding.get("objective") or "").strip()
    objective = semantic_objective if action else ""
    if active_workflow and not objective:
        objective = str(active_workflow.get("objective") or previous.get("objective") or "")
    focus = {
        "context": context_value,
        "client": client,
        "job": job,
        "candidate": candidate,
        "objective": objective,
        "action": action,
        "directions": directions[-6:],
        "attachments": attachments[-8:],
        "constraints": constraints[-8:],
        "constraint_ledger": constraint_ledger[-24:],
        "understanding": understanding,
        "turn_decision": turn_decision,
        "pending_workflow": pending_workflow,
        "current_workflow": current_workflow,
        "confidence": round(confidence, 3),
    }
    previous_state = previous.get("conversation_state") if isinstance(previous.get("conversation_state"), dict) else {}
    if not previous_state and active_workflow:
        previous_state = {
            "active_goal": {
                "action": str(active_workflow.get("action") or action),
                "objective": str(active_workflow.get("objective") or objective),
                "status": str(active_workflow.get("status") or "active"),
                "workflow_id": str(active_workflow.get("workflow_id") or ""),
            },
            "pending_plan": dict(pending_workflow),
            "constraints": list(constraint_ledger),
        }
    conversation_state = build_context_state(
        previous_state,
        message=message,
        context=selected,
        business_focus=focus,
        understanding=understanding,
        decision=turn_decision,
        workflow_intent=workflow_intent,
        now=stamp,
    )
    focus["conversation_state"] = conversation_state
    revision = int(previous.get("revision") or 0) + 1
    conn = self._connect()
    try:
        conn.execute(
            """
            INSERT INTO agent_copilot_focus
            (session_id,revision,context_type,context_id,client,job_id,candidate_id,action,
             confidence,focus_json,evidence_json,conflicts_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))
            ON CONFLICT(session_id) DO UPDATE SET
                revision=excluded.revision,context_type=excluded.context_type,context_id=excluded.context_id,
                client=excluded.client,job_id=excluded.job_id,candidate_id=excluded.candidate_id,
                action=excluded.action,confidence=excluded.confidence,focus_json=excluded.focus_json,
                evidence_json=excluded.evidence_json,conflicts_json=excluded.conflicts_json,
                updated_at=datetime('now','localtime')
            """,
            (
                session_id, revision, context_value.get("type") or "global", context_value.get("id"),
                client, job.get("id"), candidate.get("id"), focus["action"], focus["confidence"],
                _dumps(focus), _dumps(evidence[-16:]), _dumps(conflicts[-8:]),
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_copilot_state(session_id,revision,state_json,updated_at)
            VALUES (?,?,?,datetime('now','localtime'))
            ON CONFLICT(session_id) DO UPDATE SET
                revision=excluded.revision,state_json=excluded.state_json,
                updated_at=datetime('now','localtime')
            """,
            (session_id, int(conversation_state.get("revision") or 1), _dumps(conversation_state)),
        )
        conn.commit()
    finally:
        conn.close()
    return self.get_copilot_focus(session_id) or focus


def get_copilot_session(self, session_id: str, limit: int = 100) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"ok": True, "session_id": "", "messages": [], "business_focus": None}
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT role,content,context_type,context_id,structured_json,created_at
            FROM agent_copilot_messages WHERE session_id=? ORDER BY id DESC LIMIT ?
            """,
            (session_id, max(1, min(int(limit or 100), 200))),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            structured = _loads(row["structured_json"], {})
            uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
            uploaded_items = []
            for item in (uploaded.get("items") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                uploaded_items.append({
                    key: item.get(key)
                    for key in (
                        "attachment_id", "file_name", "file_type", "mime_type", "size_bytes",
                        "content_available", "truncated", "is_image", "status",
                    )
                })
            message_context = {"type": row["context_type"], "id": row["context_id"]}
            if uploaded_items:
                message_context["uploaded_attachments"] = uploaded_items
            messages.append(
                {
                    "role": row["role"], "content": row["content"],
                    "context": message_context,
                    "references": structured.get("references") or [],
                    "suggested_actions": structured.get("suggested_actions") or [],
                    "skill_runs": structured.get("skill_runs") or [],
                    "goal": structured.get("goal"),
                    "workflow": structured.get("workflow"),
                    "plan_ref": structured.get("plan_ref"),
                    "plan_summary": structured.get("plan_summary") or [],
                    "workflow_progress": structured.get("workflow_progress"),
                    "business_focus": structured.get("business_focus"),
                    "turn_decision": structured.get("turn_decision"),
                    # R9/R12-b：透传持久化的 pending_intent，浮窗恢复会话时可重渲染确认卡
                    #（确认/取消终态是 UI 本地态；过期或已执行的意图确认时会走 409 漂移路径）。
                    "pending_intent": structured.get("pending_intent"),
                    "action_card": structured.get("action_card"),
                    "action_cards": structured.get("action_cards") or [],
                    "model_participation": structured.get("model_participation"),
                    "analysis_card": structured.get("analysis_card"),
                    # 策略建议补丁：浮窗恢复会话时可重渲染「应用到策略」操作栏
                    "strategy_patch": structured.get("strategy_patch"),
                    "strategy_patch_applied": bool(structured.get("strategy_patch_applied")),
                    "strategy_patch_revised_workflow_id": structured.get("strategy_patch_revised_workflow_id"),
                    "strategy_patch_reverted": bool(structured.get("strategy_patch_reverted")),
                    "strategy_patch_restored_workflow_id": structured.get("strategy_patch_restored_workflow_id"),
                    "created_at": row["created_at"],
                }
            )
        return {
            "ok": True,
            "session_id": session_id,
            "messages": messages,
            "business_focus": self.get_copilot_focus(session_id),
        }
    finally:
        conn.close()


def list_copilot_sessions(
    self,
    limit: int = 30,
    query: str = "",
    include_archived: bool = False,
) -> dict[str, Any]:
    query = " ".join(str(query or "").split())[:120]
    # 转义 LIKE 通配符，避免 q=% / q=_ 匹配全部会话
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    search = f"%{escaped}%"
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            WITH rollup AS (
                SELECT messages.session_id,
                       COUNT(*) AS message_count,
                       MAX(messages.id) AS latest_id,
                       MAX(messages.created_at) AS message_updated_at,
                       (SELECT content FROM agent_copilot_messages first_user
                        WHERE first_user.session_id=messages.session_id AND first_user.role='user'
                        ORDER BY first_user.id LIMIT 1) AS derived_title,
                       (SELECT content FROM agent_copilot_messages latest_message
                        WHERE latest_message.session_id=messages.session_id
                        ORDER BY latest_message.id DESC LIMIT 1) AS preview,
                       (SELECT context_type FROM agent_copilot_messages latest_context
                        WHERE latest_context.session_id=messages.session_id
                        ORDER BY latest_context.id DESC LIMIT 1) AS context_type,
                       (SELECT context_id FROM agent_copilot_messages latest_context
                        WHERE latest_context.session_id=messages.session_id
                        ORDER BY latest_context.id DESC LIMIT 1) AS context_id
                FROM agent_copilot_messages messages
                GROUP BY messages.session_id
            )
            SELECT rollup.session_id, rollup.message_count, rollup.latest_id,
                   COALESCE(metadata.updated_at, rollup.message_updated_at) AS updated_at,
                   COALESCE(NULLIF(metadata.title, ''), rollup.derived_title) AS title,
                   rollup.preview, rollup.context_type, rollup.context_id,
                   metadata.archived_at,
                   focus.revision AS focus_revision,
                   focus.context_type AS focus_context_type,
                   focus.context_id AS focus_context_id,
                   focus.client AS focus_client,
                   focus.action AS focus_action,
                   focus.confidence AS focus_confidence,
                   focus.focus_json,
                   focus.evidence_json AS focus_evidence_json,
                   focus.conflicts_json AS focus_conflicts_json,
                   focus.updated_at AS focus_updated_at
            FROM rollup
            LEFT JOIN agent_copilot_sessions metadata ON metadata.session_id=rollup.session_id
            LEFT JOIN agent_copilot_focus focus ON focus.session_id=rollup.session_id
            WHERE (? OR metadata.archived_at IS NULL)
              AND (? = '' OR COALESCE(NULLIF(metadata.title, ''), rollup.derived_title, '') LIKE ? ESCAPE '\\'
                   OR COALESCE(rollup.preview, '') LIKE ? ESCAPE '\\')
            ORDER BY latest_id DESC
            LIMIT ?
            """,
            (int(bool(include_archived)), query, search, search, max(1, min(int(limit or 30), 100))),
        ).fetchall()
        sessions = []
        for row in rows:
            item = _row(row)
            item["title"] = str(item.get("title") or "未命名对话")[:80]
            item["preview"] = " ".join(str(item.get("preview") or "").split())[:120]
            item["archived"] = bool(item.pop("archived_at", None))
            item["business_focus"] = _copilot_focus_from_joined_row(row)
            for key in [key for key in item if key.startswith("focus_") or key == "focus_json"]:
                item.pop(key, None)
            sessions.append(item)
        return {"ok": True, "sessions": sessions}
    finally:
        conn.close()


def update_copilot_session(
    self,
    session_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
    clear_focus: bool = False,
) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    normalized_title = " ".join(str(title or "").split()) if title is not None else None
    if title is not None and not normalized_title:
        raise ValueError("Agent task title cannot be empty")
    conn = self._connect()
    try:
        # 存在性口径与列表查询一致：仅 focus/metadata 中有记录但没有消息的会话
        # 在列表里永远不可见，PATCH 应按不存在处理（与 GET detail 的 404 语义一致）。
        exists = conn.execute(
            "SELECT 1 FROM agent_copilot_messages WHERE session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
        if exists is None:
            raise LookupError("Agent task not found")
        conn.execute(
            """INSERT INTO agent_copilot_sessions(session_id,title,archived_at,updated_at)
               VALUES (?, ?, CASE WHEN ? THEN datetime('now','localtime') ELSE NULL END, datetime('now','localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 title=CASE WHEN ? THEN excluded.title ELSE agent_copilot_sessions.title END,
                 archived_at=CASE WHEN ? THEN excluded.archived_at ELSE agent_copilot_sessions.archived_at END,
                 updated_at=datetime('now','localtime')""",
            (
                session_id,
                normalized_title,
                int(archived is True),
                int(title is not None),
                int(archived is not None),
            ),
        )
        if clear_focus:
            conn.execute("DELETE FROM agent_copilot_focus WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM agent_copilot_state WHERE session_id=?", (session_id,))
        conn.commit()
        row = conn.execute(
            """SELECT metadata.session_id,
                      COALESCE(NULLIF(metadata.title, ''),
                        (SELECT content FROM agent_copilot_messages first_user
                         WHERE first_user.session_id=metadata.session_id AND first_user.role='user'
                         ORDER BY first_user.id LIMIT 1)) AS title,
                      metadata.archived_at,
                      focus.revision AS focus_revision,
                      focus.context_type AS focus_context_type,
                      focus.context_id AS focus_context_id,
                      focus.client AS focus_client,
                      focus.action AS focus_action,
                      focus.confidence AS focus_confidence,
                      focus.focus_json,
                      focus.evidence_json AS focus_evidence_json,
                      focus.conflicts_json AS focus_conflicts_json,
                      focus.updated_at AS focus_updated_at
               FROM agent_copilot_sessions metadata
               LEFT JOIN agent_copilot_focus focus ON focus.session_id=metadata.session_id
               WHERE metadata.session_id=?""",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "ok": True,
        "session_id": session_id,
        "title": str(row["title"] or "未命名对话")[:80],
        "archived": bool(row["archived_at"]),
        "business_focus": _copilot_focus_from_joined_row(row),
    }


def archive_all_copilot_sessions(self) -> dict[str, Any]:
    """Archive every visible Copilot task while preserving messages and focus evidence."""
    conn = self._connect()
    try:
        rows = conn.execute(
            """SELECT DISTINCT messages.session_id
                 FROM agent_copilot_messages messages
                 LEFT JOIN agent_copilot_sessions metadata ON metadata.session_id=messages.session_id
                WHERE metadata.archived_at IS NULL
                ORDER BY messages.session_id"""
        ).fetchall()
        session_ids = [str(row["session_id"]) for row in rows]
        conn.executemany(
            """INSERT INTO agent_copilot_sessions(session_id,title,archived_at,updated_at)
               VALUES (?, NULL, datetime('now','localtime'), datetime('now','localtime'))
               ON CONFLICT(session_id) DO UPDATE SET
                 archived_at=datetime('now','localtime'),
                 updated_at=datetime('now','localtime')""",
            [(session_id,) for session_id in session_ids],
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "archived_count": len(session_ids),
        "session_ids": session_ids,
    }


def _copilot_conversation_history(self, session_id: str, limit: int = 16) -> list[dict[str, str]]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return []
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT role,content FROM agent_copilot_messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY id DESC LIMIT ?
            """,
            (session_id, max(2, min(int(limit or 16), 24))),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"role": str(row["role"]), "content": str(row["content"] or "")[:1800]}
        for row in reversed(rows)
    ]


def _copilot_session_business_evidence(self, session_id: str, limit: int = 8) -> dict[str, Any]:
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"clients": [], "jobs": [], "directions": [], "attachment_names": []}
    focus = self.get_copilot_focus(session_id) or {}
    conn = self._connect()
    try:
        rows = conn.execute(
            """
            SELECT role,content,context_type,context_id,structured_json
            FROM agent_copilot_messages
            WHERE session_id=? AND role IN ('user','assistant')
            ORDER BY id DESC LIMIT ?
            """,
            (session_id, max(2, min(int(limit or 8), 16))),
        ).fetchall()
    finally:
        conn.close()

    text_parts: list[str] = []
    strong_client_parts: list[str] = []
    job_ids: list[int] = []
    attachment_names: list[str] = [str(item) for item in focus.get("attachments") or [] if str(item).strip()]

    def add_job_id(value: Any) -> None:
        try:
            job_id = int(value or 0)
        except (TypeError, ValueError):
            return
        if job_id > 0 and job_id not in job_ids:
            job_ids.append(job_id)

    focus_client = str(focus.get("client") or "").strip()
    if focus_client:
        strong_client_parts.append(focus_client)
    focus_job = focus.get("job") if isinstance(focus.get("job"), dict) else {}
    add_job_id(focus_job.get("id"))

    for row in rows:
        content = str(row["content"] or "")[:2400]
        if content:
            text_parts.append(content)
        if str(row["context_type"] or "") == "job":
            add_job_id(row["context_id"])
        structured = _loads(row["structured_json"], {})
        for item in structured.get("mentioned_jobs") or []:
            if isinstance(item, dict):
                add_job_id(item.get("id"))
                text_parts.extend([str(item.get("client") or ""), str(item.get("job") or "")])
                strong_client_parts.append(str(item.get("client") or ""))
        for item in structured.get("references") or []:
            if isinstance(item, dict) and item.get("type") == "job":
                add_job_id(item.get("id"))
                text_parts.extend([str(item.get("label") or ""), str(item.get("subtitle") or "")])
                strong_client_parts.append(str(item.get("subtitle") or ""))
        uploaded = structured.get("uploaded_attachment_evidence") if isinstance(structured.get("uploaded_attachment_evidence"), dict) else {}
        for item in uploaded.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("file_name") or "")[:180]
            if name and name not in attachment_names:
                attachment_names.append(name)
            text_parts.extend([name, str(item.get("extracted_text") or "")[:18000]])

    evidence_text = "\n".join(part for part in text_parts if part)
    if not job_ids:
        inferred_jobs = self._mentioned_jobs_for_copilot(evidence_text)
        if len(inferred_jobs) == 1:
            add_job_id(inferred_jobs[0].get("id"))
    strong_clients = self._mentioned_client_names("\n".join(strong_client_parts))
    clients = strong_clients or self._mentioned_client_names(evidence_text)
    directions = list(focus.get("directions") or [])
    directions.extend(
        label
        for label, tokens in (
            ("PC", ("PC", "pc", "电脑")),
            ("服务器", ("服务器", "server", "Server")),
            ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
        )
        if any(token in evidence_text for token in tokens) and label not in directions
    )
    jobs: list[dict[str, Any]] = []
    if job_ids:
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in job_ids)
            found = conn.execute(
                f"""
                SELECT j.id,c.name AS client,j.title AS job,j.status
                FROM jobs j JOIN clients c ON c.id=j.client_id
                WHERE j.id IN ({placeholders})
                """,
                job_ids,
            ).fetchall()
            indexed = {int(row["id"]): _row(row) for row in found}
            jobs = [indexed[job_id] for job_id in job_ids if job_id in indexed]
        finally:
            conn.close()
    return {
        "clients": clients,
        "jobs": jobs,
        "directions": directions,
        "attachment_names": attachment_names,
    }


def _ground_copilot_goal(
    self, message: str, selected: dict[str, Any], session_id: str
) -> tuple[dict[str, Any], dict[str, Any], str]:
    new_candidate_outreach = _new_candidate_outreach_requested(message)
    job_write = bool(
        any(token in message for token in ("更新", "拆分", "拆成", "分成", "新建", "建立", "归档", "关闭"))
        and any(token in message for token in ("岗位", "职位", "岗位库"))
    )
    sourcing = any(token in message for token in ("补池", "寻访", "找人", "候选人", "人选")) and any(
        token in message for token in ("补充", "继续", "再找", "搜索", "搜", "找", "寻访", "多渠道")
    )
    publishing = any(token in message for token in ("发布", "上架")) and any(
        token in message for token in ("岗位", "职位")
    )
    job_bound = job_write or sourcing or publishing or new_candidate_outreach
    if not job_bound:
        return dict(selected), {}, ""

    evidence = self._copilot_session_business_evidence(session_id)
    current_clients = self._mentioned_client_names(message)
    client_candidates = current_clients or evidence["clients"]
    client_candidates = list(dict.fromkeys(client_candidates))
    selected_job: dict[str, Any] = {}
    if selected.get("type") == "job" and selected.get("id"):
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status
                FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?
                """,
                (int(selected["id"]),),
            ).fetchone()
            selected_job = _row(row)
        finally:
            conn.close()
    if selected_job:
        client_candidates = [str(selected_job["client"])]

    target_job = selected_job
    current_jobs = self._mentioned_jobs_for_copilot(message)
    if not target_job and len(current_jobs) == 1:
        target_job = current_jobs[0]
    client = client_candidates[0] if len(client_candidates) == 1 else ""
    recent_jobs = [item for item in evidence["jobs"] if not client or item.get("client") == client]
    archive_reference = any(token in message for token in ("之前", "那个", "原来", "旧", "没拆分", "未拆分", "合并"))
    if not target_job and client and archive_reference:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status
                FROM jobs j JOIN clients c ON c.id=j.client_id
                WHERE c.name=? AND j.title LIKE '%技术市场%'
                ORDER BY j.id DESC
                """,
                (client,),
            ).fetchall()
        finally:
            conn.close()
        merged = [
            _row(row) for row in rows
            if any(token in str(row["job"] or "") for token in ("或PC", "服务器或", "/服务器", "／服务器"))
        ]
        if len(merged) == 1:
            target_job = merged[0]
    if not target_job and len(recent_jobs) == 1:
        target_job = recent_jobs[0]
    if target_job:
        client = str(target_job.get("client") or client)

    current_direction_text = message
    directions = [
        label
        for label, tokens in (
            ("PC", ("PC", "pc", "电脑")),
            ("服务器", ("服务器", "server", "Server")),
            ("ADAS", ("ADAS", "adas", "智驾", "辅助驾驶")),
        )
        if any(token in current_direction_text for token in tokens)
    ]
    continuation = archive_reference or _is_short_ack(message) or any(token in message for token in ("这个", "上述", "按刚才", "按之前", "按此"))
    if continuation:
        directions = list(dict.fromkeys([*directions, *evidence["directions"]]))

    missing: list[str] = []
    if not client:
        missing.append("客户")
    if any(token in message for token in ("归档", "关闭")) and not target_job:
        missing.append("要归档的岗位")
    if (sourcing or publishing or new_candidate_outreach) and not target_job:
        missing.append("唯一岗位")
    split_requested = any(token in message for token in ("拆分", "拆成", "分成", "三个", "分别"))
    if split_requested and not directions:
        missing.append("拆分方向")
    if missing:
        if new_candidate_outreach and not target_job:
            return dict(selected), {}, "你要为哪个岗位补充并触达新候选人？"
        known = ""
        if client_candidates:
            known = f"当前识别到客户候选：{'、'.join(client_candidates[:3])}。"
        clarification = (
            f"结论：还不能建立写入计划，缺少{'、'.join(missing)}。\n\n"
            f"{known}请补充{'、'.join(missing)}后再执行。"
        )
        return dict(selected), {}, clarification

    grounded = dict(selected)
    if target_job:
        grounded.update({"type": "job", "id": int(target_job["id"]), "page": "positions", "filters": {}})
    goal_inputs = {
        "client": client,
        "directions": directions,
        "archive_legacy": bool(any(token in message for token in ("归档", "旧", "没拆分", "未拆分", "合并"))),
    }
    grounded["goal_inputs"] = goal_inputs
    grounded["goal_grounding"] = {
        "source": "current_context" if selected_job else "recent_session_evidence",
        "client": client,
        "job_id": int(target_job["id"]) if target_job else None,
        "job": target_job.get("job") if target_job else "",
        "directions": directions,
        "attachment_names": evidence["attachment_names"][:3],
        "validated_against_v3": True,
    }
    return grounded, goal_inputs, ""


def _pending_strategy_clarification(self, session_id: str) -> dict[str, Any]:
    """本会话最近一条四锚点提问清单记录；仅 status=pending 时返回（S4-1）。"""
    session_id = str(session_id or "").strip()
    if not session_id:
        return {}
    conn = self._connect()
    try:
        row = conn.execute(
            """
            SELECT structured_json FROM agent_copilot_messages
            WHERE session_id=? AND role='assistant' AND structured_json LIKE '%strategy_clarification%'
            ORDER BY id DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    data = _loads(row["structured_json"], {}) if row else {}
    pending = data.get("strategy_clarification") if isinstance(data, dict) else None
    if isinstance(pending, dict) and pending.get("status") == "pending" and pending.get("job_id"):
        return pending
    return {}


def _sourcing_strategy_gate(
    self, goal_request: str, goal_context: dict[str, Any], *, floating_compact: bool = False
) -> dict[str, Any]:
    """S4-1 L3 提问门控（PRD §1 最高优先单点）：四锚点缺失 ≥2 且知识库无对应
    岗位原型时，不创建寻访工作流，改为输出四锚点提问清单。仅作用于寻访类目标。"""
    text = str(goal_request or "").lower()
    sourcing_like = any(token in text for token in ("补充", "补池", "寻访", "找人", "搜索", "搜人", "再找", "继续找", "多找")) or any(
        token in text for token in ("人选", "候选人")
    )
    if not sourcing_like or goal_context.get("type") != "job" or not goal_context.get("id"):
        return {"action": "proceed"}
    try:
        job = self.capability_runtime._job(goal_context)
    except ValueError:
        return {"action": "proceed"}
    archetype, match_trace = strategy_v2.match_job_archetype(job.get("client"), job.get("title"))
    classification = strategy_v2.classify_strategy_input(job, archetype=archetype)
    classification["trace"] = [*match_trace, *classification["trace"]]
    if archetype or len(classification.get("missing_anchors") or []) < 2:
        return {"action": "proceed"}
    answer = strategy_v2.build_clarification_answer(job, classification, floating_compact=floating_compact)
    pending = {
        "status": "pending",
        "job_id": int(goal_context["id"]),
        "client": str(job.get("client") or ""),
        "job": str(job.get("title") or ""),
        "original_objective": " ".join(str(goal_request or "").split()),
        "input_level": str(classification.get("input_level") or "L3"),
        "missing_anchors": list(classification.get("missing_anchors") or []),
        "questions": strategy_v2.build_anchor_questions(job, classification),
        "trace": list(classification.get("trace") or [])[-12:],
    }
    return {"action": "ask", "answer": answer, "pending": pending}


def _mentioned_client_names(self, message: str) -> list[str]:
    text = " ".join(str(message or "").split())
    if not text:
        return []
    conn = self._connect()
    try:
        rows = conn.execute("SELECT name FROM clients ORDER BY length(name) DESC, id").fetchall()
    finally:
        conn.close()
    return [
        str(row["name"])
        for row in rows
        if any(alias in text for alias in _client_aliases(str(row["name"] or "")))
    ]


def _route_copilot_skills(self, message: str, context: dict[str, Any]) -> list[str]:
    routes: list[str] = []
    normalized = message.lower()

    def add(skill_id: str) -> None:
        spec = self.skills.get(skill_id)
        if spec and context["type"] in spec.supported_contexts and skill_id not in routes:
            routes.append(skill_id)

    if "opencli" in normalized:
        add("opencli_usage")
        if any(token in message for token in ("当前页面", "浏览器", "网页", "Chrome", "chrome", "页面状态")):
            add("opencli_browser_read")
    if (
        context["type"] == "candidate" and "猎聘" in message
        and any(token in message for token in ("抓取", "补全", "补充", "读取", "简历"))
    ):
        return ["liepin_resume_capture"]

    direct_rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("job_intake", ("岗位接入", "录入岗位", "接入岗位", "需求接入")),
        ("jd_calibration", ("jd校准", "JD校准", "岗位校准", "校准岗位", "硬门槛", "岗位需求", "分析岗位", "分析JD", "JD分析", "看看这个JD", "岗位要求", "JD")),
        ("job_library_update", ("更新岗位", "拆分岗位", "新建岗位", "建立岗位", "岗位库更新")),
        ("job_diagnosis", ("岗位诊断", "岗位风险", "岗位漏斗", "风险", "漏斗", "诊断", "驾驶舱", "看板")),
        ("talent_pool_search", ("人才库", "历史人才", "存量人选", "库里", "搜库", "检索人才")),
        ("search_strategy", ("寻访策略", "搜索策略", "怎么找", "搜人策略", "目标公司", "关键词")),
        ("job_publish_prepare", ("发布准备", "岗位发布准备", "准备发布", "发布草稿", "上架准备")),
        ("candidate_assessment", ("评估", "匹配", "判断", "合不合适", "适配", "推荐吗")),
        ("verification_plan", ("核验", "验证", "缺什么", "待核验", "核实", "问题清单")),
        ("communication_draft", ("草稿", "怎么联系", "沟通话术", "怎么聊", "私聊话术")),
        ("resume_export", ("导出简历", "简历导出", "结构化简历", "简历文档")),
        ("candidate_batch_assessment", ("批量评估", "批量判断", "批量匹配", "评估这一批")),
        ("matching_report", ("匹配报告", "人岗匹配报告", "匹配分析", "人岗分析")),
        ("recommendation_report", ("推荐报告", "嘉驰推荐", "推荐材料", "候选人报告")),
        ("reply_triage", ("回复识别", "回复分流", "回复待办", "回复处理", "回复 triage")),
        ("communication_draft_batch", ("批量草稿", "批量话术", "批量沟通", "草稿这一批")),
        ("outreach_prepare", ("触达准备", "准备触达", "锁定触达", "触达草稿", "外呼准备")),
        ("interview_followup", ("面试反馈", "面试跟进", "面试纪要", "客户反馈")),
        ("salary_verification", ("薪资核验", "薪资验证", "薪资报告", "薪资证明")),
        ("salary_negotiation", ("谈薪", "薪资谈判", "谈薪风险", "薪资风险")),
        ("decision_coaching", ("决策辅导", "候选人决策", "决策建议", "offer决策")),
        ("onboarding_followup", ("入职跟进", "onboarding", "入职计划", "入职事项")),
        ("project_retrospective", ("项目复盘", "复盘", "结案总结", "项目总结")),
    )
    for skill_id, tokens in direct_rules:
        if _contains_any(message, tokens):
            add(skill_id)
    return routes[: max(1, int(self.config["runtime"]["copilot_max_skills"]))]


def _generate_copilot_model_answer(
    self,
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the canonical answer model with read-only tools on the same turn state."""
    if not bool(self.config.get("runtime", {}).get("copilot_tools_enabled", True)):
        return self.llm.copilot(payload), [], []

    from .copilot_tools import COPILOT_TOOLS, TOOL_EXECUTORS

    max_rounds = max(1, min(int(self.config.get("runtime", {}).get("copilot_tool_rounds", 3)), 5))
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
    ]
    executed_calls: set[tuple[str, str]] = set()
    tool_results: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []

    for round_num in range(max_rounds):
        response = self.llm.copilot_with_tools(payload, COPILOT_TOOLS, messages=messages)
        if not isinstance(response, dict):
            return str(response or "").strip(), tool_results, references
        calls = response.get("tool_calls") if isinstance(response.get("tool_calls"), list) else []
        if not calls:
            return str(response.get("content") or "").strip(), tool_results, references

        assistant_calls: list[dict[str, Any]] = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or f"tool_{round_num}_{index}")
            name = str(call.get("name") or "").strip()
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            assistant_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            })
        messages.append({
            "role": "assistant",
            "content": response.get("content") or None,
            "tool_calls": assistant_calls,
        })

        new_call_executed = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "").strip()
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            call_key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            executor = TOOL_EXECUTORS.get(name)
            if executor is None:
                result = {"success": False, "error": f"不允许的只读工具: {name or 'unknown'}"}
            elif call_key in executed_calls:
                result = {"success": False, "error": "本轮已返回相同查询，请直接使用已有结果作答。"}
            else:
                executed_calls.add(call_key)
                new_call_executed = True
                try:
                    result = executor(str(self.db_path), **arguments)
                except Exception as exc:
                    result = {"success": False, "error": str(exc)[:300]}
            tool_results.append({"tool": name, "args": arguments, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            })
            references.append({
                "type": "tool_result",
                "id": call_id,
                "label": name or "只读查询",
                "subtitle": "查询成功" if result.get("success") else str(result.get("error") or "查询失败")[:80],
            })

        if not new_call_executed or round_num == max_rounds - 1:
            final = self.llm.copilot_with_tools(
                payload,
                COPILOT_TOOLS,
                messages=messages,
                allow_tools=False,
            )
            return str(final.get("content") or "").strip(), tool_results, references
    return "", tool_results, references


def copilot(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = " ".join(str(message or "").split())
    if not normalized:
        raise ValueError("请输入问题")
    stable_session_id = str(session_id or "").strip() or f"copilot_{secrets.token_hex(6)}"
    with self._copilot_locks_guard:
        session_lock = self._copilot_session_locks.setdefault(stable_session_id, threading.RLock())
    with session_lock:
        return self._copilot_impl(normalized, session_id=stable_session_id, context=context)


def _copilot_impl(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = " ".join(str(message or "").split())
    if not message:
        raise ValueError("请输入问题")
    raw_context = dict(context or {})
    floating_compact = str(raw_context.get("display_mode") or "").strip() == "floating_compact"
    selected = self._normalize_copilot_context(raw_context)
    selected, focus_conflicts = self._copilot_context_from_focus(session_id, message, selected)
    existing_focus = self.get_copilot_focus(session_id)
    conversation_history = self._copilot_conversation_history(session_id)
    selected_facts = self._copilot_context_facts(selected)
    if selected.get("type") == "workflow":
        focused_context = existing_focus.get("context") if isinstance(existing_focus, dict) and isinstance(existing_focus.get("context"), dict) else {}
        if focused_context.get("type") != "workflow" or focused_context.get("id") != selected.get("id"):
            # A persistent floating session may have been used for another job. Its chat
            # history is not evidence for a newly selected workflow.
            conversation_history = []
    last_assistant_message = next(
        (
            str(item.get("content") or "")
            for item in reversed(conversation_history)
            if item.get("role") == "assistant"
        ),
        "",
    )
    intent_understanding = _interpret_copilot_message(
        self,
        message,
        selected,
        selected_facts,
        existing_focus,
        conversation_history,
        last_assistant_message,
    )
    understood_target = intent_understanding.get("target") if isinstance(intent_understanding.get("target"), dict) else {}
    if (
        understood_target.get("type") == "job"
        and understood_target.get("id")
        and selected.get("type") not in {"candidate", "workflow"}
    ):
        selected = {
            "type": "job", "id": int(understood_target["id"]),
            "page": "positions", "filters": {},
        }
        selected_facts = self._copilot_context_facts(selected)
        focus_conflicts = []
    pending_plan_ref, pending_plan_state = _copilot_pending_plan(self, selected, existing_focus)
    pending_goal_context = (
        (pending_plan_state.get("goal") or {}).get("context")
        if pending_plan_state else {}
    )
    previous_constraints = (
        pending_goal_context.get("constraint_ledger")
        or pending_goal_context.get("locked_constraints")
        or ((existing_focus or {}).get("constraint_ledger") if isinstance(existing_focus, dict) else [])
        or ((existing_focus or {}).get("constraints") if isinstance(existing_focus, dict) else [])
    )
    turn_decision = build_turn_decision(
        intent_understanding,
        message=message,
        previous_constraints=previous_constraints,
        pending_plan_ref=pending_plan_ref,
        raw_constraint_changes=intent_understanding.get("raw_constraint_changes"),
    )
    intent_understanding["safe_for_action"] = bool(turn_decision.get("safe_for_action"))
    intent_understanding["constraint_changes"] = list(turn_decision.get("constraint_changes") or [])
    intent_understanding["effective_constraints"] = list(turn_decision.get("effective_constraints") or [])
    semantic_action = str(intent_understanding.get("action") or "none")
    semantic_speech_act = str(intent_understanding.get("speech_act") or "other")
    workflow_outcome_question = bool(
        "寻访" in message
        and re.search(r"(?:什么结果|结果如何|结果怎样|结果怎么样|进展如何|进展怎么样|情况如何|情况怎么样)", message)
    )
    workflow_strategy_question = _workflow_strategy_question(message, selected)
    semantic_constraints = [
        str(item.get("quote") or "").strip()
        for item in (turn_decision.get("effective_constraints") or [])
        if isinstance(item, dict) and str(item.get("quote") or "").strip()
    ]
    pending_scope_request = ""
    if (
        last_assistant_message.strip() == "你要为哪个岗位补充并触达新候选人？"
        and selected.get("type") == "job"
        and selected.get("id")
    ):
        pending_scope_request = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(conversation_history)
                if item.get("role") == "user"
                and self._copilot_action_kind(str(item.get("content") or ""))
                in {"new_candidate_outreach", "candidate_sourcing"}
            ),
            "",
        )
    scope_clarification_resolved = bool(pending_scope_request)
    explicit_sourcing_confirmation = bool(
        semantic_action == "candidate_sourcing"
        and turn_decision.get("effect") == "start_plan"
        and intent_understanding.get("safe_for_action")
    )
    short_sourcing_confirmation = explicit_sourcing_confirmation and _is_short_ack(message)
    focus_context = (
        existing_focus.get("context")
        if existing_focus and isinstance(existing_focus.get("context"), dict)
        else {}
    )
    if (
        (explicit_sourcing_confirmation or short_sourcing_confirmation)
        and selected.get("type") not in {"job", "candidate"}
        and focus_context.get("type") == "job"
        and focus_context.get("id")
        and float(existing_focus.get("confidence") or 0) >= 0.7
    ):
        selected = {
            "type": "job",
            "id": int(focus_context["id"]),
            "page": "positions",
            "filters": {},
        }
    sourcing_focus = bool(pending_plan_ref and semantic_action == "candidate_sourcing")
    auto_start_sourcing = (
        sourcing_focus and explicit_sourcing_confirmation and not workflow_outcome_question
    )
    pending_sourcing_workflow: dict[str, Any] | None = None
    sourcing_revision_instruction = ""
    goal_request = message
    if auto_start_sourcing:
        pending_workflow = (
            existing_focus.get("pending_workflow")
            if isinstance(existing_focus, dict) and isinstance(existing_focus.get("pending_workflow"), dict)
            else {}
        )
        candidate_context = (pending_plan_state.get("goal") or {}).get("context") or {}
        same_target = bool(
            candidate_context.get("type") == "job"
            and candidate_context.get("id")
            and (
                selected.get("type") not in {"job", "candidate"}
                or str(candidate_context.get("id")) == str(selected.get("id"))
            )
        )
        if (pending_plan_state.get("workflow") or {}).get("status") == "planned" and same_target:
            pending_sourcing_workflow = pending_plan_state
            selected = {
                "type": "job",
                "id": int(candidate_context["id"]),
                "page": "positions",
                "filters": {},
            }
        prior_objective = str(existing_focus.get("objective") or "").strip()
        prior_is_sourcing = bool(
            any(token in prior_objective for token in ("补池", "寻访", "找人", "搜索人选", "搜索候选人"))
            or (
                any(token in prior_objective for token in ("人选", "候选人"))
                and any(
                    token in prior_objective
                    for token in ("补充", "再找", "继续找", "找人", "搜索", "搜人")
                )
            )
        )
        if not prior_is_sourcing:
            prior_objective = next(
                (
                    str(item.get("content") or "").strip()
                    for item in reversed(conversation_history)
                    if item.get("role") == "user"
                    and any(token in str(item.get("content") or "") for token in ("人选", "候选人"))
                    and any(
                        token in str(item.get("content") or "")
                        for token in ("补充", "补池", "再找", "继续找", "找人", "搜索", "搜人", "寻访")
                    )
                ),
                "",
            )
        if not prior_objective:
            focus_job = existing_focus.get("job") if isinstance(existing_focus.get("job"), dict) else {}
            target = str(focus_job.get("title") or "当前岗位")
            prior_objective = f"继续为{target}补充候选人"
        pending_source_message = str(pending_workflow.get("source_message") or "").strip()
        refinements = [
            str(item.get("content") or "").strip()
            for item in conversation_history
            if item.get("role") == "user"
            and str(item.get("content") or "").strip() != pending_source_message
            and any(
                token in str(item.get("content") or "")
                for token in ("放宽", "年限", "职级", "行业", "方向", "条件", "要求")
            )
        ][-5:]
        adjustment = "；".join(dict.fromkeys(item for item in refinements if item))
        goal_request = prior_objective
        if adjustment:
            goal_request += f"。本轮条件调整：{adjustment}"
        confirmed_detail = next(
            (
                " ".join(str(item.get("content") or "").split())[:360]
                for item in reversed(conversation_history)
                if item.get("role") == "assistant"
                and any(token in str(item.get("content") or "") for token in ("年限", "职级"))
                and any(
                    token in str(item.get("content") or "")
                    for token in ("年以上", "资深工程师", "主管", "经理", "总监")
                )
            ),
            "",
        )
        if confirmed_detail:
            goal_request += f"。已确认细化条件：{confirmed_detail}"
        if semantic_constraints:
            goal_request += f"。顾问原话约束：{'；'.join(dict.fromkeys(semantic_constraints))}"
        goal_request += "。确认执行多渠道寻访"
    elif scope_clarification_resolved:
        goal_request = pending_scope_request
    mentioned_clients = self._mentioned_client_names(message)
    primary_client = mentioned_clients[0] if mentioned_clients else str((existing_focus or {}).get("client") or "该客户")
    forced_answer = None
    workflow_cancelled = False
    started_new_plan = False
    if re.search(r"(?:性子|性质)结构", message) and not any(token in message for token in ("公司性质", "组织结构", "薪资结构")):
        forced_answer = (
            f"你是想问{primary_client}的公司性质/组织结构，还是薪资结构？\n\n"
            "请确认一个方向，我再按对应证据回答。"
        )
    goal_workflow = None
    goal_patterns = (
        r"(?:补充|补池|寻访|再寻访|继续寻访|找|搜索|搜|再找|继续找)\s*(?:一些|些|若干|一批|一轮|新一批)?\s*\d*\s*(?:位|个|名|人)?\s*(?:合适|匹配|合适的)?(?:人选|候选人)",
        r"(?:多渠道|猎聘|X-?SaaS|x-?saas).*(?:寻访|搜索|找人|找候选人|补池)",
        r"(?:更新|拆分|拆成|分成|新建|建立).*(?:岗位|职位|岗位库)",
        r"(?:岗位|职位).*(?:更新|拆分|拆成|分成|新建|建立)",
        r"(?:归档|关闭).*(?:岗位|职位)",
        r"(?:岗位|职位).*(?:归档|关闭)",
        r"(?:整理|生成|制作).*(?:推荐报告|谈薪|薪资|面试反馈|入职)",
        r"推进今天.*(?:回复|人选|待办)",
        r"(?:发布|上架|发).*岗位",
        r"(?:触达|发送|开聊|沟通).*(?:候选人|人选|这一批|当前)",
        r"推荐给客户",
        r"(?:客户推荐|提交客户|推给客户)",
        r"(?:身份合并|合并人选|同一人|重复人选)",
        r"(?:确认|锁定).*(?:offer|Offer|入职条件)",
        r"(?:记住|沉淀|保存).*(?:规则|经验|记忆)",
    )
    # R9：CoreService 判定该消息是待确认的候选人写入意图时置
    # suppress_goal_intent，此处不再路由工作流级目标（防止同一条
    # 消息既产生确认卡片又建立/启动工作流）。
    suppress_goal_intent = (
        bool(raw_context.get("suppress_goal_intent"))
        or workflow_outcome_question
        or workflow_strategy_question
    )
    if forced_answer is None and not suppress_goal_intent:
        context_mismatch_answer = _format_context_mismatch_answer(
            focus_conflicts,
            floating_compact=floating_compact,
        )
        if context_mismatch_answer:
            forced_answer = context_mismatch_answer
    if forced_answer is None and _is_job_budget_fact_update(message):
        forced_answer = _format_job_budget_fact_answer(message, selected_facts)
    if forced_answer is None and _is_candidate_result_observation(message, intent_understanding):
        forced_answer = _format_candidate_result_observation_answer(
            message,
            selected_facts,
            existing_focus,
            floating_compact=floating_compact,
        )
    strategy_revision: dict[str, Any] | None = None
    strategy_revision_requested = bool(
        not suppress_goal_intent
        and turn_decision.get("effect") == "revise_plan"
        and intent_understanding.get("safe_for_action")
    )
    if strategy_revision_requested:
        source_workflow_id = str(pending_plan_ref.get("workflow_id") or "")
        revision_error = "" if source_workflow_id else "当前没有唯一待修订计划"
        change_parts = []
        for change in turn_decision.get("constraint_changes") or []:
            operation = str(change.get("operation") or "")
            if operation == "replace":
                change_parts.append(f"将“{change.get('previous_quote')}”替换为“{change.get('quote')}”")
            elif operation == "remove":
                change_parts.append(f"删除“{change.get('previous_quote')}”")
            elif operation == "add":
                change_parts.append(f"增加“{change.get('quote')}”")
        revision_instruction = "；".join(change_parts) or _strategy_revision_instruction(message, conversation_history)
        confirmed_assistant_detail = _confirmed_assistant_refinement(message, last_assistant_message)
        if confirmed_assistant_detail and confirmed_assistant_detail not in revision_instruction:
            revision_instruction += f"；用户本轮确认的上一轮细化：{confirmed_assistant_detail}"
        if revision_error:
            forced_answer = f"结论：尚未生成策略变更确认。\n\n下一步：{revision_error}"
        elif not revision_instruction:
            forced_answer = (
                "结论：已定位待修订的寻访工作流，但缺少明确修改条件。\n\n"
                "下一步：请说明要增加、删除或调整的经验、公司池、关键词或排除项。"
            )
        else:
            try:
                goal_workflow = self.revise_workflow(
                    source_workflow_id,
                    revision_instruction,
                    effective_constraints=list(turn_decision.get("effective_constraints") or []),
                    constraint_changes=list(turn_decision.get("constraint_changes") or []),
                    turn_decision=turn_decision,
                )
                strategy_revision = {
                    "source_workflow_id": source_workflow_id,
                    "revised_workflow_id": goal_workflow["workflow"]["workflow_id"],
                    "constraint_changes": list(turn_decision.get("constraint_changes") or []),
                }
                selected = self._normalize_copilot_context(goal_workflow["goal"]["context"])
                focus_conflicts = []
            except ValueError as exc:
                forced_answer = f"结论：未生成修订版。\n\n下一步：{str(exc)[:180]}。"
    if (
        not suppress_goal_intent
        and turn_decision.get("effect") == "cancel_plan"
        and intent_understanding.get("safe_for_action")
        and goal_workflow is None
        and forced_answer is None
    ):
        try:
            goal_workflow = self.cancel_workflow(str(pending_plan_ref["workflow_id"]), message)
            workflow_cancelled = True
            forced_answer = "结论：当前计划已取消，后续步骤不会继续执行。"
        except ValueError as exc:
            forced_answer = f"结论：未能取消当前计划。\n\n下一步：{str(exc)[:180]}。"
    # S4-1 L3 提问清单门控：上一轮若因四锚点缺失 ≥2 且无岗位原型而出过提问
    # 清单，本轮顾问“直接搜/先搜”类回复视为放行（consultant_override，推断项
    # 保持 inferred+confidence），锚点类回复并入策略上下文；两者都还原原始
    # 寻访目标继续建工作流。
    pending_clarification = self._pending_strategy_clarification(session_id)
    strategy_gate_clarification: dict[str, Any] = {}
    strategy_gate_pending_record: dict[str, Any] = {}
    strategy_gate_force_goal = False
    if pending_clarification and not suppress_goal_intent:
        pending_job_id = int(pending_clarification.get("job_id") or 0)
        selected_job_id = int(selected["id"]) if selected.get("type") == "job" and selected.get("id") else 0
        focused_job_id = int(focus_context["id"]) if focus_context.get("type") == "job" and focus_context.get("id") else 0
        targeted_job_id = selected_job_id or focused_job_id
        same_job = not targeted_job_id or targeted_job_id == pending_job_id
        override_reply = strategy_v2.is_direct_search_override(message) or (explicit_sourcing_confirmation and same_job)
        new_goal_intent = any(re.search(pattern, message, re.I) for pattern in goal_patterns)
        if override_reply and same_job:
            strategy_gate_clarification = {
                "consultant_override": True,
                "asked_questions": True,
                "input_level": str(pending_clarification.get("input_level") or "L3"),
                "missing_anchors": list(pending_clarification.get("missing_anchors") or []),
                "original_objective": str(pending_clarification.get("original_objective") or ""),
            }
            if not auto_start_sourcing:
                goal_request = str(pending_clarification.get("original_objective") or message)
                strategy_gate_force_goal = True
        elif (
            same_job
            and not auto_start_sourcing
            and not new_goal_intent
            and strategy_v2.looks_like_anchor_answer(message)
        ):
            strategy_gate_clarification = {
                "consultant_override": False,
                "consultant_answers": message,
                "asked_questions": True,
                "input_level": str(pending_clarification.get("input_level") or "L3"),
                "missing_anchors": list(pending_clarification.get("missing_anchors") or []),
                "original_objective": str(pending_clarification.get("original_objective") or ""),
            }
            goal_request = f"{pending_clarification.get('original_objective') or message}。顾问锚点补充：{message}"
            strategy_gate_force_goal = True
    semantic_goal_intent = bool(
        semantic_action in {
            "candidate_sourcing", "candidate_outreach", "job_publish", "job_split",
            "job_archive", "candidate_review", "recommendation", "salary",
        }
        and semantic_speech_act in {"propose", "execute", "confirm"}
        and intent_understanding.get("safe_for_action")
    )
    action_context_prompts = {
        "candidate_outreach": ({"candidate", "queue"}, "请先选择具体候选人，或明确要处理的待联系队列。"),
        "candidate_review": ({"job", "candidate", "queue"}, "请先选择要核验的岗位、候选人或候选队列。"),
        "recommendation": ({"candidate"}, "请先选择要生成报告或推荐给客户的具体候选人。"),
        "salary": ({"candidate"}, "请先选择要处理谈薪的具体候选人。"),
    }
    # 查询型名单请求直答：顾问要“名单/筛出/列表”时直接返回候选池，不建
    # 等待确认的执行计划（2026-08-10 长越机械人选名单卡在 create_plan）。
    # 必须放在 action_context_rule 之前，否则“请先选择要核验的岗位”会先抢答，
    # 名单拦截永远执行不到（kimi review #5）。
    candidate_list_answer = ""
    candidate_list_card: dict[str, Any] = {}
    if (
        forced_answer is None
        and not suppress_goal_intent
        and _is_candidate_list_query(message)
    ):
        list_job_id = 0
        if selected.get("type") == "job" and selected.get("id"):
            list_job_id = int(selected["id"])
        elif isinstance(focus_context, dict) and focus_context.get("type") == "job" and focus_context.get("id"):
            list_job_id = int(focus_context["id"])
        if not list_job_id:
            # 消息里显式提到岗位（如“岗位 137 的名单”）优先于选中/焦点，避免答错对象。
            mentioned = self._mentioned_jobs_for_copilot(message)
            if len(mentioned) == 1:
                list_job_id = int(mentioned[0]["id"])
        if not list_job_id and mentioned_clients:
            conn = self._connect()
            try:
                job_row = conn.execute(
                    """
                    SELECT j.id FROM jobs j
                    JOIN clients c ON c.id = j.client_id
                    WHERE c.name LIKE ? AND j.status NOT IN ('closed','archived')
                    ORDER BY j.id LIMIT 1
                    """,
                    (f"%{mentioned_clients[0]}%",),
                ).fetchone()
                if job_row:
                    list_job_id = int(job_row["id"])
            finally:
                conn.close()
        if list_job_id:
            candidate_list_answer, candidate_list_card = _build_candidate_list_card(self.db_path, list_job_id, message)
            if candidate_list_answer:
                forced_answer = candidate_list_answer
    # 名单构成质疑直答：用户问“怎么都是做光刻机的”时给出构成分析，
    # 而不是再输出一遍名单（2026-08-11 copilot_ad7e7086917d 答非所问修复）。
    if forced_answer is None and not suppress_goal_intent and _is_candidate_list_composition_question(message):
        comp_job_id = 0
        if selected.get("type") == "job" and selected.get("id"):
            comp_job_id = int(selected["id"])
        elif isinstance(focus_context, dict) and focus_context.get("type") == "job" and focus_context.get("id"):
            comp_job_id = int(focus_context["id"])
        if not comp_job_id:
            mentioned = self._mentioned_jobs_for_copilot(message)
            if len(mentioned) == 1:
                comp_job_id = int(mentioned[0]["id"])
        if not comp_job_id and mentioned_clients:
            conn = self._connect()
            try:
                job_row = conn.execute(
                    """
                    SELECT j.id FROM jobs j
                    JOIN clients c ON c.id = j.client_id
                    WHERE c.name LIKE ? AND j.status NOT IN ('closed','archived')
                    ORDER BY j.id LIMIT 1
                    """,
                    (f"%{mentioned_clients[0]}%",),
                ).fetchone()
                if job_row:
                    comp_job_id = int(job_row["id"])
            finally:
                conn.close()
        if comp_job_id:
            composition_answer = _build_candidate_list_composition_answer(self.db_path, comp_job_id, message)
            if composition_answer:
                forced_answer = composition_answer
    action_context_rule = action_context_prompts.get(semantic_action)
    if (
        forced_answer is None
        and semantic_goal_intent
        and turn_decision.get("effect") == "create_plan"
        and action_context_rule
        and selected.get("type") not in action_context_rule[0]
        and not candidate_list_answer
    ):
        # 语义动作需要岗位/候选人上下文但 selected 缺失时，先从会话焦点/历史证据
        # 补全目标岗位，避免有明确主线的会话被"请先选择"卡死（2026-08-07 郭杨评估指令被吞）。
        inferred_target: dict[str, Any] | None = None
        focus_snapshot = focus_context if isinstance(focus_context, dict) else {}
        focus_context_candidate = focus_snapshot if focus_snapshot.get("type") in {"job", "candidate"} and focus_snapshot.get("id") else {}
        if focus_context_candidate and float((existing_focus or {}).get("confidence") or 0) >= 0.7:
            inferred_target = {"type": str(focus_context_candidate["type"]), "id": int(focus_context_candidate["id"])}
        else:
            evidence = self._copilot_session_business_evidence(session_id)
            evidence_jobs = list(evidence.get("jobs") or [])
            if len(evidence_jobs) == 1:
                inferred_target = {"type": "job", "id": int(evidence_jobs[0]["id"])}
            elif len(evidence_jobs) > 1:
                # 多岗位时取最近一条 assistant 引用/用户 context 的岗位（按消息倒序优先）。
                recent = conversation_history[-6:]
                for item in reversed(recent):
                    mentioned = self._mentioned_jobs_for_copilot(str(item.get("content") or ""))
                    if len(mentioned) == 1:
                        inferred_target = {"type": "job", "id": int(mentioned[0]["id"])}
                        break
        if inferred_target:
            target_type = str(inferred_target["type"])
            selected = {
                "type": target_type, "id": int(inferred_target["id"]),
                "page": "positions" if target_type == "job" else "candidates", "filters": {},
            }
            selected_facts = self._copilot_context_facts(selected)
            focus_conflicts = []
        else:
            forced_answer = action_context_rule[1]
    if (
        forced_answer is None
        and semantic_action != "none"
        and intent_understanding.get("needs_clarification")
        and not suppress_goal_intent
    ):
        missing_text = "、".join(intent_understanding.get("missing_fields") or [])
        forced_answer = str(intent_understanding.get("clarification_question") or "").strip() or (
            f"我还缺少{missing_text}，确认后才能继续。" if missing_text else "我还不能唯一确定你的对象或动作，请再确认一句。"
        )
    start_pending_plan = bool(
        turn_decision.get("effect") == "start_plan"
        and pending_plan_state
        and intent_understanding.get("safe_for_action")
        and not workflow_outcome_question
    )
    if start_pending_plan and forced_answer is None and not suppress_goal_intent and goal_workflow is None:
        try:
            goal_workflow = self.start_workflow(
                str(pending_plan_ref["workflow_id"]),
                expected_plan_version=int(pending_plan_ref["version"]),
                expected_plan_hash=str(pending_plan_ref["plan_hash"]),
            )
            selected = self._normalize_copilot_context(goal_workflow["goal"]["context"])
            focus_conflicts = []
        except ValueError as exc:
            forced_answer = f"结论：未启动已确认计划。\n\n下一步：{str(exc)[:180]}。"
    create_plan_requested = bool(
        turn_decision.get("effect") == "create_plan"
        and intent_understanding.get("safe_for_action")
    )
    if goal_workflow is None and forced_answer is None and not suppress_goal_intent and (
        create_plan_requested or scope_clarification_resolved or strategy_gate_force_goal
    ):
        ground_base = selected
        if strategy_gate_force_goal and pending_clarification.get("job_id"):
            ground_base = {"type": "job", "id": int(pending_clarification["job_id"]), "page": "positions", "filters": {}}
        goal_context, _, grounding_error = self._ground_copilot_goal(goal_request, ground_base, session_id)
        if grounding_error:
            forced_answer = grounding_error
        else:
            if semantic_action == "candidate_sourcing" and not any(
                token in goal_request for token in ("补池", "寻访", "找人", "候选人", "人选", "搜索", "搜人")
            ):
                goal_request = f"为当前岗位补充候选人。顾问原话：{message}"
            if semantic_constraints:
                locked_text = "；".join(dict.fromkeys(semantic_constraints))
                if locked_text and locked_text not in goal_request:
                    goal_request += f"。顾问原话约束：{locked_text}"
            goal_context["intent_understanding"] = intent_understanding
            goal_context["turn_decision"] = turn_decision
            goal_context["constraint_ledger"] = list(turn_decision.get("effective_constraints") or [])
            goal_context["locked_constraints"] = list(dict.fromkeys(semantic_constraints))
            continued_sourcing = _continued_sourcing_requested(goal_request)
            if _new_candidate_outreach_requested(goal_request) or continued_sourcing:
                grounding = goal_context.get("goal_grounding") if isinstance(goal_context.get("goal_grounding"), dict) else {}
                client = str(grounding.get("client") or "该客户")
                job = str(grounding.get("job") or "当前岗位")
                # This is deliberately a sourcing plan. It may prepare a new batch, but
                # the R3 multi-channel step still prevents any external message from sending.
                grounded_goal = (
                    f"为{client}{job}补充并准备触达新候选人"
                    if _new_candidate_outreach_requested(goal_request)
                    else f"为{client}{job}继续补充候选人"
                )
                if goal_request not in grounded_goal:
                    grounded_goal += f"。顾问原始目标：{goal_request}"
                goal_request = grounded_goal
            strategy_gate = (
                {"action": "proceed"}
                if (
                    strategy_gate_clarification
                    or _new_candidate_outreach_requested(message)
                    or scope_clarification_resolved
                    or (
                        continued_sourcing
                        and isinstance(existing_focus, dict)
                        and str(existing_focus.get("action") or "") in {"candidate_sourcing", "strategy_revision"}
                    )
                )
                else self._sourcing_strategy_gate(goal_request, goal_context, floating_compact=floating_compact)
            )
            if strategy_gate.get("action") == "ask":
                # 红线：提问清单场景不创建 workflow_id，不声称已启动，无任何外部执行。
                forced_answer = str(strategy_gate.get("answer") or "")
                strategy_gate_pending_record = strategy_gate.get("pending") or {}
            else:
                if strategy_gate_clarification:
                    goal_context["strategy_clarification"] = strategy_gate_clarification
                try:
                    goal_workflow = self.create_goal(goal_request, goal_context)
                    if (turn_decision.get("authorization") or {}).get("mode") == "explicit_execute":
                        created_ref = dict(goal_workflow.get("plan_ref") or {})
                        goal_workflow = self.start_workflow(
                            goal_workflow["workflow"]["workflow_id"],
                            expected_plan_version=int(created_ref["version"]),
                            expected_plan_hash=str(created_ref["plan_hash"]),
                        )
                        started_new_plan = True
                    selected = self._normalize_copilot_context(goal_context)
                    if selected.get("type") in {"job", "candidate"} and selected.get("id"):
                        focus_conflicts = []
                except ValueError as exc:
                    forced_answer = (
                        f"结论：未建立工作流，执行对象校验未通过。\n\n"
                        f"下一步：{str(exc)[:180]}。"
                    )
    context_type = selected["type"]
    context_id = selected.get("id")
    dashboard = self.get_dashboard()
    selected_payload: dict[str, Any] = dict(selected)
    selected_payload["intent_understanding"] = intent_understanding
    selected_payload["turn_decision"] = turn_decision
    current_workflow_context: dict[str, Any] = {}
    if goal_workflow:
        workflow = goal_workflow.get("workflow") or {}
        goal = goal_workflow.get("goal") or {}
        goal_context = goal.get("context") if isinstance(goal.get("context"), dict) else {}
        result_plan_ref = dict(goal_workflow.get("plan_ref") or {})
        selected_payload["workflow_intent"] = {
            "workflow_id": str(workflow.get("workflow_id") or ""),
            "status": str(workflow.get("status") or ""),
            "version": result_plan_ref.get("version"),
            "plan_hash": result_plan_ref.get("plan_hash"),
            "action": semantic_action,
            "objective": str(goal.get("objective") or ""),
            "locked_constraints": list(goal_context.get("locked_constraints") or []),
            "constraint_ledger": list(goal_context.get("constraint_ledger") or []),
            "source_message": message,
        }
    if selected.get("type") == "workflow" and selected_facts:
        workflow = dict(selected_facts.get("workflow") or {})
        job = dict(selected_facts.get("job") or {})
        selected_payload.update({
            "workflow": workflow,
            "client": str(selected_facts.get("client") or ""),
            "job": str(job.get("title") or ""),
            "job_id": job.get("id"),
            "workflow_context": workflow.get("context") or {},
            "business_focus": {
                "context": dict(selected_facts.get("context") or selected),
                "client": str(selected_facts.get("client") or ""),
                "job": job,
                "candidate": {},
                "confidence": 1.0,
            },
        })
        try:
            current_workflow_context = _compact_workflow_context(self.get_workflow(str(selected.get("id") or "")))
        except (sqlite3.Error, ValueError):
            current_workflow_context = {}
        if current_workflow_context:
            selected_payload["workflow_detail"] = current_workflow_context
            if workflow_strategy_question and forced_answer is None:
                forced_answer = _format_workflow_strategy_answer(
                    current_workflow_context,
                    expanded=_copilot_response_detail(message) == "expanded",
                )
    elif existing_focus:
        selected_payload["business_focus"] = existing_focus
    references: list[dict[str, Any]] = []
    suggested_actions: list[dict[str, Any]] = []
    if context_type == "candidate" and context_id:
        candidate_context = build_candidate_context(self.db_path, context_id)
        state = self.get_candidate_state(context_id)
        assessment = state.get("assessment") or {}
        stopped_context = is_stopped(candidate_context)
        stop_review = _latest_event(candidate_context, "resume_review_completed")
        stop_stage_event = _latest_event(candidate_context, "candidate_stage_update")
        selected_payload.update(
            {
                "candidate": candidate_context.get("identity", {}),
                "position": candidate_context.get("position", {}),
                "stage": (candidate_context.get("relation") or {}).get("clean_stage") or "",
                "stopped": stopped_context,
                "latest_stop": {
                    "review": stop_review,
                    "stage": stop_stage_event,
                },
                "assessment": _copilot_assessment_context(assessment),
            }
        )
        if (
            assessment
            and not is_stopped(candidate_context)
            and forced_answer is None
            and _candidate_evidence_question(message)
        ):
            # 主 Agent 对话也必须使用可核验的结构化证据，不能让模型把详细追问
            # 压缩成一句泛化结论；模型仍可在非证据型问题中自由回答。
            forced_answer = _format_candidate_evidence_answer(assessment)
        identity = candidate_context.get("identity", {})
        position = candidate_context.get("position", {})
        references.append(
            {
                "type": "candidate",
                "id": context_id,
                "label": identity.get("name") or f"关系 #{context_id}",
                "subtitle": f"{position.get('client','')} / {position.get('job','')}",
            }
        )
        suggested_actions.append({"type": "open_candidate", "id": context_id, "label": "打开人选"})
        if stopped_context:
            stage = str((candidate_context.get("relation") or {}).get("clean_stage") or "已停止")
            identity_label = identity.get("name") or f"关系 #{context_id}"
            project_label = f"{position.get('client','')} / {position.get('job','')}".strip(" /")
            stop_summary = (
                str(stop_review.get("summary") or "").strip()
                or str(stop_stage_event.get("summary") or "").strip()
                or "已有人工停止记录"
            )
            if _is_short_ack(message):
                if floating_compact:
                    answer = (
                        f"结论：已确认，{identity_label} 保持“{stage}”。\n\n"
                        "下一步：不用继续推进；如需重启，先做人工状态纠正。"
                    )
                else:
                    answer = (
                        f"结论：已确认，{identity_label} 当前保持“{stage}”。\n\n"
                        f"依据：{project_label} 的最新复核结果为停止推进；记录为：{stop_summary}。\n\n"
                        "下一步：无需再推进或触达。若你要重新启用这个人选，需要先到人选详情里做人工状态纠正/重新复核。"
                    )
            else:
                if floating_compact:
                    answer = (
                        f"结论：不能继续推进，{identity_label} 已是“{stage}”。\n\n"
                        "下一步：保持归档；重启前先人工纠正状态。"
                    )
                else:
                    answer = (
                        f"结论：当前不能继续推进，{identity_label} 已处于“{stage}”。\n\n"
                        f"依据：{project_label} 的最新停止记录是：{stop_summary}。\n\n"
                        "下一步：保持历史归档；如需重新考虑，先打开人选详情查看记录，再人工纠正状态或重新复核。"
                    )
            persisted_payload = _persistable_attachment_payload(selected_payload)
            business_focus = self._persist_copilot_focus(
                session_id, message, persisted_payload,
                structured=persisted_payload, conflicts=focus_conflicts,
            )
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO agent_copilot_messages
                    (session_id,context_type,context_id,role,content,structured_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    [
                        (session_id, context_type, context_id, "user", message, _dumps(persisted_payload)),
                        (
                            session_id, context_type, context_id, "assistant", answer,
                            _dumps({
                                "references": references, "suggested_actions": suggested_actions,
                                "skill_runs": [], "business_focus": business_focus,
                                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
                            }),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            return {
                "ok": True,
                "session_id": session_id,
                "answer": answer,
                "context": {"type": context_type, "id": context_id},
                "references": references,
                "suggested_actions": suggested_actions,
                "skill_runs": [],
                "business_focus": business_focus,
                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
            }
    elif context_type == "job" and context_id:
        conn = self._connect()
        try:
            job = conn.execute(
                """
                SELECT j.id,c.name AS client,j.title AS job,j.status,j.summary,
                       COUNT(jc.id) AS candidate_total
                FROM jobs j JOIN clients c ON c.id=j.client_id
                LEFT JOIN job_candidates jc ON jc.job_id=j.id
                WHERE j.id=? GROUP BY j.id
                """,
                (context_id,),
            ).fetchone()
        finally:
            conn.close()
        if job:
            selected_payload.update(_row(job))
            selected_payload["position"] = _copilot_job_evidence(self, int(context_id))
            references.append({"type": "job", "id": context_id, "label": job["job"], "subtitle": job["client"]})
            suggested_actions.append({"type": "open_job", "id": context_id, "label": "打开岗位"})
    elif context_type == "queue":
        inbox = self.get_flow_inbox(**selected.get("filters", {}))
        selected_payload["queue"] = {
            "filters": selected.get("filters", {}),
            "summary": inbox.get("summary", {}),
            "items": (inbox.get("items") or [])[:12],
        }
        references = [
            {"type": "candidate", "id": item["job_candidate_id"], "label": item["candidate"], "subtitle": item["project"]}
            for item in (inbox.get("items") or [])[:5]
        ]
    elif context_type == "page":
        selected_payload["page"] = selected.get("page") or "overview"
    bridge_evidence = self._floating_bridge_evidence(raw_context)
    attachment_skill_run = None
    attachment_evidence: dict[str, Any] = {}
    raw_bridge = raw_context.get("bridge") if isinstance(raw_context.get("bridge"), dict) else {}
    raw_wechat = raw_bridge.get("wechat") if isinstance(raw_bridge.get("wechat"), dict) else {}
    if (
        attachment_read_requested(message)
        and str(raw_bridge.get("surface") or "").strip().lower() == "native"
        and bool(raw_wechat)
    ):
        try:
            attachment_skill_context = {**raw_context, "type": "page"}
            attachment_skill_run = self.execute_skill(
                "document_understanding",
                context=attachment_skill_context,
                inputs={
                    "request": message,
                    "bridge": raw_bridge,
                },
            )
            attachment_evidence = (attachment_skill_run.get("result") or {}).get("attachment_evidence") or {}
        except Exception as exc:
            attachment_skill_run = {
                "skill": {"id": "document_understanding", "label": "本机文档理解"},
                "ok": False,
                "error": str(exc)[:500],
            }
    if attachment_evidence:
        selected_payload["attachment_evidence"] = attachment_evidence
        if bridge_evidence:
            bridge_evidence["attachment_content_available"] = any(
                bool(item.get("content_available"))
                for item in attachment_evidence.get("items") or []
            )
    uploaded_attachment_evidence = self._uploaded_attachment_evidence(raw_context, session_id)
    if uploaded_attachment_evidence:
        selected_payload["uploaded_attachment_evidence"] = uploaded_attachment_evidence
        for item in uploaded_attachment_evidence.get("items") or []:
            references.append(
                {
                    "type": "local_attachment",
                    "id": item.get("attachment_id") or "",
                    "label": item.get("file_name") or "本地附件",
                    "subtitle": item.get("status") or "用户粘贴/选择的附件",
                }
            )
    history_text = " ".join(
        item["content"] for item in conversation_history if item.get("role") == "user"
    )
    has_confirmed_salary_structure = bool(
        re.search(r"(?:12\s*(?:薪|个月固定)?\s*[+＋]\s*3|13\s*(?:薪|个月固定)?\s*[+＋]\s*5)", history_text)
    )
    has_attachment_salary_evidence = any(
        bool(item.get("content_available"))
        for item in (attachment_evidence.get("items") or [])
    ) or bool(uploaded_attachment_evidence.get("content_available"))
    if (
        forced_answer is None
        and "薪资结构" in message
        and mentioned_clients
        and not has_confirmed_salary_structure
        and not has_attachment_salary_evidence
    ):
        forced_answer = (
            f"系统里还没有{primary_client}已确认的客户级薪资结构，岗位预算不能替代固定薪资与奖金月数。\n\n"
            "下一步：补充 Dylan/财务确认的固定月数、年终奖月数和适用条件。"
        )
    pending_image_analysis = bool(
        image_analysis_requested(message)
        and bridge_evidence.get("source") == "native"
        and bridge_evidence.get("page_type") == "wechat_visible_window"
        and not bridge_evidence.get("image_analysis")
    )
    if pending_image_analysis:
        suggested_actions.append(
            {
                "type": "native_action",
                "id": "recognizeWeChatImage",
                "label": "打开并识别当前图片",
            }
        )
    if bridge_evidence:
        selected_payload["page_evidence"] = bridge_evidence
        references.append(
            {
                "type": bridge_evidence.get("source") or "page",
                "id": bridge_evidence.get("source_url") or "",
                "label": bridge_evidence.get("candidate_name") or bridge_evidence.get("label") or "当前页面",
                "subtitle": bridge_evidence.get("bridge_status") or bridge_evidence.get("page_type") or "页面桥接证据",
            }
        )
        for item in attachment_evidence.get("items") or []:
            references.append(
                {
                    "type": "native_attachment",
                    "id": "",
                    "label": item.get("file_name") or "微信附件",
                    "subtitle": item.get("status") or "本机附件读取证据",
                }
            )
        unread_attachment = next(
            (
                item for item in (attachment_evidence.get("items") or [])
                if item.get("file_name") and not item.get("content_available")
            ),
            None,
        )
        if unread_attachment and bridge_evidence.get("source") == "native":
            suggested_actions.append(
                {
                    "type": "floating_action",
                    "id": f"open_wechat_attachment::{unread_attachment['file_name']}",
                    "label": "打开并读取当前附件",
                }
            )
        if (
            bridge_evidence.get("source") == "liepin"
            and any(token in message for token in ("录入", "入库", "加入人才库", "保存到人才库"))
        ):
            if floating_compact:
                answer = (
                    "结论：可以继续，但还没到写库完成态。\n\n"
                    "下一步：先补全简历并定位；未选岗位时将先入库为人才库储备（不挂岗位）。"
                )
            else:
                answer = (
                    "结论：可以继续，但当前不是直接写库完成态。\n\n"
                    "依据：当前猎聘页面已识别到候选人"
                    f"{bridge_evidence.get('candidate_name') or '当前人选'}，"
                    f"页面状态为“{bridge_evidence.get('bridge_status') or '已同步'}”。"
                    "ASA 需要先做页面采集、客户/岗位定位和入库预检，确认唯一后才能写入人才库。\n\n"
                    "下一步：我已把动作识别为“补全简历并定位”。请在浮窗或猎聘桥接入口执行该动作；"
                    "如果当前页面尚未选择客户/岗位，确认后将先入库为人才库储备（不挂岗位），之后可再补选客户和岗位。"
                )
            suggested_actions.extend(
                [
                    {"type": "floating_action", "id": "fill_resume", "label": "补全简历并定位"},
                    {"type": "floating_action", "id": "refresh_bridge", "label": "刷新页面识别"},
                ]
            )
            business_focus = self._persist_copilot_focus(
                session_id, message, selected_payload,
                structured=selected_payload, conflicts=focus_conflicts,
            )
            conn = self._connect()
            try:
                conn.executemany(
                    """
                    INSERT INTO agent_copilot_messages
                    (session_id,context_type,context_id,role,content,structured_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    [
                        (session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                        (
                            session_id, context_type, context_id, "assistant", answer,
                            _dumps({
                                "references": references, "suggested_actions": suggested_actions,
                                "skill_runs": [], "business_focus": business_focus,
                                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
                            }),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            return {
                "ok": True,
                "session_id": session_id,
                "answer": answer,
                "context": {"type": context_type, "id": context_id},
                "references": references,
                "suggested_actions": suggested_actions,
                "skill_runs": [],
                "business_focus": business_focus,
                "model_participation": {"mode": "rules", "label": "规则生成", "model": None},
            }
    mentioned_jobs = self._mentioned_jobs_for_copilot(message)
    if mentioned_jobs:
        selected_payload["mentioned_jobs"] = mentioned_jobs
        for item in mentioned_jobs[:3]:
            references.append(
                {"type": "job", "id": item.get("id"), "label": item.get("job"), "subtitle": item.get("client")}
            )
    workflow_outcome_context = self._copilot_workflow_outcome_context(
        message, selected, mentioned_jobs, existing_focus
    )
    routed_skills = (
        []
        if goal_workflow or forced_answer is not None or workflow_outcome_question
        else self._route_copilot_skills(message, selected)
    )
    business_routed_skills = list(routed_skills)
    needs_browser_assist = any(
        (self.skills.get(skill_id) and self.skills.get(skill_id).adapter_type == "browser")
        for skill_id in business_routed_skills
    )
    if needs_browser_assist:
        for support_skill in ("opencli_usage", "opencli_browser_read"):
            if support_skill not in routed_skills:
                routed_skills.append(support_skill)
    skill_runs = [attachment_skill_run] if attachment_skill_run else []
    for skill_id in routed_skills:
        try:
            skill_inputs: dict[str, Any] = {}
            if skill_id == "opencli_usage":
                skill_inputs["command"] = "browser" if any(token in message for token in ("浏览器", "网页", "Chrome", "chrome", "页面")) else "list"
            elif skill_id == "opencli_browser_read":
                skill_inputs = {"args": "asa state", "timeout_seconds": 20}
            skill_run = self.execute_skill(skill_id, context=selected, inputs=skill_inputs)
            skill_runs.append(skill_run)
            result = skill_run.get("result") or {}
            references.extend(result.get("references") or [])
            suggested_actions.extend(result.get("suggested_actions") or [])
        except Exception as exc:
            skill_runs.append({"skill": {"id": skill_id}, "ok": False, "error": str(exc)[:500]})
    # 仅在没有任何任务信号时才用 dashboard top_actions 兜底；
    # 否则用户会收到与当前对话无关的候选/岗位卡片（功能卡）。
    has_task_signal = bool(
        goal_workflow
        or forced_answer is not None
        or workflow_outcome_context
        or routed_skills
        or mentioned_jobs
        or attachment_skill_run
        or _is_job_requirement_message(message)
        or str(intent_understanding.get("action") or "none") != "none"
    )
    if not references and not has_task_signal:
        references = [
            {"type": item["type"], "id": item["id"], "label": item["label"], "subtitle": item["project"]}
            for item in dashboard.get("top_actions", [])[:5]
        ]
    memories = self.search_memories(
        message, context_type=context_type, context_id=context_id,
        client=str(selected_payload.get("client") or selected_payload.get("position", {}).get("client") or ""),
        job=str(selected_payload.get("job") or selected_payload.get("position", {}).get("job") or ""),
    )
    payload = {
        "question": message,
        "intent_understanding": intent_understanding,
        "response_mode": "floating_compact" if floating_compact else "default",
        "response_detail": _copilot_response_detail(message),
        "conversation": self._copilot_conversation_context(session_id, conversation_history),
        "conversation_history": conversation_history,
        "selected_context": selected_payload,
        "dashboard": {
            "summary": dashboard.get("summary", {}),
            "top_actions": dashboard.get("top_actions", [])[:5],
            "exceptions": dashboard.get("exceptions", [])[:5],
            "p0_jobs": dashboard.get("p0_jobs", [])[:8],
            "analytics": dashboard.get("analytics", {}),
        },
        "skill_results": [
            item.get("result") if item.get("ok") else {
                "skill_id": (item.get("skill") or {}).get("id"),
                "ok": False,
                "error": item.get("error"),
            }
            for item in skill_runs
        ],
    }
    if memories.get("mode") == "active":
        payload["approved_memories"] = memories.get("memories") or []
    if workflow_outcome_context:
        payload["workflow_outcome"] = workflow_outcome_context
    capture_run = next(
        (
            item for item in skill_runs
            if (item.get("skill") or {}).get("id") == "liepin_resume_capture"
        ),
        None,
    )
    answer_source = "rules"
    model_tool_calls: list[dict[str, Any]] = []
    if goal_workflow:
        plan_steps = goal_workflow.get("steps") or []
        risk_steps = [step for step in plan_steps if step.get("risk_level") in {"R2", "R3"}]
        if workflow_cancelled:
            answer = "结论：当前计划已取消，后续步骤不会继续执行。"
        elif strategy_revision and floating_compact:
            answer = (
                "结论：已生成寻访策略修订版，旧计划和旧审批已失效。\n\n"
                "下一步：查看新计划，确认后再开始准备。"
            )
        elif strategy_revision:
            answer = (
                f"已生成策略修订版：{goal_workflow['goal']['title']}。\n\n"
                "旧工作流及其待审批已失效；修订版尚未开始，请查看新计划后确认。"
            )
        elif start_pending_plan or started_new_plan:
            answer = (
                f"正在执行：{goal_workflow['goal']['title']}。\n\n"
                "当前步骤完成后会给出可核验结果；涉及外部寻访时再单独请求 R3 授权。"
            )
        elif floating_compact:
            answer = (
                f"结论：目标已建立，计划共 {len(plan_steps)} 步。\n\n"
                f"下一步：先查看计划；{len(risk_steps)} 个风险节点会单次确认。"
            )
        else:
            answer = (
                f"已整理好本次任务：{goal_workflow['goal']['title']}。\n\n"
                f"当前尚未开始，共 {len(plan_steps)} 步；开始后会交付本轮可核验结果。"
            )
        references.extend(
            [
                {"type": goal_workflow["goal"]["context"].get("type"), "id": goal_workflow["goal"]["context"].get("id"), "label": goal_workflow["goal"]["title"], "subtitle": "ASA 目标"}
            ]
        )
        workflow_status = str((goal_workflow.get("workflow") or {}).get("status") or "")
        if workflow_status == "planned":
            suggested_actions.append(
                {
                    "type": "start_workflow",
                    "id": goal_workflow["workflow"]["workflow_id"],
                    "label": "确认新计划" if strategy_revision else "开始执行本次任务",
                    "plan_ref": goal_workflow.get("plan_ref") or {},
                }
            )
        if not workflow_cancelled:
            suggested_actions.append(
                {"type": "open_workflow", "id": goal_workflow["workflow"]["workflow_id"], "label": "查看计划"}
            )
    elif (
        any((item.get("skill") or {}).get("id") == "opencli_usage" for item in skill_runs)
        and not any(
            (item.get("skill") or {}).get("id") not in {"opencli_usage", "opencli_browser_read"}
            for item in skill_runs
        )
    ):
        usage_run = next(item for item in skill_runs if (item.get("skill") or {}).get("id") == "opencli_usage")
        opencli_result = ((usage_run.get("result") or {}).get("opencli") or {}) if usage_run.get("ok") else {}
        browser_run = next((item for item in skill_runs if (item.get("skill") or {}).get("id") == "opencli_browser_read"), None)
        browser_result = ((browser_run.get("result") or {}).get("opencli") or {}) if browser_run and browser_run.get("ok") else {}
        if usage_run.get("ok") and opencli_result.get("ok"):
            if floating_compact:
                answer = (
                    "结论：能用，ASA 后端已接入 OpenCLI。\n\n"
                    "下一步：可用于只读查看浏览器和网页状态。"
                )
            else:
                answer = (
                    "结论：能用。ASA 后端已经接入本机 OpenCLI，并刚刚通过 skill 调用验证成功。\n\n"
                    "依据：`opencli_usage` 调用了本机 `/Users/messi/.hermes/node/bin/opencli`，返回码为 0。\n\n"
                    "下一步：ASA 现在可以用 OpenCLI 做只读浏览器/网页状态读取；点击、填写、发送等写动作仍需要走审批工作流。"
                )
            if browser_run:
                if browser_result.get("ok"):
                    answer += "\n\n当前浏览器只读状态也已读取成功。"
                else:
                    answer += "\n\n当前浏览器只读状态暂未读到，可能还没有绑定 OpenCLI browser session。"
        else:
            reason = opencli_result.get("stderr") or opencli_result.get("reason") or usage_run.get("error") or "OpenCLI 调用失败"
            answer = (
                f"结论：OpenCLI 后端调用未成功。\n\n"
                f"下一步：检查 OpenCLI/Node 路径。错误：{str(reason)[:180]}"
            )
    elif pending_image_analysis:
        answer = (
            "结论：需要打开当前微信图片后才能识别。\n\n"
            "下一步：确认“打开并识别当前图片”；ASA 会本地识别后自动回答。"
        )
    elif capture_run and capture_run.get("ok"):
        capture_result = capture_run.get("result") or {}
        resume = capture_result.get("resume") or {}
        if floating_compact:
            answer = (
                f"结论：已补全 {resume.get('name') or '当前人选'} 的简历。\n\n"
                "下一步：ASA 会重新评估当前人岗关系。"
            )
        else:
            answer = (
                f"已从猎聘补全 {resume.get('name') or '当前人选'} 的完整简历。\n\n"
                f"工作经历 {int(resume.get('work_chars') or 0)} 字，"
                f"项目经历 {int(resume.get('project_chars') or 0)} 字，"
                f"教育经历 {int(resume.get('education_chars') or 0)} 字。\n\n"
                "简历已写入人才库，并正在重新评估当前人岗关系。"
            )
    elif capture_run:
        if floating_compact:
            answer = (
                "结论：简历补全失败。\n\n"
                "下一步：打开匹配的猎聘简历详情页后重试。"
            )
        else:
            answer = (
                "未能从猎聘补全当前人选的简历。\n\n"
                f"原因：{capture_run.get('error') or '猎聘简历读取失败'}\n\n"
                "请在猎聘打开与 ASA 当前选中人选一致的简历详情页后重试。"
            )
    elif forced_answer is not None:
        answer = forced_answer
    else:
        answer, model_tool_calls, tool_references = _generate_copilot_model_answer(
            self,
            sanitize_payload(payload),
        )
        references.extend(tool_references)
        if not answer:
            answer = "当前查询已完成，但暂时没有生成可用结论。"
        answer_source = "model_tools" if model_tool_calls else "model"
    persisted_payload = _persistable_attachment_payload(selected_payload)
    focus_context = (
        (goal_workflow.get("goal") or {}).get("context")
        if goal_workflow else persisted_payload
    ) or persisted_payload
    business_focus = self._persist_copilot_focus(
        session_id, message, focus_context,
        structured=persisted_payload, conflicts=focus_conflicts,
    )
    assistant_structured = {
        "references": references,
        "suggested_actions": suggested_actions,
        "skill_runs": skill_runs,
        "goal": goal_workflow.get("goal") if goal_workflow else None,
        "workflow": (
            goal_workflow.get("workflow")
            if goal_workflow else current_workflow_context.get("workflow") or None
        ),
        "plan_ref": (
            goal_workflow.get("plan_ref")
            if goal_workflow else current_workflow_context.get("plan_ref") or None
        ),
        "plan_summary": [
            {
                "id": step.get("id"),
                "capability_id": step.get("capability_id"),
                "label": step.get("business_label") or step.get("label"),
                "status": step.get("status"),
                "risk_level": step.get("risk_level"),
            }
            for step in (
                (goal_workflow.get("steps") or [])
                if goal_workflow else (current_workflow_context.get("steps") or [])
            )
        ],
        "business_focus": business_focus,
        "intent_understanding": intent_understanding,
        "turn_decision": turn_decision,
        "tool_calls": model_tool_calls,
        "model_participation": {
            "mode": answer_source,
            "label": (
                "模型生成 + 只读工具证据" if answer_source == "model_tools"
                else "模型生成 + 上下文约束" if answer_source == "model"
                else "规则生成"
            ),
            "model": (
                self.llm.copilot_runtime_metadata().get("model")
                if answer_source in {"model", "model_tools"} else None
            ),
            "routing": (
                self.llm.copilot_runtime_metadata()
                if answer_source in {"model", "model_tools"} else None
            ),
        },
    }
    if strategy_revision:
        assistant_structured["workflow_revision"] = strategy_revision
    # 策略建议结构化：本轮未直接执行修订时，从回答中提取可应用的策略补丁
    strategy_patch = (
        _build_strategy_patch(self, message, answer, selected_payload, conversation_history)
        if strategy_revision is None and not workflow_strategy_question else None
    )
    if strategy_patch:
        assistant_structured["strategy_patch"] = strategy_patch
    if strategy_gate_pending_record:
        assistant_structured["strategy_clarification"] = strategy_gate_pending_record
    elif strategy_gate_clarification:
        assistant_structured["strategy_clarification"] = {
            "status": "resolved",
            "job_id": int(pending_clarification.get("job_id") or 0) if pending_clarification else 0,
            "consultant_override": bool(strategy_gate_clarification.get("consultant_override")),
            "consultant_answers": str(strategy_gate_clarification.get("consultant_answers") or ""),
            "input_level": str(strategy_gate_clarification.get("input_level") or ""),
            "missing_anchors": list(strategy_gate_clarification.get("missing_anchors") or []),
        }
    if goal_workflow or current_workflow_context:
        workflow_state = goal_workflow or current_workflow_context
        workflow = workflow_state.get("workflow") or {}
        progress = workflow_state.get("progress") or {}
        assistant_structured["workflow_progress"] = {
            "workflow_id": workflow.get("workflow_id"),
            "status": workflow.get("status") or (workflow_state.get("goal") or {}).get("status") or "queued",
            "completed": progress.get("completed") or 0,
            "total": progress.get("total") or len(workflow_state.get("steps") or []),
            "label": workflow.get("current_stage") or "准备执行",
            "pending_approvals": [item for item in (workflow_state.get("approvals") or []) if item.get("status") == "pending"],
        }
    conn = self._connect()
    try:
        # 查询型名单直答：附带结构化名单卡（前端渲染可点击名单弹窗）。
        if candidate_list_card:
            assistant_structured["action_card"] = candidate_list_card
        # 寻访结果卡：已完成/阻塞的寻访工作流在对话中附带可展示的结果卡。
        if goal_workflow or current_workflow_context:
            workflow_state = goal_workflow or current_workflow_context
            workflow_id = str((workflow_state.get("workflow") or {}).get("workflow_id") or "")
            workflow_status = str(
                (workflow_state.get("workflow") or {}).get("status")
                or (workflow_state.get("goal") or {}).get("status")
                or ""
            )
            if workflow_id and workflow_status in {"completed", "blocked", "failed"}:
                try:
                    from . import sourcing_result_card

                    result_card = sourcing_result_card.build_sourcing_result_card(conn, workflow_id)
                    if result_card:
                        assistant_structured["action_card"] = result_card
                except Exception:
                    # 结果卡生成失败不应阻塞主回复。
                    pass
        conn.executemany(
            """
            INSERT INTO agent_copilot_messages
            (session_id,context_type,context_id,role,content,structured_json)
            VALUES (?,?,?,?,?,?)
            """,
            [
                (session_id, context_type, context_id, "user", message, _dumps(persisted_payload)),
                (
                    session_id,
                    context_type,
                    context_id,
                    "assistant",
                    answer,
                    _dumps(assistant_structured),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    # Phase 1.2: 触发对话摘要（非阻塞，失败不影响主流程）
    try:
        self._maybe_summarize_copilot_conversation(session_id)
    except Exception:
        pass
    return {
        "ok": True,
        "session_id": session_id,
        "answer": answer,
        "context": {"type": context_type, "id": context_id},
        "references": references,
        "suggested_actions": suggested_actions,
        "skill_runs": skill_runs,
        "model_participation": assistant_structured["model_participation"],
        "goal_id": goal_workflow["goal"]["goal_id"] if goal_workflow else None,
        "workflow_id": (
            goal_workflow["workflow"]["workflow_id"]
            if goal_workflow else (current_workflow_context.get("workflow") or {}).get("workflow_id")
        ),
        "goal": goal_workflow.get("goal") if goal_workflow else None,
        "workflow": (
            goal_workflow.get("workflow")
            if goal_workflow else current_workflow_context.get("workflow") or None
        ),
        "plan_ref": (
            goal_workflow.get("plan_ref")
            if goal_workflow else current_workflow_context.get("plan_ref") or None
        ),
        "plan_summary": [
            {
                "id": step.get("id"),
                "capability_id": step.get("capability_id"),
                "label": step.get("business_label") or step.get("label"),
                "status": step.get("status"),
                "risk_level": step.get("risk_level"),
                "reason": step.get("reason"),
            }
            for step in (
                (goal_workflow.get("steps") or [])
                if goal_workflow else (current_workflow_context.get("steps") or [])
            )
        ],
        "approvals": (
            goal_workflow.get("approvals")
            if goal_workflow else current_workflow_context.get("approvals") or []
        ),
        "artifacts": (
            goal_workflow.get("artifacts")
            if goal_workflow else (
                [current_workflow_context["strategy"]]
                if current_workflow_context.get("strategy") else []
            )
        ),
        "progress": (
            goal_workflow.get("progress")
            if goal_workflow else current_workflow_context.get("progress") or None
        ),
        "workflow_revision": strategy_revision,
        "strategy_patch": strategy_patch,
        "memory": {"mode": memories.get("mode"), "hits": len(memories.get("memories") or [])},
        "business_focus": business_focus,
        "intent_understanding": intent_understanding,
        "turn_decision": turn_decision,
        "tool_calls": model_tool_calls,
        "proactive_suggestions": generate_proactive_suggestions(str(self.db_path)),
        "action_card": assistant_structured.get("action_card"),
        "action_cards": [assistant_structured["action_card"]] if assistant_structured.get("action_card") else [],
    }


def chat(self, job_candidate_id: int, message: str, session_id: str = "") -> dict[str, Any]:
    message = " ".join(str(message or "").split())
    if not message:
        raise ValueError("请输入问题")
    context = build_candidate_context(self.db_path, int(job_candidate_id))
    state = self.get_candidate_state(int(job_candidate_id))
    assessment = state.get("assessment") or {}
    if not assessment:
        raise ValueError("请先完成当前人选的 Agent 评估")
    if _copilot_response_detail(message) == "expanded":
        answer = _format_candidate_evidence_answer(assessment)
    elif any(token in message for token in ["缺什么", "还缺", "核验"]):
        answer = _format_candidate_evidence_answer(assessment, gaps_only=True)
    else:
        answer = self.llm.chat(context["model_context"], assessment, message)
    session_id = session_id or f"candidate_{job_candidate_id}_{secrets.token_hex(4)}"
    conn = self._connect()
    try:
        conn.executemany(
            """
            INSERT INTO agent_messages(session_id,job_candidate_id,role,content,structured_json)
            VALUES (?,?,?,?,?)
            """,
            [
                (session_id, int(job_candidate_id), "user", message, "{}"),
                (session_id, int(job_candidate_id), "assistant", answer, _dumps({"assessment_id": assessment.get("id")})),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "session_id": session_id, "answer": answer}


# ---- Phase 1.2: 对话摘要记忆 ----

_CONVERSATION_SUMMARY_THRESHOLD = 6  # 每 6 轮用户消息生成一次摘要
_CONVERSATION_HISTORY_WINDOW = 8     # 保留最近 8 轮完整历史

_SUMMARY_SYSTEM_PROMPT = """你是 ASA 对话摘要器。将 copilot 对话历史压缩为结构化摘要。
只输出 JSON，不执行业务动作。摘要字段：
- stage: 当前业务阶段（如 "候选人评估中"/"寻访策略制定"/"待触达"/"面试跟进中"）
- entities: [{type: "job"|"candidate"|"client", id, name_or_title}] 涉及的关键实体
- decisions: ["已确认的决策列表"] 
- pending: ["待处理的待办"]
- key_facts: ["对话中确认的关键事实"]
只返回 JSON：{"stage":"...","entities":[...],"decisions":[...],"pending":[...],"key_facts":[...]}
"""


def _ensure_copilot_summaries_table(self, conn: Any) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_copilot_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            message_range_start INTEGER NOT NULL DEFAULT 0,
            message_range_end INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cs_session ON agent_copilot_summaries(session_id)")


def _ensure_copilot_events_table(self, conn: Any) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_copilot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ce_session ON agent_copilot_events(session_id)")


def record_copilot_event(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """记录 Copilot 事件，并将策略确认卡的终态写回会话以支持恢复展示。"""
    event = str(event or "").strip()
    if not event:
        raise ValueError("event 不能为空")
    session_id = str(session_id or "")
    payload = payload if isinstance(payload, dict) else {}
    conn = self._connect()
    try:
        _ensure_copilot_events_table(self, conn)
        conn.execute(
            "INSERT INTO agent_copilot_events (session_id,event,payload_json) VALUES (?,?,?)",
            (session_id, event, _dumps(payload)),
        )
        # 不新增交互接口：复用浮窗原有事件通道，将修订结果写回产生该确认卡的
        # assistant structured_json。仅匹配同一 session 内的策略卡，避免跨工作流串写。
        if event in {"copilot_strategy_applied", "copilot_strategy_reverted"} and _table_exists(conn, "agent_copilot_messages"):
            rows = conn.execute(
                """
                SELECT id,structured_json FROM agent_copilot_messages
                WHERE session_id=? AND role='assistant' ORDER BY id DESC
                """,
                (session_id,),
            ).fetchall()
            if event == "copilot_strategy_applied":
                workflow_id = str(payload.get("workflow_id") or "").strip()
                revised_workflow_id = str(payload.get("revised_workflow_id") or "").strip()
                for row in rows:
                    structured = _loads(row["structured_json"], {}) or {}
                    patch = structured.get("strategy_patch") if isinstance(structured.get("strategy_patch"), dict) else {}
                    if workflow_id and revised_workflow_id and str(patch.get("workflow_id") or "") == workflow_id:
                        structured.update({
                            "strategy_patch_applied": True,
                            "strategy_patch_revised_workflow_id": revised_workflow_id,
                        })
                        conn.execute(
                            "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                            (_dumps(structured), row["id"]),
                        )
                        break
            else:
                revised_workflow_id = str(payload.get("workflow_id") or "").strip()
                restored_workflow_id = str(payload.get("restored_workflow_id") or "").strip()
                for row in rows:
                    structured = _loads(row["structured_json"], {}) or {}
                    if revised_workflow_id and str(structured.get("strategy_patch_revised_workflow_id") or "") == revised_workflow_id:
                        structured.update({
                            "strategy_patch_reverted": True,
                            "strategy_patch_restored_workflow_id": restored_workflow_id,
                        })
                        conn.execute(
                            "UPDATE agent_copilot_messages SET structured_json=? WHERE id=?",
                            (_dumps(structured), row["id"]),
                        )
                        break
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _maybe_summarize_copilot_conversation(self, session_id: str) -> dict[str, Any] | None:
    """检查是否需要产生新的对话摘要。返回新摘要 dict 或 None。"""
    conn = self._connect()
    try:
        _ensure_copilot_summaries_table(self, conn)
        # 统计自上次摘要后的用户消息数
        last_summary = conn.execute(
            "SELECT MAX(message_range_end) FROM agent_copilot_summaries WHERE session_id=?",
            (session_id,),
        ).fetchone()
        last_end = int(last_summary[0]) if last_summary and last_summary[0] else 0
        user_count = conn.execute(
            "SELECT COUNT(*) FROM agent_copilot_messages WHERE session_id=? AND role='user' AND id>?",
            (session_id, last_end),
        ).fetchone()[0]
        if user_count < _CONVERSATION_SUMMARY_THRESHOLD:
            return None
        # 获取需要摘要的消息范围
        messages = conn.execute(
            """SELECT role,content FROM agent_copilot_messages
               WHERE session_id=? AND id>? AND role IN ('user','assistant')
               ORDER BY id""",
            (session_id, last_end),
        ).fetchall()
        if not messages:
            return None
        # 构建对话文本
        conversation_text = "\n".join(
            f"{'顾问' if row[0]=='user' else 'ASA'}: {row[1][:300]}"
            for row in messages[-20:]  # 最多取最近20条
        )
        context_state = self.get_copilot_context_state(session_id)
        deterministic_summary = deterministic_context_summary(context_state)
        # 调用 LLM 生成摘要；结构化状态始终作为保底事实，模型只补充表达。
        try:
            summary_text = self.llm._request(
                _SUMMARY_SYSTEM_PROMPT,
                {"conversation": conversation_text, "context_state": context_state},
                temperature=0.05,
                operation="copilot_summary",
            )
            model_summary = json.loads(summary_text.strip())
            if not isinstance(model_summary, dict):
                raise ValueError("summary must be an object")
            summary = dict(deterministic_summary)
            if str(model_summary.get("stage") or "").strip():
                summary["stage"] = str(model_summary["stage"]).strip()[:120]
            for key in ("entities", "decisions", "pending", "key_facts"):
                model_values = model_summary.get(key) if isinstance(model_summary.get(key), list) else []
                base_values = summary.get(key) if isinstance(summary.get(key), list) else []
                if key == "entities":
                    combined = [*base_values, *[item for item in model_values if isinstance(item, dict)]]
                    seen_entities: set[tuple[str, str, str]] = set()
                    summary[key] = []
                    for item in combined:
                        marker = (str(item.get("type") or ""), str(item.get("id") or ""), str(item.get("name_or_title") or ""))
                        if marker not in seen_entities:
                            seen_entities.add(marker)
                            summary[key].append(item)
                else:
                    summary[key] = list(dict.fromkeys(str(item) for item in [*base_values, *model_values] if str(item).strip()))[-16:]
        except Exception:
            summary = deterministic_summary
        # 持久化
        max_id = conn.execute(
            "SELECT MAX(id) FROM agent_copilot_messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0] or 0
        conn.execute(
            "INSERT INTO agent_copilot_summaries (session_id,summary_json,message_range_start,message_range_end) VALUES (?,?,?,?)",
            (session_id, json.dumps(summary, ensure_ascii=False), last_end, max_id),
        )
        conn.commit()
        return summary
    finally:
        conn.close()


def _copilot_conversation_context(self, session_id: str, conversation_history: list[dict[str, Any]]) -> dict[str, Any]:
    """构建注入 payload 的对话上下文（历史窗口 + 摘要）。"""
    conn = self._connect()
    try:
        _ensure_copilot_summaries_table(self, conn)
        summaries = conn.execute(
            "SELECT summary_json FROM agent_copilot_summaries WHERE session_id=? ORDER BY id DESC LIMIT 3",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    recent_history = conversation_history[-_CONVERSATION_HISTORY_WINDOW * 2:]  # user+assistant pairs
    return {
        "recent_history": recent_history,
        "summaries": [
            json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for row in summaries
        ] if summaries else [],
        "state": self.get_copilot_context_state(session_id),
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    """格式化 SSE 事件字符串。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def copilot_stream_generator(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
):
    """Expose the canonical Copilot result over SSE without duplicating decisions."""
    normalized = " ".join(str(message or "").split())
    if not normalized:
        yield _sse("error", {"error": "请输入问题"})
        return
    # SSE is transport only. Every message uses the canonical decision path so
    # clarification, focus, safety gates and workflow creation cannot diverge.
    result = self.copilot(normalized, session_id=session_id, context=context)
    yield _sse("context", {
        "session_id": result.get("session_id"),
        "context": result.get("context") or {},
        "references": result.get("references") or [],
        "suggested_actions": result.get("suggested_actions") or [],
    })
    answer = str(result.get("answer") or "")
    for offset in range(0, len(answer), 80):
        yield _sse("text", {"content": answer[offset:offset + 80]})
    yield _sse("done", result)
    return

# RETIRED: this pre-canonical tool loop is intentionally unbound. It remains
# temporarily for rollback archaeology; AgentService.copilot_agent is bound to
# copilot(), and all production traffic uses _generate_copilot_model_answer().

_MAX_TOOL_ROUNDS = 5  # 最多 5 轮工具调用


def _retired_legacy_copilot_agent(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retired pre-canonical tool loop; never bind this method in AgentService.
    
    与普通 copilot 的区别：LLM 可以主动调用工具（查DB、搜知识库等），
    工具结果注入对话后 LLM 综合给出最终回答。
    """
    from .copilot_tools import COPILOT_TOOLS, TOOL_EXECUTORS

    normalized = " ".join(str(message or "").split())
    if not normalized:
        raise ValueError("请输入问题")
    recent = self._copilot_conversation_history(session_id)
    awaiting_job_scope = bool(
        recent
        and recent[-1].get("role") == "assistant"
        and str(recent[-1].get("content") or "").strip() == "你要为哪个岗位补充并触达新候选人？"
    )
    if self._copilot_action_kind(normalized) or awaiting_job_scope:
        return self.copilot(normalized, session_id=session_id, context=context)
    stable_session_id = str(session_id or "").strip() or f"copilot_{secrets.token_hex(6)}"
    with self._copilot_locks_guard:
        session_lock = self._copilot_session_locks.setdefault(stable_session_id, threading.RLock())
    with session_lock:
        # ---- 复用 _copilot_impl 的预处理 ----
        message = " ".join(str(message or "").split())
        raw_context = dict(context or {})
        floating_compact = str(raw_context.get("display_mode") or "").strip() == "floating_compact"
        selected = self._normalize_copilot_context(raw_context)
        selected, focus_conflicts = self._copilot_context_from_focus(stable_session_id, message, selected)
        existing_focus = self.get_copilot_focus(stable_session_id)
        conversation_history = self._copilot_conversation_history(stable_session_id)
        if existing_focus:
            selected["business_focus"] = existing_focus

        context_type = selected["type"]
        context_id = selected.get("id")
        dashboard = self.get_dashboard()
        selected_payload: dict[str, Any] = dict(selected)
        references: list[dict[str, Any]] = []
        suggested_actions: list[dict[str, Any]] = []

        # 基础上下文注入
        if context_type == "candidate" and context_id:
            candidate_context = build_candidate_context(self.db_path, context_id)
            state = self.get_candidate_state(int(context_id))
            identity = candidate_context.get("identity", {})
            position = candidate_context.get("position", {})
            selected_payload["candidate"] = identity
            selected_payload["position"] = position
            selected_payload["assessment"] = _copilot_assessment_context(state.get("assessment") or {})
            references.append({"type": "candidate", "id": context_id, "label": identity.get("name") or "", "subtitle": f"{position.get('client','')}/{position.get('job','')}"})
        elif context_type == "job" and context_id:
            conn = self._connect()
            try:
                job = conn.execute(
                    "SELECT j.id,c.name AS client,j.title AS job FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?",
                    (context_id,),
                ).fetchone()
            finally:
                conn.close()
            if job:
                selected_payload["client"] = job["client"]
                selected_payload["job"] = job["job"]
                selected_payload["position"] = _copilot_job_evidence(self, int(context_id))
                references.append({"type": "job", "id": context_id, "label": job["job"], "subtitle": job["client"]})

        memories = self.search_memories(message, context_type=context_type, context_id=context_id)

        # ---- 工具调用 Agent 循环 ----
        response_mode = "floating_compact" if floating_compact else "default"
        response_detail = _copilot_response_detail(message)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": json.dumps({
                "question": message,
                "response_mode": response_mode,
                "response_detail": response_detail,
                "conversation": self._copilot_conversation_context(stable_session_id, conversation_history),
                "selected_context": selected_payload,
                "dashboard": {"summary": dashboard.get("summary", {}), "top_actions": dashboard.get("top_actions", [])[:5]},
                "approved_memories": memories.get("memories") or [] if memories.get("mode") == "active" else [],
            }, ensure_ascii=False)},
        ]

        tool_results: list[dict[str, Any]] = []
        final_answer = ""
        executed_calls: set[tuple[str, str]] = set()

        for round_num in range(_MAX_TOOL_ROUNDS):
            request_payload = {
                "question": message,
                "response_mode": response_mode,
                "response_detail": response_detail,
                "selected_context": selected_payload,
                "dashboard": {"summary": dashboard.get("summary", {}), "tool_round": round_num + 1},
            }
            response = self.llm.copilot_with_tools(request_payload, COPILOT_TOOLS, messages=messages)

            # 如果没有工具调用，直接用 content
            if not response.get("tool_calls"):
                final_answer = response.get("content", "")
                break

            messages.append({
                "role": "assistant",
                "content": response.get("content") or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)},
                    }
                    for tc in response["tool_calls"]
                ],
            })
            new_call_executed = False
            for tc in response["tool_calls"]:
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                call_key = (tool_name, json.dumps(tool_args, sort_keys=True, ensure_ascii=False))
                if call_key in executed_calls:
                    result = {"success": False, "error": "本轮已返回相同查询结果，请直接据此作答。"}
                else:
                    executed_calls.add(call_key)
                    new_call_executed = True
                    executor = TOOL_EXECUTORS.get(tool_name)
                    if executor:
                        try:
                            result = executor(str(self.db_path), **tool_args)
                        except Exception as exc:
                            result = {"success": False, "error": str(exc)}
                    else:
                        result = {"success": False, "error": f"未知工具: {tool_name}"}
                tool_results.append({"tool": tool_name, "args": tool_args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)})
                references.append({
                    "type": "tool_result",
                    "id": tc.get("id", ""),
                    "label": f"🔧 {tool_name}",
                    "subtitle": "成功" if result.get("success") else str(result.get("error", ""))[:80],
                })

            if not new_call_executed or round_num == _MAX_TOOL_ROUNDS - 1:
                final = self.llm.copilot_with_tools(request_payload, COPILOT_TOOLS, messages=messages, allow_tools=False)
                final_answer = final.get("content", "") or "已完成查询，但暂未生成可用结论。"
                break

        if not final_answer:
            final_answer = "已执行工具查询，请查看上方结果。"

        agent_assessment = selected_payload.get("assessment")
        if (
            context_type == "candidate"
            and isinstance(agent_assessment, dict)
            and agent_assessment.get("criteria")
            and _candidate_evidence_question(message)
        ):
            final_answer = _format_candidate_evidence_answer(agent_assessment)

        # ---- 后处理（与 _copilot_impl 一致） ----
        business_focus = self._persist_copilot_focus(
            stable_session_id, message, selected_payload,
            structured=selected_payload, conflicts=focus_conflicts,
        )
        # 策略建议结构化（与 _copilot_impl 同语义）：回答含可落地策略建议时出 patch
        strategy_patch = _build_strategy_patch(self, message, final_answer, selected_payload)
        assistant_structured = {
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": [],
            "tool_calls": tool_results,
            "business_focus": business_focus,
            "model_participation": {
                "mode": "model_tools",
                "label": "模型生成 + 工具证据",
                "model": self.llm.model,
            },
        }
        if strategy_patch:
            assistant_structured["strategy_patch"] = strategy_patch
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO agent_copilot_messages (session_id,context_type,context_id,role,content,structured_json) VALUES (?,?,?,?,?,?)""",
                [
                    (stable_session_id, context_type, context_id, "user", message, _dumps(selected_payload)),
                    (stable_session_id, context_type, context_id, "assistant", final_answer, _dumps(assistant_structured)),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            self._maybe_summarize_copilot_conversation(stable_session_id)
        except Exception:
            pass
        return {
            "ok": True,
            "session_id": stable_session_id,
            "answer": final_answer,
            "context": {"type": context_type, "id": context_id},
            "references": references,
            "suggested_actions": suggested_actions,
            "skill_runs": [],
            "tool_calls": tool_results,
            "business_focus": business_focus,
            "model_participation": assistant_structured["model_participation"],
            "memory": {"mode": memories.get("mode"), "hits": len(memories.get("memories") or [])},
            "proactive_suggestions": generate_proactive_suggestions(str(self.db_path)),
            "strategy_patch": strategy_patch,
        }
