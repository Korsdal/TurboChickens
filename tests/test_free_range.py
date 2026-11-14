"""Tests for spec 23: Free-Range chickens — no hard TTL, attested death only
by hand (run_stop, spec 22) or daemon shutdown.

Seams: the pure spec-model/loader pairing rules (free_range = false requires
timeout_sec > 0; free_range = true allows it absent; extra="forbid" intact;
spec_hash stable for pre-23 eggs), and e2e through the `tc` CLI against a real
daemon on a temp socket (thread daemon for most tests, a real
`python -m turbochickens daemon start` subprocess for the shutdown e2e) with
the hermetic fake vibe binary via $TURBOCHICKENS_VIBE_BIN. Assertions are
external, observable behavior only: socket/CLI responses, store + audit
contents, status transitions, attestations.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from test_daemon import DaemonThread
from test_lifecycle_ops import ATT_SQL, wait_status, wait_terminal  # noqa: F401
from test_run_e2e import (  # noqa: F401
    FAKE_VIBE,
    combined_output,
    db_row,
    run_id_of,
)
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.settings import load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.spec.model import ResourcesSection

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent


def write_free_range_job(
    tmp_path: Path,
    name: str,
    prompt: str = "Say hello",
    *,
    free_range: bool = True,
    timeout: int | None = None,
) -> Path:
    """A vibe-cli job; `timeout=None` omits timeout_sec from [resources]."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    resources = "free_range = true\n" if free_range else ""
    if timeout is not None:
        resources += f"timeout_sec = {timeout}\n"
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
{resources}max_context_mb = 2
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
    # budget 4: several vibe-cli chickens (weight 1.0 each) run concurrently
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


