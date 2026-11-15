"""Tests for spec 17: stats + retention from the GUI.

Seams (per spec 11/17 testing decisions — no browser automation):
- the HTTP API against a real coop-enabled daemon booted in a thread on a temp
  port, driven with httpx exactly the way the browser does: GET /api/stats,
  GET /api/stats/{job_id}, POST /api/retention/sweep — with socket parity
  (stats_list / stats_show / retention_sweep) and CLI parity
  (`tc stats list/show`, `tc results get`);
- runs triggered over HTTP (POST /api/runs + fake vibe binary) roll up into
  the stats the Stats view renders;
- a sweep over HTTP with backdated runs deletes the expired artifacts
  (result and quarantine alike), audit-logs each deletion as clutch.composted,
  and the swept run then reads back as expired over /api/results/{run_id};
- auth-required 401s, dead-socket daemon-down payloads (never 500), served
  SPA markup, and a subprocess e2e (`tc coop start` + fake vibe).

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, socket payloads, CLI output, DB rows, audit contents, served assets.
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
from test_coop_runs import auth_daemon, coop_daemon, enqueue_over_http, fake_vibe, wait_terminal_http, write_strict_job
from test_run_e2e import FAKE_VIBE, combined_output, write_job
from test_stats_retention import backdate_run, months_ago_iso
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()

AUTH_HEADERS = {"Authorization": "Bearer spec14-secret-token"}  # auth_daemon's configured token


# --- stats endpoints: HTTP == socket parity + CLI parity after HTTP-triggered runs ---


def test_stats_endpoints_mirror_socket_and_cli_after_http_run(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path

    # empty at first, over HTTP and over the socket alike
    assert c.get("/api/stats").json()["stats"] == []
    assert request(sock, "stats_list", timeout=2.0)["stats"] == []

    # run a job over HTTP (the Stats view renders jobs whose runs came over HTTP)
    spec_path = write_job(tmp_path, name="http-stats-job")
    run_id = enqueue_over_http(c, spec_path).json()["run_id"]
    assert wait_terminal_http(c, run_id)["status"] == "succeeded"

    # GET /api/stats == socket stats_list
    http_list = c.get("/api/stats")
    assert http_list.status_code == 200
    sock_list = request(sock, "stats_list", timeout=2.0)
    assert http_list.json() == sock_list
    assert len(sock_list["stats"]) == 1
    s = sock_list["stats"][0]
    assert s["job_id"] == "http-stats-job" and s["job_name"] == "http-stats-job"
    assert s["n_runs"] == 1
    assert s["tokens_in_mean"] == s["tokens_in_p50"] == s["tokens_in_p95"] == 12  # fake vibe export.json
    assert s["tokens_out_mean"] == s["tokens_out_p50"] == s["tokens_out_p95"] == 34
    assert s["tokens_p95"] == 46
    assert s["expected_cost_usd"] == pytest.approx(0.001)  # harness cost fallback (vibe-cli model)
    assert s["updated_at"]

    # GET /api/stats/{job} == socket stats_show
    http_show = c.get("/api/stats/http-stats-job")
    assert http_show.status_code == 200
    sock_show = request(sock, "stats_show", job_id="http-stats-job", timeout=2.0)
    assert http_show.json() == sock_show
    assert http_show.json()["stats"]["n_runs"] == 1

    # unknown job → 404 (not a broken page), socket agrees it is an error
    r = c.get("/api/stats/no-such-job")
    assert r.status_code == 404
    assert r.json()["ok"] is False and "no feed recorded" in r.json()["error"]
    assert request(sock, "stats_show", job_id="no-such-job", timeout=2.0)["ok"] is False

    # CLI parity: `tc stats list` / `tc stats show` print the same numbers
    lst = runner.invoke(app, ["stats", "list"])
    assert lst.exit_code == 0, combined_output(lst)
    assert "http-stats-job" in lst.output and "n=1" in lst.output and "tokens_total_p95=46" in lst.output
    show = runner.invoke(app, ["stats", "show", "http-stats-job"])
    assert show.exit_code == 0, combined_output(show)
    assert "n_runs: 1" in show.output and "tokens_total_p95: 46" in show.output
    assert "expected_cost_usd_per_run" in show.output


def test_stats_list_rolls_up_multiple_http_runs(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    spec_path = write_job(tmp_path, name="multi-stats-job")
    for _ in range(3):
        run_id = enqueue_over_http(c, spec_path).json()["run_id"]
        assert wait_terminal_http(c, run_id)["status"] == "succeeded"
    s = c.get("/api/stats").json()["stats"][0]
    assert s["n_runs"] == 3
    assert s["tokens_in_p95"] == 12 and s["tokens_out_p95"] == 34 and s["tokens_p95"] == 46
    lst = runner.invoke(app, ["stats", "list"])
    assert "n=3" in lst.output


# --- retention sweep over HTTP ---------------------------------------------------------


def test_retention_sweep_over_http_expires_backdated_runs(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    settings = coop_daemon.settings

    # one valid result + one quarantined result, both triggered over HTTP
    good = write_job(tmp_path, name="sweep-good")
    strict = write_strict_job(tmp_path, name="sweep-strict")
    run_ok = enqueue_over_http(c, good).json()["run_id"]
    run_bad = enqueue_over_http(c, strict).json()["run_id"]
    assert wait_terminal_http(c, run_ok)["status"] == "succeeded"
    assert wait_terminal_http(c, run_bad)["status"] == "failed"
    assert c.get(f"/api/results/{run_ok}").json()["ok"] is True
    assert c.get(f"/api/results/{run_bad}").json()["quarantined"] is True
    ok_dir = settings.results_dir / run_ok
    bad_dir = settings.quarantine_dir / run_bad
    assert ok_dir.exists() and bad_dir.exists()

    # backdate both past the 6-month TTL
    backdate_run(settings, run_ok, months_ago_iso(7))
    backdate_run(settings, run_bad, months_ago_iso(7))

    # sweep over HTTP
    r = c.post("/api/retention/sweep", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["op"] == "retention_sweep"
    assert body["results_retention_months"] == 6 and body["audit_retention"] == "forever"
    assert body["deleted"] == 2
    expired = {e["run_id"]: e for e in body["expired"]}
    assert expired[run_ok]["kind"] == "result" and expired[run_bad]["kind"] == "quarantine"
    assert body["cutoff"]

    # status carries the last sweep outcome (what the Dashboard lists)
    status = request(sock, "status", timeout=2.0)
    assert status["retention"]["last_sweep"]["deleted"] == 2
    assert {e["run_id"] for e in status["retention"]["last_sweep"]["expired"]} == {run_ok, run_bad}
    assert c.get("/api/status").json()["retention"]["last_sweep"]["deleted"] == 2

    # socket parity for the same op, and a second sweep is a no-op (idempotent)
    sock_sweep = request(sock, "retention_sweep", timeout=5.0)
    assert sock_sweep["ok"] is True and sock_sweep["deleted"] == 0
    again = c.post("/api/retention/sweep", json={})
    assert again.status_code == 200 and again.json()["deleted"] == 0

    # artifacts are gone; each deletion audit-logged as clutch.composted; the headcount itself is untouched
    assert not ok_dir.exists() and not bad_dir.exists()
    events = AuditLog(settings.audit_log_path).read_all()
    expired_events = [e for e in events if e["event"] == "clutch.composted"]
    assert {e["run_id"] for e in expired_events} == {run_ok, run_bad}
    assert all(e["retention_months"] == 6 for e in expired_events)
    # the append-only results index rows remain
    import sqlite3

    conn = sqlite3.connect(str(settings.db_path))
    try:
        rows = conn.execute("SELECT run_id, valid FROM results WHERE run_id IN (?, ?)", (run_ok, run_bad)).fetchall()
    finally:
        conn.close()
    assert sorted(rows) == sorted([(run_ok, 1), (run_bad, 0)])

    # the swept runs read back as expired over HTTP (the Runs/Results expired state) — payload never served
    for run_id in (run_ok, run_bad):
        res = c.get(f"/api/results/{run_id}")
        assert res.status_code == 200
        rb = res.json()
        assert rb["ok"] is False and rb["expired"] is True and rb["run_id"] == run_id
        assert "output" not in rb and "raw_output" not in rb
    cli = runner.invoke(app, ["results", "get", run_ok])
    assert cli.exit_code == 1 and "composted" in combined_output(cli)

    # stats are unaffected by expiry (the rollup lives in the DB, not the artifacts)
    stats = c.get("/api/stats").json()["stats"]
    assert {s["job_id"] for s in stats} == {"sweep-good", "sweep-strict"}


# --- auth required + daemon-down ---------------------------------------------------------


def test_stats_and_sweep_endpoints_require_bearer_when_auth_required(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    assert c.get("/api/stats").status_code == 401
    assert c.get("/api/stats/some-job").status_code == 401
    assert c.post("/api/retention/sweep", json={}).status_code == 401
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open

    assert c.get("/api/stats", headers=AUTH_HEADERS).status_code == 200
    r = c.get("/api/stats/some-job", headers=AUTH_HEADERS)
    assert r.status_code == 404 and "no feed recorded" in r.json()["error"]
    r = c.post("/api/retention/sweep", json={}, headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["deleted"] == 0


def test_stats_and_sweep_endpoints_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
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
                r = await c.get("/api/stats")
                out["list"] = (r.status_code, r.json())
                r = await c.get("/api/stats/some-job")
                out["show"] = (r.status_code, r.json())
                r = await c.post("/api/retention/sweep", json={})
                out["sweep"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key in ("list", "show", "sweep"):
        code, body = out[key]
        assert code == 200, key  # structured payload, never a broken 500 (POST included)
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- SPA serves the Stats view + the Dashboard retention card ----------------------------


def test_spa_serves_stats_view_and_retention_card(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-stats"', 'id="stats-grid"', 'id="stats-empty"', 'id="stats-detail"',
        'id="stats-detail-list"', 'id="stats-detail-close"', ">Feed</a>",
        'id="retention-card"', 'id="retention-summary"', 'id="sweep-now"', ">Turn the compost</button>",
        'id="sweep-error"', 'id="sweep-outcome"', 'id="sweep-summary"', 'id="sweep-list"',
        'data-stat="retention"',
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    assert '"/api/stats"' in js and '"/api/stats/"' in js and '"/api/retention/sweep"' in js
    assert "stats: loadStats" in js and "loadStats" in js and "loadStatsDetail" in js
    assert '"#sweep-now"' in js and "renderSweepOutcome" in js and "renderRetentionCard" in js
    css = c.get("/styles.css").text
    for cls in (".stats-grid", ".stat-card", ".stat-numerals", ".num-value", ".retention-card", ".retention-head", ".sweep-outcome", ".sweep-list"):
        assert cls in css, cls
    # no host paths ever enter the UI (served assets are static)
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: run + stats + sweep entirely over HTTP ------------------------------


def test_e2e_stats_and_retention_sweep_over_http_subprocess(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port, coop_loopback_open=True)  # `tc coop start` forces coop on; the opt-in keeps the loopback bind open
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

        # 1. run a job over HTTP → it rolls up into GET /api/stats (and `tc stats list`)
        spec_path = write_job(tmp_path, name="e2e-stats", prompt="Say hello")
        r = c.post("/api/runs", json={"spec_path": str(spec_path.resolve()), "spec_hash": spec_hash(load_spec(spec_path))})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        run = wait_terminal_http(c, run_id)
        assert run["status"] == "succeeded"
        stats = c.get("/api/stats").json()["stats"]
        assert len(stats) == 1 and stats[0]["job_id"] == "e2e-stats" and stats[0]["n_runs"] == 1
        assert stats[0]["tokens_p95"] == 46
        assert request(settings.socket_path, "stats_show", job_id="e2e-stats", timeout=2.0) == c.get("/api/stats/e2e-stats").json()
        cli = runner.invoke(app, ["stats", "list"])
        assert cli.exit_code == 0 and "e2e-stats" in cli.output and "n=1" in cli.output

        # 2. backdate the run, sweep over HTTP → clutch.composted, artifacts gone, composted over HTTP
        backdate_run(settings, run_id, months_ago_iso(8))
        r = c.post("/api/retention/sweep", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["deleted"] == 1
        assert body["expired"][0]["run_id"] == run_id and body["expired"][0]["kind"] == "result"
        assert not (settings.results_dir / run_id).exists()
        events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
        assert "clutch.composted" in events
        res = c.get(f"/api/results/{run_id}")
        assert res.status_code == 200 and res.json()["expired"] is True and res.json()["ok"] is False

        # 3. the SPA serves the Stats view + the Dashboard retention card
        page = c.get("/")
        assert page.status_code == 200 and 'id="stats-grid"' in page.text and 'id="sweep-now"' in page.text
        for asset in (page.text, c.get("/app.js").text, c.get("/styles.css").text):
            assert str(tmp_path) not in asset

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
