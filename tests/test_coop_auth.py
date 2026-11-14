"""Tests for spec 19: auth & access management in the coop GUI.

Seams (per spec 11/19 testing decisions — no browser automation):
- the HTTP API against a real coop-enabled daemon booted in a thread on a temp
  port, with a hermetic loopback fake OAuth server (the spec-09 one) behind a
  seat provider in providers.toml: GET /api/auth/providers, GET /api/auth/access,
  POST /api/auth/login, POST /api/auth/login/status, POST /api/auth/logout —
  with socket-op parity (auth_providers / auth_access / auth_login_start /
  auth_login_poll / auth_logout), 422 refusals, 400 malformed bodies;
- the device login flow end-to-end over HTTP (auto-approve and pending-then-
  approve), the session stored 0600 under <data_dir>/seats/ exactly as the CLI
  path, audit rows perch.claimed / perch.released carrying provider + mode only;
- an assertion that no response body (and no audit row) ever contains token or
  device-code material;
- auth-required 401s when bound non-loopback with a token, dead-socket
  daemon-down payloads (never 500) on all five endpoints, served SPA markup,
  the per-run auth_mode surfaced in runs_list (HTTP == socket), and a
  subprocess e2e (`tc coop start` + fake OAuth): the whole login → logout
  flow over HTTP, then a seat run whose runs-list row reads auth_mode=seat.

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, socket payloads, file modes, audit contents, served assets.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import _wait_socket, free_port, http_client, write_settings
from test_coop_runs import enqueue_over_http, fake_vibe, wait_terminal_http
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, write_job
from test_seat_auth import (
    SEAT_PROVIDERS_TOML,
    FakeAPIHandler,
    FakeOAuthHandler,
    _serve,
    wait_terminal,
    write_seat_job,
)
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings

runner = CliRunner()

AUTH_HEADERS = {"Authorization": "Bearer spec19-secret-token"}

#: secret strings that must never appear in any API response or audit row
SECRET_MARKERS = ("seat-access-1", "seat-refresh-1", "dev-123")
#: the user-facing device code is shown to the browser on login start only —
#: it must never land in the audit log
USER_CODE = "ABCD-EFGH"


def _providers_file(tmp_path: Path, oauth_port: int, api_port: int) -> Path:
    f = tmp_path / "providers.toml"
    f.write_text(SEAT_PROVIDERS_TOML.format(api_port=api_port, oauth_port=oauth_port))
    return f


@pytest.fixture
def seat_coop_env(tmp_path, monkeypatch) -> SimpleNamespace:
    """A seat provider (seatco) against a hermetic loopback fake OAuth + API,
    with coop enabled on a free loopback port and no API keys in the env."""
    api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPIHandler)
    api.requests = []
    oauth = ThreadingHTTPServer(("127.0.0.1", 0), FakeOAuthHandler)
    oauth.oauth_requests = []
    oauth.counters = {}
    oauth.mode = "auto"
    t1 = _serve(api)
    t2 = _serve(oauth)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers_file = _providers_file(tmp_path, oauth.server_address[1], api.server_address[1])
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    yield SimpleNamespace(
        tmp_path=tmp_path,
        api=api,
        oauth=oauth,
        providers_file=providers_file,
        settings_file=settings_file,
    )
    api.shutdown(); api.server_close(); t1.join(timeout=5)
    oauth.shutdown(); oauth.server_close(); t2.join(timeout=5)


@pytest.fixture
def seat_coop_daemon(seat_coop_env):
    settings = load_settings(seat_coop_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def seat_auth_env(tmp_path, monkeypatch) -> SimpleNamespace:
    """Same seat provider, but the coop bound non-loopback with a configured
    token → every /api/auth/* op requires the bearer."""
    oauth = ThreadingHTTPServer(("127.0.0.1", 0), FakeOAuthHandler)
    oauth.oauth_requests = []
    oauth.counters = {}
    oauth.mode = "auto"
    t = _serve(oauth)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    providers_file = _providers_file(tmp_path, oauth.server_address[1], 1)
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    settings_file = write_settings(
        tmp_path,
        coop_enabled=True,
        coop_host="0.0.0.0",
        coop_port=free_port(),
        coop_auth_token="spec19-secret-token",
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(settings_file))
    yield SimpleNamespace(tmp_path=tmp_path, oauth=oauth, providers_file=providers_file, settings_file=settings_file)
    oauth.shutdown(); oauth.server_close(); t.join(timeout=5)


@pytest.fixture
def seat_auth_daemon(seat_auth_env):
    settings = load_settings(seat_auth_env.settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- GET /api/auth/providers: modes + seat session state, never tokens --------


def test_auth_providers_lists_modes_and_session_state_with_socket_parity(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    sock = seat_coop_daemon.settings.socket_path

    r = c.get("/api/auth/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["op"] == "auth_providers"
    # HTTP == socket parity
    assert body == request(sock, "auth_providers", timeout=2.0)

    rows = {p["provider"]: p for p in body["providers"]}
    assert rows["mistral"]["mode"] == "api-key" and rows["mistral"]["session"] is None
    assert rows["seatco"]["mode"] == "seat" and rows["seatco"]["session"] is None  # no session yet
    # never any token material — no token keys, no token values
    assert "access_token" not in r.text and "refresh_token" not in r.text
    for marker in SECRET_MARKERS:
        assert marker not in r.text


def test_auth_providers_reflects_seat_session_after_login(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    r = c.post("/api/auth/login", json={"provider": "seatco"})
    assert r.status_code == 200, r.text
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["status"] == "authorized"

    body = c.get("/api/auth/providers").json()
    seatco = {p["provider"]: p for p in body["providers"]}["seatco"]
    assert seatco["mode"] == "seat"
    assert seatco["session"]["expired"] is False
    assert seatco["session"]["expires_at"].endswith("Z")
    assert seatco["session"]["token_type"] == "Bearer"
    # the payload carries expiry metadata only — no token keys or values
    assert "access_token" not in json.dumps(body)
    for marker in SECRET_MARKERS:
        assert marker not in json.dumps(body)


# --- POST /api/auth/login + /api/auth/login/status: the device flow over HTTP ---


def test_device_login_flow_over_http_stores_session_0600_and_audits(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    settings = seat_coop_daemon.settings
    sock = settings.socket_path

    # start: relays the user-facing code + verification URI, never the device code
    r = c.post("/api/auth/login", json={"provider": "seatco"})
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["ok"] is True and start["provider"] == "seatco" and start["mode"] == "seat"
    assert start["user_code"] == USER_CODE
    assert "/verify" in start["verification_uri"]
    assert start["expires_at"].endswith("Z") and start["interval"] > 0
    assert "device_code" not in start
    for marker in SECRET_MARKERS:
        assert marker not in r.text  # dev-123 is the device code — never served

    # poll: auto-approve fake → authorized
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 200, r.text
    poll = r.json()
    assert poll["ok"] is True and poll["status"] == "authorized"
    assert poll["session"]["expired"] is False and poll["session"]["expires_at"].endswith("Z")
    for marker in SECRET_MARKERS:
        assert marker not in r.text

    # the session is stored 0600 under <data_dir>/seats/ exactly as the CLI path
    session_file = settings.seats_dir / "seatco.json"
    assert session_file.exists()
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600
    on_disk = json.loads(session_file.read_text())
    assert on_disk["access_token"] == "seat-access-1"  # on disk it must; served it never is

    # providers now show the session; socket parity for the read op
    assert request(sock, "auth_providers", timeout=2.0) == c.get("/api/auth/providers").json()

    # audit: perch.claimed with provider + mode only — never tokens or codes
    events = AuditLog(settings.audit_log_path).read_all()
    logins = [e for e in events if e["event"] == "perch.claimed"]
    assert len(logins) == 1
    assert logins[0]["provider"] == "seatco" and logins[0]["mode"] == "seat"
    audit_text = json.dumps(events)
    for marker in (*SECRET_MARKERS, USER_CODE):
        assert marker not in audit_text


def test_device_login_pending_then_authorized_over_http(seat_coop_env, seat_coop_daemon) -> None:
    seat_coop_env.oauth.mode = "pending_once"
    c = http_client(seat_coop_daemon.settings)
    r = c.post("/api/auth/login", json={"provider": "seatco"})
    assert r.status_code == 200, r.text
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert r.json()["ok"] is True
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["status"] == "authorized"


def test_socket_ops_standalone_login_poll_logout(seat_coop_daemon) -> None:
    """The same ops over the raw socket — the GUI is a thin mirror, and the
    CLI path (SeatBroker.login) composes the same two steps."""
    settings = seat_coop_daemon.settings
    sock = settings.socket_path

    start = request(sock, "auth_login_start", provider="seatco", timeout=5.0)
    assert start["ok"] is True and start["user_code"] == USER_CODE
    assert "device_code" not in start and all(m not in json.dumps(start) for m in SECRET_MARKERS)

    poll = request(sock, "auth_login_poll", provider="seatco", timeout=5.0)
    assert poll["ok"] is True and poll["status"] == "authorized"
    assert (settings.seats_dir / "seatco.json").exists()

    out = request(sock, "auth_logout", provider="seatco", timeout=2.0)
    assert out["ok"] is True and out["provider"] == "seatco"
    assert not (settings.seats_dir / "seatco.json").exists()
    again = request(sock, "auth_logout", provider="seatco", timeout=2.0)
    assert again["ok"] is False and "no perch seat" in again["error"]

    events = [e["event"] for e in AuditLog(settings.audit_log_path).read_all()]
    assert "perch.claimed" in events and "perch.released" in events


def test_login_and_logout_refusals_and_malformed_bodies(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    # api-key provider → 422, unknown provider → 422, poll without a start → 422
    r = c.post("/api/auth/login", json={"provider": "mistral"})
    assert r.status_code == 422 and "api-key" in r.json()["error"]
    r = c.post("/api/auth/login", json={"provider": "no-such"})
    assert r.status_code == 422 and "unknown provider" in r.json()["error"]
    r = c.post("/api/auth/login/status", json={"provider": "seatco"})
    assert r.status_code == 422 and "no device login in progress" in r.json()["error"]
    r = c.post("/api/auth/logout", json={"provider": "seatco"})
    assert r.status_code == 422 and "no perch seat" in r.json()["error"]
    # malformed bodies → 400
    assert c.post("/api/auth/login", json={}).status_code == 400
    assert c.post("/api/auth/login", json={"provider": 42}).status_code == 400
    assert c.post("/api/auth/login/status", content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/auth/logout", json=["seatco"]).status_code == 400


# --- POST /api/auth/logout over HTTP ---------------------------------------------


def test_logout_over_http_deletes_session_and_audits(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    settings = seat_coop_daemon.settings

    assert c.post("/api/auth/login", json={"provider": "seatco"}).status_code == 200
    assert c.post("/api/auth/login/status", json={"provider": "seatco"}).json()["status"] == "authorized"
    assert (settings.seats_dir / "seatco.json").exists()

    r = c.post("/api/auth/logout", json={"provider": "seatco"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["provider"] == "seatco"
    assert not (settings.seats_dir / "seatco.json").exists()
    body = c.get("/api/auth/providers").json()
    seatco = {p["provider"]: p for p in body["providers"]}["seatco"]
    assert seatco["session"] is None

    events = AuditLog(settings.audit_log_path).read_all()
    logouts = [e for e in events if e["event"] == "perch.released"]
    assert len(logouts) == 1 and logouts[0]["provider"] == "seatco" and logouts[0]["mode"] == "seat"
    for marker in (*SECRET_MARKERS, USER_CODE):
        assert marker not in json.dumps(events)

    # second logout → 422
    r = c.post("/api/auth/logout", json={"provider": "seatco"})
    assert r.status_code == 422 and r.json()["ok"] is False


# --- GET /api/auth/access: the declared scope ------------------------------------


def test_auth_access_declared_scope_with_socket_parity(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    sock = seat_coop_daemon.settings.socket_path

    r = c.get("/api/auth/access")
    assert r.status_code == 200
    body = r.json()
    assert body == request(sock, "auth_access", timeout=2.0)  # HTTP == socket
    access = body["access"]
    assert access["env_allowlist"] == ["MISTRAL_API_KEY"]
    assert access["env_allowlist_enforced"] is True
    assert "HOME" in access["base_env_keys"] and "PATH" in access["base_env_keys"]
    assert access["network_policy"] == "none"
    assert access["network_allowed"] == ["none"]
    assert access["network_enforced"] is False
    assert [s["kind"] for s in access["scope"]] == ["tools", "data", "mcp_servers", "connectors"]
    assert all(s["declared"] is None and s["note"] for s in access["scope"])
    for marker in SECRET_MARKERS:
        assert marker not in r.text


# --- auth-required mode + daemon-down payloads ------------------------------------


def test_auth_endpoints_require_bearer_when_non_loopback(seat_auth_daemon) -> None:
    c = http_client(seat_auth_daemon.settings)
    for method, path in (
        ("get", "/api/auth/providers"),
        ("get", "/api/auth/access"),
        ("post", "/api/auth/login"),
        ("post", "/api/auth/login/status"),
        ("post", "/api/auth/logout"),
    ):
        if method == "get":
            assert c.get(path).status_code == 401, path
        else:
            assert c.post(path, json={"provider": "seatco"}).status_code == 401, path
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open

    # with the bearer everything answers (200 or a structured 422 refusal)
    assert c.get("/api/auth/providers", headers=AUTH_HEADERS).status_code == 200
    assert c.get("/api/auth/access", headers=AUTH_HEADERS).status_code == 200
    r = c.post("/api/auth/login", json={"provider": "seatco"}, headers=AUTH_HEADERS)
    assert r.status_code == 200, r.text
    r = c.post("/api/auth/login/status", json={"provider": "seatco"}, headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "authorized"
    r = c.post("/api/auth/logout", json={"provider": "seatco"}, headers=AUTH_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] is True
    # the configured coop token is never any of the seat material
    assert "spec19-secret-token" not in json.dumps(c.get("/api/auth/providers", headers=AUTH_HEADERS).json())


def test_auth_endpoints_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
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
                for key, method, path, kwargs in (
                    ("providers", "get", "/api/auth/providers", {}),
                    ("access", "get", "/api/auth/access", {}),
                    ("login", "post", "/api/auth/login", {"json": {"provider": "seatco"}}),
                    ("login_status", "post", "/api/auth/login/status", {"json": {"provider": "seatco"}}),
                    ("logout", "post", "/api/auth/logout", {"json": {"provider": "seatco"}}),
                ):
                    r = await c.request(method, path, **kwargs)
                    out[key] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key, (code, body) in out.items():
        assert code == 200, key  # structured payload, never a broken 500 (POSTs included)
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- per-run auth_mode surfaced in runs_list ---------------------------------------


def test_runs_list_carries_auth_mode_over_http_and_socket(seat_coop_env, seat_coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(seat_coop_daemon.settings)
    sock = seat_coop_daemon.settings.socket_path

    # 1. a seat login over HTTP, then a seat run → auth_mode=seat
    assert c.post("/api/auth/login", json={"provider": "seatco"}).status_code == 200
    assert c.post("/api/auth/login/status", json={"provider": "seatco"}).json()["status"] == "authorized"
    seat_job = write_seat_job(tmp_path, name="gui-seat-job")
    enq = request(sock, "enqueue", spec_path=str(seat_job.resolve()), timeout=5.0)
    assert enq["ok"] is True, enq
    run_seat = wait_terminal(sock, enq["run_id"], timeout=30.0)
    assert run_seat["status"] == "succeeded"
    # the provider saw the seat bearer token; the API responses never did
    assert seat_coop_env.api.requests[-1]["authorization"] == "Bearer seat-access-1"

    # 2. a vibe-cli run with no API key in env → auth_mode=none
    vibe_job = write_job(tmp_path, name="gui-vibe-job")
    run_id = enqueue_over_http(c, vibe_job).json()["run_id"]
    assert wait_terminal_http(c, run_id)["status"] == "succeeded"

    # runs_list rows carry auth_mode; HTTP == socket parity
    http_list = c.get("/api/runs")
    assert http_list.status_code == 200
    assert http_list.json() == request(sock, "runs_list", timeout=2.0)
    modes = {r["id"]: r["auth_mode"] for r in http_list.json()["runs"]}
    assert modes[enq["run_id"]] == "seat"
    assert modes[run_id] == "none"

    # the run detail (manifest auth block) agrees — mode only, never tokens
    show = c.get(f"/api/runs/{enq['run_id']}").json()["run"]
    assert show["manifest"]["auth"]["mode"] == "seat"
    assert "access_token" not in json.dumps(show)
    # the audit chicken.finished row carries auth_mode (spec 09) — surfaced in the Audit view
    events = AuditLog(seat_coop_daemon.settings.audit_log_path).read_all()
    fin = [e for e in events if e["event"] == "chicken.finished" and e.get("run_id") == enq["run_id"]]
    assert fin and fin[0]["auth_mode"] == "seat"


# --- no token material in any response across the whole flow -----------------------


def test_no_token_material_in_any_response_during_full_flow(seat_coop_daemon) -> None:
    c = http_client(seat_coop_daemon.settings)
    bodies: list[str] = []

    def collect(r: httpx.Response) -> httpx.Response:
        bodies.append(r.text)
        return r

    collect(c.get("/api/auth/providers"))
    collect(c.get("/api/auth/access"))
    collect(c.post("/api/auth/login", json={"provider": "seatco"}))
    collect(c.post("/api/auth/login/status", json={"provider": "seatco"}))
    collect(c.get("/api/auth/providers"))
    collect(c.get("/api/status"))
    collect(c.post("/api/auth/logout", json={"provider": "seatco"}))
    collect(c.get("/api/auth/providers"))

    everything = "\n".join(bodies)
    for marker in SECRET_MARKERS:
        assert marker not in everything
    assert "access_token" not in everything and "refresh_token" not in everything
    # the user-facing code appears only in the login-start response (the 3rd call)
    assert USER_CODE in bodies[2]
    assert all(USER_CODE not in b for b in bodies[:2] + bodies[3:])


# --- SPA serves the Auth view -------------------------------------------------------


def test_spa_serves_auth_view_and_runs_auth_column(seat_coop_daemon, tmp_path) -> None:
    c = http_client(seat_coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-auth"', 'id="auth-providers-tbody"', 'id="auth-card"', 'id="auth-empty"',
        'id="auth-error"', 'id="device-card"', 'id="device-code"', 'id="device-provider"',
        'id="device-verification"', 'id="device-status"', 'id="device-expires"', 'id="device-cancel"',
        'id="access-card"', 'id="access-list"', 'id="access-scope-table"', 'id="access-scope-tbody"',
        'data-view="auth"', "/icons/key-round.svg", ">Perch</a>",
        "<th scope=\"col\">Auth</th>",  # runs table column
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    for marker in (
        '"/api/auth/providers"', '"/api/auth/access"', '"/api/auth/login"', '"/api/auth/login/status"', '"/api/auth/logout"',
        "auth: loadAuth", "loadAuth", "startDeviceLogin", "pollDeviceLogin", "renderAccess", "fmtCountdown",
        "r.auth_mode",
    ):
        assert marker in js, marker
    css = c.get("/styles.css").text
    for cls in (".badge", ".badge-seat", ".badge-api-key", ".device-card", ".device-code", ".access-card", ".access-scope-table"):
        assert cls in css, cls
    icon = c.get("/icons/key-round.svg")
    assert icon.status_code == 200 and icon.headers["content-type"].startswith("image/svg+xml")
    # no host paths ever enter the UI
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: the whole auth flow over HTTP on `tc coop start` ---------------


def test_e2e_auth_flow_over_http_subprocess(tmp_path, monkeypatch) -> None:
    oauth = ThreadingHTTPServer(("127.0.0.1", 0), FakeOAuthHandler)
    oauth.oauth_requests = []
    oauth.counters = {}
    oauth.mode = "auto"
    t = _serve(oauth)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("SEATCO_TOKEN", raising=False)
    providers_file = _providers_file(tmp_path, oauth.server_address[1], 1)
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers_file))
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)  # `tc coop start` forces coop on (loopback → open)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
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

        bodies: list[str] = []

        def collect(r: httpx.Response) -> httpx.Response:
            bodies.append(r.text)
            return r

        # providers: seatco is a seat provider with no session yet
        body = collect(c.get("/api/auth/providers")).json()
        seatco = {p["provider"]: p for p in body["providers"]}["seatco"]
        assert seatco["mode"] == "seat" and seatco["session"] is None

        # access scope is served too
        access = collect(c.get("/api/auth/access")).json()["access"]
        assert access["network_policy"] == "none" and access["env_allowlist"] == ["MISTRAL_API_KEY"]

        # device login over HTTP → authorized → session stored 0600 → audited
        start = collect(c.post("/api/auth/login", json={"provider": "seatco"})).json()
        assert start["ok"] is True and start["user_code"] == USER_CODE and "device_code" not in start
        poll = collect(c.post("/api/auth/login/status", json={"provider": "seatco"})).json()
        assert poll["status"] == "authorized"
        session_file = settings.seats_dir / "seatco.json"
        assert session_file.exists() and stat.S_IMODE(session_file.stat().st_mode) == 0o600
        body = collect(c.get("/api/auth/providers")).json()
        seatco = {p["provider"]: p for p in body["providers"]}["seatco"]
        assert seatco["session"] and seatco["session"]["expired"] is False

        # logout over HTTP → session gone, audited
        out = collect(c.post("/api/auth/logout", json={"provider": "seatco"})).json()
        assert out["ok"] is True and not session_file.exists()
        body = collect(c.get("/api/auth/providers")).json()
        assert {p["provider"]: p for p in body["providers"]}["seatco"]["session"] is None

        events = AuditLog(settings.audit_log_path).read_all()
        kinds = [e["event"] for e in events]
        assert "perch.claimed" in kinds and "perch.released" in kinds

        # no token/device-code material in any response or audit row
        everything = "\n".join(bodies) + "\n" + json.dumps(events)
        for marker in SECRET_MARKERS:
            assert marker not in everything
        assert USER_CODE not in json.dumps(events)

        # the SPA serves the Auth view
        page = c.get("/")
        assert page.status_code == 200 and 'id="view-auth"' in page.text and 'id="device-card"' in page.text
        assert 'data-view="auth"' in page.text and c.get("/icons/key-round.svg").status_code == 200
        for asset in (page.text, c.get("/app.js").text, c.get("/styles.css").text):
            assert str(tmp_path) not in asset

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=10)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        oauth.shutdown(); oauth.server_close(); t.join(timeout=5)
