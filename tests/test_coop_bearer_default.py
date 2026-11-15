"""Tests for spec 32 ticket 04 (review G3 auth core + G19 compare): the Coop
requires the bearer token on **every** bind by default — loopback-open is only
an explicit opt-in via the new ``coop_loopback_open`` settings key (default
off, ``extra="forbid"`` intact, not runtime-mutable from the GUI) — and the
bearer is compared with ``hmac.compare_digest`` everywhere it is the sole
network credential (the Coop and Farmhand middlewares).

Seams (highest existing first): the HTTP surface of a real thread daemon on a
temp loopback port driven with httpx exactly like the browser; the
``settings_set`` socket op + ``POST /api/settings`` mirror; the ``farmhand_info``
socket op; the Settings model itself; a subprocess ``tc farmhand serve
--transport https`` for the standalone Farmhand middleware; and a source guard
pinning the constant-time compare. All assertions are external behavior:
status codes, payload shapes, field-named errors, printed lines, audit rows.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopAuth, coop_auth
from turbochickens.settings import Settings, SettingsError, load_settings

runner = CliRunner()

MCP_INIT_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}},
}

#: protected routes of the socket-mirrored HTTP surface (the 401 matrix)
PROTECTED_GETS = [
    "/api/status",
    "/api/jobs",
    "/api/audit",
    "/api/settings",
    "/api/runs",
    "/api/connectors",
    "/api/schedule",
    "/api/stats",
    "/api/auth/providers",
    "/api/auth/access",
    "/api/farmhand",
]


# --- the Settings model: the opt-in key, default off, extra="forbid" intact ---


def test_coop_loopback_open_defaults_off_and_is_served() -> None:
    s = Settings()
    assert s.coop_loopback_open is False
    assert s.public_dict()["coop_loopback_open"] is False
    assert "coop_auth_token" not in s.public_dict()  # secrets stay omitted


def test_coop_loopback_open_loads_from_file(tmp_path) -> None:
    f = write_settings(tmp_path, coop_loopback_open=True)
    s = load_settings(f)
    assert s.coop_loopback_open is True
    assert s.public_dict()["coop_loopback_open"] is True


def test_near_miss_key_still_rejected_by_extra_forbid(tmp_path) -> None:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\ncoop_loopback_openn = true\n')
    with pytest.raises(SettingsError, match="coop_loopback_openn"):
        load_settings(f)
    with pytest.raises(Exception):
        Settings.model_validate({"coop_loopback_openn": True})


# --- pure auth seam: the default matrix ----------------------------------------


def test_coop_auth_requires_bearer_on_loopback_by_default() -> None:
    a = coop_auth(Settings(coop_host="127.0.0.1"))  # no token, no opt-in
    assert isinstance(a, CoopAuth)
    assert a.required is True and a.ephemeral is True and a.token


def test_coop_auth_open_only_with_opt_in_on_loopback_without_token() -> None:
    a = coop_auth(Settings(coop_host="127.0.0.1", coop_loopback_open=True))
    assert a.required is False and a.token is None
    # the opt-in never opens a non-loopback bind and never overrides a token
    assert coop_auth(Settings(coop_host="0.0.0.0", coop_loopback_open=True)).required is True
    assert coop_auth(Settings(coop_host="127.0.0.1", coop_loopback_open=True, coop_auth_token="t")).required is True


# --- fixtures: thread daemons in default mode and opt-in mode ------------------


def _start_daemon(settings, capture: io.StringIO | None):
    dt = DaemonThread(settings)
    if capture is not None:
        with contextlib.redirect_stdout(capture):
            dt.start()
    else:
        dt.start()
    return dt


@pytest.fixture
def default_daemon(tmp_path, monkeypatch):
    """Loopback coop, no token, NO opt-in → the spec-32 default: bearer required,
    ephemeral token minted and printed once to stdout."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    buf = io.StringIO()
    dt = _start_daemon(settings, buf)
    token_lines = [l for l in buf.getvalue().splitlines() if l.startswith("coop auth token: ")]
    assert len(token_lines) == 1, buf.getvalue()
    token = token_lines[0].split(": ", 1)[1].strip()
    dt._captured_stdout = buf.getvalue()  # type: ignore[attr-defined]
    yield dt, token
    dt.stop()
    assert buf.getvalue().count("coop auth token: ") == 1  # printed exactly once overall


