"""Tests for spec 32 ticket 24 (review finding G24): every mutating Headcount
(audit) row carries an additive `via` actor field — `socket` / `http` /
`farmhand` / `rooster` — stamped server-side from the verified connection,
outside the client-writable request body.

Seams (external, observable behavior only):
- a real `python -m turbochickens daemon start` subprocess + the `tc` CLI and
  raw JSON-lines socket requests (the socket origin, and the forgery probes:
  a `via` key smuggled into a socket request line must be overridden);
- the coop HTTP seam (`DaemonThread` + `POST /api/*`): rows carry `via=http`,
  and a `via` key in a POST body is ignored (the coop layer stamps its own);
- a real `tc farmhand serve --transport stdio` subprocess driven by the MCP
  SDK's stdio client: rows carry `via=farmhand`;
- a firing next-minute cron schedule: rows carry `via=rooster`.

Also pinned: every row in the log carries a valid `via`, and the DB schema
version stays 5 (no schema change — `via` lives in the Headcount rows only).
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

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, run_id_of, write_job
from test_schedule import next_minute_cron, write_cron_job
from turbochickens.audit import VIA_ACTORS, AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings

runner = CliRunner()

LIFECYCLE_EVENTS = ("egg.queued", "egg.hatched", "clutch.stored", "abattoir.attested", "chicken.finished")


def rows_for(audit_path: Path, run_id: str) -> list[dict]:
    return [r for r in AuditLog(audit_path).read_all() if r.get("run_id") == run_id]


def wait_status(socket_path, run_id: str, want, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def budget1_settings(tmp_path, monkeypatch) -> Path:
    """Budget 1: a second vibe-cli egg (weight 1.0) backlogs as queued."""
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch, fake_vibe):
    """Coop on a free loopback port (open mode), budget 1 for the queued-egg stop."""
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\n'
        f"weight_budget_override = 1\n"
        f"coop_enabled = true\n"
        f"coop_port = {free_port()}\n"
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def start_daemon_subprocess(settings, env) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "daemon", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if settings.socket_path.exists():
            try:
                if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                    return proc
            except DaemonUnreachable:
                pass
        if proc.poll() is not None:
            raise AssertionError(f"daemon subprocess exited early rc={proc.returncode}")
        time.sleep(0.05)
    raise AssertionError("daemon subprocess did not start answering")


# --- socket origin: full lifecycle over a subprocess daemon + the CLI --------


def test_socket_origin_rows_carry_via_socket_and_schema_stays_v5(budget1_settings, tmp_path, fake_vibe) -> None:
    settings = load_settings(budget1_settings)
    env = dict(os.environ)
    proc = start_daemon_subprocess(settings, env)
    sock = settings.socket_path
    try:
        quick = write_job(tmp_path, name="quick-egg", prompt="Say hello")
        slow = write_job(tmp_path, name="slow-egg", prompt="SLEEP 10 working")

        # a full successful lifecycle from the CLI (a direct socket caller)
        r = runner.invoke(app, ["run", str(quick), "--wait"])
        assert r.exit_code == 0, combined_output(r)
        rid_ok = run_id_of(r)
        assert wait_status(sock, rid_ok, ("succeeded",))["status"] == "succeeded"

        # a queued egg cancelled from the CLI (budget 1: the slow one runs first)
        r1 = runner.invoke(app, ["run", str(slow)])
        r2 = runner.invoke(app, ["run", str(slow)])
        assert r1.exit_code == 0 and r2.exit_code == 0
        slow1, slow2 = run_id_of(r1), run_id_of(r2)
        wait_status(sock, slow1, ("running",))
        r = runner.invoke(app, ["runs", "stop", slow2])
        assert r.exit_code == 0, combined_output(r)
        assert f"chicken {slow2} → cancelled" in r.output

        audit = AuditLog(settings.audit_log_path).read_all()

        # every lifecycle row of both runs records via=socket
        for rid in (rid_ok, slow2):
            rows = rows_for(settings.audit_log_path, rid)
            events = {r["event"] for r in rows}
            assert events >= {"egg.queued", "abattoir.attested", "chicken.finished"}, events
            for row in rows:
                assert row["via"] == "socket", row
        ok_rows = rows_for(settings.audit_log_path, rid_ok)
        assert {r["event"] for r in ok_rows} >= set(LIFECYCLE_EVENTS)
        assert any(r["event"] == "egg.cancelled" and r["via"] == "socket" for r in rows_for(settings.audit_log_path, slow2))

        # every row in the whole Headcount carries a valid via actor
        assert audit, "no audit rows"
        for row in audit:
            assert row["via"] in VIA_ACTORS, row

        # no schema change: the DB stays at v5 (via lives in the rows only)
        conn = sqlite3.connect(str(settings.db_path))
        try:
            version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        finally:
            conn.close()
        assert version == ("5",)

        r = runner.invoke(app, ["daemon", "stop"])
        assert r.exit_code == 0, combined_output(r)
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- forgery: a client-sent `via` in a socket request line is overridden ------


def test_forged_via_in_socket_request_body_is_overridden(budget1_settings, tmp_path, fake_vibe) -> None:
    settings = load_settings(budget1_settings)
    env = dict(os.environ)
    proc = start_daemon_subprocess(settings, env)
    sock = settings.socket_path
    try:
        job = write_job(tmp_path, name="forge-egg", prompt="Say hello")
        forged = ["http", "farmhand", "rooster", "bogus-actor"]
        run_ids = []
        for v in forged:
            resp = request(sock, "enqueue", spec_path=str(job), via=v, timeout=5.0)
            assert resp.get("ok"), resp
            run_ids.append(resp["run_id"])
        # also an explicit "socket" claim is fine (it is the socket)
        resp = request(sock, "enqueue", spec_path=str(job), via="socket", timeout=5.0)
        assert resp.get("ok"), resp
        run_ids.append(resp["run_id"])

        rows = {r["run_id"]: r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "egg.queued"}
        for rid in run_ids:
            assert rows[rid]["via"] == "socket", rows[rid]

        for rid in run_ids:
            request(sock, "run_stop", run_id=rid, timeout=10.0)
        r = runner.invoke(app, ["daemon", "stop"])
        assert r.exit_code == 0, combined_output(r)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- http origin: the coop layer stamps via=http outside the request body -----


def test_http_origin_rows_carry_via_http_and_body_via_is_ignored(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    client = http_client(settings)
    try:
        slow = write_job(tmp_path, name="http-slow", prompt="SLEEP 10 working")
        quick = write_job(tmp_path, name="http-quick", prompt="Say hello")

        # enqueue over HTTP — with a forged `via` in the POST body, which the
        # coop layer must drop and replace with its own actor
        resp = client.post("/api/runs", json={"spec_path": str(quick), "via": "socket"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        rid_ok = body["run_id"]
        assert wait_status(sock, rid_ok, ("succeeded",))["status"] == "succeeded"

        # a second egg backlogs (budget 1) and is cancelled over HTTP
        resp = client.post("/api/runs", json={"spec_path": str(slow), "via": "farmhand"})
        assert resp.status_code == 200, resp.text
        slow1 = resp.json()["run_id"]
        resp = client.post("/api/runs", json={"spec_path": str(slow), "via": "rooster"})
        assert resp.status_code == 200, resp.text
        slow2 = resp.json()["run_id"]
        wait_status(sock, slow1, ("running",))
        resp = client.post(f"/api/runs/{slow2}/stop", json={"via": "socket"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"

        # every lifecycle row of both runs records via=http
        for rid in (rid_ok, slow2):
            rows = rows_for(settings.audit_log_path, rid)
            assert rows, rid
            for row in rows:
                assert row["via"] == "http", row
        ok_rows = rows_for(settings.audit_log_path, rid_ok)
        assert {r["event"] for r in ok_rows} >= set(LIFECYCLE_EVENTS)
        assert any(r["event"] == "egg.cancelled" and r["via"] == "http" for r in rows_for(settings.audit_log_path, slow2))

        # the Headcount served over HTTP carries the via field too
        tail = client.get("/api/audit", params={"limit": 500}).json()
        assert all(e["via"] in VIA_ACTORS for e in tail["events"])
        assert any(e["event"] == "egg.queued" and e["run_id"] == rid_ok and e["via"] == "http" for e in tail["events"])

        # stopping the in-flight chicken over HTTP → chicken.stopped via=http
        resp = client.post(f"/api/runs/{slow1}/stop", json={"via": "socket"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "stopped"
        assert wait_status(sock, slow1, ("stopped",))["status"] == "stopped"
        assert any(
            r["event"] == "chicken.stopped" and r["via"] == "http" for r in rows_for(settings.audit_log_path, slow1)
        )
    finally:
        client.close()


def test_http_origin_other_mutating_ops_carry_via_http(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    client = http_client(settings)
    try:
        # settings_set over HTTP → settings.changed via=http
        resp = client.post("/api/settings", json={"farmhand_transport": "stdio"})
        assert resp.status_code == 200, resp.text
        rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "settings.changed"]
        assert rows and all(r["via"] == "http" for r in rows), rows

        # a gate registered over HTTP → gate.registered via=http
        resp = client.post(
            "/api/connectors/add",
            json={"name": "via-gate", "transport": "stdio", "command": "/bin/true", "via": "socket"},
        )
        assert resp.status_code == 200, resp.text
        rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "gate.registered"]
        assert rows and rows[-1]["name"] == "via-gate" and rows[-1]["via"] == "http", rows

        # a schedule added over HTTP → rooster.scheduled via=http
        cron_spec = write_cron_job(tmp_path, name="http-cron", schedule="0 7 * * 1")
        resp = client.post("/api/schedule", json={"spec_path": str(cron_spec), "via": "socket"})
        assert resp.status_code == 200, resp.text
        rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "rooster.scheduled"]
        assert rows and all(r["via"] == "http" for r in rows), rows

        # removing it again over HTTP → rooster.unscheduled via=http
        resp = client.request("DELETE", "/api/schedule/http-cron")
        assert resp.status_code == 200, resp.text
        rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "rooster.unscheduled"]
        assert rows and all(r["via"] == "http" for r in rows), rows
    finally:
        client.close()


# --- farmhand origin: a real stdio Farmhand subprocess stamps via=farmhand -----


def test_farmhand_origin_rows_carry_via_farmhand(budget1_settings, tmp_path, fake_vibe) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    settings = load_settings(budget1_settings)
    dt = DaemonThread(settings)
    dt.start()
    sock = settings.socket_path

    def tool_payload(result) -> dict:
        assert not result.is_error, f"tool errored: {result.content}"
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        assert texts, f"no text content: {result.content}"
        return json.loads(texts[0])

    async def drive():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "turbochickens", "farmhand", "serve", "--transport", "stdio"],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, args=None):
                    return tool_payload(await session.call_tool(name, args or {}))

                async def sock_call(op, **payload):
                    from turbochickens.client import request_async

                    return await request_async(sock, op, timeout=2.0, **payload)

                quick = write_job(tmp_path, name="fh-quick", prompt="Say hello")
                slow = write_job(tmp_path, name="fh-slow", prompt="SLEEP 10 working")

                # enqueue through the Farmhand → full lifecycle rows via=farmhand
                p = await call("enqueue", {"spec_path": str(quick)})
                assert p["ok"] is True
                rid_ok = p["run_id"]

                # a second egg backlogs (budget 1), then is cancelled through
                # the Farmhand's guarded run_stop → egg.cancelled via=farmhand
                p1 = await call("enqueue", {"spec_path": str(slow)})
                p2 = await call("enqueue", {"spec_path": str(slow)})
                assert p1["ok"] and p2["ok"]
                slow1, slow2 = p1["run_id"], p2["run_id"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    run = (await sock_call("runs_show", run_id=slow1))["run"]
                    if run["status"] == "running":
                        break
                    await asyncio.sleep(0.05)
                p = await call("run_stop", {"run_id": slow2})
                assert p["ok"] is True and p["status"] == "cancelled", p

                # wait for the first farmhand run to finish
                deadline = time.time() + 40
                while time.time() < deadline:
                    run = (await sock_call("runs_show", run_id=rid_ok))["run"]
                    if run["status"] in TERMINAL_STATES:
                        break
                    await asyncio.sleep(0.1)
                assert run["status"] == "succeeded", run["manifest"]
                return rid_ok, slow2

    try:
        rid_ok, slow2 = asyncio.run(asyncio.wait_for(drive(), timeout=120))
        for rid in (rid_ok, slow2):
            rows = rows_for(settings.audit_log_path, rid)
            assert rows, rid
            for row in rows:
                assert row["via"] == "farmhand", row
        ok_rows = rows_for(settings.audit_log_path, rid_ok)
        assert {r["event"] for r in ok_rows} >= set(LIFECYCLE_EVENTS)
        assert any(r["event"] == "egg.cancelled" and r["via"] == "farmhand" for r in rows_for(settings.audit_log_path, slow2))
    finally:
        dt.stop()


# --- rooster origin: a firing cron crows with via=rooster ----------------------


def test_rooster_origin_rows_carry_via_rooster(budget1_settings, tmp_path, fake_vibe) -> None:
    settings = load_settings(budget1_settings)
    dt = DaemonThread(settings)
    dt.start()
    sock = settings.socket_path
    try:
        expr = next_minute_cron()
        spec_path = write_cron_job(tmp_path, name="via-cron-job", schedule=expr)

        # registered from the CLI (a socket caller) → rooster.scheduled via=socket
        r = runner.invoke(app, ["schedule", "add", str(spec_path)])
        assert r.exit_code == 0, combined_output(r)
        sched_rows = [x for x in AuditLog(settings.audit_log_path).read_all() if x["event"] == "rooster.scheduled"]
        assert sched_rows and all(x["via"] == "socket" for x in sched_rows), sched_rows

        # wait for the crow: a run with trigger=cron appears
        deadline = time.time() + 90
        cron_run = None
        while time.time() < deadline:
            resp = request(sock, "runs_list", timeout=2.0)
            cron_runs = [x for x in resp["runs"] if x["trigger"] == "cron"]
            if cron_runs:
                cron_run = cron_runs[0]
                break
            time.sleep(0.5)
        assert cron_run is not None, "cron schedule did not fire within 90s"
        rid = cron_run["id"]
        run = wait_status(sock, rid, TERMINAL_STATES, timeout=40)
        assert run["status"] == "succeeded", run["manifest"]

        audit = AuditLog(settings.audit_log_path).read_all()
        # the crow + the queued egg + the whole lifecycle of the cron chicken
        # all record via=rooster
        crowed = [x for x in audit if x["event"] == "rooster.crowed"]
        assert crowed and all(x["via"] == "rooster" for x in crowed), crowed
        rows = rows_for(settings.audit_log_path, rid)
        assert rows, rid
        for row in rows:
            assert row["via"] == "rooster", row
        assert {x["event"] for x in rows} >= {"egg.queued", "egg.hatched", "chicken.finished"}
    finally:
        dt.stop()
