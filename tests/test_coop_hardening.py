"""Tests for spec 32 ticket 05 — Coop request hardening, security headers,
bare ping, uniform /mcp, path-param precedence (review G3 transport + G17
ping + G19 + G21 headers).

Seams (highest existing first): the pure Host/Origin/Referer validation
functions; the HTTP surface of real thread daemons on temp loopback ports
driven with httpx exactly like the browser (open mode via the
``coop_loopback_open`` opt-in, and bearer-required mode with a configured
token on a wildcard bind); a standalone ``CoopServer`` with a recording
transport for the path-param precedence payload; a dead-socket transport for
the bare 503. All assertions are external behavior: status codes, payload
shapes, response headers, what the socket op actually received.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from turbochickens.coop import (
    CoopServer,
    SocketTransport,
    host_allowed,
    origin_allowed,
    referer_allowed,
    split_host_port,
)
from turbochickens.fox import SECURITY_HEADERS
from turbochickens.settings import Settings, load_settings

MCP_INIT_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}},
}

COOP_TOKEN = "spec05-coop-token-do-not-serve"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def open_daemon(tmp_path, monkeypatch):
    """Loopback coop, no token, coop_loopback_open opt-in → open (wave-1 rule)."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def auth_daemon(tmp_path, monkeypatch):
    """Wildcard bind + configured token → bearer required (also proves the
    loopback Host 127.0.0.1 is accepted on a wildcard bind)."""
    f = write_settings(
        tmp_path, coop_enabled=True, coop_host="0.0.0.0", coop_port=free_port(), coop_auth_token=COOP_TOKEN
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def _raw_http(port: int, raw: bytes) -> bytes:
    """One raw HTTP/1.1 request on a fresh connection, read to EOF."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        s.sendall(raw)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        s.close()
    return b"".join(chunks)


def _assert_security_headers(r: httpx.Response) -> None:
    for key, value in SECURITY_HEADERS.items():
        assert r.headers.get(key) == value, (key, dict(r.headers))


# --- pure seams: Host / Origin / Referer validation ----------------------------


def test_split_host_port() -> None:
    assert split_host_port("127.0.0.1:8737") == ("127.0.0.1", "8737")
    assert split_host_port("127.0.0.1") == ("127.0.0.1", None)
    assert split_host_port("[::1]:8737") == ("::1", "8737")
    assert split_host_port("[::1]") == ("::1", None)
    assert split_host_port("::1") == ("::1", None)
    assert split_host_port("LocalHost:80") == ("localhost", "80")
    assert split_host_port("") == ("", None)


def test_host_validation_pure_seam() -> None:
    s = Settings(coop_host="127.0.0.1", coop_port=8737)
    # own host:port, host without port, loopback names/IPv6
    assert host_allowed("127.0.0.1:8737", s) is True
    assert host_allowed("127.0.0.1", s) is True
    assert host_allowed("localhost:8737", s) is True
    assert host_allowed("[::1]:8737", s) is True
    # rebinding names, wrong port, missing header
    assert host_allowed("evil.example.com:8737", s) is False
    assert host_allowed("evil.example.com", s) is False
    assert host_allowed("127.0.0.1:9999", s) is False
    assert host_allowed(None, s) is False
    assert host_allowed("", s) is False
    # a non-loopback IP literal is not "our own" on a loopback bind
    assert host_allowed("192.168.1.5:8737", s) is False
    # wildcard bind: any interface IP is our own, foreign names still refused
    w = Settings(coop_host="0.0.0.0", coop_port=8737)
    assert host_allowed("192.168.1.5:8737", w) is True
    assert host_allowed("127.0.0.1:8737", w) is True
    assert host_allowed("evil.example.com:8737", w) is False
    # a specific non-loopback bind accepts that address
    n = Settings(coop_host="192.168.1.5", coop_port=8737)
    assert host_allowed("192.168.1.5:8737", n) is True
    assert host_allowed("10.0.0.9:8737", n) is False


def test_origin_referer_validation_pure_seam() -> None:
    s = Settings(coop_host="127.0.0.1", coop_port=8737)
    own = f"http://127.0.0.1:{s.coop_port}"
    assert origin_allowed(own, s) is True
    assert origin_allowed("http://localhost:8737", s) is True
    assert origin_allowed("http://evil.example.com", s) is False
    assert origin_allowed("http://evil.example.com:8737", s) is False
    assert origin_allowed("http://127.0.0.1:9999", s) is False  # wrong port
    assert origin_allowed("null", s) is False  # sandboxed iframe
    assert origin_allowed("https://127.0.0.1:8737", s) is False  # the coop serves plain http
    assert origin_allowed("", s) is False
    assert referer_allowed(own + "/", s) is True
    assert referer_allowed("http://localhost:8737/app", s) is True
    assert referer_allowed("https://evil.example.com/x", s) is False
    assert referer_allowed("about:blank", s) is False


# --- Content-Type gate: JSON only as application/json ---------------------------


def test_json_body_requires_application_json(open_daemon) -> None:
    c = http_client(open_daemon.settings)
    # negative: a CSRF-style text/plain POST carrying a JSON body → 415, refused
    # before parsing (CORS-safelisted: no preflight needed)
    r = c.post("/api/runs", content=b'{"spec_path": "/nope"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and r.json()["ok"] is False
    # negative: missing Content-Type entirely → 415 as well
    r = c.post("/api/runs", content=b'{"spec_path": "/nope"}')
    assert r.status_code == 415
    # nothing was enqueued by the refused writes
    assert c.get("/api/runs").json()["runs"] == []
    # the gate covers every body-reading POST mirror, not just /api/runs
    assert c.post("/api/settings", content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/connectors/rm", content=b'{"name": "x"}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/jobs/validate", content=b'{"files": {}}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.post("/api/auth/logout", content=b'{"provider": "x"}', headers={"Content-Type": "text/plain"}).status_code == 415
    # positive: application/json (parameters allowed) is parsed as before
    r = c.post("/api/runs", content=b'{"spec_path": "/nope"}', headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 422  # the daemon's refusal — the body was read
    r = c.post("/api/runs", json={"spec_path": "/nope"})
    assert r.status_code == 422
    # malformed JSON with the right Content-Type is still a clean 400
    assert c.post("/api/runs", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400


# --- Origin / Referer / Host rejection over HTTP --------------------------------


def test_foreign_origin_referer_host_refused(open_daemon) -> None:
    c = http_client(open_daemon.settings)
    port = open_daemon.settings.coop_port
    own = f"http://127.0.0.1:{port}"
    # positive: the coop's own origin/referer pass (same-origin browser traffic)
    assert c.get("/api/status", headers={"Origin": own}).status_code == 200
    assert c.get("/api/status", headers={"Referer": own + "/"}).status_code == 200
    assert c.get("/api/status", headers={"Origin": own, "Referer": own + "/"}).status_code == 200
    # negative: foreign Origin on a read and on a write (the G3 drive-by vector)
    r = c.get("/api/status", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403 and r.json()["ok"] is False
    r = c.post("/api/runs", json={"spec_path": "/nope"}, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    assert c.get("/api/status", headers={"Origin": "null"}).status_code == 403
    assert c.get("/api/status", headers={"Referer": "https://evil.example.com/page"}).status_code == 403
    assert c.get("/api/status", headers={"Origin": f"http://127.0.0.1:{port + 1}"}).status_code == 403
    # static assets are covered too — a foreign page cannot even load the shell
    assert c.get("/", headers={"Origin": "http://evil.example.com"}).status_code == 403
    assert c.get("/api/ping", headers={"Origin": "http://evil.example.com"}).status_code == 403
    # nothing was enqueued by the refused write
    assert c.get("/api/runs").json()["runs"] == []
    # Host validation: DNS rebinding sends the attacker's name in Host
    assert c.get("/api/ping", headers={"Host": f"evil.example.com:{port}"}).status_code == 403
    assert c.get("/api/ping", headers={"Host": f"127.0.0.1:{port + 1}"}).status_code == 403  # wrong port
    # positive: own host:port, and a port-less Host (raw HTTP/1.0-style) pass
    assert c.get("/api/ping", headers={"Host": f"127.0.0.1:{port}"}).status_code == 200
    assert c.get("/api/ping", headers={"Host": "127.0.0.1"}).status_code == 200
    assert c.get("/api/ping", headers={"Host": "localhost"}).status_code == 200
    # raw socket: a request with no Host header at all is refused by the HTTP
    # protocol layer itself (400, before routing — nothing to serve), and a
    # port-less own Host passes the fence
    resp = _raw_http(port, b"GET /api/ping HTTP/1.1\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.0 400"), resp[:60]  # aiohttp: "Missing 'Host' header"
    resp = _raw_http(port, b"GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200"), resp[:60]


# --- security headers on every response -----------------------------------------


def test_security_headers_on_every_response_open_mode(open_daemon) -> None:
    c = http_client(open_daemon.settings)
    cases = [
        c.get("/"),  # 200 static
        c.get("/app.js"),
        c.get("/api/ping"),  # 200 open route
        c.get("/api/status"),  # 200 API
        c.get("/nope"),  # 404
        c.get("/api/status", headers={"Origin": "http://evil.example.com"}),  # 403
        c.post("/api/runs", content=b"{}", headers={"Content-Type": "text/plain"}),  # 415
        c.post("/api/runs", json={}),  # 400 malformed
    ]
    assert [r.status_code for r in cases] == [200, 200, 200, 200, 404, 403, 415, 400]
    for r in cases:
        _assert_security_headers(r)


def test_security_headers_on_every_response_auth_mode(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    bearer = {"Authorization": f"Bearer {COOP_TOKEN}"}
    cases = [
        c.get("/api/status"),  # 401 no bearer
        c.get("/api/status", headers={"Authorization": "Bearer wrong"}),  # 401
        c.get("/api/status", headers={**bearer, "Origin": "http://evil.example.com"}),  # 403
        c.post("/api/runs", content=b"{}", headers={**bearer, "Content-Type": "text/plain"}),  # 415
        c.get("/nope", headers=bearer),  # 404
        c.get("/", headers=bearer),  # 200 static (login card route)
        c.get("/api/status", headers=bearer),  # 200 positive
    ]
    assert [r.status_code for r in cases] == [401, 401, 403, 415, 404, 200, 200]
    for r in cases:
        _assert_security_headers(r)
    # the mounted MCP endpoint's streamed response carries them too (ASGI bridge)
    r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, **bearer})
    assert r.status_code == 200 and r.headers.get("mcp-session-id")
    _assert_security_headers(r)


def test_security_headers_on_dead_socket_responses(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), farmhand_transport="stdio", coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> list[httpx.Response]:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0) as c:
                return [await c.get("/api/ping"), await c.get("/api/status"), await c.get("/nope")]
        finally:
            await server.stop()

    ping, status, notfound = asyncio.run(go())
    assert ping.status_code == 503 and status.status_code == 200 and notfound.status_code == 404
    for r in (ping, status, notfound):
        _assert_security_headers(r)


# --- bare ping -------------------------------------------------------------------


def test_api_ping_is_a_bare_pong(open_daemon) -> None:
    c = http_client(open_daemon.settings)
    r = c.get("/api/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "op": "ping", "pong": True}
    # marker sweep: no version, pid, uptime, error, or host path in the body
    for key in ("version", "pid", "uptime_sec", "error"):
        assert key not in r.json()
    assert str(open_daemon.settings.data_dir) not in r.text
    assert "daemon.sock" not in r.text
    _assert_security_headers(r)


def test_api_ping_dead_socket_is_a_bare_503(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), farmhand_transport="stdio", coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> httpx.Response:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0) as c:
                return await c.get("/api/ping")
        finally:
            await server.stop()

    r = asyncio.run(go())
    assert r.status_code == 503
    assert r.json() == {"ok": False, "op": "ping", "pong": False}  # no error/version/pid/path either
    assert str(tmp_path) not in r.text


# --- uniform /mcp: no mount-state oracle ------------------------------------------


def _start_daemon(base: Path, name: str, monkeypatch, **fields) -> DaemonThread:
    """One thread daemon under its own subdir (two daemons in one test must
    not share a data dir)."""
    sub = base / name
    sub.mkdir()
    f = write_settings(sub, coop_enabled=True, coop_host="0.0.0.0", coop_auth_token=COOP_TOKEN, **fields)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    return dt


def test_unmounted_mcp_answers_uniformly(tmp_path, monkeypatch) -> None:
    dt_un = _start_daemon(tmp_path, "unmounted", monkeypatch, coop_port=free_port(), farmhand_transport="stdio")
    dt_mounted = _start_daemon(tmp_path, "mounted", monkeypatch, coop_port=free_port())
    try:
        c_un = http_client(dt_un.settings)
        c_mounted = http_client(dt_mounted.settings)
        bearer = {"Authorization": f"Bearer {COOP_TOKEN}"}

        # without the bearer, mounted and unmounted /mcp answer the SAME 401 —
        # the route is protected even while unmounted, so it leaks no mount state
        r_un = c_un.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        r_mounted = c_mounted.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        assert r_un.status_code == 401 and r_mounted.status_code == 401
        assert r_un.json() == r_mounted.json() == {"ok": False, "error": "unauthorized: send Authorization: Bearer <token>"}
        _assert_security_headers(r_un)

        # with the bearer: mounted answers MCP, unmounted answers the same generic
        # 404 any unknown path gets (no farmhand_transport value in the body)
        r = c_mounted.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, **bearer})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")
        r = c_un.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, **bearer})
        assert r.status_code == 404
        assert r.json() == {"ok": False, "error": "not found"}
        assert "farmhand_transport" not in r.text and "stdio" not in r.text
        # subpaths of the endpoint answer the same way while unmounted
        r = c_un.post("/mcp/", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, **bearer})
        assert r.status_code == 404 and r.json() == {"ok": False, "error": "not found"}
        # the GUI still serves next to the unmounted endpoint
        assert c_un.get("/").status_code == 200
        assert c_un.get("/api/ping").status_code == 200
    finally:
        dt_un.stop()
        dt_mounted.stop()


# --- path parameters win over same-named body keys -------------------------------


class RecordingTransport:
    """CoopTransport seam that records exactly what the web layer sends to the
    socket op — the external contract of the POST mirrors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def request(self, op: str, **payload):
        self.calls.append((op, payload))
        return {"ok": True, "op": op, "status": "stopped"}


def test_path_param_overrides_same_named_body_key(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), farmhand_transport="stdio", coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    rec = RecordingTransport()
    server = CoopServer(settings, transport=rec)

    async def go() -> tuple[int, dict, int, dict]:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0) as c:
                # the body tries to redirect the op to another chicken — the path wins
                r1 = await c.post("/api/runs/run-from-path/stop", json={"run_id": "run-from-body", "caller_run_id": "run-caller"})
                r2 = await c.post("/api/runs/run-from-path/rerun", json={"run_id": "run-from-body"})
                return r1.status_code, r1.json(), r2.status_code, r2.json()
        finally:
            await server.stop()

    code1, body1, code2, body2 = asyncio.run(go())
    assert code1 == 200 and body1["ok"] is True
    assert code2 == 200 and body2["ok"] is True
    assert rec.calls[-2][0] == "run_stop" and rec.calls[-1][0] == "run_rerun"
    stop_payload = dict(rec.calls[-2][1])
    rerun_payload = dict(rec.calls[-1][1])
    assert stop_payload["run_id"] == "run-from-path"  # NOT run-from-body
    assert rerun_payload["run_id"] == "run-from-path"
    # positive: body keys the path did not set are still forwarded
    assert stop_payload["caller_run_id"] == "run-caller"


