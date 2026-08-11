"""寻访策略结构化按项编辑（一期可信推荐闭环缺口）。

与自由文本修订（revise_workflow，LLM 整体重生成）和 Copilot 补丁（仅 4 种 add）不同，
本模块提供确定性的按项编辑：更新/删除关键词组（terms、targets）、按 tier 更新/删除
公司池公司、更新职级映射 accepted_levels、更新顾问约束，并保留 add 语义。

语义约束：
- 每次编辑落一个新的 search_strategy artifact（新 revision），不原地改写旧 artifact；
  状态闸门与 revise_workflow 一致——外部寻访已开始（multi_channel_sourcing 离开
  pending/waiting_approval/blocked/failed）一律拒绝，返回可读 ValueError（API 409）。
- 编辑后重新编译 query_plan_v1 并校验，R3 审批语义不绕过：策略 hash 变化后，
  处于 waiting_approval 的寻访步骤作废旧审批卡并自动换新（新卡携带新快照）。
- query_builders 的质量约束不被编辑绕过：公司词不两两成对、单词条 ≤2 词、
  组内词条 ≤20、组数封顶 12、电源岗位禁裸公司词；违规则整体拒绝并说明，不落库。
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from ._shared import _dumps, _loads
from . import query_builders, strategy_v2

# 状态闸门：与 workflow.py revise_workflow 保持一致（外部寻访已开始则拒绝原地替换）。
_EDITABLE_WORKFLOW_STATUSES = {"planned", "queued", "paused", "waiting_approval", "blocked", "failed"}
_EDITABLE_SOURCING_STATUSES = {"pending", "waiting_approval", "blocked", "failed"}

# 质量约束（query_builders 规则链的编辑侧镜像）：
# 组数封顶；组内词条 ≤20（build_strategy_v2 terms[:20] 同款）；单词条 ≤2 词
# （LIEPIN_QUERY_MAX_TERMS，多词查询两渠道均实证 0 召回/误过滤）。
MAX_KEYWORD_GROUPS = 12
MAX_TERMS_PER_GROUP = 20
MAX_TOKENS_PER_TERM = 2
MAX_EDITS_PER_REQUEST = 20

_POOL_SOURCES = {
    "client_doc",
    "kb_graph",
    "kb_profile",
    "legacy_profile_suggestions",
    "llm_inferred",
    "consultant_calibrated",
}
_CONFIDENCES = {"high", "medium", "low"}
_CONSTRAINT_TYPES = {"hard_requirement", "preference", "conditional_acceptance", "exclusion", "target_count", "consultant_wording"}

SUPPORTED_OPS = (
    "add_keyword_group", "update_keyword_group", "delete_keyword_group",
    "add_company", "update_company", "delete_company",
    "update_accepted_levels", "update_consultant_constraints",
)


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _norm_terms(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[、；;，,/｜|\n]+", str(value or ""))
    terms = [_norm_text(item) for item in items]
    return [term for term in dict.fromkeys(terms) if term]


def _find_group(groups: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((group for group in groups if str(group.get("group") or "") == name), None)


def _find_pool(pools: list[dict[str, Any]], tier: str) -> dict[str, Any] | None:
    return next((pool for pool in pools if str(pool.get("tier") or "") == tier), None)


def apply_item_edits(v2: dict[str, Any], edits: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """对 strategy_v2 逐项应用编辑，返回 (新策略, 已应用操作留痕)。

    纯函数：不碰数据库。目标项不存在/字段非法/未知 op 抛 ValueError（API 409 可读错误）。
    """
    if not isinstance(v2, dict) or not v2:
        raise ValueError("当前策略缺少 strategy_v2 结构，不能按项编辑")
    if not isinstance(edits, list) or not edits:
        raise ValueError("编辑列表不能为空")
    if len(edits) > MAX_EDITS_PER_REQUEST:
        raise ValueError(f"单次最多 {MAX_EDITS_PER_REQUEST} 项编辑，本次提交了 {len(edits)} 项")

    new_v2 = json.loads(json.dumps(v2, ensure_ascii=False))
    groups = new_v2.setdefault("step4_keyword_groups", [])
    pools = new_v2.setdefault("step2_target_pool", [])
    step3 = new_v2.setdefault("step3_level_mapping", {})
    applied: list[dict[str, str]] = []

    for index, raw in enumerate(edits, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 项编辑必须是对象")
        op = _norm_text(raw.get("op"))
        if op not in SUPPORTED_OPS:
            raise ValueError(f"第 {index} 项编辑 op 非法：{op or '（缺失）'}，支持 {'/'.join(SUPPORTED_OPS)}")

        if op in {"add_keyword_group", "update_keyword_group", "delete_keyword_group"}:
            name = _norm_text(raw.get("group"))
            if not name:
                raise ValueError(f"第 {index} 项编辑缺少关键词组名 group")
            group = _find_group(groups, name)
            if op == "delete_keyword_group":
                if group is None:
                    raise ValueError(f"关键词组「{name}」不存在，不能删除（策略可能已被他人修改，请刷新）")
                groups.remove(group)
                applied.append({"op": op, "summary": f"删除关键词组「{name}」"})
                continue
            terms = _norm_terms(raw.get("terms")) if raw.get("terms") is not None else None
            targets = _norm_text(raw.get("targets")) if raw.get("targets") is not None else None
            if op == "add_keyword_group":
                if group is not None:
                    raise ValueError(f"关键词组「{name}」已存在，不能重复新增")
                if not terms:
                    raise ValueError(f"新增关键词组「{name}」必须提供至少 1 个词条 terms")
                groups.append({"group": name, "targets": targets or "", "terms": terms})
                applied.append({"op": op, "summary": f"新增关键词组「{name}」（{len(terms)} 词）"})
                continue
            # update_keyword_group
            if group is None:
                raise ValueError(f"关键词组「{name}」不存在，不能更新（策略可能已被他人修改，请刷新）")
            if terms is None and targets is None:
                raise ValueError(f"更新关键词组「{name}」至少要提供 terms 或 targets 之一")
            if terms is not None:
                if not terms:
                    raise ValueError(f"关键词组「{name}」的 terms 不能为空（要移除请用删除操作）")
                group["terms"] = terms
            if targets is not None:
                group["targets"] = targets
            applied.append({"op": op, "summary": f"更新关键词组「{name}」"})

        elif op in {"add_company", "update_company", "delete_company"}:
            tier = _norm_text(raw.get("tier")).upper()
            if tier not in {"T1", "T2", "T3"}:
                raise ValueError(f"第 {index} 项编辑 tier 必须是 T1/T2/T3")
            name = _norm_text(raw.get("name"))
            if not name:
                raise ValueError(f"第 {index} 项编辑缺少公司名 name")
            pool = _find_pool(pools, tier)
            companies = pool.get("companies") if isinstance(pool, dict) else None
            companies = companies if isinstance(companies, list) else []
            company = next(
                (item for item in companies if isinstance(item, dict) and str(item.get("name") or "") == name),
                None,
            )
            if op == "add_company":
                if company is not None:
                    raise ValueError(f"公司「{name}」已在 {tier} 池内，不能重复新增")
                source = _norm_text(raw.get("source")) or "client_doc"
                if source not in _POOL_SOURCES:
                    raise ValueError(f"公司「{name}」的 source 非法：{source}")
                confidence = _norm_text(raw.get("confidence")) or "medium"
                if confidence not in _CONFIDENCES:
                    raise ValueError(f"公司「{name}」的 confidence 非法：{confidence}")
                if pool is None:
                    pool = {
                        "path": "same_layer", "tier": tier, "companies": [],
                        "rationale": "顾问按项编辑新增的公司池分层",
                    }
                    pools.append(pool)
                    companies = pool["companies"]
                companies.append({"name": name, "source": source, "confidence": confidence})
                applied.append({"op": op, "summary": f"{tier} 池新增公司「{name}」"})
                continue
            if company is None:
                raise ValueError(f"公司「{name}」不在 {tier} 池内（策略可能已被他人修改，请刷新）")
            if op == "delete_company":
                companies.remove(company)
                applied.append({"op": op, "summary": f"{tier} 池删除公司「{name}」"})
            else:
                new_name = _norm_text(raw.get("new_name"))
                confidence = _norm_text(raw.get("confidence"))
                if not new_name and not confidence:
                    raise ValueError(f"更新公司「{name}」至少要提供 new_name 或 confidence 之一")
                if new_name:
                    company["name"] = new_name
                if confidence:
                    if confidence not in _CONFIDENCES:
                        raise ValueError(f"公司「{name}」的 confidence 非法：{confidence}")
                    company["confidence"] = confidence
                applied.append({"op": op, "summary": f"{tier} 池更新公司「{name}」"})
            # 池内公司删空则整层移除（validate_strategy_v2 要求 companies 非空）
            if pool is not None and not pool.get("companies"):
                pools.remove(pool)

        elif op == "update_accepted_levels":
            levels = _norm_terms(raw.get("accepted_levels"))
            if not levels:
                raise ValueError("accepted_levels 不能为空（至少保留一个可接受职级）")
            step3["accepted_levels"] = levels
            # evaluation_constraints.levels 在 build 时固化，编辑时必须同步，否则编译侧读到旧值
            constraints = new_v2.get("evaluation_constraints")
            if isinstance(constraints, dict) and "levels" in constraints:
                constraints["levels"] = list(levels)
            applied.append({"op": op, "summary": f"职级映射更新为：{'、'.join(levels[:6])}"})

        else:  # update_consultant_constraints
            raw_constraints = raw.get("constraints")
            if not isinstance(raw_constraints, list):
                raise ValueError("update_consultant_constraints 需要提供 constraints 数组")
            constraints: list[dict[str, str]] = []
            for item in raw_constraints:
                if not isinstance(item, dict):
                    continue
                rule = _norm_text(item.get("rule"))
                if not rule:
                    continue
                constraint_type = _norm_text(item.get("type")) or "consultant_wording"
                if constraint_type not in _CONSTRAINT_TYPES:
                    constraint_type = "consultant_wording"
                constraints.append({"type": constraint_type, "rule": rule, "source": "consultant_item_edit"})
            new_v2["consultant_constraints"] = constraints
            applied.append({"op": op, "summary": f"顾问约束更新为 {len(constraints)} 条"})

    strategy_v2.refresh_consultant_judgement(new_v2)
    return new_v2, applied


def validate_edited_strategy(v2: dict[str, Any]) -> list[str]:
    """编辑后整策略质量门：schema 校验 + query_builders 规则链镜像约束。返回错误列表（空=通过）。"""
    ok, errors = strategy_v2.validate_strategy_v2(v2)
    errors = list(errors)
    if not ok:
        return errors

    groups = [group for group in v2.get("step4_keyword_groups") or [] if isinstance(group, dict)]
    if len(groups) > MAX_KEYWORD_GROUPS:
        errors.append(f"关键词组数 {len(groups)} 超过封顶 {MAX_KEYWORD_GROUPS}")
    if not groups and not any(
        isinstance(pool, dict) and pool.get("companies")
        for pool in v2.get("step2_target_pool") or []
    ):
        errors.append("编辑后策略没有任何关键词组和目标公司，无可执行查询")

    vocab = query_builders.company_vocabulary({"strategy_v2": v2})
    power_role = query_builders._requires_power_evidence(v2)
    for group in groups:
        name = str(group.get("group") or "（未命名）")
        terms = [str(term) for term in group.get("terms") or [] if str(term or "").strip()]
        if len(terms) > MAX_TERMS_PER_GROUP:
            errors.append(f"关键词组「{name}」词条数 {len(terms)} 超过封顶 {MAX_TERMS_PER_GROUP}")
            terms = terms[:MAX_TERMS_PER_GROUP]
        for term in terms:
            tokens = [token for token in term.split() if token]
            if len(tokens) > MAX_TOKENS_PER_TERM:
                errors.append(f"关键词「{term}」超过 {MAX_TOKENS_PER_TERM} 个词（渠道按 ≤2 词短查询构造，请拆分）")
            company_hits = [token for token in tokens if query_builders.is_company_token(token, vocab)]
            if len(company_hits) >= 2:
                errors.append(f"关键词「{term}」同时包含两个公司词（{'、'.join(company_hits)}），公司词不能两两成对")
            if power_role and len(tokens) == 1 and company_hits:
                errors.append(f"电源岗位关键词组不允许裸公司词「{term}」（需搭配业务/技术词）")
    return errors


def _refresh_plan_channels(plan: dict[str, Any], query_plan: dict[str, Any]) -> None:
    """按新 query_plan 重刷 plan.channels 展示层查询；purpose 沿旧值，新查询如实标注。"""
    channels = plan.get("channels")
    if not isinstance(channels, dict) or not channels:
        return
    for channel in ("liepin", "xsaas"):
        old_items = channels.get(channel) if isinstance(channels.get(channel), list) else []
        purpose_by_query = {
            " ".join(str(item.get("query") or "").split()): str(item.get("purpose") or "")
            for item in old_items
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        }
        channels[channel] = [
            {
                "query": query,
                "purpose": purpose_by_query.get(query) or "按项编辑后的寻访查询",
            }
            for query in query_builders.query_plan_channel_queries(query_plan, channel)
        ]


def apply_strategy_item_edits(
    self,
    workflow_id: str,
    edits: list[dict[str, Any]],
    *,
    note: str = "",
) -> dict[str, Any]:
    """按项编辑工作流寻访策略（self=AgentService）。落新 search_strategy artifact 并回读。

    LookupError（API 404）：工作流不存在/还没有 strategy_v2 策略。
    ValueError（API 409）：状态闸门拒绝、目标项不存在、质量校验不过、查询计划不可执行。
    """
    conn = self._connect()
    try:
        workflow = conn.execute(
            "SELECT * FROM agent_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if workflow is None:
            raise LookupError(f"工作流不存在：{workflow_id}")
        if workflow["status"] not in _EDITABLE_WORKFLOW_STATUSES:
            raise ValueError("工作流已进入外部执行或已结束，不能按项编辑策略")
        sourcing_step = conn.execute(
            """
            SELECT * FROM agent_workflow_steps
            WHERE workflow_id=? AND capability_id='multi_channel_sourcing'
            ORDER BY sequence LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
        if sourcing_step is None or sourcing_step["status"] not in _EDITABLE_SOURCING_STATUSES:
            raise ValueError("外部寻访已经开始，当前策略不能原地替换；请先停止本轮寻访再调整")
        strategy_step = conn.execute(
            """
            SELECT * FROM agent_workflow_steps
            WHERE workflow_id=? AND capability_id='search_strategy'
            ORDER BY sequence DESC LIMIT 1
            """,
            (workflow_id,),
        ).fetchone()
        _content, metadata = self.workflow_engine._latest_artifact_payload(conn, workflow_id, "search_strategy")
        v2 = metadata.get("strategy_v2") if isinstance(metadata.get("strategy_v2"), dict) else {}
        if not v2:
            raise LookupError(f"该工作流还没有可编辑的寻访策略（缺少 strategy_v2）：{workflow_id}")
        plan = metadata.get("plan") if isinstance(metadata.get("plan"), dict) else {}

        new_v2, applied = apply_item_edits(v2, edits)
        errors = validate_edited_strategy(new_v2)
        if errors:
            raise ValueError("编辑后策略未通过质量校验：" + "；".join(errors[:6]))
        query_plan = query_builders.compile_query_plan_v1(new_v2)
        plan_ok, plan_errors = query_builders.validate_query_plan_v1(query_plan)
        if not plan_ok:
            raise ValueError("编辑后查询计划不可执行：" + "；".join(plan_errors[:6]))

        revision = int(v2.get("edit_revision") or 0) + 1
        new_v2["edit_revision"] = revision
        edit_note = _norm_text(note)[:200]
        new_v2["consultant_edits"] = [
            *(new_v2.get("consultant_edits") if isinstance(new_v2.get("consultant_edits"), list) else []),
            *(
                {
                    "type": "item_edit",
                    "op": item["op"],
                    "rule": item["summary"],
                    "note": edit_note,
                    "edit_revision": revision,
                    "source": "consultant_item_edit",
                }
                for item in applied
            ),
        ][-48:]
        trace = new_v2.get("classification_trace")
        new_v2["classification_trace"] = [
            *(trace if isinstance(trace, list) else []),
            f"按项编辑 revision {revision}：{'；'.join(item['summary'] for item in applied)[:300]}；golden replay 未重算（留痕）",
        ]
        if isinstance(plan, dict) and plan:
            _refresh_plan_channels(plan, query_plan)
            if any(item["op"] == "update_consultant_constraints" for item in applied):
                plan["consultant_constraints"] = new_v2.get("consultant_constraints") or []

        artifact_id = f"artifact_{secrets.token_hex(6)}"
        content = (
            f"# 多渠道寻访策略（strategy_v2 · 按项编辑 revision {revision}）\n\n```json\n"
            + json.dumps(new_v2, ensure_ascii=False, indent=2)
            + "\n```"
        )
        new_metadata = {
            **metadata,
            "plan": plan,
            "strategy_v2": new_v2,
            "query_plan_v1": query_plan,
            # 旧 golden replay 基于旧查询网格，不再作数；置空后快照按“无回放”处理
            "golden_candidate_replay_v1": None,
            "schema_version": strategy_v2.STRATEGY_V2_VERSION,
            "edit_revision": revision,
            "edited_via": "strategy_item_edit",
        }
        conn.execute(
            """
            INSERT INTO agent_artifacts
            (artifact_id,goal_id,workflow_id,step_id,artifact_type,title,mime_type,file_path,content,metadata_json,validation_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact_id, workflow["goal_id"], workflow_id,
                int(strategy_step["id"]) if strategy_step is not None else None,
                "search_strategy", f"多渠道寻访策略（按项编辑 revision {revision}）",
                "text/markdown", None, content, _dumps(new_metadata), "passed",
            ),
        )
        # 同步 search_strategy 步骤 output，面板读取与执行侧看到的是同一份新策略
        if strategy_step is not None:
            output = _loads(strategy_step["output_json"], {})
            if isinstance(output, dict) and output:
                output["strategy_v2"] = new_v2
                output["query_plan_v1"] = query_plan
                if isinstance(plan, dict) and plan:
                    output["strategy"] = plan
                conn.execute(
                    "UPDATE agent_workflow_steps SET output_json=?,updated_at=datetime('now','localtime') WHERE id=?",
                    (_dumps(output), int(strategy_step["id"])),
                )
        # R3 审批语义：策略 hash 已变，waiting_approval 的旧审批卡作废并自动换新（新快照）
        approval_refreshed = False
        if sourcing_step["status"] == "waiting_approval":
            for row in conn.execute(
                "SELECT id,approval_id FROM agent_approvals WHERE step_id=? AND status='pending'",
                (int(sourcing_step["id"]),),
            ).fetchall():
                conn.execute(
                    "UPDATE agent_approvals SET status=?,decided_at=datetime('now','localtime'),decision_note='策略已按项编辑，原审批卡失效' WHERE id=?",
                    (f"superseded_strategy_edit_{row['approval_id']}", int(row["id"])),
                )
            self.workflow_engine._create_approval(conn, workflow["goal_id"], workflow_id, sourcing_step)
            approval_refreshed = True
        self.workflow_engine._event(
            conn, workflow_id,
            int(strategy_step["id"]) if strategy_step is not None else None,
            "strategy_item_edited", str(workflow["status"]),
            f"策略按项编辑 revision {revision}：{'；'.join(item['summary'] for item in applied)[:200]}",
            {"edit_revision": revision, "applied": applied, "note": edit_note, "artifact_id": artifact_id},
        )
        conn.commit()

        # 结果回读：以落库后的最新 artifact 重算快照，返回真实 strategy_hash
        snapshot = self.workflow_engine._sourcing_strategy_snapshot(conn, workflow_id)
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "revision": revision,
            "applied": applied,
            "edit_count": len(applied),
            "artifact_id": artifact_id,
            "approval_refreshed": approval_refreshed,
            "strategy_hash": snapshot["strategy_hash"],
            "query_plan_hash": snapshot["query_plan_hash"],
            "strategy_ready": bool(snapshot["ready"]),
            "strategy_v2": new_v2,
        }
    finally:
        conn.close()
