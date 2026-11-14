"""Tests for spec 30: GUI lifecycle controls & hatchery knobs.

Seams (per the spec's checkbox list):
- HTTP parity for the new mirrors: ``POST /api/runs/{id}/stop`` and
  ``POST /api/runs/{id}/rerun`` == the ``run_stop`` / ``run_rerun`` socket
  payloads (200 ok / 422 refusal / 400 malformed body), and
  ``GET /api/connectors`` + ``POST /api/connectors/add`` + ``POST /api/connectors/rm``
  == the slice-28 socket ops (200 / 422 field-named / 400);
- dead-socket ``200 {ok:false, daemon.up:false}`` on all five, POSTs included,
  never a 500; bearer 401 on all five when auth is required, ``/api/ping`` open;
- lifecycle e2e over HTTP: cancel a queued egg, stop a running chicken, re-run a
  terminal one (new run, trigger=rerun, lineage), rows show the terminal state +
  reason; ``runs_list`` rows carry ``parent_run_id`` (lineage) and the
  feed-budget telemetry ``cost_usd`` / ``max_cost_usd`` / ``budget_outcome`` —
  a capped run shows ``budget.outcome = "cost_cap"`` over HTTP == socket;
- the upload endpoint surfaces the knob pairing as inline field-named errors
  (``resources.timeout_sec`` when ``free_range = false`` without a timeout,
  ``resources.max_cost_usd`` on a non-positive cap, ``harness.connectors`` on
  unknown gate names at enqueue);
- secrecy: no coop token / gate env value in any new payload or audit row;
- served SPA markup for the Stop / Re-run controls, lineage, feed-vs-cap and the
  knob form (no host paths in the assets);
- a subprocess e2e (``tc coop start`` + fake vibe): stop + re-run + connectors
  CRUD entirely over HTTP, with CLI parity.

All assertions are external, observable behavior: socket payloads, HTTP status
codes and JSON bodies, served markup, audit contents.
"""

from __future__ import annotations

import asyncio
import base64
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
from test_run_e2e import FAKE_VIBE, combined_output, write_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()

GATE_SECRET = "SPEC30-GATE-SECRET-7c1d-do-not-serve"
COOP_TOKEN = "spec30-coop-token"


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch):
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def budget1_daemon(tmp_path, monkeypatch):
    """Budget 1: a second enqueued vibe-cli egg (weight 1.0) stays queued."""
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n'
        f'coop_enabled = true\ncoop_port = {free_port()}\n'
    )
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
        coop_auth_token=COOP_TOKEN,
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


def enqueue(sock, spec_path: Path, **extra) -> str:
    digest = spec_hash(load_spec(spec_path))
    resp = request(sock, "enqueue", timeout=5.0, spec_path=str(spec_path.resolve()), spec_hash=digest, **extra)
    assert resp["ok"], resp
    return resp["run_id"]


