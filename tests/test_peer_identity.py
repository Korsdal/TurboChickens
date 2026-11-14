"""Tests for spec 32 ticket 10 (review finding G8): socket caller identity via
peer credentials, Chicken pid recording, and peer-forced lineage.

Seams: a real daemon on a temp socket (thread daemon, the DaemonThread
pattern) driven over the JSON-lines socket, plus a fake vibe binary
($TURBOCHICKENS_VIBE_BIN) that connects *directly* to the coopd socket from
inside its own process — so the daemon sees the connection's peer pid as the
running chicken's pid, exactly like a real chicken (or a process it spawned)
talking to coopd.

Covered (acceptance criteria of the ticket):
- peer credentials are read on every socket connection and the caller is
  resolved server-side (observed through forced lineage below);
- each Chicken's pid is recorded at hatch/spawn and served (manifest harness
  block + runs_list row), matching the pid the chicken itself reports;
- `parent_run_id` / `caller_run_id` are forced from the authenticated peer —
  client-sent (forged) lineage fields are ignored on chicken-owned
  connections (negative tests with forged fields);
- the ChickenSub depth cap (1) and the ancestor-stop refusal hold even
  against a direct socket caller, while the legitimate paths (operator
  enqueue with lineage, operator run_stop, sub-egg enqueue from a chicken)
  still work;
- the trust model (socket/HTTP = operator; Chickens are not operators) is
  documented in docs/lexicon.md.

Assertions are external, observable behavior only: socket responses, the runs
store/manifest, the audit log, result payloads, and the doc file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from test_daemon import DaemonThread
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings

REPO = Path(__file__).resolve().parent.parent

# A stand-in for the real `vibe` binary that can also talk to coopd's socket
# DIRECTLY from inside its own process (the chicken's pid — the exact peer
# credential the daemon reads). Prompt markers:
#   "SLEEP <s>" / "EXIT <n>" — as in test_run_e2e's fake vibe
#   "PEERPROBE <socket_path>" followed by one scenario per line:
#     ping
#     enqueue <egg_path>                          — enqueue with no lineage field
#     enqueue-forge <egg_path> <forged_parent>    — enqueue with a forged parent_run_id
#     stop <run_id>                               — run_stop with no caller field
#     stop-forge <run_id> <forged_caller>         — run_stop with a forged caller_run_id
# Every scenario's socket response is collected into the run's output under
# "probe", so the test can assert on the daemon's actual answers.
FAKE_VIBE_PEER = r'''#!__PY__
import json, os, re, sys, time
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)

def argval(name):
    return args[args.index(name) + 1] if name in args else None

prompt = Path(argval("--prompt-file")).read_text() if argval("--prompt-file") else ""
out_dir = argval("--output-dir")

m = re.search(r"SLEEP ([0-9.]+)", prompt)
if m:
    time.sleep(float(m.group(1)))

out = {"pid": os.getpid(), "message": "hello from fake vibe", "echo": prompt.strip()[:40]}

m = re.search(r"PEERPROBE (\S+)", prompt)
if m:
    import socket as sk
    sock_path = m.group(1)
    scenarios = [ln.strip() for ln in prompt[m.end():].splitlines() if ln.strip()]
    s = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
    s.connect(sock_path)
    f = s.makefile("rwb")
    def call(op, **payload):
        f.write(json.dumps({"op": op, "protocol": 1, **payload}).encode() + b"\n")
        f.flush()
        return json.loads(f.readline().decode())
    probe = []
    for sc in scenarios:
        parts = sc.split()
        if parts[0] == "ping":
            probe.append({"scenario": sc, "resp": call("ping")})
        elif parts[0] == "enqueue":
            probe.append({"scenario": sc, "resp": call("enqueue", spec_path=parts[1])})
        elif parts[0] == "enqueue-forge":
            probe.append({"scenario": sc, "resp": call("enqueue", spec_path=parts[1], parent_run_id=parts[2])})
        elif parts[0] == "stop":
            probe.append({"scenario": sc, "resp": call("run_stop", run_id=parts[1])})
        elif parts[0] == "stop-forge":
            probe.append({"scenario": sc, "resp": call("run_stop", run_id=parts[1], caller_run_id=parts[2])})
    out["probe"] = probe
    s.close()

if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as fh:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, fh)
print(json.dumps(out))
m = re.search(r"EXIT ([0-9]+)", prompt)
sys.exit(int(m.group(1)) if m else 0)
'''.replace("__PY__", sys.executable)


def write_job(
    base: Path,
    name: str,
    prompt: str,
    spawn_subs: bool | None = None,
    timeout: int = 120,
) -> Path:
    """A vibe-cli job spec like test_run_e2e.write_job, plus an optional
    `spawn_subs` line in [harness] (None = key absent)."""
    d = base / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    spawn_line = "" if spawn_subs is None else f"spawn_subs = {'true' if spawn_subs else 'false'}\n"
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
{spawn_line}
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
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 6\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-peer"
    bin_path.write_text(FAKE_VIBE_PEER)
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


def wait_terminal(socket_path, run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in TERMINAL_STATES:
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def wait_status(socket_path, run_id: str, want, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(socket_path, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in want:
            return resp["run"]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {want} within {timeout}s")


def probe_report(socket_path, run_id: str) -> dict[str, dict]:
    """The direct-socket probe responses a chicken collected during its run,
    keyed by scenario line."""
    res = request(socket_path, "results_get", run_id=run_id, timeout=5.0)
    assert res.get("ok") is True, res
    out = res["output"]
    assert isinstance(out, dict) and "probe" in out, out
    return {p["scenario"]: p["resp"] for p in out["probe"]}


# --- Chicken pid recorded at hatch/spawn -------------------------------------


def test_chicken_pid_recorded_at_spawn_and_served(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, "pid-job", prompt="Say hello")
    run_id = request(sock, "enqueue", spec_path=str(spec), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    # the manifest's harness block carries the chicken's pid
    show = request(sock, "runs_show", run_id=run_id, timeout=2.0)
    pid = show["run"]["manifest"]["harness"]["pid"]
    assert isinstance(pid, int) and pid > 1

    # the chicken itself reported its os.getpid() in its clutch — same pid
    out = request(sock, "results_get", run_id=run_id, timeout=2.0)["output"]
    assert out["pid"] == pid

    # runs_list rows carry it too (parsed from the manifest, like auth_mode)
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    row = next(r for r in rows if r["id"] == run_id)
    assert row["pid"] == pid

    # a cancelled queued egg never hatched — its manifest harness block
    # carries pid=None, shape-stable. Fill the pecking order's budget with
    # six in-flight chickens first, so the next egg stays queued and the
    # stop lands on the queued egg (deterministic `cancelled`, never hatched).
    for i in range(6):
        request(sock, "enqueue", spec_path=str(write_job(tmp_path, f"filler-{i}", prompt="SLEEP 30", timeout=120)), timeout=5.0)
    deadline = time.time() + 30
    while time.time() < deadline:
        rows = request(sock, "runs_list", timeout=2.0)["runs"]
        running = [r for r in rows if r["status"] == "running" and r["job_id"].startswith("filler-")]
        if len(running) == 6:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("filler chickens did not reach running")
    spec2 = write_job(tmp_path, "cancel-me", prompt="Say hello")
    run2 = request(sock, "enqueue", spec_path=str(spec2), timeout=5.0)["run_id"]
    stop = request(sock, "run_stop", run_id=run2, timeout=10.0)
    assert stop["ok"] is True and stop["status"] == "cancelled"
    show2 = request(sock, "runs_show", run_id=run2, timeout=2.0)
    assert show2["run"]["manifest"]["harness"]["pid"] is None
    # clean up the fillers so the fixture teardown is quick
    for r in request(sock, "runs_list", timeout=2.0)["runs"]:
        if r["job_id"].startswith("filler-") and r["status"] in ("running", "queued"):
            request(sock, "run_stop", run_id=r["id"], timeout=30.0)


# --- Peer-forced lineage on enqueue (forged parent_run_id ignored) ------------


def test_enqueue_lineage_forced_from_peer_not_client(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    egg = write_job(tmp_path, "plain-egg", prompt="Say hello")
    subs_egg = write_job(tmp_path, "subs-egg", prompt="Say hello", spawn_subs=True)
    prompt = (
        f"PEERPROBE {sock}\n"
        f"enqueue {egg}\n"
        f"enqueue-forge {egg} run-forged\n"
        f"enqueue {subs_egg}\n"
    )
    prober = write_job(tmp_path, "prober", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    # enqueue with NO lineage field from a chicken → forced from the peer
    r1 = probes[f"enqueue {egg}"]
    assert r1["ok"] is True
    assert r1["parent_run_id"] == run_id
    # enqueue with a FORGED parent_run_id → ignored, forced from the peer
    r2 = probes[f"enqueue-forge {egg} run-forged"]
    assert r2["ok"] is True
    assert r2["parent_run_id"] == run_id
    assert r2["parent_run_id"] != "run-forged"
    # a spawn_subs egg from a non-sub chicken is allowed (depth cap = 1)
    r3 = probes[f"enqueue {subs_egg}"]
    assert r3["ok"] is True
    assert r3["parent_run_id"] == run_id

    # the queued sub-eggs are recorded with the forced lineage, never forged
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    subs = [r for r in rows if r["job_id"] in ("plain-egg", "subs-egg")]
    assert len(subs) == 3
    assert all(r["parent_run_id"] == run_id for r in subs)
    assert "run-forged" not in json.dumps(rows)

    # the Headcount rows carry the forced lineage too
    events = request(sock, "audit_tail", limit=100, timeout=2.0)["events"]
    queued = [
        e for e in events if e.get("event") == "egg.queued" and e.get("job_id") in ("plain-egg", "subs-egg")
    ]
    assert len(queued) == 3
    assert all(e.get("parent_run_id") == run_id for e in queued)
    assert "run-forged" not in json.dumps(events)

    # and the sub-chickens' own manifests record it (positive: legit path works)
    for r in subs:
        show = request(sock, "runs_show", run_id=r["id"], timeout=2.0)["run"]
        assert show["manifest"].get("parent_run_id") == run_id


# --- Depth cap (1) holds against a direct socket caller -----------------------


def test_depth_cap_holds_against_direct_socket_caller(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    parent = write_job(tmp_path, "depth-parent", prompt="Say hello")
    parent_id = request(sock, "enqueue", spec_path=str(parent), timeout=5.0)["run_id"]
    wait_terminal(sock, parent_id)

    subs_egg = write_job(tmp_path, "depth-egg", prompt="Say hello", spawn_subs=True)
    prompt = f"PEERPROBE {sock}\nenqueue-forge {subs_egg} run-forged\n"
    sub = write_job(tmp_path, "depth-sub", prompt=prompt)
    # the operator enqueues the sub with lineage (legit path still works)
    sr = request(sock, "enqueue", spec_path=str(sub), parent_run_id=parent_id, timeout=5.0)
    assert sr["ok"] is True and sr["parent_run_id"] == parent_id
    sub_id = sr["run_id"]
    run = wait_terminal(sock, sub_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, sub_id)
    resp = probes[f"enqueue-forge {subs_egg} run-forged"]
    # the sub-chicken (itself a sub) cannot queue a spawn_subs egg — the
    # refusal keys off the peer-forced parent (the sub itself), not the
    # forged client-sent value, and nothing is queued
    assert resp["ok"] is False
    assert resp["errors"][0]["field"] == "harness.spawn_subs"
    assert sub_id in resp["error"]
    assert "run-forged" not in resp["error"]
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    assert not any(r["job_id"] == "depth-egg" for r in rows)


# --- Ancestor-stop refusal holds against a direct socket caller ----------------


def test_ancestor_stop_refusal_holds_against_direct_socket_caller(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    parent = write_job(tmp_path, "ancestor-parent", prompt="SLEEP 30", timeout=120)
    parent_id = request(sock, "enqueue", spec_path=str(parent), timeout=5.0)["run_id"]
    wait_status(sock, parent_id, {"running"})

    prompt = f"PEERPROBE {sock}\nstop-forge {parent_id} run-forged\n"
    sub = write_job(tmp_path, "ancestor-sub", prompt=prompt)
    sr = request(sock, "enqueue", spec_path=str(sub), parent_run_id=parent_id, timeout=5.0)
    assert sr["ok"] is True
    sub_id = sr["run_id"]
    run = wait_terminal(sock, sub_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, sub_id)
    resp = probes[f"stop-forge {parent_id} run-forged"]
    # the forged caller_run_id is ignored; the peer-forced caller (the sub)
    # makes the parent its ancestor → refused
    assert resp["ok"] is False
    assert "ancestor" in resp["error"]
    assert sub_id in resp["error"]
    assert "run-forged" not in resp["error"]

    # the parent is still running — the forged stop did not land
    show = request(sock, "runs_show", run_id=parent_id, timeout=2.0)["run"]
    assert show["status"] == "running"

    # the operator can still stop the parent (positive: legit path works)
    stop = request(sock, "run_stop", run_id=parent_id, timeout=30.0)
    assert stop["ok"] is True
    assert stop["status"] == "stopped"


# --- A chicken-owned connection without lineage fields gets them forced -------


def test_chicken_cannot_enqueue_without_lineage(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    egg = write_job(tmp_path, "no-lineage-egg", prompt="Say hello")
    prompt = f"PEERPROBE {sock}\nenqueue {egg}\nping\n"
    prober = write_job(tmp_path, "no-lineage-prober", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    wait_terminal(sock, run_id)

    probes = probe_report(sock, run_id)
    # ping answers on the chicken-owned connection (identity read, not a refusal)
    assert probes["ping"]["ok"] is True
    # enqueue without any lineage field still gets the peer-forced lineage
    resp = probes[f"enqueue {egg}"]
    assert resp["ok"] is True
    assert resp["parent_run_id"] == run_id
    row = next(
        r for r in request(sock, "runs_list", timeout=2.0)["runs"] if r["job_id"] == "no-lineage-egg"
    )
    assert row["parent_run_id"] == run_id


# --- Trust model documented ----------------------------------------------------


def test_trust_model_documented_in_lexicon() -> None:
    text = (REPO / "docs" / "lexicon.md").read_text(encoding="utf-8")
    assert "## The Fence — trust model" in text
    assert "| **Operator** |" in text
    assert "SO_PEERCRED" in text
    assert "not an operator" in text
    # the statement of the model itself: socket/HTTP access means operator
    assert "socket" in text and "Operator" in text


# --- Operator connections are unaffected (peer pid is not a chicken) ----------


def test_operator_connection_lineage_still_honored(daemon, tmp_path) -> None:
    # a subprocess connects to the socket directly — its peer pid is not a
    # registered chicken pid, so it is an operator and client-sent lineage
    # is honored (unchanged spec-27 behavior for operator callers)
    sock = daemon.settings.socket_path
    parent = write_job(tmp_path, "op-parent", prompt="Say hello")
    parent_id = request(sock, "enqueue", spec_path=str(parent), timeout=5.0)["run_id"]
    wait_terminal(sock, parent_id)

    egg = write_job(tmp_path, "op-egg", prompt="Say hello")
    code = (
        "import json, socket, sys\n"
        f"s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.connect({str(sock)!r})\n"
        "f = s.makefile('rwb')\n"
        f"req = {{'op': 'enqueue', 'protocol': 1, 'spec_path': {str(egg)!r}, 'parent_run_id': {parent_id!r}}}\n"
        "f.write(json.dumps(req).encode() + b'\\n'); f.flush()\n"
        "print(f.readline().decode())\n"
    )
    import subprocess

    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    resp = json.loads(proc.stdout.strip().splitlines()[-1])
    assert resp["ok"] is True
    assert resp["parent_run_id"] == parent_id
    row = next(
        r for r in request(sock, "runs_list", timeout=2.0)["runs"] if r["job_id"] == "op-egg"
    )
    assert row["parent_run_id"] == parent_id
    # the daemon's pid registry is not exposed over any payload
    status = request(sock, "status", timeout=2.0)
    assert "chicken_pids" not in json.dumps(status)
