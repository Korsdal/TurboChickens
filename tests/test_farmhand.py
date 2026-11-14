"""Tests for spec 26: the Farmhand — an MCP server (official `mcp` SDK) whose
tools are thin wrappers over the daemon's existing socket ops via
`client.request_async`, served over stdio (`tc farmhand serve --transport
stdio`, gated by the 0600 daemon socket) and over HTTPS (streamable-HTTP at
`farmhand_https_path` on the coop listener, behind the same bearer middleware
as /api/*, loopback-open rule included).

Seams: a real daemon on a temp socket (thread daemon for the stdio tests, a
real `tc coop start` subprocess for the HTTPS tests), driven by a real MCP
client (the SDK's `stdio_client` / `streamable_http_client` + `ClientSession`)
against a real `tc farmhand serve` subprocess, with the fake vibe binary via
$TURBOCHICKENS_VIBE_BIN. Assertions are external, observable behavior only:
MCP tool listings + tool result payloads (== the socket op responses), HTTP
status codes, CLI output, store/audit contents.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import free_port, write_settings
from test_daemon import DaemonThread

from test_run_e2e import FAKE_VIBE, combined_output, write_job
from test_schedule import write_cron_job
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request, request_async
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import Settings, SettingsError, load_settings
from turbochickens.spec.loader import load_spec, spec_hash

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

runner = CliRunner()

#: the exact MCP tool surface: one thin wrapper per socket op — narrower than
#: the GUI bearer: no auth_* mutations, no retention_sweep, no shutdown/ping,
#: and no raw jobs_upload (jobs_validate wraps it)
EXPECTED_TOOLS = {
    "status",
    "jobs_list",
    "jobs_validate",
    "enqueue",
    "runs_list",
    "runs_show",
    "runs_logs",
    "results_get",
    "run_stop",
    "run_rerun",
    "schedule_add",
    "schedule_list",
    "schedule_rm",
    "stats_list",
    "stats_show",
    "audit_tail",
    "settings_get",
}


def tool_payload(result) -> dict:
    """The tool result payload: the socket response, returned verbatim as the
    tool's JSON text content."""
    assert not result.is_error, f"tool errored: {result.content}"
    texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    assert texts, f"no text content in tool result: {result.content}"
    return json.loads(texts[0])


async def _call(session: ClientSession, name: str, args: dict | None = None, read_timeout: float | None = None):
    return await session.call_tool(name, args or {}, read_timeout_seconds=read_timeout)


async def sock_call(op: str, timeout: float = 2.0, **payload):
    """Sync-free socket call, usable inside the MCP client's event loop."""
    return await request_async(SOCKET[0], op, timeout=timeout, **payload)


async def wait_terminal_async(run_id: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await sock_call("runs_show", run_id=run_id)
        if resp.get("ok") and resp["run"]["status"] in TERMINAL_STATES:
            return resp["run"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state within {timeout}s")


async def wait_status_async(run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await sock_call("runs_show", run_id=run_id)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


SOCKET: list = [None]  # set per test: the daemon socket path (for sock_call)


async def https_session(url: str, fn, token: str | None = None):
    """Async MCP streamable-HTTP client session at `url` — usable both from
    asyncio.run (top level) and inside another running loop (the stdio test).
    No client-side timeout: the streamable-HTTP GET stream is long-lived, so a
    read timeout would kill idle-but-healthy sessions; the caller's wait_for
    bounds the whole exchange."""
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=None) as hc:
        async with streamable_http_client(url, http_client=hc) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)


def run_https(url: str, fn, token: str | None = None, timeout: float = 120.0):
    return asyncio.run(asyncio.wait_for(https_session(url, fn, token), timeout))


def run_stdio(fn, transport: str = "stdio", timeout: float = 120.0):
    """Run `fn(session)` against a real `tc farmhand serve --transport …`
    subprocess (the SDK's MCP stdio client)."""

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "turbochickens", "farmhand", "serve", "--transport", transport],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(asyncio.wait_for(main(), timeout))