BASE_TOML = """
[job]
name = "fr-test"

[trigger]
type = "manual"

[harness]
adapter = "mistral-api"
model = "mistral-large-latest"
temperature = 0.0

[resources]
{resources}
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/hello.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""


def write_toml(tmp_path: Path, resources: str) -> Path:
    d = tmp_path / "spec"
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text("hi")
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    p = d / "job.toml"
    p.write_text(BASE_TOML.format(resources=resources))
    return p


# --- pure seam: the free_range / timeout_sec pairing --------------------------


def test_default_section_still_requires_timeout_sec() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResourcesSection.model_validate({"max_context_mb": 2, "network": "none"})
    assert ("timeout_sec",) in [tuple(e["loc"]) for e in exc_info.value.errors()]
    res = ResourcesSection.model_validate({"timeout_sec": 60, "max_context_mb": 2, "network": "none"})
    assert res.free_range is False and res.timeout_sec == 60


def test_free_range_false_explicitly_still_requires_timeout_sec() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResourcesSection.model_validate({"free_range": False, "max_context_mb": 2, "network": "none"})
    assert ("timeout_sec",) in [tuple(e["loc"]) for e in exc_info.value.errors()]


def test_free_range_true_allows_timeout_sec_absent() -> None:
    res = ResourcesSection.model_validate({"free_range": True, "max_context_mb": 2, "network": "none"})
    assert res.free_range is True and res.timeout_sec is None


def test_free_range_true_allows_timeout_sec_present_but_still_positive() -> None:
    res = ResourcesSection.model_validate(
        {"free_range": True, "timeout_sec": 60, "max_context_mb": 2, "network": "none"}
    )
    assert res.timeout_sec == 60
    with pytest.raises(ValidationError) as exc_info:
        ResourcesSection.model_validate(
            {"free_range": True, "timeout_sec": 0, "max_context_mb": 2, "network": "none"}
        )
    assert ("timeout_sec",) in [tuple(e["loc"]) for e in exc_info.value.errors()]


def test_free_range_typo_key_is_rejected_extra_forbid_intact(tmp_path) -> None:
    p = write_toml(tmp_path, "free_rang = true\ntimeout_sec = 60\n")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(p)
    assert ("resources", "free_rang") in [i.loc for i in exc_info.value.issues]


def test_load_spec_field_named_pairing_errors(tmp_path) -> None:
    # fenced egg with no timeout_sec → field-named error
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_toml(tmp_path, ""))
    assert ("resources", "timeout_sec") in [i.loc for i in exc_info.value.issues]
    # free-range egg with no timeout_sec → loads fine
    spec = load_spec(write_toml(tmp_path / "a", "free_range = true\n"))
    assert spec.resources.free_range is True and spec.resources.timeout_sec is None
    # free-range egg with an explicit timeout → loads, value kept (not enforced as a kill)
    spec = load_spec(write_toml(tmp_path / "b", "free_range = true\ntimeout_sec = 45\n"))
    assert spec.resources.timeout_sec == 45


def test_tc_jobs_validate_free_range_pairing(tmp_path) -> None:
    ok = write_toml(tmp_path / "ok", "free_range = true\n")
    result = runner.invoke(app, ["jobs", "validate", str(ok)])
    assert result.exit_code == 0 and "OK" in result.output, combined_output(result)
    bad = write_toml(tmp_path / "bad", "")
    result = runner.invoke(app, ["jobs", "validate", str(bad)])
    assert result.exit_code == 1
    assert "resources.timeout_sec" in combined_output(result)


def test_spec_hash_stable_for_pre_23_eggs(tmp_path) -> None:
    # free_range absent vs explicit false → same hash; true → different
    h_default = spec_hash(load_spec(write_toml(tmp_path / "d", "timeout_sec = 60\n")))
    h_false = spec_hash(load_spec(write_toml(tmp_path / "f", "free_range = false\ntimeout_sec = 60\n")))
    h_true = spec_hash(load_spec(write_toml(tmp_path / "t", "free_range = true\n")))
    assert h_default == h_false
    assert h_true != h_default
    # the shipped example egg (no free_range key) keeps its pre-23 hash
    example = REPO_ROOT / "examples" / "weekly-incidents-digest" / "job.toml"
    assert spec_hash(load_spec(example)).startswith("18efbd8c")


# --- e2e: a Free-Range chicken is not hard-killed at any TTL -------------------


def test_free_range_chicken_runs_past_any_ttl_while_fenced_control_times_out(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    # free-range, no timeout_sec at all: SLEEP 5 must run to completion
    fr = write_free_range_job(tmp_path, "free-slow", "SLEEP 5 free range", free_range=True, timeout=None)
    # fenced control with timeout_sec=1: SLEEP 30 must hard-kill at 1s
    fenced = write_free_range_job(tmp_path, "fenced-slow", "SLEEP 30 fenced", free_range=False, timeout=1)

    r_fr = runner.invoke(app, ["run", str(fr)])
    r_fc = runner.invoke(app, ["run", str(fenced)])
    assert r_fr.exit_code == 0 and r_fc.exit_code == 0, combined_output(r_fr) + combined_output(r_fc)
    id_fr, id_fc = run_id_of(r_fr), run_id_of(r_fc)
    wait_status(sock, id_fr, ("running",))
    wait_status(sock, id_fc, ("running",))

    # ~2s in: the fenced chicken is dead (timeout), the free-range one is still
    # running and visible — no hard kill happened to it
    time.sleep(2.0)
    fr_now = request(sock, "runs_show", run_id=id_fr, timeout=2.0)["run"]
    assert fr_now["status"] == "running"
    fc_now = wait_terminal(sock, id_fc, timeout=15)
    assert fc_now["status"] == "failed"
    assert fc_now["manifest"]["budget"]["outcome"] == "timeout"
    assert fc_now["manifest"]["budget"]["free_range"] is False
    assert fc_now["manifest"]["budget"]["timeout_sec"] == 1

    # the free-range chicken finishes on its own → succeeded, no timeout flags
    fr_done = wait_terminal(sock, id_fr, timeout=30)
    assert fr_done["status"] == "succeeded"
    m = fr_done["manifest"]
    assert m["timed_out"] is False
    assert m["resources"]["free_range"] is True
    assert m["resources"]["timeout_sec"] is None
    b = m["budget"]
    assert b["free_range"] is True and b["timeout_sec"] is None
    assert b["outcome"] == "ok" and b["timed_out"] is False
    assert db_row(settings, ATT_SQL, (id_fr,)) == (1, 1, 1)
    res = request(sock, "results_get", run_id=id_fr, timeout=2.0)
    assert res["ok"] is True and res["output"]["message"] == "hello from fake vibe"


def test_free_range_with_timeout_present_is_still_not_hard_killed(daemon, tmp_path) -> None:
    # free_range = true removes the hard kill even when timeout_sec is set
    settings = daemon.settings
    sock = settings.socket_path
    spec = write_free_range_job(tmp_path, "fr-with-ttl", "SLEEP 5 declared ttl", free_range=True, timeout=1)
    rid = run_id_of(runner.invoke(app, ["run", str(spec)]))
    wait_status(sock, rid, ("running",))
    time.sleep(2.5)  # past the declared timeout_sec=1
    run = request(sock, "runs_show", run_id=rid, timeout=2.0)["run"]
    assert run["status"] == "running", "free-range chicken was hard-killed despite free_range = true"
    done = wait_terminal(sock, rid, timeout=30)
    assert done["status"] == "succeeded"
    assert done["manifest"]["budget"]["timeout_sec"] == 1  # declared, recorded…
    assert done["manifest"]["budget"]["timed_out"] is False and done["manifest"]["budget"]["outcome"] == "ok"


# --- e2e: run_stop on a Free-Range chicken → stopped, attested ------------------


def test_run_stop_on_free_range_chicken_stops_it_with_full_attestation(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    spec = write_free_range_job(tmp_path, "fr-runaway", "SLEEP 60 free runaway", free_range=True, timeout=None)
    rid = run_id_of(runner.invoke(app, ["run", str(spec)]))
    wait_status(sock, rid, ("running",))

    t0 = time.time()
    resp = request(sock, "run_stop", run_id=rid, timeout=30.0)
    elapsed = time.time() - t0
    assert resp["ok"] is True and resp["status"] == "stopped" and resp["run_id"] == rid
    assert "SIGTERM" in resp["reason"]
    assert elapsed < 20, f"stop took too long: {elapsed:.1f}s"

    run = request(sock, "runs_show", run_id=rid, timeout=2.0)["run"]
    assert run["status"] == "stopped"
    assert run["manifest"]["status"] == "stopped"
    assert run["manifest"]["budget"]["free_range"] is True
    assert run["attestation"] and all(
        (run["attestation"][k] for k in ("process_reaped", "workspace_removed", "env_cleared"))
    )
    assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / rid).exists()

    # a stopped chicken never lays a clutch
    res = request(sock, "results_get", run_id=rid, timeout=2.0)
    assert res["ok"] is False and "no clutch" in res["error"]

    audit = AuditLog(settings.audit_log_path).read_all()
    stopped = [r for r in audit if r.get("event") == "chicken.stopped" and r.get("run_id") == rid]
    assert len(stopped) == 1 and "SIGTERM" in stopped[0]["reason"]
    assert any(
        r.get("event") == "abattoir.attested" and r.get("run_id") == rid and r.get("process_reaped")
        for r in audit
    )
    fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == rid]
    assert fin and fin[0]["status"] == "stopped"
    assert rid not in request(sock, "status", timeout=2.0)["alerts"]
    row = [r for r in request(sock, "runs_list", status="stopped", timeout=2.0)["runs"] if r["id"] == rid][0]
    assert row["attested"] is True and "SIGTERM" in row["status_reason"]

    # CLI parity on a second free-range chicken
    spec2 = write_free_range_job(tmp_path, "fr-runaway-2", "SLEEP 60 free runaway 2", free_range=True)
    rid2 = run_id_of(runner.invoke(app, ["run", str(spec2)]))
    wait_status(sock, rid2, ("running",))
    result = runner.invoke(app, ["runs", "stop", rid2])
    assert result.exit_code == 0, combined_output(result)
    assert f"chicken {rid2} → stopped" in result.output and "SIGTERM" in result.output
    assert db_row(settings, ATT_SQL, (rid2,)) == (1, 1, 1)


# --- e2e: daemon shutdown with a Free-Range chicken in flight -------------------


def test_daemon_shutdown_stops_in_flight_free_range_chicken_attested(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    try:
        sock = settings.socket_path
        spec = write_free_range_job(tmp_path, "fr-shutdown", "SLEEP 60 shutdown me", free_range=True)
        rid = run_id_of(runner.invoke(app, ["run", str(spec)]))
        wait_status(sock, rid, ("running",))

        t0 = time.time()
        dt.stop()  # sends the shutdown op; the daemon must tear the chicken down before exit
        elapsed = time.time() - t0
        assert elapsed < 20, f"shutdown did not tear down the free-range chicken promptly ({elapsed:.1f}s)"

        # terminal `stopped` with the abattoir's full (1,1,1) attestation
        assert db_row(settings, "SELECT status FROM runs WHERE id = ?", (rid,)) == ("stopped",)
        assert db_row(settings, ATT_SQL, (rid,)) == (1, 1, 1)
        assert not (settings.data_dir / "workspaces" / rid).exists()
        audit = AuditLog(settings.audit_log_path).read_all()
        events = {(r.get("event"), r.get("run_id")) for r in audit}
        assert ("chicken.stopped", rid) in events
        assert any(
            r.get("event") == "abattoir.attested" and r.get("run_id") == rid and r.get("process_reaped")
            for r in audit
        )
        fin = [r for r in audit if r.get("event") == "chicken.finished" and r.get("run_id") == rid]
        assert fin and fin[0]["status"] == "stopped"
        assert ("coopd.stopped", None) in events  # clean exit row present
        # a missing attestation would be an alert condition — it is not missing
        assert db_row(settings, "SELECT COUNT(*) FROM teardown_attestations WHERE run_id = ?", (rid,)) == (1,)
    finally:
        if dt.thread.is_alive():
            dt.stop()


def test_e2e_subprocess_daemon_free_range_stop_and_shutdown(settings_file, tmp_path, fake_vibe) -> None:
    settings = load_settings(settings_file)
    spec_stop = write_free_range_job(tmp_path, "e2e-fr-stop", "SLEEP 60 stop me", free_range=True)
    spec_down = write_free_range_job(tmp_path, "e2e-fr-down", "SLEEP 60 shut down with me", free_range=True)
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

        rid_stop = run_id_of(runner.invoke(app, ["run", str(spec_stop)]))
        rid_down = run_id_of(runner.invoke(app, ["run", str(spec_down)]))
        wait_status(sock, rid_stop, ("running",))
        wait_status(sock, rid_down, ("running",))

        # death by hand: tc runs stop on the free-range chicken
        result = runner.invoke(app, ["runs", "stop", rid_stop])
        assert result.exit_code == 0, combined_output(result)
        assert f"chicken {rid_stop} → stopped" in result.output
        assert db_row(settings, ATT_SQL, (rid_stop,)) == (1, 1, 1)

        # death by shutdown: tc daemon stop with the other free-range chicken in flight
        t0 = time.time()
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, combined_output(result)
        proc.wait(timeout=30)
        elapsed = time.time() - t0
        assert proc.returncode == 0
        assert elapsed < 25, f"shutdown did not tear down the free-range chicken promptly ({elapsed:.1f}s)"

        assert db_row(settings, "SELECT status FROM runs WHERE id = ?", (rid_down,)) == ("stopped",)
        assert db_row(settings, ATT_SQL, (rid_down,)) == (1, 1, 1)
        assert not (settings.data_dir / "workspaces" / rid_down).exists()
        audit = AuditLog(settings.audit_log_path).read_all()
        events = {(r.get("event"), r.get("run_id")) for r in audit}
        assert ("chicken.stopped", rid_stop) in events
        assert ("chicken.stopped", rid_down) in events
        assert any(
            r.get("event") == "abattoir.attested" and r.get("run_id") in (rid_stop, rid_down) for r in audit
        )
        assert ("coopd.stopped", None) in events
        # no results row for either (stopped chickens never lay a clutch)
        assert db_row(settings, "SELECT 1 FROM results WHERE run_id IN (?, ?)", (rid_stop, rid_down)) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
