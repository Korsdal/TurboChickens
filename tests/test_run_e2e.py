"""Tests for spec 03: manual run end-to-end via the `vibe-cli` adapter.

Seam: the real daemon on a temp socket (in a thread for most tests, as a real
`python -m turbochickens daemon start` subprocess for the e2e test), driven
through the `tc` CLI and the JSON-lines socket. The Vibe CLI itself is replaced
by a hermetic fake binary ($TURBOCHICKENS_VIBE_BIN) that emulates headless mode
(`--version`, `--prompt-file`, `--output-dir`) so tests need no credentials or
network — the adapter genuinely spawns a subprocess pinned to the workspace.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash
from turbochickens.util import utcnow_iso

runner = CliRunner()

# A stand-in for the real `vibe` binary. Honors --version, reads the prompt via
# --prompt-file, writes an export.json with usage into --output-dir, streams a
# log line containing control characters to stderr, prints a JSON document to
# stdout (the raw output), and supports "SLEEP <s>" / "EXIT <n>" prompt markers.
FAKE_VIBE = '''#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
m = re.search(r"SLEEP ([\\d.]+)", prompt)
if m:
    time.sleep(float(m.group(1)))
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, f)
print("fake harness log \\x07 bell \\x1b[31m red", file=sys.stderr)
print(json.dumps({"message": "hello from fake vibe", "echo": prompt.strip()[:40]}))
m = re.search(r"EXIT (\\d+)", prompt)
sys.exit(int(m.group(1)) if m else 0)
'''


def combined_output(result) -> str:
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "".join(parts)


def write_job(tmp_path: Path, name: str = "hello-vibe", prompt: str = "Say hello",
              timeout: int = 30, adapter: str = "vibe-cli") -> Path:
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
adapter = "{adapter}"
model = "fake-model"
temperature = 0.0

[resources]
timeout_sec = {timeout}
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


def upload_egg(sock, spec: Path, timeout: float = 15.0) -> str:
    """Spec 32 (ticket 02): register an egg directory in the coop uploads
    area via `jobs_upload` and return the stored spec_path — the only kind of
    `spec_path` a chicken-owned connection is allowed to queue."""
    files = {
        str(p.relative_to(spec.parent)): base64.b64encode(p.read_bytes()).decode()
        for p in sorted(spec.parent.rglob("*"))
        if p.is_file()
    }
    resp = request(sock, "jobs_upload", files=files, timeout=timeout)
    assert resp.get("ok") is True, resp
    return resp["spec_path"]


def wait_terminal(socket_path, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in ("succeeded", "failed"):
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def db_row(settings, sql: str, params=()) -> tuple | None:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# --- the happy path: enqueue → workspace → headless run → succeeded ---------


def test_run_reaches_succeeded_with_manifest_attestation_audit(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SECRET", "s3cr3t-value")  # ambient secret must not leak into the run env
    spec_path = write_job(tmp_path)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    assert "succeeded" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    sock = settings.socket_path

    # run row + manifest
    show = request(sock, "runs_show", run_id=run_id, timeout=2.0)
    assert show["ok"] is True
    run = show["run"]
    assert run["status"] == "succeeded"
    assert run["trigger"] == "manual"
    manifest = run["manifest"]
    assert manifest["spec_hash"] == f"sha256:{spec_hash(load_spec(spec_path))}"
    assert manifest["harness"]["adapter"] == "vibe-cli"
    assert manifest["harness"]["version"] == "9.9.9-fake"
    assert manifest["harness"]["provider"] == "mistral"
    assert manifest["harness"]["model"] == "fake-model"
    assert manifest["harness"]["temperature"] == 0.0
    assert manifest["usage"]["tokens_in"] == 12
    assert manifest["usage"]["tokens_out"] == 34
    assert "TURBOCHICKENS_RUN_ID" in manifest["env_keys"]
    assert "SUPER_SECRET" not in manifest["env_keys"]
    assert "s3cr3t-value" not in json.dumps(manifest)
    assert manifest["timed_out"] is False

    # streamed logs landed in run_events, sanitized of control characters
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert logs["ok"] is True
    events = logs["events"]
    assert any(e["source"] == "harness" and "fake harness log" in e["line"] for e in events)
    assert all("\x07" not in e["line"] and "\x1b" not in e["line"] for e in events)
    transitions = [e["line"] for e in events if e["source"] == "supervisor"]
    for state in ("provisioning", "running", "collecting", "finalizing", "succeeded"):
        assert any(state in t for t in transitions), f"missing transition {state}: {transitions}"
    assert any(e["level"] == "output" and "hello from fake vibe" in e["line"] for e in events)

    # teardown attestation + audit rows
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    events_audit = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert "egg.queued" in events_audit
    assert "chicken.finished" in events_audit
    assert "abattoir.attested" in events_audit
    finished = [r for r in AuditLog(settings.audit_log_path).read_all() if r["event"] == "chicken.finished"][0]
    assert finished["status"] == "succeeded" and finished["run_id"] == run_id
    # workspace verified gone
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    # no alert condition
    assert run_id not in request(sock, "status", timeout=2.0)["alerts"]


def test_tc_runs_list_show_logs_display_the_run(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)

    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0, combined_output(result)
    assert "hello-vibe" in result.output  # table truncates the id column at 80 cols
    assert "succeeded" in result.output
    assert "yes" in result.output  # attested

    result = runner.invoke(app, ["runs", "show", run_id])
    assert result.exit_code == 0, combined_output(result)
    assert "sha256:" in result.output
    assert "9.9.9-fake" in result.output
    assert "attestation:" in result.output

    result = runner.invoke(app, ["runs", "logs", run_id])
    assert result.exit_code == 0, combined_output(result)
    assert "fake harness log" in result.output
    assert "status" in result.output


def test_runs_show_unknown_run_exits_1(daemon) -> None:
    result = runner.invoke(app, ["runs", "show", "run-nope"])
    assert result.exit_code == 1
    assert "unknown chicken" in combined_output(result)


# --- admission control: over-budget backlogs, never rejects ------------------


def test_over_budget_run_backlogs_in_queue_not_rejected(tmp_path, monkeypatch, fake_vibe) -> None:
    f = tmp_path / "settings-b1.toml"
    f.write_text(f'data_dir = "{tmp_path / "data-b1"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    try:
        slow = write_job(tmp_path, name="slow-job", prompt="SLEEP 3 do the thing")
        r1 = runner.invoke(app, ["run", str(slow)])
        r2 = runner.invoke(app, ["run", str(slow)])
        assert r1.exit_code == 0 and r2.exit_code == 0, combined_output(r1) + combined_output(r2)
        assert "weight 1.0" in r1.output and "weight 1.0" in r2.output  # never rejected
        id1, id2 = run_id_of(r1), run_id_of(r2)

        # while run 1 is in flight (weight 1.0 = whole budget of 1), run 2 backlogs
        seen_backlog = False
        deadline = time.time() + 5
        while time.time() < deadline:
            st = request(settings.socket_path, "status", timeout=2.0)
            if st["queue_depth"] >= 1 and len(st["in_flight"]) == 1:
                seen_backlog = True
                break
            time.sleep(0.05)
        assert seen_backlog, "over-budget run was not visible in the queue"
        assert id1 in [r["run_id"] for r in st["in_flight"]]

        # both eventually run and succeed — backlog, not rejection
        run1 = wait_terminal(settings.socket_path, id1, timeout=30)
        run2 = wait_terminal(settings.socket_path, id2, timeout=30)
        assert run1["status"] == "succeeded" and run2["status"] == "succeeded"
    finally:
        dt.stop()


# --- failure paths still get teardown attested --------------------------------


def test_harness_error_marks_failed_but_still_attested(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="failing-job", prompt="EXIT 3 please")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    assert "rc=3" in run["manifest"]["status_reason"]
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert "chicken.finished" in events and "abattoir.attested" in events
    assert not (settings.data_dir / "workspaces" / run_id).exists()


def test_timeout_hard_kills_run_and_still_attests(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="slow-job", prompt="SLEEP 60 hang forever", timeout=1)
    t0 = time.time()
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    elapsed = time.time() - t0
    assert result.exit_code == 1
    assert "failed" in result.output
    assert elapsed < 30, f"run was not hard-killed (took {elapsed:.1f}s)"
    run_id = run_id_of(result)
    run = wait_terminal(daemon.settings.socket_path, run_id)
    assert run["status"] == "failed"
    assert run["manifest"]["timed_out"] is True
    assert "timeout" in run["manifest"]["status_reason"]
    att = db_row(daemon.settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)


# --- alert condition: terminal run without attestation ------------------------


def test_terminal_run_without_attestation_is_an_alert(daemon) -> None:
    settings = daemon.settings
    conn = sqlite3.connect(str(settings.db_path))
    conn.execute(
        "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at) VALUES ('ghost-job', 'ghost', 'h', 't', ?)",
        (utcnow_iso(),),
    )
    conn.execute(
        "INSERT INTO runs (id, job_id, status, trigger, started_at, finished_at) VALUES ('run-ghost', 'ghost-job', 'succeeded', 'manual', ?, ?)",
        (utcnow_iso(), utcnow_iso()),
    )
    conn.commit()
    conn.close()
    st = request(settings.socket_path, "status", timeout=2.0)
    assert "run-ghost" in st["alerts"]
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0
    assert "ALERT" in result.output
    result = runner.invoke(app, ["runs", "show", "run-ghost"])
    assert result.exit_code == 0
    assert "ALERT" in combined_output(result)


# --- enqueue validation --------------------------------------------------------


def test_tc_run_invalid_spec_exits_1_and_enqueues_nothing(daemon, tmp_path) -> None:
    bad = write_job(tmp_path, name="bad-job")
    bad.write_text(bad.read_text().replace('temperature = 0.0', 'temperature = 0.0\nmax_tokens = 10'))
    result = runner.invoke(app, ["run", str(bad)])
    assert result.exit_code == 1
    assert "harness.max_tokens" in combined_output(result)
    st = request(daemon.settings.socket_path, "status", timeout=2.0)
    assert st["queue_depth"] == 0


def test_tc_run_unresolvable_model_rejected_at_enqueue(daemon, tmp_path) -> None:
    # mistral-api is implemented since spec 05; a model missing from
    # providers.toml still fails fast at enqueue, before anything is queued
    spec_path = write_job(tmp_path, name="api-job", adapter="mistral-api")
    result = runner.invoke(app, ["run", str(spec_path)])
    assert result.exit_code == 1
    assert "not found in providers.toml" in combined_output(result)
    st = request(daemon.settings.socket_path, "status", timeout=2.0)
    assert st["queue_depth"] == 0


def test_enqueue_op_validates_spec_and_hash_over_socket(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path)
    (spec_path.parent / "schemas" / "out.schema.json").unlink()  # broken file ref
    resp = request(daemon.settings.socket_path, "enqueue", timeout=5.0, spec_path=str(spec_path))
    assert resp["ok"] is False and "egg failed candling" in resp["error"]
    (spec_path.parent / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    resp = request(daemon.settings.socket_path, "enqueue", timeout=5.0, spec_path=str(spec_path), spec_hash="deadbeef")
    assert resp["ok"] is False and "hash mismatch" in resp["error"]
    resp = request(daemon.settings.socket_path, "enqueue", timeout=5.0, spec_path=str(spec_path), spec_hash=spec_hash(load_spec(spec_path)))
    assert resp["ok"] is True and resp["status"] == "queued" and resp["weight"] == 1.0
    wait_terminal(daemon.settings.socket_path, resp["run_id"], timeout=30)


def test_tc_run_when_daemon_down_exits_1(settings_file, tmp_path) -> None:
    spec_path = write_job(tmp_path)
    result = runner.invoke(app, ["run", str(spec_path)])
    assert result.exit_code == 1
    assert "no coopd" in combined_output(result)


# --- end-to-end: real daemon subprocess + real CLI + fake vibe binary ---------


def test_e2e_subprocess_daemon_tc_run_vibe_cli(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    spec_path = write_job(tmp_path, name="e2e-job")
    env = dict(os.environ)  # includes TURBOCHICKENS_SETTINGS and TURBOCHICKENS_VIBE_BIN
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "daemon", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if settings.socket_path.exists():
                try:
                    if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                        break
                except DaemonUnreachable:
                    pass
            if proc.poll() is not None:
                raise AssertionError(f"daemon exited early: {proc.stdout.read() if proc.stdout else ''}")
            time.sleep(0.05)
        else:
            raise AssertionError("daemon subprocess did not start answering")

        result = runner.invoke(app, ["run", str(spec_path), "--wait"])
        assert result.exit_code == 0, combined_output(result)
        assert "succeeded" in result.output
        run_id = run_id_of(result)

        result = runner.invoke(app, ["runs", "list"])
        assert result.exit_code == 0 and "e2e-job" in result.output and "succeeded" in result.output
        result = runner.invoke(app, ["runs", "logs", run_id])
        assert result.exit_code == 0 and "fake harness log" in result.output

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=15)
        assert proc.returncode == 0

        att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
        assert att == (1, 1, 1)
        events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
        for expected in ("coopd.started", "egg.queued", "abattoir.attested", "chicken.finished", "coopd.stopped"):
            assert expected in events
        assert not (settings.data_dir / "workspaces" / run_id).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
