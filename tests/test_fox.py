"""Spec 32 / ticket 14 — review finding G11: all state created restrictive (the Fox).

The daemon establishes a restrictive umask (0o077) at start and sets explicit
modes on every named piece of state, so on a multi-user host other local users
cannot read Clutches, quarantine, the Headcount, the db, or a live Chicken's
resolved Gate secrets.

Seams (external, observable behavior only — on-disk modes, socket/CLI/HTTP
responses, store contents):
- the `fox` helpers (pure seam),
- the store seams: `init_db`, `AuditLog.append`, `connectors.save_registry`
  (registry tmp file 0600 at creation, before the atomic rename),
- a real daemon on a temp socket (thread daemon + subprocess daemon) asserting
  the modes of everything it created: data dir 0700, socket 0600 at bind,
  db 0600, Headcount 0600, workspaces 0700, injected Farmhand/Gate config
  0600, results/quarantine 0700 dirs + 0600 files, uploads 0700/0600,
  registry 0600,
- positive tests: the operator's own flows (perch login store, enqueue, run,
  read results, gates CRUD, upload+enqueue) are unaffected.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_chickensubs import wait_status, wait_terminal
from test_connectors import write_job_conn
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, write_job
from turbochickens import fox
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import DaemonUnreachable, request
from turbochickens.connectors import save_registry
from turbochickens.runner import TERMINAL_STATES
from turbochickens.seat import SeatSession, SeatStore
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash
from turbochickens.storage import init_db

runner = CliRunner()

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_SPEC = REPO / "examples" / "weekly-incidents-digest" / "job.toml"

GATE_SECRET = "gate-secret-value-32-do-not-leak"


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    monkeypatch.delenv("TURBOCHICKENS_CONNECTORS", raising=False)
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


def enqueue(sock, spec_path: Path) -> str:
    digest = spec_hash(load_spec(spec_path))
    resp = request(sock, "enqueue", timeout=5.0, spec_path=str(spec_path.resolve()), spec_hash=digest)
    assert resp["ok"], resp
    return resp["run_id"]


def wait_for_file(path: Path, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear within {timeout}s")


# --- pure seam: the Fox helpers --------------------------------------------------


def test_fox_restrictive_umask_and_helpers(tmp_path) -> None:
    # apply_restrictive_umask sets 0o077 and returns the previous umask
    old = fox.apply_restrictive_umask()
    try:
        assert os.umask(0o022) == fox.RESTRICTIVE_UMASK  # was 0o077 while applied
    finally:
        os.umask(old)
    # under the restrictive umask a plain create is already owner-only
    old = fox.apply_restrictive_umask()
    try:
        p = tmp_path / "plain.txt"
        p.write_text("x")
        assert mode_of(p) == 0o600
        d = tmp_path / "plaindir"
        d.mkdir()
        assert mode_of(d) == 0o700
    finally:
        os.umask(old)

    # write_text_0600 / write_bytes_0600: 0600 at creation even under the
    # default umask, and a pre-existing looser file is tightened
    os.umask(0o022)
    f = tmp_path / "a.txt"
    fox.write_text_0600(f, "hello")
    assert mode_of(f) == 0o600 and f.read_text() == "hello"
    f.chmod(0o644)
    fox.write_text_0600(f, "again")
    assert mode_of(f) == 0o600 and f.read_text() == "again"
    b = tmp_path / "b.bin"
    fox.write_bytes_0600(b, b"\x00\x01")
    assert mode_of(b) == 0o600 and b.read_bytes() == b"\x00\x01"

    # ensure_dir: 0700 at creation, pre-existing looser dir tightened
    d = tmp_path / "c"
    fox.ensure_dir(d)
    assert mode_of(d) == 0o700
    d.chmod(0o755)
    fox.ensure_dir(d)
    assert mode_of(d) == 0o700

    # open_append_0600: created 0600, pre-existing looser file tightened, append-only
    a = tmp_path / "append.log"
    fd = fox.open_append_0600(a)
    os.write(fd, b"one\n")
    os.close(fd)
    assert mode_of(a) == 0o600
    a.chmod(0o644)
    fd = fox.open_append_0600(a)
    os.write(fd, b"two\n")
    os.close(fd)
    assert mode_of(a) == 0o600
    assert a.read_text() == "one\ntwo\n"


# --- store seams: db, Headcount, registry tmp --------------------------------------


def test_init_db_creates_db_0600_and_tightens_looser(tmp_path) -> None:
    os.umask(0o022)
    db = tmp_path / "turbochickens.db"
    init_db(db)
    assert mode_of(db) == 0o600
    db.chmod(0o644)  # a db left looser by an older version
    init_db(db)
    assert mode_of(db) == 0o600


def test_audit_log_headcount_0600_no_window_and_tightens_looser(tmp_path) -> None:
    os.umask(0o022)
    log = AuditLog(tmp_path / "audit.jsonl")
    row = log.append({"event": "coopd.started", "pid": 1})
    assert row["event"] == "coopd.started"
    assert mode_of(tmp_path / "audit.jsonl") == 0o600  # created restrictive at creation
    # a pre-existing looser Headcount is tightened on the next append
    (tmp_path / "audit.jsonl").chmod(0o644)
    log.append({"event": "egg.queued"})
    assert mode_of(tmp_path / "audit.jsonl") == 0o600
    assert [r["event"] for r in log.read_all()] == ["coopd.started", "egg.queued"]


def test_save_registry_tmp_0600_at_creation_before_rename(tmp_path, monkeypatch) -> None:
    os.umask(0o022)
    dd = tmp_path / "data"
    dd.mkdir()
    raw = {"connectors": {"g": {"transport": "stdio", "command": "/bin/g", "env": {"T": GATE_SECRET}}}}
    seen: dict[str, int] = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["tmp_mode"] = mode_of(Path(src))  # the tmp file's mode at the moment of the rename
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    out = save_registry(None, raw, dd)
    # the tmp file was already 0600 when it was renamed — no umask window
    assert seen["tmp_mode"] == 0o600
    assert out == dd / "connectors.toml" and mode_of(out) == 0o600
    assert not list(dd.glob("*.tmp-*"))  # no tmp left behind
    # a pre-existing looser registry is tightened by the rewrite
    out.chmod(0o644)
    save_registry(None, raw, dd)
    assert mode_of(out) == 0o600


# --- daemon on a temp socket: every created state file's mode ----------------------


def test_daemon_core_state_modes_on_disk(daemon) -> None:
    s = daemon.settings
    assert mode_of(s.data_dir) == 0o700
    assert mode_of(s.socket_path) == 0o600  # the bound socket
    assert mode_of(s.db_path) == 0o600
    assert mode_of(s.audit_log_path) == 0o600  # coopd.started row already appended
    assert AuditLog(s.audit_log_path).read_all()[-1]["event"] == "coopd.started"


def test_daemon_umask_covers_any_state_created_while_it_runs(daemon, tmp_path) -> None:
    # while the daemon runs, its restrictive umask applies to anything the
    # process creates — owner-only from creation, no window on any state file
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    assert mode_of(probe) == 0o600
    d = tmp_path / "probedir"
    d.mkdir()
    assert mode_of(d) == 0o700


def test_workspace_and_injected_farmhand_gate_config_modes_on_live_chicken(daemon, tmp_path, monkeypatch) -> None:
    s = daemon.settings
    sock = s.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)
    assert request(
        sock, "connectors_add", timeout=5.0, name="toolx", transport="stdio",
        command="/bin/toolx", env={"GATE_TOKEN": "$TC_GATE_SECRET"},
    )["ok"]
    # a spawn_subs chicken with an opted-in gate gets BOTH injected configs
    spec = write_job_conn(tmp_path, "live-gated", prompt="SLEEP 30 live", connectors=["toolx"], spawn_subs=True)
    run_id = enqueue(sock, spec)
    wait_status(sock, run_id, ("running",))
    ws = s.data_dir / "workspaces" / run_id
    vibe_cfg = ws / ".vibe" / "config.toml"
    fh_settings = ws / ".tc-farmhand-settings.toml"
    wait_for_file(vibe_cfg)
    wait_for_file(fh_settings)
    # workspaces root + the per-run workspace are 0700
    assert mode_of(s.data_dir / "workspaces") == 0o700
    assert mode_of(ws) == 0o700
    # the injected harness config (resolved gate secrets live here) is 0600,
    # its .vibe dir 0700 — the Farmhand entry and the gate entry merged in one file
    assert mode_of(ws / ".vibe") == 0o700
    assert mode_of(vibe_cfg) == 0o600
    assert mode_of(fh_settings) == 0o600
    cfg_text = vibe_cfg.read_text()
    assert "mcp_servers.farmhand" in cfg_text and "mcp_servers.toolx" in cfg_text
    assert GATE_SECRET in cfg_text  # resolved at rest in the ephemeral workspace only
    # clean up: stop the chicken, attested terminal, workspace gone
    stopped = request(sock, "run_stop", run_id=run_id, timeout=30.0)
    assert stopped["ok"] and stopped["status"] == "stopped"
    wait_status(sock, run_id, TERMINAL_STATES, timeout=30.0)
    assert not ws.exists()


def test_results_and_quarantine_artifacts_restrictive(daemon, tmp_path) -> None:
    s = daemon.settings
    sock = s.socket_path
    # a valid clutch: results/<run>/ 0700, output.json 0600
    ok_spec = write_job(tmp_path, name="ok-job")
    run_id = enqueue(sock, ok_spec)
    run = wait_terminal(sock, run_id, timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")
    assert mode_of(s.results_dir) == 0o700
    artifacts = s.results_dir / run_id
    assert mode_of(artifacts) == 0o700
    assert mode_of(artifacts / "output.json") == 0o600
    out = request(sock, "results_get", run_id=run_id, timeout=5.0)
    assert out["ok"] is True and out["output"]["message"] == "hello from fake vibe"

    # a quarantined clutch: quarantine/<run>/ 0700, both files 0600
    bad_spec = write_job(tmp_path, name="bad-job")
    (bad_spec.parent / "schemas" / "out.schema.json").write_text('{"type": "object", "required": ["missing_field"]}')
    bad_id = enqueue(sock, bad_spec)
    run = wait_terminal(sock, bad_id, timeout=60)
    assert run["status"] == "failed"
    qdir = s.quarantine_dir / bad_id
    assert mode_of(s.quarantine_dir) == 0o700
    assert mode_of(qdir) == 0o700
    assert mode_of(qdir / "raw_output.txt") == 0o600
    assert mode_of(qdir / "schema_errors.json") == 0o600
    q = request(sock, "results_get", run_id=bad_id, timeout=5.0)
    assert q["ok"] is False and q["quarantined"] is True


def test_uploads_stored_bundle_restrictive(daemon, tmp_path) -> None:
    s = daemon.settings
    sock = s.socket_path
    spec = write_job(tmp_path, name="upload-job")
    root = spec.parent
    files = {
        str(p.relative_to(root)): base64.b64encode(p.read_bytes()).decode()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    assert resp["ok"] is True and resp["valid"] is True, resp
    # uploads root + the stored bundle dir are 0700, every bundle file 0600,
    # no staging dir left behind
    assert mode_of(s.uploads_dir) == 0o700
    target = s.uploads_dir / f"upload-job-{resp['spec_hash']}"
    assert mode_of(target) == 0o700
    assert not list(s.uploads_dir.glob(".staging-*"))
    for p in sorted(target.rglob("*")):
        if p.is_file():
            assert mode_of(p) == 0o600, p
        else:
            assert mode_of(p) == 0o700, p
    # positive: the stored bundle is enqueueable by its in-container path and runs
    run_id = enqueue(sock, Path(resp["spec_path"]))
    run = wait_terminal(sock, run_id, timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")
    assert request(sock, "results_get", run_id=run_id, timeout=5.0)["ok"] is True


# --- subprocess daemon: socket 0600 at bind, state modes in a real daemon process --


def test_subprocess_daemon_socket_and_state_modes(settings_file) -> None:
    settings = load_settings(settings_file)
    env = dict(os.environ)  # includes TURBOCHICKENS_SETTINGS from the fixture
    proc = subprocess.Popen(
        [sys.executable, "-m", "turbochickens", "daemon", "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 15
        observed: list[int] = []
        while time.time() < deadline:
            if settings.socket_path.exists():
                observed.append(mode_of(settings.socket_path))  # catch any window at bind
                try:
                    if request(settings.socket_path, "ping", timeout=1.0).get("ok"):
                        break
                except DaemonUnreachable:
                    pass
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"daemon process exited early rc={proc.returncode}: {out}")
            time.sleep(0.01)
        else:
            raise AssertionError("daemon subprocess did not start answering")
        # the socket was 0600 at bind (restrictive umask) in every observation,
        # and stays 0600 while bound
        assert observed and all(m == 0o600 for m in observed), observed
        assert mode_of(settings.socket_path) == 0o600
        assert mode_of(settings.data_dir) == 0o700
        assert mode_of(settings.db_path) == 0o600
        assert mode_of(settings.audit_log_path) == 0o600
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "coopd: up" in result.output
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        proc.wait(timeout=10)
        assert proc.returncode == 0
        assert not settings.socket_path.exists()  # unlinked on stop
        with pytest.raises(DaemonUnreachable):
            request(settings.socket_path, "ping", timeout=1.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# --- positive: the operator's own flows are unaffected ------------------------------


def test_operator_flows_unaffected(daemon, tmp_path, monkeypatch) -> None:
    s = daemon.settings
    sock = s.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", GATE_SECRET)

    # perch login store: save → 0600 file in a 0700 dir, load round-trips, delete works
    store = SeatStore(s.data_dir)
    session = SeatSession(provider="seatco", access_token="tok", refresh_token="ref", expires_at=time.time() + 3600)
    store.save(session)
    assert mode_of(s.seats_dir) == 0o700
    assert mode_of(s.seats_dir / "seatco.json") == 0o600
    loaded = store.load("seatco")
    assert loaded is not None and loaded.access_token == "tok" and not loaded.expired
    assert store.delete("seatco") is True

    # gates CRUD over the socket still works, registry stays 0600
    assert request(sock, "connectors_add", timeout=5.0, name="g1", transport="stdio", command="/bin/g1")["ok"]
    listed = request(sock, "connectors_list", timeout=5.0)
    assert [c["name"] for c in listed["connectors"]] == ["g1"]
    assert mode_of(s.data_dir / "connectors.toml") == 0o600
    assert request(sock, "connectors_rm", timeout=5.0, name="g1")["ok"]

    # enqueue → run → read results: socket and CLI both
    spec = write_job(tmp_path, name="flow-job")
    run_id = enqueue(sock, spec)
    run = wait_terminal(sock, run_id, timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")
    out = request(sock, "results_get", run_id=run_id, timeout=5.0)
    assert out["ok"] is True and out["output"]["message"] == "hello from fake vibe"
    result = runner.invoke(app, ["results", "get", run_id])
    assert result.exit_code == 0, result.output
    assert "hello from fake vibe" in result.output
    tail = request(sock, "audit_tail", limit=5, timeout=5.0)
    assert tail["ok"] and any(r["event"] == "abattoir.attested" for r in tail["events"])
    listing = request(sock, "runs_list", timeout=5.0)
    assert listing["ok"] and any(r["id"] == run_id and r["status"] == "succeeded" for r in listing["runs"])
    assert request(sock, "status", timeout=5.0)["alerts"] == []


def test_shipped_example_spec_hash_unchanged() -> None:
    h = spec_hash(load_spec(EXAMPLE_SPEC))
    assert h.startswith("18efbd8c") and h.endswith("80b959")
