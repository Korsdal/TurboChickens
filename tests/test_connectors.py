"""Tests for spec 28: the connectors registry (gates) — connectors.toml, the
`connectors_list`/`connectors_add`/`connectors_rm` socket ops, per-egg opt-in
via `harness.connectors`, and injection into the chicken's harness runtime MCP
config.

Seams: pure-function parser/spec-field tests, a real daemon on a temp socket
(thread daemon) driven over the JSON-lines socket + the `tc` CLI, and a fake
vibe binary ($TURBOCHICKENS_VIBE_BIN) that reads the run's vibe config
(`<workdir>/.vibe/config.toml`) and reports a sanitized view of the injected
`[mcp_servers.*]` entries (never env/header values — those are asserted via a
side copy of the config file, outside the workspace). Assertions are external,
observable behavior only: socket responses, CLI output, the registry file,
store/audit contents, status transitions, result payloads.

Covered: the parser + resolution order + strict schema (field-named errors),
non-secret list summaries, CRUD over the socket with audit rows
(`gate.registered`/`gate.removed`/`gate.rejected`), the `harness.connectors`
spec field + hash stability, enqueue refusals (unknown gate, non-vibe-cli
adapter), `auth_access` listing gate names, e2e injection into the vibe config
(stdio + https, merged with a Farmhand entry), fail-closed on unresolvable env
refs, and secrecy (no env/header secret material in any response, audit row,
manifest, run log, or result).
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from test_chickensubs import wait_terminal
from test_daemon import DaemonThread
from test_run_e2e import db_row
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.connectors import (
    ConnectorsError,
    entry_to_dict,
    load_connectors,
    mcp_config_entry,
    public_entry,
    read_registry_raw,
    resolve_connectors_path,
    save_registry,
    validate_connector_payload,
)
from turbochickens.settings import load_settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash

runner = CliRunner()

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_SPEC = REPO / "examples" / "weekly-incidents-digest" / "job.toml"

SECRET = "gate-secret-value-28-do-not-leak"

# A stand-in for the real `vibe` binary that also inspects the run's vibe
# config (the harness runtime MCP config the platform injects into):
#
# Prompt markers:
#   "GATES"            — report a sanitized view of <workdir>/.vibe/config.toml's
#                        mcp_servers (names, keys, command/args/url, env/header
#                        KEY NAMES only, plus whether values still look like
#                        unresolved "$refs") — never values
#   "DUMPCONFIG <path>"— copy the raw config file to <path> for the test to read
#   otherwise          — the plain fake-vibe behavior (hello output, usage
#                        export, SLEEP/EXIT markers)
FAKE_VIBE_GATES = r'''#!__PY__
import json, os, re, shutil, sys, time, tomllib
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

m_sleep = re.search(r"SLEEP ([\d.]+)", prompt)
if m_sleep:
    time.sleep(float(m_sleep.group(1)))

def sanitize(entry):
    out = {"keys": sorted(entry.keys())}
    for k in ("command", "args", "url", "transport"):
        if k in entry:
            out[k] = entry[k]
    for k, label in (("env", "env"), ("headers", "header")):
        if isinstance(entry.get(k), dict):
            out[label + "_keys"] = sorted(entry[k].keys())
            out[label + "_resolved"] = all(not str(v).startswith("$") for v in entry[k].values())
    return out

m_gates = re.search(r"\bGATES\b", prompt)
m_dump = re.search(r"DUMPCONFIG (\S+)", prompt)

cfg_path = workdir / ".vibe" / "config.toml"
if m_dump and cfg_path.exists():
    shutil.copyfile(cfg_path, m_dump.group(1))

if m_gates:
    if cfg_path.exists():
        cfg = tomllib.loads(cfg_path.read_text())
        servers = cfg.get("mcp_servers", {})
        out = {
            "message": "gate probe",
            "config_present": True,
            "mcp_servers": {name: sanitize(e) for name, e in servers.items()},
        }
    else:
        out = {"message": "gate probe", "config_present": False, "mcp_servers": {}}
else:
    out = {"message": "hello from fake vibe", "echo": prompt.strip()[:40]}

if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "export.json"), "w") as f:
        json.dump({"usage": {"input_tokens": 12, "output_tokens": 34}}, f)
print(json.dumps(out))
m_exit = re.search(r"EXIT (\d+)", prompt)
sys.exit(int(m_exit.group(1)) if m_exit else 0)
'''.replace("__PY__", sys.executable)


def write_job_conn(
    base: Path,
    name: str,
    prompt: str = "Say hello",
    connectors: list[str] | None = None,
    spawn_subs: bool = False,
    adapter: str = "vibe-cli",
    model: str = "fake-model",
    timeout: int = 120,
    job_name: str | None = None,
) -> Path:
    """A job spec like test_run_e2e.write_job, plus an optional
    `connectors = [...]` line in [harness] (None = key absent). `job_name`
    overrides the [job].name (for hash comparisons across dirs)."""
    d = base / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "hello.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    conn_line = "" if connectors is None else "connectors = [" + ", ".join(f'"{c}"' for c in connectors) + "]\n"
    spawn_line = "spawn_subs = true\n" if spawn_subs else ""
    (d / "job.toml").write_text(
        f"""