MCP_INIT_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}},
}


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def settings_file_budget1(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings-budget1.toml"
    f.write_text(f'data_dir = "{tmp_path / "data1"}"\nweight_budget_override = 1\n')
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
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()
    SOCKET[0] = None


def _wait_socket(settings, proc) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        if settings.socket_path.exists():
            try:
                if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                    return
            except DaemonUnreachable:
                pass
        if proc.poll() is not None:
            raise AssertionError("coop subprocess exited early")
        time.sleep(0.05)
    raise AssertionError("coop subprocess did not start answering")


def _wait_http(port: int, proc) -> None:
    """Wait until the coop HTTP listener answers (the socket can be up first)."""
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.0).status_code == 200:
                return
        except httpx.ConnectError:
            pass
        if proc.poll() is not None:
            raise AssertionError("coop subprocess exited early")
        time.sleep(0.05)
    raise AssertionError("coop HTTP listener did not come up")


def _stop_subprocess(settings, proc) -> None:
    try:
        request(settings.socket_path, "shutdown", timeout=3.0)
    except DaemonUnreachable:
        pass
    try:
        proc.wait(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("coop subprocess did not shut down within 45s")
    assert proc.returncode == 0


# --- settings seam: the farmhand block ----------------------------------------


def test_farmhand_settings_block_pure_seam(tmp_path) -> None:
    s = Settings()
    assert s.farmhand_transport == "both"
    assert s.farmhand_https_path == "/mcp"
    assert s.public_dict()["farmhand_transport"] == "both"
    assert s.public_dict()["farmhand_https_path"] == "/mcp"

    f = tmp_path / "s.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "d"}"\nfarmhand_transport = "https"\nfarmhand_https_path = "/farm"\n'
    )
    s = load_settings(f)
    assert s.farmhand_transport == "https" and s.farmhand_https_path == "/farm"
    assert s.public_dict()["farmhand_https_path"] == "/farm"

    # enum enforced, field-named
    f.write_text(f'data_dir = "{tmp_path / "d"}"\nfarmhand_transport = "carrier-pigeon"\n')
    with pytest.raises(SettingsError) as ei:
        load_settings(f)
    assert "farmhand_transport" in str(ei.value)

    # extra = "forbid" stays: unknown keys anywhere are rejected
    f.write_text(f'data_dir = "{tmp_path / "d"}"\nfarmhand_bogus = 1\n')
    with pytest.raises(SettingsError) as ei:
        load_settings(f)
    assert "farmhand_bogus" in str(ei.value)

    # path shape: single absolute segment
    for bad in ("mcp", "/mcp/", "/a//b", "/../x", "/a/../b", "a\\b", "/"):
        f.write_text(f'data_dir = "{tmp_path / "d"}"\nfarmhand_https_path = "{bad}"\n')
        with pytest.raises(SettingsError) as ei:
            load_settings(f)
        assert "farmhand_https_path" in str(ei.value), bad
    for good in ("/mcp", "/farm-hand", "/mcp_v2"):
        f.write_text(f'data_dir = "{tmp_path / "d"}"\nfarmhand_https_path = "{good}"\n')
        assert load_settings(f).farmhand_https_path == good

    # every transport value parses
    for t in ("stdio", "https", "both"):
        f.write_text(f'data_dir = "{tmp_path / "d"}"\nfarmhand_transport = "{t}"\n')
        assert load_settings(f).farmhand_transport == t


# --- CLI: tc farmhand serve ----------------------------------------------------


def test_cli_farmhand_serve_transport_validation(tmp_path, monkeypatch) -> None:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    result = runner.invoke(app, ["farmhand", "--help"])
    assert result.exit_code == 0 and "serve" in result.output
    result = runner.invoke(app, ["farmhand", "serve", "--transport", "bogus"])
    assert result.exit_code == 1
    assert "--transport must be one of stdio, https, both" in combined_output(result)


# --- stdio: real MCP client against `tc farmhand serve --transport stdio` -----


