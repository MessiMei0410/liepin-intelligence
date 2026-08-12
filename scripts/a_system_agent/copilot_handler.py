"""Facade for the Copilot handler.

Implementation has been split into five domain modules:
- copilot_evidence.py  — evidence/context extraction, strategy patch helpers
- copilot_intent.py    — intent detection, message interpretation, plan anchors
- copilot_sessions.py  — session/focus/context state management
- copilot_routing.py   — strategy gate, skill routing, main _copilot_impl
- copilot_api.py       — public API (copilot/chat), events, summaries, SSE stream

This module only re-exports the split functions and provides thin forwarding
wrappers for the 10 public entry points, so existing imports and AgentService
method bindings keep working unchanged.
"""

from __future__ import annotations
from typing import Any

from .copilot_evidence import (
    _copilot_response_detail,
    _copilot_assessment_context,
    _candidate_evidence_question,
    _copilot_list_value,
    _copilot_job_evidence,
    _format_candidate_evidence_answer,
    _normalize_copilot_context,
    _floating_bridge_evidence,
    _uploaded_attachment_evidence,
    _persistable_attachment_payload,
    _client_aliases,
    _mentioned_jobs_for_copilot,
    _job_title_cores,
    _job_is_explicitly_mentioned,
    _explicitly_mentioned_job_ids,
    _jobs_relevant_to_selected_context,
    _copilot_context_job_id,
    _copilot_context_job_record,
    _format_ambiguous_job_scope,
    _dedupe_copilot_references,
    _reconcile_copilot_runtime_state,
    _copilot_focus_from_joined_row,
    _is_job_budget_fact_update,
    _format_job_budget_fact_answer,
    _format_non_action_fact_answer,
    _build_fact_receipt,
    _stopped_candidate_action_requested,
    _is_candidate_result_observation,
    _format_candidate_result_observation_answer,
    _new_candidate_outreach_requested,
    _continued_sourcing_requested,
    _strategy_revision_requested,
    _pending_sourcing_refinement_mode,
    _confirmed_assistant_refinement,
    _strategy_revision_round,
    _strategy_revision_evidence,
    _strategy_revision_instruction,
    _resolve_strategy_revision_workflow,
    _strategy_patch_candidate,
    _strategy_term_key,
    _strategy_v2_existing_values,
    _normalize_strategy_patch_changes,
    _build_strategy_patch,
    _default_outreach_queue_inputs,
)
from .copilot_intent import (
    _copilot_action_kind,
    _is_contextual_job_detail_message,
    _plan_confirmation_reply,
    _salary_plan_confirmation_reply,
    _salary_recap_amounts,
    _deterministic_non_action_intent,
    _is_job_requirement_message,
    _is_plan_control_instruction,
    _is_explicit_question,
    _is_plain_query,
    _is_candidate_list_query,
    _requests_grade_filter,
    _format_candidate_list_answer,
    _build_candidate_list_card,
    _is_candidate_list_composition_question,
    _build_candidate_list_composition_answer,
    _verbatim_constraint_candidates,
    _interpret_copilot_message,
    _latest_assistant_plan_anchor,
    _latest_assistant_plan_confirmation,
    _copilot_plan_from_anchor,
    _copilot_plan_matches_selected,
    _copilot_pending_plan,
    _copilot_focus_context_facts,
    _copilot_workflow_context_facts,
    _workflow_strategy_question,
    _compact_workflow_context,
)
from .copilot_sessions import (
    _format_workflow_strategy_answer,
    _format_context_mismatch_answer,
    _copilot_context_facts,
    _copilot_context_from_focus,
    _copilot_workflow_outcome_context,
    _persist_copilot_focus,
    _copilot_conversation_history,
    _copilot_session_business_evidence,
    _ground_copilot_goal,
    _pending_strategy_clarification,
)
from .copilot_routing import (
    _sourcing_strategy_gate,
    _mentioned_client_names,
    _route_copilot_skills,
    _generate_copilot_model_answer,
    _copilot_impl,
)
from .copilot_api import (
    _ensure_copilot_summaries_table,
    _ensure_copilot_events_table,
    _maybe_summarize_copilot_conversation,
    _copilot_conversation_context,
    _sse,
    _retired_legacy_copilot_agent,
)


def get_copilot_context_state(self, session_id: str) -> dict[str, Any]:
    from .copilot_evidence import get_copilot_context_state as _impl
    return _impl(self, session_id)


def get_copilot_focus(self, session_id: str) -> dict[str, Any] | None:
    from .copilot_evidence import get_copilot_focus as _impl
    return _impl(self, session_id)


def get_copilot_session(self, session_id: str, limit: int = 100) -> dict[str, Any]:
    from .copilot_sessions import get_copilot_session as _impl
    return _impl(self, session_id, limit=limit)


def list_copilot_sessions(
    self,
    limit: int = 30,
    query: str = "",
    include_archived: bool = False,
) -> dict[str, Any]:
    from .copilot_sessions import list_copilot_sessions as _impl
    return _impl(self, limit=limit, query=query, include_archived=include_archived)


def update_copilot_session(
    self,
    session_id: str,
    *,
    title: str | None = None,
    archived: bool | None = None,
    clear_focus: bool = False,
) -> dict[str, Any]:
    from .copilot_sessions import update_copilot_session as _impl
    return _impl(self, session_id, title=title, archived=archived, clear_focus=clear_focus)


def archive_all_copilot_sessions(self) -> dict[str, Any]:
    from .copilot_sessions import archive_all_copilot_sessions as _impl
    return _impl(self)


def copilot(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .copilot_api import copilot as _impl
    return _impl(self, message, session_id=session_id, context=context)


def chat(self, job_candidate_id: int, message: str, session_id: str = "") -> dict[str, Any]:
    from .copilot_api import chat as _impl
    return _impl(self, job_candidate_id, message, session_id=session_id)


def record_copilot_event(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from .copilot_api import record_copilot_event as _impl
    return _impl(self, session_id, event, payload=payload)


def copilot_stream_generator(
    self,
    message: str,
    *,
    session_id: str = "",
    context: dict[str, Any] | None = None,
):
    from .copilot_api import copilot_stream_generator as _impl
    return _impl(self, message, session_id=session_id, context=context)
