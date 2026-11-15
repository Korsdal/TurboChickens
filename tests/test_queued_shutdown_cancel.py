"""Tests for spec 32 ticket 20 (review finding G16): eggs still queued in the
Pecking Order when Coopd shuts down are cancelled through the attested cancel
path (Headcount rows + abattoir attestation), stale queued rows left by a dead
Coopd are recovered at startup into the same cancelled-with-attestation
state, and the status alerts surface any non-terminal row the live Coopd does
not own — no run is left non-terminal across restarts without being visible.

Seams: the real daemon on a temp socket (in a thread for the shutdown/alert
tests, as a real `python -m turbochickens daemon start` subprocess for the
kill-and-restart recovery test), driven through the JSON-lines socket and the
`tc` CLI, with the fake vibe binary via $TURBOCHICKENS_VIBE_BIN and
weight_budget_override = 1 so a second egg backlogs in the pecking order.
Assertions are external, observable behavior only: socket/CLI responses, the
store + Headcount contents, status transitions, attestations, alerts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, db_row, run_id_of, write_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings
from turbochickens.util import utcnow_iso

runner = CliRunner()

ATT_SQL = (
    "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?"
)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    # budget 1: one vibe-cli chicken (weight 1.0) fills the whole budget, so a
    # second enqueued egg backlogs in the pecking order (queued)
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def daemon(settings_file, fake_vibe):
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def wait_status(socket_path, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


def wait_terminal(socket_path, run_id: str, timeout: float = 40.0) -> dict:
    return wait_status(socket_path, run_id, TERMINAL_STATES, timeout)


def wait_ping(proc, settings, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if settings.socket_path.exists():
            try:
                if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                    return
            except DaemonUnreachable:
                pass
        if proc.poll() is not None:
            raise AssertionError("daemon subprocess exited early")
        time.sleep(0.05)
    raise AssertionError("daemon subprocess did not start answering")


def start_daemon_subprocess(env) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "daemon", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# --- shutdown cancels every queued egg through the attested path ---------------


def test_shutdown_cancels_queued_eggs_with_attestation(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    sock = settings.socket_path
    slow = write_job(tmp_path, name="slow-shutdown", prompt="SLEEP 30 slow", timeout=3)
    try:
        r1 = runner.invoke(app, ["run", str(slow)])
        r2 = runner.invoke(app, ["run", str(slow)])
        assert r1.exit_code == 0 and r2.exit_code == 0, combined_output(r1) + combined_output(r2)
        id1, id2 = run_id_of(r1), run_id_of(r2)
        wait_status(sock, id1, ("provisioning", "running"))
        # id1 fills the budget of 1 → id2 sits queued in the pecking order,
        # tracked by this coopd, so it is not an alert
        show2 = request(sock, "runs_show", run_id=id2, timeout=2.0)["run"]
        assert show2["status"] == "queued"
        st = request(sock, "status", timeout=2.0)
        assert st["queue_depth"] == 1
        assert id2 not in st["alerts"] and st["alerts"] == []

        # shutdown with a non-empty pecking order
        resp = request(sock, "shutdown", timeout=5.0)
        assert resp["ok"] is True
    finally:
        dt.stop()  # joins the daemon thread: the shutdown drain must complete

    # the store after shutdown: the queued egg is terminal `cancelled`, attested
    conn = sqlite3.connect(str(settings.db_path))
    try:
        row = conn.execute(
            "SELECT status, manifest_json FROM runs WHERE id = ?", (id2,)
        ).fetchone()
        assert row is not None and row[0] == "cancelled"
        manifest = json.loads(row[1])
        assert manifest["status"] == "cancelled"
        assert "never hatched" in manifest["status_reason"]
        assert "shutdown" in manifest["status_reason"]
        att = conn.execute(ATT_SQL, (id2,)).fetchone()
        assert att == (1, 1, 1)
        # never hatched: no harness events at all
        n_harness = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND source = 'harness'", (id2,)
        ).fetchone()[0]
        assert n_harness == 0
        # the in-flight chicken still reached a terminal state (its own fence)
        row1 = conn.execute("SELECT status FROM runs WHERE id = ?", (id1,)).fetchone()
        assert row1[0] in TERMINAL_STATES
        att1 = conn.execute(ATT_SQL, (id1,)).fetchone()
        assert att1 == (1, 1, 1)
    finally:
        conn.close()

    # Headcount: the attested cancel rows + coopd.stopped, no hatch, no clutch
    audit = AuditLog(settings.audit_log_path).read_all()
    cancelled = [r for r in audit if r.get("event") == "egg.cancelled" and r.get("run_id") == id2]
    assert len(cancelled) == 1
    assert "never hatched" in cancelled[0]["reason"] and "shutdown" in cancelled[0]["reason"]
    assert any(
        r.get("event") == "abattoir.attested" and r.get("run_id") == id2 and r.get("process_reaped")
        for r in audit
    )
    fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == id2]
    assert fin and fin[0]["status"] == "cancelled"
    assert not any(r.get("event") == "egg.hatched" and r.get("run_id") == id2 for r in audit)
    assert not any(r.get("event") == "clutch.stored" and r.get("run_id") == id2 for r in audit)
    assert any(r.get("event") == "coopd.stopped" for r in audit)

    # nothing left behind: no workspace, no socket file
    assert not (settings.data_dir / "workspaces" / id2).exists()
    assert not settings.socket_path.exists()


# --- startup recovers stale queued rows left by a killed coopd ------------------


def test_startup_recovers_stale_queued_rows_after_killed_daemon(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    slow = write_job(tmp_path, name="slow-crash", prompt="SLEEP 30 crash", timeout=120)
    env = dict(os.environ)  # includes TURBOCHICKENS_SETTINGS and TURBOCHICKENS_VIBE_BIN
    proc = start_daemon_subprocess(env)
    try:
        wait_ping(proc, settings)
        sock = settings.socket_path
        r1 = request(sock, "enqueue", spec_path=str(slow), timeout=10.0)
        r2 = request(sock, "enqueue", spec_path=str(slow), timeout=10.0)
        assert r1["ok"] and r2["ok"]
        id1, id2 = r1["run_id"], r2["run_id"]
        wait_status(sock, id1, ("provisioning", "running"))
        show2 = request(sock, "runs_show", run_id=id2, timeout=2.0)["run"]
        assert show2["status"] == "queued"
        st = request(sock, "status", timeout=2.0)
        assert id2 not in st["alerts"]
        # hard kill: no clean shutdown — the queued egg is stranded in the store
        proc.kill()
        proc.wait(timeout=10)
        assert proc.returncode != 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # a fresh coopd on the same data dir recovers the stale queued row
    proc2 = start_daemon_subprocess(env)
    try:
        wait_ping(proc2, settings)
        sock = settings.socket_path
        show2 = request(sock, "runs_show", run_id=id2, timeout=2.0)["run"]
        assert show2["status"] == "cancelled"
        att = show2["attestation"]
        assert att and att["process_reaped"] and att["workspace_removed"] and att["env_cleared"]
        reason = show2["manifest"]["status_reason"]
        assert "never hatched" in reason and "startup" in reason
        assert db_row(settings, ATT_SQL, (id2,)) == (1, 1, 1)

        # alerts: the recovered egg is terminal + attested → no alert; the
        # chicken stranded mid-flight by the kill stays visible as an alert
        st = request(sock, "status", timeout=2.0)
        assert id2 not in st["alerts"]
        assert id1 in st["alerts"]

        # runs_list row carries the terminal state + reason
        row2 = [r for r in request(sock, "runs_list", timeout=2.0)["runs"] if r["id"] == id2][0]
        assert row2["status"] == "cancelled" and row2["attested"] is True
        assert "startup" in row2["status_reason"] and "never hatched" in row2["status_reason"]

        # Headcount: coopd.recovered + the attested cancel rows, no hatch/clutch
        audit = AuditLog(settings.audit_log_path).read_all()
        rec = [r for r in audit if r.get("event") == "coopd.recovered"]
        assert rec and id2 in rec[-1]["recovered"]
        cancelled = [r for r in audit if r.get("event") == "egg.cancelled" and r.get("run_id") == id2]
        assert len(cancelled) == 1 and "startup" in cancelled[0]["reason"]
        assert any(
            r.get("event") == "abattoir.attested" and r.get("run_id") == id2 and r.get("process_reaped")
            for r in audit
        )
        fin = [
            r for r in audit
            if r.get("event") == "chicken.finished" and r.get("run_id") == id2 and r.get("status") == "cancelled"
        ]
        assert fin
        assert not any(r.get("event") == "egg.hatched" and r.get("run_id") == id2 for r in audit)
        assert not any(r.get("event") == "clutch.stored" and r.get("run_id") == id2 for r in audit)

        # a clean shutdown of the second daemon (empty pecking order) exits 0
        resp = request(sock, "shutdown", timeout=5.0)
        assert resp["ok"] is True
        proc2.wait(timeout=15)
        assert proc2.returncode == 0
        audit2 = AuditLog(settings.audit_log_path).read_all()
        assert any(r.get("event") == "coopd.stopped" for r in audit2)
        # and no further recovery rows were needed
        assert len([r for r in audit2 if r.get("event") == "coopd.recovered"]) == 1
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=5)


# --- positive: a clean shutdown with an empty queue is unchanged ----------------


def test_clean_shutdown_with_empty_queue_is_unchanged(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    sock = settings.socket_path
    try:
        quick = write_job(tmp_path, name="quick-egg", prompt="Say hello")
        r = runner.invoke(app, ["run", str(quick), "--wait"])
        assert r.exit_code == 0, combined_output(r)
        rid = run_id_of(r)
        st = request(sock, "status", timeout=2.0)
        assert st["alerts"] == [] and st["queue_depth"] == 0
    finally:
        dt.stop()  # shutdown with an empty pecking order — unchanged behavior

    audit = AuditLog(settings.audit_log_path).read_all()
    assert any(e.get("event") == "coopd.started" for e in audit)
    assert any(e.get("event") == "coopd.stopped" for e in audit)
    # no egg was cancelled or recovered on this path
    assert not any(e.get("event") == "egg.cancelled" for e in audit)
    assert not any(e.get("event") == "coopd.recovered" for e in audit)
    # the finished chicken is untouched: succeeded, attested (1,1,1)
    assert db_row(settings, "SELECT status FROM runs WHERE id = ?", (rid,)) == ("succeeded",)
    assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
    assert not settings.socket_path.exists()


# --- alerts: stranded non-terminal rows are visible, a queued egg is not ---------


def test_alerts_surface_stranded_rows_but_not_a_tracked_queued_egg(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    slow = write_job(tmp_path, name="slow-alert", prompt="SLEEP 30 alert", timeout=120)
    r1 = runner.invoke(app, ["run", str(slow)])
    r2 = runner.invoke(app, ["run", str(slow)])
    assert r1.exit_code == 0 and r2.exit_code == 0
    id1, id2 = run_id_of(r1), run_id_of(r2)
    wait_status(sock, id1, ("provisioning", "running"))
    show2 = request(sock, "runs_show", run_id=id2, timeout=2.0)["run"]
    assert show2["status"] == "queued"
    # the legitimately queued egg is owned by this coopd — not an alert
    st = request(sock, "status", timeout=2.0)
    assert st["queue_depth"] == 1
    assert st["alerts"] == [] and id2 not in st["alerts"]

    # rows stranded behind the daemon's back (a dead coopd's leftovers) DO alert
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute(
            "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at) VALUES ('ghost-job2', 'ghost2', 'h', 't', ?)",
            (utcnow_iso(),),
        )
        conn.execute(
            "INSERT INTO runs (id, job_id, status, trigger, started_at) VALUES ('run-ghost-queued', 'ghost-job2', 'queued', 'manual', ?)",
            (utcnow_iso(),),
        )
        conn.execute(
            "INSERT INTO runs (id, job_id, status, trigger, started_at) VALUES ('run-ghost-running', 'ghost-job2', 'running', 'manual', ?)",
            (utcnow_iso(),),
        )
        conn.commit()
    finally:
        conn.close()
    st = request(sock, "status", timeout=2.0)
    assert "run-ghost-queued" in st["alerts"]
    assert "run-ghost-running" in st["alerts"]
    assert id1 not in st["alerts"] and id2 not in st["alerts"]

    # the real queued egg still cancels through the attested path — and then
    # it is terminal + attested, so still not an alert
    resp = request(sock, "run_stop", run_id=id2, timeout=15.0)
    assert resp["ok"] is True and resp["status"] == "cancelled"
    assert db_row(settings, ATT_SQL, (id2,)) == (1, 1, 1)
    st = request(sock, "status", timeout=2.0)
    assert id2 not in st["alerts"]
    assert "run-ghost-queued" in st["alerts"]  # the stranded rows stay visible

    # the in-flight chicken finishes normally, attested, no alert for it
    assert wait_terminal(sock, id1, timeout=40)["status"] == "succeeded"
    st = request(sock, "status", timeout=2.0)
    assert id1 not in st["alerts"]