def test_stdio_tool_surface_and_socket_parity(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, name="stdio-egg", prompt="Say hello")

    def drive(session: ClientSession):
        async def go():
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS
            # the narrower MCP scope: no auth mutations, no retention_sweep,
            # no shutdown/ping, no raw jobs_upload
            names = {t.name for t in tools.tools}
            assert not any(n.startswith("auth_") for n in names)
            assert not names & {"retention_sweep", "shutdown", "ping", "jobs_upload"}

            # status: same payload as the socket op (uptime aside)
            p = tool_payload(await _call(session, "status"))
            s = await sock_call("status")
            p["daemon"].pop("uptime_sec"), s["daemon"].pop("uptime_sec")
            assert p == s

            # read-only ops: MCP payload == socket payload
            for name, args in [
                ("jobs_list", {}),
                ("runs_list", {}),
                ("runs_list", {"job": "stdio-egg"}),
                ("settings_get", {}),
                ("audit_tail", {"limit": 20}),
                ("audit_tail", {"limit": 10, "event": "egg.queued"}),
                ("schedule_list", {}),
                ("stats_list", {}),
            ]:
                mcp = tool_payload(await _call(session, name, args))
                direct = await sock_call(name, **args)
                assert mcp == direct, name

            # enqueue over MCP → chicken runs → inspect over MCP, == socket
            p = tool_payload(await _call(session, "enqueue", {"spec_path": str(spec)}))
            assert p["ok"] is True and p["status"] == "queued" and p["weight"] == 1.0
            rid = p["run_id"]
            run = await wait_terminal_async(rid, timeout=30)
            assert run["status"] == "succeeded"
            assert tool_payload(await _call(session, "runs_show", {"run_id": rid})) == await sock_call(
                "runs_show", run_id=rid
            )
            assert tool_payload(await _call(session, "runs_logs", {"run_id": rid})) == await sock_call(
                "runs_logs", run_id=rid
            )
            res = tool_payload(await _call(session, "results_get", {"run_id": rid}))
            assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"
            assert res == await sock_call("results_get", run_id=rid)

            # stats after a finished chicken
            assert tool_payload(await _call(session, "stats_list")) == await sock_call("stats_list")
            st = tool_payload(await _call(session, "stats_show", {"job_id": "stdio-egg"}))
            assert st == await sock_call("stats_show", job_id="stdio-egg")
            assert st["stats"]["n_runs"] == 1

            # jobs_validate by files bundle → stored → enqueueable by that path
            files = {
                "job.toml": spec.read_text(),
                "prompts/hello.txt": (spec.parent / "prompts" / "hello.txt").read_text(),
                "schemas/out.schema.json": (spec.parent / "schemas" / "out.schema.json").read_text(),
            }
            p = tool_payload(await _call(session, "jobs_validate", {"files": files}))
            assert p["ok"] is True and p["valid"] is True
            assert p["spec_hash"] == spec_hash(load_spec(spec))
            stored = Path(p["spec_path"])
            assert stored.is_file() and stored.parent.parent == daemon.settings.uploads_dir
            p2 = tool_payload(await _call(session, "enqueue", {"spec_path": str(stored)}))
            assert p2["ok"] is True and p2["job_id"] == "stdio-egg"

            # jobs_validate by local spec_path (same-machine agent) → same op
            p3 = tool_payload(await _call(session, "jobs_validate", {"spec_path": str(spec)}))
            assert p3["ok"] is True and p3["valid"] is True and p3["spec_hash"] == p["spec_hash"]

            # jobs_validate refuses a bad bundle with field-named errors
            p4 = tool_payload(await _call(session, "jobs_validate", {"files": {"job.toml": "[job]\nname = 'x'\n"}}))
            assert p4["ok"] is False and p4.get("valid") is False and p4["errors"]

            # schedules over MCP, == socket
            cron = write_cron_job(tmp_path, name="stdio-cron", schedule="0 7 * * 1")
            p = tool_payload(await _call(session, "schedule_add", {"spec_path": str(cron)}))
            assert p["ok"] is True and p["schedule"] == "0 7 * * 1" and p["job_id"] == "stdio-cron"
            assert tool_payload(await _call(session, "schedule_list")) == await sock_call("schedule_list")
            p = tool_payload(await _call(session, "schedule_rm", {"job_id": "stdio-cron"}))
            assert p["ok"] is True
            assert tool_payload(await _call(session, "schedule_list"))["schedules"] == []

            # errors pass through as the socket response (ok:false), not MCP errors
            p = tool_payload(await _call(session, "runs_show", {"run_id": "run-nope"}))
            assert p["ok"] is False and "unknown chicken" in p["error"]

        return go()

    run_stdio(drive)


