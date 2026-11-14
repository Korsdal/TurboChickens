"""Tests for spec 04: output validation + result store + quarantine + `tc results get`.

Seam: the real daemon on a temp socket (in a thread; one test also boots it as
a real `python -m turbochickens daemon start` subprocess), driven through the
`tc` CLI and the JSON-lines socket. The Vibe CLI is replaced by a hermetic fake
($TURBOCHICKENS_VIBE_BIN) whose raw output is the prompt file content verbatim,
so tests choose the exact raw output the validator will see — no credentials,
no network.
"""

from __future__ import annotations

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

runner = CliRunner()

# Fake vibe binary: honors --version, writes export.json usage into
# --output-dir, prints the prompt file content verbatim as the raw output,
# and exits with the code in an "EXIT <n>" prompt marker if present.
FAKE_VIBE_ECHO = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
with open(args[args.index("--prompt-file") + 1]) as f:
    prompt = f.read()
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 5, "output_tokens": 7}}, f)
print(prompt.strip())
m = re.search(r"EXIT (\d+)", prompt)
sys.exit(int(m.group(1)) if m else 0)
'''

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["message"],
    "additionalProperties": False,
    "properties": {
        "message": {"type": "string"},
        "count": {"type": "integer"},
    },
}


def combined_output(result) -> str:
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "".join(parts)


def write_job(tmp_path: Path, name: str = "res-job", prompt: str = '{"message": "hi"}',
              schema: dict | None = None, timeout: int = 30) -> Path:
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "out.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text(json.dumps(schema or SCHEMA))
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test job"

[trigger]
type = "manual"

[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0

[resources]
timeout_sec = {timeout}
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/out.txt"
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
    bin_path.write_text(FAKE_VIBE_ECHO)
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


# --- checkbox 1: valid output is stored and `tc results get` returns it ------


def test_valid_output_stored_and_results_get_returns_it(daemon, tmp_path) -> None:
    payload = {"message": "hello results", "count": 3}
    spec_path = write_job(tmp_path, name="good-job", prompt=json.dumps(payload))
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    assert "succeeded" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    sock = settings.socket_path

    # the run finalized as succeeded and the validator event was logged
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any(e["source"] == "validator" and "matches schema" in e["line"] for e in logs)

    # results row: valid=1, output stored, artifacts dir recorded
    row = db_row(
        settings,
        "SELECT valid, output_json, artifacts_dir, schema_errors FROM results WHERE run_id = ?",
        (run_id,),
    )
    assert row is not None
    assert row[0] == 1
    assert json.loads(row[1]) == payload
    assert row[3] is None
    artifacts = Path(row[2])
    assert artifacts == settings.results_dir / run_id
    assert json.loads((artifacts / "output.json").read_text()) == payload

    # socket op returns the validated output
    resp = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert resp["ok"] is True and resp["valid"] is True
    assert resp["output"] == payload
    assert resp["artifacts_dir"] == str(artifacts)

    # CLI prints the validated result as JSON
    result = runner.invoke(app, ["results", "get", run_id])
    assert result.exit_code == 0, combined_output(result)
    assert json.loads(result.stdout) == payload

    # runs show surfaces the valid result; audit logged clutch.stored
    result = runner.invoke(app, ["runs", "show", run_id])
    assert result.exit_code == 0 and "clutch: valid" in result.output
    events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert "clutch.stored" in events
    assert "chicken.finished" in events and "abattoir.attested" in events

    # teardown still attested, workspace gone
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()


# --- checkbox 2: invalid output → failed + quarantine with schema errors -----


def test_invalid_output_is_failed_quarantined_with_schema_errors(daemon, tmp_path) -> None:
    bad_payload = {"message": 42, "surprise": True}  # wrong type + additional property
    spec_path = write_job(tmp_path, name="bad-job", prompt=json.dumps(bad_payload))
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    sock = settings.socket_path

    run = wait_terminal(sock, run_id)
    assert run["status"] == "failed"
    assert "schema validation" in run["manifest"]["status_reason"]

    # results row exists but is flagged invalid with recorded schema errors
    row = db_row(
        settings,
        "SELECT valid, output_json, artifacts_dir, schema_errors FROM results WHERE run_id = ?",
        (run_id,),
    )
    assert row is not None
    assert row[0] == 0
    assert row[1] == json.dumps(bad_payload)  # raw output kept as evidence
    errors = json.loads(row[3])
    assert any(e.startswith("message:") for e in errors)
    assert any("surprise" in e for e in errors)

    # quarantine artifacts on disk: raw output + schema errors
    qdir = Path(row[2])
    assert qdir == settings.quarantine_dir / run_id
    assert (qdir / "raw_output.txt").read_text() == json.dumps(bad_payload)
    assert json.loads((qdir / "schema_errors.json").read_text()) == errors

    # socket op refuses to return it as a result
    resp = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert resp["ok"] is False
    assert resp["quarantined"] is True
    assert resp["schema_errors"] == errors

    # validator error event streamed into run logs
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any(e["source"] == "validator" and e["level"] == "error" and "message:" in e["line"] for e in logs)

    # runs show surfaces the quarantine + errors
    result = runner.invoke(app, ["runs", "show", run_id])
    assert result.exit_code == 0
    assert "QUARANTINED" in result.output
    assert "schema error:" in result.output

    # audit + teardown attestation still complete
    events = [r["event"] for r in AuditLog(settings.audit_log_path).read_all()]
    assert "clutch.quarantined" in events
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()


def test_non_json_output_is_quarantined(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="notjson-job", prompt="this is not json {")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    row = db_row(settings, "SELECT valid, schema_errors FROM results WHERE run_id = ?", (run_id,))
    assert row[0] == 0
    assert any("not valid JSON" in e for e in json.loads(row[1]))


# --- checkbox 3: quarantined output is never returned by `tc results get -----


def test_results_get_never_returns_quarantined_output(daemon, tmp_path) -> None:
    bad_payload = {"message": 42}
    spec_path = write_job(tmp_path, name="q-job", prompt=json.dumps(bad_payload))
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    run_id = run_id_of(result)

    result = runner.invoke(app, ["results", "get", run_id])
    assert result.exit_code == 1
    out = combined_output(result)
    assert "quarantined" in out
    assert "schema error:" in out  # errors shown for inspection
    # stdout must not carry the payload as a valid result
    with pytest.raises(ValueError):
        json.loads(result.stdout)


def test_results_get_unknown_run_and_run_without_result_exit_1(daemon, tmp_path) -> None:
    result = runner.invoke(app, ["results", "get", "run-nope"])
    assert result.exit_code == 1
    assert "unknown chicken" in combined_output(result)

    # a run that fails in the harness never reaches collecting → no result row
    spec_path = write_job(tmp_path, name="rc-job", prompt="EXIT 3 boom")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    run_id = run_id_of(result)
    row = db_row(daemon.settings, "SELECT COUNT(*) FROM results WHERE run_id = ?", (run_id,))
    assert row[0] == 0
    result = runner.invoke(app, ["results", "get", run_id])
    assert result.exit_code == 1
    assert "no clutch in the nest box" in combined_output(result)


# --- e2e: real daemon subprocess + real CLI + fake vibe, both outcomes --------


def test_e2e_subprocess_daemon_results_get_valid_and_quarantined(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    good = write_job(tmp_path, name="e2e-good", prompt='{"message": "e2e ok", "count": 1}')
    bad = write_job(tmp_path, name="e2e-bad", prompt='{"message": 7}')
    env = dict(os.environ)
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
                raise AssertionError("daemon exited early")
            time.sleep(0.05)
        else:
            raise AssertionError("daemon subprocess did not start answering")

        r_good = runner.invoke(app, ["run", str(good), "--wait"])
        assert r_good.exit_code == 0, combined_output(r_good)
        good_id = run_id_of(r_good)
        r_bad = runner.invoke(app, ["run", str(bad), "--wait"])
        assert r_bad.exit_code == 1
        bad_id = run_id_of(r_bad)

        r = runner.invoke(app, ["results", "get", good_id])
        assert r.exit_code == 0 and json.loads(r.stdout) == {"message": "e2e ok", "count": 1}
        r = runner.invoke(app, ["results", "get", bad_id])
        assert r.exit_code == 1 and "quarantined" in combined_output(r)

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=15)
        assert proc.returncode == 0

        # result store contents on disk after daemon shutdown
        row = db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (good_id,))
        assert row == (1,)
        row = db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (bad_id,))
        assert row == (0,)
        assert (settings.results_dir / good_id / "output.json").is_file()
        assert (settings.quarantine_dir / bad_id / "raw_output.txt").is_file()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
