"""Tests for spec 22: lifecycle ops — `run_stop` / `run_rerun` over the socket,
the CLI, and the Headcount (audit).

Seams: the real daemon on a temp socket (in a thread for most tests, as a real
`python -m turbochickens daemon start` subprocess for the e2e test), driven
through the `tc` CLI and the JSON-lines socket, with the fake vibe binary via
$TURBOCHICKENS_VIBE_BIN — one variant traps SIGTERM so the stop path has to
escalate to the hard kill. Assertions are external, observable behavior only:
socket/CLI responses, store + audit contents, status transitions, attestations.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import turbochickens.runner as runner_mod
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, db_row, run_id_of, write_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings

runner = CliRunner()

# A fake vibe that traps SIGTERM (streams a marker line, then keeps sleeping), so
# stopping it has to escalate from SIGTERM to the hard kill after the grace.
FAKE_VIBE_STUBBORN = '''#!/usr/bin/env python3
import json, os, re, signal, sys, time

def on_term(signum, frame):
    print("harness got SIGTERM and ignored it", file=sys.stderr, flush=True)

signal.signal(signal.SIGTERM, on_term)
print("stubborn harness ready", file=sys.stderr, flush=True)
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
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps({"message": "hello from stubborn fake vibe"}))
'''


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    # budget 1: one vibe-cli chicken (weight 1.0) fills the whole budget, so a
    # second enqueued egg backlogs in the pecking order (queued) — exactly the
    # cancel-a-queued-egg scenario
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
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


def wait_status(socket_path, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


def wait_terminal(socket_path, run_id: str, timeout: float = 30.0) -> dict:
    return wait_status(socket_path, run_id, TERMINAL_STATES, timeout)


def rerun_id_of(result) -> str:
    m = re.search(r"egg re-queued (run-[0-9a-f]+)", result.output)
    assert m, f"no re-queued run id in output: {result.output}"
    return m.group(1)


ATT_SQL = (
    "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?"
)


# --- run_stop on a queued egg: cancelled, never hatches, still attested -------


def test_run_stop_on_queued_egg_cancels_it_without_hatching(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    slow = write_job(tmp_path, name="slow-egg", prompt="SLEEP 8 working")
    r1 = runner.invoke(app, ["run", str(slow)])
    r2 = runner.invoke(app, ["run", str(slow)])
    assert r1.exit_code == 0 and r2.exit_code == 0, combined_output(r1) + combined_output(r2)
    id1, id2 = run_id_of(r1), run_id_of(r2)
    # id1 fills the budget of 1 → id2 stays queued in the pecking order
    wait_status(sock, id1, ("provisioning", "running", "collecting", "finalizing"))

    resp = request(sock, "run_stop", run_id=id2, timeout=15.0)
    assert resp["ok"] is True and resp["op"] == "run_stop"
    assert resp["run_id"] == id2 and resp["status"] == "cancelled"
    assert "never hatched" in resp["reason"]

    show = request(sock, "runs_show", run_id=id2, timeout=2.0)["run"]
    assert show["status"] == "cancelled"
    assert show["manifest"]["status"] == "cancelled"
    assert "never hatched" in show["manifest"]["status_reason"]
    att = show["attestation"]
    assert att and att["process_reaped"] and att["workspace_removed"] and att["env_cleared"]

    # never hatched: no harness events, no egg.hatched row, no clutch
    logs = request(sock, "runs_logs", run_id=id2, timeout=2.0)["events"]
    assert logs and all(e["source"] != "harness" for e in logs)
    audit = AuditLog(settings.audit_log_path).read_all()
    assert not any(r.get("event") == "egg.hatched" and r.get("run_id") == id2 for r in audit)
    assert not any(r.get("event") == "clutch.stored" and r.get("run_id") == id2 for r in audit)
    cancelled = [r for r in audit if r.get("event") == "egg.cancelled" and r.get("run_id") == id2]
    assert len(cancelled) == 1 and "never hatched" in cancelled[0]["reason"]
    assert "token" not in json.dumps(cancelled[0]).lower()  # no seat/token material
    assert any(
        r.get("event") == "abattoir.attested" and r.get("run_id") == id2 and r.get("process_reaped")
        for r in audit
    )
    fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == id2]
    assert fin and fin[0]["status"] == "cancelled"

    # the abattoir attestation exists exactly like any terminal state (1,1,1), no alert
    assert db_row(settings, ATT_SQL, (id2,)) == (1, 1, 1)
    assert id2 not in request(sock, "status", timeout=2.0)["alerts"]
    assert not (settings.data_dir / "workspaces" / id2).exists()

    # runs list + audit tail show the terminal state and reason
    row2 = [r for r in request(sock, "runs_list", timeout=2.0)["runs"] if r["id"] == id2][0]
    assert row2["status"] == "cancelled" and "never hatched" in row2["status_reason"] and row2["attested"] is True
    tail = request(sock, "audit_tail", limit=50, event="egg.cancelled", timeout=2.0)
    assert tail["ok"] is True and any(e["run_id"] == id2 for e in tail["events"])

    # the other chicken is untouched and still finishes normally
    assert wait_terminal(sock, id1, timeout=30)["status"] == "succeeded"

    # stopping an already-terminal or unknown chicken is a clear error
    resp = request(sock, "run_stop", run_id=id1, timeout=5.0)
    assert resp["ok"] is False and "already terminal" in resp["error"]
    resp = request(sock, "run_stop", run_id="run-nope", timeout=5.0)
    assert resp["ok"] is False and "unknown chicken" in resp["error"]
    resp = request(sock, "run_stop", timeout=5.0)
    assert resp["ok"] is False and "requires run_id" in resp["error"]


def test_tc_runs_stop_on_queued_egg_cli_parity(daemon, tmp_path) -> None:
    settings = daemon.settings
    slow = write_job(tmp_path, name="slow-cli", prompt="SLEEP 8 working")
    r1 = runner.invoke(app, ["run", str(slow)])
    r2 = runner.invoke(app, ["run", str(slow)])
    id1, id2 = run_id_of(r1), run_id_of(r2)
    wait_status(settings.socket_path, id1, ("provisioning", "running"))

    result = runner.invoke(app, ["runs", "stop", id2])
    assert result.exit_code == 0, combined_output(result)
    assert f"chicken {id2} → cancelled" in result.output
    assert "never hatched" in result.output

    wait_terminal(settings.socket_path, id1, timeout=30)
    result = runner.invoke(app, ["runs", "stop", id1])
    assert result.exit_code == 1
    assert "already terminal" in combined_output(result)
    result = runner.invoke(app, ["runs", "stop", "run-nope"])
    assert result.exit_code == 1
    assert "unknown chicken" in combined_output(result)

    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0, combined_output(result)
    assert "status=cancelled" in result.output and "never hatched" in result.output
    result = runner.invoke(app, ["runs", "show", id2])
    assert result.exit_code == 0 and "cancelled" in result.output and "attestation" in result.output


# --- run_stop on a running chicken: SIGTERM, attested teardown, `stopped` ------


def test_run_stop_on_running_chicken_sigterms_and_attests(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    slow = write_job(tmp_path, name="runaway", prompt="SLEEP 60 runaway", timeout=120)
    rid = run_id_of(runner.invoke(app, ["run", str(slow)]))
    wait_status(sock, rid, ("running",))

    t0 = time.time()
    resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
    elapsed = time.time() - t0
    assert resp["ok"] is True and resp["status"] == "stopped" and resp["run_id"] == rid
    assert "SIGTERM" in resp["reason"]
    assert elapsed < 20, f"stop took too long: {elapsed:.1f}s"

    run = request(sock, "runs_show", run_id=rid, timeout=2.0)["run"]
    assert run["status"] == "stopped"
    assert run["manifest"]["status"] == "stopped" and "SIGTERM" in run["manifest"]["status_reason"]
    att = run["attestation"]
    assert att and att["process_reaped"] and att["workspace_removed"] and att["env_cleared"]
    assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / rid).exists()

    # a stopped chicken never lays a clutch
    res = request(sock, "results_get", run_id=rid, timeout=2.0)
    assert res["ok"] is False and "no clutch" in res["error"]

    audit = AuditLog(settings.audit_log_path).read_all()
    stopped = [r for r in audit if r.get("event") == "chicken.stopped" and r.get("run_id") == rid]
    assert len(stopped) == 1 and "SIGTERM" in stopped[0]["reason"]
    assert "token" not in json.dumps(stopped[0]).lower()  # spec 09/19 secrecy rule
    assert any(
        r.get("event") == "abattoir.attested" and r.get("run_id") == rid and r.get("process_reaped")
        for r in audit
    )
    fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == rid]
    assert fin and fin[0]["status"] == "stopped"

    # no alert condition; audit tail + runs list (with filter) show stopped + reason
    assert rid not in request(sock, "status", timeout=2.0)["alerts"]
    tail = request(sock, "audit_tail", limit=50, event="chicken.stopped", timeout=2.0)
    assert tail["ok"] is True and any(e["run_id"] == rid for e in tail["events"])
    rows = request(sock, "runs_list", status="stopped", timeout=2.0)["runs"]
    assert any(r["id"] == rid and "SIGTERM" in (r["status_reason"] or "") and r["attested"] for r in rows)

    # CLI on the now-terminal chicken: clear error, rc 1
    result = runner.invoke(app, ["runs", "stop", rid])
    assert result.exit_code == 1 and "already terminal" in combined_output(result)


def test_run_stop_escalates_to_hard_kill_when_sigterm_is_ignored(tmp_path, monkeypatch, settings_file) -> None:
    bin_path = tmp_path / "fake-vibe-stubborn"
    bin_path.write_text(FAKE_VIBE_STUBBORN)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    monkeypatch.setattr(runner_mod, "STOP_TERM_GRACE_SEC", 1.0)  # keep the test fast
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    try:
        sock = settings.socket_path
        slow = write_job(tmp_path, name="stubborn", prompt="SLEEP 60 stubborn", timeout=120)
        rid = run_id_of(runner.invoke(app, ["run", str(slow)]))
        wait_status(sock, rid, ("running",))
        # wait until the harness process is actually up and has installed its
        # SIGTERM trap, so the stop below really has to escalate to the kill
        deadline = time.time() + 15
        while time.time() < deadline:
            logs = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
            if any("stubborn harness ready" in e["line"] for e in logs):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("stubborn harness never announced readiness")

        t0 = time.time()
        resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
        elapsed = time.time() - t0
        assert resp["ok"] is True and resp["status"] == "stopped"
        assert "SIGTERM" in resp["reason"] and "hard kill" in resp["reason"]
        assert 0.9 <= elapsed < 20, f"expected SIGTERM grace then hard kill, took {elapsed:.1f}s"

        # the harness saw the SIGTERM first (streamed as a sanitized log line)
        logs = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
        assert any(e["source"] == "harness" and "got SIGTERM" in e["line"] for e in logs)

        assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
        assert not (settings.data_dir / "workspaces" / rid).exists()
        assert rid not in request(sock, "status", timeout=2.0)["alerts"]
    finally:
        dt.stop()


# --- run_rerun: same egg, fresh run, lineage recorded --------------------------


def test_run_rerun_enqueues_same_egg_as_fresh_run_with_lineage(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    spec = write_job(tmp_path, name="retry-me", prompt="Say hello")
    r = runner.invoke(app, ["run", str(spec), "--wait"])
    assert r.exit_code == 0, combined_output(r)
    old = run_id_of(r)

    resp = request(sock, "run_rerun", run_id=old, timeout=10.0)
    assert resp["ok"] is True and resp["op"] == "run_rerun"
    new = resp["run_id"]
    assert new != old and resp["rerun_of"] == old
    assert resp["status"] == "queued" and resp["weight"] == 1.0 and resp["job_id"] == "retry-me"

    run = wait_terminal(sock, new, timeout=30)
    assert run["status"] == "succeeded" and run["trigger"] == "rerun"
    assert run["manifest"]["rerun_of"] == old

    # lineage in the Headcount; the new run went through the whole pipeline
    audit = AuditLog(settings.audit_log_path).read_all()
    q = [x for x in audit if x.get("event") == "egg.queued" and x.get("run_id") == new]
    assert len(q) == 1 and q[0]["trigger"] == "rerun" and q[0]["rerun_of"] == old
    assert any(x.get("event") == "egg.hatched" and x.get("run_id") == new for x in audit)
    assert any(x.get("event") == "clutch.stored" and x.get("run_id") == new for x in audit)

    # both runs in the list; the new run's clutch is fetchable
    ids = {x["id"] for x in request(sock, "runs_list", timeout=2.0)["runs"]}
    assert {old, new} <= ids
    res = request(sock, "results_get", run_id=new, timeout=2.0)
    assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"

    # CLI parity
    result = runner.invoke(app, ["runs", "rerun", old])
    assert result.exit_code == 0, combined_output(result)
    assert "re-queued" in result.output and f"re-run of {old}" in result.output
    new2 = rerun_id_of(result)
    assert wait_terminal(sock, new2, timeout=30)["status"] == "succeeded"
    result = runner.invoke(app, ["runs", "rerun", "run-nope"])
    assert result.exit_code == 1 and "unknown chicken" in combined_output(result)
    resp = request(sock, "run_rerun", timeout=5.0)
    assert resp["ok"] is False and "requires run_id" in resp["error"]


def test_run_rerun_after_cancel_succeeds(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    slow = write_job(tmp_path, name="cancel-then-retry", prompt="SLEEP 8 working")
    r1 = runner.invoke(app, ["run", str(slow)])
    r2 = runner.invoke(app, ["run", str(slow)])
    id1, id2 = run_id_of(r1), run_id_of(r2)
    wait_status(sock, id1, ("provisioning", "running"))
    assert request(sock, "run_stop", run_id=id2, timeout=15.0)["status"] == "cancelled"

    resp = request(sock, "run_rerun", run_id=id2, timeout=10.0)
    assert resp["ok"] is True and resp["rerun_of"] == id2
    new = resp["run_id"]
    # the budget is still held by id1, so the re-run backlogs — then succeeds
    run = wait_terminal(sock, new, timeout=40)
    assert run["status"] == "succeeded" and run["manifest"]["rerun_of"] == id2 and run["trigger"] == "rerun"
    assert wait_terminal(sock, id1, timeout=40)["status"] == "succeeded"


# --- subprocess e2e: real daemon + real CLI + fake vibe, cancel + stop + rerun -


def test_e2e_subprocess_daemon_cancel_stop_rerun(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    slow = write_job(tmp_path, name="e2e-slow", prompt="SLEEP 30 e2e", timeout=120)
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
                raise AssertionError("daemon subprocess exited early")
            time.sleep(0.05)
        else:
            raise AssertionError("daemon subprocess did not start answering")
        sock = settings.socket_path

        r1 = runner.invoke(app, ["run", str(slow)])
        r2 = runner.invoke(app, ["run", str(slow)])
        id1, id2 = run_id_of(r1), run_id_of(r2)
        wait_status(sock, id1, ("running",))

        # cancel the queued egg from the CLI
        result = runner.invoke(app, ["runs", "stop", id2])
        assert result.exit_code == 0, combined_output(result)
        assert f"chicken {id2} → cancelled" in result.output

        # stop the running chicken from the CLI
        result = runner.invoke(app, ["runs", "stop", id1])
        assert result.exit_code == 0, combined_output(result)
        assert f"chicken {id1} → stopped" in result.output
        show = request(sock, "runs_show", run_id=id1, timeout=2.0)["run"]
        assert show["status"] == "stopped" and show["attestation"]["process_reaped"] is True
        assert db_row(settings, ATT_SQL, (id1,)) == (1, 1, 1)
        assert db_row(settings, ATT_SQL, (id2,)) == (1, 1, 1)

        # re-run the stopped chicken from the CLI → fresh run through the pipeline
        result = runner.invoke(app, ["runs", "rerun", id1])
        assert result.exit_code == 0, combined_output(result)
        new = rerun_id_of(result)
        run = wait_terminal(sock, new, timeout=40)
        assert run["status"] == "succeeded" and run["manifest"]["rerun_of"] == id1
        res = request(sock, "results_get", run_id=new, timeout=2.0)
        assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"

        # Headcount over the socket: the new events are in the tail, no token material
        audit = AuditLog(settings.audit_log_path).read_all()
        events = {(x.get("event"), x.get("run_id")) for x in audit}
        assert ("egg.cancelled", id2) in events
        assert ("chicken.stopped", id1) in events
        for x in audit:
            if x.get("event") in ("egg.cancelled", "chicken.stopped"):
                assert "token" not in json.dumps(x).lower()
        tail = request(sock, "audit_tail", limit=200, timeout=2.0)
        assert any(e.get("event") == "chicken.stopped" and e.get("run_id") == id1 for e in tail["events"])
        assert any(e.get("event") == "egg.cancelled" and e.get("run_id") == id2 for e in tail["events"])

        # CLI list shows both terminal states with reasons
        result = runner.invoke(app, ["runs", "list"])
        assert result.exit_code == 0, combined_output(result)
        assert "status=cancelled" in result.output
        assert "status=stopped" in result.output

        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
