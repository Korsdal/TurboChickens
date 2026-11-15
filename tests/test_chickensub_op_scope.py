"""Tests for spec 32 ticket 11 (review finding G8, scope half): the ChickenSub
op scope is enforced server-side on chicken-owned connections.

Seam: a real daemon on a temp socket (thread daemon, the DaemonThread pattern)
driven over the JSON-lines socket, plus a fake vibe binary
($TURBOCHICKENS_VIBE_BIN) that connects *directly* to the coopd socket from
inside its own process — the daemon sees the connection's peer pid as the
running chicken's pid (SO_PEERCRED, ticket 10), so the connection is
chicken-owned, exactly like a real chicken (or a process it spawned) talking
to coopd.

Covered (acceptance criteria of the ticket):
- a chicken-owned connection is refused for every operator-only op — shutdown,
  settings, gate (connectors_*), auth, upload (jobs_upload), re-run, schedule,
  plus retention/farmhand-info/unknown ops — each refusal field-named (the
  field names the refused op) with the Fox copy ("a chicken is not an
  operator"), and the daemon/ops it tried to reach are untouched;
- the allowed sub-chicken ops (enqueue + the read ops + the guarded run_stop,
  plus ping/workspace_peek) still work over a chicken-owned connection;
- direct-socket attack: a chicken cannot enqueue without lineage (lineage is
  forced from the peer, forged parent_run_id ignored), cannot stop its
  ancestors (forged caller_run_id ignored), and cannot reach admin ops;
- operator connections (peer pid is not a chicken) keep the full op surface.

Assertions are external, observable behavior only: socket responses and the
runs store / settings / gate registry as seen through operator calls.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from test_daemon import DaemonThread
from test_run_e2e import upload_egg
from turbochickens.client import request
from turbochickens.runner import TERMINAL_STATES
from turbochickens.settings import load_settings

# A stand-in for the real `vibe` binary that can also talk to coopd's socket
# DIRECTLY from inside its own process (the chicken's pid — the exact peer
# credential the daemon reads). Prompt markers:
#   "SLEEP <s>" / "EXIT <n>" — as in test_run_e2e's fake vibe
#   "PEERPROBE <socket_path>" followed by one scenario per line:
#     <op>              — call the op with no payload
#     <op> <json-payload> — call the op with the JSON object as payload
#   The string "SELFRUN" inside a payload value is replaced by the chicken's
#   own run id (TURBOCHICKENS_RUN_ID in the per-run env).
# Every scenario's socket response is collected into the run's output under
# "probe" (keyed by the raw scenario line), so the test can assert on the
# daemon's actual answers.
FAKE_VIBE_SCOPE = r'''#!__PY__
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
    self_run = os.environ.get("TURBOCHICKENS_RUN_ID", "SELFRUN")
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
        parts = sc.split(" ", 1)
        op = parts[0]
        payload = json.loads(parts[1]) if len(parts) > 1 else {}
        payload = {k: (self_run if v == "SELFRUN" else v) for k, v in payload.items()}
        probe.append({"scenario": sc, "op": op, "resp": call(op, **payload)})
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


def write_job(base: Path, name: str, prompt: str, timeout: int = 120, cron: bool = False) -> Path:
    """A vibe-cli job spec like test_run_e2e.write_job (cron=True → a
    rooster-triggered egg instead of manual)."""
    d = base / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    trigger = 'type = "cron"\nschedule = "0 7 * * 1"' if cron else 'type = "manual"'
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "test job"

[trigger]
{trigger}

[harness]
adapter = "vibe-cli"
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


def scenario_line(op: str, payload: dict | None = None) -> str:
    """The prompt line for one probe scenario — must match what the fake vibe
    parses back into (op, payload)."""
    return op if not payload else f"{op} {json.dumps(payload)}"


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 6\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-scope"
    bin_path.write_text(FAKE_VIBE_SCOPE)
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
    keyed by the raw scenario line."""
    res = request(socket_path, "results_get", run_id=run_id, timeout=5.0)
    assert res.get("ok") is True, res
    out = res["output"]
    assert isinstance(out, dict) and "probe" in out, out
    return {p["scenario"]: p["resp"] for p in out["probe"]}


