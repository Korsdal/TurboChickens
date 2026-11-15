"""Tests for spec 32 ticket 23 (review finding G20): resource caps — the Fence.

Seams (external behavior only, highest existing seam first):
- the daemon's JSON-lines unix socket: connection-count cap, idle-keepalive
  cap, per-handler timeout cap, the capped run-event log + bounded `runs_logs`
  tail, and the output (Clutch) size cap with its `truncated` marker;
- the coop HTTP listener: keepalive cap, connection-count cap (503), and
  per-handler timeout (504), plus the `?limit=` mirror of `runs_logs`;
- the real pipeline (in-thread daemon + fake vibe binary) for the run-event
  and output caps end to end.

Every cap gets a negative test (the attack/overload is refused or bounded) and
a positive test (the legitimate path under the caps is unaffected).
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from turbochickens.cli.main import app
from turbochickens.client import request, request_async
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import (
    RUNS_LOGS_TAIL_MAX,
    Settings,
    load_settings,
)

runner = CliRunner()

# A stand-in for the real `vibe` binary. Honors --version, reads the prompt via
# --prompt-file, writes export.json usage into --output-dir, and supports
# prompt markers: "SPAM <n>" prints n log lines (run events), "BIG <n>" prints
# n bytes of non-JSON output (over the output cap), otherwise a small valid
# JSON document.
FAKE_VIBE = '''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, f)
m = re.search(r"BIG (\\d+)", prompt)
if m:
    print("x" * int(m.group(1)))
else:
    m = re.search(r"SPAM (\\d+)", prompt)
    if m:
        for i in range(int(m.group(1))):
            print(f"spam line {i}", file=sys.stderr)
    print(json.dumps({"message": "hello from fake vibe", "echo": prompt.strip()[:40]}))
sys.exit(0)
'''


def write_job(tmp_path: Path, name: str = "caps-job", prompt: str = "Say hello") -> Path:
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test job"

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


def run_id_of(result) -> str:
    m = re.search(r"egg queued (run-[0-9a-f]+)", result.output)
    assert m, f"no run id in output: {result.output}"
    return m.group(1)


def wait_terminal(socket_path, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in ("succeeded", "failed", "cancelled", "stopped"):
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def db_row(settings, sql: str, params=()) -> tuple | None:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def write_settings(tmp_path: Path, **fields: object) -> Path:
    lines = [f'data_dir = "{tmp_path / "data"}"', "weight_budget_override = 4"]
    for k, v in fields.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    f = tmp_path / "settings.toml"
    f.write_text("\n".join(lines) + "\n")
    return f


# --- unix-socket Fence: connection count, keepalive, per-handler time --------


def test_socket_connection_cap_refuses_beyond_limit_then_recovers(daemon, monkeypatch) -> None:
    import turbochickens.daemon as dm

    sock = daemon.settings.socket_path
    limit = dm.SOCKET_MAX_CONNECTIONS

    async def go() -> dict:
        holders = []
        for _ in range(limit):
            r, w = await asyncio.open_unix_connection(str(sock))
            holders.append((r, w))
        try:
            # one connection over the cap: refused with a named error, not a hang
            resp = await request_async(sock, "ping", timeout=5.0)
            assert resp["ok"] is False
            assert "fence is full" in resp["error"]
            return resp
        finally:
            for r, w in holders:
                w.close()
            await asyncio.gather(*(w.wait_closed() for r, w in holders), return_exceptions=True)

    resp = asyncio.run(go())
    assert resp["ok"] is False
    # positive: once the held connections close, the fence has room again
    deadline = time.time() + 5
    ok = None
    while time.time() < deadline:
        ok = request(sock, "ping", timeout=2.0)
        if ok.get("ok"):
            break
        time.sleep(0.1)
    assert ok is not None and ok.get("ok") is True and ok.get("pong") is True


def test_socket_idle_connection_closed_after_keepalive_cap(daemon, monkeypatch) -> None:
    import turbochickens.daemon as dm

    monkeypatch.setattr(dm, "SOCKET_IDLE_TIMEOUT_SEC", 0.3)
    sock = daemon.settings.socket_path

    async def go() -> tuple[bool, bool]:
        r, w = await asyncio.open_unix_connection(str(sock))
        # positive: a request within the keepalive window is answered
        w.write(b'{"op": "ping"}\n')
        await w.drain()
        line = await r.readline()
        assert line and json.loads(line)["ok"] is True
        # negative: after the idle cap the connection is closed (EOF or reset)
        await asyncio.sleep(0.8)
        closed = False
        try:
            w.write(b'{"op": "ping"}\n')
            await w.drain()
            rest = await r.read()
            closed = rest == b""
        except (ConnectionResetError, BrokenPipeError):
            closed = True
        w.close()
        return True, closed

    answered, closed = asyncio.run(go())
    assert answered is True
    assert closed is True, "idle socket connection was not closed after the keepalive cap"


def test_socket_handler_timeout_answers_error_instead_of_hanging(daemon, monkeypatch) -> None:
    import turbochickens.daemon as dm

    monkeypatch.setattr(dm, "SOCKET_HANDLER_TIMEOUT_SEC", 0.3)
    orig_respond = dm.Daemon._respond

    async def slow_respond(self, line: bytes, caller=None):
        # caller: the SO_PEERCRED identity param added by the ticket-10 merge —
        # _handle_client calls _respond(line, caller), so the stub must accept it
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception:
            req = {}
        if req.get("op") == "ping":
            await asyncio.sleep(2.0)  # wedge this one op past the handler cap
        return await orig_respond(self, line, caller)

    monkeypatch.setattr(dm.Daemon, "_respond", slow_respond)
    sock = daemon.settings.socket_path

    t0 = time.time()
    resp = request(sock, "ping", timeout=10.0)
    elapsed = time.time() - t0
    assert resp["ok"] is False
    assert "swamped" in resp["error"]  # the Fox cut it short — a named refusal
    assert elapsed < 1.5, f"handler timeout did not fire (took {elapsed:.2f}s)"
    # positive: other ops on fresh connections are unaffected
    status = request(sock, "status", timeout=5.0)
    assert status["ok"] is True


# --- run-event cap + bounded runs_logs tail (socket seam, real pipeline) ------


def test_run_event_log_capped_and_runs_logs_serves_bounded_tail(daemon, tmp_path, monkeypatch) -> None:
    import turbochickens.daemon as dm
    import turbochickens.runner as rn

    cap = 8
    monkeypatch.setattr(rn, "MAX_RUN_EVENTS", cap)
    monkeypatch.setattr(dm, "MAX_RUN_EVENTS", cap)
    sock = daemon.settings.socket_path

    spec = write_job(tmp_path, "spam-job", prompt="SPAM 30")
    result = runner.invoke(app, ["run", str(spec), "--wait"])
    assert result.exit_code == 0, result.output
    run_id = run_id_of(result)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    # the stored log itself is capped (on-disk DB state)
    count = db_row(daemon.settings, "SELECT COUNT(*) FROM run_events WHERE run_id = ?", (run_id,))
    assert count is not None and count[0] == cap

    # default: the whole (capped) log is served, flagged capped
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert logs["ok"] is True
    assert logs["total"] == cap and logs["capped"] is True and logs["truncated"] is False
    assert len(logs["events"]) == cap
    assert [e["seq"] for e in logs["events"]] == list(range(cap))

    # LIMIT: the bounded tail — last 3 events, marked truncated
    tail = request(sock, "runs_logs", run_id=run_id, limit=3, timeout=2.0)
    assert tail["ok"] is True
    assert tail["limit"] == 3 and tail["total"] == cap and tail["truncated"] is True
    assert [e["seq"] for e in tail["events"]] == [cap - 3, cap - 2, cap - 1]

    # a huge LIMIT is clamped to the hard tail max, never unbounded
    big = request(sock, "runs_logs", run_id=run_id, limit=10**9, timeout=2.0)
    assert big["ok"] is True
    assert big["limit"] == RUNS_LOGS_TAIL_MAX and len(big["events"]) == cap and big["truncated"] is False

    # negative: a non-positive / non-integer limit is refused with a named error
    for bad in (0, -1, "abc", 1.5, True):
        resp = request(sock, "runs_logs", run_id=run_id, limit=bad, timeout=2.0)
        assert resp["ok"] is False and "positive integer" in resp["error"]


def test_runs_logs_uncapped_run_serves_everything(daemon, tmp_path) -> None:
    """Positive: a normal run under the caps is unaffected — every event is
    served, nothing flagged."""
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, "hello-job", prompt="Say hello")
    result = runner.invoke(app, ["run", str(spec), "--wait"])
    assert result.exit_code == 0, result.output
    run_id = run_id_of(result)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert logs["ok"] is True
    assert logs["capped"] is False and logs["truncated"] is False
    assert logs["total"] == len(logs["events"])
    assert any(e["level"] == "output" and "hello from fake vibe" in e["line"] for e in logs["events"])
    # a small explicit LIMIT still works and keeps the newest events
    tail = request(sock, "runs_logs", run_id=run_id, limit=2, timeout=2.0)
    assert tail["ok"] is True and tail["truncated"] is True
    assert [e["seq"] for e in tail["events"]] == [logs["total"] - 2, logs["total"] - 1]


# --- output (Clutch) size cap with the truncated marker -----------------------


def test_output_cap_truncates_stores_and_marks(daemon, tmp_path, monkeypatch) -> None:
    import turbochickens.adapter as ad
    import turbochickens.runner as rn

    cap = 1024
    monkeypatch.setattr(ad, "OUTPUT_MAX_BYTES", cap)
    monkeypatch.setattr(rn, "OUTPUT_MAX_BYTES", cap)
    sock = daemon.settings.socket_path

    spec = write_job(tmp_path, "big-job", prompt="BIG 5000")
    result = runner.invoke(app, ["run", str(spec), "--wait"])
    assert result.exit_code == 1, result.output  # invalid (truncated) JSON → quarantined
    run_id = run_id_of(result)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "failed"

    # the marker is recorded in the manifest budget
    budget = run["manifest"]["budget"]
    assert budget["output_truncated"] is True
    assert budget["output_cap_bytes"] == cap

    # the served quarantine payload is capped, never unbounded
    res = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert res["ok"] is False and res["quarantined"] is True
    assert len(res["raw_output"]) == cap

    # the DB row and the on-disk quarantine file are capped too
    row = db_row(daemon.settings, "SELECT output_json FROM results WHERE run_id = ?", (run_id,))
    assert row is not None and len(row[0]) == cap
    qfile = daemon.settings.quarantine_dir / run_id / "raw_output.txt"
    assert qfile.exists() and qfile.stat().st_size == cap

    # and the run log records the truncation
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert any("truncated=true" in e["line"] for e in logs["events"])


def test_small_output_under_cap_is_stored_whole(daemon, tmp_path) -> None:
    """Positive: normal output under the cap is unaffected — full clutch, no
    truncation marker."""
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, "small-job", prompt="Say hello")
    result = runner.invoke(app, ["run", str(spec), "--wait"])
    assert result.exit_code == 0, result.output
    run_id = run_id_of(result)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"
    assert "output_truncated" not in run["manifest"]["budget"]
    res = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert res["ok"] is True and res["valid"] is True
    assert res["output"]["message"] == "hello from fake vibe"
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert not any("truncated=true" in e["line"] for e in logs["events"])


# --- coop HTTP Fence: keepalive, connection count, per-handler timeout --------


class HangTransport:
    """Transport seam whose `status` op wedges (simulating a swamped daemon);
    every other op answers immediately."""

    def __init__(self, hang_sec: float = 30.0) -> None:
        self.hang_sec = hang_sec

    async def request(self, op: str, **payload: Any) -> dict[str, Any]:
        if op == "status":
            await asyncio.sleep(self.hang_sec)
        return {"ok": True, "op": op, "protocol": 1, "pong": op == "ping"}


def _coop_settings(tmp_path, monkeypatch, **fields: object):
    # open-mode HTTP tests: loopback bind + the coop_loopback_open opt-in
    # (bearer-by-default is covered separately in test_coop_bearer_default)
    fields.setdefault("coop_loopback_open", True)
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), **fields)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return load_settings(f)


def test_coop_handler_timeout_returns_504_not_hang(tmp_path, monkeypatch) -> None:
    import turbochickens.coop as cp

    monkeypatch.setattr(cp, "COOP_HANDLER_TIMEOUT_SEC", 0.3)
    settings = _coop_settings(tmp_path, monkeypatch)
    server = CoopServer(settings, transport=HangTransport(hang_sec=30.0))

    async def go() -> tuple[int, dict, float, int, dict]:
        await server.start()
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=10.0
            ) as c:
                t0 = time.time()
                r = await c.get("/api/status")
                elapsed = time.time() - t0
                # positive: a fast op on the same listener is unaffected
                ping = await c.get("/api/ping")
                return r.status_code, r.json(), elapsed, ping.status_code, ping.json()
        finally:
            await server.stop()

    code, body, elapsed, ping_code, ping_body = asyncio.run(go())
    assert code == 504
    assert body["ok"] is False and "swamped" in body["error"]
    assert elapsed < 5.0, f"handler timeout did not fire (took {elapsed:.2f}s)"
    assert ping_code == 200 and ping_body["pong"] is True


def test_coop_connection_cap_returns_503_then_recovers(tmp_path, monkeypatch) -> None:
    import turbochickens.coop as cp

    monkeypatch.setattr(cp, "COOP_MAX_CONNECTIONS", 2)
    settings = _coop_settings(tmp_path, monkeypatch)
    server = CoopServer(settings, transport=HangTransport(hang_sec=2.0))
    port = settings.coop_port

    async def get_close(path: str = "/api/status") -> tuple[int, str]:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        data = await r.read()
        w.close()
        head = data.split(b"\r\n", 1)[0].decode()
        code = int(head.split()[1])
        return code, data.decode("utf-8", "replace")

    async def go() -> tuple[int, str, int, int]:
        await server.start()
        try:
            # two in-flight requests fill the fence (they hang in the transport)
            t1 = asyncio.create_task(get_close())
            t2 = asyncio.create_task(get_close())
            await asyncio.sleep(0.4)
            over_code, over_body = await get_close()
            r1, _ = await t1
            r2, _ = await t2
            # positive: once they finish, the fence has room again
            after_code, _ = await get_close()
            return over_code, over_body, r1, after_code
        finally:
            await server.stop()

    over_code, over_body, r1, after_code = asyncio.run(go())
    assert over_code == 503
    assert "fence is full" in over_body
    assert r1 == 200  # the in-flight ones completed normally
    assert after_code == 200


def test_coop_keepalive_cap_closes_idle_connection(tmp_path, monkeypatch) -> None:
    import turbochickens.coop as cp

    monkeypatch.setattr(cp, "COOP_KEEPALIVE_TIMEOUT_SEC", 0.3)
    settings = _coop_settings(tmp_path, monkeypatch)
    server = CoopServer(settings, transport=HangTransport(hang_sec=0.0))
    port = settings.coop_port

    async def go() -> tuple[bool, bool, bool]:
        await server.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            req = b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"  # keep-alive

            async def read_resp() -> bytes:
                buf = b""
                while b"}" not in buf:
                    chunk = await r.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                return buf

            w.write(req)
            await w.drain()
            first = await read_resp()
            # positive: a second request within the keepalive window works
            w.write(req)
            await w.drain()
            second = await read_resp()
            # negative: past the keepalive cap the idle connection is closed
            await asyncio.sleep(1.0)
            closed = False
            try:
                w.write(req)
                await w.drain()
                tail = await r.read()
                closed = tail == b""
            except (ConnectionResetError, BrokenPipeError):
                closed = True
            w.close()
            return b"200" in first, b"200" in second, closed
        finally:
            await server.stop()

    first_ok, second_ok, closed = asyncio.run(go())
    assert first_ok and second_ok
    assert closed, "idle keep-alive connection was not closed after the keepalive cap"


# --- coop HTTP mirror of the bounded runs_logs tail ----------------------------


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch, fake_vibe):
    # open-mode HTTP: loopback bind + the coop_loopback_open opt-in
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def test_http_logs_mirror_matches_socket_tail_and_rejects_bad_limit(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    spec = write_job(tmp_path, "http-logs-job", prompt="Say hello")
    result = runner.invoke(app, ["run", str(spec), "--wait"])
    assert result.exit_code == 0, result.output
    run_id = run_id_of(result)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    with httpx.Client(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0) as c:
        # parity: HTTP payload == socket payload (default tail)
        http_logs = c.get(f"/api/runs/{run_id}/logs").json()
        sock_logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
        assert http_logs == sock_logs
        assert sock_logs["capped"] is False and sock_logs["truncated"] is False

        # ?limit= forwards to the bounded tail, HTTP == socket
        http_tail = c.get(f"/api/runs/{run_id}/logs?limit=2").json()
        sock_tail = request(sock, "runs_logs", run_id=run_id, limit=2, timeout=2.0)
        assert http_tail == sock_tail
        assert http_tail["limit"] == 2 and http_tail["truncated"] is True
        assert [e["seq"] for e in http_tail["events"]] == [sock_logs["total"] - 2, sock_logs["total"] - 1]

        # negative: a non-integer limit is a 400 (like /api/audit)
        bad = c.get(f"/api/runs/{run_id}/logs?limit=abc")
        assert bad.status_code == 400
