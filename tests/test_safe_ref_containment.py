"""Tests for spec 32 ticket 01 (review finding G1 core): the shared safe-ref
containment idiom — the Fence (`fence.py`) — applied by the Egg spec loader at
Candling for `prompt_file`, non-nestbox `attachments`, and `schema_file`, plus
the defensive re-check in the Hatchery before any file is copied or read at
provisioning.

Seams asserted at the highest existing level:
- pure: `fence.safe_ref` / `fence.resolve_contained` (the one idiom, also
  behind the Beak's path lock);
- Candling: `load_spec` refuses escaping refs with field-named Fox errors;
- socket ops: `enqueue` and `jobs_upload` refuse the same eggs — nothing
  queued, nothing stored;
- e2e: a host-file exfiltration attempt fails closed end to end — nothing
  copied into the chicken workspace, nothing shipped to the Feeder (the fake
  provider records zero requests) — while a legitimate in-directory egg still
  candles and hatches;
- invariant: the shipped example's spec hash stays `sha256:18efbd8c…80b959`.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_api_adapter import FakeMistralHandler
from test_coop import write_settings
from test_daemon import DaemonThread
from test_run_e2e import FAKE_VIBE, combined_output, db_row, run_id_of, wait_terminal, write_job
from turbochickens import runner as runner_mod
from turbochickens.adapter import RunContext
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.fence import UnsafeRefError, contained_ref, resolve_contained, safe_ref
from turbochickens.runner import RunPipeline
from turbochickens.settings import load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.spec.model import JobSpec

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent

HOST_SECRET_MARKER = "HOST-SECRET-MARKER-7f3d9"


# --- helpers -----------------------------------------------------------------


def write_egg(
    tmp_path: Path,
    *,
    name: str = "egg",
    prompt_file: str = "prompts/hello.txt",
    attachments: list[str] | None = None,
    schema_file: str = "schemas/out.schema.json",
    adapter: str = "vibe-cli",
    model: str = "fake-model",
) -> Path:
    """A minimal egg directory; returns the job.toml path. Ref fields are
    JSON-escaped into the TOML so hostile refs (backslashes etc.) survive."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text("Say hello")
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    atts = attachments if attachments is not None else []
    att_line = "[" + ", ".join(json.dumps(a) for a in atts) + "]"
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "containment test egg"

[trigger]
type = "manual"

[harness]
adapter = "{adapter}"
model = "{model}"
temperature = 0.0

[resources]
timeout_sec = 30
max_context_mb = 2
network = "none"

[input]
prompt_file = {json.dumps(prompt_file)}
attachments = {att_line}

[output]
format = "json"
schema_file = {json.dumps(schema_file)}
"""
    )
    return d / "job.toml"


def write_host_secret(tmp_path: Path) -> Path:
    """A 'host file' outside every egg dir — the exfiltration target."""
    p = tmp_path / "host-secret.txt"
    p.write_text(HOST_SECRET_MARKER)
    return p


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = write_settings(tmp_path)  # plain daemon, coop disabled
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


@pytest.fixture
def fake_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMistralHandler)
    server.requests = []
    server.counters = {}
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


@pytest.fixture
def api_daemon(tmp_path, monkeypatch, fake_api):
    """Daemon whose feeder is a hermetic loopback fake that records every
    request body it receives."""
    port = fake_api.server_address[1]
    providers = tmp_path / "providers.toml"
    providers.write_text(
        f"""
[providers.mistral]
base_url = "http://127.0.0.1:{port}/v1"
api_key_env = "MISTRAL_API_KEY"

