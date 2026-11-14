"""Tests for spec 13: Jobs / Audit / Settings views.

The three new read-only socket ops (jobs_list / audit_tail / settings_get) and
their HTTP mirrors (GET /api/jobs, GET /api/audit?limit=&event=, GET /api/settings)
against a real daemon on a temp data dir + temp port, driven exactly the way the
browser does (httpx). Assertions are external, observable behavior only: socket
payloads, HTTP status codes and JSON bodies, served SPA markup, audit contents.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, write_job
from turbochickens.audit import AuditLog
from turbochickens.client import request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash


@pytest.fixture
def daemon_file(tmp_path, monkeypatch):
    f = write_settings(tmp_path)  # plain daemon, coop disabled
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def daemon(daemon_file):
    settings = load_settings(daemon_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch):
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


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
def auth_daemon(tmp_path, monkeypatch):
    f = write_settings(
        tmp_path,
        coop_enabled=True,
        coop_host="0.0.0.0",
        coop_port=free_port(),
        coop_auth_token="spec13-secret-token",
    )
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- socket ops, standalone ----------------------------------------------------


def test_jobs_list_empty_then_registered_after_enqueue(daemon, fake_vibe, tmp_path) -> None:
    sock = daemon.settings.socket_path
    resp = request(sock, "jobs_list", timeout=2.0)
    assert resp["ok"] is True and resp["op"] == "jobs_list" and resp["jobs"] == []

    spec_path = write_job(tmp_path, name="spec13-job")
    digest = spec_hash(load_spec(spec_path))
    enq = request(sock, "enqueue", timeout=5.0, spec_path=str(spec_path.resolve()), spec_hash=digest)
    assert enq["ok"] is True

    resp = request(sock, "jobs_list", timeout=2.0)
    assert resp["ok"] is True and len(resp["jobs"]) == 1
    job = resp["jobs"][0]
    assert job["job_id"] == "spec13-job"
    assert job["name"] == "spec13-job"
    assert job["spec_hash"] == digest
    assert job["registered_at"]


def test_audit_tail_newest_first_with_limit(daemon) -> None:
    sock = daemon.settings.socket_path
    resp = request(sock, "audit_tail", limit=5, timeout=2.0)
    assert resp["ok"] is True and resp["op"] == "audit_tail" and resp["limit"] == 5
    assert resp["count"] == len(resp["events"]) <= 5
    on_disk = AuditLog(daemon.settings.audit_log_path).read_all()
    assert resp["events"] == on_disk[-5:][::-1]  # newest first
    ts = [e["ts"] for e in resp["events"]]
    assert ts == sorted(ts, reverse=True)
    assert any(e["event"] == "coopd.started" for e in resp["events"])

    default = request(sock, "audit_tail", timeout=2.0)
    assert default["ok"] is True and default["limit"] == 100
    assert default["events"] == on_disk[-100:][::-1]


def test_audit_tail_event_filter(daemon) -> None:
    sock = daemon.settings.socket_path
    resp = request(sock, "audit_tail", limit=50, event="coopd.started", timeout=2.0)
    assert resp["ok"] is True and resp["event"] == "coopd.started"
    assert resp["count"] >= 1
    assert all(e["event"] == "coopd.started" for e in resp["events"])

    none = request(sock, "audit_tail", limit=50, event="no.such.event", timeout=2.0)
    assert none["ok"] is True and none["events"] == [] and none["count"] == 0


def test_audit_tail_limit_validation(daemon) -> None:
    sock = daemon.settings.socket_path
    for bad in (0, -1, 1001, "abc"):
        resp = request(sock, "audit_tail", limit=bad, timeout=2.0)
        assert resp["ok"] is False and "limit" in resp["error"], bad
    ok = request(sock, "audit_tail", limit=1000, timeout=2.0)
    assert ok["ok"] is True and ok["limit"] == 1000


def test_settings_get_effective_settings_never_the_token(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_auth_token="spec13-secret-token", coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    try:
        resp = request(settings.socket_path, "settings_get", timeout=2.0)
        assert resp["ok"] is True and resp["op"] == "settings_get"
        s = resp["settings"]
        # spec 32 (G17): paths are served data-dir-relative — no host absolute path
        assert s["data_dir"] == "."
        assert s["socket_path"] == "daemon.sock"
        assert s["db_path"] == "turbochickens.db"
        assert s["audit_log_path"] == "audit.jsonl"
        assert s["weight_budget"] == 4 and s["weight_budget_override"] == 4
        assert s["audit_retention"] == "forever" and s["results_retention_months"] == 6
        assert s["coop"]["enabled"] is False  # coop not enabled here
        assert "spec13-secret-token" not in json.dumps(resp)  # token never served
    finally:
        dt.stop()


def test_audit_log_tail_pure_seam(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append({"event": "egg.queued" if i % 2 else "coopd.started", "n": i})
    log.path.write_text(log.path.read_text() + "{torn line\n")  # crash-torn final line
    assert [r["n"] for r in log.tail(2)] == [4, 3]  # newest first
    assert [r["n"] for r in log.tail(10)] == [4, 3, 2, 1, 0]
    assert [r["n"] for r in log.tail(10, event="coopd.started")] == [4, 2, 0]
    assert log.tail(0) == []


# --- HTTP mirrors: parity with the socket ops ------------------------------------


def test_api_jobs_mirrors_socket(coop_daemon, fake_vibe, tmp_path) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    assert c.get("/api/jobs").json() == request(sock, "jobs_list", timeout=2.0)

    spec_path = write_job(tmp_path, name="spec13-http-job")
    digest = spec_hash(load_spec(spec_path))
    assert request(sock, "enqueue", timeout=5.0, spec_path=str(spec_path.resolve()), spec_hash=digest)["ok"]

    http = c.get("/api/jobs")
    assert http.status_code == 200
    assert http.json() == request(sock, "jobs_list", timeout=2.0)  # identical payload
    job = http.json()["jobs"][0]
    assert job["name"] == "spec13-http-job" and job["spec_hash"] == digest


def test_api_audit_mirrors_socket_with_limit_and_event(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    sock = coop_daemon.settings.socket_path
    r = c.get("/api/audit?limit=3")
    assert r.status_code == 200
    assert r.json() == request(sock, "audit_tail", limit=3, timeout=2.0)

    r = c.get("/api/audit?limit=50&event=coop.started")
    assert r.status_code == 200
    body = r.json()
    assert body == request(sock, "audit_tail", limit=50, event="coop.started", timeout=2.0)
    assert body["count"] >= 1 and all(e["event"] == "coop.started" for e in body["events"])

    r = c.get("/api/audit")  # no params → defaults
    assert r.status_code == 200 and r.json()["limit"] == 100

    assert c.get("/api/audit?limit=nope").status_code == 400
    assert c.get("/api/audit?limit=0").json()["ok"] is False  # daemon-side validation, 200 mirror


def test_api_settings_mirrors_socket_and_never_serves_token(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    headers = {"Authorization": "Bearer spec13-secret-token"}
    r = c.get("/api/settings", headers=headers)
    assert r.status_code == 200
    assert r.json() == request(auth_daemon.settings.socket_path, "settings_get", timeout=2.0)
    assert "spec13-secret-token" not in r.text
    assert r.json()["settings"]["coop"] == {
        "enabled": True,
        "url": auth_daemon.settings.coop_url,
        "auth_required": True,
    }


def test_api_views_empty_states_and_filter_over_http(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    jobs = c.get("/api/jobs")
    assert jobs.status_code == 200 and jobs.json()["jobs"] == []  # empty state source
    filt = c.get("/api/audit?event=definitely.not.an.event")
    assert filt.status_code == 200
    assert filt.json()["ok"] is True and filt.json()["events"] == []  # empty filter state
    settings = c.get("/api/settings")
    assert settings.status_code == 200 and settings.json()["settings"]["weight_budget"] == 4


def test_api_views_require_bearer_when_auth_required(auth_daemon) -> None:
    c = http_client(auth_daemon.settings)
    for path in ("/api/jobs", "/api/audit", "/api/settings"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path
        r = c.get(path, headers={"Authorization": "Bearer spec13-secret-token"})
        assert r.status_code == 200 and r.json()["ok"] is True, path
    assert c.get("/api/ping").status_code == 200  # healthcheck stays open


def test_api_views_daemon_down_payload_not_500(tmp_path, monkeypatch) -> None:
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
                for path in ("/api/jobs", "/api/audit?limit=5", "/api/settings"):
                    r = await c.get(path)
                    out[path] = (r.status_code, r.json())
                ping = await c.get("/api/ping")
                out["/api/ping"] = (ping.status_code, ping.json())
                return out
        finally:
            await server.stop()

    out = asyncio.run(go())
    for path in ("/api/jobs", "/api/audit?limit=5", "/api/settings"):
        code, body = out[path]
        assert code == 200, path  # structured payload, not a broken 500
        assert body["ok"] is False and body["daemon"]["up"] is False and body["error"], path
    assert out["/api/ping"][0] == 503 and out["/api/ping"][1]["pong"] is False


# --- SPA serves the three views ----------------------------------------------------


def test_spa_serves_jobs_audit_settings_views(coop_daemon) -> None:
    c = http_client(coop_daemon.settings)
    html = c.get("/").text
    for marker in (
        'id="view-jobs"', 'id="jobs-tbody"', 'id="jobs-empty"',
        'id="view-audit"', 'id="audit-chips"', 'id="audit-log"', 'id="audit-empty"',
        'id="view-settings"', 'id="settings-list"', 'id="settings-empty"',
    ):
        assert marker in html, marker
    js = c.get("/app.js").text
    for api in ('"/api/jobs"', '"/api/audit?limit=200"', '"/api/audit?limit=100&event="', '"/api/settings"'):
        assert api in js, api
    assert "loadJobs" in js and "loadAudit" in js and "loadSettings" in js
    css = c.get("/styles.css").text
    for cls in (".data-table", ".chip", ".log-line", ".settings-row"):
        assert cls in css, cls
