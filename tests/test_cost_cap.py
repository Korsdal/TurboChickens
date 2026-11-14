"""Tests for spec 24: the Feed Budget cost cap — `resources.max_cost_usd`.

Seams:
- pure: `ResourcesSection.max_cost_usd` validation (optional, positive-only,
  `extra="forbid"` intact, still no `max_tokens`) and the finalize cost-cap
  check `cost_cap_exceeded` (over / at cap / no cap / seat cost 0).
- e2e: real daemon on a temp socket (thread) running jobs through the `tc` CLI
  against the hermetic loopback fake priced provider (spec 05 fixtures) and the
  hermetic fake vibe binary (spec 03 fixtures), plus a seat run (spec 09
  fixtures) whose `cost_usd = 0.0` can never trip a cap.

The cap is USD, enforced finalize-only: over cap → run failed with
`budget.outcome = "cost_cap"`, telemetry still recorded, teardown attested.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from test_api_adapter import api_daemon, fake_api, providers_file  # noqa: F401
from test_run_e2e import (  # noqa: F401
    combined_output,
    daemon,
    db_row,
    fake_vibe,
    run_id_of,
    settings_file,
    wait_terminal,
)
from test_seat_auth import seat_daemon, seat_env, seed_session  # noqa: F401
from turbochickens.audit import AuditLog
from turbochickens.cli.main import app
from turbochickens.client import request
from turbochickens.runner import cost_cap_exceeded
from turbochickens.spec.loader import SpecValidationError, load_spec
from turbochickens.spec.model import ResourcesSection

runner = CliRunner()

# the fake provider prices 1.0 USD/mtok in, 2.0 USD/mtok out and reports
# 100 in / 40 out → measured cost_usd = 0.00018 per run
FAKE_API_COST = 0.00018


def write_cap_job(
    tmp_path: Path,
    name: str,
    *,
    adapter: str = "mistral-api",
    model: str = "fake-model",
    prompt: str = "Say hello",
    max_cost_usd: float | None = None,
    timeout: int = 30,
) -> Path:
    """A job spec with an optional `resources.max_cost_usd` (line omitted when
    None — absent field means no cap)."""
    d = tmp_path / name
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "schemas").mkdir(parents=True, exist_ok=True)
    (d / "prompts" / "prompt.txt").write_text(prompt)
    (d / "schemas" / "out.schema.json").write_text('{"type": "object"}')
    cap_line = "" if max_cost_usd is None else f"max_cost_usd = {max_cost_usd!r}\n"
    (d / "job.toml").write_text(
        f"""
[job]
name = "{name}"
description = "cost cap test job"

[trigger]
type = "manual"

[harness]
adapter = "{adapter}"
model = "{model}"
temperature = 0.0

[resources]
timeout_sec = {timeout}
max_context_mb = 2
{cap_line}network = "none"

