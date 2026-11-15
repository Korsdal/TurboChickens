"""Tests for spec 27: ChickenSubs — a chicken spawning sub-chickens through the Farmhand.

Seams: a real daemon on a temp socket (thread daemon) driven over the
JSON-lines socket, plus a fake vibe binary ($TURBOCHICKENS_VIBE_BIN) that
itself drives the attached Farmhand over stdio (a real MCP stdio client from
the `mcp` SDK, spawned from the run's vibe config entry — exactly what a real
vibe-cli harness would do). Assertions are external, observable behavior only:
socket responses, MCP tool listings/payloads, the runs store, manifests, the
audit log, CLI output, attestation rows.

Covered: `harness.spawn_subs` spec field (pure seam + hash stability), the
`runs.parent_run_id` column + schema v5 migration, the e2e sub-spawn through
the Farmhand over stdio (lineage in store/manifest/audit, same queue/admission
weights, attested teardown), the depth-1 cap (field-named `harness.spawn_subs`
refusal), the sub tool scope (enqueue + read tools only, from a sub-chicken's
Farmhand) and the ancestor-stop refusal, and secrecy (no token material in any
manifest/audit/MCP payload).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from test_farmhand import EXPECTED_TOOLS, tool_payload
from test_run_e2e import db_row, upload_egg
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.storage import SCHEMA_VERSION, init_db

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

runner = CliRunner()

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_SPEC = REPO / "examples" / "weekly-incidents-digest" / "job.toml"

#: spec 27: the tools a sub-chicken's Farmhand exposes — enqueue + read tools
#: plus the guarded run_stop (ancestor targets refused); no schedule/auth/
#: settings/jobs_validate/run_rerun tools
SUB_SCOPE_TOOLS = {
    "enqueue",
    "run_stop",
    "status",
    "jobs_list",
    "runs_list",
    "runs_show",
    "runs_logs",
    "results_get",
    "stats_list",
    "stats_show",
    "audit_tail",
}

# A stand-in for the real `vibe` binary that can also drive the Farmhand
# attached to the run (spec 27): it reads the run's vibe config
# (`<workdir>/.vibe/config.toml`), spawns the configured MCP server entry, and
# talks MCP over stdio with the SDK's own client — exactly what a real vibe-cli
# harness does with an attached MCP server.
#
# Prompt markers:
#   "SUBSPAWN <spec_path>" — list tools, enqueue the egg as a sub-chicken,
#     poll it to a terminal state, read settings (full scope), print the report
#   "SUBSCOPE <spec_path>" — list tools, try to enqueue a spawn_subs egg (depth
#     cap refusal), try run_stop on the parent (ancestor refusal), print the report
#   otherwise — the plain fake-vibe behavior (hello output, usage export,
#     SLEEP/EXIT markers)
FAKE_VIBE_SUBS = r'''#!__PY__
import asyncio, json, os, re, sys, time, tomllib
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)

def argval(name):
    return args[args.index(name) + 1] if name in args else None

prompt = Path(argval("--prompt-file")).read_text() if argval("--prompt-file") else ""
workdir = Path(argval("--workdir") or os.getcwd())
out_dir = argval("--output-dir")

m_sleep = re.search(r"SLEEP ([\d.]+)", prompt)
if m_sleep:
    time.sleep(float(m_sleep.group(1)))

TERMINAL = ("succeeded", "failed", "cancelled", "stopped")

def payload(result):
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    return json.loads(texts[0])

async def with_farmhand(fn):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    cfg = tomllib.loads((workdir / ".vibe" / "config.toml").read_text())
    entry = cfg["mcp_servers"]["farmhand"]
    params = StdioServerParameters(
        command=entry["command"], args=list(entry.get("args", [])), env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)

async def drive_subspawn(session, spec_path):
    tools = await session.list_tools()
    enq = payload(await session.call_tool("enqueue", {"spec_path": spec_path}))
    sub_id = enq.get("run_id")
    sub_status = None
    if sub_id:
        deadline = time.time() + 90
        while time.time() < deadline:
            show = payload(await session.call_tool("runs_show", {"run_id": sub_id}))
            run = show.get("run") or {}
            sub_status = run.get("status")
            if sub_status in TERMINAL:
                break
            await asyncio.sleep(0.2)
    settings = payload(await session.call_tool("settings_get", {}))
    return {
        "message": "sub spawned through the farmhand",
        "tools": sorted(t.name for t in tools.tools),
        "enqueue_ok": enq.get("ok"),
        "weight": enq.get("weight"),
        "sub_run_id": sub_id,
        "sub_status": sub_status,
        "sub_parent_run_id": enq.get("parent_run_id"),
        "settings": settings.get("settings"),
    }

async def drive_subscope(session, spec_path):
    tools = await session.list_tools()
    depth = payload(await session.call_tool("enqueue", {"spec_path": spec_path}))
    parent = os.environ.get("TURBOCHICKENS_PARENT_RUN_ID")
    stop = (
        payload(await session.call_tool("run_stop", {"run_id": parent}))
        if parent
        else {"error": "no TURBOCHICKENS_PARENT_RUN_ID in env"}
    )
    return {
        "message": "subscope probe",
        "tools": sorted(t.name for t in tools.tools),
        "depth_refusal": depth.get("error"),
        "depth_errors": depth.get("errors"),
        "stop_refusal": stop.get("error"),
    }

m_sub = re.search(r"SUBSPAWN (\S+)", prompt)
m_scope = re.search(r"SUBSCOPE (\S+)", prompt)
if m_sub:
    out = asyncio.run(with_farmhand(lambda s: drive_subspawn(s, m_sub.group(1))))
elif m_scope:
    out = asyncio.run(with_farmhand(lambda s: drive_subscope(s, m_scope.group(1))))
else:
    out = {"message": "hello from fake vibe", "echo": prompt.strip()[:40]}

if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, f)
print("fake harness log \x07 bell", file=sys.stderr)
print(json.dumps(out))
m_exit = re.search(r"EXIT (\d+)", prompt)
sys.exit(int(m_exit.group(1)) if m_exit else 0)
'''.replace("__PY__", sys.executable)


def write_job_subs(
    base: Path,
    name: str,
    prompt: str,
    spawn_subs: bool | None = None,
    adapter: str = "vibe-cli",
    timeout: int = 120,
    model: str = "fake-model",
) -> Path:
    """A job spec like test_run_e2e.write_job, plus an optional
    `spawn_subs` line in [harness] (None = key absent, False/True = explicit)."""
    d = base / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    spawn_line = "" if spawn_subs is None else f"spawn_subs = {'true' if spawn_subs else 'false'}\n"
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test job"

[trigger]
type = "manual"

[harness]
adapter = "{adapter}"
model = "{model}"
temperature = 0.0
{spawn_line}
[resources]
timeout_sec = {timeout}
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/hello.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-subs"
    bin_path.write_text(FAKE_VIBE_SUBS)
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


def wait_terminal(socket_path, run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in TERMINAL_STATES:
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def wait_status(socket_path, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


async def wait_terminal_async(socket_path, run_id: str, timeout: float = 60.0) -> dict:
    """wait_terminal for use inside a running event loop (the MCP client's)."""
    from turbochickens.client import request_async

    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await request_async(socket_path, "runs_show", timeout=2.0, run_id=run_id)
        if resp.get("ok") and resp["run"]["status"] in TERMINAL_STATES:
            return resp["run"]
        await asyncio.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def run_stdio_env(fn, env: dict, timeout: float = 90.0):
    """Run `fn(session)` against a real `python -m turbochickens farmhand
    serve --transport stdio` subprocess spawned with a custom env (the
    per-run env of a chicken, spec 27)."""

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "turbochickens", "farmhand", "serve", "--transport", "stdio"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(asyncio.wait_for(main(), timeout))


# --- pure seams: the spec field, the hash, the schema -------------------------


def test_spawn_subs_field_pure_seam(tmp_path) -> None:
    spec = load_spec(write_job_subs(tmp_path, "plain", "hi", spawn_subs=None))
    assert spec.harness.spawn_subs is False  # default
    spec = load_spec(write_job_subs(tmp_path, "sub", "hi", spawn_subs=True))
    assert spec.harness.spawn_subs is True
    # extra = "forbid" intact: a stray key in [harness] is a field-named error
    bad = write_job_subs(tmp_path, "typo", "hi", spawn_subs=True)
    bad.write_text(bad.read_text().replace("spawn_subs = true", "spawn_sub = true"))
    with pytest.raises(SpecValidationError) as ei:
        load_spec(bad)
    assert ("harness", "spawn_sub") in [i.loc for i in ei.value.issues]


def test_spec_hash_excludes_spawn_subs_false(tmp_path) -> None:
    a = spec_hash(load_spec(write_job_subs(tmp_path / "a", "h", "hi", spawn_subs=None)))
    b = spec_hash(load_spec(write_job_subs(tmp_path / "b", "h", "hi", spawn_subs=False)))
    c = spec_hash(load_spec(write_job_subs(tmp_path / "c", "h", "hi", spawn_subs=True)))
    assert a == b  # spawn_subs = false stays out of the hash, like free_range = false
    assert a != c
    # the shipped example's egg hash is unchanged
    h = spec_hash(load_spec(EXAMPLE_SPEC))
    assert h.startswith("18efbd8c") and h.endswith("80b959")


def test_cli_jobs_validate_spawn_subs_and_example_hash(settings_file, tmp_path) -> None:
    # mistral-api spec so the model check resolves against providers.toml
    # (a vibe-cli spec's model would be checked against the real vibe catalog)
    spec = write_job_subs(tmp_path, "cli-sub", "hi", spawn_subs=True, adapter="mistral-api", model="mistral-large-latest")
    result = runner.invoke(app, ["jobs", "validate", str(spec)])
    assert result.exit_code == 0, result.output
    assert "OK: egg 'cli-sub' candled" in result.output
    result = runner.invoke(app, ["jobs", "validate", str(EXAMPLE_SPEC)])
    assert result.exit_code == 0, result.output
    assert "sha256:18efbd8c" in result.output and "80b959" in result.output


def test_schema_v4_migrates_parent_run_id_in_place(tmp_path) -> None:
    assert SCHEMA_VERSION == 5
    # a v4-era DB: runs without parent_run_id, one pre-existing row
    v4 = tmp_path / "v4.db"
    c = sqlite3.connect(str(v4))
    c.execute(
        "CREATE TABLE job_specs (id TEXT PRIMARY KEY, name TEXT NOT NULL, spec_hash TEXT NOT NULL,"
        " spec_toml TEXT NOT NULL, created_at TEXT NOT NULL, spec_path TEXT)"
    )
    c.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,"
        " trigger TEXT NOT NULL, started_at TEXT, finished_at TEXT, manifest_json TEXT, usage_json TEXT)"
    )
    c.execute("INSERT INTO job_specs VALUES ('j', 'n', 'h', 't', 'now', '/p/job.toml')")
    c.execute("INSERT INTO runs (id, job_id, status, trigger) VALUES ('r1', 'j', 'succeeded', 'manual')")
    c.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO schema_meta VALUES ('schema_version', '4')")
    c.commit()
    c.close()
    conn = init_db(v4)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert "parent_run_id" in cols
        # existing rows read as NULL — they are not sub-chickens
        assert conn.execute("SELECT parent_run_id FROM runs WHERE id = 'r1'").fetchone()[0] is None
        ver = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
        assert ver == "5"
        # new rows can carry lineage
        conn.execute(
            "INSERT INTO runs (id, job_id, status, trigger, parent_run_id) VALUES ('r2', 'j', 'queued', 'manual', 'r1')"
        )
        assert conn.execute("SELECT parent_run_id FROM runs WHERE id = 'r2'").fetchone()[0] == "r1"
    finally:
        conn.close()


