"""Tests for spec-32 ticket 08 (review finding G5): gate listings serve refs
only; literal header secrets are rejected at registration.

Seams (highest existing first):
- the pure validation/serving seams of `connectors.py`
  (`validate_connector_payload`, `public_entry`, `load_connectors`);
- the daemon socket ops `connectors_add` / `connectors_list` (thread daemon on
  a temp socket) + the `tc connectors` CLI;
- the Coop HTTP mirrors `POST /api/connectors/add` / `GET /api/connectors`
  (thread daemon with the coop on) + the served SPA assets;
- a subprocess-free e2e with the fake vibe binary ($TURBOCHICKENS_VIBE_BIN)
  proving a `$ref` gate still registers, lists, and injects (resolves) into the
  chicken's ephemeral harness config.

Every fix has a negative test (a literal header secret is refused with its
field-named `connectors.<name>.headers` error) and a positive test (a `$ref`
gate works end to end). A planted marker secret is swept over socket responses,
HTTP responses, served assets, and Headcount rows — it never appears; it lives
only at rest in the 0600 registry, operator-owned.
"""

from __future__ import annotations

import json
import stat
import tomllib

import pytest
from typer.testing import CliRunner

from test_chickensubs import wait_terminal
from test_coop import free_port, http_client, write_settings
from test_connectors import FAKE_VIBE_GATES, SECRET, write_job_conn
from test_daemon import DaemonThread
from test_run_e2e import db_row
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.connectors import WITHHELD_HEADER, load_connectors, public_entry, validate_connector_payload
from turbochickens.settings import load_settings

runner = CliRunner()

#: the planted literal header secret — must never appear in any served payload
MARKER = "G5-LITERAL-HEADER-MARKER-3f9a-do-not-serve"


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    monkeypatch.delenv("TURBOCHICKENS_CONNECTORS", raising=False)
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-vibe-gates"
    bin_path.write_text(FAKE_VIBE_GATES)
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


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch):
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    monkeypatch.delenv("TURBOCHICKENS_CONNECTORS", raising=False)
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- pure seams: registration validation + the listing summary ----------------


def test_validate_connector_payload_rejects_literal_header_secrets() -> None:
    # a literal header secret (with or without a "Bearer " prefix) → field-named refusal
    for headers in ({"Authorization": f"Bearer {MARKER}"}, {"X-Api-Key": MARKER}, {"A": "sk-live-123"}):
        entry, errs = validate_connector_payload(
            "leaky", {"transport": "https", "url": "https://x.example/mcp", "headers": headers}
        )
        assert entry is None, headers
        assert errs and errs[0]["field"] == "connectors.leaky.headers", errs
        assert "ref" in errs[0]["message"]
        assert MARKER not in json.dumps(errs) and "sk-live-123" not in json.dumps(errs)
    # a $ref header still validates
    entry, errs = validate_connector_payload(
        "webx", {"transport": "https", "url": "https://x.example/mcp", "headers": {"Authorization": "Bearer $TC_GATE_SECRET"}}
    )
    assert entry is not None and not errs
    # stdio env literals remain allowed at rest (only key names are ever served)
    entry, errs = validate_connector_payload("toolx", {"transport": "stdio", "command": "/bin/t", "env": {"K": MARKER}})
    assert entry is not None and not errs