def test_stdio_run_stop_and_run_rerun_over_mcp(settings_file_budget1, fake_vibe, tmp_path) -> None:
    settings = load_settings(settings_file_budget1)
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)
    dt.start()
    try:
        sock = settings.socket_path
        slow = write_job(tmp_path, name="mcp-slow", prompt="SLEEP 8 working")

        def drive(session: ClientSession):
            async def go():
                p1 = tool_payload(await _call(session, "enqueue", {"spec_path": str(slow)}))
                p2 = tool_payload(await _call(session, "enqueue", {"spec_path": str(slow)}))
                id1, id2 = p1["run_id"], p2["run_id"]
                # budget 1: id1 runs, id2 backlogs in the pecking order
                await wait_status_async(id1, ("provisioning", "running"))
                # run_stop on the queued egg over MCP → cancelled, never hatched
                p = tool_payload(await _call(session, "run_stop", {"run_id": id2}, read_timeout=30.0))
                assert p["ok"] is True and p["op"] == "run_stop" and p["status"] == "cancelled"
                assert "never hatched" in p["reason"]
                # run_rerun over MCP → fresh chicken with lineage → succeeded
                p = tool_payload(await _call(session, "run_rerun", {"run_id": id2}))
                assert p["ok"] is True and p["rerun_of"] == id2 and p["status"] == "queued"
                new = p["run_id"]
                run = await wait_terminal_async(new, timeout=40)
                assert run["status"] == "succeeded" and run["manifest"]["rerun_of"] == id2
                res = tool_payload(await _call(session, "results_get", {"run_id": new}))
                assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"
                # stop on an already-terminal chicken: clear error payload
                p = tool_payload(await _call(session, "run_stop", {"run_id": new}, read_timeout=30.0))
                assert p["ok"] is False and "already terminal" in p["error"]
                await wait_terminal_async(id1, timeout=40)
            return go()

        run_stdio(drive)
    finally:
        dt.stop()
        SOCKET[0] = None


def test_stdio_secrecy_no_token_in_any_tool_payload(tmp_path, monkeypatch, fake_vibe) -> None:
    token = "farmhand-spec26-secret-token"
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\ncoop_auth_token = "{token}"\n'
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)
    dt.start()
    try:
        spec = write_job(tmp_path, name="secret-egg", prompt="Say hello")

        def drive(session: ClientSession):
            async def go():
                # queue one chicken so runs_list/audit_tail/stats carry data
                p = tool_payload(await _call(session, "enqueue", {"spec_path": str(spec)}))
                rid = p["run_id"]
                await wait_terminal_async(rid, timeout=30)
                payloads = []
                for name, args in [
                    ("status", {}),
                    ("settings_get", {}),
                    ("jobs_list", {}),
                    ("runs_list", {}),
                    ("runs_show", {"run_id": rid}),
                    ("runs_logs", {"run_id": rid}),
                    ("results_get", {"run_id": rid}),
                    ("audit_tail", {"limit": 50}),
                    ("schedule_list", {}),
                    ("stats_list", {}),
                    ("stats_show", {"job_id": "secret-egg"}),
                ]:
                    payloads.append(tool_payload(await _call(session, name, args)))
                blob = json.dumps(payloads)
                assert token not in blob
                s = payloads[1]["settings"]
                assert "coop_auth_token" not in s  # the key itself is never served
                assert s["farmhand_transport"] == "both" and s["farmhand_https_path"] == "/mcp"
            return go()

        run_stdio(drive)
        # and the token never reached the audit log either
        assert token not in settings.audit_log_path.read_text()
    finally:
        dt.stop()
        SOCKET[0] = None


