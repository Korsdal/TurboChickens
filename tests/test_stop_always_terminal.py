"""Tests for spec 32 ticket 19 (review finding G15): a stopped Chicken always
reaches a terminal state.

Seams: the real daemon on a temp socket (in a thread), driven through the
JSON-lines socket and the `tc` CLI, with fake vibe binaries via
$TURBOCHICKENS_VIBE_BIN that spawn grandchildren holding the stdout/stderr
pipes open — one straggler stays in the chicken's process group, one escapes
it with `setsid`. Assertions are external, observable behavior only: the
`run_stop` socket response, the terminal status + reason, the abattoir
attestation (1,1,1) in the DB and the Headcount, the workspace on disk, and
the wall-clock bound on the stop. Pre-fix, a pipe-holding grandchild wedged
the run non-terminal forever (`run_stop` answered "did not reach a terminal
state in time" and no attestation ever landed).
"""

from __future__ import annotations

import contextlib
import json
import os as _os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import turbochickens.adapter as adapter_mod
import turbochickens.runner as runner_mod
from test_daemon import DaemonThread
from test_lifecycle_ops import wait_status, wait_terminal
from test_run_e2e import FAKE_VIBE, combined_output, db_row, run_id_of, write_job
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings

runner = CliRunner()

# A fake vibe that spawns a straggler grandchild which inherits our stdout /
# stderr pipes and holds them open while it sleeps. With GRANDCHILD (no
# ESCAPE) the straggler stays in the chicken's process group and ignores
# SIGTERM — only a process-GROUP kill can reap it. With ESCAPE it calls setsid()
# and leaves the group — even a group kill cannot reach it, so only the
# bounded post-kill await (synthetic result) can end the run.
FAKE_VIBE_STRAGGLER = '''#!/usr/bin/env python3
import json, os, re, signal, subprocess, sys, time

def on_term(signum, frame):
    print("harness got SIGTERM and ignored it", file=sys.stderr, flush=True)

signal.signal(signal.SIGTERM, on_term)
print("straggler harness ready", file=sys.stderr, flush=True)
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
m = re.search(r"SLEEP ([\\d.]+)", prompt)
if "GRANDCHILD" in prompt:
    escape = "ESCAPE" in prompt
    code = (
        "import os, signal, sys, time;"
        "signal.signal(signal.SIGTERM, lambda s, f: print('straggler got SIGTERM and ignored it', file=sys.stderr, flush=True));"
        + ("os.setsid();" if escape else "")
        + "print('straggler ready', flush=True); time.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", code])
if m:
    time.sleep(float(m.group(1)))
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps({"message": "hello from straggler fake vibe"}))
'''

ATT_SQL = (
    "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?"
)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 1\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def straggler_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-straggler"
    bin_path.write_text(FAKE_VIBE_STRAGGLER)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def fast_kill_bounds(monkeypatch):
    # keep the tests fast: short grace, short post-kill await, short reap/drain
    # (raising=False so the suite can also run against pre-fix code, where the
    # new bounds do not exist yet and the tests must fail on behavior, not here)
    monkeypatch.setattr(runner_mod, "STOP_TERM_GRACE_SEC", 1.0)
    monkeypatch.setattr(runner_mod, "POST_KILL_AWAIT_SEC", 1.0, raising=False)
    monkeypatch.setattr(adapter_mod, "KILL_REAP_WAIT_SEC", 1.0, raising=False)
    monkeypatch.setattr(adapter_mod, "PIPE_DRAIN_WAIT_SEC", 1.0, raising=False)


def _daemon(settings_file, vibe_bin):
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    return dt


def _stop_quietly(sock, rid: str) -> None:
    """Best-effort stop so no chicken is left in flight at daemon shutdown."""
    with contextlib.suppress(Exception):
        request(sock, "run_stop", run_id=rid, timeout=30.0)


def _wait_straggler_ready(sock, rid: str) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        logs = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
        if any("straggler ready" in e["line"] for e in logs):
            return
        time.sleep(0.05)
    raise AssertionError("straggler never announced readiness")