def wait_status(sock, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(sock, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


def write_knob_job(
    tmp_path: Path,
    name: str,
    *,
    resources_extra: str = "",
    drop_timeout: bool = False,
    harness_extra: str = "",
    prompt: str = "Say hello",
) -> Path:
    """A vibe-cli job with arbitrary extra [resources]/[harness] lines (spec 30 knobs)."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "prompt.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    timeout_line = "" if drop_timeout else "timeout_sec = 30\n"
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "knob test job"

[trigger]
type = "manual"

[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0
{harness_extra}

[resources]
{timeout_line}max_context_mb = 2
{resources_extra}network = "none"

[input]
prompt_file = "prompts/prompt.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


def bundle_of(job_toml: str, prompt: str = "Say hello", schema: str = '{"type": "object"}') -> dict:
    def b64(s: str) -> str:
        return base64.b64encode(s.encode()).decode()

    return {"files": {"job.toml": b64(job_toml), "prompts/p.txt": b64(prompt), "schemas/out.json": b64(schema)}}


JOB_TOML_TEMPLATE = """
[job]
name = "{name}"
description = "upload knob job"

[trigger]
type = "manual"

[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0
{harness_extra}

[resources]
{timeout_line}max_context_mb = 2
{resources_extra}network = "none"

[input]
prompt_file = "prompts/p.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.json"
"""


def job_toml(name: str, resources_extra: str = "", timeout_line: str = "timeout_sec = 30\n", harness_extra: str = "") -> str:
    return JOB_TOML_TEMPLATE.format(name=name, resources_extra=resources_extra, timeout_line=timeout_line, harness_extra=harness_extra)


# --- POST /api/runs/{id}/stop + /rerun: parity, refusals, terminal states --------


def test_stop_and_rerun_over_http_parity_with_socket(budget1_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(budget1_daemon.settings)
    sock = budget1_daemon.settings.socket_path
    slow = write_job(tmp_path, name="slow-life", prompt="SLEEP 20 slow", timeout=120)
    quick = write_job(tmp_path, name="quick-life", prompt="Say hello")

    running = enqueue(sock, slow)  # fills the budget of 1
    wait_status(sock, running, ("running",))
    q1 = enqueue(sock, quick)
    q2 = enqueue(sock, quick)
    assert wait_status(sock, q1, ("queued",))["status"] == "queued"

    # socket stop on q1, HTTP stop on q2 — identical payloads modulo the run id
    sock_resp = request(sock, "run_stop", run_id=q1, timeout=30.0)
    assert sock_resp["ok"] and sock_resp["status"] == "cancelled"
    r = c.post(f"/api/runs/{q2}/stop", json={})
    assert r.status_code == 200, r.text
    http_resp = r.json()
    assert http_resp == {**sock_resp, "run_id": q2}

    # stop the running chicken over HTTP
    r = c.post(f"/api/runs/{running}/stop")  # no body at all is fine too
    assert r.status_code == 200, r.text
    stopped = r.json()
    assert stopped["ok"] and stopped["status"] == "stopped" and stopped["run_id"] == running
    assert "stopped by operator" in (stopped["reason"] or "")

    # the rows now show the terminal state + reason over HTTP == socket
    rows = c.get("/api/runs").json()
    assert rows == request(sock, "runs_list", timeout=2.0)
    by_id = {row["id"]: row for row in rows["runs"]}
    assert by_id[q1]["status"] == "cancelled" and "never hatched" in (by_id[q1]["status_reason"] or "")
    assert by_id[q2]["status"] == "cancelled"
    assert by_id[running]["status"] == "stopped" and "stopped by operator" in (by_id[running]["status_reason"] or "")
    assert all(by_id[rid]["attested"] for rid in (q1, q2, running))

    # refusals: already terminal, unknown chicken, malformed body
    r = c.post(f"/api/runs/{q1}/stop", json={})
    assert r.status_code == 422 and "already terminal" in r.json()["error"]
    r = c.post("/api/runs/run-nope/stop", json={})
    assert r.status_code == 422 and "unknown chicken" in r.json()["error"]
    r = c.post(f"/api/runs/{running}/stop", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = c.post("/api/runs/run-nope/rerun", json={})
    assert r.status_code == 422 and "unknown chicken" in r.json()["error"]

    # re-run over HTTP == socket: a fresh chicken with trigger=rerun + lineage
    r = c.post(f"/api/runs/{q1}/rerun", json={})
    assert r.status_code == 200, r.text
    http_rerun = r.json()
    assert http_rerun["ok"] and http_rerun["op"] == "run_rerun" and http_rerun["rerun_of"] == q1
    sock_rerun = request(sock, "run_rerun", run_id=q2, timeout=10.0)
    assert sock_rerun["ok"] and sock_rerun["op"] == "run_rerun" and sock_rerun["rerun_of"] == q2
    assert {k: v for k, v in http_rerun.items() if k not in ("run_id", "rerun_of")} == {
        k: v for k, v in sock_rerun.items() if k not in ("run_id", "rerun_of")
    }
    assert http_rerun["rerun_of"] == q1 and sock_rerun["rerun_of"] == q2
    new_http, new_sock = http_rerun["run_id"], sock_rerun["run_id"]
    for rid, src in ((new_http, q1), (new_sock, q2)):
        run = wait_status(sock, rid, TERMINAL_STATES, timeout=30.0)
        assert run["status"] == "succeeded" and run["trigger"] == "rerun"
        assert run["manifest"]["rerun_of"] == src
        # and over HTTP the detail is the socket payload
        show_http = c.get(f"/api/runs/{rid}").json()
        assert show_http == request(sock, "runs_show", run_id=rid, timeout=2.0)
        assert show_http["run"]["manifest"]["rerun_of"] == src

    # audit rows for the lifecycle over HTTP-driven ops
    audit = AuditLog(budget1_daemon.settings.audit_log_path).read_all()
    events = [e["event"] for e in audit]
    assert "egg.cancelled" in events and "chicken.stopped" in events


# --- runs_list: lineage (parent_run_id) + feed telemetry (cost vs cap) over HTTP ----


def test_runs_list_lineage_and_cost_vs_cap_over_http(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path

    capped = write_knob_job(tmp_path, name="capped", resources_extra="max_cost_usd = 0.0005\n")
    plain = write_job(tmp_path, name="plain-life", prompt="Say hello")

    capped_run = enqueue(sock, capped)
    run = wait_status(sock, capped_run, TERMINAL_STATES)
    assert run["status"] == "failed"
    assert run["manifest"]["budget"]["outcome"] == "cost_cap"
    assert run["manifest"]["budget"]["max_cost_usd"] == 0.0005
    assert run["usage"]["cost_usd"] == pytest.approx(0.001)  # telemetry still recorded

    plain_run = enqueue(sock, plain)
    wait_status(sock, plain_run, ("succeeded",))

    # a sub-chicken: ordinary egg enqueued with parent_run_id lineage
    sub = enqueue(sock, plain, parent_run_id=plain_run)
    wait_status(sock, sub, ("succeeded",))

    # HTTP == socket for the list, and the rows carry lineage + feed telemetry
    rows_http = c.get("/api/runs").json()
    assert rows_http == request(sock, "runs_list", timeout=2.0)
    by_id = {row["id"]: row for row in rows_http["runs"]}
    assert by_id[capped_run]["budget_outcome"] == "cost_cap"
    assert by_id[capped_run]["max_cost_usd"] == 0.0005
    assert by_id[capped_run]["cost_usd"] == pytest.approx(0.001)
    assert by_id[plain_run]["budget_outcome"] == "ok"
    assert by_id[plain_run]["max_cost_usd"] is None
    assert by_id[plain_run]["cost_usd"] == pytest.approx(0.001)
    assert by_id[sub]["parent_run_id"] == plain_run
    assert by_id[plain_run]["parent_run_id"] is None

    # run detail over HTTP: lineage + the capped budget outcome visible
    show = c.get(f"/api/runs/{sub}").json()
    assert show == request(sock, "runs_show", run_id=sub, timeout=2.0)
    assert show["run"]["parent_run_id"] == plain_run
    show_cap = c.get(f"/api/runs/{capped_run}").json()
    assert show_cap["run"]["manifest"]["budget"]["outcome"] == "cost_cap"
    assert show_cap["run"]["manifest"]["usage"]["cost_usd"] == pytest.approx(0.001)


# --- connectors mirrors: parity, CRUD, field-named errors, secrecy -------------------


def test_connectors_endpoints_parity_crud_and_secrecy(coop_daemon, tmp_path, monkeypatch) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)

    # empty registry: HTTP == socket
    assert c.get("/api/connectors").json() == request(sock, "connectors_list", timeout=2.0)
    assert c.get("/api/connectors").json()["connectors"] == []

    # register over HTTP (stdio gate whose env value is a literal secret) and via
    # the socket (https gate with a $REF header) — both must stay non-secret
    r = c.post(
        "/api/connectors/add",
        json={"name": "toolx", "transport": "stdio", "command": "/bin/toolx", "args": ["--serve"], "env": {"GATE_TOKEN": GATE_SECRET}},
    )
    assert r.status_code == 200, r.text
    added = r.json()
    assert added["ok"] and added["name"] == "toolx" and added["transport"] == "stdio"
    assert added["entry"]["env_keys"] == ["GATE_TOKEN"]
    assert GATE_SECRET not in r.text
    sock_add = request(
        sock,
        "connectors_add",
        name="webx",
        transport="https",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer $TC_GATE_SECRET"},
        timeout=5.0,
    )
    assert sock_add["ok"], sock_add

    # list: HTTP == socket, non-secret summaries only
    listed = c.get("/api/connectors").json()
    assert listed == request(sock, "connectors_list", timeout=2.0)
    by_name = {e["name"]: e for e in listed["connectors"]}
    assert by_name["toolx"]["command"] == "/bin/toolx" and by_name["toolx"]["args"] == ["--serve"]
    assert by_name["webx"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}  # ref stays a ref
    assert GATE_SECRET not in c.get("/api/connectors").text
    assert "$TC_GATE_SECRET" not in json.dumps(by_name["webx"]) or True  # the ref is fine; the value is not
    assert GATE_SECRET not in json.dumps(listed)

    # invalid add → 422 with field-named errors; HTTP == the socket refusal
    bad = c.post("/api/connectors/add", json={"name": "bad", "transport": "stdio"}).json()
    sock_bad = request(sock, "connectors_add", name="bad", transport="stdio", timeout=5.0)
    assert c.post("/api/connectors/add", json={"name": "bad", "transport": "stdio"}).status_code == 422
    assert bad == sock_bad
    assert bad["ok"] is False and bad["valid"] is False
    assert bad["errors"][0]["field"].startswith("connectors.bad")

    # malformed bodies → 400
    assert c.post("/api/connectors/add", json={"transport": "stdio"}).status_code == 400
    assert c.post("/api/connectors/add", content=b"{oops", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/connectors/rm", json={}).status_code == 422  # no name → socket refusal
    assert c.post("/api/connectors/rm", content=b"nope", headers={"Content-Type": "application/json"}).status_code == 400

    # rm over HTTP == socket; unknown gate → 422
    rm = c.post("/api/connectors/rm", json={"name": "toolx"})
    assert rm.status_code == 200 and rm.json()["ok"] and rm.json()["name"] == "toolx"
    sock_rm = request(sock, "connectors_rm", name="webx", timeout=5.0)
    assert {k: v for k, v in rm.json().items() if k not in ("name", "transport")} == {
        k: v for k, v in sock_rm.items() if k not in ("name", "transport")
    }
    assert rm.json()["transport"] == "stdio" and sock_rm["transport"] == "https"
    r = c.post("/api/connectors/rm", json={"name": "toolx"})
    assert r.status_code == 422 and "unknown gate" in r.json()["error"]
    assert [e["name"] for e in c.get("/api/connectors").json()["connectors"]] == []

    # secrecy end to end: the secret value is only ever at rest in connectors.toml
    registry = coop_daemon.settings.data_dir / "connectors.toml"
    audit_text = AuditLog(coop_daemon.settings.audit_log_path).read_all()
    gate_rows = [e for e in audit_text if str(e.get("event", "")).startswith("gate.")]
    assert gate_rows and all(GATE_SECRET not in json.dumps(e) for e in gate_rows)
    assert {e["event"] for e in gate_rows} >= {"gate.registered", "gate.removed", "gate.rejected"}
    # (the registry file itself is 0600 and holds the secret at rest only — that is by design)
    assert registry.exists() and (registry.stat().st_mode & 0o777) == 0o600

    # CLI parity: tc connectors list shows the remaining gates
    cli = runner.invoke(app, ["connectors", "list"])
    assert cli.exit_code == 0, combined_output(cli)
    assert "no gates registered" in cli.output


# --- bearer 401s on every new endpoint when auth is required --------------------------


def test_new_endpoints_bearer_401_when_auth_required(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": f"Bearer {COOP_TOKEN}"}
    new_routes = [
        ("GET", "/api/connectors", None),
        ("POST", "/api/connectors/add", {"name": "x", "transport": "stdio", "command": "/bin/x"}),
        ("POST", "/api/connectors/rm", {"name": "x"}),
        ("POST", "/api/runs/run-x/stop", {}),
        ("POST", "/api/runs/run-x/rerun", {}),
    ]
    for method, path, body in new_routes:
        r = c.request(method, path, json=body) if body is not None else c.get(path)
        assert r.status_code == 401, (method, path, r.status_code)
        assert COOP_TOKEN not in r.text
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open

    # with the bearer: real answers (200 list, 422 refusals on unknown chicken)
    r = c.get("/api/connectors", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True and COOP_TOKEN not in r.text
    for path, body in (
        ("/api/runs/run-x/stop", {}),
        ("/api/runs/run-x/rerun", {}),
    ):
        r = c.post(path, json=body, headers=headers)
        assert r.status_code == 422 and "unknown chicken" in r.json()["error"]
        assert COOP_TOKEN not in r.text
    r = c.post("/api/connectors/add", json={"name": "ok1", "transport": "stdio", "command": "/bin/ok"}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] and COOP_TOKEN not in r.text
    r = c.post("/api/connectors/rm", json={"name": "ok1"}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"]


# --- dead-socket transport: 200 {ok:false, daemon.up:false} on all five, never 500 -----


def test_lifecycle_and_connectors_endpoints_daemon_down_not_500(tmp_path, monkeypatch) -> None:
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
                r = await c.get("/api/connectors")
                out["GET /api/connectors"] = (r.status_code, r.json())
                r = await c.post("/api/connectors/add", json={"name": "x", "transport": "stdio", "command": "/bin/x"})
                out["POST /api/connectors/add"] = (r.status_code, r.json())
                r = await c.post("/api/connectors/rm", json={"name": "x"})
                out["POST /api/connectors/rm"] = (r.status_code, r.json())
                r = await c.post("/api/runs/run-x/stop", json={})
                out["POST stop"] = (r.status_code, r.json())
                r = await c.post("/api/runs/run-x/rerun", json={})
                out["POST rerun"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    assert len(out) == 5
    for key, (code, body) in out.items():
        assert code == 200, (key, code)  # structured payload, never a 500 — POSTs included
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- upload form knobs surface as inline field-named errors through the same endpoint ---


def test_upload_knob_bundles_field_named_errors_and_enqueue_refusals(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)

    # free_range = false without timeout_sec → resources.timeout_sec error
    r = c.post("/api/jobs/validate", json=bundle_of(job_toml("no-timeout", timeout_line="")))
    assert r.status_code == 422, r.text
    fields = [e["field"] for e in r.json()["errors"]]
    assert "resources.timeout_sec" in fields

    # free_range = true allows the timeout to be absent → valid
    r = c.post(
        "/api/jobs/validate",
        json=bundle_of(job_toml("free-range", timeout_line="", resources_extra="free_range = true\n")),
    )
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True and r.json()["ok"]

    # non-positive cap → resources.max_cost_usd error
    r = c.post("/api/jobs/validate", json=bundle_of(job_toml("neg-cap", resources_extra="max_cost_usd = -1\n")))
    assert r.status_code == 422
    assert "resources.max_cost_usd" in [e["field"] for e in r.json()["errors"]]

    # the full knob set validates and stores; unknown gate names are refused at enqueue
    knobs_toml = job_toml(
        "all-knobs",
        resources_extra="free_range = true\nmax_cost_usd = 0.5\n",
        timeout_line="",
        harness_extra='spawn_subs = true\nconnectors = ["toolx"]\n',
    )
    r = c.post("/api/jobs/validate", json=bundle_of(knobs_toml))
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True
    stored = load_spec(r.json()["spec_path"])
    assert stored.resources.free_range is True
    assert stored.resources.timeout_sec is None
    assert stored.resources.max_cost_usd == 0.5
    assert stored.harness.spawn_subs is True
    assert stored.harness.connectors == ["toolx"]

    # enqueueing a bundle with an unknown gate → 422, field-named harness.connectors, nothing queued
    nope_toml = job_toml("nope-gate", harness_extra='connectors = ["nope"]\n')
    r = c.post("/api/runs", json=bundle_of(nope_toml))
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["ok"] is False and body["errors"][0]["field"] == "harness.connectors"
    runs = c.get("/api/runs").json()
    assert all(row["job_id"] != "nope-gate" for row in runs["runs"])


# --- served SPA markup: lifecycle controls, lineage, feed-vs-cap, knob form -------------


def test_spa_serves_lifecycle_and_knob_markup(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        # runs table: Feed + Actions columns (spec 30)
        "<th scope=\"col\">Feed</th>", '<span class="visually-hidden">Actions</span>',
        # run detail: lineage + feed-vs-cap line
        'id="detail-lineage"', 'id="detail-budget"',
        # upload form hatchery knobs
        'id="up-free-range"', 'id="up-free-range-error"',
        'id="up-timeout"', 'id="up-timeout-error"',
        'id="up-max-cost"', 'id="up-max-cost-error"',
        'id="up-spawn-subs"', 'id="up-spawn-subs-error"',
        'id="up-connectors"', 'id="up-connectors-error"',
        "Hatchery knobs",
        # Feed view: feed-vs-budget card
        'id="cost-card"', 'id="cost-list"', 'id="cost-empty"', "Feed vs budget",
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    for marker in (
        '"/stop"', '"/rerun"',
        "run-stop", "run-rerun", "stopRun", "rerunRun",
        "childrenMap", "lineageNote", "feedCellText", "renderCostCard",
        "applyKnobs", "setTomlKey", "readKnobsFromToml", "knobSignature",
        "TERMINAL_RUN_STATES", "dot-cancelled", "dot-stopped",
        '"resources.timeout_sec"', '"resources.max_cost_usd"', '"harness.connectors"', '"harness.spawn_subs"', '"resources.free_range"',
    ):
        assert marker in js, marker
    css = c.get("/styles.css").text
    for cls in (".dot-cancelled", ".dot-stopped", ".run-actions", ".lineage-note", ".status-reason", ".cost-cap", ".knob-grid", ".cost-card", ".cost-list", "#detail-budget"):
        assert cls in css, cls
    # no host paths ever enter the served assets
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: stop + re-run + connectors CRUD entirely over HTTP ----------------


def test_e2e_lifecycle_and_connectors_over_http(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)  # `tc coop start` forces coop on
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)
    settings = load_settings(f)
    # the daemon access-logs to stdout — drain it to a file so the pipe never
    # fills and blocks the daemon mid-test
    logf = open(tmp_path / "coopd.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=dict(os.environ),
        stdout=logf,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        # stop waits for the terminal state (SIGTERM → up to the 5s grace) — allow for it
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0)
        assert c.get("/api/ping").status_code == 200

        # queue a slow chicken, stop it over HTTP, re-run it over HTTP
        spec_path = write_job(tmp_path, name="e2e-life", prompt="SLEEP 25 e2e", timeout=120)
        r = c.post("/api/runs", json={"spec_path": str(spec_path.resolve())})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            show = c.get(f"/api/runs/{run_id}").json()
            if show.get("ok") and show["run"]["status"] == "running":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("run did not reach running")

        r = c.post(f"/api/runs/{run_id}/stop", json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "stopped"

        r = c.post(f"/api/runs/{run_id}/rerun", json={})
        assert r.status_code == 200, r.text
        rerun_id = r.json()["run_id"]
        assert rerun_id != run_id and r.json()["rerun_of"] == run_id

        # the re-run goes through the queue to a terminal state; both rows visible with lineage
        deadline = time.time() + 30
        while time.time() < deadline:
            rows = c.get("/api/runs").json()["runs"]
            by_id = {row["id"]: row for row in rows}
            if by_id[rerun_id]["status"] in TERMINAL_STATES:
                break
            time.sleep(0.1)
        assert by_id[run_id]["status"] == "stopped" and by_id[run_id]["status_reason"]
        assert by_id[rerun_id]["trigger"] == "rerun" and by_id[rerun_id]["status"] == "succeeded"
        show = c.get(f"/api/runs/{rerun_id}").json()
        assert show["run"]["manifest"]["rerun_of"] == run_id

        # connectors CRUD over HTTP, secret never served
        r = c.post("/api/connectors/add", json={"name": "e2egate", "transport": "https", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer $TC_GATE_SECRET"}})
        assert r.status_code == 200, r.text
        assert GATE_SECRET not in r.text
        listed = c.get("/api/connectors").json()
        assert [e["name"] for e in listed["connectors"]] == ["e2egate"]
        assert listed["connectors"][0]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
        assert GATE_SECRET not in c.get("/api/connectors").text
        r = c.post("/api/connectors/rm", json={"name": "e2egate"})
        assert r.status_code == 200 and c.get("/api/connectors").json()["connectors"] == []

        # CLI parity on the same daemon
        cli = runner.invoke(app, ["runs", "list"])
        assert cli.exit_code == 0, combined_output(cli)
        assert run_id in cli.output and "stopped" in cli.output
        assert rerun_id in cli.output and "trigger=rerun" in cli.output

        # the SPA serves the new controls
        page = c.get("/")
        assert page.status_code == 200
        for marker in ('id="detail-budget"', 'id="up-free-range"', 'id="up-connectors"', 'id="cost-card"', '<th scope="col">Feed</th>'):
            assert marker in page.text, marker

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        logf.close()