# --- HTTPS: streamable-HTTP at /mcp on the coop listener ------------------------


def test_https_on_coop_listener_bearer_required_and_mcp_parity(tmp_path, monkeypatch, fake_vibe) -> None:
    token = "coop-bearer-secret-26"
    port = free_port()
    f = write_settings(tmp_path, coop_port=port, coop_auth_token=token)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_socket(settings, proc)
        _wait_http(port, proc)
        sock = settings.socket_path
        url = f"http://127.0.0.1:{port}/mcp"
        spec = write_job(tmp_path, name="https-egg", prompt="Say hello")

        # bearer required on the /mcp endpoint (token configured, even on loopback)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401
        r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")
        assert c.get("/api/ping").status_code == 200  # healthcheck stays open

        def drive(session: ClientSession):
            async def go():
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                p = tool_payload(await _call(session, "enqueue", {"spec_path": str(spec)}))
                rid = p["run_id"]
                run = await wait_terminal_async(rid, timeout=30)
                assert run["status"] == "succeeded"
                # MCP payload == socket payload
                assert tool_payload(await _call(session, "runs_show", {"run_id": rid})) == await sock_call(
                    "runs_show", run_id=rid
                )
                res = tool_payload(await _call(session, "results_get", {"run_id": rid}))
                assert res == await sock_call("results_get", run_id=rid)
                assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"
                assert tool_payload(await _call(session, "runs_list")) == await sock_call("runs_list")
                assert tool_payload(await _call(session, "settings_get")) == await sock_call("settings_get")
                # secrecy over HTTPS: the bearer never appears in any tool payload
                blobs = []
                for name, args in [
                    ("status", {}), ("settings_get", {}), ("jobs_list", {}), ("runs_list", {}),
                    ("audit_tail", {"limit": 50}), ("schedule_list", {}), ("stats_list", {}),
                ]:
                    blobs.append(json.dumps(tool_payload(await _call(session, name, args))))
                assert token not in "".join(blobs)
            return go()

        run_https(url, drive, token=token)
    finally:
        _stop_subprocess(settings, proc)
        SOCKET[0] = None
    assert token not in settings.audit_log_path.read_text()


def test_https_loopback_open_without_token_and_stdio_transport_not_mounted(tmp_path, monkeypatch, fake_vibe) -> None:
    # loopback + no token → open, exactly like /api/*
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_socket(settings, proc)
        _wait_http(port, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        r = c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        assert r.status_code == 200  # no bearer needed on loopback without a token

        def drive(session: ClientSession):
            async def go():
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                p = tool_payload(await _call(session, "status"))
                assert p["ok"] is True and p["daemon"]["up"] is True
            return go()

        run_https(f"http://127.0.0.1:{port}/mcp", drive)  # no token
    finally:
        _stop_subprocess(settings, proc)
        SOCKET[0] = None

    # farmhand_transport = "stdio" → the coop listener does NOT serve /mcp
    port2 = free_port()
    f2 = write_settings(tmp_path, coop_port=port2, farmhand_transport="stdio")
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f2))
    settings2 = load_settings(f2)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_socket(settings2, proc)
        _wait_http(port2, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port2}", timeout=10.0)
        r = c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        assert r.status_code in (404, 405)  # not mounted; the GUI still serves
        assert c.get("/api/ping").status_code == 200
        assert c.get("/").status_code == 200
    finally:
        _stop_subprocess(settings2, proc)


