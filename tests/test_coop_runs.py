"""Tests for spec 14: runs from the GUI — trigger, watch, inspect results/quarantine.

Seams (per spec 11/14 testing decisions — no browser automation):
- the HTTP API against a real coop-enabled daemon booted in a thread on a temp
  port, driven with httpx exactly the way the browser does;
- socket-op parity (`runs_list` filters, `results_get` quarantine/expired) and
  CLI parity (`tc runs list/show/logs`, `tc results get` see the same run);
- a subprocess e2e (`python -m turbochickens coop start` + hermetic fake vibe
  binary) with the run triggered over HTTP and the result fetched over HTTP.

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, socket payloads, CLI output, served SPA markup, audit contents.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import _wait_socket, free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, run_id_of, wait_terminal, write_job
from test_stats_retention import backdate_run, months_ago_iso
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch):
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def auth_daemon(tmp_path, monkeypatch):
    f = write_settings(
        tmp_path,
        coop_enabled=True,
        coop_host="0.0.0.0",
        coop_port=free_port(),
        coop_auth_token="spec14-secret-token",
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


def write_strict_job(tmp_path: Path, name: str = "strict-job") -> Path:
    """A job whose schema the fake vibe output violates → quarantined."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "p.txt").write_text("Say hello")
    (d / "schemas" / "out.schema.json").write_text('{"type": "object", "required": ["no_such_field"]}')
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
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
    return d / "job.toml"


def enqueue_over_http(c: httpx.Client, spec_path: Path) -> httpx.Response:
    return c.post("/api/runs", json={"spec_path": str(spec_path.resolve())})


