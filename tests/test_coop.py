"""Tests for spec 12: coop — the daemon serves the TurboChickens GUI.

Seams (per spec 11/12 testing decisions — no browser automation):
- the HTTP API against a real daemon booted in a thread on a temp port,
  driven with httpx exactly the way the browser does;
- the pure auth-decision seam (loopback/token → required, ephemeral minting);
- a subprocess e2e (`python -m turbochickens coop start`) asserting the
  ephemeral token is printed exactly once to stdout and shutdown tears the
  HTTP server down.

All assertions are external, observable behavior: HTTP status codes and JSON
bodies, served pages/assets, auth enforcement, socket-op payloads, audit rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.coop import CoopServer, SocketTransport, coop_auth, is_loopback
from turbochickens.settings import Settings, load_settings

runner = CliRunner()


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


def http_client(settings: Settings) -> httpx.Client:
    # tests always connect over loopback, whatever the bind host is
    return httpx.Client(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0)


@pytest.fixture
def coop_file(tmp_path, monkeypatch) -> Path:
    """Settings with coop enabled on a free loopback port, no token — open via
    the spec-32 coop_loopback_open opt-in (the default requires the bearer)."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def coop_daemon(coop_file):
    settings = load_settings(coop_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def auth_file(tmp_path, monkeypatch) -> Path:
    """Non-loopback bind + configured token → bearer required."""
    f = write_settings(
        tmp_path,
        coop_enabled=True,
        coop_host="0.0.0.0",
        coop_port=free_port(),
        coop_auth_token="test-token-abc",
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def auth_daemon(auth_file):
    settings = load_settings(auth_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- pure auth seam -----------------------------------------------------------


def test_is_loopback() -> None:
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.5")
    assert not is_loopback("::")


def test_coop_auth_loopback_no_token_requires_bearer_by_default() -> None:
    # spec 32 (G3): "loopback + no token → open" is gone; the bearer is required
    # on every bind unless coop_loopback_open opts out — an ephemeral token is
    # minted when none is configured, even on loopback
    s = Settings(coop_host="127.0.0.1")
    a = coop_auth(s)
    assert a.required is True and a.ephemeral is True and a.token


def test_coop_auth_loopback_open_only_with_explicit_opt_in() -> None:
    s = Settings(coop_host="127.0.0.1", coop_loopback_open=True)
    a = coop_auth(s)
    assert a.required is False and a.token is None and a.ephemeral is False


def test_coop_auth_opt_in_does_not_open_non_loopback_binds() -> None:
    s = Settings(coop_host="0.0.0.0", coop_loopback_open=True)
    a = coop_auth(s)
    assert a.required is True and a.ephemeral is True and a.token


def test_coop_auth_opt_in_does_not_override_a_configured_token() -> None:
    s = Settings(coop_host="127.0.0.1", coop_loopback_open=True, coop_auth_token="tok")
    a = coop_auth(s)
    assert a.required is True and a.token == "tok" and a.ephemeral is False


def test_coop_auth_non_loopback_requires_token() -> None:
    s = Settings(coop_host="0.0.0.0", coop_auth_token="tok")
    a = coop_auth(s)
    assert a.required is True and a.token == "tok" and a.ephemeral is False


def test_coop_auth_non_loopback_without_token_mints_ephemeral() -> None:
    s = Settings(coop_host="0.0.0.0")
    a = coop_auth(s)
    assert a.required is True and a.ephemeral is True and a.token and a.token != ""
    b = coop_auth(s)
    assert b.token != a.token  # minted per server start


def test_coop_auth_configured_token_wins_on_loopback_too() -> None:
    s = Settings(coop_host="127.0.0.1", coop_auth_token="tok")
    a = coop_auth(s)
    assert a.required is True and a.token == "tok" and a.ephemeral is False


# --- HTTP API against a thread daemon (open mode) ------------------------------


def test_api_ping_unauthenticated_mirrors_socket_op(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    r = c.get("/api/ping")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["pong"] is True
    sock = request(coop_daemon.settings.socket_path, "ping", timeout=2.0)
    assert body["version"] == sock["version"] and body["pid"] == sock["pid"]


def _normalize_status(status: dict) -> dict:
    """Drop the volatile fields (uptime/started_at/last sweep) for comparison."""
    out = dict(status)
    out["daemon"] = {k: v for k, v in status["daemon"].items() if k not in ("uptime_sec", "started_at")}
    out["retention"] = {k: v for k, v in status["retention"].items() if k != "last_sweep"}
    return out


def test_api_status_mirrors_socket_status(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    r = c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    sock = request(coop_daemon.settings.socket_path, "status", timeout=2.0)
    assert _normalize_status(body) == _normalize_status(sock)  # thin mirror of the socket op
    assert body["daemon"]["up"] is True
    assert body["queue_depth"] == 0
    assert body["weight_budget"] == 4


def test_status_op_gains_coop_block(coop_daemon) -> None:
    sock = request(coop_daemon.settings.socket_path, "status", timeout=2.0)
    assert sock["coop"] == {
        "enabled": True,
        "url": f"http://127.0.0.1:{coop_daemon.settings.coop_port}/",
        "auth_required": False,
    }


def test_spa_shell_served_with_content_types(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    r = c.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "TurboChickens" in html  # wordmark
    assert "egg" in html  # egg-dot brand mark
    for view in ("Dashboard", "Chickens", "Eggs", "Rooster", "Feed", "Headcount", "Settings"):
        assert view in html  # sidebar nav
    assert 'id="view-login"' in html  # login card for 401
    assert 'data-stat="uptime"' in html and 'data-stat="queue_depth"' in html
    assert 'data-stat="in_flight"' in html and 'data-stat="alerts"' in html
    assert 'data-stat="schedules"' in html and 'data-stat="retention"' in html
    assert "Flock diagram" in html  # the dealt moment

    js = c.get("/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/javascript")
    assert "sessionStorage" in js.text  # token in sessionStorage only
    assert "document.cookie" not in js.text  # no cookies
    assert "2000" in js.text  # polls every 2 s

    css = c.get("/styles.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "#FCFAF5" in css.text and "#C87F0A" in css.text  # paper ground + yolk action

    for icon in ("egg", "layout-dashboard", "play", "file-text", "calendar", "bar-chart-3", "list", "settings"):
        ir = c.get(f"/icons/{icon}.svg")
        assert ir.status_code == 200, icon
        assert ir.headers["content-type"] == "image/svg+xml"


def test_unknown_path_404_and_traversal_blocked(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    assert c.get("/nope").status_code == 404
    assert c.get("/../pyproject.toml").status_code == 404
    assert c.get("/%2e%2e/pyproject.toml").status_code == 404
    assert c.get("/icons/../coop.py").status_code == 404


def test_tc_status_prints_coop_line(coop_daemon) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert f"coop: http://127.0.0.1:{coop_daemon.settings.coop_port}/ auth=open" in result.output


def test_shutdown_tears_down_http_server(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    assert c.get("/api/ping").status_code == 200
    resp = request(coop_daemon.settings.socket_path, "shutdown", timeout=2.0)
    assert resp["ok"] is True
    coop_daemon.thread.join(timeout=10)
    assert not coop_daemon.thread.is_alive()
    with pytest.raises(httpx.ConnectError):
        c.get("/api/ping")
    with pytest.raises(DaemonUnreachable):
        request(coop_daemon.settings.socket_path, "ping", timeout=1.0)
    events = [r["event"] for r in AuditLog(coop_daemon.settings.audit_log_path).read_all()]
    assert "coop.started" in events and "coop.stopped" in events
    assert events[-1] == "coopd.stopped"


def test_coop_disabled_status_block(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path)  # no coop_enabled
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    try:
        sock = request(settings.socket_path, "status", timeout=2.0)
        assert sock["coop"] == {"enabled": False, "url": None, "auth_required": False}
    finally:
        dt.stop()


# --- auth required (non-loopback + configured token) ---------------------------


def test_ping_stays_unauthenticated_when_auth_required(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    r = c.get("/api/ping")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_api_requires_bearer_token_when_non_loopback(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "test-token-abc"}).status_code == 401  # not "Bearer …"
    r = c.get("/api/status", headers={"Authorization": "Bearer test-token-abc"})
    assert r.status_code == 200 and r.json()["daemon"]["up"] is True


def test_spa_assets_stay_open_so_login_card_can_render(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    assert c.get("/").status_code == 200
    assert c.get("/app.js").status_code == 200
    assert c.get("/styles.css").status_code == 200


def test_status_coop_block_reports_auth_required_and_never_the_token(auth_daemon) -> None:
    sock = request(auth_daemon.settings.socket_path, "status", timeout=2.0)
    assert sock["coop"]["enabled"] is True
    assert sock["coop"]["auth_required"] is True
    assert "test-token-abc" not in str(sock)
    events = AuditLog(auth_daemon.settings.audit_log_path).read_all()
    coop_started = [e for e in events if e["event"] == "coop.started"]
    assert coop_started and coop_started[0]["auth_required"] is True
    assert "test-token-abc" not in str(events)  # token never audited


# --- ephemeral token: printed exactly once at startup ---------------------------


def test_ephemeral_token_printed_once_and_required(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_host="0.0.0.0", coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        dt.start()
    try:
        token_lines = [l for l in buf.getvalue().splitlines() if l.startswith("coop auth token: ")]
        assert len(token_lines) == 1  # printed exactly once, at startup
        token = token_lines[0].split(": ", 1)[1].strip()
        c = http_client(settings)
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"Authorization": f"Bearer {token}"}).status_code == 200
        sock = request(settings.socket_path, "status", timeout=2.0)
        assert sock["coop"]["auth_required"] is True
        assert token not in str(sock)
        assert token not in settings.audit_log_path.read_text()  # never audited
    finally:
        dt.stop()
    assert buf.getvalue().count("coop auth token: ") == 1  # still exactly once after shutdown


# --- daemon-down payload through the transport seam ------------------------------


def test_api_status_returns_daemon_down_payload_when_socket_unreachable(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    port = settings.coop_port
    # a CoopServer whose transport points at a dead socket (the seam, isolated)
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> tuple[int, dict, int, dict]:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
                st = await c.get("/api/status")
                ping = await c.get("/api/ping")
                return st.status_code, st.json(), ping.status_code, ping.json()
        finally:
            await server.stop()

    st_code, st_body, ping_code, ping_body = asyncio.run(go())
    assert st_code == 200  # structured payload, not a broken 500
    assert st_body["daemon"]["up"] is False and st_body["ok"] is False
    assert ping_code == 503 and ping_body["pong"] is False


def test_socket_transport_matches_sync_client(coop_daemon) -> None:
    async def go() -> dict:
        t = SocketTransport(coop_daemon.settings.socket_path)
        return await t.request("status")

    async_resp = asyncio.run(go())
    sock_resp = request(coop_daemon.settings.socket_path, "status", timeout=2.0)
    assert _normalize_status(async_resp) == _normalize_status(sock_resp)


# --- subprocess e2e: `tc coop start` ---------------------------------------------


def _wait_socket(settings: Settings, proc, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if settings.socket_path.exists():
            try:
                if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                    return
            except DaemonUnreachable:
                pass
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"coop process exited early rc={proc.returncode}: {out}")
        time.sleep(0.05)
    raise AssertionError("coop subprocess did not start answering")


def test_e2e_tc_coop_start_serves_gui_and_stops_cleanly(tmp_path, monkeypatch) -> None:
    # no coop_enabled in the file — `tc coop start` forces it on
    port = free_port()
    f = write_settings(tmp_path, coop_port=port, coop_loopback_open=True)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_socket(settings, proc)
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
        assert c.get("/api/ping").status_code == 200
        st = c.get("/api/status")
        assert st.status_code == 200
        assert st.json()["coop"] == {
            "enabled": True,
            "url": f"http://127.0.0.1:{port}/",
            "auth_required": False,
        }
        page = c.get("/")
        assert page.status_code == 200 and "TurboChickens" in page.text
        assert c.get("/app.js").status_code == 200
        assert c.get("/styles.css").status_code == 200

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
        out = proc.stdout.read()
        assert "coop: gui at" in out
        assert "coop auth token" not in out  # open mode: no token printed
        with pytest.raises(httpx.ConnectError):
            c.get("/api/ping")  # HTTP server torn down with the daemon
        events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
        assert "coop.started" in events and "coop.stopped" in events
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_e2e_coop_start_non_loopback_prints_ephemeral_token_once(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_host="0.0.0.0", coop_port=port)  # no coop_enabled, no token
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "coop", "start"],
        env=env,
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
    try:
        _wait_socket(settings, proc)
        deadline = time.time() + 10
        token = None
        while time.time() < deadline:
            token_lines = [l for l in lines if l.startswith("coop auth token: ")]
            if token_lines:
                token = token_lines[0].split(": ", 1)[1].strip()
                break
            time.sleep(0.05)
        assert token, f"no ephemeral token printed; stdout so far: {lines}"

        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
        assert c.get("/api/ping").status_code == 200  # healthcheck stays open
        assert c.get("/api/status").status_code == 401
        r = c.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["coop"]["auth_required"] is True
        assert c.get("/").status_code == 200  # SPA loads; login card shows on the 401

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
        reader.join(timeout=5)
        token_lines = [l for l in lines if l.startswith("coop auth token: ")]
        assert len(token_lines) == 1  # printed exactly once in the whole run
        assert token not in settings.audit_log_path.read_text()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