def test_https_non_loopback_ephemeral_token_gates_mcp(tmp_path, monkeypatch, fake_vibe) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_host="0.0.0.0", coop_port=port)  # no token → ephemeral
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    lines: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        _wait_socket(settings, proc)
        _wait_http(port, proc)
        deadline = time.time() + 10
        token = None
        while time.time() < deadline:
            tl = [l for l in lines if l.startswith("coop auth token: ")]
            if tl:
                token = tl[0].split(": ", 1)[1].strip()
                break
            time.sleep(0.05)
        assert token, f"no ephemeral token printed; stdout so far: {lines}"

        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        assert c.get("/api/ping").status_code == 200  # healthcheck open
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401
        r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200

        def drive(session: ClientSession):
            async def go():
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                p = tool_payload(await _call(session, "status"))
                assert p["ok"] is True and p["coop"]["auth_required"] is True
                s = tool_payload(await _call(session, "settings_get"))
                assert s["settings"]["farmhand_transport"] == "both"
                assert token not in json.dumps(s)  # the bearer never appears in a tool payload
            return go()

        run_https(f"http://127.0.0.1:{port}/mcp", drive, token=token)
        # printed exactly once in the whole run; never in the audit log
        assert len([l for l in lines if l.startswith("coop auth token: ")]) == 1
        assert token not in settings.audit_log_path.read_text()
    finally:
        _stop_subprocess(settings, proc)
        reader.join(timeout=5)
        SOCKET[0] = None


# --- "both": stdio + HTTPS served simultaneously by one farmhand process --------


def test_farmhand_serve_both_transports_simultaneously(tmp_path, monkeypatch, fake_vibe) -> None:
    port = free_port()
    f = tmp_path / "settings.toml"
    # loopback + no token → the standalone https side is open like /api/*
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\ncoop_port = {port}\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)  # coop disabled here, so the port is free for the farmhand process
    dt.start()
    try:
        sock = settings.socket_path
        spec = write_job(tmp_path, name="both-egg", prompt="Say hello")

        def drive(session: ClientSession):
            async def go():
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                p = tool_payload(await _call(session, "status"))
                assert p["ok"] is True and p["daemon"]["up"] is True
                # enqueue over stdio; the chicken runs on the daemon
                p = tool_payload(await _call(session, "enqueue", {"spec_path": str(spec)}))
                rid = p["run_id"]
                run = await wait_terminal_async(rid, timeout=30)
                assert run["status"] == "succeeded"
                res = tool_payload(await _call(session, "results_get", {"run_id": rid}))
                assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"

                # while the stdio session is open, the same process's HTTPS
                # endpoint answers MCP too (loopback open, no token)
                def drive_https(session2: ClientSession):
                    async def go2():
                        tools2 = await session2.list_tools()
                        assert {t.name for t in tools2.tools} == EXPECTED_TOOLS
                        p2 = tool_payload(await _call(session2, "status"))
                        assert p2["ok"] is True and p2["daemon"]["up"] is True
                        assert tool_payload(await _call(session2, "runs_list")) == await sock_call("runs_list")
                        assert tool_payload(await _call(session2, "results_get", {"run_id": rid})) == await sock_call(
                            "results_get", run_id=rid
                        )
                    return go2()

                await https_session(f"http://127.0.0.1:{port}/mcp", drive_https)
            return go()

        run_stdio(drive, transport="both")

        # when the stdio client hangs up, the "both" process exits (https side
        # included): the port stops answering
        deadline = time.time() + 10
        refused = False
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=1.0)
            except httpx.ConnectError:
                refused = True
                break
            time.sleep(0.1)
        assert refused, "the farmhand https listener still answers after the stdio client left"
    finally:
        dt.stop()
        SOCKET[0] = None


# --- spec 32 / G6: informational output on stderr only, stdout protocol-only --
#
# In every transport mode (`stdio`, `https`, `both`) the Farmhand's
# informational lines — including the ephemeral coop-token notice — must land
# on stderr, so the stdio MCP channel stays protocol-clean in `both` mode and
# the bearer never lands in client-side logs of the protocol stream.

