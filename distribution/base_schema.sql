-- ASA v3 基座 schema（空库初始化用，仅 DDL 无数据）
-- 由主库 sqlite_master 差集生成：distribution/generate_base_schema.py

CREATE TABLE IF NOT EXISTS agent_copilot_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    source_message TEXT NOT NULL,
    command_type TEXT NOT NULL,
    target_json TEXT NOT NULL DEFAULT '{}',
    command_json TEXT NOT NULL DEFAULT '{}',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    impact_json TEXT NOT NULL DEFAULT '{}',
    condition_version INTEGER NOT NULL DEFAULT 0,
    command_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    workflow_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    executed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS 'agent_memories_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS 'agent_memories_fts_data'(id INTEGER PRIMARY KEY, block BLOB);

CREATE TABLE IF NOT EXISTS 'agent_memories_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);

CREATE TABLE IF NOT EXISTS 'agent_memories_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS candidate_clients(
  id INT,
  candidate_name TEXT,
  candidate_company TEXT,
  client TEXT,
  source TEXT,
  position_tag TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS candidate_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_candidate_id INTEGER,
    person_id INTEGER,
    job_id INTEGER,
    event_type TEXT NOT NULL,
    event_status TEXT,
    event_time TEXT,
    summary TEXT,
    raw_json TEXT DEFAULT '{}',
    source_table TEXT,
    source_id TEXT,
    FOREIGN KEY(job_candidate_id) REFERENCES job_candidates(id),
    FOREIGN KEY(person_id) REFERENCES people(id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS candidate_intelligence(
  id INT,
  candidate_id INT,
  candidate_name TEXT,
  candidate_company TEXT,
  client TEXT,
  position TEXT,
  fit_score INT,
  fit_level TEXT,
  evidence_json TEXT,
  risk_json TEXT,
  next_action TEXT,
  last_evaluated_at TEXT,
  model_version TEXT,
  created_at TEXT,
  updated_at TEXT,
  strong_matches_json TEXT,
  weak_matches_json TEXT,
  verification_questions_json TEXT,
  recommendation_decision TEXT
);

CREATE TABLE IF NOT EXISTS candidate_profiles(
  id INT,
  candidate_id INT,
  candidate_name TEXT,
  candidate_company TEXT,
  client TEXT,
  position TEXT,
  education_level TEXT,
  seniority TEXT,
  industry_tags_json TEXT,
  function_tags_json TEXT,
  risk_tags_json TEXT,
  profile_summary TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS candidate_project_correction_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INTEGER NOT NULL, kept_job_candidate_id INTEGER NOT NULL, removed_job_candidate_id INTEGER NOT NULL, reason TEXT NOT NULL, snapshot_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now','localtime')));

CREATE TABLE IF NOT EXISTS candidates(
  id INT,
  name TEXT,
  company TEXT,
  title TEXT,
  education TEXT,
  experience TEXT,
  skills TEXT,
  level TEXT,
  city TEXT,
  client TEXT,
  position TEXT,
  search_date TEXT,
  status TEXT,
  notes TEXT,
  iteration INT,
  recommended_to_client TEXT,
  client_feedback TEXT,
  elimination_reason TEXT,
  anchor_candidate INT,
  created_at TEXT,
  updated_at TEXT,
  source TEXT,
  xsaas_id TEXT,
  talent_pool TEXT
);

CREATE TABLE IF NOT EXISTS chat_history_reply_rules(
  rule_key TEXT,
  category TEXT,
  trigger_text TEXT,
  recommended_pattern TEXT,
  avoid_pattern TEXT,
  evidence_count INT,
  confidence INT,
  examples_json TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS company_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    fact_value TEXT DEFAULT '',
    quote TEXT DEFAULT '',
    source_ref TEXT DEFAULT '',
    confidence REAL DEFAULT 0.8,
    model_version TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS company_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    aliases_json TEXT DEFAULT '[]',
    industry TEXT DEFAULT '',
    business_desc TEXT DEFAULT '',
    product_lines_json TEXT DEFAULT '[]',
    tech_stack_json TEXT DEFAULT '[]',
    org_clues_json TEXT DEFAULT '[]',
    scale TEXT DEFAULT '',
    salary_clues_json TEXT DEFAULT '[]',
    risk_signals_json TEXT DEFAULT '[]',
    headhunt_clues_json TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'auto',
    error_message TEXT,
    last_extracted_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS conversation_context_reply_rules(
  rule_key TEXT,
  category TEXT,
  trigger_text TEXT,
  recommended_pattern TEXT,
  avoid_pattern TEXT,
  evidence_count INT,
  confidence INT,
  examples_json TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS desktop_candidates(
  id INT,
  source TEXT,
  client TEXT,
  candidate_name TEXT,
  candidate_id TEXT,
  current_company TEXT,
  current_position TEXT,
  previous_company TEXT,
  previous_position TEXT,
  education TEXT,
  location TEXT,
  tags TEXT,
  position_category TEXT,
  search_keyword TEXT,
  raw_data TEXT,
  created_at NUM
);

CREATE TABLE IF NOT EXISTS desktop_positions(
  id INT,
  client TEXT,
  title TEXT,
  salary TEXT,
  location TEXT,
  headcount INT,
  experience_years TEXT,
  age_limit TEXT,
  work_intensity TEXT,
  responsibilities TEXT,
  requirements TEXT,
  hard_requirements TEXT,
  ability_keywords TEXT,
  target_companies TEXT,
  exclusions TEXT,
  search_words TEXT,
  summary TEXT,
  status TEXT,
  source_file TEXT,
  strategy_file TEXT,
  created_at TEXT,
  updated_at TEXT,
  liepin_status TEXT,
  liepin_published_at TEXT,
  liepin_verify_log TEXT
);

CREATE TABLE IF NOT EXISTS desktop_search_log(
  id INT,
  channel TEXT,
  keyword TEXT,
  candidates_found INT,
  candidates_added INT,
  searched_at NUM
);

CREATE TABLE IF NOT EXISTS desktop_wechat_evidence_links(
  id INT,
  task_id INT,
  binding_id INT,
  object_type TEXT,
  object_id INT,
  object_name TEXT,
  object_path TEXT,
  workbench_type TEXT,
  workbench_id TEXT,
  workbench_name TEXT,
  client TEXT,
  position TEXT,
  candidate TEXT,
  source_candidate_ids TEXT,
  evidence_json TEXT,
  status TEXT,
  applied_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS flow_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_code TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    definition TEXT,
    check_point TEXT,
    next_flow TEXT,
    source TEXT,
    UNIQUE(stage_code, stage_name)
);

CREATE TABLE IF NOT EXISTS job_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    job_id INTEGER,
    alias_title TEXT NOT NULL,
    standard_title TEXT,
    confidence TEXT,
    score REAL,
    reason TEXT,
    candidate_count INTEGER DEFAULT 0,
    status_counts_json TEXT DEFAULT '{}',
    needs_review INTEGER DEFAULT 0,
    source TEXT DEFAULT 'workbench_mapping',
    UNIQUE(client_id, alias_title, standard_title),
    FOREIGN KEY(client_id) REFERENCES clients(id),
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    person_id INTEGER NOT NULL,
    raw_client TEXT,
    raw_position TEXT,
    raw_status TEXT,
    raw_stage TEXT,
    clean_stage TEXT,
    flow_bucket TEXT,
    clean_reason TEXT,
    recent_hunting INTEGER DEFAULT 0,
    search_date TEXT,
    updated_at TEXT,
    source_candidate_id TEXT, stop_reason TEXT,
    UNIQUE(job_id, person_id, raw_position, source_candidate_id),
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(person_id) REFERENCES people(id)
);

CREATE TABLE IF NOT EXISTS job_list_filters (
            job_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

CREATE TABLE IF NOT EXISTS job_pipeline_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    snapshot_id INTEGER,
    metric_date TEXT,
    a_count INTEGER,
    b_count INTEGER,
    p0_count INTEGER,
    p1_count INTEGER,
    published_count INTEGER,
    under_review_count INTEGER,
    contacted_count INTEGER,
    pending_followup_count INTEGER,
    thin_role TEXT,
    risk TEXT,
    priority TEXT,
    stop_condition TEXT,
    next_keywords_json TEXT DEFAULT '[]',
    target_companies_json TEXT DEFAULT '[]',
    exclude_terms_json TEXT DEFAULT '[]',
    data_gap INTEGER DEFAULT 0,
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(snapshot_id) REFERENCES job_snapshots(id)
);

CREATE TABLE IF NOT EXISTS job_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    source_id INTEGER,
    snapshot_type TEXT NOT NULL,
    captured_at TEXT,
    role_line TEXT,
    raw_json TEXT NOT NULL,
    source_path TEXT,
    FOREIGN KEY(job_id) REFERENCES jobs(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    job_code TEXT,
    location TEXT,
    status TEXT,
    source_layer TEXT NOT NULL DEFAULT 'workbench',
    hard_requirements TEXT,
    ability_keywords TEXT,
    target_companies TEXT,
    exclusions TEXT,
    search_words TEXT,
    summary TEXT,
    evidence_file TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')), lifecycle_stage TEXT, closed_reason TEXT, closed_at TEXT, gender_requirement TEXT NOT NULL DEFAULT '',
    UNIQUE(client_id, title),
    FOREIGN KEY(client_id) REFERENCES clients(id)
);

CREATE TABLE IF NOT EXISTS learning_notes(
  id INT,
  client TEXT,
  position TEXT,
  topic TEXT,
  note TEXT,
  source TEXT,
  confidence INT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    current_company TEXT,
    current_title TEXT,
    city TEXT,
    education TEXT,
    experience TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS position_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    position TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    asset_title TEXT,
    asset_summary TEXT,
    file_path TEXT NOT NULL,
    source_snapshot_id INTEGER,
    asset_json TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(client, position, asset_type, file_path)
);

CREATE TABLE IF NOT EXISTS position_profiles(
  id INT,
  client TEXT,
  position TEXT,
  education_requirement TEXT,
  experience_requirement TEXT,
  hard_requirements_json TEXT,
  ability_keywords_json TEXT,
  target_companies_json TEXT,
  exclusion_tags_json TEXT,
  search_keywords_json TEXT,
  source_position_ids_json TEXT,
  updated_at TEXT,
  soft_preferences_json TEXT,
  pitch_points_json TEXT,
  risk_points_json TEXT,
  jd_analysis_summary TEXT
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    position TEXT NOT NULL,
    position_id INTEGER,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT,
    source_url TEXT,
    source_title TEXT,
    raw_text TEXT,
    raw_json TEXT DEFAULT '{}',
    content_hash TEXT NOT NULL,
    captured_at TEXT DEFAULT (datetime('now','localtime')),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(content_hash)
);

CREATE TABLE IF NOT EXISTS positions(
  id INT,
  client TEXT,
  department TEXT,
  team TEXT,
  title TEXT,
  responsibilities TEXT,
  requirements TEXT,
  headcount INT,
  gap INT,
  deadline TEXT,
  level TEXT,
  education TEXT,
  experience TEXT,
  status TEXT,
  created_at TEXT,
  updated_at TEXT
, salary TEXT, location TEXT, experience_years TEXT, age_limit TEXT, work_intensity TEXT, hard_requirements TEXT, ability_keywords TEXT, target_companies TEXT, exclusions TEXT, search_words TEXT, summary TEXT, source_file TEXT, strategy_file TEXT, liepin_status TEXT, liepin_published_at TEXT, liepin_verify_log TEXT);

CREATE TABLE IF NOT EXISTS reply_learning_rules(
  id INT,
  rule_key TEXT,
  category TEXT,
  rule_text TEXT,
  evidence_count INT,
  confidence INT,
  examples_json TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS roster_audit_exemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    job TEXT NOT NULL,
    reason TEXT NOT NULL,
    approved_by TEXT DEFAULT '用户',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(client, job)
);

CREATE TABLE IF NOT EXISTS source_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_candidate_id TEXT,
    source_date TEXT,
    raw_status TEXT,
    raw_client TEXT,
    raw_position TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(person_id) REFERENCES people(id)
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    path TEXT,
    description TEXT,
    captured_at TEXT,
    metadata_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS talk_algorithm_rules(
  id INT,
  strategy_key TEXT,
  stage TEXT,
  goal TEXT,
  pattern TEXT,
  risk TEXT,
  upgrade TEXT,
  sample_count INT,
  positive_count INT,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS talk_samples(
  id INT,
  channel TEXT,
  candidate_name TEXT,
  candidate_title TEXT,
  time_text TEXT,
  direction_guess TEXT,
  strategy_guess TEXT,
  message TEXT,
  raw_text TEXT,
  source TEXT,
  collected_at TEXT
);

CREATE TABLE IF NOT EXISTS wechat_evidence_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER UNIQUE,
  binding_id INTEGER,
  object_type TEXT,
  object_id INTEGER,
  object_name TEXT,
  object_path TEXT,
  workbench_type TEXT,
  workbench_id TEXT,
  workbench_name TEXT,
  client TEXT,
  position TEXT,
  candidate TEXT,
  source_candidate_ids TEXT,
  evidence_json TEXT,
  status TEXT DEFAULT 'applied',
  applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_copilot_commands_session
ON agent_copilot_commands(session_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_events_api_v1_request
            ON candidate_events(job_candidate_id, event_type, source_id)
            WHERE source_table='api_v1' AND source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ci_client_position ON candidate_intelligence(client, position);

CREATE INDEX IF NOT EXISTS idx_company_evidence_key ON company_evidence(company_key);

CREATE INDEX IF NOT EXISTS idx_company_knowledge_industry ON company_knowledge(industry);

CREATE INDEX IF NOT EXISTS idx_company_knowledge_status ON company_knowledge(status);

CREATE INDEX IF NOT EXISTS idx_jobs_lifecycle ON jobs(lifecycle_stage, status);

CREATE INDEX IF NOT EXISTS idx_position_assets_project_type ON position_assets(client, position, asset_type);

CREATE INDEX IF NOT EXISTS idx_position_profiles_project ON position_profiles(client, position);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_project_time ON position_snapshots(client, position, captured_at);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_source ON position_snapshots(source_type, source_ref);

CREATE INDEX IF NOT EXISTS idx_positions_client_title ON positions(client, title);

CREATE INDEX IF NOT EXISTS idx_wechat_evidence_links_candidate ON wechat_evidence_links(candidate, client, position);

CREATE INDEX IF NOT EXISTS idx_wechat_evidence_links_target ON wechat_evidence_links(workbench_type, workbench_id, workbench_name);

CREATE VIEW IF NOT EXISTS v_candidate_flow AS
SELECT
    p.display_name,
    p.current_company,
    p.current_title,
    c.name AS client,
    j.title AS job,
    jc.raw_status,
    jc.clean_stage,
    jc.flow_bucket,
    jc.search_date,
    jc.updated_at
FROM job_candidates jc
JOIN people p ON p.id = jc.person_id
LEFT JOIN jobs j ON j.id = jc.job_id
LEFT JOIN clients c ON c.id = j.client_id;

CREATE VIEW IF NOT EXISTS v_job_dashboard AS
SELECT
    c.name AS client,
    j.title AS job,
    j.job_code,
    j.status,
    COALESCE(m.a_count, 0) AS A,
    COALESCE(m.b_count, 0) AS B,
    COALESCE(m.p0_count, 0) AS P0,
    COALESCE(m.p1_count, 0) AS P1,
    COALESCE(m.published_count, 0) AS published,
    COALESCE(m.under_review_count, 0) AS under_review,
    COALESCE(m.contacted_count, 0) AS contacted,
    COALESCE(m.pending_followup_count, 0) AS pending_followup,
    m.thin_role,
    m.priority,
    m.stop_condition,
    m.risk,
    COUNT(jc.id) AS linked_candidates
FROM jobs j
JOIN clients c ON c.id = j.client_id
LEFT JOIN job_pipeline_metrics m ON m.job_id = j.id
LEFT JOIN job_candidates jc ON jc.job_id = j.id
GROUP BY j.id;

CREATE VIEW IF NOT EXISTS v_job_lifecycle AS
        SELECT
            j.id AS job_id,
            c.name AS client,
            j.title AS job,
            j.status AS raw_status,
            COALESCE(NULLIF(j.lifecycle_stage, ''), 'intake') AS lifecycle_stage,
            COALESCE(m.priority, '') AS priority,
            COALESCE(m.linked_candidates, 0) AS linked_candidates,
            COALESCE(m.contacted_count, 0) AS contacted_count,
            COALESCE(m.pending_followup_count, 0) AS pending_followup_count,
            j.updated_at,
            j.closed_reason,
            j.closed_at
        FROM jobs j
        JOIN clients c ON c.id = j.client_id
        LEFT JOIN (
            SELECT pm.*,
                   (SELECT COUNT(*) FROM job_candidates jc WHERE jc.job_id = pm.job_id) AS linked_candidates
            FROM job_pipeline_metrics pm
            WHERE pm.id = (
                SELECT pm2.id FROM job_pipeline_metrics pm2
                WHERE pm2.job_id = pm.job_id
                ORDER BY COALESCE(pm2.metric_date, '') DESC, pm2.id DESC LIMIT 1
            )
        ) m ON m.job_id = j.id;
