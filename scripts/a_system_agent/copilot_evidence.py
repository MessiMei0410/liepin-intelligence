"""Copilot evidence/context extraction helpers (split from copilot_handler.py).

All functions receive 'self' (AgentService instance) as first parameter where present.
"""

from __future__ import annotations
import hashlib, re, sqlite3
from pathlib import Path
from typing import Any

from ._shared import (
    _loads,
    _row,
    _table_exists,
)
from .context import build_candidate_context
from .job_status import job_status_intake_allowed
from .conversation_state import (
    TERMINAL_WORKFLOW_STATUSES,
    _fact_scope,
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
    explicit = [
        item
        for _, _, item in scored
        if _job_is_explicitly_mentioned(cleaned, item)
    ]
    # Preserve every explicitly named job. A top-score winner is useful for a
    # client-only phrase, but it must not erase a second named job from a
    # sentence such as "机械岗位和软件岗位预算 120w".
    if len(explicit) >= 2:
        return explicit[: max(1, min(int(limit or 5), 10))]
    if len(explicit) == 1:
        return explicit
    # "长越的机械岗位" often matches every 长越岗位 through the client name,
    # while "机械" identifies one of them. Return that clear winner as a real
    # reference; only similarly scored jobs remain ambiguous for clarification.
    if len(scored) > 1 and scored[0][0] > scored[1][0] and scored[0][1]:
        return [scored[0][2]]
    return [item for _, _, item in scored[: max(1, min(int(limit or 5), 10))]]


_JOB_TITLE_ROLE_WORDS = (
    "高级", "资深", "初级", "中级", "首席", "工程师", "经理", "总监", "专家", "主管",
    "岗位", "职位",
)


def _job_title_cores(title: str) -> list[str]:
    cores: list[str] = []
    for part in re.split(r"[\s/（）()、,，｜|]+", str(title or "")):
        core = part.strip()
        for role_word in _JOB_TITLE_ROLE_WORDS:
            core = core.replace(role_word, "")
        core = core.strip()
        if len(core) >= 2 and core not in cores:
            cores.append(core)
    return cores


def _job_is_explicitly_mentioned(message: str, job: dict[str, Any]) -> bool:
    """Detect a concrete job mention without treating a client name as one."""
    text = " ".join(str(message or "").split())
    if not text or not isinstance(job, dict):
        return False
    try:
        job_id = int(job.get("id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    if job_id and re.search(rf"(?:#\s*|岗位\s*#?\s*){job_id}(?!\d)", text, re.I):
        return True
    title = str(job.get("job") or job.get("title") or "").strip()
    if title and title in text:
        return True
    for core in _job_title_cores(title):
        if not re.search(re.escape(core), text, re.I):
            continue
        # A core followed/preceded by a job noun is an explicit role mention;
        # this avoids treating shared words in a long JD as a target selector.
        if re.search(
            rf"{re.escape(core)}.{{0,5}}(?:岗|岗位|职位|方向|职缺)|"
            rf"(?:岗|岗位|职位|方向|职缺).{{0,5}}{re.escape(core)}",
            text,
            re.I,
        ):
            return True
    return False


def _explicitly_mentioned_job_ids(message: str, jobs: list[dict[str, Any]]) -> set[int]:
    ids: set[int] = set()
    for item in jobs:
        if not _job_is_explicitly_mentioned(message, item):
            continue
        try:
            job_id = int(item.get("id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if job_id > 0:
            ids.add(job_id)
    return ids


def _jobs_relevant_to_selected_context(
    mentioned_jobs: list[dict[str, Any]],
    selected: dict[str, Any],
    selected_facts: dict[str, Any],
    message: str = "",
) -> list[dict[str, Any]]:
    """Resolve client-only mentions without hiding explicitly named jobs."""
    jobs = [item for item in mentioned_jobs if isinstance(item, dict)]
    explicit_ids = _explicitly_mentioned_job_ids(message, jobs)
    if explicit_ids:
        explicit_jobs = []
        for item in jobs:
            try:
                item_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                item_id = 0
            if item_id in explicit_ids:
                explicit_jobs.append(item)
        if explicit_jobs:
            return explicit_jobs
    if len(jobs) <= 1:
        return jobs

    selected_job_id = _copilot_context_job_id(selected, selected_facts)
    if selected_job_id is None:
        return jobs

    matched = []
    for item in jobs:
        try:
            item_job_id = int(item.get("id") or 0) or None
        except (TypeError, ValueError):
            item_job_id = None
        if item_job_id == selected_job_id:
            matched.append(item)
    # An explicit mention of another job must remain visible instead of being
    # silently rewritten to the selected page object.
    return matched or jobs


def _copilot_context_job_id(
    selected: dict[str, Any], selected_facts: dict[str, Any]
) -> int | None:
    """Return the canonical job attached to a job, candidate, or workflow context."""
    candidates: list[Any] = []
    if str(selected.get("type") or "") == "job":
        candidates.append(selected.get("id"))
    job_facts = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    candidates.append(job_facts.get("id"))
    for value in candidates:
        try:
            job_id = int(value or 0)
        except (TypeError, ValueError):
            continue
        if job_id > 0:
            return job_id
    return None


def _copilot_context_job_record(selected_facts: dict[str, Any]) -> dict[str, Any]:
    """Convert context facts into the compact job shape used by goal grounding."""
    job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    job_id = _copilot_context_job_id({}, selected_facts)
    title = str(job.get("title") or job.get("job") or "").strip()
    client = str(selected_facts.get("client") or job.get("client") or "").strip()
    if not job_id or not title:
        return {}
    return {
        "id": job_id,
        "client": client,
        "job": title,
        "status": str(job.get("status") or ""),
    }


def _format_ambiguous_job_scope(client: str, jobs: list[dict[str, Any]]) -> str:
    labels = []
    for item in jobs[:4]:
        title = str(item.get("job") or "").strip()
        job_id = item.get("id")
        if title:
            labels.append(f"{title}（岗位 {job_id}）")
    options = "、".join(labels)
    scope = f"{client}" if client else "当前客户"
    detail = f"当前可见岗位包括：{options}。" if options else "当前识别到多个岗位。"
    return (
        f"结论：还不能唯一确定{scope}的目标岗位，暂不读取或创建任务。\n\n"
        f"依据：{detail}\n\n"
        "下一步：请补充岗位名称或岗位编号。"
    )


def _dedupe_copilot_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in references:
        if not isinstance(item, dict):
            continue
        reference_type = str(item.get("type") or "")
        reference_id = str(item.get("id") if item.get("id") is not None else "")
        # Some attachment references have no stable id; retain distinct files.
        fallback = "" if reference_id else str(item.get("label") or "")
        key = (reference_type, reference_id, fallback)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


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
    # "这个人选的预算/薪资" is compensation evidence for the candidate,
    # not the budget of the job linked to that candidate.
    if (
        re.search(r"(?:这个|该|当前|这位)?(?:人选|候选人|候选|人儿).{0,12}(?:预算|薪资|薪酬|总包|期望|预期|目前|现在|当前)", text)
        or re.search(r"(?:预算|薪资|薪酬|总包|期望|预期|目前|现在|当前).{0,12}(?:人选|候选人|候选)", text)
    ):
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


def _format_non_action_fact_answer(
    message: str,
    understanding: dict[str, Any],
    selected_facts: dict[str, Any],
) -> str:
    """Acknowledge contextual facts without turning them into workflow requests."""
    if str(understanding.get("action") or "none") != "none":
        return ""
    fact_updates = [
        item
        for item in (understanding.get("fact_updates") or [])
        if isinstance(item, dict)
    ]
    kinds = {str(item.get("kind") or "") for item in fact_updates}
    quote = " ".join(str(message or "").split())
    job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    candidate = selected_facts.get("candidate") if isinstance(selected_facts.get("candidate"), dict) else {}
    client = str(selected_facts.get("client") or job.get("client") or "").strip()
    title = str(job.get("title") or job.get("job") or "").strip()
    if "job_budget" in kinds:
        return _format_job_budget_fact_answer(message, selected_facts)
    if "candidate_compensation" in kinds:
        candidate_name = str(candidate.get("name") or "当前人选").strip()
        return (
            f"结论：已把「{quote}」记录为{candidate_name}的薪资事实，不创建谈薪任务。\n\n"
            "下一步：后续讨论匹配度或沟通口径时会沿用这组数据；需要整理谈薪方案时再明确下达任务。"
        )
    if "candidate_availability" in kinds or "candidate_preference" in kinds:
        candidate_name = str(candidate.get("name") or "当前人选").strip()
        fact_label = "到岗/意向事实" if "candidate_availability" in kinds and "candidate_preference" in kinds else (
            "到岗事实" if "candidate_availability" in kinds else "意向事实"
        )
        return (
            f"结论：已把「{quote}」记录为{candidate_name}的{fact_label}，不自动创建推进或触达任务。\n\n"
            "下一步：后续匹配和沟通判断会沿用这条信息；需要实际推进时请明确下达动作。"
        )
    if "job_requirement" in kinds:
        scope = " / ".join(part for part in (client, title) if part) or "当前岗位"
        return (
            f"结论：已把「{quote}」作为{scope}的岗位细节补充，不自动新建或启动任务。\n\n"
            "下一步：后续判断和策略讨论会沿用这些信息；需要写入岗位库时再明确说“更新岗位库”。"
        )
    if (
        str(understanding.get("topic") or "") == "candidate_match"
        and str(understanding.get("speech_act") or "") != "ask"
        and not _is_explicit_question(message)
        and any(token in quote for token in ("匹配", "完美", "适合", "符合", "不合适"))
    ):
        # 只把陈述句当作“匹配度看法”记录；疑问/请求解释（以及“补充简历”等仅因
        # topic 命中 candidate_match 的动作请求）让位给证据回答/技能路径。
        candidate_name = str(candidate.get("name") or "当前人选").strip()
        scope = " / ".join(part for part in (client, title) if part) or "当前岗位"
        return (
            f"结论：已记录你对{candidate_name}与{scope}匹配度的判断，不自动复核或生成推荐材料。\n\n"
            "下一步：如果要形成可核验的匹配结论，请明确说“复核这个人选”或“生成推荐报告”。"
        )
    if "client_preference" in kinds:
        scope = client or "当前客户"
        return (
            f"结论：已把「{quote}」记录为{scope}的客户偏好，不改变当前任务计划。\n\n"
            "下一步：后续筛选和沟通口径会参考这条偏好；需要修改岗位或计划时再明确下达动作。"
        )
    if "workflow_observation" in kinds:
        return (
            f"结论：已记录「{quote}」这条执行反馈，不自动新建或启动任务。\n\n"
            "下一步：需要基于反馈继续寻访、复核或调整策略时，请明确说出对应动作。"
        )
    return ""


_FACT_RECEIPT_KIND_PRIORITY = (
    "job_budget", "candidate_compensation", "candidate_availability",
    "candidate_preference", "job_requirement", "client_preference", "workflow_observation",
)


def _build_fact_receipt(
    message: str,
    understanding: dict[str, Any],
    selected_facts: dict[str, Any],
    conversation_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """与文本回执配套的结构化理解回执；没有可识别事实时返回 {}。"""
    if str(understanding.get("action") or "none") != "none":
        return {}
    fact_updates = [
        item
        for item in (understanding.get("fact_updates") or [])
        if isinstance(item, dict)
    ]
    kinds = {str(item.get("kind") or "") for item in fact_updates}
    kind = next((item for item in _FACT_RECEIPT_KIND_PRIORITY if item in kinds), "")
    if not kind and str(understanding.get("topic") or "") == "candidate_match":
        kind = "candidate_match"
    if not kind:
        return {}
    quote = " ".join(str(message or "").split())
    job = selected_facts.get("job") if isinstance(selected_facts.get("job"), dict) else {}
    candidate = selected_facts.get("candidate") if isinstance(selected_facts.get("candidate"), dict) else {}
    client = str(selected_facts.get("client") or job.get("client") or "").strip()
    title = str(job.get("title") or job.get("job") or "").strip()
    if kind.startswith("candidate_") or kind == "candidate_match":
        object_label = str(candidate.get("name") or "当前人选").strip()
    elif kind == "client_preference":
        object_label = client or "当前客户"
    else:
        object_label = " / ".join(part for part in (client, title) if part) or "当前岗位"
    value = next(
        (
            str(item.get("value") or "").strip()
            for item in fact_updates
            if str(item.get("kind") or "") == kind
        ),
        "",
    ) or quote
    scope = _fact_scope(
        kind,
        {
            "type": str((selected_facts.get("context") or {}).get("type") or "global"),
            "id": (selected_facts.get("context") or {}).get("id"),
            "job": job,
            "candidate": candidate,
        },
    )
    scope_label = (
        f"{scope.get('type')}:{scope.get('id')}"
        if scope.get("type") in {"job", "candidate"} and scope.get("id") not in (None, "")
        else "global"
    )
    state = conversation_state if isinstance(conversation_state, dict) else {}
    pending_plan = state.get("pending_plan") if isinstance(state.get("pending_plan"), dict) else {}
    active_context = state.get("active_context") if isinstance(state.get("active_context"), dict) else {}
    impact = "仅更新上下文，不启动工作流"
    if pending_plan.get("workflow_id") and str(pending_plan.get("status") or "planned") == "planned":
        scope_type = str(scope.get("type") or "")
        scope_id = scope.get("id")
        if scope_type == "job" and scope_id not in (None, ""):
            related = str((active_context.get("job") or {}).get("id") or "") == str(scope_id)
        elif scope_type == "candidate" and scope_id not in (None, ""):
            related = str((active_context.get("candidate") or {}).get("id") or "") == str(scope_id)
        else:
            # 全局事实无法定位到具体对象，保守视为与待确认计划相关。
            related = True
        if related:
            impact = "已记录的事实可能影响待确认计划，需重新确认"
    return {
        "object": object_label,
        "kind": kind,
        "quote": quote,
        "value": value,
        "scope": scope_label,
        "impact": impact,
    }


def _stopped_candidate_action_requested(
    message: str,
    understanding: dict[str, Any],
    turn_decision: dict[str, Any],
) -> bool:
    """Block only commands that would resume activity for a stopped relation."""
    text = " ".join(str(message or "").split())
    if not text or _is_explicit_question(text):
        return False
    action = str(understanding.get("action") or "none")
    fact_updates = [
        item
        for item in (understanding.get("fact_updates") or [])
        if isinstance(item, dict)
    ]
    if action == "none" and fact_updates:
        return False
    blocked_actions = {"candidate_outreach", "candidate_review", "recommendation", "salary"}
    if (
        action in blocked_actions
        and bool(understanding.get("action_evidence"))
        and bool(turn_decision.get("safe_for_action"))
    ):
        return True
    explicit_resume_patterns = (
        r"(?:继续|恢复|重新|重启|再).{0,10}(?:推进|复核|联系|触达|开聊|推荐|谈薪).{0,16}(?:人选|候选人|他|她)?",
        r"(?:推进|复核|联系|触达|开聊|推荐|谈薪).{0,12}(?:这个|当前|该)?(?:人选|候选人|他|她)",
        r"(?:这个|当前|该)?(?:人选|候选人|他|她).{0,12}(?:继续推进|恢复推进|联系|触达|开聊|推荐给客户|推给客户|谈薪)",
        r"(?:约|安排).{0,8}(?:面试|下一轮)",
    )
    return any(re.search(pattern, text, re.I) for pattern in explicit_resume_patterns)


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
    ) or bool(
        re.search(r"(?:岗位|职位).{0,24}(?:再找|继续找|重新找|找找).{0,8}(?:人|人选|候选人)", text)
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
    explicit_match = re.search(r"workflow_[0-9a-zA-Z]+", str(message or ""))
    selected_workflow_id = str(selected.get("id") or "").strip() if selected.get("type") == "workflow" else ""
    workflow_intent = selected.get("workflow_intent") if isinstance(selected.get("workflow_intent"), dict) else {}
    business_focus = selected.get("business_focus") if isinstance(selected.get("business_focus"), dict) else {}
    pending_workflow = business_focus.get("pending_workflow") if isinstance(business_focus.get("pending_workflow"), dict) else {}
    current_workflow = business_focus.get("current_workflow") if isinstance(business_focus.get("current_workflow"), dict) else {}
    focus_job = business_focus.get("job") if isinstance(business_focus.get("job"), dict) else {}
    trusted_workflow_ids = [
        selected_workflow_id,
        str(workflow_intent.get("workflow_id") or "").strip(),
        str(pending_workflow.get("workflow_id") or "").strip(),
        str(current_workflow.get("workflow_id") or "").strip(),
    ]
    bound_workflow_id = next(
        (value for value in trusted_workflow_ids if re.fullmatch(r"workflow_[0-9a-zA-Z]+", value)),
        "",
    )
    target_workflow_id = explicit_match.group(0) if explicit_match else bound_workflow_id
    try:
        job_id = int(
            selected.get("id")
            if selected.get("type") == "job"
            else selected.get("job_id") or focus_job.get("id") or 0
        )
    except (TypeError, ValueError):
        job_id = 0
    asked_round = _strategy_revision_round(message)
    # A visible job is scope, not a workflow selection. Only an explicit workflow,
    # this session's workflow binding, or an explicitly named round may be revised.
    if not target_workflow_id and not (job_id and asked_round is not None):
        return None, "当前会话没有明确选中的待修订工作流，请先打开工作流或明确轮次。"
    conn = self._connect()
    try:
        params: list[Any] = []
        where = "w.workflow_id=?" if target_workflow_id else "g.context_type='job' AND g.context_id=?"
        params.append(target_workflow_id or job_id)
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
    if target_workflow_id and rows and job_id and int(rows[0]["context_id"] or 0) != job_id:
        return None, "消息中的工作流不属于当前岗位，请重新确认目标。"
    eligible = [
        row for row in rows
        if str(row["status"] or "") in {"planned", "queued", "paused", "waiting_approval", "blocked", "failed"}
        and str(row["sourcing_status"] or "") in {"pending", "waiting_approval", "blocked", "failed"}
    ]
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
        strategy_hash = ""
        workflow_engine = getattr(self, "workflow_engine", None)
        if workflow_engine is not None:
            conn = self._connect()
            try:
                strategy_hash = str(
                    workflow_engine._sourcing_strategy_snapshot(conn, workflow_id).get("strategy_hash") or ""
                )
            finally:
                conn.close()
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
            "strategy_hash": strategy_hash,
            "changes": changes,
            "instruction_prefix": instruction_prefix,
            "instruction_suffix": _STRATEGY_PATCH_INSTRUCTION_SUFFIX,
            "consultant_evidence": evidence,
        }
    except Exception:
        return None


def _default_outreach_queue_inputs(self, message: str, selected: dict[str, Any]) -> tuple[list[int], dict[int, str]]:
    """触达队列 skill 的默认输入：取当前岗位 A 级候选人的 jc_id，优先级按消息关键词。"""
    job_id = 0
    if selected.get("type") == "job" and selected.get("id"):
        job_id = int(selected["id"])
    elif selected.get("job") and isinstance(selected.get("job"), dict):
        job_id = int((selected.get("job") or {}).get("id") or 0)
    if not job_id:
        return [], {}
    from .candidate_pool_filter import filter_job_candidates
    import sqlite3 as _sqlite3
    _client_name = ""
    try:
        _conn = _sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        _conn.row_factory = _sqlite3.Row
        try:
            _jrow = _conn.execute("SELECT c.name AS client FROM jobs j JOIN clients c ON c.id=j.client_id WHERE j.id=?", (job_id,)).fetchone()
            _client_name = str(_jrow["client"]) if _jrow is not None else ""
        finally:
            _conn.close()
    except Exception:
        _client_name = ""
    try:
        result = filter_job_candidates(self.db_path, job_id, client=_client_name)
    except Exception:
        try:
            result = filter_job_candidates(self.db_path, job_id)
        except Exception:
            return [], {}
    candidates = result.get("candidates") or []
    a_ids = [int(c["id"]) for c in candidates if c.get("grade", "").startswith("A") and c.get("id")]
    text = " ".join(str(message or "").split())
    default_prio = "P0" if any(t in text for t in ("优先", "P0", "最急")) else "P1" if "P1" in text else "P1"
    return a_ids[:30], {jc_id: default_prio for jc_id in a_ids[:30]}


# Lazy proxy to avoid the copilot_evidence <-> copilot_intent import cycle:
# copilot_intent imports several helpers from this module at top level, so this
# module cannot top-level import _is_explicit_question back from copilot_intent.
def _is_explicit_question(message: str) -> bool:
    from .copilot_intent import _is_explicit_question as _impl
    return _impl(message)