def _assert_stopped_terminal(settings, sock, rid, reason_substr: str | None = None) -> dict:
    run = request(sock, "runs_show", run_id=rid, timeout=2.0)["run"]
    assert run["status"] == "stopped", f"run not stopped: {run['status']}"
    assert run["manifest"]["status"] == "stopped"
    assert "stopped by operator" in run["manifest"]["status_reason"]
    if reason_substr:
        assert reason_substr in run["manifest"]["status_reason"]
    att = run["attestation"]
    assert att and att["process_reaped"] and att["workspace_removed"] and att["env_cleared"]
    # the abattoir attestation (1,1,1) landed on the stop path
    assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / rid).exists()
    audit = AuditLog(settings.audit_log_path).read_all()
    assert any(r.get("event") == "chicken.stopped" and r.get("run_id") == rid for r in audit)
    assert any(
        r.get("event") == "abattoir.attested" and r.get("run_id") == rid and r.get("process_reaped")
        for r in audit
    )
    fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == rid]
    assert fin and fin[0]["status"] == "stopped"
    # a stopped chicken never lays a clutch; no alert condition
    res = request(sock, "results_get", run_id=rid, timeout=2.0)
    assert res["ok"] is False and "no clutch" in res["error"]
    assert rid not in request(sock, "status", timeout=2.0)["alerts"]
    return run


# --- the pipe-holding grandchild in the process group: group kill ------------


def test_stop_kills_whole_process_group_so_pipe_holding_grandchild_cannot_wedge_the_run(
    tmp_path, monkeypatch, settings_file, straggler_vibe, fast_kill_bounds
) -> None:
    """The straggler stays in the chicken's process group, ignores SIGTERM, and
    holds the stdout pipe open. Pre-fix only the direct child was killed, so
    the pipe never saw EOF, the post-kill `await exec_task` hung forever, and
    the run never reached a terminal state (no attestation). Now the SIGTERM
    and the hard kill go to the whole group: the straggler dies, the pipes
    close, and the stop completes."""
    dt = _daemon(settings_file, straggler_vibe)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="straggler-in-group", prompt="SLEEP 60 GRANDCHILD", timeout=120)
        rid = run_id_of(runner.invoke(app, ["run", str(job)]))
        try:
            wait_status(sock, rid, ("running",))
            _wait_straggler_ready(sock, rid)

            t0 = time.time()
            resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
            elapsed = time.time() - t0
            assert resp["ok"] is True, f"stop did not reach terminal: {resp}"
            assert resp["status"] == "stopped" and resp["run_id"] == rid
            assert "SIGTERM" in resp["reason"] and "hard kill" in resp["reason"]
            # grace (1s) + reap + drain — bounded, far below the 45s op deadline
            assert elapsed < 15, f"stop took too long: {elapsed:.1f}s"

            _assert_stopped_terminal(settings, sock, rid, reason_substr="hard kill")
            # the straggler saw the group SIGTERM before the SIGKILL (streamed log)
            logs = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
            assert any("straggler got SIGTERM" in e["line"] for e in logs)
        finally:
            _stop_quietly(sock, rid)
    finally:
        dt.stop()


# --- the pipe-holding escapee (setsid): bounded await + synthetic result ------


def test_stop_with_pipe_holding_escapee_still_reaches_terminal_via_bounded_await(
    tmp_path, monkeypatch, settings_file, straggler_vibe, fast_kill_bounds
) -> None:
    """The straggler calls setsid() and escapes the process group, so even the
    group SIGKILL cannot reap it and the pipes stay open. The post-kill await
    of the harness task is bounded: on timeout a synthetic harness result is
    produced, teardown completes, and the run still reaches terminal `stopped`
    with the abattoir attestation (1,1,1)."""
    dt = _daemon(settings_file, straggler_vibe)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="escapee", prompt="SLEEP 60 GRANDCHILD ESCAPE", timeout=120)
        rid = run_id_of(runner.invoke(app, ["run", str(job)]))
        try:
            wait_status(sock, rid, ("running",))
            _wait_straggler_ready(sock, rid)

            t0 = time.time()
            resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
            elapsed = time.time() - t0
            assert resp["ok"] is True, f"stop did not reach terminal: {resp}"
            assert resp["status"] == "stopped"
            assert "SIGTERM" in resp["reason"]
            # the synthetic-result marker: pipes were still held after the group kill
            assert "pipes still held" in resp["reason"]
            # grace (1s) + post-kill await (1s) + reap — bounded
            assert elapsed < 15, f"stop took too long: {elapsed:.1f}s"

            _assert_stopped_terminal(settings, sock, rid, reason_substr="pipes still held")
            # the run is terminal in runs_list as well, attested
            rows = request(sock, "runs_list", status="stopped", timeout=2.0)["runs"]
            assert any(r["id"] == rid and r["attested"] for r in rows)
        finally:
            _stop_quietly(sock, rid)
    finally:
        dt.stop()