#: every operator-only op, with a representative payload — each must be
#: refused on a chicken-owned connection with a field-named error.
#: "{EGG}" in a payload is replaced by the egg path when the prompt is built.
ADMIN_OPS: list[tuple[str, dict | None]] = [
    ("shutdown", None),
    ("settings_get", None),
    ("settings_set", {"farmhand_transport": "stdio"}),
    ("connectors_list", None),
    ("connectors_add", {"name": "evil", "transport": "stdio", "command": "/bin/true"}),
    ("connectors_rm", {"name": "evil"}),
    ("auth_providers", None),
    ("auth_access", None),
    ("auth_login_start", {"provider": "seatco"}),
    ("auth_login_poll", {"provider": "seatco"}),
    ("auth_logout", {"provider": "seatco"}),
    ("jobs_upload", {"files": {"job.toml": "e30="}}),
    ("run_rerun", {"run_id": "run-nope"}),
    ("schedule_add", {"spec_path": "{EGG}"}),
    ("schedule_list", None),
    ("schedule_rm", {"job_id": "some-job"}),
    ("retention_sweep", None),
    ("farmhand_info", None),
    ("rm_rf_hosts", None),  # even an unknown op is refused, not dispatched
]


def admin_line(op: str, payload: dict | None, egg: Path) -> str:
    if payload is None:
        return op
    p = {k: (str(egg) if v == "{EGG}" else v) for k, v in payload.items()}
    return scenario_line(op, p)


# --- Refusal matrix: chicken-owned connection cannot reach admin ops ----------


