"""Tests for spec 32 ticket 18 (review finding G14): the Coop→Coopd transport
enforces its timeout around the whole round trip and never 500s or hangs.

Seams (external behavior only):
- the socket client itself (`request_async` / `request`) against fake coopd
  unix-socket servers: wedged (never answers) and over-limit (response line
  longer than the protocol limit) daemons both raise DaemonUnreachable — the
  structured unreachable-daemon error — within the configured timeout;
- the Coop HTTP seam: a CoopServer whose SocketTransport points at those fake
  daemons keeps its contract — every handler answers the structured
  ``200 {ok:false, daemon.up:false}`` payload (GETs and POSTs, never a 500,
  never a hang), and a healthy daemon round trips unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from test_coop import free_port, write_settings
from turbochickens.client import DaemonUnreachable, request, request_async
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import SOCKET_STREAM_LIMIT, load_settings

TIMEOUT = 0.2  # short round-trip budget for the wedged-daemon tests


async def _serve(path: Path, handler) -> tuple[asyncio.AbstractServer, set[asyncio.Task]]:
    """One fake coopd on a unix socket: `handler(reader, writer)` per conn.
    Returns the server plus its handler tasks so teardown can cancel them
    (a wedged handler never finishes on its own)."""

    tasks: set[asyncio.Task] = set()

    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _wrap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            tasks.add(task)
        try:
            await _on_client(reader, writer)
        finally:
            if task is not None:
                tasks.discard(task)

    server = await asyncio.start_unix_server(_wrap, path=str(path))
    return server, tasks


async def _stop(srv: asyncio.AbstractServer, tasks: set[asyncio.Task]) -> None:
    srv.close()
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _healthy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A healthy coopd: answer one JSON-lines request, echoing the op."""
    line = await reader.readline()
    req = json.loads(line.decode("utf-8"))
    resp = {"ok": True, "op": req["op"], "marker": "healthy-round-trip"}
    if req["op"] == "ping":
        resp = {"ok": True, "op": "ping", "pong": True}
    writer.write(json.dumps(resp).encode("utf-8") + b"\n")
    await writer.drain()


async def _wedged(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A wedged coopd: swallow the request, never answer, never close."""
    await reader.readline()
    await asyncio.sleep(3600)


async def _over_limit(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A coopd whose response line exceeds the protocol stream limit."""
    await reader.readline()
    writer.write(b"x" * (SOCKET_STREAM_LIMIT + 1))  # no newline: readline overruns
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass


def _coop_settings(tmp_path: Path, monkeypatch) -> object:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), farmhand_transport="stdio")
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return load_settings(f)


# --- socket client seam ------------------------------------------------------


def test_request_async_wedged_daemon_times_out_instead_of_hanging(tmp_path) -> None:
    async def go() -> tuple[float, str]:
        srv, tasks = await _serve(tmp_path / "wedged.sock", _wedged)
        try:
            start = time.monotonic()
            try:
                await request_async(tmp_path / "wedged.sock", "ping", timeout=TIMEOUT)
                raise AssertionError("wedged daemon answered?")
            except DaemonUnreachable as e:
                return time.monotonic() - start, str(e)
        finally:
            await _stop(srv, tasks)

    elapsed, msg = asyncio.run(go())
    assert "timed out" in msg
    assert elapsed < 2.0  # bounded by the configured timeout, not forever


def test_request_sync_wedged_daemon_times_out(tmp_path) -> None:
    async def go() -> None:
        srv, tasks = await _serve(tmp_path / "wedged.sock", _wedged)
        try:
            with pytest.raises(DaemonUnreachable, match="timed out"):
                # the sync client runs its own loop — in a worker thread here
                await asyncio.to_thread(request, tmp_path / "wedged.sock", "ping", TIMEOUT)
        finally:
            await _stop(srv, tasks)

    asyncio.run(go())


def test_request_async_over_limit_response_is_unreachable_not_value_error(tmp_path) -> None:
    async def go() -> str:
        srv, tasks = await _serve(tmp_path / "overlimit.sock", _over_limit)
        try:
            try:
                await request_async(tmp_path / "overlimit.sock", "ping", timeout=5.0)
                raise AssertionError("over-limit response parsed?")
            except DaemonUnreachable as e:
                return str(e)
        finally:
            await _stop(srv, tasks)

    msg = asyncio.run(go())
    assert "exceeds" in msg and "protocol limit" in msg


