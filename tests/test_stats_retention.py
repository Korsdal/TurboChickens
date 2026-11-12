"""Tests for spec 08: per-job usage stats + expected cost + retention TTLs.

Seams:
- pure: `stats.percentile` / `stats.rollup_usage` math, `retention.months_before`
  calendar arithmetic, and the storage migration to schema_version 3.
- e2e: the real daemon on a temp socket (thread), driven through the `tc` CLI
  and the JSON-lines socket, with the hermetic fake vibe binary (spec 03) and
  the hermetic loopback fake provider (spec 05). Retention is exercised by
  backdating `runs.finished_at` (the `runs` table is mutable by design) and
  triggering the `retention_sweep` op.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

# fixtures + helpers shared with the spec 03 / spec 05 e2e suites
from test_api_adapter import api_daemon, fake_api, providers_file, write_api_job  # noqa: F401
from test_daemon import DaemonThread
from test_run_e2e import (  # noqa: F401
    combined_output,
    daemon,
    db_row,
    fake_vibe,
    run_id_of,
    settings_file,
    wait_terminal,
    write_job,
)
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.retention import months_before, parse_ts, sweep_expired_results
from turbochickens.settings import load_settings
from turbochickens.stats import percentile, rollup_usage
from turbochickens.storage import SCHEMA_VERSION, init_db
from turbochickens.util import utcnow, utcnow_iso

runner = CliRunner()


def backdate_run(settings, run_id: str, finished_at: str) -> None:
    """The `runs` table is mutable by design; tests backdate finished_at to
    simulate an old run for retention."""
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
        conn.commit()
    finally:
        conn.close()


def months_ago_iso(months: int) -> str:
    return months_before(utcnow(), months).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- pure seams ----------------------------------------------------------------


def test_percentile_linear_interpolation() -> None:
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7.0
    vals = [10.0, 20.0, 30.0, 40.0]
    assert percentile(vals, 50) == pytest.approx(25.0)
    assert percentile(vals, 95) == pytest.approx(38.5)
    assert percentile(vals, 0) == 10.0
    assert percentile(vals, 100) == 40.0


def test_rollup_usage_computes_n_mean_p50_p95_in_out_and_total() -> None:
    usages = [
        {"tokens_in": 100, "tokens_out": 40},
        {"tokens_in": 200, "tokens_out": 60},
        {"tokens_in": 300, "tokens_out": 80},
        {"tokens_in": 400, "tokens_out": 100},
    ]
    s = rollup_usage(usages)
    assert s["n_runs"] == 4
    assert s["tokens_in_mean"] == pytest.approx(250.0)
    assert s["tokens_in_p50"] == pytest.approx(250.0)
    assert s["tokens_in_p95"] == pytest.approx(385.0)  # 300 + 0.85*(400-300)
    assert s["tokens_out_mean"] == pytest.approx(70.0)
    assert s["tokens_out_p50"] == pytest.approx(70.0)
    assert s["tokens_out_p95"] == pytest.approx(97.0)  # 80 + 0.85*(100-80)
    assert s["tokens_p95"] == pytest.approx(482.0)  # p95 of per-run totals [140,260,380,500]
    # missing tokens count as zero; non-dict entries are skipped
    s2 = rollup_usage([{"tokens_in": 5}, "junk", {}])
    assert s2["n_runs"] == 2 and s2["tokens_in_mean"] == pytest.approx(2.5)
    assert rollup_usage([])["n_runs"] == 0


def test_months_before_calendar_arithmetic() -> None:
    dt = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    assert months_before(dt, 1) == datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc)
    assert months_before(dt, 6) == datetime(2025, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert months_before(dt, 14) == datetime(2025, 1, 31, 12, 0, tzinfo=timezone.utc)
    assert parse_ts("2026-11-12T13:30:00.123Z") == datetime(2026, 11, 12, 13, 30, 0, 123000, tzinfo=timezone.utc)


def test_schema_v3_adds_percentile_columns_and_migrates_v2_dbs(tmp_path) -> None:
    # fresh DB: schema_version 4 with all percentile columns + job_specs.spec_path (spec 16)
    conn = init_db(tmp_path / "fresh.db")
    try:
        assert SCHEMA_VERSION == 4
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
        assert ver == "4"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(job_usage_stats)")}
        assert {"tokens_in_p50", "tokens_in_p95", "tokens_out_p50", "tokens_out_p95"} <= cols
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_specs)")}
        assert "spec_path" in job_cols
    finally:
        conn.close()
    # a v2-era DB (old column set) is migrated in place by init_db
    old = tmp_path / "old.db"
    c = sqlite3.connect(str(old))
    c.execute(
        "CREATE TABLE job_usage_stats (job_id TEXT PRIMARY KEY, n_runs INTEGER NOT NULL,"
        " tokens_in_mean REAL, tokens_out_mean REAL, tokens_p95 REAL, expected_cost REAL,"
        " updated_at TEXT NOT NULL)"
    )
    c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    c.commit()
    c.close()
    conn = init_db(old)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(job_usage_stats)")}
        assert {"tokens_in_p50", "tokens_in_p95", "tokens_out_p50", "tokens_out_p95"} <= cols
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
        assert ver == "4"
    finally:
        conn.close()
    # a v3-era DB (job_specs without spec_path) is migrated in place by init_db
    v3 = tmp_path / "v3.db"
    c = sqlite3.connect(str(v3))
    c.execute(
        "CREATE TABLE job_specs (id TEXT PRIMARY KEY, name TEXT NOT NULL, spec_hash TEXT NOT NULL,"
        " spec_toml TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO schema_meta VALUES ('schema_version', '3')")
    c.commit()
    c.close()
    conn = init_db(v3)
    try:
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_specs)")}
        assert "spec_path" in job_cols
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
        assert ver == "4"
        # the append-only guard still holds after the in-place migration
        conn.execute(
            "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at, spec_path)"
            " VALUES ('j', 'j', 'h', 't', 'now', '/p/job.toml')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE job_specs SET name = 'x' WHERE id = 'j'")
    finally:
        conn.close()


# --- usage stats e2e (spec 08 checkboxes 1 + 2) --------------------------------


def test_api_runs_roll_up_per_job_stats_with_expected_cost(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_api_job(tmp_path, name="stats-api-job", prompt="Say hello")
    run_ids = []
    for _ in range(3):
        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
        run_ids.append(run_id_of(result))
    settings = api_daemon.settings
    # each run's manifest carries its token usage (telemetry, never a cap)
    for run_id in run_ids:
        run = wait_terminal(settings.socket_path, run_id)
        assert run["status"] == "succeeded"
        assert run["manifest"]["usage"]["tokens_in"] == 100
        assert run["manifest"]["usage"]["tokens_out"] == 40
        assert run["manifest"]["usage"]["cost_usd"] == pytest.approx(0.00018)

    # per-job stats over the socket: n, mean/p50/p95 in+out, expected cost
    resp = request(settings.socket_path, "stats_show", job_id="stats-api-job", timeout=2.0)
    assert resp["ok"] is True
    s = resp["stats"]
    assert s["n_runs"] == 3
    assert s["tokens_in_mean"] == s["tokens_in_p50"] == s["tokens_in_p95"] == 100
    assert s["tokens_out_mean"] == s["tokens_out_p50"] == s["tokens_out_p95"] == 40
    assert s["tokens_p95"] == 140
    # providers.toml pricing: 1.0/Mtok in, 2.0/Mtok out → (100*1 + 40*2)/1e6
    assert s["expected_cost_usd"] == pytest.approx(0.00018)

    # stats_list carries the same row; the CLI surfaces both
    lst = request(settings.socket_path, "stats_list", timeout=2.0)
    assert lst["ok"] is True and [x["job_id"] for x in lst["stats"]] == ["stats-api-job"]
    out = runner.invoke(app, ["stats", "list"])
    assert out.exit_code == 0, combined_output(out)
    assert "job=stats-api-job" in out.output and "n=3" in out.output and "expected_cost_usd=0.00018" in out.output
    show = runner.invoke(app, ["stats", "show", "stats-api-job"])
    assert show.exit_code == 0, combined_output(show)
    assert "n_runs: 3" in show.output and "tokens_in" in show.output and "expected_cost_usd_per_run" in show.output

    # the rollup row is in the mutable job_usage_stats table
    row = db_row(
        settings,
        "SELECT n_runs, tokens_in_p95, tokens_out_p95, tokens_p95, expected_cost FROM job_usage_stats"
        " WHERE job_id = 'stats-api-job'",
    )
    assert row is not None
    assert row[0] == 3 and row[1] == 100 and row[2] == 40 and row[3] == 140
    assert row[4] == pytest.approx(0.00018)


def test_cli_runs_roll_up_stats_with_harness_cost_fallback(daemon, tmp_path) -> None:
    # vibe-cli job with a model that does not resolve from providers.toml: the
    # expected cost falls back to the per-run cost_usd the harness reported
    spec_path = write_job(tmp_path, name="stats-cli-job", prompt="Say hello")
    for _ in range(3):
        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
    resp = request(daemon.settings.socket_path, "stats_show", job_id="stats-cli-job", timeout=2.0)
    assert resp["ok"] is True
    s = resp["stats"]
    assert s["n_runs"] == 3
    assert s["tokens_in_mean"] == s["tokens_in_p50"] == s["tokens_in_p95"] == 12  # fake vibe export.json
    assert s["tokens_out_mean"] == s["tokens_out_p50"] == s["tokens_out_p95"] == 34
    assert s["tokens_p95"] == 46
    assert s["expected_cost_usd"] == pytest.approx(0.001)  # mean of harness-reported cost_usd


def test_stats_show_unknown_job_is_an_error(daemon) -> None:
    resp = request(daemon.settings.socket_path, "stats_show", job_id="nope", timeout=2.0)
    assert resp["ok"] is False and "no usage stats" in resp["error"]
    out = runner.invoke(app, ["stats", "show", "nope"])
    assert out.exit_code == 1
    lst = runner.invoke(app, ["stats", "list"])
    assert lst.exit_code == 0 and "no usage stats yet" in lst.output


# --- retention e2e (spec 08 checkbox 3) ----------------------------------------


def test_retention_sweep_expires_old_results_and_audit_logs_each_deletion(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="retention-job", prompt="Say hello")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    artifacts = Path(run["result"]["artifacts_dir"])
    assert artifacts.exists() and (artifacts / "output.json").exists()
    audit_before = AuditLog(settings.audit_log_path).read_all()

    # not old enough yet: a sweep now must not delete anything
    resp = request(settings.socket_path, "retention_sweep", timeout=5.0)
    assert resp["ok"] is True and resp["deleted"] == 0 and resp["expired"] == []
    assert artifacts.exists()

    # backdate the run past the 6-month TTL and sweep again
    backdate_run(settings, run_id, months_ago_iso(7))
    resp = request(settings.socket_path, "retention_sweep", timeout=5.0)
    assert resp["ok"] is True
    assert resp["audit_retention"] == "forever"
    assert resp["results_retention_months"] == 6
    assert resp["deleted"] == 1
    assert resp["expired"][0]["run_id"] == run_id
    assert resp["expired"][0]["kind"] == "result"
    assert not artifacts.exists()

    # the append-only results row is NOT deleted; the deletion is audit-logged
    assert db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_id,)) == (1,)
    rows = AuditLog(settings.audit_log_path).read_all()
    expired_events = [r for r in rows if r["event"] == "result.expired" and r.get("run_id") == run_id]
    assert len(expired_events) == 1
    assert expired_events[0]["kind"] == "result" and expired_events[0]["retention_months"] == 6
    # the audit log itself is never TTL-deleted: all earlier rows are intact
    assert rows[: len(audit_before)] == audit_before

    # results_get now reports the result as expired (artifacts gone); CLI exits 1
    got = request(settings.socket_path, "results_get", run_id=run_id, timeout=2.0)
    assert got["ok"] is False and got.get("expired") is True and "expired" in got["error"]
    cli = runner.invoke(app, ["results", "get", run_id])
    assert cli.exit_code == 1 and "expired" in combined_output(cli)

    # a second sweep is idempotent: nothing left to delete, no duplicate audit rows
    resp2 = request(settings.socket_path, "retention_sweep", timeout=5.0)
    assert resp2["deleted"] == 0 and resp2["expired"] == []
    assert len([r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "result.expired"]) == 1


def test_retention_sweep_expires_quarantined_output_too(daemon, tmp_path) -> None:
    # a job whose schema the fake vibe output violates → quarantined (valid=0)
    d = tmp_path / "quarantine-job"
    (d / "prompts").mkdir(parents=True)
    (d / "schemas").mkdir(parents=True)
    (d / "prompts" / "p.txt").write_text("Say hello")
    (d / "schemas" / "out.schema.json").write_text('{"type": "object", "required": ["no_such_field"]}')
    (d / "job.toml").write_text(
        """
