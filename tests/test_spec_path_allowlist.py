"""Tests for spec 32 ticket 02 (review finding G1 entry points): the
spec-path allowlist at enqueue / re-run / schedule, and bundle schema
membership in `jobs_upload`.

Seams asserted at the highest existing level:

- socket ops on a chicken-owned connection (a fake vibe probing the socket
  directly from inside its own process — the exact SO_PEERCRED class a
  ChickenSub's Farmhand uses): `enqueue`, `run_rerun`, and `schedule_add`
  with a non-allowlisted `spec_path` are refused with a field-named
  `spec_path` error, nothing is queued or scheduled, and the refusal is
  byte-identical for existing and nonexistent host files — `spec_path` is
  not a file-existence oracle;
- the Farmhand tool seam from inside a real chicken (spawn_subs): the
  attached Farmhand's `enqueue` outside the uploads area is refused and
  nothing is queued; an uploaded egg path works;
- `jobs_upload`: a bundle whose `schema_file` is not a bundle member is
  rejected (field-named, nothing stored, never registered); a complete
  bundle is stored under the uploads area and its path still enqueues,
  re-runs, and schedules;
- the operator trust boundary is unchanged (socket/HTTP access means
  operator — see docs/lexicon.md): operator enqueue of a local path still
  works;
- the shipped example's spec hash stays `sha256:18efbd8c…80b959`.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

from test_coop import write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, upload_egg, wait_terminal, write_job
from test_schedule import write_cron_job
from test_chickensubs import write_job_subs
from turbochickens.client import request
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec, spec_hash

REPO = Path(__file__).resolve().parent.parent

# A stand-in for the real `vibe` binary that talks to coopd's socket DIRECTLY
# from inside its own process (the chicken's pid — the exact peer credential
# the daemon reads, the same class as a ChickenSub's Farmhand connection).
# Prompt markers:
#   "PEERPROBE <socket_path>" followed by one scenario per line:
#     enq <path>     — enqueue by spec_path
#     sched <path>   — schedule_add by spec_path
#     rerun <run_id> — run_rerun
#   "FARMENQ <path>" — drive the attached Farmhand over stdio (spawn_subs)
#     and call its enqueue tool with the path
# Every response is collected into the run's output under "probe" /
# "farmhand_enq".
FAKE_VIBE_ALLOW = r'''#!__PY__
import asyncio, json, os, re, sys, time, tomllib
from pathlib import Path

args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)

def argval(name):
    return args[args.index(name) + 1] if name in args else None

prompt = Path(argval("--prompt-file")).read_text() if argval("--prompt-file") else ""
workdir = Path(argval("--workdir") or os.getcwd())
out_dir = argval("--output-dir")

m = re.search(r"SLEEP ([\d.]+)", prompt)
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
        if parts[0] == "enq":
            probe.append({"scenario": sc, "resp": call("enqueue", spec_path=parts[1])})
        elif parts[0] == "sched":
            probe.append({"scenario": sc, "resp": call("schedule_add", spec_path=parts[1])})
        elif parts[0] == "rerun":
            probe.append({"scenario": sc, "resp": call("run_rerun", run_id=parts[1])})
    out["probe"] = probe
    s.close()

m = re.search(r"FARMENQ (\S+)", prompt)
if m:
    async def drive():
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        cfg = tomllib.loads((workdir / ".vibe" / "config.toml").read_text())
        entry = cfg["mcp_servers"]["farmhand"]
        params = StdioServerParameters(
            command=entry["command"], args=list(entry.get("args", [])), env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("enqueue", {"spec_path": m.group(1)})
                texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                return json.loads(texts[0])
    out["farmhand_enq"] = asyncio.run(drive())

if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as fh:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34, "cost_usd": 0.001}}, fh)
print(json.dumps(out))
m = re.search(r"EXIT ([0-9]+)", prompt)
sys.exit(int(m.group(1)) if m else 0)
'''.replace("__PY__", sys.executable)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = write_settings(tmp_path)  # plain daemon, coop disabled
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-allow"
    bin_path.write_text(FAKE_VIBE_ALLOW)
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


def probe_report(socket_path, run_id: str) -> dict[str, dict]:
    """The direct-socket probe responses a chicken collected during its run,
    keyed by scenario line."""
    res = request(socket_path, "results_get", run_id=run_id, timeout=5.0)
    assert res.get("ok") is True, res
    out = res["output"]
    assert isinstance(out, dict) and "probe" in out, out
    return {p["scenario"]: p["resp"] for p in out["probe"]}


def bundle_of(spec: Path, *, skip: str | None = None) -> dict[str, str]:
    return {
        str(p.relative_to(spec.parent)): base64.b64encode(p.read_bytes()).decode()
        for p in sorted(spec.parent.rglob("*"))
        if p.is_file() and p.name != skip
    }


# --- unit seam: the allowlist itself -------------------------------------------


def test_spec_path_allowlisted_unit(settings_file, tmp_path) -> None:
    from turbochickens.daemon import Daemon
    from turbochickens.storage import init_db

    settings = load_settings(settings_file)
    d = Daemon(settings)
    d.db = init_db(tmp_path / "unit.db")
    uploads = settings.uploads_dir
    # inside the uploads area → allowlisted (existence is never probed)
    assert d._spec_path_allowlisted(uploads / "job-x" / "job.toml") is True
    assert d._spec_path_allowlisted(uploads / "missing" / "job.toml") is True
    # outside: host paths, escapes, unrelated dirs → refused
    assert d._spec_path_allowlisted(tmp_path / "elsewhere" / "job.toml") is False
    assert d._spec_path_allowlisted(Path("/etc/passwd")) is False
    assert d._spec_path_allowlisted(uploads / ".." / "escape" / "job.toml") is False
    assert d._spec_path_allowlisted(Path("/etc")) is False
    # an already-registered egg path (job_specs) is allowlisted
    reg = tmp_path / "reg" / "job.toml"
    d.db.execute(
        "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at, spec_path) VALUES (?,?,?,?,?,?)",
        ("reg-job", "reg-job", "0" * 64, "", "now", str(reg.resolve())),
    )
    d.db.commit()
    assert d._spec_path_allowlisted(reg) is True
    assert d._spec_path_allowlisted(tmp_path / "reg" / "other.toml") is False
    d.db.close()


# --- chicken-owned connection: enqueue allowlist --------------------------------


def test_chicken_enqueue_outside_uploads_refused_field_named_nothing_queued(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    host_egg = write_job(tmp_path, "host-egg", prompt="Say hello")  # exists, outside uploads
    secret = tmp_path / "host-secret.txt"
    secret.write_text("HOST-SECRET-MARKER")
    missing = tmp_path / "no-such-egg" / "job.toml"  # does not exist at all
    prompt = (
        f"PEERPROBE {sock}\n"
        f"enq {host_egg}\n"
        f"enq {secret}\n"
        f"enq {missing}\n"
    )
    prober = write_job(tmp_path, "prober", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    r_host = probes[f"enq {host_egg}"]
    r_secret = probes[f"enq {secret}"]
    r_missing = probes[f"enq {missing}"]
    for r in (r_host, r_secret, r_missing):
        assert r["ok"] is False
        assert r["errors"][0]["field"] == "spec_path"
        assert "the fox refuses" in r["error"]
        # G17: the refusal never echoes the caller's host path back
        assert str(host_egg) not in r["error"] and str(secret) not in r["error"]
    # the file-existence oracle is closed: byte-identical refusal whether
    # or not the host file exists
    assert r_host["error"] == r_missing["error"] == r_secret["error"]
    # nothing was queued for any of the refused paths
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    assert {r["job_id"] for r in rows} == {"prober"}
    events = request(sock, "audit_tail", limit=50, timeout=2.0)["events"]
    assert not [e for e in events if e.get("event") == "egg.queued" and e.get("job_id") != "prober"]


def test_chicken_enqueue_of_uploaded_and_registered_paths_ok(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    # positive 1: an uploads-area (Coop-uploaded) egg path is allowlisted
    egg = write_job(tmp_path, "up-egg", prompt="Say hello")
    egg_up = upload_egg(sock, egg)
    # positive 2: an already-registered local egg path (the operator queued
    # it once) is allowlisted too
    local = write_job(tmp_path, "local-egg", prompt="Say hello")
    local_run = request(sock, "enqueue", spec_path=str(local), timeout=5.0)["run_id"]
    wait_terminal(sock, local_run)
    prompt = f"PEERPROBE {sock}\nenq {egg_up}\nenq {local}\n"
    prober = write_job(tmp_path, "prober2", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    r_up = probes[f"enq {egg_up}"]
    assert r_up["ok"] is True and r_up["parent_run_id"] == run_id
    r_local = probes[f"enq {local}"]
    assert r_local["ok"] is True and r_local["parent_run_id"] == run_id
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    queued = [r for r in rows if r["job_id"] in ("up-egg", "local-egg") and r["parent_run_id"] == run_id]
    assert len(queued) == 2 and {r["job_id"] for r in queued} == {"up-egg", "local-egg"}


# --- chicken-owned connection: schedule_add + run_rerun allowlist ---------------


def test_chicken_schedule_add_and_rerun_allowlist(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    cron = write_cron_job(tmp_path, "cron-egg", schedule="0 0 29 2 *")  # local, never fires
    cron_up = upload_egg(sock, cron)
    egg = write_job(tmp_path, "rerun-egg", prompt="Say hello")
    egg_up = upload_egg(sock, egg)
    base_run = request(sock, "enqueue", spec_path=egg_up, timeout=5.0)["run_id"]
    wait_terminal(sock, base_run)
    prompt = (
        f"PEERPROBE {sock}\n"
        f"sched {cron}\n"  # local path, not registered → refused
        f"sched {cron_up}\n"  # uploads path → ok
        f"rerun {base_run}\n"  # stored (registered) path → ok
    )
    prober = write_job(tmp_path, "prober3", prompt=prompt)
    run_id = request(sock, "enqueue", spec_path=str(prober), timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"

    probes = probe_report(sock, run_id)
    r_bad = probes[f"sched {cron}"]
    assert r_bad["ok"] is False
    assert r_bad["errors"][0]["field"] == "spec_path"
    assert "the fox refuses" in r_bad["error"]
    r_ok = probes[f"sched {cron_up}"]
    assert r_ok["ok"] is True and r_ok["job_id"] == "cron-egg"
    r_rerun = probes[f"rerun {base_run}"]
    assert r_rerun["ok"] is True and r_rerun.get("rerun_of") == base_run
    # only the allowlisted path was scheduled
    scheds = request(sock, "schedule_list", timeout=2.0)["schedules"]
    assert len(scheds) == 1 and scheds[0]["job_id"] == "cron-egg"
    request(sock, "schedule_rm", job_id="cron-egg", timeout=5.0)


# --- the ChickenSub's Farmhand tool seam, from inside a real chicken ------------


def test_farmhand_enqueue_from_chicken_outside_uploads_refused_nothing_queued(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    host_egg = write_job(tmp_path, "fh-host-egg", prompt="Say hello")
    parent = write_job_subs(tmp_path, "fh-parent", f"FARMENQ {host_egg}", spawn_subs=True)
    run_id = request(sock, "enqueue", spec_path=str(parent), timeout=10.0)["run_id"]
    run = wait_terminal(sock, run_id, timeout=150)
    assert run["status"] == "succeeded"
    out = request(sock, "results_get", run_id=run_id, timeout=5.0)["output"]
    enq = out["farmhand_enq"]
    assert enq["ok"] is False
    assert enq["errors"][0]["field"] == "spec_path"
    assert "the fox refuses" in enq["error"]
    # nothing was queued for the host egg
    rows = request(sock, "runs_list", timeout=2.0)["runs"]
    assert not any(r["job_id"] == "fh-host-egg" for r in rows)

    # positive: the same Farmhand enqueue of an uploaded egg works, with the
    # peer-forced sub lineage
    egg = write_job(tmp_path, "fh-up-egg", prompt="Say hello")
    egg_up = upload_egg(sock, egg)
    parent2 = write_job_subs(tmp_path, "fh-parent2", f"FARMENQ {egg_up}", spawn_subs=True)
    run2 = request(sock, "enqueue", spec_path=str(parent2), timeout=10.0)["run_id"]
    r2 = wait_terminal(sock, run2, timeout=150)
    assert r2["status"] == "succeeded"
    out2 = request(sock, "results_get", run_id=run2, timeout=5.0)["output"]
    enq2 = out2["farmhand_enq"]
    assert enq2["ok"] is True and enq2["parent_run_id"] == run2 and enq2["weight"] == 1.0


# --- jobs_upload: bundle schema membership --------------------------------------


def test_upload_bundle_without_schema_member_rejected_nothing_stored(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, "no-schema-egg")
    resp = request(sock, "jobs_upload", files=bundle_of(spec, skip="out.schema.json"), timeout=10.0)
    assert resp["ok"] is False and resp["valid"] is False
    assert resp["errors"][0]["field"] == "output.schema_file"
    assert "not in the bundle" in resp["errors"][0]["message"]
    # nothing was stored under uploads/ (staging is cleaned up)
    uploads = daemon.settings.uploads_dir
    stored = [p for p in uploads.iterdir()] if uploads.exists() else []
    assert not [p for p in stored if not p.name.startswith(".staging-")]
    # never registered as an egg
    assert request(sock, "jobs_list", timeout=2.0)["jobs"] == []
    events = request(sock, "audit_tail", limit=10, timeout=2.0)["events"]
    rejected = [e for e in events if e.get("event") == "egg.rejected"]
    assert rejected and rejected[-1]["fields"] == ["output.schema_file"]


def test_upload_bundle_with_schema_stored_and_path_enqueues_reruns_schedules(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    spec = write_job(tmp_path, "full-egg")
    up = upload_egg(sock, spec)
    assert up.startswith(str(daemon.settings.uploads_dir))
    # enqueue by the stored (uploads) path → full run
    run_id = request(sock, "enqueue", spec_path=up, timeout=5.0)["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"
    assert request(sock, "results_get", run_id=run_id, timeout=5.0)["ok"] is True
    # re-run by the stored path
    rr = request(sock, "run_rerun", run_id=run_id, timeout=10.0)
    assert rr["ok"] is True and rr.get("rerun_of") == run_id
    wait_terminal(sock, rr["run_id"])
    # schedule by an uploaded cron egg path
    cron_up = upload_egg(sock, write_cron_job(tmp_path, "up-cron", schedule="0 0 29 2 *"))
    sa = request(sock, "schedule_add", spec_path=cron_up, timeout=5.0)
    assert sa["ok"] is True and sa["job_id"] == "up-cron"
    request(sock, "schedule_rm", job_id="up-cron", timeout=5.0)
    # the operator trust boundary is unchanged: a local path still enqueues
    # (socket/HTTP access means operator — docs/lexicon.md)
    local = write_job(tmp_path, "op-local-egg")
    r = request(sock, "enqueue", spec_path=str(local), timeout=5.0)
    assert r["ok"] is True and r["status"] == "queued"


# --- invariants ------------------------------------------------------------------


def test_example_spec_hash_unchanged() -> None:
    spec = load_spec(REPO / "examples" / "weekly-incidents-digest" / "job.toml")
    assert spec_hash(spec) == "18efbd8c7222a161fa1f0ae953295f4d22d6763413a21754c82b87e32580b959"