def test_public_entry_withholds_at_rest_literal_headers(tmp_path) -> None:
    # a hand-edited registry carrying a literal header (legacy, operator-owned
    # 0600 file) still loads, but the listing seam never serves the literal
    f = tmp_path / "connectors.toml"
    f.write_text(
        '[connectors.legacy]\ntransport = "https"\nurl = "https://l.example/mcp"\n'
        f'headers = {{ Authorization = "Bearer {MARKER}" }}\n'
        '[connectors.webx]\ntransport = "https"\nurl = "https://w.example/mcp"\n'
        'headers = { Authorization = "Bearer $TC_GATE_SECRET" }\n'
    )
    cfg = load_connectors(f, tmp_path)
    legacy = public_entry("legacy", cfg.connectors["legacy"])
    assert legacy["headers"] == {"Authorization": WITHHELD_HEADER}
    assert MARKER not in json.dumps(legacy)
    webx = public_entry("webx", cfg.connectors["webx"])
    assert webx["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}  # refs stay refs


# --- negative: literal header secret refused at registration (socket + CLI) -----


def test_literal_header_rejected_at_registration_over_socket(daemon) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    for headers in ({"Authorization": f"Bearer {MARKER}"}, {"X-Api-Key": MARKER}):
        r = request(
            sock,
            "connectors_add",
            timeout=5.0,
            name="leaky",
            transport="https",
            url="https://mcp.example.com/mcp",
            headers=headers,
        )
        assert r["ok"] is False and r["valid"] is False
        assert any(e["field"] == "connectors.leaky.headers" for e in r["errors"]), r["errors"]
        assert MARKER not in json.dumps(r)
    # nothing was written to the registry
    assert not (settings.data_dir / "connectors.toml").exists()
    # the refusals are audited as gate.rejected — name + field names only, no marker
    rejected = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "gate.rejected"]
    assert len(rejected) == 2 and all(r["name"] == "leaky" for r in rejected)
    assert all("connectors.leaky.headers" in r["fields"] for r in rejected)
    assert MARKER not in settings.audit_log_path.read_text()
    # the listing stays empty and clean
    r = request(sock, "connectors_list", timeout=5.0)
    assert r["ok"] is True and r["connectors"] == []
    assert MARKER not in json.dumps(r)


def test_cli_literal_header_refused(daemon) -> None:
    result = runner.invoke(
        app,
        ["connectors", "add", "leaky", "--transport", "https", "--url", "https://mcp.example.com/mcp",
         "--header", f"Authorization=Bearer {MARKER}"],
    )
    assert result.exit_code == 1
    assert "connectors.leaky.headers" in result.output
    assert MARKER not in result.output
    result = runner.invoke(app, ["connectors", "list"])
    assert result.exit_code == 0 and "no gates registered" in result.output
    assert MARKER not in result.output


# --- refs-only listings + marker sweep over socket, HTTP, assets, Headcount ------