[job]
name = "quarantine-job"
[trigger]
type = "manual"
[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0
[resources]
timeout_sec = 30
max_context_mb = 2
network = "none"
[input]
prompt_file = "prompts/p.txt"
attachments = []
[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    result = runner.invoke(app, ["run", str(d / "job.toml"), "--wait"])
    assert result.exit_code == 1  # quarantined
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed" and run["result"]["valid"] is False
    qdir = Path(run["result"]["artifacts_dir"])
    assert qdir.exists() and (qdir / "raw_output.txt").exists()

    backdate_run(settings, run_id, months_ago_iso(7))
    resp = request(settings.socket_path, "retention_sweep", timeout=5.0)
    assert resp["ok"] is True and resp["deleted"] == 1
    assert resp["expired"][0]["kind"] == "quarantine"
    assert not qdir.exists()
    # the quarantined results row (valid=0) is append-only and stays
    assert db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_id,)) == (0,)
    ev = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "result.expired"]
    assert len(ev) == 1 and ev[0]["run_id"] == run_id and ev[0]["kind"] == "quarantine"


def test_retention_respects_custom_results_retention_months(tmp_path, monkeypatch, fake_vibe) -> None:
    # settings override: results_retention_months = 1 → a 2-month-old run expires
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\nresults_retention_months = 1\n'
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    try:
        spec_path = write_job(tmp_path, name="ttl-job", prompt="Say hello")
        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
        run_id = run_id_of(result)
        run = wait_terminal(settings.socket_path, run_id)
        artifacts = Path(run["result"]["artifacts_dir"])
        assert artifacts.exists()
        backdate_run(settings, run_id, months_ago_iso(2))
        resp = request(settings.socket_path, "retention_sweep", timeout=5.0)
        assert resp["ok"] is True and resp["results_retention_months"] == 1
        assert resp["deleted"] == 1 and not artifacts.exists()
        # a fresh run (finished now) is not expired under the 1-month TTL
        spec_path2 = write_job(tmp_path, name="ttl-fresh-job", prompt="Say hello again")
        result2 = runner.invoke(app, ["run", str(spec_path2), "--wait"])
        assert result2.exit_code == 0, combined_output(result2)
        run2 = wait_terminal(settings.socket_path, run_id_of(result2))
        assert Path(run2["result"]["artifacts_dir"]).exists()
        resp2 = request(settings.socket_path, "retention_sweep", timeout=5.0)
        assert resp2["deleted"] == 0
    finally:
        dt.stop()