[job]
name = "{job_name or name}"
description = "test job"

[trigger]
type = "manual"

[harness]
adapter = "{adapter}"
model = "{model}"
temperature = 0.0
{spawn_line}{conn_line}
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
    f.write_text(f'data_dir = "{tmp_path / "data"}"\nweight_budget_override = 4\n')
    monkeypatch.setenv("TURBOCHICKENS_SETTINGS", str(f))
    monkeypatch.delenv("TURBOCHICKENS_CONNECTORS", raising=False)
    return f


@pytest.fixture
def fake_vibe(tmp_path, monkeypatch) -> Path:
    bin_path = tmp_path / "fake-vibe-gates"
    bin_path.write_text(FAKE_VIBE_GATES)
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


# --- pure seams: the parser, resolution order, strict schema -------------------


def test_parser_resolution_order_and_empty_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TURBOCHICKENS_CONNECTORS", raising=False)
    # no file anywhere → empty registry (no built-in gates)
    assert load_connectors(None, tmp_path / "nope").connectors == {}
    assert resolve_connectors_path(None, tmp_path / "nope") is None
    # <data_dir>/connectors.toml
    dd = tmp_path / "data"
    dd.mkdir()
    (dd / "connectors.toml").write_text('[connectors.dd]\ntransport = "stdio"\ncommand = "/dd"\n')
    assert resolve_connectors_path(None, dd) == dd / "connectors.toml"
    assert set(load_connectors(None, dd).connectors) == {"dd"}
    # $TURBOCHICKENS_CONNECTORS wins over <data_dir>
    envf = tmp_path / "env.toml"
    envf.write_text('[connectors.envc]\ntransport = "stdio"\ncommand = "/env"\n')
    monkeypatch.setenv("TURBOCHICKENS_CONNECTORS", str(envf))
    assert resolve_connectors_path(None, dd) == envf
    assert set(load_connectors(None, dd).connectors) == {"envc"}
    # explicit path wins over both; a missing explicit/env file is an error
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[connectors.ex]\ntransport = "https"\nurl = "https://x.example/mcp"\n')
    assert resolve_connectors_path(explicit, dd) == explicit
    assert set(load_connectors(explicit, dd).connectors) == {"ex"}
    with pytest.raises(ConnectorsError, match="not found"):
        resolve_connectors_path(tmp_path / "missing.toml", dd)
    monkeypatch.setenv("TURBOCHICKENS_CONNECTORS", str(tmp_path / "missing-env.toml"))
    with pytest.raises(ConnectorsError, match="TURBOCHICKENS_CONNECTORS"):
        resolve_connectors_path(None, dd)


