"""Tests for spec 25: chaining — eggs consuming earlier clutches via
`nestbox://<run_id>/output.json` attachments.

Seams: the pure URI-parse + Nest-Box resolver seam (valid / quarantined /
expired / missing / wrong-shape), the spec-load validator (field-named errors
via `load_spec` and `tc jobs validate`), and e2e through the real daemon on a
temp socket with the hermetic fake vibe binary ($TURBOCHICKENS_VIBE_BIN):
run A lays a valid clutch, run B chains it and succeeds with the lineage in
the manifest + Headcount; quarantined / composted / missing references fail
closed at provisioning before `running` (attested teardown, no results row);
resolved clutch bytes count toward `max_context_mb` (over-cap chain →
`budget.outcome = "context_cap"` exactly like spec 07).
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

# fixtures + helpers shared with the spec 03 / spec 08 e2e suites
from test_run_e2e import (  # noqa: F401
    combined_output,
    daemon,
    db_row,
    fake_vibe,
    run_id_of,
    settings_file,
    wait_terminal,
    write_job,
)
from test_stats_retention import backdate_run, months_ago_iso  # noqa: F401
from turbochickens.audit import AuditLog
from turbochickens.chaining import (
    NestboxError,
    attachment_workspace_name,
    is_nestbox_uri,
    looks_like_uri,
    parse_nestbox_uri,
    resolve_nestbox_clutch,
)
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.spec.loader import SpecValidationError, load_spec
from turbochickens.storage import init_db

runner = CliRunner()

MIB = 1024 * 1024

CHAIN_JOB_TOML = """
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
timeout_sec = {timeout}
max_context_mb = {max_context_mb}
network = "none"

[input]
prompt_file = "prompts/p.txt"
attachments = {attachments}

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""