# --- the hard-timeout kill path is group-wide and bounded too -----------------


def test_hard_timeout_with_pipe_holding_escapee_still_reaches_terminal(
    tmp_path, monkeypatch, settings_file, straggler_vibe, fast_kill_bounds
) -> None:
    """timeout_sec fires: the whole process group gets the SIGKILL and the pipe
    drain is bounded, so even a setsid escapee holding the pipes cannot leave
    the run wedged in `running` — it ends `failed` (timeout) with attestation."""
    dt = _daemon(settings_file, straggler_vibe)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="timeout-escapee", prompt="SLEEP 60 GRANDCHILD ESCAPE", timeout=1)
        rid = run_id_of(runner.invoke(app, ["run", str(job)]))

        t0 = time.time()
        run = wait_terminal(sock, rid, timeout=30.0)
        elapsed = time.time() - t0
        assert run["status"] == "failed"
        assert "timeout: hard-killed after 1s" in run["manifest"]["status_reason"]
        assert run["manifest"]["budget"]["outcome"] == "timeout"
        assert run["manifest"]["budget"]["timed_out"] is True
        # 1s timeout + bounded reap (1s) + bounded drain (1s) + slack
        assert elapsed < 15, f"timeout path took too long: {elapsed:.1f}s"
        assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
        assert not (settings.data_dir / "workspaces" / rid).exists()
        assert rid not in request(sock, "status", timeout=2.0)["alerts"]
        audit = AuditLog(settings.audit_log_path).read_all()
        assert any(
            r.get("event") == "abattoir.attested" and r.get("run_id") == rid and r.get("process_reaped")
            for r in audit
        )
    finally:
        dt.stop()


# --- positive: the normal stop path is unchanged ------------------------------


def test_normal_stop_path_unchanged_and_legit_run_still_succeeds(
    tmp_path, monkeypatch, settings_file, fast_kill_bounds
) -> None:
    """Positive test: with a well-behaved fake vibe (dies on SIGTERM, no
    straggler), `run_stop` behaves exactly as before — `stopped` with the
    SIGTERM-within-grace reason and attestation (1,1,1) — and an untouched
    egg still runs to `succeeded` with a stored clutch."""
    bin_path = tmp_path / "fake-vibe-plain"
    bin_path.write_text(FAKE_VIBE)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    dt = _daemon(settings_file, bin_path)
    try:
        settings = dt.settings
        sock = settings.socket_path
        # a legit egg runs to success end to end (positive path)
        ok_job = write_job(tmp_path, name="plain-ok", prompt="Say hello")
        ok_id = run_id_of(runner.invoke(app, ["run", str(ok_job), "--wait"]))
        assert wait_terminal(sock, ok_id, timeout=30)["status"] == "succeeded"
        res = request(sock, "results_get", run_id=ok_id, timeout=2.0)
        assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"

        # the normal stop path is unchanged
        slow = write_job(tmp_path, name="plain-slow", prompt="SLEEP 60 slow", timeout=120)
        rid = run_id_of(runner.invoke(app, ["run", str(slow)]))
        try:
            wait_status(sock, rid, ("running",))
            t0 = time.time()
            resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
            elapsed = time.time() - t0
            assert resp["ok"] is True and resp["status"] == "stopped"
            assert "SIGTERM" in resp["reason"] and "reaped within 1s grace" in resp["reason"]
            assert "hard kill" not in resp["reason"]
            assert elapsed < 10, f"normal stop took too long: {elapsed:.1f}s"
            _assert_stopped_terminal(settings, sock, rid, reason_substr="reaped within 1s grace")

            # CLI parity on the stop
            slow2 = write_job(tmp_path, name="plain-slow2", prompt="SLEEP 60 slow2", timeout=120)
            rid2 = run_id_of(runner.invoke(app, ["run", str(slow2)]))
            try:
                wait_status(sock, rid2, ("running",))
                result = runner.invoke(app, ["runs", "stop", rid2])
                assert result.exit_code == 0, combined_output(result)
                assert f"chicken {rid2} → stopped" in result.output
                assert (
                    request(sock, "runs_show", run_id=rid2, timeout=2.0)["run"]["status"]
                    in TERMINAL_STATES
                )
            finally:
                _stop_quietly(sock, rid2)
        finally:
            _stop_quietly(sock, rid)
    finally:
        dt.stop()


