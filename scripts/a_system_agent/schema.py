from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    context_type TEXT NOT NULL,
    context_id INTEGER NOT NULL,
    snapshot_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger TEXT,
    model TEXT,
    prompt_version TEXT,
    reviewer_used INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_context
ON agent_runs(context_type, context_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_snapshot
ON agent_runs(kind, context_id, snapshot_hash, status);

CREATE TABLE IF NOT EXISTS agent_candidate_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    job_candidate_id INTEGER NOT NULL,
    candidate_id INTEGER,
    person_id INTEGER NOT NULL,
    job_id INTEGER,
    client TEXT,
    job TEXT,
    snapshot_hash TEXT NOT NULL,
    assessment_version TEXT NOT NULL,
    fit_score INTEGER NOT NULL,
    fit_level TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_coverage REAL NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '{}',
    strengths_json TEXT NOT NULL DEFAULT '[]',
    gaps_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    verification_questions_json TEXT NOT NULL DEFAULT '[]',
    next_action TEXT,
    outreach_angle TEXT,
    citations_json TEXT NOT NULL DEFAULT '[]',
    policy_json TEXT NOT NULL DEFAULT '{}',
    reviewer_json TEXT NOT NULL DEFAULT '{}',
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id),
    FOREIGN KEY(job_candidate_id) REFERENCES job_candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_assessment_current
