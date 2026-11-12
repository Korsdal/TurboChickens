"""SQLite storage: the MVP schema (baseline v0.2, section 12).

Append-only tables (job_specs, run_events, results, teardown_attestations) are
protected by triggers that abort any UPDATE or DELETE — the result store and
attestations are append-only by construction. runs, job_usage_stats, and
schedules stay mutable (status transitions, stats rollups, schedule add/rm —
spec 06; stats rollups — spec 08). The audit trail itself is the append-only
JSONL file in audit.py. Trajectory tables are deliberately absent (post-MVP).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS job_specs (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        spec_hash TEXT NOT NULL,
        spec_toml TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES job_specs(id),
        status TEXT NOT NULL,
        trigger TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        manifest_json TEXT,
        usage_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_events (
        run_id TEXT NOT NULL REFERENCES runs(id),
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        level TEXT NOT NULL,
        source TEXT NOT NULL,
        line TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS results (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        valid INTEGER NOT NULL,
        output_json TEXT,
        artifacts_dir TEXT,
        schema_errors TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS teardown_attestations (
        run_id TEXT PRIMARY KEY REFERENCES runs(id),
        process_reaped INTEGER NOT NULL,
        workspace_removed INTEGER NOT NULL,
        env_cleared INTEGER NOT NULL,
        verified_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_usage_stats (
        job_id TEXT PRIMARY KEY REFERENCES job_specs(id),
        n_runs INTEGER NOT NULL,
        tokens_in_mean REAL,
        tokens_out_mean REAL,
        tokens_p95 REAL,
        expected_cost REAL,
        updated_at TEXT NOT NULL,
        -- spec 08: full mean/p50/p95 for tokens in and out
        tokens_in_p50 REAL,
        tokens_in_p95 REAL,
        tokens_out_p50 REAL,
        tokens_out_p95 REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedules (
        job_id TEXT PRIMARY KEY REFERENCES job_specs(id),
        spec_path TEXT NOT NULL,
        schedule TEXT NOT NULL,
        spec_hash TEXT NOT NULL,
        added_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
]

_APPEND_ONLY_TABLES = ("job_specs", "run_events", "results", "teardown_attestations")


def init_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the full MVP schema.

    Idempotent: safe to call on every daemon start.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    for stmt in _TABLES:
        conn.execute(stmt)
    for table in _APPEND_ONLY_TABLES:
        for op in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{op.lower()} "
                f"BEFORE {op} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
    # spec 08 migration: DBs created at schema_version 2 lack the percentile
    # columns on job_usage_stats; add them in place (the table is mutable).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(job_usage_stats)")}
    for col in ("tokens_in_p50", "tokens_in_p95", "tokens_out_p50", "tokens_out_p95"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE job_usage_stats ADD COLUMN {col} REAL")
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