# --- the spawn really is its own session (external: pid/pgid/sid) -------------


def test_spawned_chicken_runs_in_its_own_session_process_group(
    tmp_path, monkeypatch, settings_file, fast_kill_bounds
) -> None:
    """External proof of `start_new_session`: the harness reports its own
    pid/pgid/sid on stdout (streamed into the run log) — sid == pgid == pid,
    and the group is the chicken's, not coopd's (the daemon shares the pytest
    process's group). The stop then still works through the group kill."""
    probe = '''#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
print(json.dumps({"pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0)}), flush=True)
m = re.search(r"SLEEP ([\\d.]+)", prompt)
if m:
    time.sleep(float(m.group(1)))
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps({"message": "hello from probe vibe"}))
'''
    bin_path = tmp_path / "fake-vibe-probe"
    bin_path.write_text(probe)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    dt = _daemon(settings_file, bin_path)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="probe", prompt="SLEEP 30 probe", timeout=120)
        rid = run_id_of(runner.invoke(app, ["run", str(job)]))
        try:
            wait_status(sock, rid, ("running",))
            deadline = time.time() + 15
            probe_line = None
            while time.time() < deadline:
                logs = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
                for e in logs:
                    if e["source"] == "harness" and '"pgid"' in e["line"]:
                        probe_line = json.loads(e["line"])
                        break
                if probe_line:
                    break
                time.sleep(0.05)
            assert probe_line, "harness never reported its pid/pgid/sid"
            # own session: sid == pid == pgid, and the group is the chicken's,
            # not coopd's (the daemon shares the pytest process's group)
            assert probe_line["sid"] == probe_line["pid"]
            assert probe_line["pgid"] == probe_line["pid"]
            assert probe_line["pgid"] != _os.getpgid(0)
            # and the stop still works through the group kill
            resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
            assert resp["ok"] is True and resp["status"] == "stopped"
            _assert_stopped_terminal(settings, sock, rid)
        finally:
            _stop_quietly(sock, rid)
    finally:
        dt.stop()


# --- reviewer finding: the proc-less (HTTP-adapter) stop path -------------------


class _ProcLessAdapter:
    """A proc-less harness adapter (the mistral-api class): `execute` hangs
    until cancelled and `ctx.proc` stays None, so `run_stop` takes the
    cancel branch of the stop path, not the signal branch."""

    name = "proc-less-fake"

    def prepare(self, run):
        from turbochickens.adapter import PreparedRun

        run.env = dict(run.env)
        return PreparedRun(
            context=run,
            harness={
                "adapter": self.name,
                "model": run.spec.harness.model,
                "temperature": run.spec.harness.temperature,
            },
            auth={"mode": "none", "token_expires_at": None},
        )

    async def execute(self, prepared, event_sink, timeout_sec):
        import asyncio

        await event_sink("harness", "info", "proc-less harness in flight")
        await asyncio.Event().wait()  # hangs forever — only cancellation ends it

    async def teardown(self, run):
        import shutil

        from turbochickens.adapter import TeardownReport

        if run.workspace.exists():
            shutil.rmtree(run.workspace, ignore_errors=True)
        run.env.clear()
        return TeardownReport(
            process_reaped=True, workspace_removed=not run.workspace.exists(), env_cleared=True
        )