def test_daemon_startup_sweeps_expired_results_automatically(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="startup-sweep-job", prompt="Say hello")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    artifacts = Path(run["result"]["artifacts_dir"])
    assert artifacts.exists()
    backdate_run(settings, run_id, months_ago_iso(8))
    daemon.stop()  # restart on the same data dir: the startup sweep must fire
    dt = DaemonThread(settings)
    dt.start()
    try:
        assert not artifacts.exists()
        ev = [
            r
            for r in AuditLog(settings.audit_log_path).read_all()
            if r["event"] == "result.expired" and r.get("run_id") == run_id
        ]
        assert len(ev) == 1
        # status reports the retention settings and the last sweep
        st = request(settings.socket_path, "status", timeout=2.0)
        assert st["retention"]["audit_retention"] == "forever"
        assert st["retention"]["results_retention_months"] == 6
        assert st["retention"]["last_sweep"]["deleted"] == 1
        cli = runner.invoke(app, ["status"])
        assert cli.exit_code == 0 and "retention: audit_log=forever results_retention_months=6" in cli.output
    finally:
        dt.stop()


def test_sweep_pure_seam_never_touches_audit_log(tmp_path) -> None:
    # pure-seam check of the sweep contract on a hand-built db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(f'data_dir = "{data_dir}"\n')
    settings = load_settings(settings_file)
    db = init_db(settings.db_path)
    audit = AuditLog(settings.audit_log_path)
    audit.append({"event": "daemon.started", "pid": 1})
    db.execute(
        "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at) VALUES ('j', 'j', 'h', 'x', ?)",
        (utcnow_iso(),),
    )
    db.execute(
        "INSERT INTO runs (id, job_id, status, trigger, started_at, finished_at) VALUES ('run-x', 'j', 'succeeded', 'manual', ?, ?)",
        (months_ago_iso(9), months_ago_iso(9)),
    )
    adir = settings.results_dir / "run-x"
    adir.mkdir(parents=True)
    (adir / "output.json").write_text("{}")
    db.execute(
        "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors) VALUES ('run-x', 1, '{}', ?, NULL)",
        (str(adir),),
    )
    db.commit()
    n_audit_before = len(audit.read_all())
    summary = sweep_expired_results(settings, db, audit)
    assert summary["deleted"] == 1 and not adir.exists()
    rows = audit.read_all()
    assert len(rows) == n_audit_before + 1  # only the result.expired row was appended
    assert rows[0]["event"] == "daemon.started"  # audit log untouched, kept forever
    assert rows[1]["event"] == "result.expired"
    db.close()