[providers.mistral.models.fake-model]
context_window = 8192
input_usd_per_mtok = 1.0
output_usd_per_mtok = 2.0
json_mode = true
"""
    )
    monkeypatch.setenv("TURBOCHICKENS_PROVIDERS", str(providers))
    monkeypatch.setenv("MISTRAL_API_KEY", "tc-test-key")
    f = write_settings(tmp_path)
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    settings = load_settings(f)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


# --- pure seam: the one Fence idiom -------------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["/etc/passwd", "/abs", "a\\b", "a\x00b", "", ".", "..", "a/../b", "a//b", "./a", "a/.", "a/..", " a", "a ", "\\"],
)
def test_safe_ref_rejects_escaping_or_malformed_refs(ref) -> None:
    assert safe_ref(ref) is None


@pytest.mark.parametrize("ref", ["a", "a/b", "prompts/hello.txt", "schemas/out.schema.json", "a-b_c.d/e"])
def test_safe_ref_accepts_plain_relative_refs(ref) -> None:
    assert safe_ref(ref) == ref


def test_resolve_contained_refuses_symlink_escape(tmp_path) -> None:
    base = tmp_path / "egg"
    base.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text(HOST_SECRET_MARKER)
    (base / "link.txt").symlink_to(secret)
    assert resolve_contained(base, "link.txt") is None
    assert resolve_contained(base, "../host-secret.txt") is None
    assert resolve_contained(base, "/etc/passwd") is None
    # a plain in-base ref resolves fine
    (base / "ok.txt").write_text("ok")
    assert resolve_contained(base, "ok.txt") == (base / "ok.txt").resolve()


def test_resolve_contained_allows_symlink_that_stays_inside(tmp_path) -> None:
    base = tmp_path / "egg"
    (base / "prompts").mkdir(parents=True)
    (base / "prompts" / "hello.txt").write_text("hi")
    (base / "prompts" / "alias.txt").symlink_to(base / "prompts" / "hello.txt")
    assert resolve_contained(base, "prompts/alias.txt") == (base / "prompts" / "hello.txt").resolve()


def test_beak_path_lock_delegates_to_the_fence() -> None:
    from turbochickens import beak

    assert beak.safe_relpath("../x") is None
    assert beak.safe_relpath("a/b.txt") == "a/b.txt"
    assert beak.resolve_locked(Path("/nonexistent-base-xyz"), "a/../b") is None


# --- Candling: parametrized refusals for all three ref fields ------------------

BAD_REFS = [
    "../host-secret.txt",
    "../../etc/passwd",
    "/etc/passwd",
    "a/../b",
    ".",
    "..",
    "a//b",
    "a\\b",
    "prompts/../../host-secret.txt",
    "./prompts/hello.txt",
    " ",
]

FIELD_LOCS = {
    "prompt_file": ("input", "prompt_file"),
    "attachments": ("input", "attachments"),
    "schema_file": ("output", "schema_file"),
}


@pytest.mark.parametrize("field", ["prompt_file", "attachments", "schema_file"])
@pytest.mark.parametrize("ref", BAD_REFS)
def test_candling_refuses_escaping_ref_with_field_named_fox_error(tmp_path, field, ref) -> None:
    kwargs = {field: [ref] if field == "attachments" else ref}
    spec_path = write_egg(tmp_path, name=f"egg-{field}-{abs(hash((field, ref)))}" .replace("-", "")[:40], **kwargs)
    with pytest.raises(SpecValidationError) as ei:
        load_spec(spec_path)
    loc = FIELD_LOCS[field]
    issues = [i for i in ei.value.issues if i.loc == loc]
    assert issues, f"no {loc} issue among: {[str(i) for i in ei.value.issues]}"
    msg = str(issues[0])
    assert msg.startswith(".".join(loc) + ":"), msg
    assert "the fox refuses" in msg and "fence" in msg, msg


def test_candling_refuses_empty_attachment_ref(tmp_path) -> None:
    spec_path = write_egg(tmp_path, name="egg-empty-att", attachments=[""])
    with pytest.raises(SpecValidationError) as ei:
        load_spec(spec_path)
    issues = [i for i in ei.value.issues if i.loc == ("input", "attachments")]
    assert issues and "the fox refuses" in str(issues[0])


@pytest.mark.parametrize("field", ["prompt_file", "attachments", "schema_file"])
def test_candling_refuses_symlink_escape_for_every_ref_field(tmp_path, field) -> None:
    secret = write_host_secret(tmp_path)
    name = f"egg-sym-{field}"
    spec_path = write_egg(tmp_path, name=name)
    egg_dir = spec_path.parent
    (egg_dir / "prompts" / "link.txt").symlink_to(secret)
    (egg_dir / "link-att.txt").symlink_to(secret)
    (egg_dir / "schemas" / "link.schema.json").symlink_to(secret)
    ref_by_field = {
        "prompt_file": "prompts/link.txt",
        "attachments": ["link-att.txt"],
        "schema_file": "schemas/link.schema.json",
    }
    # rewrite just the one ref under test, keep the others legitimate
    toml = spec_path.read_text(encoding="utf-8")
    if field == "prompt_file":
        toml = toml.replace('prompt_file = "prompts/hello.txt"', 'prompt_file = "prompts/link.txt"')
    elif field == "attachments":
        toml = toml.replace("attachments = []", 'attachments = ["link-att.txt"]')
    else:
        toml = toml.replace('schema_file = "schemas/out.schema.json"', 'schema_file = "schemas/link.schema.json"')
    spec_path.write_text(toml, encoding="utf-8")
    with pytest.raises(SpecValidationError) as ei:
        load_spec(spec_path)
    loc = FIELD_LOCS[field]
    issues = [i for i in ei.value.issues if i.loc == loc]
    assert issues and "the fox refuses" in str(issues[0]) and "resolves outside" in str(issues[0]), (
        f"{[str(i) for i in ei.value.issues]}"
    )


# --- positive Candling --------------------------------------------------------


def test_legit_in_directory_egg_candles(tmp_path) -> None:
    spec_path = write_egg(tmp_path, name="good-egg", attachments=["prompts/hello.txt"])
    spec = load_spec(spec_path)  # no error
    assert spec.input.prompt_file == "prompts/hello.txt"
    # a symlink that stays inside the egg dir is fine too
    (spec_path.parent / "prompts" / "alias.txt").symlink_to(spec_path.parent / "prompts" / "hello.txt")
    toml = spec_path.read_text(encoding="utf-8").replace(
        'prompt_file = "prompts/hello.txt"', 'prompt_file = "prompts/alias.txt"'
    )
    spec_path.write_text(toml, encoding="utf-8")
    assert load_spec(spec_path).input.prompt_file == "prompts/alias.txt"


def test_shipped_example_spec_hash_unchanged() -> None:
    spec = load_spec(REPO_ROOT / "examples" / "weekly-incidents-digest" / "job.toml")
    assert spec_hash(spec) == "18efbd8c7222a161fa1f0ae953295f4d22d6763413a21754c82b87e32580b959"


# --- socket op seams: enqueue + jobs_upload refuse, nothing queued/stored ------


@pytest.mark.parametrize("field", ["prompt_file", "attachments", "schema_file"])
def test_enqueue_refuses_escaping_ref_field_named_nothing_queued(daemon, tmp_path, field) -> None:
    write_host_secret(tmp_path)
    kwargs = {field: ["../host-secret.txt"] if field == "attachments" else "../host-secret.txt"}
    spec_path = write_egg(tmp_path, name=f"enq-{field}", **kwargs)
    sock = daemon.settings.socket_path
    resp = request(sock, "enqueue", spec_path=str(spec_path), trigger="manual", timeout=5.0)
    assert resp["ok"] is False
    dotted = ".".join(FIELD_LOCS[field])
    assert "egg failed candling" in resp["error"]
    assert f"{dotted}:" in resp["error"] and "the fox refuses" in resp["error"], resp["error"]
    # nothing was queued
    assert request(sock, "runs_list", timeout=2.0)["runs"] == []


def test_cli_jobs_validate_refuses_escaping_ref(daemon, tmp_path) -> None:
    write_host_secret(tmp_path)
    spec_path = write_egg(tmp_path, name="cli-egg", prompt_file="../host-secret.txt")
    result = runner.invoke(app, ["jobs", "validate", str(spec_path)])
    assert result.exit_code == 1
    out = combined_output(result)
    assert "error input.prompt_file:" in out and "the fox refuses" in out, out


def test_jobs_upload_bundle_refuses_escaping_schema_ref_nothing_stored(daemon, tmp_path) -> None:
    write_host_secret(tmp_path)
    spec_path = write_egg(tmp_path, name="up-egg", schema_file="../host-secret.txt")
    files = {
        str(p.relative_to(spec_path.parent)): base64.b64encode(p.read_bytes()).decode()
        for p in sorted(spec_path.parent.rglob("*"))
        if p.is_file()
    }
    sock = daemon.settings.socket_path
    resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    assert resp["ok"] is False and resp["valid"] is False
    fields = [e["field"] for e in resp["errors"]]
    assert "output.schema_file" in fields
    msg = next(e["message"] for e in resp["errors"] if e["field"] == "output.schema_file")
    assert "the fox refuses" in msg, msg
    # nothing was stored under uploads/
    uploads = daemon.settings.uploads_dir
    assert not uploads.exists() or not any(uploads.iterdir())


# --- Hatchery re-check: unit seam (nothing copied before the refusal) ---------


def _pipeline(tmp_path) -> RunPipeline:
    settings = load_settings(write_settings(tmp_path))
    db = sqlite3.connect(":memory:")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return RunPipeline(settings, db, audit)


def _spec_with_ref(prompt_file: str) -> JobSpec:
    """A JobSpec built straight through the model — bypassing the loader, so
    only the Hatchery re-check stands between the ref and the host file."""
    return JobSpec.model_validate(
        {
            "job": {"name": "x"},
            "trigger": {"type": "manual"},
            "harness": {"adapter": "vibe-cli", "model": "fake-model", "temperature": 0.0},
            "resources": {"timeout_sec": 30, "max_context_mb": 2},
            "input": {"prompt_file": prompt_file, "attachments": []},
            "output": {"format": "json", "schema_file": "schemas/out.schema.json"},
        }
    )


@pytest.mark.parametrize(
    "ref,field",
    [
        ("../host-secret.txt", "input.prompt_file"),
        ("/etc/passwd", "input.prompt_file"),
        ("a\\b", "input.prompt_file"),
    ],
)
def test_hatchery_recheck_refuses_before_any_copy(tmp_path, ref, field) -> None:
    write_host_secret(tmp_path)
    egg = write_egg(tmp_path, name="hatch-egg")
    ws = tmp_path / "ws" / "run-x"
    ctx = RunContext(
        run_id="run-x",
        job_id="hatch-egg",
        spec=_spec_with_ref(ref),
        spec_path=egg,
        spec_hash="0" * 64,
        trigger="manual",
        workspace=ws,
    )
    pipeline = _pipeline(tmp_path)
    with pytest.raises(UnsafeRefError) as ei:
        pipeline._provision(ctx)
    assert ei.value.field == field
    assert "the fox refuses" in str(ei.value)
    # fail closed: nothing was copied into the chicken workspace
    assert not (ws / "prompt.txt").exists()
    assert not ws.exists() or not any(ws.iterdir())


def test_hatchery_recheck_refuses_escaping_attachment_and_schema(tmp_path) -> None:
    write_host_secret(tmp_path)
    egg = write_egg(tmp_path, name="hatch-egg2")
    ws = tmp_path / "ws" / "run-y"
    spec = _spec_with_ref("prompts/hello.txt")
    spec.input.attachments = ["../host-secret.txt"]
    ctx = RunContext(
        run_id="run-y", job_id="hatch-egg2", spec=spec, spec_path=egg,
        spec_hash="0" * 64, trigger="manual", workspace=ws,
    )
    with pytest.raises(UnsafeRefError) as ei:
        _pipeline(tmp_path)._provision(ctx)
    assert ei.value.field == "input.attachments" and "the fox refuses" in str(ei.value)
    assert not (ws / "prompt.txt").exists()  # the prompt was not copied either

    spec2 = _spec_with_ref("prompts/hello.txt")
    spec2.output.schema_file = "../../host-secret.txt"
    ctx2 = RunContext(
        run_id="run-z", job_id="hatch-egg2", spec=spec2, spec_path=egg,
        spec_hash="0" * 64, trigger="manual", workspace=tmp_path / "ws" / "run-z",
    )
    with pytest.raises(UnsafeRefError) as ei2:
        _pipeline(tmp_path)._provision(ctx2)
    assert ei2.value.field == "output.schema_file" and "the fox refuses" in str(ei2.value)


def test_hatchery_recheck_allows_legit_egg(tmp_path) -> None:
    egg = write_egg(tmp_path, name="hatch-ok")
    ws = tmp_path / "ws" / "run-ok"
    ctx = RunContext(
        run_id="run-ok", job_id="hatch-ok", spec=load_spec(egg), spec_path=egg,
        spec_hash="0" * 64, trigger="manual", workspace=ws,
    )
    injected, chaining = _pipeline(tmp_path)._provision(ctx)
    assert (ws / "prompt.txt").read_text() == "Say hello"
    assert injected == len("Say hello") and chaining == []


# --- e2e: exfiltration attempt fails closed end to end -------------------------


def test_e2e_exfil_attempt_at_candling_fails_closed_feeder_silent(api_daemon, fake_api, tmp_path) -> None:
    """An egg referencing a host file (outside its own dir) is refused at
    Candling: nothing is queued, the run never starts, and the Feeder (fake
    provider) records zero requests — nothing is shipped to it."""
    write_host_secret(tmp_path)
    spec_path = write_egg(tmp_path, name="exfil", adapter="mistral-api", prompt_file="../host-secret.txt")
    sock = api_daemon.settings.socket_path

    resp = request(sock, "enqueue", spec_path=str(spec_path), trigger="manual", timeout=5.0)
    assert resp["ok"] is False
    assert "input.prompt_file:" in resp["error"] and "the fox refuses" in resp["error"], resp["error"]
    assert request(sock, "runs_list", timeout=2.0)["runs"] == []
    assert fake_api.requests == []  # the feeder never saw any prompt content

    # same via the CLI
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    out = combined_output(result)
    assert "input.prompt_file" in out and "the fox refuses" in out, out
    assert fake_api.requests == []


def test_e2e_exfil_swap_at_hatch_fails_closed_feeder_silent(api_daemon, fake_api, tmp_path, monkeypatch) -> None:
    """Defense in depth: even if an egg passes Candling and is queued, a spec
    whose ref escapes at hatch time (here simulated by the loader handing the
    Hatchery a ref that crosses the fence) fails closed at hatch — attested
    teardown, no clutch, no feeder request, secret never in any served
    payload. Spec 32 ticket 21 (G18): the swapped spec no longer candles to
    the enqueue-time pin, so the hatch-time hash re-verification refuses it
    first (the Fence re-check at provisioning remains the second layer for
    hash-preserving escapes, covered by the `test_hatchery_recheck_*` tests)."""
    secret = write_host_secret(tmp_path)
    spec_path = write_egg(tmp_path, name="swap", adapter="mistral-api")  # legit refs on disk
    sock = api_daemon.settings.socket_path

    real_load_spec = runner_mod.load_spec

    def swapped_load_spec(path):
        spec = real_load_spec(path)
        spec.input.prompt_file = "../host-secret.txt"  # crosses the fence
        return spec

    monkeypatch.setattr(runner_mod, "load_spec", swapped_load_spec)

    enq = request(sock, "enqueue", spec_path=str(spec_path), trigger="manual", timeout=5.0)
    assert enq["ok"] is True, enq  # the on-disk egg itself candles fine
    run_id = enq["run_id"]
    run = wait_terminal(sock, run_id)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert "fox refuses" in reason and "hash mismatch" in reason and "swapped egg" in reason, reason

    # nothing shipped to the feeder, nothing stored, teardown attested (1,1,1)
    assert fake_api.requests == []
    settings = api_daemon.settings
    assert db_row(settings, "SELECT * FROM results WHERE run_id = ?", (run_id,)) is None
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    # the host secret appears in no served payload: manifest, run events, audit
    manifest_json = db_row(settings, "SELECT manifest_json FROM runs WHERE id = ?", (run_id,))[0]
    assert HOST_SECRET_MARKER not in manifest_json
    events = request(sock, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert all(HOST_SECRET_MARKER not in e["line"] for e in events)
    assert not any(e["source"] == "harness" for e in events)  # the harness never ran
    audit_rows = AuditLog(settings.audit_log_path).read_all()
    assert any(r["event"] == "abattoir.attested" and r["run_id"] == run_id for r in audit_rows)
    assert all(HOST_SECRET_MARKER not in json.dumps(r) for r in audit_rows)
    # the workspace was removed by the abattoir
    assert not (settings.data_dir / "workspaces" / run_id).exists()


def test_e2e_legit_egg_candles_and_hatches(daemon, tmp_path) -> None:
    """Positive path: a legitimate in-directory egg still candles, hatches,
    and ships its own prompt to the harness."""
    spec_path = write_job(tmp_path, name="legit", prompt="Say hello legit")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    sock = daemon.settings.socket_path
    run = wait_terminal(sock, run_id)
    assert run["status"] == "succeeded"
    res = request(sock, "results_get", run_id=run_id, timeout=2.0)
    assert res["ok"] is True
    # the fake vibe echoes the prompt it was fed from the workspace
    assert "Say hello legit" in res["output"]["echo"]
    att = db_row(daemon.settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