def test_parser_strict_schema_field_named_errors(tmp_path) -> None:
    cases = [
        ('[connectors.a]\ntransport = "stdio"\n', "connectors.a.command"),  # stdio needs command
        ('[connectors.a]\ntransport = "https"\n', "connectors.a.url"),  # https needs url
        ('[connectors.a]\ntransport = "ftp"\nurl = "https://x"\n', "transport"),
        ('[connectors.a]\ntransport = "stdio"\ncommand = "c"\nbogus = 1\n', "bogus"),  # unknown key forbidden
        ('[connectors.a]\ntransport = "https"\nurl = "ftp://x"\n', "url"),  # http(s) only
        ('[bogus]\nx = 1\n', "bogus"),  # top-level unknown key forbidden
        ('[connectors.a]\ntransport = "stdio"\ncommand = ""\n', "command"),
    ]
    for i, (content, want) in enumerate(cases):
        p = tmp_path / f"e{i}.toml"
        p.write_text(content)
        with pytest.raises(ConnectorsError) as ei:
            load_connectors(p, tmp_path)
        assert want in str(ei.value), (content, str(ei.value))
    # malformed TOML
    p = tmp_path / "broken.toml"
    p.write_text("[connectors.a\n")
    with pytest.raises(ConnectorsError, match="cannot load connectors"):
        load_connectors(p, tmp_path)


def test_public_entry_never_exposes_env_values_headers_stay_refs(tmp_path) -> None:
    f = tmp_path / "connectors.toml"
    f.write_text(
        '[connectors.s]\ntransport = "stdio"\ncommand = "/bin/s"\nenv = { TOKEN = "literal-secret", LIT = "x" }\n'
        '[connectors.h]\ntransport = "https"\nurl = "https://h.example/mcp"\n'
        'headers = { Authorization = "Bearer $H_TOKEN" }\n'
    )
    cfg = load_connectors(f, tmp_path)
    ps = public_entry("s", cfg.connectors["s"])
    assert ps["env_keys"] == ["LIT", "TOKEN"]
    assert "literal-secret" not in json.dumps(ps)
    ph = public_entry("h", cfg.connectors["h"])
    assert ph["headers"] == {"Authorization": "Bearer $H_TOKEN"}  # refs stay refs
    assert json.dumps(public_entry("s", cfg.connectors["s"])) .count("literal-secret") == 0


def test_validate_connector_payload_field_named_errors() -> None:
    entry, errs = validate_connector_payload("ok", {"transport": "stdio", "command": "/bin/x", "args": ["--a"]})
    assert entry is not None and not errs
    assert entry_to_dict(entry) == {"transport": "stdio", "command": "/bin/x", "args": ["--a"]}
    entry, errs = validate_connector_payload("x", {"transport": "stdio"})
    assert entry is None and any(e["field"] == "connectors.x.command" for e in errs), errs
    entry, errs = validate_connector_payload("x", {"transport": "carrier-pigeon"})
    assert entry is None and errs
    entry, errs = validate_connector_payload("farmhand", {"transport": "stdio", "command": "/bin/x"})
    assert entry is None and "reserved" in errs[0]["message"]
    entry, errs = validate_connector_payload("bad name!", {"transport": "stdio", "command": "c"})
    assert entry is None and errs[0]["field"] == "connectors.name"


def test_save_registry_roundtrip_and_perms(tmp_path) -> None:
    dd = tmp_path / "data"
    dd.mkdir()
    entry, _ = validate_connector_payload("g", {"transport": "https", "url": "https://x.example/mcp", "headers": {"A": "$V"}})
    raw = {"connectors": {"g": entry_to_dict(entry)}}
    out = save_registry(None, raw, dd)
    assert out == dd / "connectors.toml"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600  # registry may hold literal secrets
    cfg = load_connectors(None, dd)
    assert set(cfg.connectors) == {"g"} and cfg.connectors["g"].url == "https://x.example/mcp"
    # rewrite keeps 0600 and round-trips
    raw2 = read_registry_raw(out)
    raw2["connectors"]["h"] = {"transport": "stdio", "command": "/bin/h"}
    save_registry(None, raw2, dd)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert set(load_connectors(None, dd).connectors) == {"g", "h"}


