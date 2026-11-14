"""Tests for spec 07: run budgets — wall-clock hard kill + injected-context cap.

Seams: e2e through the `tc` CLI against a real daemon on a temp socket (thread
daemon; the hermetic fake vibe binary from spec 03 and the hermetic loopback
fake provider from spec 05), plus a pure model check that the job spec carries
no token cap. Token usage stays telemetry; the only enforced budgets are
`timeout_sec` (hard kill) and `max_context_mb` (injected-context cap).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

# fixtures + helpers shared with the spec 03 / spec 05 e2e suites
from test_api_adapter import api_daemon, fake_api, providers_file, write_api_job  # noqa: F401
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
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.spec.model import ResourcesSection

runner = CliRunner()

MIB = 1024 * 1024


def write_ctx_job(
    tmp_path: Path,
    name: str,
    *,
    prompt: str,
    attachments: dict[str, bytes] | None = None,
    timeout: int = 30,
    max_context_mb: int = 2,
) -> Path:
    """A vibe-cli job with explicit attachment bytes and resource budgets."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "p.txt").write_text(prompt)
    att_list: list[str] = []
    for rel, data in (attachments or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        att_list.append(rel)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
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
timeout_sec = {timeout}
max_context_mb = {max_context_mb}
network = "none"

[input]
prompt_file = "prompts/p.txt"
attachments = {json.dumps(att_list)}

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


# --- wall-clock hard kill ----------------------------------------------------


def test_timeout_hard_kills_run_marks_failed_attests_and_audits(daemon, tmp_path) -> None:
    spec_path = write_job(tmp_path, name="slow-job", prompt="SLEEP 60 hang forever", timeout=1)
    t0 = time.time()
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    elapsed = time.time() - t0
    assert result.exit_code == 1
    assert "failed" in result.output
    assert elapsed < 30, f"run was not hard-killed (took {elapsed:.1f}s)"
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    assert m["timed_out"] is True
    assert "timeout" in m["status_reason"]
    b = m["budget"]
    assert b["outcome"] == "timeout"
    assert b["timed_out"] is True
    assert b["timeout_sec"] == 1
    assert b["context_exceeded"] is False
    # teardown attestation + audit rows even on a hard kill
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    assert "abattoir.attested" in {r["event"] for r in rows}
    finished = next(r for r in rows if r["event"] == "chicken.finished")
    assert finished["status"] == "failed" and "timeout" in finished["reason"]
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    # the budget outcome is visible via tc runs show
    show = runner.invoke(app, ["runs", "show", run_id])
    assert show.exit_code == 0
    assert '"budget"' in show.output and '"timeout"' in show.output


def test_api_run_exceeding_timeout_is_hard_killed_and_attested(api_daemon, fake_api, tmp_path) -> None:
    # the fake provider sleeps 3s; timeout_sec=1 must hard-stop the call
    spec_path = write_api_job(tmp_path, name="slow-api-timeout-job", prompt="SCENARIO: slow work", timeout=1)
    t0 = time.time()
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    elapsed = time.time() - t0
    assert result.exit_code == 1
    assert elapsed < 20, f"api run was not hard-killed (took {elapsed:.1f}s)"
    run_id = run_id_of(result)
    settings = api_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    assert m["timed_out"] is True
    assert "timeout" in m["status_reason"]
    assert m["budget"]["outcome"] == "timeout"
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    assert "abattoir.attested" in {r["event"] for r in rows}
    assert not (settings.data_dir / "workspaces" / run_id).exists()


# --- injected-context cap ------------------------------------------------------


def test_context_over_cap_fails_at_provisioning_before_harness_work(daemon, tmp_path) -> None:
    prompt = "hello"
    big = b"x" * (MIB + MIB // 2)  # 1.5 MiB attachment against a 1 MiB cap
    spec_path = write_ctx_job(
        tmp_path, "big-ctx-job", prompt=prompt, attachments={"data/big.bin": big}, max_context_mb=1
    )
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    assert "provisioning" in m["status_reason"] and "max_context_mb" in m["status_reason"]
    b = m["budget"]
    assert b["outcome"] == "context_cap"
    assert b["context_exceeded"] is True
    assert b["injected_context_bytes"] == len(prompt) + len(big)
    assert b["max_context_mb"] == 1
    assert b["timed_out"] is False
    # failed before doing work: never reached running, no harness events at all
    logs = request(settings.socket_path, "runs_logs", run_id=run_id, timeout=2.0)["events"]
    assert any("status → provisioning" in e["line"] for e in logs)
    assert not any("status → running" in e["line"] for e in logs)
    assert not any(e["source"] == "harness" for e in logs)
    # still fully attested + audited, no result stored
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    assert "abattoir.attested" in {r["event"] for r in rows}
    finished = next(r for r in rows if r["event"] == "chicken.finished")
    assert finished["status"] == "failed" and "max_context_mb" in finished["reason"]
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    assert db_row(settings, "SELECT 1 FROM results WHERE run_id = ?", (run_id,)) is None
    show = runner.invoke(app, ["runs", "show", run_id])
    assert show.exit_code == 0 and '"context_cap"' in show.output


def test_context_exactly_at_cap_is_allowed(daemon, tmp_path) -> None:
    big = b"x" * MIB  # exactly 1 MiB; cap is inclusive
    spec_path = write_ctx_job(tmp_path, "edge-job", prompt="", attachments={"data/big.bin": big}, max_context_mb=1)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    run = wait_terminal(daemon.settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    b = run["manifest"]["budget"]
    assert b["outcome"] == "ok"
    assert b["context_exceeded"] is False
    assert b["injected_context_bytes"] == MIB


def test_success_manifest_records_budget_outcome_ok(daemon, tmp_path) -> None:
    prompt = "Say hello"
    att = b"a,b\n1,2\n"
    spec_path = write_ctx_job(
        tmp_path, "ok-job", prompt=prompt, attachments={"data/a.csv": att}, timeout=30, max_context_mb=2
    )
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    run = wait_terminal(daemon.settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    m = run["manifest"]
    assert m["timed_out"] is False
    assert m["budget"] == {
        "free_range": False,  # spec 23: fenced by default
        "timeout_sec": 30,
        "max_context_mb": 2,
        "max_cost_usd": None,  # spec 24: no cost cap set by default
        "injected_context_bytes": len(prompt) + len(att),
        "timed_out": False,
        "context_exceeded": False,
        "cost_exceeded": False,
        "outcome": "ok",
    }


# --- no token cap in the spec model --------------------------------------------


def test_resources_section_has_no_token_cap() -> None:
    with pytest.raises(ValidationError) as exc_info:
        ResourcesSection.model_validate(
            {"timeout_sec": 600, "max_context_mb": 2, "network": "none", "max_tokens": 4096}
        )
    assert ("max_tokens",) in [tuple(e["loc"]) for e in exc_info.value.errors()]
    # the enforced budgets are wall-clock timeout + injected-context cap + the
    # optional USD cost cap (spec 24) — still no token cap
    res = ResourcesSection.model_validate({"timeout_sec": 600, "max_context_mb": 2, "network": "none"})
    assert set(res.model_dump()) == {"free_range", "timeout_sec", "max_context_mb", "max_cost_usd", "network"}
    assert res.max_cost_usd is None  # off by default
    assert res.free_range is False  # spec 23: fenced by default