def test_listings_serve_refs_only_and_marker_sweep(coop_daemon, monkeypatch) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    c = http_client(settings)
    monkeypatch.setenv("TC_GATE_SECRET", SECRET)

    # plant the marker at rest in a hand-edited registry, next to a $ref gate
    reg = settings.data_dir / "connectors.toml"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        '[connectors.legacy]\ntransport = "https"\nurl = "https://legacy.example/mcp"\n'
        f'headers = {{ Authorization = "Bearer {MARKER}" }}\n'
        '[connectors.webx]\ntransport = "https"\nurl = "https://mcp.example.com/mcp"\n'
        'headers = { Authorization = "Bearer $TC_GATE_SECRET" }\n'
    )
    assert MARKER in reg.read_text()  # the plant is real: the secret is at rest

    # socket listing: refs only — the at-rest literal is withheld
    r = request(sock, "connectors_list", timeout=5.0)
    assert r["ok"] is True
    by_name = {e["name"]: e for e in r["connectors"]}
    assert by_name["webx"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    assert by_name["legacy"]["headers"] == {"Authorization": WITHHELD_HEADER}
    assert MARKER not in json.dumps(r) and SECRET not in json.dumps(r)

    # HTTP mirror: identical payload, marker never in the body
    h = c.get("/api/connectors")
    assert h.status_code == 200 and h.json() == r
    assert MARKER not in h.text

    # literal registration refused over HTTP: 422, field-named, HTTP == socket
    body = {"name": "leaky", "transport": "https", "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": f"Bearer {MARKER}"}}
    a = c.post("/api/connectors/add", json=body)
    assert a.status_code == 422
    assert any(e["field"] == "connectors.leaky.headers" for e in a.json()["errors"])
    assert MARKER not in a.text
    assert a.json() == request(sock, "connectors_add", timeout=5.0, **body)

    # add-response echo for a $ref gate serves the ref only
    a = c.post("/api/connectors/add", json={"name": "webx2", "transport": "https",
                                            "url": "https://mcp2.example.com/mcp",
                                            "headers": {"Authorization": "Bearer $TC_GATE_SECRET"}})
    assert a.status_code == 200
    assert a.json()["entry"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    assert MARKER not in a.text and SECRET not in a.text
    # the rewrite keeps the at-rest literal on disk (0600) without serving it
    assert MARKER in reg.read_text()
    assert stat.S_IMODE(reg.stat().st_mode) == 0o600
    listed = c.get("/api/connectors").json()
    by_name = {e["name"]: e for e in listed["connectors"]}
    assert by_name["legacy"]["headers"] == {"Authorization": WITHHELD_HEADER}
    assert MARKER not in c.get("/api/connectors").text

    # marker sweep: served SPA assets + Headcount rows never carry the marker
    for path in ("/", "/app.js", "/styles.css"):
        t = c.get(path).text
        assert MARKER not in t, path
    audit_text = settings.audit_log_path.read_text()
    assert MARKER not in audit_text
    assert all(MARKER not in json.dumps(row) for row in AuditLog(settings.audit_log_path).read_all())


# --- positive: a $ref gate registers, lists, and injects end to end ---------------


def test_ref_gate_registers_lists_and_injects_e2e(daemon, tmp_path, monkeypatch) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", SECRET)

    # register over the socket — the add-response echo serves the ref only
    r = request(
        sock, "connectors_add", timeout=5.0, name="webx", transport="https",
        url="https://mcp.example.com/mcp", headers={"Authorization": "Bearer $TC_GATE_SECRET"},
    )
    assert r["ok"] is True and r["entry"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    assert SECRET not in json.dumps(r) and MARKER not in json.dumps(r)
    # list serves the ref
    by_name = {e["name"]: e for e in request(sock, "connectors_list", timeout=5.0)["connectors"]}
    assert by_name["webx"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    # CLI parity: register + list via tc — ref shown, secret never printed
    result = runner.invoke(
        app, ["connectors", "add", "webx2", "--transport", "https", "--url", "https://mcp2.example.com/mcp",
              "--header", "Authorization=Bearer $TC_GATE_SECRET"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["connectors", "list"])
    assert result.exit_code == 0
    assert "gate=webx2  transport=https" in result.output
    assert "headers={'Authorization': 'Bearer $TC_GATE_SECRET'}" in result.output
    assert SECRET not in result.output and MARKER not in result.output

    # e2e: the egg opts into webx; the ref resolves at injection into the
    # chicken's ephemeral harness config only
    dump = tmp_path / "dumped-config.toml"
    spec = write_job_conn(tmp_path, "gated", prompt=f"GATES DUMPCONFIG {dump}", connectors=["webx"])
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is True
    run = wait_terminal(sock, r["run_id"], timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")

    out = request(sock, "results_get", run_id=r["run_id"], timeout=5.0)
    probe = out["output"]
    assert probe["config_present"] is True
    assert probe["mcp_servers"]["webx"]["url"] == "https://mcp.example.com/mcp"
    assert probe["mcp_servers"]["webx"]["header_keys"] == ["Authorization"]
    assert probe["mcp_servers"]["webx"]["header_resolved"] is True  # $TC_GATE_SECRET resolved at injection
    # the raw injected config (side copy) carries the resolved value — ephemeral workspace only
    raw_cfg = tomllib.loads(dump.read_text())
    assert raw_cfg["mcp_servers"]["webx"]["headers"]["Authorization"] == f"Bearer {SECRET}"

    # manifest carries the gate name only; attested teardown as usual
    assert run["manifest"]["harness"]["connectors"] == ["webx"]
    assert db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (r["run_id"],),
    ) == (1, 1, 1)

    # secrecy sweep over every served payload + the Headcount: no secret, no marker
    blob = json.dumps(
        [
            request(sock, "connectors_list", timeout=5.0),
            request(sock, "auth_access", timeout=5.0),
            request(sock, "runs_show", run_id=r["run_id"], timeout=5.0),
            request(sock, "runs_list", timeout=5.0),
            request(sock, "runs_logs", run_id=r["run_id"], timeout=5.0),
            request(sock, "status", timeout=5.0),
            out["output"],
            AuditLog(settings.audit_log_path).read_all(),
        ]
    )
    assert SECRET not in blob and MARKER not in blob
    assert SECRET not in settings.audit_log_path.read_text()
    assert MARKER not in settings.audit_log_path.read_text()
