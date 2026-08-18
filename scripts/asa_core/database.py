from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DB = Path(
    os.environ.get(
        "A_SYSTEM_DB",
        "/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db",
    )
).expanduser()


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(db_path: Path = DEFAULT_DB) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "asa_core_governance",
        """
        CREATE TABLE IF NOT EXISTS entity_source_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_type TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            source_system TEXT NOT NULL,
            source_entity_type TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            source_url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(source_system, source_entity_type, source_entity_id, canonical_type, canonical_id)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_source_links_canonical
            ON entity_source_links(canonical_type, canonical_id);

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            actor TEXT NOT NULL,
            surface TEXT NOT NULL,
            request_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL,
            business_event_type TEXT,
            business_event_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_audit_events_target
            ON audit_events(target_type, target_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_request
            ON audit_events(request_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS api_idempotency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            request_id TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            response_status INTEGER,
            response_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            expires_at TEXT,
            UNIQUE(idempotency_key, operation)
        );
        CREATE INDEX IF NOT EXISTS idx_api_idempotency_request
            ON api_idempotency(request_id);
        """,
    ),
    (
        2,
        "asa_core_compatibility_views",
        """
        CREATE VIEW IF NOT EXISTS v_asa_audit_events AS
        SELECT event_id AS event_key, created_at AS event_time, actor, surface,
               operation, target_type, target_id, result, metadata_json
          FROM audit_events
        UNION ALL
        SELECT 'candidate_event:' || id, event_time, 'system', COALESCE(source_table,'core'),
               event_type, 'job_candidate', CAST(job_candidate_id AS TEXT),
               COALESCE(event_status,''), raw_json
          FROM candidate_events
        UNION ALL
        SELECT 'agent_action:' || id, created_at, 'agent', 'agent_service',
               action_type, 'job_candidate', CAST(job_candidate_id AS TEXT), status, result_json
          FROM agent_actions;
        """,
    ),
    (
        3,
        "canonical_external_profile_links",
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(person_id AS TEXT),
               CASE WHEN lower(source_url) LIKE '%x-saas%' THEN 'xsaas' ELSE 'liepin' END,
               'external_profile', source_url, source_url,
               json_object('backfilled_from','candidate_events')
          FROM (
            SELECT DISTINCT jc.person_id,
                   COALESCE(json_extract(ce.raw_json,'$.source_url'),json_extract(ce.raw_json,'$.resume_url')) source_url
              FROM candidate_events ce
              JOIN job_candidates jc ON jc.id=ce.job_candidate_id
             WHERE json_valid(ce.raw_json)
               AND COALESCE(json_extract(ce.raw_json,'$.source_url'),json_extract(ce.raw_json,'$.resume_url')) LIKE 'http%'
          );

        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(person_id AS TEXT),
               CASE WHEN lower(source_url) LIKE '%x-saas%' THEN 'xsaas' ELSE 'liepin' END,
               'external_profile', source_url, source_url,
               json_object('backfilled_from','source_profiles')
          FROM (
            SELECT DISTINCT person_id,json_extract(raw_json,'$.source_url') source_url
              FROM source_profiles
             WHERE json_valid(raw_json) AND json_extract(raw_json,'$.source_url') LIKE 'http%'
          );
        """,
    ),
    (
        4,
        "sourcing_card_source_profiles",
        """
        WITH card_profiles AS (
            SELECT
                jc.person_id,
                CASE
                    WHEN lower(COALESCE(c.source,'')) LIKE '%xsaas%'
                      OR lower(COALESCE(c.source,'')) LIKE '%x-saas%' THEN 'xsaas'
                    ELSE 'liepin'
                END AS source_type,
                CASE
                    WHEN lower(COALESCE(c.source,'')) LIKE '%xsaas%'
                      OR lower(COALESCE(c.source,'')) LIKE '%x-saas%'
                      THEN NULLIF(trim(COALESCE(c.xsaas_id,'')),'')
                    ELSE NULLIF(trim(COALESCE(
                        (
                            SELECT json_extract(ce.raw_json,'$.res_id_encode')
                            FROM candidate_events ce
                            WHERE (ce.job_candidate_id=jc.id OR ce.person_id=jc.person_id)
                              AND json_valid(ce.raw_json)
                              AND COALESCE(json_extract(ce.raw_json,'$.res_id_encode'),'')<>''
                            ORDER BY ce.id DESC LIMIT 1
                        ),
                        ''
                    )),'')
                END AS source_candidate_id,
                COALESCE(c.search_date,substr(jc.updated_at,1,10),date('now','localtime')) AS source_date,
                COALESCE(jc.raw_status,'search_shortlisted') AS raw_status,
                COALESCE(jc.raw_client,c.client,'') AS raw_client,
                COALESCE(jc.raw_position,c.position,'') AS raw_position,
                c.name,c.company,c.title,c.education,c.experience,c.city,
                COALESCE(
                    (
                        SELECT json_extract(ce.raw_json,'$.profile_text')
                        FROM candidate_events ce
                        WHERE (ce.job_candidate_id=jc.id OR ce.person_id=jc.person_id)
                          AND json_valid(ce.raw_json)
                          AND COALESCE(json_extract(ce.raw_json,'$.profile_text'),'')<>''
                        ORDER BY ce.id DESC LIMIT 1
                    ),
                    c.skills,
                    ''
                ) AS profile_text,
                COALESCE(
                    (
                        SELECT COALESCE(
                            json_extract(ce.raw_json,'$.source_url'),
                            json_extract(ce.raw_json,'$.resume_url')
                        )
                        FROM candidate_events ce
                        WHERE (ce.job_candidate_id=jc.id OR ce.person_id=jc.person_id)
                          AND json_valid(ce.raw_json)
                          AND COALESCE(
                            json_extract(ce.raw_json,'$.source_url'),
                            json_extract(ce.raw_json,'$.resume_url'),
                            ''
                          )<>''
                        ORDER BY ce.id DESC LIMIT 1
                    ),
                    ''
                ) AS source_url
            FROM job_candidates jc
            JOIN candidates c ON CAST(c.id AS TEXT)=CAST(jc.source_candidate_id AS TEXT)
            WHERE jc.person_id IS NOT NULL
        )
        INSERT INTO source_profiles
            (person_id,source_type,source_candidate_id,source_date,raw_status,
             raw_client,raw_position,raw_json)
        SELECT
            cp.person_id,cp.source_type,cp.source_candidate_id,cp.source_date,
            cp.raw_status,cp.raw_client,cp.raw_position,
            json_object(
                'name',cp.name,'company',cp.company,'title',cp.title,
                'education',cp.education,'experience',cp.experience,'city',cp.city,
                'profile_text',cp.profile_text,'full_text',cp.profile_text,
                'source_url',cp.source_url,'backfilled_from','sourcing_card'
            )
        FROM card_profiles cp
        WHERE trim(COALESCE(cp.profile_text,''))<>''
          AND NOT EXISTS (
              SELECT 1 FROM source_profiles sp
              WHERE sp.person_id=cp.person_id
                AND lower(COALESCE(sp.source_type,''))=cp.source_type
          );
        """,
    ),
    (
        5,
        "recommendation_packages",
        # 版本化推荐包闭环：consultant_confirmed_recommendations 确认后聚合生成
        # 推荐包（候选摘要/人岗证据/风险/待核验问题），客户反馈按包版本关联留痕。
        """
        CREATE TABLE IF NOT EXISTS recommendation_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT NOT NULL UNIQUE,
            job_candidate_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            recommendation_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'generated',
            summary_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            risks_json TEXT NOT NULL DEFAULT '[]',
            verification_questions_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(job_candidate_id, version)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_packages_candidate
            ON recommendation_packages(job_candidate_id, version DESC);

        CREATE TABLE IF NOT EXISTS recommendation_package_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT NOT NULL,
            package_version INTEGER NOT NULL,
            job_candidate_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            feedback_type TEXT NOT NULL,
            content TEXT NOT NULL,
            feedback_time TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            recorded_by TEXT NOT NULL DEFAULT 'consultant',
            request_id TEXT NOT NULL,
            event_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(package_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_package_feedback_candidate
            ON recommendation_package_feedback(job_candidate_id, created_at DESC);
        """,
    ),
    (
        6,
        "knowledge_proposals",
        # 二期知识飞轮：knowledge_proposal 知识增补提案。Agent 从停止原因聚类/客户反馈/
        # 已确认推荐中确定性生成提案（证据不足只留候选），顾问 preflight/commit 两段确认后
        # 才写入知识文件；UNIQUE(proposal_type, content_key) 保证同一内容不重复提案。
        """
        CREATE TABLE IF NOT EXISTS knowledge_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL UNIQUE,
            proposal_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            content_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confirm_token TEXT,
            applied_to TEXT,
            decided_by TEXT,
            decision_note TEXT,
            decided_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(proposal_type, content_key)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_proposals_status
            ON knowledge_proposals(status, created_at DESC);
        """,
    ),
    (
        7,
        "company_calibrations",
        # 二期知识飞轮：company_calibration 核心公司校准覆盖层。图谱 JSON 保持原始名单不改；
        # 顾问逐公司确认/修正的（行业/产品线/技能标签/职级体系/禁挖竞业标记/备注）落本表，
        # 消费侧经 knowledge_base 校准覆盖层合并钩子按 company_key（规范化公司名）覆盖。
        # UNIQUE(company_key) 保证一家公司一条校准；version 随内容变更自增（同内容重提不 bump）。
        """
        CREATE TABLE IF NOT EXISTS company_calibrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            calibration_id TEXT NOT NULL UNIQUE,
            company_key TEXT NOT NULL UNIQUE,
            company_name TEXT NOT NULL,
            track TEXT NOT NULL DEFAULT '',
            product_lines_json TEXT NOT NULL DEFAULT '[]',
            skill_tags_json TEXT NOT NULL DEFAULT '[]',
            level_system TEXT NOT NULL DEFAULT '',
            no_poach INTEGER NOT NULL DEFAULT 0,
            non_compete INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'needs_review',
            calibrated_by TEXT NOT NULL DEFAULT 'consultant',
            calibrated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_company_calibrations_status
            ON company_calibrations(status, updated_at DESC);
        """,
    ),
    (
        8,
        "copilot_attachment_registry",
        """
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
        CREATE INDEX IF NOT EXISTS idx_copilot_attachments_expiry
            ON agent_copilot_attachments(expires_at);
        """,
    ),
    (
        9,
        "stop_note_sourcing_adjustments",
        # 停止备注 → 寻访调整指令闭环：LLM 分析停止备注生成结构化调整，
        # 下一轮寻访策略自动注入；dedupe_key 保证同岗位同类型同词条不重复。
        """
        CREATE TABLE IF NOT EXISTS agent_sourcing_adjustments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        INTEGER NOT NULL,
            candidate_id  INTEGER,
            adjust_type   TEXT NOT NULL,
            value         TEXT NOT NULL,
            rationale     TEXT,
            confidence    REAL DEFAULT 0.5,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            applied_at    TEXT,
            applied_round INTEGER,
            dedupe_key    TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_adjustments_job_status
            ON agent_sourcing_adjustments(job_id, status);
        """,
    ),
    (
        10,
        "adjustment_baseline_snapshot",
        # 调整效果追踪：应用调整时记录候选池基线快照（总池/待复核/已触达/已停止），
        # 列表查询时与当前值对比，展示"调整前后候选池质量变化"。
        """
        ALTER TABLE agent_sourcing_adjustments
            ADD COLUMN baseline_json TEXT;
        """,
    ),
    (
        11,
        "sourcing_adjustment_acceptance_lineage",
        # 顾问采纳与策略实际消费分离；applied 必须能追溯到成功落库的策略产物。
        """
        ALTER TABLE agent_sourcing_adjustments
            ADD COLUMN accepted_at TEXT;
        ALTER TABLE agent_sourcing_adjustments
            ADD COLUMN applied_workflow_id TEXT;
        ALTER TABLE agent_sourcing_adjustments
            ADD COLUMN applied_artifact_id TEXT;
        CREATE INDEX IF NOT EXISTS idx_adjustments_applied_workflow
            ON agent_sourcing_adjustments(applied_workflow_id, applied_artifact_id);
        """,
    ),
    (
        13,
        "job_list_filters",
        # 岗位级名单口径记忆：岗位做过严格筛选后，后续任意会话问"名单"默认按
        # grade_filter 口径重算；显式"全量名单"清除。会话级 list_filters 之外
        # 的跨会话兜底（2026-08-18 新会话问名单回落全量 275 人的问题）。
        # 注意：版本 12（copilot_persistent_commands）在 fix/kb-correctness-2026-08-16
        # 分支且已应用于生产库，本迁移只能用 13，否则生产库校验和冲突。
        """
        CREATE TABLE IF NOT EXISTS job_list_filters (
            job_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        """,
    ),
]


def ensure_stop_reason_schema(conn: sqlite3.Connection) -> None:
    """job_candidates.stop_reason（PRD 阶段 4 R10 停止原因标准化）。

    走 _ensure_column 幂等模式而非 MIGRATIONS：standalone legacy 与 ASA Core
    都可能先启动，Python 级 PRAGMA 检查保证任一方先加列后另一方安全跳过；
    历史数据不迁移，存量行保持 NULL（统计口径归入"未标注"）。
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(job_candidates)")}
    if "stop_reason" not in columns:
        conn.execute("ALTER TABLE job_candidates ADD COLUMN stop_reason TEXT")


def ensure_idempotency_recovery_schema(conn: sqlite3.Connection) -> None:
    """Add failure/recovery fields and close abandoned processing leases."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(api_idempotency)")}
    if "error_json" not in columns:
        conn.execute("ALTER TABLE api_idempotency ADD COLUMN error_json TEXT")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE api_idempotency ADD COLUMN updated_at TEXT")
    conn.execute(
        "UPDATE api_idempotency SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL"
    )
    stale = conn.execute(
        """
        SELECT id,idempotency_key,operation,request_id
          FROM api_idempotency
         WHERE status='processing'
           AND datetime(COALESCE(expires_at,datetime(created_at,'+5 minutes'))) <= datetime('now','localtime')
        """
    ).fetchall()
    if not stale:
        return
    error_json = json.dumps(
        {
            "type": "abandoned_processing",
            "message": "processing lease expired before a result was recorded",
            "outcome": "unknown",
        },
        ensure_ascii=False,
    )
    for row in stale:
        conn.execute(
            """
            UPDATE api_idempotency
               SET status='failed',response_status=500,error_json=?,updated_at=datetime('now','localtime'),expires_at=NULL
             WHERE id=? AND status='processing'
            """,
            (error_json, int(row["id"])),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO audit_events
                (event_id,actor,surface,request_id,operation,target_type,target_id,result,metadata_json)
            VALUES (?,?,?,?,?,'idempotency',?,'failed',?)
            """,
            (
                f"idempotency_recovery_{int(row['id'])}",
                "system",
                "asa_core_startup",
                str(row["request_id"]),
                str(row["operation"]),
                str(row["idempotency_key"]),
                error_json,
            ),
        )


def _backup(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{db_path.stem}.pre_asa_core_{stamp}.db"
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _integrity(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {result}")


def migrate(db_path: Path = DEFAULT_DB, *, backup: bool = True) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    probe = sqlite3.connect(str(db_path))
    try:
        _integrity(probe)
        has_migrations = bool(
            probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
        )
    finally:
        probe.close()
    backup_path = _backup(db_path) if backup and not has_migrations else None

    conn = connect(db_path)
    applied: list[int] = []
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        conn.commit()
        current = {
            int(row["version"]): str(row["checksum"])
            for row in conn.execute("SELECT version, checksum FROM schema_migrations")
        }
        for version, name, sql in MIGRATIONS:
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in current:
                if current[version] != checksum:
                    raise RuntimeError(f"migration {version} checksum mismatch")
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum) VALUES (?,?,?)",
                (version, name, checksum),
            )
            conn.commit()
            applied.append(version)
        ensure_stop_reason_schema(conn)
        ensure_idempotency_recovery_schema(conn)
        _backfill_source_links(conn)
        conn.commit()
        _integrity(conn)
        conn.execute("PRAGMA optimize")
        fk_issues = [dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
    finally:
        conn.close()
    return {
        "ok": True,
        "db": str(db_path),
        "backup": str(backup_path) if backup_path else None,
        "applied": applied,
        "foreign_key_issues": fk_issues,
    }


def _backfill_source_links(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,metadata_json)
        SELECT 'job', CAST(j.id AS TEXT), 'a_system_v3', 'position', CAST(p.id AS TEXT),
               json_object('client',p.client,'title',p.title)
          FROM positions p
          JOIN clients c ON trim(c.name)=trim(p.client)
          JOIN jobs j ON j.client_id=c.id AND trim(j.title)=trim(p.title)
         WHERE p.id IS NOT NULL
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(sp.person_id AS TEXT), lower(COALESCE(sp.source_type,'unknown')),
               'source_profile', CAST(sp.id AS TEXT),
               CASE
                 WHEN lower(COALESCE(sp.source_type,''))='liepin' AND sp.source_candidate_id IS NOT NULL
                   THEN 'https://www.liepin.com/resume/showresumedetail/?res_id_encode=' || sp.source_candidate_id
                 WHEN lower(COALESCE(sp.source_type,'')) IN ('xsaas','x-saas') AND sp.source_candidate_id IS NOT NULL
                   THEN 'https://headhunt.x-saas.com.cn/#/app/candidate/info/' || sp.source_candidate_id
               END,
               json_object('source_candidate_id',sp.source_candidate_id)
          FROM source_profiles sp
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(p.id AS TEXT), 'xsaas', 'candidate', CAST(c.id AS TEXT),
               CASE WHEN trim(COALESCE(c.xsaas_id,''))<>''
                    THEN 'https://headhunt.x-saas.com.cn/#/app/candidate/info/' || c.xsaas_id END,
               json_object('legacy_name',c.name,'xsaas_id',c.xsaas_id)
          FROM candidates c
          JOIN people p ON trim(p.display_name)=trim(c.name)
           AND (trim(COALESCE(p.current_company,''))=trim(COALESCE(c.company,'')) OR trim(COALESCE(c.company,''))='')
         WHERE c.id IS NOT NULL AND trim(COALESCE(c.xsaas_id,''))<>''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(person_id AS TEXT),
               CASE WHEN lower(source_url) LIKE '%x-saas%' THEN 'xsaas' ELSE 'liepin' END,
               'external_profile', source_url, source_url,
               json_object('backfilled_from','candidate_events')
          FROM (
            SELECT DISTINCT COALESCE(ce.person_id,jc.person_id) AS person_id,
                   COALESCE(
                       json_extract(ce.raw_json,'$.source_url'),
                       json_extract(ce.raw_json,'$.resume_url'),
                       CASE WHEN ce.source_id LIKE 'http%' THEN ce.source_id END
                   ) AS source_url
              FROM candidate_events ce
              LEFT JOIN job_candidates jc ON jc.id=ce.job_candidate_id
             WHERE json_valid(ce.raw_json)
          )
         WHERE person_id IS NOT NULL AND source_url LIKE 'http%'
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO entity_source_links
            (canonical_type,canonical_id,source_system,source_entity_type,source_entity_id,source_url,metadata_json)
        SELECT 'person', CAST(person_id AS TEXT),
               CASE WHEN lower(source_url) LIKE '%x-saas%' THEN 'xsaas' ELSE 'liepin' END,
               'external_profile', source_url, source_url,
               json_object('backfilled_from','source_profiles_raw')
          FROM (
            SELECT DISTINCT person_id,
                   COALESCE(
                       json_extract(raw_json,'$.source_url'),
                       json_extract(raw_json,'$.resume_url')
                   ) AS source_url
              FROM source_profiles
             WHERE json_valid(raw_json)
          )
         WHERE person_id IS NOT NULL AND source_url LIKE 'http%'
        """
    )
    conn.commit()


def json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