def test_request_async_healthy_daemon_round_trip_unchanged(tmp_path) -> None:
    async def go() -> dict:
        srv, tasks = await _serve(tmp_path / "healthy.sock", _healthy)
        try:
            return await request_async(tmp_path / "healthy.sock", "status", timeout=2.0)
        finally:
            await _stop(srv, tasks)

    resp = asyncio.run(go())
    assert resp == {"ok": True, "op": "status", "marker": "healthy-round-trip"}


# --- Coop HTTP seam -----------------------------------------------------------


def test_coop_http_healthy_daemon_round_trips_unchanged(tmp_path, monkeypatch) -> None:
    settings = _coop_settings(tmp_path, monkeypatch)

    async def go() -> tuple[int, dict, int, dict]:
        srv, tasks = await _serve(tmp_path / "healthy.sock", _healthy)
        server = CoopServer(settings, transport=SocketTransport(tmp_path / "healthy.sock", timeout=2.0))
        try:
            await server.start()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0
            ) as c:
                st = await c.get("/api/status")
                ping = await c.get("/api/ping")
                return st.status_code, st.json(), ping.status_code, ping.json()
        finally:
            await server.stop()
            await _stop(srv, tasks)

    st_code, st_body, ping_code, ping_body = asyncio.run(go())
    assert st_code == 200
    assert st_body == {"ok": True, "op": "status", "marker": "healthy-round-trip"}
    assert ping_code == 200 and ping_body["pong"] is True


def test_coop_http_wedged_daemon_answers_structured_payload_within_timeout(tmp_path, monkeypatch) -> None:
    settings = _coop_settings(tmp_path, monkeypatch)

    async def go() -> tuple[float, int, dict, int, dict, int, dict]:
        srv, tasks = await _serve(tmp_path / "wedged.sock", _wedged)
        server = CoopServer(settings, transport=SocketTransport(tmp_path / "wedged.sock", timeout=TIMEOUT))
        try:
            await server.start()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0
            ) as c:
                start = time.monotonic()
                st = await c.get("/api/status")
                get_elapsed = time.monotonic() - start
                post = await c.post("/api/runs/run-abc/stop", json={})
                ping = await c.get("/api/ping")
                return (
                    get_elapsed,
                    st.status_code,
                    st.json(),
                    post.status_code,
                    post.json(),
                    ping.status_code,
                    ping.json(),
                )
        finally:
            await server.stop()
            await _stop(srv, tasks)

    get_elapsed, st_code, st_body, post_code, post_body, ping_code, ping_body = asyncio.run(go())
    # GET and POST both keep the 200 {ok:false, daemon.up:false} contract —
    # never a 500, never a hang — and answer within the configured timeout
    assert st_code == 200 and post_code == 200
    for body, op in ((st_body, "status"), (post_body, "run_stop")):
        assert body["ok"] is False
        assert body["daemon"]["up"] is False
        assert body["op"] == op
        assert "timed out" in body["error"]
    assert get_elapsed < 2.0
    assert ping_code == 503 and ping_body["pong"] is False


def test_coop_http_over_limit_response_is_structured_payload_not_500(tmp_path, monkeypatch) -> None:
    settings = _coop_settings(tmp_path, monkeypatch)

    async def go() -> tuple[int, dict, int, dict]:
        srv, tasks = await _serve(tmp_path / "overlimit.sock", _over_limit)
        server = CoopServer(settings, transport=SocketTransport(tmp_path / "overlimit.sock", timeout=5.0))
        try:
            await server.start()
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=10.0
            ) as c:
                st = await c.get("/api/status")
                post = await c.post("/api/retention/sweep")
                return st.status_code, st.json(), post.status_code, post.json()
        finally:
            await server.stop()
            await _stop(srv, tasks)

    st_code, st_body, post_code, post_body = asyncio.run(go())
    # the over-limit response maps to the structured daemon-down payload on
    # GET and POST alike — an uncaught ValueError would surface as a 500
    assert st_code == 200 and post_code == 200
    for body in (st_body, post_body):
        assert body["ok"] is False
        assert body["daemon"]["up"] is False
        assert "exceeds" in body["error"]
