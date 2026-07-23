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