_TEE_WRAPPER = r"""
import os, subprocess, sys, threading

def pump(src, *dsts):
    fd = src.fileno()
    while True:
        chunk = os.read(fd, 65536)  # returns as soon as any bytes are available
        if not chunk:
            break
        for d in dsts:
            d.write(chunk)
            d.flush()

out_cap = open(sys.argv[1], "wb")
err_cap = open(sys.argv[2], "wb")
proc = subprocess.Popen(
    [sys.executable, "-m", "turbochickens", "farmhand", "serve", "--transport", sys.argv[3]],
    stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
t1 = threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer, out_cap), daemon=True)
t2 = threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer, err_cap), daemon=True)
t1.start(); t2.start()
rc = proc.wait()
t1.join(timeout=5); t2.join(timeout=5)
out_cap.close(); err_cap.close()
sys.exit(rc)
"""


def run_stdio_capture(fn, transport: str, out_cap: Path, err_cap: Path, timeout: float = 120.0):
    """Like run_stdio, but the serve subprocess's stdout/stderr are teed into
    capture files, so the test can assert which stream each line landed on."""

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", _TEE_WRAPPER, str(out_cap), str(err_cap), transport],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    return asyncio.run(asyncio.wait_for(main(), timeout))


def assert_stdout_protocol_only(out_cap: Path) -> None:
    """Every stdout line of a stdio-serving Farmhand is one JSON-RPC frame —
    the protocol channel carries nothing else (no token notice, no info)."""
    lines = [l for l in out_cap.read_bytes().splitlines() if l.strip()]
    assert lines, "no protocol frames captured on stdout"
    for line in lines:
        frame = json.loads(line)  # a non-protocol line (e.g. the token) breaks this
        assert isinstance(frame, dict) and "jsonrpc" in frame, f"non-protocol line on stdout: {line[:120]!r}"


def _token_from_stderr(err_cap: Path, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tl = [l for l in err_cap.read_text(errors="replace").splitlines() if l.startswith("coop auth token: ")]
        if tl:
            return tl[0].split(": ", 1)[1].strip()
        time.sleep(0.05)
    raise AssertionError(f"no ephemeral token line on stderr; stderr so far: {err_cap.read_text(errors='replace')!r}")


def test_serve_both_ephemeral_token_on_stderr_only_and_clean_stdio(tmp_path, monkeypatch, fake_vibe) -> None:
    # G6: non-loopback host + no configured token → an ephemeral bearer is
    # minted; in `both` mode (the default) that notice used to go to stdout,
    # corrupting the stdio MCP channel. Now: stderr only, stdout protocol-only.
    port = free_port()
    f = write_settings(tmp_path, coop_host="0.0.0.0", coop_port=port)  # no token → ephemeral
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)  # coop disabled here → the port is the farmhand process's own
    dt.start()
    out_cap = tmp_path / "stdout.cap"
    err_cap = tmp_path / "stderr.cap"
    try:
        spec = write_job(tmp_path, name="g6-egg", prompt="Say hello")

        def drive(session: ClientSession):
            async def go():
                # the token notice is already on stderr while the stdio
                # session is up — read it there and prove it gates the HTTPS
                # side of the very same process
                token = _token_from_stderr(err_cap)
                c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
                assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401
                r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, "Authorization": f"Bearer {token}"})
                assert r.status_code == 200 and r.headers.get("mcp-session-id")

                # a clean stdio MCP session on the same channel: tools + a
                # full enqueue → clutch round trip
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS
                p = tool_payload(await _call(session, "status"))
                assert p["ok"] is True and p["daemon"]["up"] is True
                p = tool_payload(await _call(session, "enqueue", {"spec_path": str(spec)}))
                rid = p["run_id"]
                run = await wait_terminal_async(rid, timeout=30)
                assert run["status"] == "succeeded"
                res = tool_payload(await _call(session, "results_get", {"run_id": rid}))
                assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"
                return token
            return go()

        token = run_stdio_capture(drive, "both", out_cap, err_cap)

        # stdout carried only JSON-RPC frames; the bearer never touched it
        assert_stdout_protocol_only(out_cap)
        stdout_text = out_cap.read_text(errors="replace")
        assert not any(l.startswith("coop auth token:") for l in stdout_text.splitlines())
        assert token not in stdout_text
        # the notice is on stderr, exactly once, and never in the Headcount
        stderr_text = err_cap.read_text(errors="replace")
        assert len([l for l in stderr_text.splitlines() if l.startswith("coop auth token: ")]) == 1
        assert f"coop auth token: {token}" in stderr_text
        assert token not in settings.audit_log_path.read_text()
    finally:
        dt.stop()
        SOCKET[0] = None


