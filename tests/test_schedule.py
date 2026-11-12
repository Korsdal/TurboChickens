"""Tests for spec 06: internal cron scheduler (APScheduler) + `tc schedule add/list/rm`.

Seams: the real daemon on a temp socket (in a thread, via DaemonThread), driven
through the `tc` CLI and the JSON-lines socket — external, observable behavior
only. The harness is the hermetic fake vibe binary ($TURBOCHICKENS_VIBE_BIN),
so the one true cron firing is exercised end-to-end without credentials.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from test_run_e2e import (
    FAKE_VIBE,
    combined_output,
    db_row,
    wait_terminal,
)
from croniter import croniter
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.scheduler import aps_trigger
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

runner = CliRunner()


# --- pure seam: the scheduler honors standard POSIX cron semantics ------------


@pytest.mark.parametrize(
    "expr",
    ["0 7 * * 1", "0 0 * * 0", "0 0 * * 7", "*/15 * * * *", "30 8 1 * *", "0 9 * * 1-5", "5 4 * * sun", "0 22 * * 1-5"],
)
def test_aps_trigger_matches_croniter_next_fire(expr: str) -> None:
    # APScheduler numbers day-of-week 0=Monday; specs use POSIX (1=Monday,
    # 0/7=Sunday) — e.g. the baseline example "0 7 * * 1" is Monday 07:00.
    now = datetime(2026, 11, 12, 14, 27, 30, tzinfo=datetime.now().astimezone().tzinfo)
    trig = aps_trigger(expr)
    aps_next = trig.get_next_fire_time(None, now)
    cron_next = croniter(expr, now).get_next(datetime)
    assert aps_next is not None
    assert aps_next.replace(tzinfo=None) == cron_next.replace(tzinfo=None), (
        f"{expr!r}: apscheduler says {aps_next}, POSIX cron says {cron_next}"
    )


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


def write_cron_job(tmp_path: Path, name: str = "cron-job", schedule: str = "0 7 * * 1",
                   prompt: str = "Say hello", trigger_type: str = "cron") -> Path:
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    schedule_line = f'schedule = "{schedule}"' if trigger_type == "cron" else ""
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test cron job"

[trigger]
type = "{trigger_type}"
{schedule_line}

[harness]
adapter = "vibe-cli"
model = "fake-model"
temperature = 0.0

[resources]
timeout_sec = 30
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


def next_minute_cron() -> str:
    """A 5-field cron expression firing at the next minute boundary (<=60s away)."""
    now = datetime.now().astimezone()
    fire = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return f"{fire.minute} {fire.hour} * * *"


# --- add / list / rm round trip ------------------------------------------------


def test_schedule_add_list_rm_round_trip(daemon, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path, schedule="0 7 * * 1")
    sock = daemon.settings.socket_path

    result = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert result.exit_code == 0, combined_output(result)
    assert "scheduled job 'cron-job'" in result.output
    assert "cron='0 7 * * 1'" in result.output
    assert "next_run=" in result.output and "next_run=-" not in result.output

    # visible over the socket, in the DB, in the audit log, and in status
    resp = request(sock, "schedule_list", timeout=2.0)
    assert resp["ok"] is True and len(resp["schedules"]) == 1
    s = resp["schedules"][0]
    assert s["job_id"] == "cron-job"
    assert s["schedule"] == "0 7 * * 1"
    assert s["spec_hash"] == spec_hash(load_spec(spec_path))
    assert s["next_run"] is not None
    row = db_row(daemon.settings, "SELECT schedule, spec_hash FROM schedules WHERE job_id = 'cron-job'")
    assert row is not None and row[0] == "0 7 * * 1"
    events = [r["event"] for r in AuditLog(daemon.settings.audit_log_path).read_all()]
    assert "schedule.added" in events
    st = request(sock, "status", timeout=2.0)
    assert st["schedules"] == 1

    # CLI list shows it
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0, combined_output(result)
    assert "cron-job" in result.output and "0 7 * * 1" in result.output and "next_run=" in result.output

    # rm removes it everywhere
    result = runner.invoke(app, ["schedule", "rm", "cron-job"])
    assert result.exit_code == 0, combined_output(result)
    assert "removed schedule for job 'cron-job'" in result.output
    resp = request(sock, "schedule_list", timeout=2.0)
    assert resp["ok"] is True and resp["schedules"] == []
    assert db_row(daemon.settings, "SELECT job_id FROM schedules") is None
    events = [r["event"] for r in AuditLog(daemon.settings.audit_log_path).read_all()]
    assert "schedule.removed" in events
    result = runner.invoke(app, ["schedule", "list"])
    assert result.exit_code == 0 and "no schedules" in result.output

    # rm again is an error, not a silent no-op
    result = runner.invoke(app, ["schedule", "rm", "cron-job"])
    assert result.exit_code == 1
    assert "no schedule registered" in combined_output(result)


def test_schedule_add_rejects_manual_trigger_job(daemon, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path, name="manual-job", trigger_type="manual")
    result = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert result.exit_code == 1
    assert "trigger.type='manual'" in combined_output(result)
    resp = request(daemon.settings.socket_path, "schedule_list", timeout=2.0)
    assert resp["schedules"] == []


def test_schedule_add_invalid_cron_is_a_field_named_error(daemon, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path, name="bad-cron", schedule="not a cron")
    result = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert result.exit_code == 1
    assert "trigger.schedule" in combined_output(result)
    resp = request(daemon.settings.socket_path, "schedule_list", timeout=2.0)
    assert resp["schedules"] == []


def test_schedule_add_over_socket_validates_spec_and_hash(daemon, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path)
    sock = daemon.settings.socket_path
    (spec_path.parent / "schemas" / "out.schema.json").unlink()  # broken file ref
    resp = request(sock, "schedule_add", timeout=5.0, spec_path=str(spec_path))
    assert resp["ok"] is False and "invalid job spec" in resp["error"]
    (spec_path.parent / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    resp = request(sock, "schedule_add", timeout=5.0, spec_path=str(spec_path), spec_hash="deadbeef")
    assert resp["ok"] is False and "hash mismatch" in resp["error"]
    resp = request(
        sock, "schedule_add", timeout=5.0, spec_path=str(spec_path),
        spec_hash=spec_hash(load_spec(spec_path)),
    )
    assert resp["ok"] is True and resp["schedule"] == "0 7 * * 1" and resp["next_run"] is not None
    # manual-trigger spec over the socket is refused too
    manual = write_cron_job(tmp_path, name="manual-2", trigger_type="manual")
    resp = request(sock, "schedule_add", timeout=5.0, spec_path=str(manual))
    assert resp["ok"] is False and "only cron-triggered jobs" in resp["error"]


def test_schedule_add_replaces_existing_schedule(daemon, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path, schedule="0 7 * * 1")
    r = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert r.exit_code == 0, combined_output(r)
    # operator edits the spec's cron expression and re-adds
    spec_path.write_text(spec_path.read_text().replace('schedule = "0 7 * * 1"', 'schedule = "30 8 * * 2"'))
    r = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert r.exit_code == 0, combined_output(r)
    assert "cron='30 8 * * 2'" in r.output
    resp = request(daemon.settings.socket_path, "schedule_list", timeout=2.0)
    assert len(resp["schedules"]) == 1
    assert resp["schedules"][0]["schedule"] == "30 8 * * 2"
    assert resp["schedules"][0]["spec_hash"] == spec_hash(load_spec(spec_path))


def test_schedules_persist_across_daemon_restart(settings_file, tmp_path, fake_vibe) -> None:
    spec_path = write_cron_job(tmp_path, schedule="15 3 * * *")
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    try:
        r = runner.invoke(app, ["schedule", "add", str(spec_path)])
        assert r.exit_code == 0, combined_output(r)
    finally:
        dt.stop()
    assert not settings.socket_path.exists()

    dt2 = DaemonThread(settings)  # fresh daemon process over the same data_dir
    dt2.start()
    try:
        resp = request(settings.socket_path, "schedule_list", timeout=2.0)
        assert resp["ok"] is True and len(resp["schedules"]) == 1
        assert resp["schedules"][0]["job_id"] == "cron-job"
        assert resp["schedules"][0]["schedule"] == "15 3 * * *"
        assert resp["schedules"][0]["next_run"] is not None  # re-registered with APScheduler
        result = runner.invoke(app, ["schedule", "list"])
        assert result.exit_code == 0 and "15 3 * * *" in result.output
    finally:
        dt2.stop()


def test_schedule_commands_when_daemon_down_exit_1(settings_file, tmp_path) -> None:
    spec_path = write_cron_job(tmp_path)
    for args in (["schedule", "add", str(spec_path)], ["schedule", "list"], ["schedule", "rm", "cron-job"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1, args
        assert "no daemon" in combined_output(result)


# --- the acceptance behavior: a firing cron enqueues a run with trigger=cron ---


def test_firing_cron_enqueues_run_through_the_same_pipeline(daemon, tmp_path) -> None:
    expr = next_minute_cron()
    spec_path = write_cron_job(tmp_path, name="firing-job", schedule=expr)
    sock = daemon.settings.socket_path
    settings = daemon.settings

    r = runner.invoke(app, ["schedule", "add", str(spec_path)])
    assert r.exit_code == 0, combined_output(r)
    assert f"cron='{expr}'" in r.output

    # wait for the cron to fire: a run with trigger=cron appears in runs_list
    deadline = time.time() + 90
    cron_run = None
    while time.time() < deadline:
        resp = request(sock, "runs_list", timeout=2.0)
        cron_runs = [x for x in resp["runs"] if x["trigger"] == "cron"]
        if cron_runs:
            cron_run = cron_runs[0]
            break
        time.sleep(0.5)
    assert cron_run is not None, "cron schedule did not fire within 90s"
    run_id = cron_run["id"]

    # it flows through the identical pipeline: full lifecycle → succeeded,
    # validated result stored, teardown attested, workspace gone
    run = wait_terminal(sock, run_id, timeout=30)
    assert run["status"] == "succeeded", run["manifest"]
    assert run["trigger"] == "cron"
    assert run["manifest"]["trigger"] == "cron"
    assert run["manifest"]["harness"]["adapter"] == "vibe-cli"
    assert run["attestation"] is not None
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    res = db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_id,))
    assert res == (1,)

    # audit trail: the fire and the queued run are both recorded with trigger=cron
    audit = AuditLog(settings.audit_log_path).read_all()
    fired = [r for r in audit if r["event"] == "schedule.fired"]
    assert fired and fired[0]["run_id"] == run_id and fired[0]["trigger"] == "cron"
    queued = [r for r in audit if r["event"] == "run.queued" and r.get("run_id") == run_id]
    assert queued and queued[0]["trigger"] == "cron"

    # and the CLI surfaces all of it: runs list shows trigger=cron, results get works
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0, combined_output(result)
    assert "trigger=cron" in result.output and "firing-job" in result.output
    result = runner.invoke(app, ["results", "get", run_id])
    assert result.exit_code == 0, combined_output(result)
    assert "hello from fake vibe" in result.output
    # manual runs remain distinguishable by trigger=manual
    manual_spec = write_cron_job(tmp_path, name="manual-side", trigger_type="manual")
    r = runner.invoke(app, ["run", str(manual_spec), "--wait"])
    assert r.exit_code == 0, combined_output(r)
    m = re.search(r"queued run (run-[0-9a-f]+)", r.output)
    assert m
    manual_run = wait_terminal(sock, m.group(1), timeout=30)
    assert manual_run["trigger"] == "manual"