def test_mcp_config_entry_resolves_refs_and_fails_closed(tmp_path, monkeypatch) -> None:
    f = tmp_path / "connectors.toml"
    f.write_text(
        '[connectors.s]\ntransport = "stdio"\ncommand = "/bin/s"\nenv = { T = "$TC_GATE_SECRET", L = "debug" }\n'
        '[connectors.h]\ntransport = "https"\nurl = "https://h.example/mcp"\n'
        'headers = { Authorization = "Bearer $TC_GATE_SECRET" }\n'
    )
    cfg = load_connectors(f, tmp_path)
    monkeypatch.setenv("TC_GATE_SECRET", SECRET)
    es = mcp_config_entry("s", cfg.connectors["s"])
    assert es == {"command": "/bin/s", "args": [], "env": {"T": SECRET, "L": "debug"}}
    eh = mcp_config_entry("h", cfg.connectors["h"])
    assert eh == {"url": "https://h.example/mcp", "headers": {"Authorization": f"Bearer {SECRET}"}}
    # unresolvable ref → fail closed, message names the var (a ref), never a value
    monkeypatch.delenv("TC_MISSING_GATE_VAR", raising=False)
    f2 = tmp_path / "c2.toml"
    f2.write_text('[connectors.b]\ntransport = "https"\nurl = "https://b.example/mcp"\nheaders = { A = "$TC_MISSING_GATE_VAR" }\n')
    bcfg = load_connectors(f2, tmp_path)
    with pytest.raises(ConnectorsError) as ei:
        mcp_config_entry("b", bcfg.connectors["b"])
    assert "TC_MISSING_GATE_VAR" in str(ei.value)


# --- pure seams: the spec field + hash stability ---------------------------------


def test_connectors_spec_field_and_hash(tmp_path) -> None:
    a = load_spec(write_job_conn(tmp_path, "c-none", connectors=None, job_name="same"))
    b = load_spec(write_job_conn(tmp_path, "c-empty", connectors=[], job_name="same"))
    c = load_spec(write_job_conn(tmp_path, "c-two", connectors=["x", "y"], job_name="same"))
    assert a.harness.connectors == [] and b.harness.connectors == []
    assert c.harness.connectors == ["x", "y"]
    assert spec_hash(a) == spec_hash(b)  # connectors = [] stays out of the hash
    assert spec_hash(a) != spec_hash(c)
    # extra = "forbid" intact: a stray key in [harness] is a field-named error
    bad = write_job_conn(tmp_path, "c-typo", connectors=["x"])
    bad.write_text(bad.read_text().replace('connectors = ["x"]', 'connectorz = ["x"]'))
    with pytest.raises(SpecValidationError) as ei:
        load_spec(bad)
    assert ("harness", "connectorz") in [i.loc for i in ei.value.issues]
    # the shipped example's egg hash is unchanged
    h = spec_hash(load_spec(EXAMPLE_SPEC))
    assert h.startswith("18efbd8c") and h.endswith("80b959")


def test_cli_jobs_validate_with_connectors_and_example_hash(settings_file, tmp_path) -> None:
    # mistral-api so the model check resolves without a vibe probe
    spec = write_job_conn(tmp_path, "cli-conn", connectors=["x"], adapter="mistral-api", model="mistral-large-latest")
    result = runner.invoke(app, ["jobs", "validate", str(spec)])
    assert result.exit_code == 0, result.output
    assert "OK: egg 'cli-conn' candled" in result.output
    result = runner.invoke(app, ["jobs", "validate", str(EXAMPLE_SPEC)])
    assert result.exit_code == 0, result.output
    assert "sha256:18efbd8c" in result.output and "80b959" in result.output


# --- socket ops: CRUD over the registry, audit-logged, non-secret ----------------