def write_chain_job(
    tmp_path: Path,
    name: str,
    *,
    prompt: str,
    attachments: list[str],
    max_context_mb: int = 2,
    timeout: int = 30,
    schema: dict | None = None,
) -> Path:
    """A vibe-cli job whose [input].attachments is an explicit list of strings
    (plain spec-relative paths and/or nestbox:// URIs)."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "p.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text(json.dumps(schema or {"type": "object"}))
    (d / "job.toml").write_text(
        CHAIN_JOB_TOML.format(
            name=name,
            timeout=timeout,
            max_context_mb=max_context_mb,
            attachments=json.dumps(attachments),
        )
    )
    return d / "job.toml"


def b64_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --- pure seam: the URI shape ---------------------------------------------------


def test_parse_nestbox_uri_accepts_exactly_the_nestbox_shape() -> None:
    assert parse_nestbox_uri("nestbox://run-abc123def456/output.json") == "run-abc123def456"
    assert is_nestbox_uri("nestbox://run-abc123def456/output.json")
    # plain spec-relative paths are not URIs
    assert parse_nestbox_uri("inputs/incidents.csv") is None
    assert not is_nestbox_uri("inputs/incidents.csv")
    assert not looks_like_uri("inputs/incidents.csv")
    assert looks_like_uri("https://example.com/x")
    assert looks_like_uri("nestbox:/run-1/output.json")


@pytest.mark.parametrize(
    "bad",
    [
        "nestbox://",  # missing run id
        "nestbox:///output.json",  # missing run id
        "nestbox://run-1/extra/output.json",  # extra path segment
        "nestbox://run-1/other.json",  # wrong artifact name
        "nestbox://run-1",  # missing path
        "nestbox://run-1/output.json/",  # trailing segment
        "https://run-1/output.json",  # wrong scheme
        "nestbox:/run-1/output.json",  # wrong separator
        "nestbox://run-1/output.json?dl=1",  # query
        "nestbox://run-1/output.json#frag",  # fragment
        "nestbox://user@run-1/output.json",  # userinfo
        "nestbox://run-1:8080/output.json",  # port
        "nestbox://run 1/output.json",  # whitespace in run id
    ],
)
def test_parse_nestbox_uri_rejects_malformed_shapes(bad: str) -> None:
    assert parse_nestbox_uri(bad) is None, bad
    assert not is_nestbox_uri(bad)


def test_attachment_workspace_name_never_collides() -> None:
    assert attachment_workspace_name("nestbox://run-aaa/output.json") == "nestbox-run-aaa-output.json"
    assert attachment_workspace_name("nestbox://run-bbb/output.json") == "nestbox-run-bbb-output.json"
    assert attachment_workspace_name("data/a.csv") == "a.csv"


# --- spec load: field-named validation of nestbox:// attachments -----------------


def test_load_spec_accepts_nestbox_attachment(tmp_path: Path) -> None:
    spec_path = write_chain_job(
        tmp_path, "chained", prompt="Summarize", attachments=["nestbox://run-abc123def456/output.json"]
    )
    spec = load_spec(spec_path)
    assert spec.input.attachments == ["nestbox://run-abc123def456/output.json"]


@pytest.mark.parametrize(
    "bad",
    [
        "https://run-1/output.json",  # wrong scheme
        "nestbox://",  # missing run id
        "nestbox://run-1/extra/output.json",  # extra path segments
        "nestbox:/run-1/output.json",
    ],
)
def test_load_spec_rejects_malformed_uri_with_field_named_error(tmp_path: Path, bad: str) -> None:
    spec_path = write_chain_job(tmp_path, "bad-chain", prompt="p", attachments=[bad])
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(spec_path)
    issues = [(i.loc, i.msg) for i in exc_info.value.issues]
    assert ("input", "attachments") in [loc for loc, _ in issues]
    msg = next(m for loc, m in issues if loc == ("input", "attachments"))
    assert "nestbox" in msg and bad in msg


def test_input_section_extra_forbid_intact(tmp_path: Path) -> None:
    spec_path = write_chain_job(tmp_path, "extra", prompt="p", attachments=[])
    toml = spec_path.read_text().replace('prompt_file = "prompts/p.txt"', 'prompt_file = "prompts/p.txt"\nmax_tokens = 100')
    spec_path.write_text(toml)
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(spec_path)
    assert any(i.loc == ("input", "max_tokens") for i in exc_info.value.issues)


def test_cli_validate_reports_malformed_nestbox_uri(tmp_path: Path, fake_vibe) -> None:
    spec_path = write_chain_job(tmp_path, "cli-bad", prompt="p", attachments=["nestbox://run-1/nope.json"])
    result = runner.invoke(app, ["jobs", "validate", str(spec_path)])
    assert result.exit_code == 1
    assert "input.attachments" in combined_output(result)
    # a well-formed nestbox URI validates fine
    good = write_chain_job(tmp_path, "cli-good", prompt="p", attachments=["nestbox://run-abc123def456/output.json"])
    result = runner.invoke(app, ["jobs", "validate", str(good)])
    assert result.exit_code == 0, combined_output(result)
    assert "OK" in result.output


# --- pure seam: the Nest Box resolver ----------------------------------------------


def _seed_db(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "nestbox.db")
    conn.execute(
        "INSERT INTO job_specs (id, name, spec_hash, spec_toml, created_at)"
        " VALUES ('job-a', 'job-a', 'hash', 'toml', '2026-01-01T00:00:00.000Z')"
    )
    conn.commit()
    return conn


def _add_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT INTO runs (id, job_id, status, trigger, started_at, finished_at)"
        " VALUES (?, 'job-a', 'succeeded', 'manual', '2026-01-01T00:00:00.000Z', '2026-01-01T00:01:00.000Z')",
        (run_id,),
    )
    conn.commit()


def test_resolver_returns_valid_clutch_bytes(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    run_id = "run-aaa111bbb222"
    _add_run(conn, run_id)
    artifacts = tmp_path / "results" / run_id
    artifacts.mkdir(parents=True)
    payload = b'{\n  "message": "hi"\n}\n'
    (artifacts / "output.json").write_bytes(payload)
    conn.execute(
        "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
        " VALUES (?, 1, ?, ?, NULL)",
        (run_id, payload.decode(), str(artifacts)),
    )
    conn.commit()
    clutch = resolve_nestbox_clutch(f"nestbox://{run_id}/output.json", conn)
    assert clutch.run_id == run_id
    assert clutch.data == payload
    assert clutch.artifact_path == str(artifacts / "output.json")


def test_resolver_refuses_quarantined_clutch(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    run_id = "run-qqq111www222"
    _add_run(conn, run_id)
    qdir = tmp_path / "quarantine" / run_id
    qdir.mkdir(parents=True)
    conn.execute(
        "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
        " VALUES (?, 0, 'raw', ?, ?)",
        (run_id, str(qdir), json.dumps(["$: missing digest"])),
    )
    conn.commit()
    with pytest.raises(NestboxError) as exc_info:
        resolve_nestbox_clutch(f"nestbox://{run_id}/output.json", conn)
    assert exc_info.value.kind == "quarantined"
    assert "quarantined" in str(exc_info.value)


def test_resolver_refuses_composted_clutch(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    run_id = "run-old111old222"
    _add_run(conn, run_id)
    # valid row, but the retention sweep already removed the artifacts
    gone = tmp_path / "results" / run_id
    conn.execute(
        "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
        " VALUES (?, 1, '{}', ?, NULL)",
        (run_id, str(gone)),
    )
    conn.commit()
    assert not gone.exists()
    with pytest.raises(NestboxError) as exc_info:
        resolve_nestbox_clutch(f"nestbox://{run_id}/output.json", conn)
    assert exc_info.value.kind == "expired"
    assert "composted" in str(exc_info.value)


def test_resolver_refuses_missing_clutch(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    with pytest.raises(NestboxError) as exc_info:
        resolve_nestbox_clutch("nestbox://run-doesnotexist1/output.json", conn)
    assert exc_info.value.kind == "missing"
    assert "no clutch in the nest box" in str(exc_info.value)


def test_resolver_refuses_wrong_shape(tmp_path: Path) -> None:
    conn = _seed_db(tmp_path)
    with pytest.raises(NestboxError) as exc_info:
        resolve_nestbox_clutch("nestbox://run-1/extra/output.json", conn)
    assert exc_info.value.kind == "wrong_shape"


# --- e2e: run A lays a clutch, run B chains it --------------------------------------


def test_chained_run_consumes_earlier_clutch_and_records_lineage(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    # run A produces a valid clutch in the nest box
    spec_a = write_job(tmp_path, name="producer")
    result = runner.invoke(app, ["run", str(spec_a), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_a = run_id_of(result)
    run = wait_terminal(sock, run_a)
    assert run["status"] == "succeeded"
    clutch_path = settings.results_dir / run_a / "output.json"
    clutch_bytes = clutch_path.read_bytes()

    # run B chains A's clutch via a nestbox:// attachment
    prompt_b = "Summarize the attached clutch"
    uri = f"nestbox://{run_a}/output.json"
    spec_b = write_chain_job(tmp_path, "consumer", prompt=prompt_b, attachments=[uri])
    result = runner.invoke(app, ["run", str(spec_b), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_b = run_id_of(result)
    run = wait_terminal(sock, run_b)
    assert run["status"] == "succeeded"

    # manifest records the resolved source (run id + artifact path + bytes),
    # and the resolved bytes counted toward the injected-context size
    m = run["manifest"]
    assert m["chaining"] == [
        {
            "attachment": uri,
            "run_id": run_a,
            "artifact_path": f"results/{run_a}/output.json",  # spec 32 (G17): data-dir-relative
            "size_bytes": len(clutch_bytes),
        }
    ]
    assert m["input"]["attachments"] == [uri]
    b = m["budget"]
    assert b["outcome"] == "ok"
    assert b["injected_context_bytes"] == len(prompt_b.encode()) + len(clutch_bytes)

    # the lineage is in the Headcount + the run log; the chicken really ran
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_b]
    consumed = [r for r in rows if r["event"] == "clutch.consumed"]
    assert len(consumed) == 1
    assert consumed[0]["source_run_id"] == run_a
    assert consumed[0]["attachment"] == uri
    assert consumed[0]["artifact"] == f"results/{run_a}/output.json"  # spec 32 (G17)
    logs = request(sock, "runs_logs", run_id=run_b, timeout=2.0)["events"]
    assert any("status → running" in e["line"] for e in logs)
    assert any(e["source"] == "harness" for e in logs)
    assert any(f"chained clutch from chicken {run_a}" in e["line"] and "nestbox-run" in e["line"] for e in logs)

    # B laid its own valid clutch; attested as usual; visible via tc runs show
    assert db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_b,)) == (1,)
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_b,),
    )
    assert att == (1, 1, 1)
    show = runner.invoke(app, ["runs", "show", run_b])
    assert show.exit_code == 0 and '"chaining"' in show.output
    got = request(sock, "results_get", run_id=run_b, timeout=2.0)
    assert got["ok"] is True and got["valid"] is True


# --- e2e fail-closed: quarantined / missing / composted references ------------------


def _assert_provisioning_failure(settings, sock, run_id: str, reason_fragment: str) -> dict:
    run = wait_terminal(sock, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    assert "provisioning failed" in m["status_reason"]
    assert reason_fragment in m["status_reason"]
    assert m["budget"]["outcome"] == "ok"  # not a budget failure — a provisioning one
    # failed before running: no harness events at all
    logs = request(sock, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any("status → provisioning" in e["line"] for e in logs)
    assert not any("status → running" in e["line"] for e in logs)
    assert not any(e["source"] == "harness" for e in logs)
    # attested teardown (1,1,1), no results row, workspace gone, audited
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    assert db_row(settings, "SELECT 1 FROM results WHERE run_id = ?", (run_id,)) is None
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    events = {r["event"] for r in rows}
    assert "abattoir.attested" in events and "chicken.finished" in events
    assert "clutch.consumed" not in events
    finished = next(r for r in rows if r["event"] == "chicken.finished")
    assert finished["status"] == "failed" and reason_fragment in finished["reason"]
    return m


def test_quarantined_clutch_reference_fails_at_provisioning(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    # a chicken whose clutch failed candling → quarantined (valid=0)
    strict_schema = {"type": "object", "required": ["digest"], "additionalProperties": False}
    spec_q = write_chain_job(tmp_path, "bad-output", prompt='{"message": "hi"}', attachments=[], schema=strict_schema)
    result = runner.invoke(app, ["run", str(spec_q), "--wait"])
    assert result.exit_code == 1
    run_q = run_id_of(result)
    assert db_row(settings, "SELECT valid FROM results WHERE run_id = ?", (run_q,)) == (0,)

    spec_b = write_chain_job(
        tmp_path, "chain-quarantined", prompt="use it", attachments=[f"nestbox://{run_q}/output.json"]
    )
    result = runner.invoke(app, ["run", str(spec_b), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    run_b = run_id_of(result)
    m = _assert_provisioning_failure(settings, sock, run_b, "quarantined")
    # only the prompt was injected before the failure
    assert m["budget"]["injected_context_bytes"] == len(b"use it")
    # tc results get never serves anything for the failed chain
    got = runner.invoke(app, ["results", "get", run_b])
    assert got.exit_code == 1


def test_missing_clutch_reference_fails_at_provisioning(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    spec_b = write_chain_job(
        tmp_path, "chain-missing", prompt="use it", attachments=["nestbox://run-000000000000/output.json"]
    )
    result = runner.invoke(app, ["run", str(spec_b), "--wait"])
    assert result.exit_code == 1
    run_b = run_id_of(result)
    _assert_provisioning_failure(settings, sock, run_b, "no clutch in the nest box")


def test_composted_clutch_reference_fails_at_provisioning(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    # run A lays a valid clutch, then retention composts it
    spec_a = write_job(tmp_path, name="aging-producer")
    result = runner.invoke(app, ["run", str(spec_a), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_a = run_id_of(result)
    assert wait_terminal(sock, run_a)["status"] == "succeeded"
    backdate_run(settings, run_a, months_ago_iso(7))
    sweep = request(sock, "retention_sweep", timeout=10.0)
    assert sweep["ok"] is True
    assert not (settings.results_dir / run_a).exists()

    spec_b = write_chain_job(
        tmp_path, "chain-composted", prompt="use it", attachments=[f"nestbox://{run_a}/output.json"]
    )
    result = runner.invoke(app, ["run", str(spec_b), "--wait"])
    assert result.exit_code == 1
    run_b = run_id_of(result)
    _assert_provisioning_failure(settings, sock, run_b, "composted")


# --- e2e: resolved clutch bytes count toward the context cap ------------------------


def test_over_cap_chain_fails_with_context_cap_like_spec_07(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    spec_a = write_job(tmp_path, name="cap-producer")
    result = runner.invoke(app, ["run", str(spec_a), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_a = run_id_of(result)
    assert wait_terminal(sock, run_a)["status"] == "succeeded"
    clutch_bytes = (settings.results_dir / run_a / "output.json").read_bytes()
    assert len(clutch_bytes) > 10  # the chained bytes genuinely push over the cap

    # prompt sized so prompt + chained clutch exceeds the 1 MiB cap
    prompt = "x" * (MIB - 10)
    spec_b = write_chain_job(
        tmp_path,
        "over-cap-chain",
        prompt=prompt,
        attachments=[f"nestbox://{run_a}/output.json"],
        max_context_mb=1,
    )
    result = runner.invoke(app, ["run", str(spec_b), "--wait"])
    assert result.exit_code == 1
    run_b = run_id_of(result)
    run = wait_terminal(sock, run_b)
    assert run["status"] == "failed"
    m = run["manifest"]
    b = m["budget"]
    assert b["outcome"] == "context_cap"
    assert b["context_exceeded"] is True
    # the resolved clutch bytes are counted in the injected context
    assert b["injected_context_bytes"] == len(prompt.encode()) + len(clutch_bytes)
    assert b["injected_context_bytes"] > MIB
    assert "max_context_mb" in m["status_reason"]
    logs = request(sock, "runs_logs", run_id=run_b, timeout=2.0)["events"]
    assert not any("status → running" in e["line"] for e in logs)
    assert not any(e["source"] == "harness" for e in logs)
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_b,),
    )
    assert att == (1, 1, 1)
    assert db_row(settings, "SELECT 1 FROM results WHERE run_id = ?", (run_b,)) is None
    assert not (settings.data_dir / "workspaces" / run_b).exists()
    show = runner.invoke(app, ["runs", "show", run_b])
    assert show.exit_code == 0 and '"context_cap"' in show.output


# --- e2e: a nestbox:// job can be uploaded as a bundle (not shipped in it) -----------


def test_jobs_upload_accepts_nestbox_attachment_and_runs_it(daemon, tmp_path) -> None:
    settings = daemon.settings
    sock = settings.socket_path
    spec_a = write_job(tmp_path, name="upload-producer")
    result = runner.invoke(app, ["run", str(spec_a), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_a = run_id_of(result)
    assert wait_terminal(sock, run_a)["status"] == "succeeded"

    spec_b = write_chain_job(
        tmp_path, "upload-consumer", prompt="summarize", attachments=[f"nestbox://{run_a}/output.json"]
    )
    root = spec_b.parent
    files = {
        str(p.relative_to(root)): b64_bytes(p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
    resp = request(sock, "jobs_upload", files=files, timeout=10.0)
    assert resp["ok"] is True and resp["valid"] is True, resp
    assert resp["job_id"] == "upload-consumer"
    # the nestbox:// attachment was not required to be a bundle file
    assert not any("not in the bundle" in e["message"] for e in resp.get("errors", []))

    enq = request(sock, "enqueue", spec_path=resp["spec_path"], spec_hash=resp["spec_hash"], trigger="manual", timeout=5.0)
    assert enq["ok"] is True
    run = wait_terminal(sock, enq["run_id"])
    assert run["status"] == "succeeded"
    assert run["manifest"]["chaining"][0]["run_id"] == run_a
