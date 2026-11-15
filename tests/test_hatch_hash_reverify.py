"""Tests for spec 32 ticket 21 (review finding G18): the spec hash pinned at
enqueue is re-verified when the egg is loaded at hatch. A swapped egg (its
job.toml changed between enqueue and hatch) fails closed — terminal `failed`
with the abattoir's attestation (1,1,1), nothing provisioned and nothing sent
to the feeder — instead of shipping different content under the stale hash.

Seams: the real daemon on a temp socket (in a thread) driven over the
JSON-lines socket ops, with the hermetic fake vibe binary via
$TURBOCHICKENS_VIBE_BIN. The enqueue-time pin is the `spec_hash` argument of
`enqueue`; the weight budget is 1, so a slow chicken holds the whole budget
and a second egg backlogs in the pecking order — a deterministic swap window
between enqueue and hatch. Assertions are external, observable behavior only:
socket responses, run rows/manifests, run events, the results table, on-disk
attestation rows, and the Headcount.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, db_row, write_job
from turbochickens.audit import AuditLog
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

REPO_ROOT = Path(__file__).resolve().parent.parent

ATT_SQL = (
    "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?"
)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    # budget 1: one vibe-cli chicken (weight 1.0) fills the whole budget, so a
    # second enqueued egg backlogs in the pecking order (queued) — the window
    # in which the egg on disk can be swapped between enqueue and hatch
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


def audit_rows_for(settings, run_id: str) -> list[dict]:
    return [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]


# --- negative: an egg swapped between enqueue and hatch fails closed ----------


def test_swapped_egg_fails_closed_at_hatch_with_attestation(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path

    # a slow chicken holds the whole budget (weight 1.0 of 1), so the second
    # egg waits in the pecking order — the swap window between enqueue and hatch
    slow = write_job(tmp_path, name="slow-egg", prompt="SLEEP 8 working")
    r1 = request(sock, "enqueue", timeout=10.0, spec_path=str(slow), spec_hash=spec_hash(load_spec(slow)))
    assert r1["ok"] is True
    id1 = r1["run_id"]
    wait_status(sock, id1, ("running",))

    # enqueue the victim egg with its hash pinned (exactly what the CLI sends)
    egg = write_job(tmp_path, name="swap-egg", prompt="Say hello")
    pinned = spec_hash(load_spec(egg))
    r2 = request(sock, "enqueue", timeout=10.0, spec_path=str(egg), spec_hash=pinned)
    assert r2["ok"] is True and r2["status"] == "queued"
    id2 = r2["run_id"]
    assert wait_status(sock, id2, ("queued",))["status"] == "queued"

    # swap the egg on disk: same shape (still candles, still a valid harness),
    # different content → a different spec hash under the stale pin
    original = egg.read_text(encoding="utf-8")
    swapped_toml = original.replace("temperature = 0.0", "temperature = 0.9")
    assert swapped_toml != original
    egg.write_text(swapped_toml, encoding="utf-8")
    swapped = spec_hash(load_spec(egg))
    assert swapped != pinned

    # free the budget → the swapped egg is admitted and hatched (re-verified)
    resp = request(sock, "run_stop", run_id=id1, timeout=15.0)
    assert resp["ok"] is True
    wait_terminal(sock, id1)
    run2 = wait_terminal(sock, id2)

    # terminal failed, pinned hash in the manifest, Fox refusal as the reason
    assert run2["status"] == "failed"
    manifest = run2["manifest"]
    assert manifest["spec_hash"] == f"sha256:{pinned}"
    reason = manifest["status_reason"]
    assert "fox refuses" in reason and "hash mismatch" in reason
    assert f"sha256:{pinned}" in reason and f"sha256:{swapped}" in reason

    # nothing was sent to the feeder: the harness never started — no running
    # status, no harness-sourced run events, just the supervisor refusal
    logs = request(sock, "runs_logs", run_id=id2, timeout=2.0)
    events = logs["events"]
    assert all(e["source"] != "harness" for e in events)
    assert all("status → running" not in e["line"] for e in events)
    assert any(e["source"] == "supervisor" and "hash mismatch" in e["line"] for e in events)

    # abattoir attestation (1,1,1), no clutch in the nest box, workspace gone
    assert db_row(settings, ATT_SQL, (id2,)) == (1, 1, 1)
    assert db_row(settings, "SELECT COUNT(*) FROM results WHERE run_id = ?", (id2,)) == (0,)
    assert not (settings.data_dir / "workspaces" / id2).exists()

    # Headcount: egg.queued → egg.hatched → abattoir.attested → chicken.finished
    rows = audit_rows_for(settings, id2)
    assert [r["event"] for r in rows] == ["egg.queued", "egg.hatched", "abattoir.attested", "chicken.finished"]
    att = next(r for r in rows if r["event"] == "abattoir.attested")
    assert (att["process_reaped"], att["workspace_removed"], att["env_cleared"]) == (True, True, True)
    fin = rows[-1]
    assert fin["status"] == "failed"
    assert "hash mismatch" in fin["reason"] and f"sha256:{swapped}" in fin["reason"]


# --- positive: an unmodified egg hatches normally under its pinned hash --------


def test_unmodified_egg_hatches_normally_under_its_pinned_hash(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    egg = write_job(tmp_path, name="honest-egg", prompt="Say hello")
    pinned = spec_hash(load_spec(egg))
    r = request(sock, "enqueue", timeout=10.0, spec_path=str(egg), spec_hash=pinned)
    assert r["ok"] is True and r["status"] == "queued" and r["run_id"]
    run_id = r["run_id"]

    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"
    assert run["manifest"]["spec_hash"] == f"sha256:{pinned}"
    assert run["manifest"]["status_reason"] == "ok"
    assert db_row(settings, ATT_SQL, (run_id,)) == (1, 1, 1)
    assert db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_id,)) == (1,)

    # the harness really ran under the pinned hash
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)
    assert any(e["source"] == "harness" and "fake harness log" in e["line"] for e in logs["events"])

    rows = audit_rows_for(settings, run_id)
    events = [r["event"] for r in rows]
    assert events[0] == "egg.queued" and "egg.hatched" in events and events[-1] == "chicken.finished"
    assert rows[-1]["status"] == "succeeded"


def test_shipped_example_spec_hash_unchanged() -> None:
    # the re-verification must not alter hashed spec fields — the shipped
    # example still candles to the pinned sha256:18efbd8c…80b959
    spec = load_spec(REPO_ROOT / "examples" / "weekly-incidents-digest" / "job.toml")
    assert spec_hash(spec) == "18efbd8c7222a161fa1f0ae953295f4d22d6763413a21754c82b87e32580b959"