def test_serve_stdio_stdout_protocol_only_informational_on_stderr(daemon, tmp_path) -> None:
    # stdio mode: the CLI's informational "farmhand serving …" line is on
    # stderr; stdout carries only MCP frames
    out_cap = tmp_path / "stdout.cap"
    err_cap = tmp_path / "stderr.cap"

    def drive(session: ClientSession):
        async def go():
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED_TOOLS
            p = tool_payload(await _call(session, "status"))
            assert p["ok"] is True and p["daemon"]["up"] is True
        return go()

    run_stdio_capture(drive, "stdio", out_cap, err_cap)
    assert_stdout_protocol_only(out_cap)
    stdout_text = out_cap.read_text(errors="replace")
    assert not any(l.startswith("coop auth token:") for l in stdout_text.splitlines())
    assert "farmhand serving" not in stdout_text
    err_text = err_cap.read_text(errors="replace")
    assert "farmhand serving transport=stdio" in err_text  # informational line on stderr


def test_serve_https_informational_and_token_on_stderr_only(tmp_path, monkeypatch, fake_vibe) -> None:
    # https mode on a non-loopback host with no configured token: both the
    # "farmhand serving …" line and the ephemeral token notice are stderr-only;
    # stdout stays empty (no protocol channel here, but no leaks either)
    port = free_port()
    f = write_settings(tmp_path, coop_host="0.0.0.0", coop_port=port)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    SOCKET[0] = settings.socket_path
    dt = DaemonThread(settings)
    dt.start()
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "farmhand", "serve", "--transport", "https"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain(stream, sink) -> None:
        for line in stream:
            sink.append(line.rstrip("\n"))

    r1 = threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True)
    r2 = threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True)
    r1.start()
    r2.start()
    try:
        # wait until the listener answers 401 (bearer required, ephemeral token)
        deadline = time.time() + 15
        up = False
        while time.time() < deadline:
            try:
                if httpx.post(f"http://127.0.0.1:{port}/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS, timeout=1.0).status_code == 401:
                    up = True
                    break
            except httpx.ConnectError:
                pass
            if proc.poll() is not None:
                raise AssertionError("farmhand https subprocess exited early")
            time.sleep(0.05)
        assert up, "the farmhand https listener did not come up"

        token = _token_from_lines(err_lines)
        # the bearer from stderr gates the endpoint; stdout stayed clean
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401
        r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")
        time.sleep(0.3)  # let the drain threads see everything written so far
        assert not [l for l in out_lines if l.strip()], f"informational output leaked to stdout: {out_lines}"
        assert not any("coop auth token" in l for l in out_lines)
        assert any("farmhand serving transport=https" in l for l in err_lines)
        assert len([l for l in err_lines if l.startswith("coop auth token: ")]) == 1
        assert token not in settings.audit_log_path.read_text()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        r1.join(timeout=5)
        r2.join(timeout=5)
        dt.stop()
        SOCKET[0] = None


def _token_from_lines(lines: list[str], timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        tl = [l for l in lines if l.startswith("coop auth token: ")]
        if tl:
            return tl[0].split(": ", 1)[1].strip()
        time.sleep(0.05)
    raise AssertionError(f"no ephemeral token line on stderr; stderr so far: {lines}")