@pytest.fixture
def optin_daemon(tmp_path, monkeypatch):
    """Loopback coop, no token, WITH the opt-in → open as before."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    buf = io.StringIO()
    dt = _start_daemon(settings, buf)
    yield dt, buf.getvalue()
    dt.stop()


# --- default mode: the extended 401 matrix over HTTP ----------------------------


def test_default_loopback_401_matrix_and_positive_bearer(default_daemon) -> None:
    dt, token = default_daemon
    c = http_client(dt.settings)
    bearer = {"Authorization": f"Bearer {token}"}

    # the one intentionally open route
    assert c.get("/api/ping").status_code == 200
    # SPA assets stay open so the login card can render
    assert c.get("/").status_code == 200
    assert c.get("/app.js").status_code == 200

    # every socket-mirrored route refuses without the bearer …
    for path in PROTECTED_GETS:
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"Authorization": "Bearer wrong-token"}).status_code == 401, path
        assert c.get(path, headers={"Authorization": token}).status_code == 401, path  # missing "Bearer "
    # … including the mutating routes (the G3 code-execution vector)
    assert c.post("/api/runs", json={"spec_path": "/nope"}).status_code == 401
    assert c.post("/api/settings", json={"farmhand_transport": "https"}).status_code == 401
    assert c.post("/api/connectors/add", json={"name": "x"}).status_code == 401
    # … and the Farmhand MCP route
    assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401

    # legitimate token holders are unaffected
    for path in PROTECTED_GETS:
        r = c.get(path, headers=bearer)
        assert r.status_code == 200, (path, r.text)
    r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, **bearer})
    assert r.status_code == 200 and r.headers.get("mcp-session-id")

    body = c.get("/api/status", headers=bearer).json()
    assert body["coop"]["auth_required"] is True
    assert token not in c.get("/api/status", headers=bearer).text  # never served back


def test_bearer_edge_cases_refused_not_crashed(default_daemon) -> None:
    dt, token = default_daemon
    c = http_client(dt.settings)
    # same length, differs only in the last character — a timing oracle must not exist
    assert c.get("/api/status", headers={"Authorization": f"Bearer {token[:-1]}X"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": ""}).status_code == 401
    # a crafted non-ASCII bearer is refused at the seam, never an exception
    from turbochickens.coop import bearer_matches

    assert bearer_matches(f"Bearer {token}", token) is True
    assert bearer_matches(f"Bearer {token[:-1]}X", token) is False
    assert bearer_matches("Bearer tést-tókén-\udcff", token) is False  # no UnicodeEncodeError
    assert bearer_matches("", token) is False
    assert bearer_matches(f"bearer {token}", token) is False  # scheme is case-sensitive


def test_status_audit_and_farmhand_info_report_auth_required_and_never_the_token(default_daemon) -> None:
    dt, token = default_daemon
    sock = request(dt.settings.socket_path, "status", timeout=2.0)
    assert sock["coop"]["auth_required"] is True
    assert token not in str(sock)
    info = request(dt.settings.socket_path, "farmhand_info", timeout=10.0)
    assert info["ok"] is True and info["auth_required"] is True
    assert token not in str(info)
    events = AuditLog(dt.settings.audit_log_path).read_all()
    coop_started = [e for e in events if e["event"] == "coop.started"]
    assert coop_started and coop_started[0]["auth_required"] is True
    assert all(token not in str(e) for e in events)  # never audited


# --- opt-in mode: loopback binds are open as before -----------------------------


def test_opt_in_loopback_bind_is_open_as_before(optin_daemon) -> None:
    dt, stdout = optin_daemon
    c = http_client(dt.settings)
    assert "coop auth token" not in stdout  # open mode prints no token
    for path in PROTECTED_GETS:
        assert c.get(path).status_code == 200, path
    assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 200
    sock = request(dt.settings.socket_path, "status", timeout=2.0)
    assert sock["coop"] == {
        "enabled": True,
        "url": f"http://127.0.0.1:{dt.settings.coop_port}/",
        "auth_required": False,
    }
    info = request(dt.settings.socket_path, "farmhand_info", timeout=10.0)
    assert info["auth_required"] is False
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert f"coop: http://127.0.0.1:{dt.settings.coop_port}/ auth=open" in result.output


# --- the opt-in is not runtime-mutable from the GUI (settings_set) -------------


def test_settings_set_refuses_coop_loopback_open_with_field_named_error(default_daemon) -> None:
    dt, token = default_daemon
    sock = dt.settings.socket_path

    resp = request(sock, "settings_set", coop_loopback_open=True, timeout=5.0)
    assert resp["ok"] is False
    assert resp["errors"][0]["field"] == "settings.coop_loopback_open"
    assert "read-only at runtime" in resp["errors"][0]["message"]

    # a near-miss unknown key is still a field-named refusal (extra="forbid" intact)
    resp = request(sock, "settings_set", coop_loopback_openn=True, timeout=5.0)
    assert resp["ok"] is False and resp["valid"] is False
    assert resp["errors"][0]["field"] == "coop_loopback_openn"

    # over HTTP the refusal is a 422 behind the bearer, 401 without it
    c = http_client(dt.settings)
    bearer = {"Authorization": f"Bearer {token}"}
    assert c.post("/api/settings", json={"coop_loopback_open": True}).status_code == 401
    r = c.post("/api/settings", json={"coop_loopback_open": True}, headers=bearer)
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "settings.coop_loopback_open"

    # positive: the one genuinely mutable key still works with the bearer
    r = c.post("/api/settings", json={"farmhand_transport": "https"}, headers=bearer)
    assert r.status_code == 200 and r.json()["changed"] == {"farmhand_transport": "https"}
    # the settings payload carries the new key (not a secret) and never the token
    s = r.json()["settings"]
    assert s["coop_loopback_open"] is False
    assert "coop_auth_token" not in s


# --- G19: constant-time compare everywhere the bearer is the sole credential ----


def test_bearer_compared_constant_time_in_coop_and_farmhand_middlewares() -> None:
    src_root = Path(__file__).resolve().parent.parent / "src" / "turbochickens"
    coop_text = (src_root / "coop.py").read_text(encoding="utf-8")
    farmhand_text = (src_root / "farmhand.py").read_text(encoding="utf-8")
    # the compare itself lives in coop.py and uses hmac.compare_digest; the
    # Farmhand middleware reuses that helper instead of comparing directly
    assert "compare_digest" in coop_text
    assert "bearer_matches" in farmhand_text
    for name, text in (("coop.py", coop_text), ("farmhand.py", farmhand_text)):
        # no plain equality/inequality comparison against the bearer remains
        assert re.search(r"(==|!=)\s*f?\"Bearer", text) is None, name
        assert re.search(r"(==|!=)\s*f?'Bearer", text) is None, name


# --- the standalone Farmhand HTTPS endpoint follows the same rule ---------------


def _farmhand_serve_https(tmp_path, monkeypatch, port: int, extra_toml: str = ""):
    f = tmp_path / "settings.toml"
    f.write_text(
        f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\ncoop_port = {port}\n{extra_toml}'
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)  # coop disabled: the port is free for the farmhand process
    dt.start()
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "farmhand", "serve", "--transport", "https"],
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    def wait_http(timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                httpx.post(
                    f"http://127.0.0.1:{port}/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS, timeout=1.0
                )
                return
            except httpx.ConnectError:
                pass
            if proc.poll() is not None:
                raise AssertionError(f"farmhand exited early rc={proc.returncode}: {lines}")
            time.sleep(0.05)
        raise AssertionError(f"farmhand https listener did not come up: {lines}")

    return settings, dt, proc, lines, reader, wait_http


def _stop_farmhand(settings, dt, proc) -> None:
    try:
        request(settings.socket_path, "shutdown", timeout=3.0)
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    dt.stop()


def test_farmhand_standalone_https_requires_bearer_by_default(tmp_path, monkeypatch) -> None:
    port = free_port()
    settings, dt, proc, lines, reader, wait_http = _farmhand_serve_https(tmp_path, monkeypatch, port)
    try:
        wait_http()
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        # loopback + no token + no opt-in → the MCP route is NOT open anymore
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 401
        # the ephemeral token is printed once and unlocks the endpoint
        deadline = time.time() + 10
        token = None
        while time.time() < deadline and token is None:
            for l in lines:
                if l.startswith("coop auth token: "):
                    token = l.split(": ", 1)[1].strip()
                    break
            time.sleep(0.05)
        assert token, f"no ephemeral token printed: {lines}"
        r = c.post("/mcp", json=MCP_INIT_BODY, headers={**MCP_INIT_HEADERS, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.headers.get("mcp-session-id")
        assert [l for l in lines if l.startswith("coop auth token: ")].__len__() == 1
    finally:
        _stop_farmhand(settings, dt, proc)
        reader.join(timeout=5)


def test_farmhand_standalone_https_open_with_opt_in(tmp_path, monkeypatch) -> None:
    port = free_port()
    settings, dt, proc, lines, reader, wait_http = _farmhand_serve_https(
        tmp_path, monkeypatch, port, extra_toml="coop_loopback_open = true\n"
    )
    try:
        wait_http()
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0)
        r = c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        assert r.status_code == 200 and r.headers.get("mcp-session-id")  # open as before
        assert not any(l.startswith("coop auth token: ") for l in lines)
    finally:
        _stop_farmhand(settings, dt, proc)
        reader.join(timeout=5)
