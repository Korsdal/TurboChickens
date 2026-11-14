"""Tests for the SQLite schema and the append-only JSONL audit log."""

from __future__ import annotations

import json
import sqlite3

import pytest

from turbochickens.audit import AuditLog
from turbochickens.storage import SCHEMA_VERSION, init_db, table_names

EXPECTED_TABLES = {
    "job_specs",
    "runs",
    "run_events",
    "results",
    "teardown_attestations",
    "job_usage_stats",
    "schema_meta",
}


def _seed_run(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at) VALUES ('j1', 'n', 'h', 'toml', 'ts')"
    )
    conn.execute(
        "INSERT INTO runs (id, job_id, status, trigger) VALUES ('r1', 'j1', 'queued', 'manual')"
    )
    conn.execute(
        "INSERT INTO run_events (run_id, seq, ts, level, source, line) VALUES ('r1', 0, 'ts', 'info', 'harness', 'hi')"
    )
    conn.execute("INSERT INTO results (run_id, valid) VALUES ('r1', 1)")
    conn.execute(
        "INSERT INTO teardown_attestations (run_id, process_reaped, workspace_removed, env_cleared, verified_at)"
        " VALUES ('r1', 1, 1, 1, 'ts')"
    )
    conn.commit()


def test_schema_creates_all_mvp_tables(tmp_path) -> None:
    conn = init_db(tmp_path / "tc.db")
    assert EXPECTED_TABLES <= table_names(conn)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row is not None and row[0] == str(SCHEMA_VERSION)
    conn.close()


def test_init_db_is_idempotent(tmp_path) -> None:
    db = tmp_path / "tc.db"
    conn = init_db(db)
    conn.close()
    conn = init_db(db)  # second open must not fail or duplicate
    assert EXPECTED_TABLES <= table_names(conn)
    conn.close()


def test_append_only_tables_reject_update_and_delete(tmp_path) -> None:
    conn = init_db(tmp_path / "tc.db")
    _seed_run(conn)
    forbidden = [
        "UPDATE job_specs SET name = 'x' WHERE id = 'j1'",
        "DELETE FROM job_specs WHERE id = 'j1'",
        "UPDATE run_events SET line = 'x' WHERE run_id = 'r1' AND seq = 0",
        "DELETE FROM run_events WHERE run_id = 'r1'",
        "UPDATE results SET valid = 0 WHERE run_id = 'r1'",
        "DELETE FROM results WHERE run_id = 'r1'",
        "UPDATE teardown_attestations SET process_reaped = 0 WHERE run_id = 'r1'",
        "DELETE FROM teardown_attestations WHERE run_id = 'r1'",
    ]
    for sql in forbidden:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    conn.close()


def test_runs_and_stats_tables_stay_mutable(tmp_path) -> None:
    conn = init_db(tmp_path / "tc.db")
    _seed_run(conn)
    conn.execute("UPDATE runs SET status = 'running' WHERE id = 'r1'")  # lifecycle transitions
    conn.execute(
        "INSERT INTO job_usage_stats (job_id, n_runs, tokens_in_mean, tokens_out_mean, tokens_p95, expected_cost, updated_at)"
        " VALUES ('j1', 1, 100.0, 50.0, 90.0, 0.01, 'ts')"
    )
    conn.execute("UPDATE job_usage_stats SET n_runs = 2 WHERE job_id = 'j1'")  # stats rollup
    conn.commit()
    assert conn.execute("SELECT status FROM runs WHERE id = 'r1'").fetchone()[0] == "running"
    assert conn.execute("SELECT n_runs FROM job_usage_stats WHERE job_id = 'j1'").fetchone()[0] == 2
    conn.close()


def test_audit_log_appends_rows_in_order(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"event": "coopd.started", "pid": 1})
    log.append({"event": "coopd.stopped", "pid": 1})
    rows = log.read_all()
    assert [r["event"] for r in rows] == ["coopd.started", "coopd.stopped"]
    assert all(r["ts"].endswith("Z") for r in rows)
    assert rows[0]["ts"] <= rows[1]["ts"]


def test_audit_log_is_append_only_across_reopen(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append({"event": "one"})
    AuditLog(path).append({"event": "two"})  # a fresh handle must append, never truncate
    AuditLog(path).append({"event": "three"})
    rows = AuditLog(path).read_all()
    assert [r["event"] for r in rows] == ["one", "two", "three"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all(json.loads(line) for line in lines)  # every line is one JSON object


def test_audit_log_missing_file_reads_empty(tmp_path) -> None:
    assert AuditLog(tmp_path / "nope.jsonl").read_all() == []
