"""Tests for spec 16: schedules from the GUI.

Seams (per spec 11/16 testing decisions — no browser automation):
- the HTTP API against a real coop-enabled daemon booted in a thread on a temp
  port, driven with httpx exactly the way the browser does: GET /api/schedule,
  POST /api/schedule (by spec path or by files bundle), DELETE
  /api/schedule/{job_id} — with socket parity (schedule_list/schedule_add/
  schedule_rm) and CLI parity (`tc schedule add/list/rm`);
- one real cron firing: a schedule added over HTTP fires a trigger=cron run
  through the normal pipeline (next-minute cron, audit shows schedule.fired);
- rejections surface as clear inline errors (422 with the socket error):
  manual-trigger spec, invalid cron, spec-hash mismatch, malformed body (400);
- auth-required 401s, dead-socket daemon-down payloads (never 500), served SPA
  markup, and a subprocess e2e (`tc coop start` + fake vibe binary).

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, socket payloads, CLI output, DB rows, audit contents, served assets.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import _wait_socket, free_port, http_client, write_settings
from test_coop_runs import auth_daemon, coop_daemon, fake_vibe, wait_terminal_http
from test_coop_upload import bundle_of
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, wait_terminal
from test_schedule import next_minute_cron, write_cron_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()

AUTH_HEADERS = {"Authorization": "Bearer spec14-secret-token"}


def db_row(settings, sql: str, params=()) -> tuple | None:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# --- add over HTTP → socket parity, CLI parity, DB, audit, status ---------------


def test_schedule_add_over_http_appears_everywhere_and_readd_replaces(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    settings = coop_daemon.settings
    spec_path = write_cron_job(tmp_path, name="gui-cron", schedule="0 7 * * 1")
    digest = spec_hash(load_spec(spec_path))

    r = c.post("/api/schedule", json={"spec_path": str(spec_path.resolve()), "spec_hash": digest})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["job_id"] == "gui-cron"
    assert body["schedule"] == "0 7 * * 1" and body["spec_hash"] == digest
    assert body["next_run"] is not None  # live next run from APScheduler

    # HTTP == socket parity for the list
    http_list = c.get("/api/schedule")
    assert http_list.status_code == 200
    sock_list = request(sock, "schedule_list", timeout=2.0)
    assert http_list.json() == sock_list
    assert len(sock_list["schedules"]) == 1
    s = sock_list["schedules"][0]
    assert s["job_id"] == "gui-cron" and s["schedule"] == "0 7 * * 1"
    assert s["spec_hash"] == digest and s["next_run"] is not None and s["added_at"]

    # CLI parity: `tc schedule list` shows it with its next_run
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0, combined_output(result)
    assert "gui-cron" in result.output and "0 7 * * 1" in result.output and "next_run=" in result.output

    # DB row, audit row, status count, and the job is registered with its path
    row = db_row(settings, "SELECT spec_path, schedule, spec_hash FROM schedules WHERE job_id = 'gui-cron'")
    assert row is not None and row[0] == str(spec_path.resolve()) and row[1] == "0 7 * * 1"
    events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
    assert "schedule.added" in events
    assert request(sock, "status", timeout=2.0)["schedules"] == 1
    jobs = request(sock, "jobs_list", timeout=2.0)["jobs"]
    assert jobs[0]["job_id"] == "gui-cron" and jobs[0]["spec_path"] == str(spec_path.resolve())
    assert c.get("/api/jobs").json()["jobs"][0]["spec_path"] == str(spec_path.resolve())

    # re-adding an edited spec replaces the schedule (still one row, new cron)
    edited = write_cron_job(tmp_path, name="gui-cron", schedule="30 8 * * 2")
    r = c.post("/api/schedule", json={"spec_path": str(edited.resolve()), "spec_hash": spec_hash(load_spec(edited))})
    assert r.status_code == 200 and r.json()["schedule"] == "30 8 * * 2"
    schedules = c.get("/api/schedule").json()["schedules"]
    assert len(schedules) == 1 and schedules[0]["schedule"] == "30 8 * * 2"
    result = runner.invoke(app, ["schedule", "list"])
    assert "30 8 * * 2" in result.output


# --- remove over HTTP → CLI parity, audit, 404 on unknown -----------------------


def test_schedule_rm_over_http_removes_it_everywhere(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    settings = coop_daemon.settings
    spec_path = write_cron_job(tmp_path, name="rm-job", schedule="0 7 * * 1")
    r = c.post("/api/schedule", json={"spec_path": str(spec_path.resolve())})
    assert r.status_code == 200, r.text
    assert len(c.get("/api/schedule").json()["schedules"]) == 1

    r = c.delete("/api/schedule/rm-job")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["job_id"] == "rm-job"
    # HTTP == socket parity after removal
    assert c.get("/api/schedule").json() == request(sock, "schedule_list", timeout=2.0)
    assert c.get("/api/schedule").json()["schedules"] == []
    assert request(sock, "status", timeout=2.0)["schedules"] == 0
    assert db_row(settings, "SELECT * FROM schedules WHERE job_id = 'rm-job'") is None
    events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
    assert "schedule.removed" in events
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0 and "no schedules" in result.output

    # removing an unknown schedule is a 404 carrying the socket error
    r = c.delete("/api/schedule/rm-job")
    assert r.status_code == 404
    assert r.json()["ok"] is False and "no schedule registered" in r.json()["error"]


# --- the acceptance behavior: an HTTP-added schedule fires a trigger=cron run ---


def test_http_added_schedule_fires_cron_run_through_the_pipeline(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    settings = coop_daemon.settings
    expr = next_minute_cron()
    spec_path = write_cron_job(tmp_path, name="firing-gui", schedule=expr)

    r = c.post("/api/schedule", json={"spec_path": str(spec_path.resolve())})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["next_run"] is not None

    # wait for the cron to fire: a trigger=cron run appears
    deadline = time.time() + 90
    cron_run = None
    while time.time() < deadline:
        runs = c.get("/api/runs").json()["runs"]
        cron_runs = [x for x in runs if x["trigger"] == "cron"]
        if cron_runs:
            cron_run = cron_runs[0]
            break
        time.sleep(0.5)
    assert cron_run is not None, "HTTP-added cron schedule did not fire within 90s"
    run_id = cron_run["id"]

    # it flows through the identical pipeline to a succeeded, attested run
    run = wait_terminal_http(c, run_id, timeout=30.0)
    assert run["status"] == "succeeded" and run["trigger"] == "cron"
    assert run["manifest"]["trigger"] == "cron"
    assert run["attestation"] is not None
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()

    # audit shows the fire + the queued run with trigger=cron; result over HTTP
    audit = AuditLog(settings.audit_log_path).read_all()
    fired = [e for e in audit if e["event"] == "schedule.fired"]
    assert fired and fired[0]["run_id"] == run_id and fired[0]["trigger"] == "cron"
    queued = [e for e in audit if e["event"] == "run.queued" and e.get("run_id") == run_id]
    assert queued and queued[0]["trigger"] == "cron"
    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert res.json()["output"]["message"] == "hello from fake vibe"

    # CLI sees the same cron run
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0 and "trigger=cron" in result.output and "firing-gui" in result.output


# --- rejections come back as clear inline errors (422) / malformed (400) ---------


def test_schedule_add_rejections_are_clear_errors(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path

    # manual-trigger spec → refused, nothing scheduled
    manual = write_cron_job(tmp_path, name="manual-job", trigger_type="manual")
    r = c.post("/api/schedule", json={"spec_path": str(manual.resolve())})
    assert r.status_code == 422
    assert r.json()["ok"] is False and "only cron-triggered jobs" in r.json()["error"]

    # invalid cron → field-named error like `tc jobs validate`
    bad = write_cron_job(tmp_path, name="bad-cron", schedule="not a cron")
    r = c.post("/api/schedule", json={"spec_path": str(bad.resolve())})
    assert r.status_code == 422 and "trigger.schedule" in r.json()["error"]

    # spec-hash mismatch → refused
    good = write_cron_job(tmp_path, name="hash-job", schedule="0 7 * * 1")
    r = c.post("/api/schedule", json={"spec_path": str(good.resolve()), "spec_hash": "deadbeef"})
    assert r.status_code == 422 and "spec hash mismatch" in r.json()["error"]

    # unreadable spec path → refused (not a 500)
    r = c.post("/api/schedule", json={"spec_path": str(tmp_path / "nope" / "job.toml")})
    assert r.status_code == 422 and r.json()["ok"] is False and "cannot read spec file" in r.json()["error"]

    # malformed bodies → 400
    assert c.post("/api/schedule", json={}).status_code == 400
    assert c.post("/api/schedule", json={"spec_path": 42}).status_code == 400
    assert c.post("/api/schedule", json={"spec_path": "   "}).status_code == 400
    r = c.post("/api/schedule", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400

    # none of the rejected adds registered anything
    assert c.get("/api/schedule").json()["schedules"] == []
    assert request(sock, "schedule_list", timeout=2.0)["schedules"] == []
    events = [e["event"] for e in AuditLog(coop_daemon.settings.audit_log_path).read_all()]
    assert "schedule.added" not in events


def test_schedule_add_accepts_files_bundle_in_one_call(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    spec_path = write_cron_job(tmp_path, name="bundle-cron", schedule="0 7 * * 1")
    files = bundle_of(spec_path)

    # bundle → stored + validated + scheduled in one POST
    r = c.post("/api/schedule", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["job_id"] == "bundle-cron" and body["schedule"] == "0 7 * * 1"
    schedules = c.get("/api/schedule").json()["schedules"]
    assert len(schedules) == 1 and "bundle-cron-" in schedules[0]["spec_path"]
    result = runner.invoke(app, ["schedule", "list"])
    assert "bundle-cron" in result.output

    # a manual-trigger bundle uploads+validates but is refused at schedule_add → 422 inline error
    manual = write_cron_job(tmp_path, name="bundle-manual", trigger_type="manual")
    r = c.post("/api/schedule", json={"files": bundle_of(manual)})
    assert r.status_code == 422
    assert r.json()["ok"] is False and "only cron-triggered jobs" in r.json()["error"]
    assert str(tmp_path) not in r.text
    assert len(c.get("/api/schedule").json()["schedules"]) == 1  # unchanged

    # an invalid bundle (bad cron) → 422 with field-named errors, nothing scheduled
    bad = write_cron_job(tmp_path, name="bundle-bad-cron", schedule="not a cron")
    r = c.post("/api/schedule", json={"files": bundle_of(bad)})
    assert r.status_code == 422
    assert r.json()["valid"] is False and "trigger.schedule" in [e["field"] for e in r.json()["errors"]]
    assert str(tmp_path) not in json.dumps(r.json()["errors"])
    assert len(c.get("/api/schedule").json()["schedules"]) == 1  # unchanged


# --- auth required + daemon-down ---------------------------------------------------


def test_schedule_endpoints_require_bearer_when_auth_required(auth_daemon, tmp_path) -> None:
    c = http_client(auth_daemon.settings)
    spec_path = write_cron_job(tmp_path, name="auth-cron", schedule="0 7 * * 1")

    assert c.get("/api/schedule").status_code == 401
    assert c.post("/api/schedule", json={"spec_path": str(spec_path.resolve())}).status_code == 401
    assert c.delete("/api/schedule/auth-cron").status_code == 401
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open

    r = c.get("/api/schedule", headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["schedules"] == []
    r = c.post("/api/schedule", json={"spec_path": str(spec_path.resolve())}, headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["next_run"] is not None
    r = c.delete("/api/schedule/auth-cron", headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/schedule", headers=AUTH_HEADERS).json()["schedules"] == []


def test_schedule_endpoints_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    port = settings.coop_port
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> dict:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
                out = {}
                r = await c.get("/api/schedule")
                out["list"] = (r.status_code, r.json())
                r = await c.post("/api/schedule", json={"spec_path": "/x/job.toml"})
                out["add"] = (r.status_code, r.json())
                r = await c.delete("/api/schedule/some-job")
                out["rm"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key in ("list", "add", "rm"):
        code, body = out[key]
        assert code == 200, key  # structured payload, never a broken 500
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- SPA serves the Schedules view ---------------------------------------------------


def test_spa_serves_schedules_view_and_no_host_paths(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-schedules"', 'id="sched-form"', 'id="sched-spec"', 'id="sched-submit"',
        ">Add schedule</button>", 'id="sched-error"', 'id="schedules-card"', 'id="schedules-tbody"',
        'id="schedules-empty"', "Next run", "Cron", 'for="sched-spec"',
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    assert '"/api/schedule"' in js and "apiDelete" in js and "loadSchedules" in js
    assert "schedules: loadSchedules" in js and "removeSchedule" in js
    css = c.get("/styles.css").text
    for cls in (".sched-form", ".sched-remove", ".visually-hidden"):
        assert cls in css, cls
    # no host paths ever enter the UI (served assets are static)
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: add/list/remove a schedule entirely over HTTP --------------------


def test_e2e_schedule_add_list_rm_over_http_subprocess(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)  # `tc coop start` forces coop on
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    settings = load_settings(f)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        assert c.get("/api/ping").status_code == 200

        # 1. add a cron schedule over HTTP (a registered-style in-container path)
        spec_path = write_cron_job(tmp_path, name="e2e-cron", schedule="0 7 * * 1")
        r = c.post("/api/schedule", json={"spec_path": str(spec_path.resolve()), "spec_hash": spec_hash(load_spec(spec_path))})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["job_id"] == "e2e-cron" and body["next_run"] is not None

        # 2. it shows in GET /api/schedule with the live next_run, and in `tc schedule list`
        schedules = c.get("/api/schedule").json()["schedules"]
        assert len(schedules) == 1 and schedules[0]["job_id"] == "e2e-cron"
        assert schedules[0]["schedule"] == "0 7 * * 1" and schedules[0]["next_run"] is not None
        cli = runner.invoke(app, ["schedule", "list"])
        assert cli.exit_code == 0 and "e2e-cron" in cli.output and "0 7 * * 1" in cli.output
        assert request(settings.socket_path, "schedule_list", timeout=2.0) == c.get("/api/schedule").json()

        # 3. a manual spec is refused with a clear inline error over HTTP
        manual = write_cron_job(tmp_path, name="e2e-manual", trigger_type="manual")
        r = c.post("/api/schedule", json={"spec_path": str(manual.resolve())})
        assert r.status_code == 422 and "only cron-triggered jobs" in r.json()["error"]

        # 4. remove over HTTP; CLI shows none afterwards
        r = c.delete("/api/schedule/e2e-cron")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert c.get("/api/schedule").json()["schedules"] == []
        cli = runner.invoke(app, ["schedule", "list"])
        assert cli.exit_code == 0 and "no schedules" in cli.output
        events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
        assert "schedule.added" in events and "schedule.removed" in events

        # 5. the SPA serves the Schedules view with the add form
        page = c.get("/")
        assert page.status_code == 200 and 'id="sched-form"' in page.text and 'id="schedules-tbody"' in page.text
        for asset in (page.text, c.get("/app.js").text, c.get("/styles.css").text):
            assert str(tmp_path) not in asset

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