# --- extended 401 matrix in bearer-required mode ----------------------------------


def test_extended_401_matrix_with_hardening_on_top(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    bearer = {"Authorization": f"Bearer {COOP_TOKEN}"}
    port = auth_daemon.settings.coop_port
    own = f"http://127.0.0.1:{port}"

    # the intentionally open routes stay open (and bare / header-carrying)
    r = c.get("/api/ping")
    assert r.status_code == 200 and r.json() == {"ok": True, "op": "ping", "pong": True}
    _assert_security_headers(r)
    assert c.get("/").status_code == 200
    assert c.get("/app.js").status_code == 200

    # every socket-mirrored route refuses without the bearer (401 matrix)
    for path in (
        "/api/status", "/api/jobs", "/api/audit", "/api/settings", "/api/runs",
        "/api/connectors", "/api/schedule", "/api/stats", "/api/auth/providers",
        "/api/auth/access", "/api/farmhand",
    ):
        assert c.get(path).status_code == 401, path
    assert c.post("/api/runs", json={"spec_path": "/nope"}).status_code == 401
    assert c.post("/api/settings", json={"farmhand_transport": "https"}).status_code == 401
    assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401  # mounted /mcp

    # the hardening sits on top of auth: a bearer holder with a foreign
    # Origin/Host or a non-JSON Content-Type is still refused
    assert c.get("/api/status", headers={**bearer, "Origin": "http://evil.example.com"}).status_code == 403
    assert c.get("/api/status", headers={**bearer, "Referer": "https://evil.example.com/"}).status_code == 403
    assert c.get("/api/status", headers={**bearer, "Host": f"evil.example.com:{port}"}).status_code == 403
    assert c.post("/api/runs", content=b"{}", headers={**bearer, "Content-Type": "text/plain"}).status_code == 415
    # and without the bearer a foreign origin is refused by the fence first (403)
    assert c.get("/api/status", headers={"Origin": "http://evil.example.com"}).status_code == 403

    # positive: bearer + own origin + JSON content type → business as usual
    r = c.get("/api/status", headers={**bearer, "Origin": own, "Referer": own + "/"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = c.post("/api/connectors/rm", json={"name": "no-such-gate"}, headers=bearer)
    assert r.status_code == 422  # the daemon's refusal — the body was read and forwarded