def test_admin_ops_refused_on_chicken_owned_connection(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    egg = write_job(tmp_path, "matrix-egg", prompt="Say hello")
    lines = [admin_line(op, payload, egg) for op, payload in ADMIN_OPS]
    prompt = f"PEERPROBE {sock}\n" + "\n".join(lines) + "\n"
    prober = write_job(tmp_path, "matrix-prober", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    for op, payload in ADMIN_OPS:
        resp = probes[admin_line(op, payload, egg)]
        assert resp["ok"] is False, (op, resp)
        # field-named refusal: the field names the refused op
        assert resp["errors"][0]["field"] == op, (op, resp)
        assert "the fox refuses" in resp["error"], (op, resp)
        assert "not an operator" in resp["error"], (op, resp)
        assert run_id in resp["error"], (op, resp)  # names the owning chicken

    # the attack changed nothing: the daemon is still up and every one of
    # those ops still works — and is untouched — for the operator
    ping = request(sock, "ping", timeout=2.0)
    assert ping["ok"] is True  # the shutdown attempt did not land
    settings = request(sock, "settings_get", timeout=2.0)
    assert settings["ok"] is True
    assert settings["settings"]["farmhand_transport"] == "both"  # settings_set refused
    gates = request(sock, "connectors_list", timeout=2.0)
    assert gates["ok"] is True
    assert "evil" not in json.dumps(gates)  # connectors_add/rm refused
    scheds = request(sock, "schedule_list", timeout=2.0)
    assert scheds["ok"] is True and scheds["schedules"] == []  # schedule_* refused
    jobs = request(sock, "jobs_list", timeout=2.0)
    assert jobs["ok"] is True and all(j["job_id"] != "evil" for j in jobs["jobs"])
    # the only queued runs are the prober and the egg it never enqueued
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    assert {r["job_id"] for r in rows} == {"matrix-prober"}


# --- Positive: the sub-chicken op set still works over a chicken connection ---


def test_sub_chicken_ops_still_work_on_chicken_owned_connection(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    # a finished run the chicken can read through every read op
    done = write_job(tmp_path, "done-egg", prompt="Say hello")
    done_id = request(sock, "enqueue", spec_path=str(done), timeout=5.0)["run_id"]
    assert wait_terminal(sock, done_id)["status"] == "succeeded"
    # a running non-ancestor chicken the sub may stop (guarded run_stop)
    victim = write_job(tmp_path, "victim-egg", prompt="SLEEP 30")
    victim_id = request(sock, "enqueue", spec_path=str(victim), timeout=5.0)["run_id"]
    wait_status(sock, victim_id, {"running"})

    egg = write_job(tmp_path, "sub-egg", prompt="Say hello")
    # spec 32 (G1 allowlist): a chicken-owned connection may only queue egg
    # paths inside the coop uploads area (or already-registered egg paths),
    # so the probe enqueues the uploads-area copy of the egg
    egg_up = upload_egg(sock, egg)
    read_ops: list[tuple[str, dict | None]] = [
        ("ping", None),
        ("status", None),
        ("jobs_list", None),
        ("runs_list", None),
        ("runs_show", {"run_id": done_id}),
        ("runs_logs", {"run_id": done_id}),
        ("results_get", {"run_id": done_id}),
        ("stats_list", None),
        ("stats_show", {"job_id": "done-egg"}),
        ("audit_tail", {"limit": 5}),
        ("workspace_peek", {"run_id": "SELFRUN"}),
    ]
    extra: list[tuple[str, dict | None]] = [
        ("enqueue", {"spec_path": str(egg_up)}),
        ("enqueue", {"spec_path": str(egg_up), "parent_run_id": "run-forged"}),
        ("run_stop", {"run_id": victim_id}),
    ]
    lines = [scenario_line(op, p) for op, p in read_ops + extra]
    prompt = f"PEERPROBE {sock}\n" + "\n".join(lines) + "\n"
    prober = write_job(tmp_path, "positive-prober", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    # every read op answers ok over the chicken-owned connection
    for op, payload in read_ops:
        resp = probes[scenario_line(op, payload)]
        assert resp["ok"] is True, (op, resp)
    assert probes[scenario_line("results_get", {"run_id": done_id})]["output"]["message"] == "hello from fake vibe"
    peek = probes[scenario_line("workspace_peek", {"run_id": "SELFRUN"})]
    assert "workspace_gone" not in peek and ("tree" in peek or "entries" in peek or "files" in peek), peek

    # enqueue without a lineage field: lineage is forced from the peer — the
    # chicken cannot enqueue without lineage
    r1 = probes[scenario_line("enqueue", {"spec_path": str(egg_up)})]
    assert r1["ok"] is True and r1["parent_run_id"] == run_id
    # enqueue with a forged parent_run_id: ignored, still forced from the peer
    r2 = probes[scenario_line("enqueue", {"spec_path": str(egg_up), "parent_run_id": "run-forged"})]
    assert r2["ok"] is True and r2["parent_run_id"] == run_id
    assert "run-forged" not in json.dumps(r2)
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    subs = [r for r in rows if r["job_id"] == "sub-egg"]
    assert len(subs) == 2 and all(r["parent_run_id"] == run_id for r in subs)

    # the guarded run_stop works on a non-ancestor chicken
    stop = probes[scenario_line("run_stop", {"run_id": victim_id})]
    assert stop["ok"] is True and stop["status"] == "stopped", stop
    assert request(sock, "runs_show", run_id=victim_id, timeout=2.0)["run"]["status"] == "stopped"


# --- Direct-socket attack from a sub-chicken: ancestors + admin ops -----------


def test_sub_cannot_stop_ancestors_or_reach_admin_ops_over_direct_socket(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    parent = write_job(tmp_path, "attack-parent", prompt="SLEEP 30")
    parent_id = request(sock, "enqueue", spec_path=str(parent), timeout=5.0)["run_id"]
    wait_status(sock, parent_id, {"running"})

    egg = write_job(tmp_path, "attack-egg", prompt="Say hello")
    # G1 allowlist: a chicken-owned connection can only queue uploads-area
    # (or registered) egg paths, so the attack probes enqueue the uploaded copy
    egg_up = upload_egg(sock, egg)
    scenarios = [
        ("run_stop", {"run_id": parent_id}),  # no caller field — forced from peer
        ("run_stop", {"run_id": parent_id, "caller_run_id": "run-forged"}),
        ("shutdown", None),
        ("settings_set", {"farmhand_transport": "stdio"}),
        ("connectors_add", {"name": "evil", "transport": "stdio", "command": "/bin/true"}),
        ("auth_logout", {"provider": "seatco"}),
        ("jobs_upload", {"files": {"job.toml": "e30="}}),
        ("run_rerun", {"run_id": parent_id}),
        ("schedule_add", {"spec_path": str(egg)}),
        ("enqueue", {"spec_path": str(egg_up), "parent_run_id": "run-forged"}),
    ]
    lines = [scenario_line(op, p) for op, p in scenarios]
    prompt = f"PEERPROBE {sock}\n" + "\n".join(lines) + "\n"
    sub = write_job(tmp_path, "attack-sub", prompt=prompt)
    sr = request(sock, "enqueue", spec_path=str(sub), parent_run_id=parent_id, timeout=5.0)
    assert sr["ok"] is True
    sub_id = sr["run_id"]
    run = wait_terminal(sock, sub_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, sub_id)
    # cannot stop its ancestor — with or without a forged caller_run_id
    for payload in ({"run_id": parent_id}, {"run_id": parent_id, "caller_run_id": "run-forged"}):
        resp = probes[scenario_line("run_stop", payload)]
        assert resp["ok"] is False, resp
        assert "ancestor" in resp["error"], resp
        assert sub_id in resp["error"] and "run-forged" not in resp["error"], resp
    # cannot reach admin ops
    for op, payload in scenarios[2:9]:
        resp = probes[scenario_line(op, payload)]
        assert resp["ok"] is False and resp["errors"][0]["field"] == op, (op, resp)
        assert "not an operator" in resp["error"], (op, resp)
    # enqueue lineage still forced from the peer even with a forged parent
    resp = probes[scenario_line("enqueue", {"spec_path": str(egg_up), "parent_run_id": "run-forged"})]
    assert resp["ok"] is True and resp["parent_run_id"] == sub_id, resp

    # the parent is still running — none of the attacks landed
    assert request(sock, "runs_show", run_id=parent_id, timeout=2.0)["run"]["status"] == "running"
    assert request(sock, "ping", timeout=2.0)["ok"] is True
    # the operator can still stop the parent (positive control)
    stop = request(sock, "run_stop", run_id=parent_id, timeout=30.0)
    assert stop["ok"] is True and stop["status"] == "stopped"


# --- Operator connections keep the full op surface -----------------------------


def test_operator_connection_keeps_full_op_scope(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    done = write_job(tmp_path, "op-done-egg", prompt="Say hello")
    done_id = request(sock, "enqueue", spec_path=str(done), timeout=5.0)["run_id"]
    assert wait_terminal(sock, done_id)["status"] == "succeeded"

    # the test process's peer pid is not a registered chicken pid → operator:
    # every admin op answers without a scope refusal
    for op, payload in [
        ("settings_get", {}),
        ("connectors_list", {}),
        ("schedule_list", {}),
        ("auth_providers", {}),
        ("auth_access", {}),
        ("farmhand_info", {}),
        ("retention_sweep", {}),
        ("jobs_list", {}),
        ("run_rerun", {"run_id": done_id}),
        ("schedule_add", {"spec_path": str(write_job(tmp_path, "cron-egg", "hello", cron=True))}),
    ]:
        resp = request(sock, op, timeout=10.0, **payload)
        assert resp.get("ok") is True, (op, resp)
        assert "not an operator" not in resp.get("error", ""), (op, resp)
    rerun_id = request(sock, "run_rerun", run_id=done_id, timeout=10.0)["run_id"]
    assert wait_terminal(sock, rerun_id)["status"] == "succeeded"
    settings = request(sock, "settings_get", timeout=2.0)
    assert settings["settings"]["farmhand_transport"] == "both"
