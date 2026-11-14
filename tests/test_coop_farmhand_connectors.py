"""Tests for spec 31: the Farmhand view & the Connectors (gates) CRUD view.

Seams (per the spec's checkbox list):
- the new socket ops ``farmhand_info`` / ``settings_set`` and their HTTP
  mirrors ``GET /api/farmhand`` / ``POST /api/settings``: payload parity,
  200/422/400 mapping, dead-socket ``200 {ok:false, daemon.up:false}`` (POSTs
  included, never a 500), bearer 401 when auth is required with ``/api/ping``
  open;
- the Farmhand payload: active transport(s), the HTTPS endpoint URL on the
  coop host + mounted flag, the exact 17-tool surface (no auth_*), the stdio
  command, and ready-to-paste client-config snippets whose bearer header is a
  placeholder only;
- secrecy: with a configured coop token, no response body, served asset, or
  audit row ever contains it (nor gate env/header values);
- ``settings_set`` validation server-side: ``extra="forbid"`` intact (an
  unknown key like ``max_tokens`` is a field-named error), read-only keys
  refused field-named, invalid enum refused, and the runtime effect — the coop
  mounts/unmounts the Farmhand HTTPS endpoint (``/mcp`` answers vs 404s) as
  ``farmhand_transport`` crosses the https boundary, audited as
  ``settings.changed``;
- the Connectors view's CRUD over the existing mirrors
  (``/api/connectors`` + ``/add`` + ``/rm``): list/add/remove with non-secret
  summaries, inline field-named errors for unknown-transport or
  missing-field entries, audit via the socket ops (``gate.*``);
- served SPA markup for both views (nav entries, forms, tables, snippets, new
  icons), app.js/css hooks, no host paths in the assets;
- a subprocess e2e (``tc coop start``): farmhand info + transport switching
  (mount/unmount of /mcp) + gates CRUD entirely over HTTP.

All assertions are external, observable behavior: socket payloads, HTTP status
codes and JSON bodies, served markup, audit contents.
"""

from __future__ import annotations

import asyncio
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
from test_farmhand import EXPECTED_TOOLS, MCP_INIT_BODY, MCP_INIT_HEADERS
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings

runner = CliRunner()