def test_stop_of_proc_less_adapter_ends_stopped_not_failed(tmp_path, monkeypatch, settings_file) -> None:
    """Reviewer finding (runner stop path, proc-less branch): `exec_task.cancel()`
    then awaiting the shielded task raises CancelledError, not TimeoutError —
    pre-fix that escaped the `except TimeoutError` and the stopped chicken ended
    failed/internal error instead of `stopped`."""
    monkeypatch.setattr(runner_mod, "make_adapter", lambda *a, **k: _ProcLessAdapter())
    monkeypatch.setattr(runner_mod, "POST_KILL_AWAIT_SEC", 1.0, raising=False)
    dt = _daemon(settings_file, None)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="procless-egg", prompt="Say hello")
        rid = request(sock, "enqueue", spec_path=str(job), timeout=5.0)["run_id"]
        wait_status(sock, rid, ("running",))
        resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
        assert resp["ok"] is True, resp
        # positive: the proc-less chicken ends `stopped` (not failed/internal error)
        assert resp["status"] == "stopped" and resp["run_id"] == rid
        assert "stopped by operator" in resp["reason"]
        run = request(sock, "runs_show", run_id=rid, timeout=2.0)["run"]
        assert run["status"] == "stopped", run["status"]
        assert run["manifest"]["status"] == "stopped"
        assert "in-flight harness call cancelled" in run["manifest"]["status_reason"]
        # attested teardown (1,1,1) exactly like the signal branch
        assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
        assert not (settings.data_dir / "workspaces" / rid).exists()
        audit = AuditLog(settings.audit_log_path).read_all()
        assert any(r.get("event") == "chicken.stopped" and r.get("run_id") == rid for r in audit)
        assert rid not in request(sock, "status", timeout=2.0)["alerts"]
    finally:
        _stop_quietly(sock, rid)
        dt.stop()


# --- reviewer finding: harness-streamed lines are host-path redacted ------------


FAKE_VIBE_ECHO_HOME = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt_file = args[args.index("--prompt-file") + 1] if "--prompt-file" in args else ""
print("HOME=" + os.environ.get("HOME", ""), file=sys.stderr)
print("PROMPT=" + prompt_file, file=sys.stderr)
print("CWD=" + os.getcwd(), file=sys.stderr)
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps({"message": "hello from echo fake vibe"}))
'''


def test_harness_lines_echoing_home_are_host_path_redacted_in_runs_logs(
    tmp_path, monkeypatch, settings_file
) -> None:
    """Reviewer finding (G17, _event choke point): run-event lines are
    sanitized but must also be host-path-redacted — a harness-streamed line
    can echo the chicken's absolute HOME (the Scratch inside its workspace).
    Pre-fix the absolute path landed in runs_logs."""
    bin_path = tmp_path / "fake-vibe-echo"
    bin_path.write_text(FAKE_VIBE_ECHO_HOME)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    dt = _daemon(settings_file, bin_path)
    try:
        settings = dt.settings
        sock = settings.socket_path
        job = write_job(tmp_path, name="echo-egg", prompt="Say hello")
        rid = request(sock, "enqueue", spec_path=str(job), timeout=5.0)["run_id"]
        run = wait_terminal(sock, rid)
        assert run["status"] == "succeeded", run["manifest"].get("status_reason")
        events = request(sock, "runs_logs", run_id=rid, timeout=2.0)["events"]
        lines = [e["line"] for e in events]
        # positive: the harness's HOME line was captured (sanitized, stored)
        home_lines = [ln for ln in lines if "HOME=" in ln]
        assert home_lines, lines
        # negative: no stored line carries the host paths (data dir / egg dir)
        for ln in lines:
            assert str(settings.data_dir) not in ln, ln
            assert str(tmp_path) not in ln, ln
        # positive: the redaction kept the root-relative form
        assert any(".tc-home" in ln for ln in home_lines), home_lines
    finally:
        dt.stop()