def test_connectors_crud_over_socket(daemon, tmp_path, monkeypatch) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", SECRET)

    # empty registry, no file yet
    r = request(sock, "connectors_list", timeout=5.0)
    assert r["ok"] is True and r["connectors"] == [] and r["source"] is None

    # add a stdio gate with a secret env value (literal in the file, never served)
    r = request(
        sock,
        "connectors_add",
        timeout=5.0,
        name="toolx",
        transport="stdio",
        command="/bin/toolx",
        args=["--serve"],
        env={"GATE_TOKEN": SECRET, "LOG": "debug"},
    )
    assert r["ok"] is True and r["transport"] == "stdio" and r["name"] == "toolx"
    assert r["entry"]["command"] == "/bin/toolx" and r["entry"]["env_keys"] == ["GATE_TOKEN", "LOG"]
    assert SECRET not in json.dumps(r)
    reg_path = settings.data_dir / "connectors.toml"
    assert r["source"] == str(reg_path)
    assert stat.S_IMODE(reg_path.stat().st_mode) == 0o600
    on_disk = load_connectors(reg_path, settings.data_dir)
    assert on_disk.connectors["toolx"].env["GATE_TOKEN"] == SECRET  # at rest, operator-owned

    # list: non-secret summary only
    r = request(sock, "connectors_list", timeout=5.0)
    assert [c["name"] for c in r["connectors"]] == ["toolx"]
    c = r["connectors"][0]
    assert c == {
        "name": "toolx",
        "transport": "stdio",
        "command": "/bin/toolx",
        "args": ["--serve"],
        "env_keys": ["GATE_TOKEN", "LOG"],
    }
    assert SECRET not in json.dumps(r)

    # add an https gate with a header env ref — the ref stays a reference
    r = request(
        sock,
        "connectors_add",
        timeout=5.0,
        name="webx",
        transport="https",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer $TC_GATE_SECRET"},
    )
    assert r["ok"] is True and r["entry"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    assert SECRET not in json.dumps(r)
    r = request(sock, "connectors_list", timeout=5.0)
    by_name = {c["name"]: c for c in r["connectors"]}
    assert by_name["webx"]["transport"] == "https" and by_name["webx"]["url"] == "https://mcp.example.com/mcp"
    assert by_name["webx"]["headers"] == {"Authorization": "Bearer $TC_GATE_SECRET"}
    assert SECRET not in json.dumps(r)

    # re-adding replaces the entry
    r = request(sock, "connectors_add", timeout=5.0, name="toolx", transport="stdio", command="/bin/toolx2")
    assert r["ok"] is True and r["entry"]["command"] == "/bin/toolx2" and r["entry"]["args"] == []
    assert [c["name"] for c in request(sock, "connectors_list", timeout=5.0)["connectors"]] == ["toolx", "webx"]

    # invalid adds: field-named errors, nothing written, gate.rejected audited
    before = (settings.data_dir / "connectors.toml").read_text()
    for payload, want in [
        ({"name": "bad1", "transport": "stdio"}, "connectors.bad1.command"),
        ({"name": "bad2", "transport": "https"}, "connectors.bad2.url"),
        ({"name": "bad3", "transport": "carrier-pigeon"}, "transport"),
        ({"name": "farmhand", "transport": "stdio", "command": "/bin/x"}, "reserved"),
        ({"name": "bad name!", "transport": "stdio", "command": "/bin/x"}, "connectors.name"),
    ]:
        r = request(sock, "connectors_add", timeout=5.0, **payload)
        assert r["ok"] is False and r["valid"] is False, payload
        assert any(want in e["field"] or want in e["message"] for e in r["errors"]), (payload, r["errors"])
    assert (settings.data_dir / "connectors.toml").read_text() == before  # nothing written

    # rm: gone from list + file, audited; unknown rm is a clean error
    r = request(sock, "connectors_rm", timeout=5.0, name="webx")
    assert r["ok"] is True and r["name"] == "webx" and r["transport"] == "https"
    assert [c["name"] for c in request(sock, "connectors_list", timeout=5.0)["connectors"]] == ["toolx"]
    assert "webx" not in load_connectors(None, settings.data_dir).connectors
    r = request(sock, "connectors_rm", timeout=5.0, name="webx")
    assert r["ok"] is False and "unknown gate" in r["error"]
    r = request(sock, "connectors_rm", timeout=5.0)
    assert r["ok"] is False and "requires name" in r["error"]

    # audit rows: gate.registered / gate.removed / gate.rejected — name+transport+fields only
    rows = AuditLog(settings.audit_log_path).read_all()
    reg = [r for r in rows if r["event"] == "gate.registered"]
    assert [(r["name"], r["transport"]) for r in reg] == [("toolx", "stdio"), ("webx", "https"), ("toolx", "stdio")]
    removed = [r for r in rows if r["event"] == "gate.removed"]
    assert [(r["name"], r["transport"]) for r in removed] == [("webx", "https")]
    rejected = [r for r in rows if r["event"] == "gate.rejected"]
    # spec 32 ticket 24: rows carry the additive `via` actor (socket here)
    assert len(rejected) == 5 and all(set(r) <= {"event", "ts", "name", "fields", "via"} for r in rejected)
    assert all(r["via"] == "socket" for r in rejected)
    audit_text = settings.audit_log_path.read_text()
    assert SECRET not in audit_text
    assert "Bearer $TC_GATE_SECRET" not in audit_text  # header refs are not audited either


def test_connectors_env_var_source(daemon, tmp_path, monkeypatch) -> None:
    sock = daemon.settings.socket_path
    envf = tmp_path / "env-connectors.toml"
    envf.write_text('[connectors.envgate]\ntransport = "stdio"\ncommand = "/env-gate"\n')
    monkeypatch.setenv("TURBOCHICKENS_CONNECTORS", str(envf))
    r = request(sock, "connectors_list", timeout=5.0)
    assert r["ok"] is True and r["source"] == str(envf)
    assert [c["name"] for c in r["connectors"]] == ["envgate"] and r["connectors"][0]["command"] == "/env-gate"
    # a broken env-pointed file is a clean error, not a crash
    envf.write_text("[connectors.x\n")
    r = request(sock, "connectors_list", timeout=5.0)
    assert r["ok"] is False and "cannot load connectors" in r["error"]


def test_auth_access_lists_gate_names(daemon) -> None:
    sock = daemon.settings.socket_path
    r = request(sock, "auth_access", timeout=5.0)
    by_kind = {s["kind"]: s for s in r["access"]["scope"]}
    assert by_kind["connectors"]["declared"] == []  # nothing registered yet
    assert request(sock, "connectors_add", timeout=5.0, name="g1", transport="stdio", command="/bin/g1")["ok"]
    r = request(sock, "auth_access", timeout=5.0)
    by_kind = {s["kind"]: s for s in r["access"]["scope"]}
    assert by_kind["connectors"]["declared"] == ["g1"]  # names only
    assert all(by_kind[k]["declared"] is None for k in ("tools", "data", "mcp_servers"))


# --- enqueue: unknown gate names + adapter restriction ---------------------------


def test_enqueue_connector_refusals(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    assert request(sock, "connectors_add", timeout=5.0, name="x", transport="stdio", command="/bin/x")["ok"]
    depth = request(sock, "status", timeout=5.0)["queue_depth"]
    # unknown connector name → field-named harness.connectors refusal, nothing queued
    spec = write_job_conn(tmp_path, "unk", connectors=["nope"])
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is False and "unknown connector" in r["error"] and "'nope'" in r["error"]
    assert r["errors"][0]["field"] == "harness.connectors"
    assert request(sock, "status", timeout=5.0)["queue_depth"] == depth
    # connectors with a non-vibe-cli adapter → refused (no runtime to inject into)
    api_spec = write_job_conn(tmp_path, "api-conn", connectors=["x"], adapter="mistral-api", model="mistral-large-latest")
    r = request(sock, "enqueue", spec_path=str(api_spec), timeout=10.0)
    assert r["ok"] is False and "harness.connectors" in r["error"]
    assert r["errors"][0]["field"] == "harness.connectors"
    assert request(sock, "status", timeout=5.0)["queue_depth"] == depth


# --- e2e: gates injected into the chicken's harness runtime MCP config -----------


def test_e2e_gates_injected_into_vibe_config(daemon, tmp_path, monkeypatch) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    monkeypatch.setenv("TC_GATE_SECRET", SECRET)
    assert request(
        sock, "connectors_add", timeout=5.0, name="toolx", transport="stdio",
        command="/bin/toolx", args=["--serve"], env={"GATE_TOKEN": "$TC_GATE_SECRET", "LOG": "debug"},
    )["ok"]
    assert request(
        sock, "connectors_add", timeout=5.0, name="webx", transport="https",
        url="https://mcp.example.com/mcp", headers={"Authorization": "Bearer $TC_GATE_SECRET"},
    )["ok"]

    dump = tmp_path / "dumped-config.toml"
    spec = write_job_conn(tmp_path, "gated", prompt=f"GATES DUMPCONFIG {dump}", connectors=["toolx", "webx"])
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is True and r["weight"] == 1.0
    run = wait_terminal(sock, r["run_id"], timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")

    # the fake vibe saw both gates in its harness runtime MCP config — sanitized
    out = request(sock, "results_get", run_id=r["run_id"], timeout=5.0)
    assert out["ok"] is True
    probe = out["output"]
    assert probe["config_present"] is True
    servers = probe["mcp_servers"]
    assert set(servers) == {"toolx", "webx"}
    assert servers["toolx"]["command"] == "/bin/toolx" and servers["toolx"]["args"] == ["--serve"]
    assert servers["toolx"]["env_keys"] == ["GATE_TOKEN", "LOG"]
    assert servers["toolx"]["env_resolved"] is True  # $TC_GATE_SECRET was resolved at injection
    assert servers["webx"]["url"] == "https://mcp.example.com/mcp"
    assert servers["webx"]["header_keys"] == ["Authorization"]
    assert servers["webx"]["header_resolved"] is True

    # the raw config file (side copy taken by the fake vibe) carries the resolved
    # values — inside the ephemeral workspace only — and parses as TOML
    import tomllib

    raw_cfg = tomllib.loads(dump.read_text())
    assert raw_cfg["mcp_servers"]["toolx"]["env"]["GATE_TOKEN"] == SECRET
    assert raw_cfg["mcp_servers"]["webx"]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert raw_cfg["mcp_servers"]["toolx"]["command"] == "/bin/toolx"

    # manifest carries the gate names only; run log announces the attach
    assert run["manifest"]["harness"]["connectors"] == ["toolx", "webx"]
    assert SECRET not in json.dumps(run["manifest"])
    logs = request(sock, "runs_logs", run_id=r["run_id"], timeout=5.0)["events"]
    assert any("gates attached" in e["line"] and "toolx" in e["line"] and "webx" in e["line"] for e in logs)
    assert SECRET not in json.dumps(logs)

    # full pipeline hygiene: attested teardown, workspace gone, no alerts
    assert db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (r["run_id"],),
    ) == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / r["run_id"]).exists()
    assert request(sock, "status", timeout=5.0)["alerts"] == []

    # secrecy: the secret value appears in NO socket response, audit row, or result
    blob = json.dumps(
        [
            request(sock, "connectors_list", timeout=5.0),
            request(sock, "auth_access", timeout=5.0),
            request(sock, "runs_show", run_id=r["run_id"], timeout=5.0),
            request(sock, "runs_list", timeout=5.0),
            request(sock, "status", timeout=5.0),
            out["output"],
            AuditLog(settings.audit_log_path).read_all(),
        ]
    )
    assert SECRET not in blob
    assert SECRET not in settings.audit_log_path.read_text()


def test_e2e_no_connectors_key_means_no_injection(daemon, tmp_path) -> None:
    sock = daemon.settings.socket_path
    assert request(sock, "connectors_add", timeout=5.0, name="toolx", transport="stdio", command="/bin/toolx")["ok"]
    spec = write_job_conn(tmp_path, "plain", prompt="GATES", connectors=None)  # key absent
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is True
    run = wait_terminal(sock, r["run_id"], timeout=60)
    assert run["status"] == "succeeded"
    out = request(sock, "results_get", run_id=r["run_id"], timeout=5.0)["output"]
    assert out["config_present"] is False and out["mcp_servers"] == {}
    assert run["manifest"]["harness"]["connectors"] == []
    # and an explicit empty list behaves the same
    spec2 = write_job_conn(tmp_path, "plain2", prompt="GATES", connectors=[])
    r2 = request(sock, "enqueue", spec_path=str(spec2), timeout=10.0)
    run2 = wait_terminal(sock, r2["run_id"], timeout=60)
    assert run2["status"] == "succeeded"
    out2 = request(sock, "results_get", run_id=r2["run_id"], timeout=5.0)["output"]
    assert out2["config_present"] is False


def test_e2e_spawn_subs_and_connectors_merge_in_one_config(daemon, tmp_path, monkeypatch) -> None:
    sock = daemon.settings.socket_path
    assert request(sock, "connectors_add", timeout=5.0, name="toolx", transport="stdio", command="/bin/toolx")["ok"]
    spec = write_job_conn(tmp_path, "both", prompt="GATES", connectors=["toolx"], spawn_subs=True)
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is True
    run = wait_terminal(sock, r["run_id"], timeout=60)
    assert run["status"] == "succeeded", run["manifest"].get("status_reason")
    out = request(sock, "results_get", run_id=r["run_id"], timeout=5.0)["output"]
    # the Farmhand entry AND the gate entry live in the same vibe config
    assert set(out["mcp_servers"]) == {"farmhand", "toolx"}
    assert out["mcp_servers"]["farmhand"]["keys"] == ["args", "command"]
    assert out["mcp_servers"]["toolx"]["command"] == "/bin/toolx"
    man = run["manifest"]["harness"]
    assert man["spawn_subs"] is True and man["farmhand"]["attached"] is True
    assert man["connectors"] == ["toolx"]


def test_e2e_unresolvable_env_ref_fails_closed(daemon, tmp_path, monkeypatch) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    monkeypatch.delenv("TC_MISSING_GATE_VAR", raising=False)
    assert request(
        sock, "connectors_add", timeout=5.0, name="badref", transport="https",
        url="https://b.example/mcp", headers={"Authorization": "$TC_MISSING_GATE_VAR"},
    )["ok"]
    spec = write_job_conn(tmp_path, "badref-job", connectors=["badref"])
    r = request(sock, "enqueue", spec_path=str(spec), timeout=10.0)
    assert r["ok"] is True  # enqueue only checks the name exists — resolution is at provisioning
    run = wait_terminal(sock, r["run_id"], timeout=60)
    assert run["status"] == "failed"
    reason = run["manifest"]["status_reason"]
    assert "TC_MISSING_GATE_VAR" in reason and "not set in the daemon environment" in reason
    # fail-closed shape: attested teardown, no results row, workspace gone, no secret anywhere
    assert db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (r["run_id"],),
    ) == (1, 1, 1)
    assert db_row(settings, "SELECT COUNT(*) FROM results WHERE run_id = ?", (r["run_id"],)) == (0,)
    assert not (settings.data_dir / "workspaces" / r["run_id"]).exists()
    assert request(sock, "status", timeout=5.0)["alerts"] == []
    assert "TC_MISSING_GATE_VAR" in json.dumps(request(sock, "runs_logs", run_id=r["run_id"], timeout=5.0))


