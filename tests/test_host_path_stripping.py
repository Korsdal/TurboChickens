"""Tests for spec 32 ticket 22 (review finding G17): no host absolute paths in
served payloads — the ping half is ticket 05 and not covered here.

Seams (per the ticket's checkbox list):
- the grep-guard: status, manifest, results, Beak errors, and Headcount
  responses (socket ops and their HTTP mirrors) never contain a host absolute
  path — asserted against the temp data dir / tmp root that would leak;
- positive tests: all views still render with run-relative identifiers, and
  the operator's local CLI output is unchanged where it is local-only
  (`tc status` / `tc runs show` / `tc results get` still print the operator's
  own absolute paths, derived from local settings);
- every stripped field also gets a negative test (the attack/old leak is
  refused or absent) and a positive test (the legitimate path still works).

All assertions are external, observable behavior: socket payloads, HTTP status
codes and bodies, CLI output, audit contents, on-disk state.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import socket as socket_mod
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from test_chaining import write_chain_job
from test_coop import free_port, http_client, write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, run_id_of, wait_terminal, write_job
from test_stats_retention import backdate_run, months_ago_iso
from turbochickens import beak
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.coop import CoopServer, SocketTransport
from turbochickens.settings import load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.validate import validate_output

runner = CliRunner()


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def coop_daemon(tmp_path, monkeypatch, fake_vibe):
    """Coop-enabled daemon in a thread, temp data dir, fake vibe harness."""
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def write_strict_job(tmp_path: Path, name: str, prompt: str = "Say hello") -> Path:
    """A job whose schema the fake vibe output fails → quarantined chicken."""
    spec_path = write_job(tmp_path, name=name, prompt=prompt)
    (spec_path.parent / "schemas" / "out.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["message"],
                "additionalProperties": False,
                "properties": {"message": {"const": "not-what-fake-vibe-says"}},
            }
        )
    )
    return spec_path


def enqueue(sock, spec_path: Path, **extra) -> str:
    spec = load_spec(spec_path)
    resp = request(sock, "enqueue", spec_path=str(spec_path), spec_hash=spec_hash(spec), timeout=5.0, **extra)
    assert resp["ok"] is True, resp
    return resp["run_id"]


def wait_status(sock, run_id: str, want: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = request(sock, "runs_show", run_id=run_id, timeout=2.0)["run"]
        if run["status"] == want:
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach {want}")


def host_markers(*paths: Path) -> list[str]:
    """The host-path strings a served payload must never contain."""
    out = []
    for p in paths:
        out.append(str(p))
        out.append(str(p.resolve()))
        out.append(str(p.parent.resolve()))  # the tmp root itself
    return [m for m in out if m and len(m) > 1]


def assert_no_host_paths(payload, *paths: Path) -> None:
    blob = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for marker in host_markers(*paths):
        assert marker not in blob, f"host path leaked in payload: {marker!r} in {blob[:400]!r}"


def b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def bundle_of(job_path: Path) -> dict[str, str]:
    root = job_path.parent
    return {
        str(p.relative_to(root)): base64.b64encode(p.read_bytes()).decode("ascii")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- status / settings serve run-relative identifiers only --------------------


def test_status_serves_data_dir_relative_identifiers(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    resp = request(settings.socket_path, "status", timeout=2.0)
    assert resp["ok"] is True
    # keys unchanged (machine interface), values are run-relative identifiers
    assert resp["socket"] == "daemon.sock"
    assert resp["db_path"] == "turbochickens.db"
    assert resp["audit_log"] == "audit.jsonl"
    for v in (resp["socket"], resp["db_path"], resp["audit_log"]):
        assert not v.startswith("/"), v
    assert_no_host_paths(resp, settings.data_dir, tmp_path)


def test_settings_get_serves_relative_paths_over_socket_and_http(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    resp = request(settings.socket_path, "settings_get", timeout=2.0)
    s = resp["settings"]
    assert s["data_dir"] == "."
    assert s["socket_path"] == "daemon.sock"
    assert s["db_path"] == "turbochickens.db"
    assert s["audit_log_path"] == "audit.jsonl"
    assert s["results_dir"] == "results"
    assert s["quarantine_dir"] == "quarantine"
    assert s["uploads_dir"] == "uploads"
    for key in ("data_dir", "socket_path", "db_path", "audit_log_path", "results_dir", "quarantine_dir", "seats_dir", "uploads_dir"):
        assert not str(s[key]).startswith("/"), (key, s[key])
    assert_no_host_paths(resp, settings.data_dir, tmp_path)
    # HTTP mirror parity + the Settings view still renders from these rows
    c = http_client(settings)
    r = c.get("/api/settings")
    assert r.status_code == 200 and r.json() == resp
    assert_no_host_paths(r.text, settings.data_dir, tmp_path)


# --- the grep-guard over the served surfaces ----------------------------------


def test_grep_guard_no_host_paths_in_socket_payloads(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path

    # one succeeded chicken, one quarantined chicken, one stopped mid-flight
    good = enqueue(sock, write_job(tmp_path, name="guard-good"))
    bad = enqueue(sock, write_strict_job(tmp_path, name="guard-bad"))  # fails schema
    live = enqueue(sock, write_job(tmp_path, name="guard-live", prompt="SLEEP 20 live", timeout=60))
    run_good = wait_terminal(sock, good)
    run_bad = wait_terminal(sock, bad)
    wait_status(sock, live, "running")

    payloads: list[object] = [
        request(sock, "status", timeout=2.0),
        request(sock, "runs_list", timeout=2.0),
        request(sock, "runs_show", run_id=good, timeout=2.0),
        request(sock, "runs_show", run_id=bad, timeout=2.0),
        request(sock, "runs_show", run_id=live, timeout=2.0),
        request(sock, "results_get", run_id=good, timeout=2.0),
        request(sock, "results_get", run_id=bad, timeout=2.0),
        request(sock, "runs_logs", run_id=good, timeout=2.0),
        request(sock, "runs_logs", run_id=bad, timeout=2.0),
        request(sock, "audit_tail", limit=1000, timeout=2.0),
        request(sock, "settings_get", timeout=2.0),
        # the Beak: tree, file peek, and every refusal shape
        request(sock, "workspace_peek", run_id=live, timeout=2.0),
        request(sock, "workspace_peek", run_id=live, path="prompt.txt", timeout=2.0),
        request(sock, "workspace_peek", run_id=live, path="../escape", timeout=2.0),
        request(sock, "workspace_peek", run_id=live, path="/etc/passwd", timeout=2.0),
        request(sock, "workspace_peek", run_id=live, path=".vibe/config.toml", timeout=2.0),
        request(sock, "workspace_peek", run_id=live, path="missing.txt", timeout=2.0),
        request(sock, "workspace_peek", run_id="run-nope", timeout=2.0),
    ]
    # stop the live chicken → closed-beak payload; then a retention sweep
    request(sock, "run_stop", run_id=live, timeout=30.0)
    wait_status(sock, live, "stopped")
    payloads += [
        request(sock, "workspace_peek", run_id=live, timeout=2.0),
        request(sock, "runs_show", run_id=live, timeout=2.0),
    ]
    backdate_run(settings, good, months_ago_iso(8))
    payloads.append(request(sock, "retention_sweep", timeout=5.0))
    payloads.append(request(sock, "results_get", run_id=good, timeout=2.0))  # expired now
    payloads.append(request(sock, "audit_tail", limit=1000, timeout=2.0))

    for p in payloads:
        assert_no_host_paths(p, settings.data_dir, tmp_path)

    # positive: the run-relative identifiers are actually there
    assert run_good["manifest"]["workspace"] == f"workspaces/{good}"
    assert run_bad["result"]["valid"] is False and run_bad["result"]["schema_errors"]
    tree = request(sock, "workspace_peek", run_id=live, timeout=2.0)
    assert tree["ok"] is False and tree["workspace_gone"] is True


def test_grep_guard_no_host_paths_in_http_payloads(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    run_id = enqueue(sock, write_job(tmp_path, name="http-guard"))
    wait_terminal(sock, run_id)
    live = enqueue(sock, write_job(tmp_path, name="http-live", prompt="SLEEP 20 live", timeout=60))
    wait_status(sock, live, "running")

    c = http_client(settings)
    responses = [
        c.get("/"),
        c.get("/app.js"),
        c.get("/styles.css"),
        c.get("/api/status"),
        c.get("/api/settings"),
        c.get("/api/runs"),
        c.get(f"/api/runs/{run_id}"),
        c.get(f"/api/runs/{run_id}/logs"),
        c.get(f"/api/results/{run_id}"),
        c.get("/api/audit?limit=1000"),
        c.get(f"/api/runs/{live}/workspace"),
        c.get(f"/api/runs/{live}/workspace?path=..%2F..%2Fetc"),
        c.post("/api/retention/sweep", json={}),
    ]
    for r in responses:
        assert r.status_code == 200, (r.status_code, r.text[:200])
        assert_no_host_paths(r.text, settings.data_dir, tmp_path)
    # SPA shell renders (the views poll these endpoints and render the rows)
    html = c.get("/").text
    assert "settings-list" in html and "runs-tbody" in html

    request(sock, "run_stop", run_id=live, timeout=30.0)
    wait_status(sock, live, "stopped")
    r = c.get(f"/api/runs/{live}/workspace")
    assert r.status_code == 200 and r.json()["workspace_gone"] is True
    assert_no_host_paths(r.text, settings.data_dir, tmp_path)


# --- results / runs_show drop artifacts_dir (negative) but work (positive) ----


def test_results_get_and_runs_show_drop_artifacts_dir(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    good = enqueue(sock, write_job(tmp_path, name="drop-good"))
    bad = enqueue(sock, write_strict_job(tmp_path, name="drop-bad"))
    wait_terminal(sock, good)
    run_bad = wait_terminal(sock, bad)

    resp = request(sock, "results_get", run_id=good, timeout=2.0)
    assert resp["ok"] is True and resp["valid"] is True
    assert "artifacts_dir" not in resp  # negative: never served
    assert resp["output"]["message"] == "hello from fake vibe"  # positive: clutch served
    # the on-disk location still exists (daemon-internal, DB column unchanged)
    assert (settings.results_dir / good / "output.json").exists()

    resp = request(sock, "results_get", run_id=bad, timeout=2.0)
    assert resp["ok"] is False and resp["quarantined"] is True
    assert "artifacts_dir" not in resp
    assert resp["schema_errors"] and resp["raw_output"]
    assert (settings.quarantine_dir / bad / "raw_output.txt").exists()

    show = request(sock, "runs_show", run_id=good, timeout=2.0)["run"]
    assert show["result"]["valid"] is True and "artifacts_dir" not in show["result"]
    show_bad = run_bad
    assert show_bad["result"]["valid"] is False and "artifacts_dir" not in show_bad["result"]
    assert show_bad["result"]["schema_errors"]


# --- manifest + Headcount carry run-relative identifiers -----------------------


def test_manifest_workspace_and_chaining_are_relative(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    producer = enqueue(sock, write_job(tmp_path, name="chain-prod"))
    run_a = wait_terminal(sock, producer)
    assert run_a["status"] == "succeeded"

    consumer_spec = write_chain_job(
        tmp_path, "chain-cons", prompt="p", attachments=[f"nestbox://{producer}/output.json"]
    )
    consumer = enqueue(sock, consumer_spec)
    run_b = wait_terminal(sock, consumer)
    assert run_b["status"] == "succeeded"

    m = run_b["manifest"]
    assert m["workspace"] == f"workspaces/{consumer}"
    assert_no_host_paths(m, settings.data_dir, tmp_path)
    assert m["chaining"] == [
        {
            "attachment": f"nestbox://{producer}/output.json",
            "run_id": producer,
            "artifact_path": f"results/{producer}/output.json",
            "size_bytes": m["chaining"][0]["size_bytes"],
        }
    ]
    assert_no_host_paths(m["chaining"], settings.data_dir, tmp_path)

    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == consumer]
    consumed = [r for r in rows if r["event"] == "clutch.consumed"]
    assert len(consumed) == 1
    assert consumed[0]["artifact"] == f"results/{producer}/output.json"
    assert_no_host_paths(rows, settings.data_dir, tmp_path)
    # clutch.stored carries no artifacts_dir either
    stored = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "clutch.stored"]
    assert stored and all("artifacts_dir" not in r for r in stored)


def test_headcount_egg_candled_spec_path_is_relative(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    spec_path = write_job(tmp_path, name="candled-job")
    resp = request(sock, "jobs_upload", files=bundle_of(spec_path), timeout=10.0)
    assert resp["ok"] is True and resp["valid"] is True
    # positive: the returned spec_path is still the machine value enqueue accepts
    enq = request(sock, "enqueue", spec_path=resp["spec_path"], spec_hash=resp["spec_hash"], timeout=5.0)
    assert enq["ok"] is True
    wait_terminal(sock, enq["run_id"])

    uploaded = [e for e in AuditLog(settings.audit_log_path).read_all() if e["event"] == "egg.candled"]
    assert len(uploaded) == 1
    assert uploaded[0]["spec_path"].startswith("uploads/")  # data-dir-relative, not absolute
    assert not uploaded[0]["spec_path"].startswith("/")
    assert_no_host_paths(uploaded, settings.data_dir, tmp_path)
    tail = request(sock, "audit_tail", limit=1000, timeout=2.0)
    assert_no_host_paths(tail, settings.data_dir, tmp_path)
    # coopd.started row too
    started = [e for e in AuditLog(settings.audit_log_path).read_all() if e["event"] == "coopd.started"]
    assert started and started[0]["socket"] == "daemon.sock"


def test_retention_sweep_payload_and_audit_are_clean(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    run_id = enqueue(sock, write_job(tmp_path, name="sweep-me"))
    wait_terminal(sock, run_id)
    backdate_run(settings, run_id, months_ago_iso(9))

    resp = request(sock, "retention_sweep", timeout=5.0)
    assert resp["ok"] is True and resp["deleted"] == 1
    assert resp["expired"][0]["run_id"] == run_id
    assert "artifacts_dir" not in resp["expired"][0]  # negative: dropped
    assert_no_host_paths(resp, settings.data_dir, tmp_path)
    # positive: the artifacts really were composted, the index row stays (schema v5)
    assert not (settings.results_dir / run_id).exists()
    conn = sqlite3.connect(str(settings.db_path))
    try:
        row = conn.execute("SELECT valid, artifacts_dir FROM results WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[1] == str(settings.results_dir / run_id)
    composted = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "clutch.composted"]
    assert len(composted) == 1 and "artifacts_dir" not in composted[0]
    assert_no_host_paths(composted, settings.data_dir, tmp_path)


# --- loader / validator / beak / client errors report spec-relative names ------


def test_loader_errors_do_not_echo_host_paths(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    # negative (socket seam): unreadable spec path is an existence oracle only —
    # the error must not echo the host path back
    resp = request(sock, "enqueue", spec_path=str(tmp_path / "nope" / "job.toml"), timeout=5.0)
    assert resp["ok"] is False
    assert "cannot read spec file" in resp["error"] and "job.toml" in resp["error"]
    assert_no_host_paths(resp, settings.data_dir, tmp_path)

    # negative (pure seam): missing schema ref reports the spec-relative name
    spec_path = write_job(tmp_path, name="no-schema")
    (spec_path.parent / "schemas" / "out.schema.json").unlink()
    with pytest.raises(SpecValidationError) as ei:
        load_spec(spec_path)
    msg = "; ".join(str(i) for i in ei.value.issues)
    assert "schema file not found: 'schemas/out.schema.json'" in msg
    assert "resolved to" not in msg
    assert_no_host_paths(msg, settings.data_dir, tmp_path)

    # positive: a well-formed egg still candles
    assert load_spec(write_job(tmp_path, name="fine-job")).job.name == "fine-job"


def test_validate_schema_errors_do_not_echo_host_paths(tmp_path) -> None:
    # negative: missing schema file → the error names the schema, not the host path
    outcome = validate_output('{"a": 1}', tmp_path / "schemas" / "missing.json", schema_name="schemas/missing.json")
    assert outcome.valid is False
    assert outcome.schema_errors == ["<schema>: cannot load schema 'schemas/missing.json': No such file or directory"]
    assert_no_host_paths(outcome.schema_errors, tmp_path)
    # default label is the file name
    outcome = validate_output('{"a": 1}', tmp_path / "nope.json")
    assert "cannot load schema 'nope.json'" in outcome.schema_errors[0]
    assert_no_host_paths(outcome.schema_errors, tmp_path)
    # positive: a real schema still validates
    schema = tmp_path / "ok.json"
    schema.write_text('{"type": "object", "required": ["a"]}')
    assert validate_output('{"a": 1}', schema).valid is True


def test_beak_errors_do_not_echo_host_paths(tmp_path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello beak\n")
    # a unix socket file makes open() raise OSError deterministically (the
    # unreadable branch) without depending on uid/permissions
    s = socket_mod.socket(socket_mod.AF_UNIX)
    s.bind(str(ws / "sock.file"))
    try:
        # positive: a real file peeks fine
        ok = beak.peek_file(ws, "notes.txt")
        assert "error" not in ok and ok["content"] == "hello beak\n"
        # negative: the OSError branch reports the workspace-relative name only
        err = beak.peek_file(ws, "sock.file")
        assert err.get("refused") == "unreadable"
        assert "cannot read 'sock.file'" in err["error"]
        assert_no_host_paths(err, ws, tmp_path)
        # negative: traversal / absolute / private refusals are path-free too
        for bad in ("../escape", "/etc/passwd", ".vibe/config.toml", "a/../../b"):
            e = beak.peek_file(ws, bad)
            assert "error" in e
            assert_no_host_paths(e, ws, tmp_path)
    finally:
        s.close()


def test_client_daemon_unreachable_message_is_path_free(tmp_path) -> None:
    dead = tmp_path / "dead.sock"
    with pytest.raises(DaemonUnreachable) as ei:
        request(dead, "ping", timeout=1.0)
    msg = str(ei.value)
    assert "no coopd" in msg  # the CLI contract line is unchanged
    assert_no_host_paths(msg, tmp_path)  # negative: the socket host path is not echoed


def test_dead_socket_http_payload_is_path_free(tmp_path, monkeypatch) -> None:
    f = write_settings(tmp_path, coop_enabled=True, coop_port=free_port())
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    server = CoopServer(settings, transport=SocketTransport(tmp_path / "dead.sock"))

    async def go() -> tuple[httpx.Response, httpx.Response]:
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.coop_port}", timeout=5.0) as c:
                st = await c.get("/api/status")
                post = await c.post("/api/runs", json={"spec_path": "x"})
                return st, post
        finally:
            await server.stop()

    st, post = asyncio.run(go())
    for r in (st, post):
        assert r.status_code == 200
        assert r.json()["ok"] is False and r.json()["daemon"]["up"] is False
        assert_no_host_paths(r.text, tmp_path, settings.data_dir)  # negative: no socket path served


# --- provisioning failure: served reason/events redacted, full detail in coopd log


def test_provisioning_oserror_reason_is_redacted(coop_daemon, tmp_path, caplog) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    spec_path = write_job(tmp_path, name="broken-egg", prompt="hi")
    (spec_path.parent / "prompts" / "hello.txt").unlink()  # prompt ref now dangles
    run_id = enqueue(sock, spec_path)
    run = wait_terminal(sock, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert "FileNotFoundError" in reason
    # negative: neither the egg dir nor the data dir leaks into served text
    assert_no_host_paths(run["manifest"], settings.data_dir, tmp_path)
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any(e["source"] == "supervisor" and e["level"] == "error" for e in logs)
    assert_no_host_paths(logs, settings.data_dir, tmp_path)
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    assert_no_host_paths(rows, settings.data_dir, tmp_path)
    # positive: the full unredacted detail stays in coopd's own log
    assert any(
        "broken-egg" in r.getMessage() and str(tmp_path) in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
    # positive: still a proper fail-closed terminal run with attestation
    att = run["attestation"]
    assert att and att["process_reaped"] and att["workspace_removed"] and att["env_cleared"]


# --- operator CLI output unchanged where local-only ----------------------------


def test_cli_local_output_still_prints_operators_own_paths(coop_daemon, tmp_path) -> None:
    settings = coop_daemon.settings
    sock = settings.socket_path
    good = enqueue(sock, write_job(tmp_path, name="cli-good"))
    bad = enqueue(sock, write_strict_job(tmp_path, name="cli-bad"))
    wait_terminal(sock, good)
    wait_terminal(sock, bad)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, combined_output(result)
    # local-only lines unchanged: the operator's own absolute paths, from local settings
    assert f"socket: {settings.socket_path}" in result.output
    assert f"db: {settings.db_path}" in result.output
    assert f"audit_log: {settings.audit_log_path}" in result.output

    result = runner.invoke(app, ["runs", "show", good])
    assert result.exit_code == 0, combined_output(result)
    assert f"nest box: {settings.results_dir / good}" in result.output
    # the served manifest inside shows the run-relative identifier
    assert f'"workspace": "workspaces/{good}"' in result.output

    result = runner.invoke(app, ["results", "get", bad])
    assert result.exit_code == 1
    out = "".join(combined_output(result).split())  # rich wraps the long path mid-word
    assert f"quarantinedclutchkeptat:{settings.quarantine_dir / bad}".replace(" ", "") in out

    result = runner.invoke(app, ["results", "get", good])
    assert result.exit_code == 0 and json.loads(result.stdout)["message"] == "hello from fake vibe"


# --- shipped example spec hash unchanged ---------------------------------------


def test_example_spec_hash_unchanged() -> None:
    root = Path(__file__).resolve().parent.parent
    spec = load_spec(root / "examples" / "weekly-incidents-digest" / "job.toml")
    h = spec_hash(spec)
    assert h.startswith("18efbd8c") and h.endswith("80b959")