[input]
prompt_file = "prompts/prompt.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/out.schema.json"
"""
    )
    return d / "job.toml"


# --- spec model: the optional USD cap -----------------------------------------


def test_max_cost_usd_is_optional_off_by_default_and_positive_only() -> None:
    base = {"timeout_sec": 600, "max_context_mb": 2, "network": "none"}
    res = ResourcesSection.model_validate(base)
    assert res.max_cost_usd is None  # off by default
    assert ResourcesSection.model_validate({**base, "max_cost_usd": 0.5}).max_cost_usd == 0.5
    for bad in (0, 0.0, -1, -0.25):
        with pytest.raises(ValidationError) as exc_info:
            ResourcesSection.model_validate({**base, "max_cost_usd": bad})
        assert ("max_cost_usd",) in [tuple(e["loc"]) for e in exc_info.value.errors()]
    # extra="forbid" intact — still no token cap anywhere
    with pytest.raises(ValidationError) as exc_info:
        ResourcesSection.model_validate({**base, "max_tokens": 4096})
    assert ("max_tokens",) in [tuple(e["loc"]) for e in exc_info.value.errors()]


def test_load_spec_names_resources_max_cost_usd_on_bad_values(tmp_path: Path) -> None:
    good = write_cap_job(tmp_path, "cap-ok", max_cost_usd=0.25)
    assert load_spec(good).resources.max_cost_usd == 0.25
    plain = write_cap_job(tmp_path, "cap-absent")
    assert load_spec(plain).resources.max_cost_usd is None
    for bad in ("0", "-0.5"):
        spec = write_cap_job(tmp_path, f"cap-bad-{bad}", max_cost_usd=None)
        text = spec.read_text().replace("network = \"none\"", f"max_cost_usd = {bad}\nnetwork = \"none\"")
        spec.write_text(text)
        with pytest.raises(SpecValidationError) as exc_info:
            load_spec(spec)
        assert ("resources", "max_cost_usd") in [i.loc for i in exc_info.value.issues]


# --- pure seam: the finalize cost-cap check --------------------------------------


def test_cost_cap_pure_seam_over_at_no_cap_seat_and_unmeasured() -> None:
    usage = {"tokens_in": 100, "tokens_out": 40, "cost_usd": 0.00018}
    assert cost_cap_exceeded(usage, None) is False  # no cap set → never
    assert cost_cap_exceeded(usage, 0.0001) is True  # over cap
    assert cost_cap_exceeded(usage, 0.00018) is False  # at cap exactly → allowed
    assert cost_cap_exceeded(usage, 0.001) is False  # under cap
    # seat runs (spec 09) measure cost_usd = 0.0 — no positive cap can exceed it
    assert cost_cap_exceeded({"cost_usd": 0.0, "billed_via": "seat"}, 1e-9) is False
    # unmeasured / malformed cost telemetry never trips a cap
    assert cost_cap_exceeded({"tokens_in": 5, "tokens_out": 5}, 0.5) is False
    assert cost_cap_exceeded({}, 0.5) is False
    assert cost_cap_exceeded({"cost_usd": "0.9"}, 0.5) is False
    assert cost_cap_exceeded({"cost_usd": True}, 0.5) is False


# --- e2e over cap: fake priced provider ------------------------------------------


def test_over_cap_api_run_fails_at_finalize_with_cost_cap(api_daemon, fake_api, tmp_path) -> None:
    # measured cost is 0.00018 (100 in @1.0 + 40 out @2.0 per mtok); cap 0.0001 → over
    spec_path = write_cap_job(tmp_path, "over-cap-job", max_cost_usd=0.0001)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "failed" in result.output
    assert "feed budget exceeded at finalize" in result.output and "max_cost_usd" in result.output
    run_id = run_id_of(result)
    settings = api_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    # the budget block carries the cap and the cost_cap outcome, like timeout/context_cap
    b = m["budget"]
    assert b["outcome"] == "cost_cap"
    assert b["cost_exceeded"] is True
    assert b["max_cost_usd"] == 0.0001
    assert b["timed_out"] is False and b["context_exceeded"] is False
    assert m["resources"]["max_cost_usd"] == 0.0001
    assert "max_cost_usd" in m["status_reason"] and "cost_cap" not in m["status_reason"]
    # cost + token telemetry still recorded on the failed run
    assert m["usage"]["tokens_in"] == 100 and m["usage"]["tokens_out"] == 40
    assert m["usage"]["cost_usd"] == pytest.approx(FAKE_API_COST)
    # teardown attested as usual; workspace gone; no clutch stored (budget failure)
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    assert not (settings.data_dir / "workspaces" / run_id).exists()
    assert db_row(settings, "SELECT 1 FROM results WHERE run_id = ?", (run_id,)) is None
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 1
    # audit: abattoir attestation + terminal row with the feed-budget reason
    rows = [r for r in AuditLog(settings.audit_log_path).read_all() if r.get("run_id") == run_id]
    assert "abattoir.attested" in {r["event"] for r in rows}
    finished = next(r for r in rows if r["event"] == "chicken.finished")
    assert finished["status"] == "failed" and "max_cost_usd" in finished["reason"]
    # no alert; visible via tc runs show; feed ledger still rolled the telemetry up
    assert run_id not in request(settings.socket_path, "status", timeout=2.0)["alerts"]
    show = runner.invoke(app, ["runs", "show", run_id])
    assert show.exit_code == 0 and '"cost_cap"' in show.output
    stats = request(settings.socket_path, "stats_show", job_id="over-cap-job", timeout=2.0)["stats"]
    assert stats["n_runs"] == 1 and stats["tokens_in_mean"] == 100 and stats["tokens_out_mean"] == 40


def test_at_cap_exactly_api_run_succeeds(api_daemon, fake_api, tmp_path) -> None:
    # cost_usd == max_cost_usd → not over → the cap does not trip
    spec_path = write_cap_job(tmp_path, "at-cap-job", max_cost_usd=FAKE_API_COST)
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    assert "succeeded" in result.output
    run_id = run_id_of(result)
    run = wait_terminal(api_daemon.settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    b = run["manifest"]["budget"]
    assert b["outcome"] == "ok" and b["cost_exceeded"] is False and b["max_cost_usd"] == FAKE_API_COST
    assert run["manifest"]["usage"]["cost_usd"] == pytest.approx(FAKE_API_COST)
    res = runner.invoke(app, ["results", "get", run_id])
    assert res.exit_code == 0 and json.loads(res.output)["message"] == "hello from fake api"


def test_no_cap_set_behavior_unchanged_but_cost_telemetry_recorded(api_daemon, fake_api, tmp_path) -> None:
    spec_path = write_cap_job(tmp_path, "no-cap-job")  # max_cost_usd line absent
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = api_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    m = run["manifest"]
    assert m["budget"]["outcome"] == "ok" and m["budget"]["max_cost_usd"] is None
    assert m["budget"]["cost_exceeded"] is False
    assert m["resources"]["max_cost_usd"] is None
    # usage.cost_usd + tokens recorded on the run and visible to the feed ledger (spec 08)
    assert m["usage"]["cost_usd"] == pytest.approx(FAKE_API_COST)
    assert m["usage"]["tokens_in"] == 100 and m["usage"]["tokens_out"] == 40
    stats = request(settings.socket_path, "stats_show", job_id="no-cap-job", timeout=2.0)["stats"]
    assert stats["n_runs"] == 1
    assert stats["expected_cost_usd"] == pytest.approx(FAKE_API_COST)  # from providers.toml pricing
    assert stats["tokens_in_p95"] == 100 and stats["tokens_out_p95"] == 40


# --- e2e over cap via the fake vibe binary (harness-reported cost) ---------------


def test_vibe_cli_run_over_cap_fails_with_cost_cap(daemon, tmp_path) -> None:
    # the fake vibe binary reports cost_usd = 0.001 in its export.json
    spec_path = write_cap_job(
        tmp_path, "vibe-over-cap", adapter="vibe-cli", model="fake-model", max_cost_usd=0.0005
    )
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 1
    assert "feed budget exceeded at finalize" in result.output
    run_id = run_id_of(result)
    settings = daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "failed"
    m = run["manifest"]
    assert m["budget"]["outcome"] == "cost_cap" and m["budget"]["cost_exceeded"] is True
    assert m["budget"]["max_cost_usd"] == 0.0005
    assert m["usage"]["cost_usd"] == pytest.approx(0.001)  # telemetry recorded
    assert m["usage"]["tokens_in"] == 12 and m["usage"]["tokens_out"] == 34
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    assert db_row(settings, "SELECT 1 FROM results WHERE run_id = ?", (run_id,)) is None


def test_vibe_cli_run_at_cap_succeeds(daemon, tmp_path) -> None:
    spec_path = write_cap_job(
        tmp_path, "vibe-at-cap", adapter="vibe-cli", model="fake-model", max_cost_usd=0.001
    )
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    run = wait_terminal(daemon.settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    assert run["manifest"]["budget"]["outcome"] == "ok"
    assert run["manifest"]["usage"]["cost_usd"] == pytest.approx(0.001)


# --- seat runs (cost_usd = 0.0) never trip the cap --------------------------------


def test_seat_run_with_a_tiny_cap_never_trips_it(seat_env, seat_daemon, tmp_path) -> None:
    seed_session(seat_env)  # seat session via the fake OAuth device flow
    spec_path = write_cap_job(
        tmp_path, "seat-capped", adapter="mistral-api", model="seatco/seat-model", max_cost_usd=0.000001
    )
    result = runner.invoke(app, ["run", str(spec_path), "--wait"])
    assert result.exit_code == 0, combined_output(result)
    run_id = run_id_of(result)
    settings = seat_daemon.settings
    run = wait_terminal(settings.socket_path, run_id)
    assert run["status"] == "succeeded"
    m = run["manifest"]
    # seat billing measures cost_usd = 0.0 — strictly under any positive cap
    assert m["usage"]["cost_usd"] == 0.0 and m["usage"]["billed_via"] == "seat"
    assert m["budget"]["outcome"] == "ok"
    assert m["budget"]["cost_exceeded"] is False
    assert m["budget"]["max_cost_usd"] == 0.000001
    att = db_row(
        settings,
        "SELECT process_reaped, workspace_removed, env_cleared FROM teardown_attestations WHERE run_id = ?",
        (run_id,),
    )
    assert att == (1, 1, 1)
    stats = request(settings.socket_path, "stats_show", job_id="seat-capped", timeout=2.0)["stats"]
    assert stats["n_runs"] == 1 and stats["expected_cost_usd"] == 0.0