def wait_terminal_http(c: httpx.Client, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/runs/{run_id}")
        if r.status_code == 200 and r.json()["run"]["status"] in ("succeeded", "failed"):
            return r.json()["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal over HTTP after {timeout}s")


# --- socket op: runs_list filters + result_valid --------------------------------


def test_runs_list_socket_filters_by_job_and_status(coop_daemon, fake_vibe, tmp_path) -> None:
    sock = coop_daemon.settings.socket_path
    spec_a = write_job(tmp_path, name="job-a")
    spec_b = write_job(tmp_path, name="job-b")
    for spec in (spec_a, spec_b):
        digest = spec_hash(load_spec(spec))
        assert request(sock, "enqueue", timeout=5.0, spec_path=str(spec.resolve()), spec_hash=digest)["ok"]
    run_a = wait_terminal(sock, request(sock, "runs_list", timeout=2.0)["runs"][0]["id"])
    run_b = wait_terminal(sock, request(sock, "runs_list", timeout=2.0)["runs"][1]["id"])
    assert run_a["status"] == run_b["status"] == "succeeded"

    all_runs = request(sock, "runs_list", timeout=2.0)["runs"]
    assert len(all_runs) == 2
    assert all(r["result_valid"] is True for r in all_runs)  # spec 14: quarantine flag on rows

    by_job = request(sock, "runs_list", job="job-a", timeout=2.0)["runs"]
    assert len(by_job) == 1 and by_job[0]["job_name"] == "job-a"
    by_status = request(sock, "runs_list", status="succeeded", timeout=2.0)["runs"]
    assert len(by_status) == 2
    assert request(sock, "runs_list", status="failed", timeout=2.0)["runs"] == []
    assert request(sock, "runs_list", job="no-such-job", timeout=2.0)["runs"] == []
    both = request(sock, "runs_list", job="job-b", status="succeeded", timeout=2.0)["runs"]
    assert len(both) == 1 and both[0]["job_name"] == "job-b"
    both_bad = request(sock, "runs_list", job="job-b", status="failed", timeout=2.0)["runs"]
    assert both_bad == []


# --- POST /api/runs: enqueue over HTTP, parity with CLI + socket -----------------


def test_post_api_runs_enqueues_manual_run_seen_by_cli_and_socket(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="http-run-job")

    r = enqueue_over_http(c, spec_path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["op"] == "enqueue" and body["status"] == "queued"
    run_id = body["run_id"]
    assert body["job_id"] == "http-run-job" and body["weight"] == 1.0

    # CLI parity: the same run appears in `tc runs list` with trigger=manual
    cli = runner.invoke(app, ["runs", "list"])
    assert cli.exit_code == 0, combined_output(cli)
    assert run_id in cli.output and "http-run-job" in cli.output and "trigger=manual" in cli.output

    # and it flows through the normal pipeline to a terminal state
    run = wait_terminal_http(c, run_id)
    assert run["status"] == "succeeded" and run["trigger"] == "manual"

    # the HTTP list mirrors the socket op exactly, filters included
    http_list = c.get("/api/runs").json()
    assert http_list == request(sock, "runs_list", timeout=2.0)
    filt = c.get("/api/runs?job=http-run-job&status=succeeded")
    assert filt.status_code == 200
    assert filt.json() == request(sock, "runs_list", job="http-run-job", status="succeeded", timeout=2.0)
    assert len(filt.json()["runs"]) == 1 and filt.json()["runs"][0]["id"] == run_id
    assert c.get("/api/runs?status=failed").json()["runs"] == []

    # detail over HTTP == socket runs_show; manifest carries what the spec asks for
    show = c.get(f"/api/runs/{run_id}")
    assert show.status_code == 200
    assert show.json() == request(sock, "runs_show", run_id=run_id, timeout=2.0)
    manifest = show.json()["run"]["manifest"]
    assert manifest["spec_hash"] == f"sha256:{spec_hash(load_spec(spec_path))}"
    assert manifest["harness"]["adapter"] == "vibe-cli"
    assert manifest["harness"]["model"] == "fake-model"
    assert manifest["harness"]["temperature"] == 0.0
    assert manifest["budget"]["outcome"] == "ok" and manifest["budget"]["timeout_sec"] == 30
    assert manifest["usage"]["tokens_in"] == 12 and manifest["usage"]["tokens_out"] == 34
    assert show.json()["run"]["attestation"] == {
        "process_reaped": True,
        "workspace_removed": True,
        "env_cleared": True,
        "verified_at": show.json()["run"]["attestation"]["verified_at"],
    }
    assert show.json()["run"]["result"]["valid"] is True

    # logs over HTTP == socket runs_logs; streamed + sanitized
    logs = c.get(f"/api/runs/{run_id}/logs")
    assert logs.status_code == 200
    assert logs.json() == request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    events = logs.json()["events"]
    assert any(e["source"] == "harness" and "fake harness log" in e["line"] for e in events)
    assert all("\x07" not in e["line"] and "\x1b" not in e["line"] for e in events)

    # results over HTTP == socket results_get == `tc results get`
    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 200
    res_body = res.json()
    assert res_body["ok"] is True and res_body["valid"] is True
    assert res_body["output"]["message"] == "hello from fake vibe"
    assert res_body == request(sock, "results_get", run_id=run_id, timeout=2.0)
    cli_get = runner.invoke(app, ["results", "get", run_id])
    assert cli_get.exit_code == 0, combined_output(cli_get)
    assert json.loads(cli_get.output) == res_body["output"]

    # the enqueue was audited like any manual run
    events_audit = [e["event"] for e in AuditLog(coop_daemon.settings.audit_log_path).read_all()]
    assert "egg.queued" in events_audit and "clutch.stored" in events_audit


def test_post_api_runs_rejects_bad_requests(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    assert c.post("/api/runs", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/runs", json={}).status_code == 400
    assert c.post("/api/runs", json={"spec_path": "  "}).status_code == 400
    assert c.post("/api/runs", json={"spec_path": 42}).status_code == 400

    r = c.post("/api/runs", json={"spec_path": "/no/such/job.toml"})
    assert r.status_code == 422
    assert r.json()["ok"] is False and "egg failed candling" in r.json()["error"]

    # unknown resources are 404s, not broken pages
    assert c.get("/api/runs/run-nope").status_code == 404
    assert c.get("/api/runs/run-nope/logs").status_code == 404
    assert c.get("/api/results/run-nope").status_code == 404


def test_results_endpoint_404_while_run_has_no_result_yet(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    spec_path = write_job(tmp_path, name="sleepy-job", prompt="SLEEP 5")
    run_id = enqueue_over_http(c, spec_path).json()["run_id"]
    # the run is queued/running (fake vibe sleeps 5 s) and never reached collecting
    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 404
    assert res.json()["ok"] is False and "no clutch in the nest box" in res.json()["error"]
    wait_terminal_http(c, run_id, timeout=30.0)  # let it finish so the daemon stops cleanly


# --- quarantine over HTTP ----------------------------------------------------------


def test_quarantined_run_over_http_never_a_valid_result(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    spec_path = write_strict_job(tmp_path)

    r = enqueue_over_http(c, spec_path)
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    run = wait_terminal_http(c, run_id)
    assert run["status"] == "failed"
    assert run["result"]["valid"] is False

    # the runs list row carries result_valid=false (the quarantined egg-dot source)
    row = [x for x in c.get("/api/runs").json()["runs"] if x["id"] == run_id][0]
    assert row["result_valid"] is False and row["status"] == "failed"
    assert len(c.get("/api/runs?status=failed").json()["runs"]) == 1

    # the results endpoint returns quarantine + schema errors + raw output at 200
    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False and body["quarantined"] is True
    assert body["schema_errors"] and any("no_such_field" in e for e in body["schema_errors"])
    assert "hello from fake vibe" in body["raw_output"]  # raw output inspectable, never valid
    # socket parity: results_get carries the same raw_output
    sock_body = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert sock_body["quarantined"] is True and sock_body["raw_output"] == body["raw_output"]

    # CLI parity: tc results get exits 1, never prints the payload as a result
    cli = runner.invoke(app, ["results", "get", run_id])
    assert cli.exit_code == 1
    assert "quarantined" in combined_output(cli)
    assert "no_such_field" in combined_output(cli)


# --- expired artifacts over HTTP ----------------------------------------------------


def test_expired_result_over_http_renders_expired_state(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    spec_path = write_job(tmp_path, name="expiring-job")
    run_id = enqueue_over_http(c, spec_path).json()["run_id"]
    run = wait_terminal_http(c, run_id)
    assert run["status"] == "succeeded"
    assert c.get(f"/api/results/{run_id}").json()["ok"] is True

    backdate_run(coop_daemon.settings, run_id, months_ago_iso(7))
    sweep = request(sock, "retention_sweep", timeout=5.0)
    assert sweep["ok"] is True and sweep["deleted"] == 1

    res = c.get(f"/api/results/{run_id}")
    assert res.status_code == 200  # structured state, not an error page
    body = res.json()
    assert body["ok"] is False and body["expired"] is True and body["run_id"] == run_id
    assert "output" not in body and "raw_output" not in body  # the payload is not served
    cli = runner.invoke(app, ["results", "get", run_id])
    assert cli.exit_code == 1 and "composted" in combined_output(cli)


# --- auth required --------------------------------------------------------------------


def test_runs_endpoints_require_bearer_when_auth_required(auth_daemon, tmp_path) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": "Bearer spec14-secret-token"}
    spec_path = tmp_path / "job.toml"
    spec_path.write_text("")  # path need not exist for the 401 check
    for method, path in (
        ("get", "/api/runs"),
        ("get", "/api/runs/run-x"),
        ("get", "/api/runs/run-x/logs"),
        ("get", "/api/results/run-x"),
    ):
        assert getattr(c, method)(path).status_code == 401, path
        assert getattr(c, method)(path, headers=headers).status_code in (200, 404), path
    assert c.post("/api/runs", json={"spec_path": str(spec_path)}).status_code == 401
    r = c.post("/api/runs", json={"spec_path": str(spec_path)}, headers=headers)
    assert r.status_code == 422 and r.json()["ok"] is False  # path does not exist → daemon refused
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open


# --- daemon-down payloads through the transport seam -----------------------------------


def test_runs_endpoints_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    port = settings.coop_port
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> dict:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
                out = {}
                for path in ("/api/runs", "/api/runs/run-x", "/api/runs/run-x/logs", "/api/results/run-x"):
                    r = await c.get(path)
                    out[path] = (r.status_code, r.json())
                post = await c.post("/api/runs", json={"spec_path": "/data/x/job.toml"})
                out["POST /api/runs"] = (post.status_code, post.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for path in ("/api/runs", "/api/runs/run-x", "/api/runs/run-x/logs", "/api/results/run-x"):
        code, body = out[path]
        assert code == 200, path  # structured payload, not a broken 500
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], path
    code, body = out["POST /api/runs"]
    assert code == 200 and body["ok"] is False and body["daemon"]["up"] is False


# --- SPA serves the runs view -----------------------------------------------------------


def test_spa_serves_runs_view(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-runs"', 'id="run-form"', 'id="run-spec-path"', 'id="run-error"',
        'id="runs-job-chips"', 'id="runs-status-chips"', 'id="runs-tbody"', 'id="runs-card"', 'id="runs-empty"',
        'id="run-detail"', 'id="detail-dot"', 'id="detail-close"',
        'id="tab-manifest"', 'id="tab-logs"', 'id="tab-result"',
        'id="pane-manifest"', 'id="pane-logs"', 'id="pane-result"',
        'id="manifest-pre"', 'id="run-log"', 'id="result-pre"', 'id="result-state"',
        "Queue egg",
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    for api in ('"/api/runs"', '"/api/jobs"', '"/api/runs/"', '"/api/results/"', '"/logs"'):
        assert api in js, api
    assert "loadRuns" in js and "apiPost" in js and "flash" in js
    assert 'method: "POST"' in js
    css = c.get("/styles.css").text
    for cls in (".dot-queued", ".dot-running", ".dot-succeeded", ".dot-failed", ".dot-quarantined", ".run-row", ".detail-card", ".tab", ".data-pane", ".run-form", ".quarantine-note"):
        assert cls in css, cls


# --- subprocess e2e: run triggered over HTTP, result fetched over HTTP -------------------


def test_e2e_run_triggered_over_http_reaches_succeeded_and_serves_result(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port, coop_loopback_open=True)  # `tc coop start` forces coop on; the opt-in keeps the loopback bind open
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    settings = load_settings(f)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
        assert c.get("/api/ping").status_code == 200

        spec_path = write_job(tmp_path, name="e2e-http-job")
        r = c.post("/api/runs", json={"spec_path": str(spec_path.resolve())})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        run = wait_terminal_http(c, run_id, timeout=30.0)
        assert run["status"] == "succeeded" and run["trigger"] == "manual"
        assert run["manifest"]["harness"]["adapter"] == "vibe-cli"
        assert run["manifest"]["budget"]["outcome"] == "ok"

        res = c.get(f"/api/results/{run_id}")
        assert res.status_code == 200 and res.json()["ok"] is True
        assert res.json()["output"]["message"] == "hello from fake vibe"

        logs = c.get(f"/api/runs/{run_id}/logs")
        assert logs.status_code == 200 and any("fake harness log" in e["line"] for e in logs.json()["events"])

        # CLI parity against the same daemon
        cli = runner.invoke(app, ["runs", "list"])
        assert cli.exit_code == 0, combined_output(cli)
        assert run_id in cli.output and "trigger=manual" in cli.output
        cli_get = runner.invoke(app, ["results", "get", run_id])
        assert cli_get.exit_code == 0 and json.loads(cli_get.output) == res.json()["output"]

        # the SPA serves the runs view
        page = c.get("/")
        assert page.status_code == 200 and 'id="view-runs"' in page.text and 'id="runs-tbody"' in page.text

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