def test_farmhand_attach_writes_vibe_config_and_token_free_settings(tmp_path) -> None:
    """Pure seam for the attach: the run's vibe config gets the MCP server
    entry spawning `tc farmhand serve --transport stdio`, and the generated
    settings file for the spawned Farmhand never contains the coop token."""
    from turbochickens.adapter import RunContext
    from turbochickens.farmhand import attach_farmhand_to_run, farmhand_serve_command, settings_toml_for_farmhand
    from turbochickens.settings import Settings
    import tomllib

    token = "attach-secrecy-token-27"
    settings = Settings(data_dir=tmp_path / "data", coop_auth_token=token)
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = load_spec(write_job_subs(tmp_path, "attach-egg", "hi", spawn_subs=True))
    run = RunContext(
        run_id="run-attach1",
        job_id="attach-egg",
        spec=spec,
        spec_path=tmp_path / "attach-egg" / "job.toml",
        spec_hash="h",
        trigger="manual",
        workspace=ws,
        env={"TURBOCHICKENS_RUN_ID": "run-attach1"},
        parent_run_id="run-parent1",
    )
    block = attach_farmhand_to_run(run, settings)
    assert block["attached"] is True and block["transport"] == "stdio" and block["server"] == "farmhand"
    cfg = tomllib.loads((ws / ".vibe" / "config.toml").read_text())
    entry = cfg["mcp_servers"]["farmhand"]
    command, args = farmhand_serve_command()
    assert entry["command"] == command and entry["args"] == args
    assert entry["args"][:3] == ["farmhand", "serve", "--transport"] or entry["args"][1:4] == [
        "farmhand",
        "serve",
        "--transport",
    ]
    assert "stdio" in entry["args"]
    # the per-run env points the spawned farmhand at the generated settings
    assert run.env["TURBOCHICKENS_SETTINGS"] == str(ws / ".tc-farmhand-settings.toml")
    assert run.env["TURBOCHICKENS_PARENT_RUN_ID"] == "run-parent1"  # sub-chicken → narrowed scope
    gen = (ws / ".tc-farmhand-settings.toml").read_text()
    assert token not in gen and "coop_auth_token" not in gen
    assert f'data_dir = "{tmp_path / "data"}"' in gen
    # the generated file is itself loadable settings pointing at the same data dir
    s2 = load_settings(ws / ".tc-farmhand-settings.toml")
    assert s2.data_dir == settings.data_dir and s2.socket_path == settings.socket_path
    assert settings_toml_for_farmhand(settings).count("coop_auth_token") == 0


