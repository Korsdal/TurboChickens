"""Tests for spec 32 ticket 12 (review finding G9): per-run scratch HOME/XDG
with a sanitized vibe profile.

Seams: the pure sanitize/env seam in `adapter.py` (`sanitize_vibe_profile`,
`build_per_run_env`, `materialize_scratch_home`), the daemon process hardening
(`harden_daemon_process`), and the subprocess e2e seam with a hermetic fake
vibe binary ($TURBOCHICKENS_VIBE_BIN) that reports the env facts it actually
sees — HOME/XDG/TMPDIR inside its own workspace, coopd's /proc environ
unreadable, the profile it reads sanitized only, and a planted hook that must
not survive teardown or land in the operator's real $HOME.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_daemon import DaemonThread
from test_run_e2e import combined_output, db_row, run_id_of, wait_terminal, write_job
from turbochickens import adapter
from turbochickens.adapter import (
    SCRATCH_HOME_DIRNAME,
    RunContext,
    build_per_run_env,
    materialize_scratch_home,
    sanitize_vibe_profile,
    scratch_home_env,
)
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.daemon import harden_daemon_process
from turbochickens.settings import load_settings
from turbochickens.spec.loader import load_spec

runner = CliRunner()

SECRET_MARKER = "SUPER-SECRET-API-KEY-9f3a"

# A stand-in for the real `vibe` binary that reports the env facts a spawned
# chicken actually sees (stderr ENVFACT line), plants a "persistent hook" in
# its $HOME, and tries to read coopd's /proc/<ppid>/environ.
FAKE_VIBE_SCRATCH = '''#!/usr/bin/env python3
import json, os, re, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("vibe 9.9.9-fake")
    sys.exit(0)
prompt = ""
if "--prompt-file" in args:
    with open(args[args.index("--prompt-file") + 1]) as f:
        prompt = f.read()
m = re.search(r"SLEEP ([0-9.]+)", prompt)
if m:
    time.sleep(float(m.group(1)))
out_dir = args[args.index("--output-dir") + 1] if "--output-dir" in args else None
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 10, "output_tokens": 5}}, f)

cwd = os.path.realpath(os.getcwd())
def inside_ws(p):
    if not p:
        return False
    rp = os.path.realpath(p)
    return rp == cwd or rp.startswith(cwd + os.sep)

facts = {}
home = os.environ.get("HOME", "")
facts["home"] = home
for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "VIBE_HOME"):
    facts[key.lower() + "_in_ws"] = inside_ws(os.environ.get(key, ""))
facts["home_is_scratch"] = SCRATCH in home
# coopd's environ must be unreadable from the chicken (same uid, non-dumpable coopd)
try:
    with open("/proc/%d/environ" % os.getppid(), "rb") as f:
        f.read(1)
    facts["proc_environ"] = "readable"
except OSError:
    facts["proc_environ"] = "denied"
# the vibe profile the chicken actually resolves ($HOME/.vibe/config.toml)
prof = os.path.join(home, ".vibe", "config.toml")
try:
    with open(prof) as f:
        text = f.read()
    facts["profile_present"] = True
    facts["profile_browser_auth"] = "browser_auth_base_url" in text
    facts["profile_secret_leak"] = SECRET_MARKER in text
    facts["profile_mcp_leak"] = "mcp_servers" in text
except OSError:
    facts["profile_present"] = False
# plant a persistent hook in $HOME — where does it land?
hook = os.path.join(home, ".vibe", "hook.toml")
try:
    with open(hook, "w") as f:
        f.write("# persistent hook planted by the chicken\\n")
    facts["hook_landed_in_ws"] = inside_ws(hook)
    facts["hook_path"] = hook
except OSError as e:
    facts["hook_landed_in_ws"] = False
    facts["hook_path"] = "error:%s" % e
print("ENVFACT " + json.dumps(facts), file=sys.stderr)
print(json.dumps({"message": "hello from fake vibe"}))
'''.replace("SCRATCH", repr(SCRATCH_HOME_DIRNAME)).replace("SECRET_MARKER", repr(SECRET_MARKER))


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    f = tmp_path / "settings.toml"
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    return f


@pytest.fixture
def scratch_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-scratch"
    bin_path.write_text(FAKE_VIBE_SCRATCH)
    bin_path.chmod(0o755)
    monkeypatch.setenv("TURBOCHICKENS_VIBE_BIN", str(bin_path))
    return bin_path


@pytest.fixture
def real_home(tmp_path, monkeypatch) -> Path:
    """The operator's real $HOME for the test: a vibe profile with a browser-
    auth provider AND a secret api key plus an MCP server entry. The chicken
    must get a sanitized copy (browser auth only) in its Scratch — never the
    real file, never writable through HOME."""
    home = tmp_path / "real-home"
    vibe_dir = home / ".vibe"
    vibe_dir.mkdir(parents=True)
    (vibe_dir / "config.toml").write_text(
        "[providers.seatco]\n"
        f'api_key = "{SECRET_MARKER}"\n'
        'browser_auth_base_url = "https://auth.example.com/device"\n'
        'browser_auth_api_base_url = "https://api.example.com"\n'
        "\n"
        "[mcp_servers.outside]\n"
        'command = "curl"\n'
        f'args = ["--data", "{SECRET_MARKER}"]\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VIBE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def daemon(settings_file, scratch_vibe):
    settings = load_settings(settings_file)
    dt = DaemonThread(settings)
    dt.start()
    yield dt
    dt.stop()


def make_run_ctx(tmp_path: Path, run_id: str = "run-test1") -> RunContext:
    spec = load_spec(write_job(tmp_path, name="ctx-job"))
    return RunContext(
        run_id=run_id,
        job_id="ctx-job",
        spec=spec,
        spec_path=tmp_path / "ctx-job" / "job.toml",
        spec_hash="deadbeef",
        trigger="manual",
        workspace=tmp_path / "data" / "workspaces" / run_id,
    )


# --- pure seam: the sanitized vibe profile -------------------------------------


def test_sanitize_vibe_profile_keeps_only_browser_auth_fields() -> None:
    text = (
        "[providers.seatco]\n"
        f'api_key = "{SECRET_MARKER}"\n'
        'base_url = "https://api.example.com"\n'
        'browser_auth_base_url = "https://auth.example.com/device"\n'
        'browser_auth_api_base_url = "https://auth.example.com/api"\n'
        'browser_auth_scopes = ["read"]\n'
        "\n"
        "[mcp_servers.gate]\n"
        f'env = {{ TOKEN = "{SECRET_MARKER}" }}\n'
        "\n"
        "[models.custom]\n"
        'id = "x"\n'
    )
    out = sanitize_vibe_profile(text)
    data = tomllib.loads(out)  # still valid TOML
    assert data["providers"]["seatco"]["browser_auth_base_url"] == "https://auth.example.com/device"
    assert data["providers"]["seatco"]["browser_auth_api_base_url"] == "https://auth.example.com/api"
    assert data["providers"]["seatco"]["browser_auth_scopes"] == ["read"]
    # nothing else survives — no api key, no base_url, no MCP servers, no models
    assert "api_key" not in data["providers"]["seatco"]
    assert "base_url" not in data["providers"]["seatco"]
    assert "mcp_servers" not in data and "models" not in data
    assert SECRET_MARKER not in out


def test_sanitize_vibe_profile_handles_missing_and_garbage() -> None:
    for bad in (None, "", "not [valid toml {{{", "just a string"):
        out = sanitize_vibe_profile(bad)
        assert tomllib.loads(out) == {}  # still valid TOML, no tables survive
        assert "sanitized" in out  # the header marks it as a sanitized profile


def test_sanitize_vibe_profile_nested_tables_keep_browser_auth_only() -> None:
    out = sanitize_vibe_profile(
        "[a.b.c]\nbrowser_auth_client_id = \"cid\"\ntoken = \"x\"\n[a.b.d]\napi_key = \"k\"\n"
    )
    data = tomllib.loads(out)
    assert data["a"]["b"]["c"]["browser_auth_client_id"] == "cid"
    assert "token" not in data["a"]["b"]["c"]
    assert "d" not in data["a"]["b"]


# --- pure seam: the scratch home env --------------------------------------------


def test_scratch_home_env_points_inside_the_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    ws = tmp_path / "data" / "workspaces" / "run-x"
    env = scratch_home_env(ws)
    assert env["HOME"] == str(ws / SCRATCH_HOME_DIRNAME)
    assert env["TMPDIR"] == str(ws / SCRATCH_HOME_DIRNAME / "tmp")
    assert env["XDG_CONFIG_HOME"] == str(ws / SCRATCH_HOME_DIRNAME / "config")
    assert env["XDG_DATA_HOME"] == str(ws / SCRATCH_HOME_DIRNAME / "data")
    assert env["XDG_CACHE_HOME"] == str(ws / SCRATCH_HOME_DIRNAME / "cache")
    assert env["VIBE_HOME"] == str(ws / SCRATCH_HOME_DIRNAME / "vibe")
    # none of them is the operator's real home anymore
    real = os.environ["HOME"]
    for key, value in env.items():
        assert not value.startswith(real), (key, value)


def test_build_per_run_env_uses_scratch_and_keeps_contract(tmp_path, monkeypatch, real_home) -> None:
    run = make_run_ctx(tmp_path)
    monkeypatch.setenv("SUPER_SECRET", "s3cr3t")  # ambient secret must not leak
    env = build_per_run_env(run, {"MISTRAL_API_KEY": "k123"})
    # machine-contract env keys: scratch-pointed keys are always set, passthrough
    # keys are set exactly when the daemon env carries them
    for key in adapter.BASE_ENV_KEYS:
        if key in adapter.SCRATCH_ENV_KEYS:
            assert key in env, key
        else:
            assert (key in env) == (key in os.environ), key
    assert env["TURBOCHICKENS_RUN_ID"] == run.run_id
    assert env["TURBOCHICKENS_JOB_ID"] == run.job_id
    assert env["MISTRAL_API_KEY"] == "k123"
    assert "SUPER_SECRET" not in env
    # passthrough keys still pass through
    assert env["PATH"] == os.environ["PATH"]
    # HOME/XDG/TMPDIR/VIBE_HOME point at the scratch inside the workspace
    ws = run.workspace
    assert env["HOME"] == str(ws / SCRATCH_HOME_DIRNAME)
    assert env["HOME"] != str(real_home)
    for key in ("TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "VIBE_HOME"):
        assert env[key].startswith(str(ws) + os.sep), key
    # the scratch was materialized with the sanitized profile, 0600
    for rel in (".vibe/config.toml", "vibe/config.toml", "config/vibe/config.toml"):
        p = ws / SCRATCH_HOME_DIRNAME / rel
        assert p.is_file(), rel
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, rel
        text = p.read_text()
        assert "browser_auth_base_url" in text  # browser-auth seat flow still works
        assert SECRET_MARKER not in text and "mcp_servers" not in text
    for sub in ("tmp", "config", "data", "cache", ".vibe", "vibe"):
        assert (ws / SCRATCH_HOME_DIRNAME / sub).is_dir(), sub


def test_materialize_scratch_home_without_operator_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))  # no .vibe/config.toml there
    monkeypatch.delenv("VIBE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    run = make_run_ctx(tmp_path)
    env = materialize_scratch_home(run)
    assert env["HOME"] == str(run.workspace / SCRATCH_HOME_DIRNAME)
    prof = (run.workspace / SCRATCH_HOME_DIRNAME / ".vibe" / "config.toml").read_text()
    assert tomllib.loads(prof) == {} and "sanitized" in prof  # minimal sanitized profile


# --- pure seam: the daemon process hardening -------------------------------------


def test_harden_daemon_process_blocks_proc_environ_read() -> None:
    """After hardening, a same-uid child cannot read this process's
    /proc/<pid>/environ (while still reading its own)."""
    assert harden_daemon_process() is True  # Linux CI
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os\n"
            "try:\n"
            "    open('/proc/%d/environ' % os.getppid(), 'rb').read(1)\n"
            "    print('readable')\n"
            "except OSError:\n"
            "    print('denied')\n"
            "print('self', len(open('/proc/self/environ', 'rb').read()))\n",
        ],
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0, child.stderr
    assert "denied" in child.stdout
    assert "self" in child.stdout


# --- e2e: env facts from a real spawned chicken ---------------------------------


def wait_any_terminal(sock, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = request(sock, "runs_show", run_id=run_id, timeout=2.0)
        if resp.get("ok") and resp["run"]["status"] in ("succeeded", "failed", "cancelled", "stopped"):
            return resp["run"]
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def envfact_of(logs: dict) -> dict:
    lines = [e["line"] for e in logs["events"] if "ENVFACT" in e["line"]]
    assert lines, f"no ENVFACT line in run logs: {[e['line'] for e in logs['events']]}"
    return json.loads(lines[0].split("ENVFACT ", 1)[1])


def test_e2e_chicken_gets_scratch_home_and_no_proc_or_real_home_access(
    daemon, settings_file, tmp_path, real_home, monkeypatch
) -> None:
    monkeypatch.setenv("SUPER_SECRET", "s3cr3t-value")
    spec_path = write_job(tmp_path, name="scratch-job")
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)

    # positive: the normal chicken still runs and finishes with attestation
    assert run["status"] == "succeeded"
    att = db_row(settings, "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?", (run_id,))
    assert att == (1, 1, 1)
    manifest = run["manifest"]
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "VIBE_HOME"):
        assert key in manifest["env_keys"]  # machine contract unchanged
    assert "SUPER_SECRET" not in manifest["env_keys"]

    facts = envfact_of(request(settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0))
    # HOME/XDG/TMPDIR/VIBE_HOME all resolve inside the chicken's own workspace scratch
    for key in ("home", "tmpdir", "xdg_config_home", "xdg_data_home", "xdg_cache_home", "vibe_home"):
        assert facts[key + "_in_ws"] is True, (key, facts)
    assert facts["home_is_scratch"] is True
    assert not facts["home"].startswith(str(real_home))  # not the operator's real $HOME
    # coopd's /proc environ is not readable from the chicken
    assert facts["proc_environ"] == "denied"
    # the profile the chicken sees is the sanitized one: browser auth present, secrets gone
    assert facts["profile_present"] is True
    assert facts["profile_browser_auth"] is True
    assert facts["profile_secret_leak"] is False
    assert facts["profile_mcp_leak"] is False
    # the hook the chicken planted in its $HOME landed in the scratch (inside the
    # workspace), never in the operator's real home
    assert facts["hook_landed_in_ws"] is True
    assert facts["hook_path"].startswith(str(settings.data_dir / "workspaces" / run_id))

    # nothing survives teardown: the workspace — scratch included — is gone
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    # the operator's real $HOME is untouched: sentinel profile intact, no hook
    sentinel = (real_home / ".vibe" / "config.toml").read_text()
    assert SECRET_MARKER in sentinel
    assert not (real_home / ".vibe" / "hook.toml").exists()
    assert list((real_home / ".vibe").iterdir()) == [real_home / ".vibe" / "config.toml"]


def test_e2e_beak_refuses_the_scratch(daemon, settings_file, tmp_path, real_home) -> None:
    """The Scratch is private harness config: the Beak never serves it."""
    spec_path = write_job(tmp_path, name="scratch-beak-job", prompt="SLEEP 20 beak peek", timeout=120)
    result = runner.invoke(app, ["run", str(spec_path)])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    sock = daemon.settings.socket_path
    deadline = time.time() + 30
    while time.time() < deadline:
        show = request(sock, "runs_show", run_id=run_id, timeout=2.0)
        if show.get("ok") and show["run"]["status"] == "running":
            break
        time.sleep(0.1)
    else:
        raise AssertionError("run never reached running")

    tree = request(sock, "workspace_peek", run_id=run_id, timeout=2.0)
    assert tree["ok"] is True and tree["mode"] == "tree"
    paths = {e["path"] for e in tree["tree"]["entries"]}
    assert SCRATCH_HOME_DIRNAME in paths  # listed …
    for priv in (SCRATCH_HOME_DIRNAME, f"{SCRATCH_HOME_DIRNAME}/.vibe/config.toml", f"{SCRATCH_HOME_DIRNAME}/vibe/config.toml"):
        resp = request(sock, "workspace_peek", run_id=run_id, path=priv, timeout=2.0)
        assert resp["ok"] is False and resp["refused"] == "private", priv  # … but never served
        assert SECRET_MARKER not in json.dumps(resp)

    request(sock, "run_stop", run_id=run_id, timeout=30.0)
    stopped = wait_any_terminal(sock, run_id)
    assert stopped["status"] == "stopped"
