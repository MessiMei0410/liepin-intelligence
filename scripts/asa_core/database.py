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