# --- socket-level lineage + refusals -------------------------------------------


def test_enqueue_with_parent_run_id_records_lineage(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    parent_spec = write_job_subs(tmp_path, "lin-parent", "hello")
    sub_spec = write_job_subs(tmp_path, "lin-sub", "hello sub")
    r1 = request(sock, "enqueue", spec_path=str(parent_spec), timeout=10.0)
    assert r1["ok"] is True and "parent_run_id" not in r1
    pid = r1["run_id"]
    r2 = request(sock, "enqueue", spec_path=str(sub_spec), parent_run_id=pid, timeout=10.0)
    assert r2["ok"] is True and r2["parent_run_id"] == pid and r2["weight"] == 1.0  # same admission weight
    sid = r2["run_id"]
    run_p = wait_terminal(sock, pid)
    run_s = wait_terminal(sock, sid)
    assert run_p["status"] == "succeeded" and run_s["status"] == "succeeded"
    # runs store column
    assert db_row(settings, "SELECT parent_run_id FROM runs WHERE id = ?", (sid,)) == (pid,)
    assert db_row(settings, "SELECT parent_run_id FROM runs WHERE id = ?", (pid,)) == (None,)
    # runs_show + runs_list carry lineage
    show_s = request(sock, "runs_show", run_id=sid, timeout=5.0)["run"]
    assert show_s["parent_run_id"] == pid
    assert show_s["manifest"]["parent_run_id"] == pid
    show_p = request(sock, "runs_show", run_id=pid, timeout=5.0)["run"]
    assert show_p["parent_run_id"] is None and "parent_run_id" not in show_p["manifest"]
    by_id = {r["id"]: r for r in request(sock, "runs_list", timeout=5.0)["runs"]}
    assert by_id[sid]["parent_run_id"] == pid and by_id[pid]["parent_run_id"] is None
    # audit: the egg.queued row carries the lineage
    rows = AuditLog(settings.audit_log_path).read_all()
    queued = [r for r in rows if r["event"] == "egg.queued" and r.get("run_id") == sid]
    assert queued and queued[0]["parent_run_id"] == pid
    # unknown parent is refused
    r3 = request(sock, "enqueue", spec_path=str(sub_spec), parent_run_id="run-nope", timeout=10.0)
    assert r3["ok"] is False and "unknown chicken" in r3["error"] and "parent_run_id" in r3["error"]
    # both attested
    for rid in (pid, sid):
        assert db_row(
            settings,
            "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
            (rid,),
        ) == (1, 1, 1)


def test_enqueue_depth_cap_1_and_adapter_restriction(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    parent_spec = write_job_subs(tmp_path, "cap-parent", "hello", spawn_subs=True)
    r = request(sock, "enqueue", spec_path=str(parent_spec), timeout=10.0)
    pid = r["run_id"]
    wait_terminal(sock, pid)
    sub_spec = write_job_subs(tmp_path, "cap-sub", "hello sub")  # spawn_subs absent
    r2 = request(sock, "enqueue", spec_path=str(sub_spec), parent_run_id=pid, timeout=10.0)
    assert r2["ok"] is True
    sid = r2["run_id"]
    wait_terminal(sock, sid)
    # sid is now a sub-chicken: it cannot queue an egg that would itself spawn subs
    spawn_spec = write_job_subs(tmp_path, "cap-spawn", "hello", spawn_subs=True)
    depth_before = request(sock, "status", timeout=5.0)["queue_depth"]
    r3 = request(sock, "enqueue", spec_path=str(spawn_spec), parent_run_id=sid, timeout=10.0)
    assert r3["ok"] is False
    assert "harness.spawn_subs" in r3["error"] and "capped at 1" in r3["error"]
    assert r3["errors"][0]["field"] == "harness.spawn_subs"
    assert request(sock, "status", timeout=5.0)["queue_depth"] == depth_before  # nothing was queued
    # the same egg enqueued by the operator (no parent) is fine — depth cap is about subs spawning
    r4 = request(sock, "enqueue", spec_path=str(spawn_spec), timeout=10.0)
    assert r4["ok"] is True and "parent_run_id" not in r4
    wait_terminal(sock, r4["run_id"])
    # spawn_subs = true with the mistral-api adapter is refused at enqueue (no runtime to attach to)
    api_spec = write_job_subs(tmp_path, "cap-api", "hello", spawn_subs=True, adapter="mistral-api")
    r5 = request(sock, "enqueue", spec_path=str(api_spec), timeout=10.0)
    assert r5["ok"] is False and "harness.spawn_subs" in r5["error"]
    assert r5["errors"][0]["field"] == "harness.spawn_subs"


def test_run_stop_refuses_ancestor_target_for_sub_caller(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    parent_spec = write_job_subs(tmp_path, "anc-parent", "SLEEP 30 hold the line")
    r = request(sock, "enqueue", spec_path=str(parent_spec), timeout=10.0)
    pid = r["run_id"]
    wait_status(sock, pid, ("provisioning", "running"))
    sub_spec = write_job_subs(tmp_path, "anc-sub", "hello")
    r2 = request(sock, "enqueue", spec_path=str(sub_spec), parent_run_id=pid, timeout=10.0)
    sid = r2["run_id"]
    # run_stop on the parent with the sub as caller → refused (ancestor)
    resp = request(sock, "run_stop", run_id=pid, caller_run_id=sid, timeout=10.0)
    assert resp["ok"] is False and "ancestor" in resp["error"]
    # the parent is untouched
    assert request(sock, "runs_show", run_id=pid, timeout=5.0)["run"]["status"] in ("provisioning", "running")
    # run_stop on a non-ancestor with the sub as caller still works (a sibling egg queued next)
    other_spec = write_job_subs(tmp_path, "anc-other", "SLEEP 30 too")
    r3 = request(sock, "enqueue", spec_path=str(other_spec), timeout=10.0)
    oid = r3["run_id"]
    resp2 = request(sock, "run_stop", run_id=oid, caller_run_id=sid, timeout=30.0)
    assert resp2["ok"] is True and resp2["status"] in ("cancelled", "stopped")
    # the operator (no caller context) can still stop the parent
    resp3 = request(sock, "run_stop", run_id=pid, timeout=30.0)
    assert resp3["ok"] is True and resp3["status"] == "stopped"
    wait_terminal(sock, sid)


# --- the e2e: a spawn_subs chicken drives its attached Farmhand over stdio ------


def test_e2e_subspawn_through_attached_farmhand_over_stdio(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    # spec 32 (ticket 02): a chicken-owned connection may only queue
    # allowlisted (uploads-area) spec paths — the operator registers the eggs
    # first and the prompts carry the stored paths
    # grand egg: spawn_subs = true, would be refused when enqueued by the sub (depth cap)
    grand = write_job_subs(tmp_path, "grand-egg", "never runs", spawn_subs=True)
    grand_up = upload_egg(sock, grand)
    # the sub egg: spawn_subs = true so the sub itself gets a Farmhand attached
    sub = write_job_subs(tmp_path, "sub-egg", f"SUBSCOPE {grand_up}", spawn_subs=True)
    sub_up = upload_egg(sock, sub)
    # the parent egg: spawn_subs = true, its fake vibe drives the Farmhand
    parent = write_job_subs(tmp_path, "parent-egg", f"SUBSPAWN {sub_up}", spawn_subs=True)

    resp = request(sock, "enqueue", spec_path=str(parent), timeout=10.0)
    assert resp["ok"] is True and resp["weight"] == 1.0 and "parent_run_id" not in resp
    parent_id = resp["run_id"]
    run_p = wait_terminal(sock, parent_id, timeout=150)
    assert run_p["status"] == "succeeded", run_p["manifest"].get("status_reason")

    # the parent's manifest records the Farmhand attachment
    man_p = run_p["manifest"]
    assert man_p["harness"]["spawn_subs"] is True
    assert man_p["harness"]["farmhand"] == {
        "attached": True,
        "server": "farmhand",
        "transport": "stdio",
        "config": ".vibe/config.toml",
    }
    assert run_p["parent_run_id"] is None

    # the parent's clutch reports the sub-spawn through the Farmhand
    out = request(sock, "results_get", run_id=parent_id, timeout=5.0)
    assert out["ok"] is True
    p = out["output"]
    assert p["enqueue_ok"] is True and p["weight"] == 1.0  # same queue/admission weights
    assert p["sub_parent_run_id"] == parent_id  # the farmhand passed parent_run_id
    assert set(p["tools"]) == EXPECTED_TOOLS  # the parent (not a sub) gets the full tool set
    sub_id = p["sub_run_id"]
    assert p["sub_status"] == "succeeded"

    # lineage in the runs store, runs_show, runs_list, the sub's manifest, and the audit
    assert db_row(settings, "SELECT parent_run_id FROM runs WHERE id = ?", (sub_id,)) == (parent_id,)
    run_s = request(sock, "runs_show", run_id=sub_id, timeout=5.0)["run"]
    assert run_s["status"] == "succeeded"
    assert run_s["parent_run_id"] == parent_id
    assert run_s["manifest"]["parent_run_id"] == parent_id
    assert run_s["manifest"]["harness"]["spawn_subs"] is True
    assert run_s["manifest"]["harness"]["farmhand"]["attached"] is True
    by_id = {r["id"]: r for r in request(sock, "runs_list", timeout=5.0)["runs"]}
    assert by_id[sub_id]["parent_run_id"] == parent_id
    rows = AuditLog(settings.audit_log_path).read_all()
    queued = [r for r in rows if r["event"] == "egg.queued" and r.get("run_id") == sub_id]
    assert queued and queued[0]["parent_run_id"] == parent_id

    # no sub-sub-chicken exists — exactly the two runs
    assert set(by_id) == {parent_id, sub_id}

    # the sub's own Farmhand was sub-scoped: enqueue + read tools + guarded run_stop only
    sub_out = request(sock, "results_get", run_id=sub_id, timeout=5.0)["output"]
    assert set(sub_out["tools"]) == SUB_SCOPE_TOOLS
    assert not (set(sub_out["tools"]) & {"schedule_add", "schedule_list", "schedule_rm", "settings_get", "jobs_validate", "run_rerun"})
    # depth cap: the sub's enqueue of a spawn_subs egg was refused, field-named
    assert "harness.spawn_subs" in (sub_out["depth_refusal"] or "")
    assert sub_out["depth_errors"][0]["field"] == "harness.spawn_subs"
    # ancestor stop: run_stop on the parent from the sub's Farmhand was refused
    assert "ancestor" in (sub_out["stop_refusal"] or "")

    # both chickens went through the full pipeline with attested teardown + valid clutches
    for rid in (parent_id, sub_id):
        assert db_row(
            settings,
            "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
            (rid,),
        ) == (1, 1, 1)
        assert request(sock, "results_get", run_id=rid, timeout=5.0)["ok"] is True
        assert not (settings.data_dir / "workspaces" / rid).exists()
    assert request(sock, "status", timeout=5.0)["alerts"] == []
    # the farmhand attach line is in the parent's run log
    logs = request(sock, "runs_logs", run_id=parent_id, timeout=5.0)["events"]
    assert any("farmhand attached" in e["line"] and "stdio" in e["line"] for e in logs)


def test_stdio_farmhand_with_sub_run_env_is_sub_scoped_and_tags_parent(daemon, tmp_path) -> None:
    """The Farmhand process spawned with a sub-chicken's per-run env: narrowed
    tool listing, enqueue carries parent_run_id, run_stop carries caller_run_id
    (ancestor refusal at the daemon)."""
    sock = daemon.settings.socket_path
    parent_spec = write_job_subs(tmp_path, "fh-parent", "SLEEP 30 hold")  # stays running for the stop probe
    pid = request(sock, "enqueue", spec_path=str(parent_spec), timeout=10.0)["run_id"]
    wait_status(sock, pid, ("provisioning", "running"))
    sub_spec = write_job_subs(tmp_path, "fh-sub", "hello")
    sid = request(sock, "enqueue", spec_path=str(sub_spec), parent_run_id=pid, timeout=10.0)["run_id"]
    wait_terminal(sock, sid)
    assert db_row(daemon.settings, "SELECT parent_run_id FROM runs WHERE id = ?", (sid,)) == (pid,)

    env = dict(os.environ)  # TURBOCHICKENS_SETTINGS from the fixture
    env["TURBOCHICKENS_RUN_ID"] = sid
    env["TURBOCHICKENS_PARENT_RUN_ID"] = pid

    def drive(session: ClientSession):
        async def go():
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == SUB_SCOPE_TOOLS
            # enqueue through the sub's farmhand → parent_run_id = the sub's run id
            egg = write_job_subs(tmp_path, "fh-egg2", "hello")
            p = tool_payload(await session.call_tool("enqueue", {"spec_path": str(egg)}))
            assert p["ok"] is True and p["parent_run_id"] == sid and p["weight"] == 1.0
            # depth cap over MCP: a spawn_subs egg from this sub is refused, field-named
            spawn_egg = write_job_subs(tmp_path, "fh-spawn", "hi", spawn_subs=True)
            p2 = tool_payload(await session.call_tool("enqueue", {"spec_path": str(spawn_egg)}))
            assert p2["ok"] is False and "harness.spawn_subs" in p2["error"]
            assert p2["errors"][0]["field"] == "harness.spawn_subs"
            # ancestor stop over MCP: run_stop on the (still running) parent is refused
            p3 = tool_payload(await session.call_tool("run_stop", {"run_id": pid}))
            assert p3["ok"] is False and "ancestor" in p3["error"]
            # read tools still work and carry lineage
            rl = tool_payload(await session.call_tool("runs_list"))
            by_id = {r["id"]: r for r in rl["runs"]}
            assert by_id[sid]["parent_run_id"] == pid
            await wait_terminal_async(sock, p["run_id"], timeout=60)
        return go()

    try:
        run_stdio_env(drive, env)
    finally:
        # operator stop of the parent (no caller context) still works
        resp = request(sock, "run_stop", run_id=pid, timeout=30.0)
        assert resp["ok"] is True and resp["status"] == "stopped"


def test_full_scope_farmhand_passes_parent_run_id_when_in_run_env(daemon, tmp_path) -> None:
    """A Farmhand inside a non-sub spawn_subs chicken (TURBOCHICKENS_RUN_ID
    set, no parent) keeps the full 17-tool surface and tags enqueue with its
    own run id as parent."""
    sock = daemon.settings.socket_path
    parent_spec = write_job_subs(tmp_path, "full-parent", "hello", spawn_subs=True)
    pid = request(sock, "enqueue", spec_path=str(parent_spec), timeout=10.0)["run_id"]
    wait_terminal(sock, pid)

    env = dict(os.environ)
    env["TURBOCHICKENS_RUN_ID"] = pid  # no TURBOCHICKENS_PARENT_RUN_ID → full scope

    def drive(session: ClientSession):
        async def go():
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS
            egg = write_job_subs(tmp_path, "full-egg", "hello")
            p = tool_payload(await session.call_tool("enqueue", {"spec_path": str(egg)}))
            assert p["ok"] is True and p["parent_run_id"] == pid
            await wait_terminal_async(sock, p["run_id"], timeout=60)
            assert db_row(daemon.settings, "SELECT parent_run_id FROM runs WHERE id = ?", (p["run_id"],)) == (pid,)
        return go()

    run_stdio_env(drive, env)


# --- secrecy --------------------------------------------------------------------


def test_subspawn_carries_no_token_material(tmp_path, monkeypatch) -> None:
    token = "chickensub-spec27-secret-token"
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\ncoop_auth_token = "{token}"\n'
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    bin_path = tmp_path / "fake-vibe-subs"
    bin_path.write_text(FAKE_VIBE_SUBS)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    try:
        sock = settings.socket_path
        sub = write_job_subs(tmp_path, "sec-sub", "hello sub")
        # spec 32 (ticket 02): the sub-egg path the chicken queues is the
        # allowlisted (uploaded) one
        sub_up = upload_egg(sock, sub)
        parent = write_job_subs(tmp_path, "sec-parent", f"SUBSPAWN {sub_up}", spawn_subs=True)
        pid = request(sock, "enqueue", spec_path=str(parent), timeout=10.0)["run_id"]
        run_p = wait_terminal(sock, pid, timeout=150)
        assert run_p["status"] == "succeeded"
        sub_id = request(sock, "results_get", run_id=pid, timeout=5.0)["output"]["sub_run_id"]
        run_s = wait_terminal(sock, sub_id, timeout=60)
        assert run_s["status"] == "succeeded"
        # no token material in manifests, results (incl. the settings_get
        # payload the parent's farmhand read), run events, or the audit log
        blob = json.dumps(
            [
                run_p["manifest"],
                run_s["manifest"],
                request(sock, "results_get", run_id=pid, timeout=5.0)["output"],
                request(sock, "results_get", run_id=sub_id, timeout=5.0)["output"],
                request(sock, "runs_logs", run_id=pid, timeout=5.0)["events"],
                request(sock, "runs_logs", run_id=sub_id, timeout=5.0)["events"],
                request(sock, "runs_list", timeout=5.0)["runs"],
                request(sock, "status", timeout=5.0),
                AuditLog(settings.audit_log_path).read_all(),
            ]
        )
        assert token not in blob
        assert token not in settings.audit_log_path.read_text()
        assert "coop_auth_token" not in blob
    finally:
        dt.stop()
