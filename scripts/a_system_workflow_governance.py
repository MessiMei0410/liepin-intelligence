#!/usr/bin/env python3
"""Install the A System workflow governance schema on the unified v3 database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db")


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> bool:
    if name in table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    return True


def install(conn: sqlite3.Connection) -> dict[str, Any]:
    added: list[str] = []
    for table, columns in {
        "followup_tasks": {
            "job_candidate_id": "INTEGER",
            "completed_at": "TEXT",
        },
        "client_feedback_events": {
            "job_candidate_id": "INTEGER",
            "event_id": "INTEGER",
            "updated_at": "TEXT",
        },
        "jobs": {
            "lifecycle_stage": "TEXT",
            "closed_reason": "TEXT",
            "closed_at": "TEXT",
        },
    }.items():
        for name, definition in columns.items():
            if add_column(conn, table, name, definition):
                added.append(f"{table}.{name}")

    conn.execute(
        """
        UPDATE followup_tasks
        SET id = rowid
        WHERE id IS NULL OR id = 0
        """
    )
    conn.execute(
        """
        UPDATE client_feedback_events
        SET id = rowid
        WHERE id IS NULL OR id = 0
        """
    )
    conn.execute(
        """
        UPDATE followup_tasks
        SET job_candidate_id = (
            SELECT jc.id
            FROM job_candidates jc
            LEFT JOIN jobs j ON j.id = jc.job_id
            LEFT JOIN clients cl ON cl.id = j.client_id
            WHERE CAST(jc.source_candidate_id AS TEXT) = CAST(followup_tasks.candidate_id AS TEXT)
              AND (COALESCE(followup_tasks.client, '') = '' OR cl.name = followup_tasks.client)
              AND (COALESCE(followup_tasks.position, '') = '' OR j.title = followup_tasks.position OR jc.raw_position = followup_tasks.position)
            ORDER BY jc.id DESC
            LIMIT 1
        )
        WHERE job_candidate_id IS NULL AND candidate_id IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE client_feedback_events
        SET job_candidate_id = (
            SELECT jc.id
            FROM job_candidates jc
            LEFT JOIN jobs j ON j.id = jc.job_id
            LEFT JOIN clients cl ON cl.id = j.client_id
            WHERE CAST(jc.source_candidate_id AS TEXT) = CAST(client_feedback_events.candidate_id AS TEXT)
              AND (COALESCE(client_feedback_events.client, '') = '' OR cl.name = client_feedback_events.client)
              AND (COALESCE(client_feedback_events.position, '') = '' OR j.title = client_feedback_events.position OR jc.raw_position = client_feedback_events.position)
            ORDER BY jc.id DESC
            LIMIT 1
        )
        WHERE job_candidate_id IS NULL AND candidate_id IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE jobs
        SET lifecycle_stage = CASE
            WHEN COALESCE(status, '') LIKE '%误归属%' OR COALESCE(status, '') LIKE '%迁移%' THEN 'archived'
            WHEN COALESCE(status, '') IN ('关闭','已关闭','closed') THEN 'closed'
            WHEN COALESCE(status, '') IN ('暂停','paused','hold') THEN 'paused'
            WHEN COALESCE(status, '') LIKE '%谈薪%' OR COALESCE(status, '') LIKE '%Offer%' THEN 'offer'
            WHEN COALESCE(status, '') LIKE '%反馈%' THEN 'client_feedback'
            WHEN COALESCE(status, '') LIKE '%触达%' OR COALESCE(status, '') LIKE '%推进%' THEN 'active_pipeline'
            WHEN COALESCE(status, '') LIKE '%搜索%' THEN 'sourcing'
            WHEN COALESCE(status, '') LIKE '%发布%' THEN 'published'
            WHEN COALESCE(status, '') LIKE 'P0%' OR COALESCE(status, '') LIKE '%启动%' OR COALESCE(status, '') = '' THEN 'intake'
            ELSE 'intake'
        END
        WHERE COALESCE(lifecycle_stage, '') = ''
        """
    )

    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_tasks_stable_id ON followup_tasks(id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_client_feedback_stable_id ON client_feedback_events(id);
        CREATE INDEX IF NOT EXISTS idx_followup_tasks_job_candidate ON followup_tasks(job_candidate_id, status, due_at);
        CREATE INDEX IF NOT EXISTS idx_feedback_job_candidate ON client_feedback_events(job_candidate_id, feedback_time);
        CREATE INDEX IF NOT EXISTS idx_jobs_lifecycle ON jobs(lifecycle_stage, status);

        DROP VIEW IF EXISTS v_job_lifecycle;
        CREATE VIEW v_job_lifecycle AS
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

        DROP VIEW IF EXISTS v_workbench_timeline;
        CREATE VIEW v_workbench_timeline AS
        SELECT
            'candidate_event' AS source_type,
            ce.id AS source_id,
            ce.job_candidate_id,
            ce.person_id,
            ce.job_id,
            c.name AS client,
            j.title AS job,
            p.display_name AS candidate,
            ce.event_type AS event_type,
            ce.event_status AS event_status,
            ce.event_time AS event_time,
            ce.summary AS summary
        FROM candidate_events ce
        LEFT JOIN job_candidates jc ON jc.id = ce.job_candidate_id
        LEFT JOIN people p ON p.id = COALESCE(ce.person_id, jc.person_id)
        LEFT JOIN jobs j ON j.id = COALESCE(ce.job_id, jc.job_id)
        LEFT JOIN clients c ON c.id = j.client_id
        UNION ALL
        SELECT
            'client_feedback', f.id, f.job_candidate_id, jc.person_id, jc.job_id,
            f.client, f.position, f.candidate_name, 'client_feedback', f.feedback_type,
            COALESCE(f.feedback_time, f.created_at),
            TRIM(COALESCE(f.feedback_detail, '') || CASE WHEN COALESCE(f.next_action, '') = '' THEN '' ELSE '｜下一步：' || f.next_action END)
        FROM client_feedback_events f
        LEFT JOIN job_candidates jc ON jc.id = f.job_candidate_id
        UNION ALL
        SELECT
            'followup_task', t.id, t.job_candidate_id, jc.person_id, jc.job_id,
            t.client, t.position, t.candidate_name, 'followup_task', COALESCE(t.status, 'open'),
            COALESCE(t.updated_at, t.created_at, t.due_at),
            TRIM(COALESCE(t.task_type, '跟进任务') || CASE WHEN COALESCE(t.reason, '') = '' THEN '' ELSE '｜' || t.reason END)
        FROM followup_tasks t
        LEFT JOIN job_candidates jc ON jc.id = t.job_candidate_id
        UNION ALL
        SELECT
            'search_experiment', s.id, NULL, NULL, j.id,
            s.client, s.position, '', 'search_experiment', COALESCE(s.status, s.channel, ''),
            COALESCE(s.run_time, s.created_at),
            TRIM(COALESCE(s.channel, '') || '｜' || COALESCE(s.query, '') || '｜结果 ' || COALESCE(s.result_count, 0))
        FROM search_experiments s
        LEFT JOIN clients c ON c.name = s.client
        LEFT JOIN jobs j ON j.client_id = c.id AND j.title = s.position;

        DROP VIEW IF EXISTS v_workbench_exceptions;
        CREATE VIEW v_workbench_exceptions AS
        SELECT 'duplicate_relation' AS exception_type, 'high' AS severity,
               MIN(jc.id) AS record_id, c.name AS client, j.title AS job,
               p.display_name AS candidate,
               '同一岗位、人选和原始岗位存在重复推进关系' AS detail,
               MAX(jc.updated_at) AS detected_at
        FROM job_candidates jc
        JOIN people p ON p.id = jc.person_id
        LEFT JOIN jobs j ON j.id = jc.job_id
        LEFT JOIN clients c ON c.id = j.client_id
        GROUP BY jc.job_id, jc.person_id, jc.raw_position
        HAVING COUNT(*) > 1
        UNION ALL
        SELECT 'orphan_task', 'medium', t.id, t.client, t.position, t.candidate_name,
               '开放跟进任务未绑定 job_candidate_id', COALESCE(t.updated_at, t.created_at)
        FROM followup_tasks t
        WHERE COALESCE(t.status, 'open') = 'open' AND t.job_candidate_id IS NULL
        UNION ALL
        SELECT 'overdue_task', 'high', t.id, t.client, t.position, t.candidate_name,
               '开放跟进任务已逾期：' || COALESCE(t.due_at, ''), COALESCE(t.updated_at, t.created_at)
        FROM followup_tasks t
        WHERE COALESCE(t.status, 'open') = 'open'
          AND COALESCE(t.due_at, '') != ''
          AND datetime(t.due_at) < datetime('now','localtime')
        UNION ALL
        SELECT 'stale_job', 'medium', j.id, c.name, j.title, '',
               '开放岗位超过 14 天没有更新', j.updated_at
        FROM jobs j JOIN clients c ON c.id = j.client_id
        WHERE COALESCE(j.lifecycle_stage, 'intake') NOT IN ('closed','archived','paused')
          AND datetime(COALESCE(j.updated_at, j.created_at)) < datetime('now','localtime','-14 days');
        """
    )
    conn.commit()
    return {
        "added_columns": added,
        "followup_tasks": conn.execute("SELECT COUNT(*) FROM followup_tasks").fetchone()[0],
        "client_feedback_events": conn.execute("SELECT COUNT(*) FROM client_feedback_events").fetchone()[0],
        "lifecycle_rows": conn.execute("SELECT COUNT(*) FROM v_job_lifecycle").fetchone()[0],
        "timeline_rows": conn.execute("SELECT COUNT(*) FROM v_workbench_timeline").fetchone()[0],
        "exception_rows": conn.execute("SELECT COUNT(*) FROM v_workbench_exceptions").fetchone()[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(str(Path(args.db).expanduser()))
    try:
        result = install(conn)
        if args.check:
            result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