ON agent_candidate_assessments(job_candidate_id, is_current, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_review_panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    job_candidate_id INTEGER NOT NULL,
    assessment_id INTEGER NOT NULL,
    snapshot_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    model_mode TEXT NOT NULL DEFAULT 'hybrid',
    synthesis_json TEXT NOT NULL DEFAULT '{}',
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    completed_at TEXT,
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id),
    FOREIGN KEY(job_candidate_id) REFERENCES job_candidates(id),
    FOREIGN KEY(assessment_id) REFERENCES agent_candidate_assessments(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_review_panels_current
ON agent_review_panels(job_candidate_id, is_current, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_role_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id TEXT NOT NULL,
    role TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL,
    findings_json TEXT NOT NULL DEFAULT '[]',
    questions_json TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT,
    source TEXT NOT NULL DEFAULT 'rules',
    model TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(panel_id, role),
    FOREIGN KEY(panel_id) REFERENCES agent_review_panels(panel_id)
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_session
ON agent_messages(session_id, id);

CREATE TABLE IF NOT EXISTS agent_copilot_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    context_type TEXT NOT NULL DEFAULT 'global',
    context_id INTEGER,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_copilot_session
ON agent_copilot_messages(session_id, id);

CREATE TABLE IF NOT EXISTS agent_copilot_attachments (
    attachment_id TEXT PRIMARY KEY,
    access_token_hash TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_accessed_at TEXT,
    expires_at TEXT NOT NULL DEFAULT (datetime('now','+7 days'))
);

CREATE INDEX IF NOT EXISTS idx_copilot_attachments_session
ON agent_copilot_attachments(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    fallback_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    request_hash TEXT NOT NULL,
    request_preview TEXT NOT NULL DEFAULT '',
    response_preview TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_model_calls_recent
ON agent_model_calls(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_agent_model_calls_operation
ON agent_model_calls(operation, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_copilot_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_copilot_sessions_updated
ON agent_copilot_sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_copilot_focus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL DEFAULT 1,
    context_type TEXT NOT NULL DEFAULT 'global',
    context_id INTEGER,
    client TEXT,
    job_id INTEGER,
    candidate_id INTEGER,
    action TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    focus_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(candidate_id) REFERENCES job_candidates(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_copilot_focus_updated
ON agent_copilot_focus(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_copilot_state (
    session_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_copilot_state_updated
ON agent_copilot_state(updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    job_candidate_id INTEGER,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    request_json TEXT NOT NULL DEFAULT '{}',
    preflight_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_action_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL UNIQUE,
    job_candidate_id INTEGER NOT NULL,
    assessment_id INTEGER,
    snapshot_hash TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    action_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    title TEXT NOT NULL,
    rationale TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    preflight_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TEXT,
    review_note TEXT,
    action_id INTEGER,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(job_candidate_id) REFERENCES job_candidates(id),
    FOREIGN KEY(assessment_id) REFERENCES agent_candidate_assessments(id),
    FOREIGN KEY(action_id) REFERENCES agent_actions(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_queue
ON agent_action_proposals(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_proposals_candidate
ON agent_action_proposals(job_candidate_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_stage_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL,
    assessment_id INTEGER NOT NULL UNIQUE,
    snapshot_hash TEXT NOT NULL,
    current_stage TEXT,
    proposed_stage TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'shadow',
    status TEXT NOT NULL DEFAULT 'pending',
    executed INTEGER NOT NULL DEFAULT 0,
    action_type TEXT NOT NULL DEFAULT 'internal_stage_recommendation',
    undo_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(job_candidate_id) REFERENCES job_candidates(id),
    FOREIGN KEY(assessment_id) REFERENCES agent_candidate_assessments(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_stage_recommendations_queue
ON agent_stage_recommendations(mode, status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL,
    corrected_json TEXT NOT NULL DEFAULT '{}',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(assessment_id) REFERENCES agent_candidate_assessments(id)
);

CREATE TABLE IF NOT EXISTS agent_learning_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_key TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'job',
    client TEXT,
    job TEXT,
    rule_type TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    source_assessment_id INTEGER,
    support_count INTEGER NOT NULL DEFAULT 1,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    last_supported_at TEXT,
    last_used_at TEXT,
    approved_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(rule_key, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_learning_scope
ON agent_learning_rules(client, job, status, version DESC);

CREATE TABLE IF NOT EXISTS agent_evaluation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL UNIQUE,
    metrics_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    skill_version TEXT NOT NULL,
    context_type TEXT NOT NULL,
    context_id INTEGER,
    risk_level TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT,
    confidence REAL NOT NULL DEFAULT 1,
    content_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    hit_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_memories_scope
ON agent_memories(scope_type,scope_id,status,updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_sourcing_attributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL,
    candidate_id INTEGER,
    job_id INTEGER NOT NULL,
    workflow_id TEXT,
    strategy_hash TEXT,
    strategy_model TEXT,
    channel TEXT NOT NULL,
    source_query TEXT NOT NULL,
    source_round TEXT,
    source_purpose TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(job_candidate_id,channel,source_query)
);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_attribution_job
ON agent_sourcing_attributions(job_id,channel,source_query);

CREATE TABLE IF NOT EXISTS agent_sourcing_query_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    workflow_id TEXT,
    job_id INTEGER NOT NULL DEFAULT 0,
    plan_hash TEXT NOT NULL,
    cell_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    query TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    reported_total INTEGER,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    extracted_count INTEGER NOT NULL DEFAULT 0,
    unique_count INTEGER NOT NULL DEFAULT 0,
    cursor_json TEXT NOT NULL DEFAULT '{}',
    retry_count INTEGER NOT NULL DEFAULT 0,
    terminal_reason TEXT,
    last_error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id,cell_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_query_cells_workflow
ON agent_sourcing_query_cells(workflow_id,status,priority,cell_id);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_query_cells_plan
ON agent_sourcing_query_cells(plan_hash,status,channel,priority);

CREATE TABLE IF NOT EXISTS agent_candidate_recalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    workflow_id TEXT,
    job_id INTEGER NOT NULL DEFAULT 0,
    strategy_hash TEXT NOT NULL DEFAULT '',
    strategy_artifact_id TEXT,
    strategy_revision INTEGER,
    query_plan_hash TEXT NOT NULL DEFAULT '',
    query_cell_id TEXT NOT NULL DEFAULT '',
    query_family_ids_json TEXT NOT NULL DEFAULT '[]',
    query_provenance_json TEXT NOT NULL DEFAULT '[]',
    channel TEXT NOT NULL,
    source_candidate_id TEXT NOT NULL DEFAULT '',
    source_query TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    page_number INTEGER NOT NULL DEFAULT 1,
    position_index INTEGER NOT NULL DEFAULT 0,
    identity_key TEXT NOT NULL DEFAULT '',
    candidate_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    fit_score INTEGER,
    fit_level TEXT,
    duplicate_state TEXT NOT NULL DEFAULT 'not_intaked',
    exclusion_reason TEXT,
    detail_status TEXT NOT NULL DEFAULT 'not_requested',
    candidate_id INTEGER,
    job_candidate_id INTEGER,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_candidate_recalls_run
ON agent_candidate_recalls(run_id,channel,query_cell_id,page_number,position_index);

CREATE INDEX IF NOT EXISTS idx_agent_candidate_recalls_job
ON agent_candidate_recalls(job_id,channel,created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_candidate_recalls_identity
ON agent_candidate_recalls(channel,source_candidate_id,identity_key);

CREATE TABLE IF NOT EXISTS agent_sourcing_coverage_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT,
    job_id INTEGER NOT NULL DEFAULT 0,
    plan_hash TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    certificate_json TEXT NOT NULL,
    issued_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_coverage_workflow
ON agent_sourcing_coverage_certificates(workflow_id,issued_at DESC);

CREATE TABLE IF NOT EXISTS agent_sourcing_funnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    workflow_id TEXT,
    job_id INTEGER NOT NULL DEFAULT 0,
    client TEXT NOT NULL DEFAULT '',
    job TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    query_count INTEGER NOT NULL DEFAULT 0,
    queries_json TEXT NOT NULL DEFAULT '[]',
    recall_count INTEGER NOT NULL DEFAULT 0,
    extracted_count INTEGER NOT NULL DEFAULT 0,
    dedupe_count INTEGER NOT NULL DEFAULT 0,
    unique_count INTEGER NOT NULL DEFAULT 0,
    detail_complete INTEGER NOT NULL DEFAULT 0,
    detail_partial INTEGER NOT NULL DEFAULT 0,
    detail_failed INTEGER NOT NULL DEFAULT 0,
    intake_duplicate_count INTEGER NOT NULL DEFAULT 0,
    intake_new_count INTEGER NOT NULL DEFAULT 0,
    assessed_count INTEGER NOT NULL DEFAULT 0,
    high_score_count INTEGER NOT NULL DEFAULT 0,
    zero_attribution TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id,channel)
);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_funnel_workflow
ON agent_sourcing_funnel(workflow_id,channel,created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_funnel_job
ON agent_sourcing_funnel(job_id,channel,created_at DESC);

CREATE TABLE IF NOT EXISTS agent_channel_effectiveness (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    archetype_id TEXT NOT NULL DEFAULT 'unknown',
    rounds INTEGER NOT NULL DEFAULT 0,
    recall_total INTEGER NOT NULL DEFAULT 0,
    intake_total INTEGER NOT NULL DEFAULT 0,
    high_score_total INTEGER NOT NULL DEFAULT 0,
    zero_streak INTEGER NOT NULL DEFAULT 0,
    conversion REAL,
    last_verdict TEXT,
    last_workflow_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(channel, archetype_id)
);

CREATE TABLE IF NOT EXISTS agent_sourcing_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    attribution_id INTEGER NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    weight REAL NOT NULL,
    note TEXT,
    source_type TEXT,
    source_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(attribution_id) REFERENCES agent_sourcing_attributions(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_sourcing_feedback_query
ON agent_sourcing_feedback(job_id,attribution_id,created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory_recalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_hash TEXT NOT NULL,
    context_type TEXT NOT NULL,
    context_id TEXT,
    memory_ids_json TEXT NOT NULL DEFAULT '[]',
    mode TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0,
    conflict INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS agent_context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    context_type TEXT NOT NULL DEFAULT 'global',
    context_id TEXT,
    snapshot_hash TEXT NOT NULL UNIQUE,
    title TEXT,
    summary TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_context_snapshots_source
ON agent_context_snapshots(source, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT,
    tool_name TEXT NOT NULL,
    permission_level TEXT NOT NULL DEFAULT 'read',
    request_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_status
ON agent_tool_calls(status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    permission_id TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    permission_level TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    preview_json TEXT NOT NULL DEFAULT '{}',
    scope TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_permissions_queue
ON agent_permissions(status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_learning_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    assessment_id INTEGER NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(rule_id,assessment_id,signal_type),
    FOREIGN KEY(rule_id) REFERENCES agent_learning_rules(id)
);

CREATE TABLE IF NOT EXISTS agent_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    title TEXT NOT NULL,
    context_type TEXT NOT NULL DEFAULT 'global',
    context_id INTEGER,
    context_json TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'draft',
    progress REAL NOT NULL DEFAULT 0,
    result_summary TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT,
    business_outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_goals_status
ON agent_goals(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    current_stage TEXT,
    status TEXT NOT NULL DEFAULT 'planned',
    plan_json TEXT NOT NULL DEFAULT '{}',
    active_step_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    started_at TEXT,
    finished_at TEXT,
    business_outcome TEXT,
    FOREIGN KEY(goal_id) REFERENCES agent_goals(goal_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_workflows_goal
ON agent_workflows(goal_id, version DESC);

CREATE TABLE IF NOT EXISTS agent_workflow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    capability_id TEXT NOT NULL,
    business_label TEXT NOT NULL,
    business_stage TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reason TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    references_json TEXT NOT NULL DEFAULT '[]',
    verification_json TEXT NOT NULL DEFAULT '{}',
    recovery_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(workflow_id, step_key),
    FOREIGN KEY(workflow_id) REFERENCES agent_workflows(workflow_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_workflow_steps_run
ON agent_workflow_steps(workflow_id, sequence, status);

CREATE TABLE IF NOT EXISTS agent_step_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    step_id INTEGER,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_step_events_workflow
ON agent_step_events(workflow_id, id);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    step_id INTEGER,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'text/markdown',
    file_path TEXT,
    content TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    validation_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_goal
ON agent_artifacts(goal_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL UNIQUE,
    goal_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    step_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    title TEXT NOT NULL,
    preflight_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    decision_note TEXT,
    token_hash TEXT,
    expires_at TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(step_id, status)
);

CREATE INDEX IF NOT EXISTS idx_agent_approvals_queue
ON agent_approvals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_workflow_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(workflow_id, snapshot_hash)
);

CREATE TABLE IF NOT EXISTS agent_workflow_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    note TEXT,
    correction_json TEXT NOT NULL DEFAULT '{}',
    context_type TEXT NOT NULL,
    context_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(workflow_id, feedback_type)
);

CREATE INDEX IF NOT EXISTS idx_agent_workflow_feedback_type
ON agent_workflow_feedback(feedback_type, created_at DESC);

-- S8 岗位画像学习：单人职责事实（抽取器产出，证据逐字校验后才入库）。
CREATE TABLE IF NOT EXISTS job_profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    job_candidate_id INTEGER NOT NULL,
    person_id INTEGER,
    facts_json TEXT NOT NULL DEFAULT '{}',
    fact_count INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}',
    as_of TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(job_id, job_candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_job_profile_facts_job
ON job_profile_facts(job_id);

-- S8 岗位画像学习：按客户+岗位聚合的岗位真实画像（先给人看，不接策略/评估消费）。
CREATE TABLE IF NOT EXISTS job_profile_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    client TEXT NOT NULL DEFAULT '',
    job_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'insufficient',
    source_count INTEGER NOT NULL DEFAULT 0,
    insight_json TEXT NOT NULL DEFAULT '{}',
    model TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    as_of TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- S8 岗位画像顾问纠正通道：标记 disputed 不删除，聚合时排除并留痕（质量闭环）。
CREATE TABLE IF NOT EXISTS job_profile_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    item_label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'disputed',
    note TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'consultant',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(job_id, item_type, item_key)
);

CREATE INDEX IF NOT EXISTS idx_job_profile_feedback_job
ON job_profile_feedback(job_id);

-- 顾问确认推荐事实链：顾问确认某候选人已向客户推荐（必须附原因）。
-- 同一人岗关系仅确认一次（UNIQUE job_candidate_id），重复提交幂等返回已确认事实，
-- 不重复写事件、不重复计入岗位指标。
CREATE TABLE IF NOT EXISTS consultant_confirmed_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    confirmation_token TEXT NOT NULL UNIQUE,
    confirmed_by TEXT NOT NULL DEFAULT 'consultant',
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    event_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(job_candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_consultant_confirmed_recommendations_job
ON consultant_confirmed_recommendations(job_id, confirmed_at);

-- S6-4 评估校准：改判样例库（advisor_action ∈ modified/rejected 回流；
-- 只存口径与维度标签，不存简历原文；敏感因子拒入由写入侧拦截）。
CREATE TABLE IF NOT EXISTS assessment_calibration_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    candidate_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    advisor_action TEXT NOT NULL,
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    machine_verdicts_json TEXT NOT NULL DEFAULT '{}',
    advisor_note TEXT NOT NULL DEFAULT '',
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_assessment_calibration_match
ON assessment_calibration_samples(client, job_type, id DESC);

CREATE TABLE IF NOT EXISTS agent_analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    catalog_id TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    supersedes_run_id TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    export_path TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(supersedes_run_id) REFERENCES agent_analysis_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_analysis_runs_catalog
ON agent_analysis_runs(catalog_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_analysis_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_kind TEXT NOT NULL DEFAULT 'manual',
    schedule_enabled INTEGER NOT NULL DEFAULT 0,
    schedule_time TEXT NOT NULL DEFAULT '09:00',
    schedule_weekday INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT,
    last_run_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY(last_run_id) REFERENCES agent_analysis_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_analysis_templates_enabled
ON agent_analysis_templates(enabled, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_analysis_template_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_run_id TEXT NOT NULL UNIQUE,
    template_id TEXT NOT NULL,
    analysis_run_id TEXT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT,
    FOREIGN KEY(template_id) REFERENCES agent_analysis_templates(template_id),
    FOREIGN KEY(analysis_run_id) REFERENCES agent_analysis_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_analysis_template_runs_template
ON agent_analysis_template_runs(template_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_analysis_template_runs_status
ON agent_analysis_template_runs(status, started_at);

CREATE TABLE IF NOT EXISTS agent_inbox_state (
    item_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'unread',
    source_revision TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 孤儿会话元数据清理：metadata 只在消息存在时才会写入（见 update_copilot_session），
    # 因此没有任何消息的 metadata 行必是消息被删后的残留，可安全删除；语句幂等可重复执行。
    conn.execute(
        """DELETE FROM agent_copilot_sessions
           WHERE NOT EXISTS (
               SELECT 1 FROM agent_copilot_messages c
               WHERE c.session_id = agent_copilot_sessions.session_id
           )"""
    )
    _ensure_column(conn, "agent_workflows", "archived_at", "TEXT")
    _ensure_column(conn, "agent_workflows", "business_outcome", "TEXT")
    _ensure_column(conn, "agent_goals", "business_outcome", "TEXT")
    _ensure_column(conn, "agent_workflow_steps", "verification_json", "TEXT NOT NULL DEFAULT '{}'" )
    _ensure_column(conn, "agent_workflow_steps", "recovery_json", "TEXT NOT NULL DEFAULT '{}'" )
    _ensure_column(conn, "agent_learning_rules", "support_count", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "agent_learning_rules", "contradiction_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "agent_learning_rules", "last_supported_at", "TEXT")
    _ensure_column(conn, "agent_learning_rules", "last_used_at", "TEXT")
    _ensure_column(conn, "agent_learning_rules", "candidate_count", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "agent_analysis_templates", "schedule_kind", "TEXT NOT NULL DEFAULT 'manual'")
    _ensure_column(conn, "agent_analysis_templates", "schedule_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "agent_analysis_templates", "schedule_time", "TEXT NOT NULL DEFAULT '09:00'")
    _ensure_column(conn, "agent_analysis_templates", "schedule_weekday", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "agent_analysis_templates", "timezone", "TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
    _ensure_column(conn, "agent_analysis_templates", "next_run_at", "TEXT")
    _ensure_column(conn, "agent_analysis_templates", "last_run_at", "TEXT")
    _ensure_column(conn, "agent_analysis_templates", "last_status", "TEXT")
    _ensure_column(conn, "agent_candidate_recalls", "strategy_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "agent_candidate_recalls", "strategy_artifact_id", "TEXT")
    _ensure_column(conn, "agent_candidate_recalls", "strategy_revision", "INTEGER")
    _ensure_column(conn, "agent_candidate_recalls", "query_plan_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "agent_candidate_recalls", "query_family_ids_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "agent_candidate_recalls", "query_provenance_json", "TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_agent_candidate_recalls_strategy
           ON agent_candidate_recalls(strategy_hash,query_plan_hash,query_cell_id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_agent_analysis_templates_due
           ON agent_analysis_templates(enabled, schedule_enabled, next_run_at)"""
    )
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS agent_memories_fts USING fts5(content, content='agent_memories', content_rowid='id')"
        )
        conn.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS agent_memories_ai AFTER INSERT ON agent_memories BEGIN
              INSERT INTO agent_memories_fts(rowid,content) VALUES (new.id,new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS agent_memories_ad AFTER DELETE ON agent_memories BEGIN
              INSERT INTO agent_memories_fts(agent_memories_fts,rowid,content) VALUES('delete',old.id,old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS agent_memories_au AFTER UPDATE OF content ON agent_memories BEGIN
              INSERT INTO agent_memories_fts(agent_memories_fts,rowid,content) VALUES('delete',old.id,old.content);
              INSERT INTO agent_memories_fts(rowid,content) VALUES (new.id,new.content);
            END;
            """
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()