COOP_TOKEN = "spec31-coop-token-do-not-serve"
GATE_SECRET = "SPEC31-GATE-SECRET-9f2e-do-not-serve"


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
def stdio_daemon(tmp_path, monkeypatch):
    """Coop on, but farmhand_transport = "stdio" — the HTTPS endpoint starts
    unmounted (spec 31 switches it at runtime)."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port(), farmhand_transport="stdio")
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


# --- farmhand_info: socket op + GET /api/farmhand parity, payload shape ---------


def test_farmhand_info_socket_and_http_parity(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path

    sock_resp = request(sock, "farmhand_info", timeout=10.0)
    assert sock_resp["ok"], sock_resp
    http_resp = c.get("/api/farmhand")
    assert http_resp.status_code == 200, http_resp.text
    assert http_resp.json() == sock_resp

    info = sock_resp
    assert info["op"] == "farmhand_info"
    assert info["transport"] == "both"
    assert info["transports"] == ["stdio", "https"]
    assert info["https_path"] == "/mcp"
    assert info["https_url"] == coop_daemon.settings.coop_url.rstrip("/") + "/mcp"
    assert info["https_mounted"] is True
    assert info["coop_enabled"] is True
    assert info["auth_required"] is False
    # the exposed tool list is the exact 17-tool operator surface — no auth_*/
    # retention_sweep/shutdown/ping/jobs_upload
    assert {t["name"] for t in info["tools"]} == EXPECTED_TOOLS
    assert all(t.get("description") for t in info["tools"])
    # ready-to-paste client config: stdio command + https URL with a
    # placeholder bearer only
    assert info["stdio_command"] == {"command": "tc", "args": ["farmhand", "serve", "--transport", "stdio"]}
    cfg = info["client_config"]
    assert cfg["stdio"]["mcpServers"]["farmhand"]["command"] == "tc"
    assert cfg["stdio"]["mcpServers"]["farmhand"]["args"] == ["farmhand", "serve", "--transport", "stdio"]
    https_cfg = cfg["https"]["mcpServers"]["farmhand"]
    assert https_cfg["url"] == info["https_url"]
    assert https_cfg["headers"] == {"Authorization": "Bearer <coop token>"}
    assert cfg["https_note"] and "never served" in cfg["https_note"]


# --- secrecy: the bearer token never appears in any payload/asset/audit row ------


def test_farmhand_and_settings_secrecy_with_configured_token(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": f"Bearer {COOP_TOKEN}"}

    # 401 without the bearer on both new endpoints; /api/ping stays open
    assert c.get("/api/farmhand").status_code == 401
    assert c.post("/api/settings", json={"farmhand_transport": "https"}).status_code == 401
    assert c.get("/api/ping").status_code == 200
    assert COOP_TOKEN not in c.get("/api/farmhand").text

    r = c.get("/api/farmhand", headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["auth_required"] is True
    assert COOP_TOKEN not in r.text

    r = c.post("/api/settings", json={"farmhand_transport": "https"}, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["changed"] == {"farmhand_transport": "https"}
    assert COOP_TOKEN not in r.text
    # the settings payload inside the response never carries the token either
    assert "coop_auth_token" not in json.dumps(r.json()["settings"])

    # the served assets never contain it either
    for path in ("/", "/app.js", "/styles.css"):
        assert COOP_TOKEN not in c.get(path).text

    # nor does the audit log
    audit_text = AuditLog(auth_daemon.settings.audit_log_path).read_all()
    assert any(e["event"] == "settings.changed" for e in audit_text)
    assert all(COOP_TOKEN not in json.dumps(e) for e in audit_text)


# --- dead-socket transport: 200 {ok:false, daemon.up:false} on both, never 500 ----


def test_farmhand_and_settings_endpoints_daemon_down_not_500(tmp_path, monkeypatch) -> None:
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
                r = await c.get("/api/farmhand")
                out["GET /api/farmhand"] = (r.status_code, r.json())
                r = await c.post("/api/settings", json={"farmhand_transport": "https"})
                out["POST /api/settings"] = (r.status_code, r.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for key, (code, body) in out.items():
        assert code == 200, (key, code)  # structured payload, never a 500 — POST included
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], key


# --- settings_set: extra=forbid validation, read-only refusals, HTTP parity -------


def test_settings_set_validation_and_parity(stdio_daemon) -> None:
    c = http_client(stdio_daemon.settings)
    sock = stdio_daemon.settings.socket_path

    # starts as stdio: the HTTPS endpoint is not mounted
    info = c.get("/api/farmhand").json()
    assert info["transport"] == "stdio" and info["transports"] == ["stdio"]
    assert info["https_mounted"] is False
    assert info["https_url"] == stdio_daemon.settings.coop_url.rstrip("/") + "/mcp"
    assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 404

    # invalid enum → field-named refusal, HTTP == socket
    r = c.post("/api/settings", json={"farmhand_transport": "carrier-pigeon"})
    assert r.status_code == 422
    bad = r.json()
    sock_bad = request(sock, "settings_set", farmhand_transport="carrier-pigeon", timeout=5.0)
    assert bad == sock_bad
    assert bad["ok"] is False and bad["valid"] is False
    assert bad["errors"][0]["field"] == "farmhand_transport"

    # extra="forbid" intact server-side: an unknown key is a field-named error
    r = c.post("/api/settings", json={"max_tokens": 100})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "max_tokens"
    assert request(sock, "settings_set", max_tokens=100, timeout=5.0) == r.json()

    # a known but read-only key is refused field-named
    r = c.post("/api/settings", json={"data_dir": "/tmp/elsewhere"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "settings.data_dir"
    assert "read-only" in r.json()["errors"][0]["message"]

    # empty object / malformed body → clean errors
    r = c.post("/api/settings", json={})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = c.post("/api/settings", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert request(sock, "settings_set", timeout=5.0)["ok"] is False

    # a real switch: stdio → https mounts the Farmhand endpoint at runtime
    r = c.post("/api/settings", json={"farmhand_transport": "https"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == {"farmhand_transport": "https"}
    assert r.json()["settings"]["farmhand_transport"] == "https"
    # HTTP == socket parity: a repeated (no-op) update answers identically on both
    sock_noop = request(sock, "settings_set", farmhand_transport="https", timeout=10.0)
    http_noop = c.post("/api/settings", json={"farmhand_transport": "https"}).json()
    assert http_noop == sock_noop
    assert sock_noop["ok"] and sock_noop["changed"] == {}
    info = c.get("/api/farmhand").json()
    assert info["transport"] == "https" and info["https_mounted"] is True
    assert request(sock, "settings_get", timeout=2.0)["settings"]["farmhand_transport"] == "https"
    # /mcp now answers (loopback + no token → open, like /api/*)
    r = c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
    assert r.status_code == 200 and r.headers.get("mcp-session-id")

    # https → both keeps it mounted; both → stdio unmounts it again (404)
    assert c.post("/api/settings", json={"farmhand_transport": "both"}).json()["changed"] == {"farmhand_transport": "both"}
    assert c.get("/api/farmhand").json()["https_mounted"] is True
    assert c.post("/api/settings", json={"farmhand_transport": "stdio"}).status_code == 200
    info = c.get("/api/farmhand").json()
    assert info["transport"] == "stdio" and info["https_mounted"] is False
    assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 404
    # the GUI still serves while the endpoint is unmounted
    assert c.get("/").status_code == 200

    # the switches were audited (field names + values — none secret)
    audit = AuditLog(stdio_daemon.settings.audit_log_path).read_all()
    changes = [e for e in audit if e["event"] == "settings.changed"]
    assert [e["changes"] for e in changes] == [
        {"farmhand_transport": "https"},
        {"farmhand_transport": "both"},
        {"farmhand_transport": "stdio"},
    ]


# --- connectors view: CRUD over the mirrors, inline field-named errors, audit ------


def test_connectors_view_crud_inline_errors_and_audit(coop_daemon, monkeypatch) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)

    # empty list: HTTP == socket
    assert c.get("/api/connectors").json() == request(sock, "connectors_list", timeout=2.0)
    assert c.get("/api/connectors").json()["connectors"] == []

    # add a stdio gate whose env value is a literal secret — only key names served
    r = c.post(
        "/api/connectors/add",
        json={"name": "toolx", "transport": "stdio", "command": "/bin/toolx", "args": ["--serve"], "env": {"GATE_TOKEN": GATE_SECRET}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["entry"] == {"name": "toolx", "transport": "stdio", "command": "/bin/toolx", "args": ["--serve"], "env_keys": ["GATE_TOKEN"]}
    assert GATE_SECRET not in r.text

    # add an https gate with a $REF header
    r = c.post("/api/connectors/add", json={"name": "webx", "transport": "https", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer $TC_GATE_SECRET"}})
    assert r.status_code == 200 and r.json()["entry"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}

    # list: non-secret summaries, HTTP == socket
    listed = c.get("/api/connectors").json()
    assert listed == request(sock, "connectors_list", timeout=2.0)
    by_name = {e["name"]: e for e in listed["connectors"]}
    assert by_name["toolx"]["env_keys"] == ["GATE_TOKEN"]
    assert GATE_SECRET not in c.get("/api/connectors").text

    # inline errors: missing command (stdio), unknown transport, bad url
    r = c.post("/api/connectors/add", json={"name": "broken", "transport": "stdio"})
    assert r.status_code == 422
    body = r.json()
    assert body == request(sock, "connectors_add", name="broken", transport="stdio", timeout=5.0)
    assert body["errors"][0]["field"].startswith("connectors.broken.")
    r = c.post("/api/connectors/add", json={"name": "pigeon", "transport": "carrier-pigeon"})
    assert r.status_code == 422 and r.json()["errors"]
    assert r.json() == request(sock, "connectors_add", name="pigeon", transport="carrier-pigeon", timeout=5.0)
    r = c.post("/api/connectors/add", json={"name": "badurl", "transport": "https", "url": "ftp://x"})
    assert r.status_code == 422 and any("connectors.badurl.url" in e["field"] for e in r.json()["errors"])

    # remove: 200 then 422 unknown gate; HTTP == socket (modulo the name)
    sock_rm = request(sock, "connectors_rm", name="toolx", timeout=5.0)
    assert sock_rm["ok"] and sock_rm["name"] == "toolx"
    rm = c.post("/api/connectors/rm", json={"name": "webx"})
    assert rm.status_code == 200 and rm.json()["name"] == "webx"
    assert {k: v for k, v in rm.json().items() if k not in ("name", "transport")} == {
        k: v for k, v in sock_rm.items() if k not in ("name", "transport")
    }
    r = c.post("/api/connectors/rm", json={"name": "webx"})
    assert r.status_code == 422 and "unknown gate" in r.json()["error"]
    assert c.get("/api/connectors").json()["connectors"] == []

    # the changes were audit-logged via the socket ops — name/transport/fields only
    audit = AuditLog(coop_daemon.settings.audit_log_path).read_all()
    gate_rows = [e for e in audit if str(e.get("event", "")).startswith("gate.")]
    assert {e["event"] for e in gate_rows} >= {"gate.registered", "gate.removed", "gate.rejected"}
    assert all(GATE_SECRET not in json.dumps(e) for e in gate_rows)
    registry = coop_daemon.settings.data_dir / "connectors.toml"
    assert registry.exists() and (registry.stat().st_mode & 0o777) == 0o600


# --- served SPA markup: both views, nav, icons, no host paths ----------------------


def test_spa_serves_farmhand_and_connectors_views(coop_daemon, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    # nav entries (design system shell)
    for marker in (
        'data-view="farmhand"', 'src="/icons/bot.svg"', ">Farmhand</a>",
        'data-view="connectors"', 'src="/icons/plug.svg"', ">Gates</a>",
        # farmhand view
        'id="view-farmhand"', 'id="farmhand-form"', 'id="fh-transport"', 'id="fh-save"', "Save transport",
        '<option value="stdio">stdio</option>', '<option value="https">https</option>', '<option value="both">both</option>',
        'id="farmhand-error"', 'id="farmhand-card"', 'id="farmhand-list"', 'id="farmhand-empty"',
        'id="farmhand-tools-card"', 'id="farmhand-tools-tbody"',
        'id="farmhand-snippet-card"', 'id="fh-snippet-stdio"', 'id="fh-snippet-https"',
        "the coop token itself never appears",
        # connectors view
        'id="view-connectors"', 'id="gate-form"', 'id="gate-name"', 'id="gate-transport"',
        'id="gate-command"', 'id="gate-args"', 'id="gate-env"', 'id="gate-url"', 'id="gate-headers"',
        'id="gate-submit"', "Register gate", 'id="connectors-tbody"', 'id="connectors-card"', 'id="connectors-empty"', 'id="connectors-error"',
    ):
        assert marker in html, marker

    js = c.get("/app.js").text
    for marker in (
        '"farmhand", "connectors"', "loadFarmhand", "loadConnectors",
        '"/api/farmhand"', '"/api/settings"', '"/api/connectors"', '"/api/connectors/add"', '"/api/connectors/rm"',
        "renderFarmhand", "toggleGateTransportFields", "parseKvLines", "gateSummaryText", "removeGate", "setGateError",
    ):
        assert marker in js, marker

    css = c.get("/styles.css").text
    for cls in (".farmhand-form", ".farmhand-card", ".snippet-card", ".gate-form-card", ".gate-grid", ".gate-remove", ".gate-summary", ".card-head"):
        assert cls in css, cls

    # the new lucide icons are served locally with the right content type
    for icon, frag in (("/icons/bot.svg", '<rect width="16" height="12" x="4" y="8" rx="2"/>'), ("/icons/plug.svg", 'M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z')):
        r = c.get(icon)
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
        assert frag in r.text

    # no host paths ever enter the served assets; no token material either
    for asset in (html, js, css):
        assert str(tmp_path) not in asset


# --- subprocess e2e: farmhand info + transport switch + gates CRUD over HTTP --------


def test_e2e_farmhand_view_and_gates_over_http(tmp_path, monkeypatch) -> None:
    port = free_port()
    f = write_settings(tmp_path, coop_port=port)  # `tc coop start` forces coop on; transport defaults to both
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)
    settings = load_settings(f)
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
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0)
        assert c.get("/api/ping").status_code == 200

        # the Farmhand view's data over HTTP
        r = c.get("/api/farmhand")
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["transport"] == "both" and info["https_mounted"] is True
        assert info["https_url"] == f"http://127.0.0.1:{port}/mcp"
        assert {t["name"] for t in info["tools"]} == EXPECTED_TOOLS
        assert info["client_config"]["https"]["mcpServers"]["farmhand"]["headers"] == {"Authorization": "Bearer <coop token>"}
        # the endpoint is mounted: MCP initialize answers (loopback-open)
        r = c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS)
        assert r.status_code == 200 and r.headers.get("mcp-session-id")

        # switch the transport over HTTP: both → stdio unmounts /mcp, back to both remounts
        r = c.post("/api/settings", json={"farmhand_transport": "stdio"})
        assert r.status_code == 200 and r.json()["changed"] == {"farmhand_transport": "stdio"}
        assert c.get("/api/farmhand").json()["https_mounted"] is False
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 404
        assert c.get("/api/settings").json()["settings"]["farmhand_transport"] == "stdio"
        r = c.post("/api/settings", json={"farmhand_transport": "both"})
        assert r.status_code == 200
        assert c.get("/api/farmhand").json()["https_mounted"] is True
        assert c.post("/mcp", json=MCP_INIT_BODY, headers=MCP_INIT_HEADERS).status_code == 200
        # invalid switch → 422 field-named
        r = c.post("/api/settings", json={"farmhand_transport": "nope"})
        assert r.status_code == 422 and r.json()["errors"][0]["field"] == "farmhand_transport"

        # gates CRUD entirely over HTTP, secret never served
        r = c.post("/api/connectors/add", json={"name": "e2egate", "transport": "stdio", "command": "/bin/e2e", "env": {"K": GATE_SECRET}})
        assert r.status_code == 200, r.text
        assert r.json()["entry"]["env_keys"] == ["K"] and GATE_SECRET not in r.text
        listed = c.get("/api/connectors").json()
        assert [e["name"] for e in listed["connectors"]] == ["e2egate"]
        assert GATE_SECRET not in c.get("/api/connectors").text
        r = c.post("/api/connectors/add", json={"name": "half", "transport": "https"})
        assert r.status_code == 422 and any(e["field"].startswith("connectors.half.") for e in r.json()["errors"])
        assert c.post("/api/connectors/rm", json={"name": "e2egate"}).status_code == 200
        assert c.get("/api/connectors").json()["connectors"] == []

        # the SPA serves both views
        page = c.get("/")
        assert page.status_code == 200
        for marker in ('id="view-farmhand"', 'id="view-connectors"', 'id="fh-transport"', 'id="gate-form"', 'data-view="farmhand"', 'data-view="connectors"'):
            assert marker in page.text, marker

        # audit: the transport switches + gate CRUD landed in the headcount
        audit = AuditLog(settings.audit_log_path).read_all()
        events = [e["event"] for e in audit]
        assert "settings.changed" in events
        assert {"gate.registered", "gate.removed", "gate.rejected"} <= set(events)
        assert all(GATE_SECRET not in json.dumps(e) for e in audit)

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=30)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        logf.close()