# --- CLI parity -------------------------------------------------------------------


def test_cli_connectors_roundtrip(daemon) -> None:
    sock = daemon.settings.socket_path
    result = runner.invoke(app, ["connectors", "list"])
    assert result.exit_code == 0 and "no gates registered" in result.output
    result = runner.invoke(
        app,
        ["connectors", "add", "toolx", "--transport", "stdio", "--command", "/bin/toolx", "--arg", "--serve",
         "--env", f"GATE_TOKEN={SECRET}", "--env", "LOG=debug"],
    )
    assert result.exit_code == 0, result.output
    assert "registered gate 'toolx' (transport=stdio)" in result.output
    assert SECRET not in result.output
    result = runner.invoke(
        app,
        ["connectors", "add", "webx", "--transport", "https", "--url", "https://mcp.example.com/mcp",
         "--header", "Authorization=Bearer $TC_GATE_SECRET"],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["connectors", "list"])
    assert result.exit_code == 0
    assert "gate=toolx  transport=stdio  command=/bin/toolx  args=['--serve']  env_keys=['GATE_TOKEN', 'LOG']" in result.output
    assert "gate=webx  transport=https  url=https://mcp.example.com/mcp  headers={'Authorization': 'Bearer $TC_GATE_SECRET'}" in result.output
    assert SECRET not in result.output
    # the CLI and the socket agree
    listed = request(sock, "connectors_list", timeout=5.0)["connectors"]
    assert [c["name"] for c in listed] == ["toolx", "webx"]
    # invalid add → rc 1 with the field-named error
    result = runner.invoke(app, ["connectors", "add", "bad", "--transport", "stdio"])
    assert result.exit_code == 1 and "connectors.bad.command" in result.output
    # rm
    result = runner.invoke(app, ["connectors", "rm", "webx"])
    assert result.exit_code == 0 and "removed gate 'webx' (transport=https)" in result.output
    result = runner.invoke(app, ["connectors", "rm", "webx"])
    assert result.exit_code == 1 and "unknown gate" in result.output
    result = runner.invoke(app, ["connectors", "list"])
    assert "gate=toolx" in result.output and "webx" not in result.output
