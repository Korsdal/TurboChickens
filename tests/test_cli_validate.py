"""Tests for the `tc` CLI, focused on `tc jobs validate` (CLI seam)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from turbochickens.cli.main import app

runner = CliRunner()

VALID_TOML = """
[job]
name = "weekly-incidents-digest"
description = "Summarize the week's security incidents"

[trigger]
type = "cron"
schedule = "0 7 * * 1"

[harness]
adapter = "mistral-api"
model = "mistral-large-latest"
temperature = 0.1

[resources]
timeout_sec = 600
max_context_mb = 2
network = "none"

[input]
prompt_file = "prompts/digest.txt"
attachments = []

[output]
format = "json"
schema_file = "schemas/digest.schema.json"
"""


def combined_output(result) -> str:
    parts = [result.output or ""]
    try:
        parts.append(result.stderr or "")
    except ValueError:
        pass
    return "".join(parts)


def write_spec(tmp_path: Path, toml_text: str = VALID_TOML, schema: bool = True) -> Path:
    spec = tmp_path / "job.toml"
    spec.write_text(toml_text)
    if schema:
        schema_dir = tmp_path / "schemas"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "digest.schema.json").write_text('{"type": "object"}')
    return spec


def test_tc_help_works() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "jobs" in result.output


def test_jobs_validate_ok_prints_hash(tmp_path: Path) -> None:
    spec = write_spec(tmp_path)
    result = runner.invoke(app, ["jobs", "validate", str(spec)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "weekly-incidents-digest" in result.output
    assert "sha256:" in result.output


def test_jobs_validate_bad_cron_exit_1_names_field(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('schedule = "0 7 * * 1"', 'schedule = "61 25 * * 9"')
    result = runner.invoke(app, ["jobs", "validate", str(write_spec(tmp_path, bad))])
    assert result.exit_code == 1
    assert "trigger.schedule" in combined_output(result)


def test_jobs_validate_unknown_adapter_names_field(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('adapter = "mistral-api"', 'adapter = "gpt-9000"')
    result = runner.invoke(app, ["jobs", "validate", str(write_spec(tmp_path, bad))])
    assert result.exit_code == 1
    assert "harness.adapter" in combined_output(result)


def test_jobs_validate_missing_schema_reference_names_field(tmp_path: Path) -> None:
    result = runner.invoke(app, ["jobs", "validate", str(write_spec(tmp_path, schema=False))])
    assert result.exit_code == 1
    assert "output.schema_file" in combined_output(result)


def test_jobs_validate_stray_max_tokens_names_field(tmp_path: Path) -> None:
    bad = VALID_TOML.replace("temperature = 0.1", "temperature = 0.1\nmax_tokens = 4096")
    result = runner.invoke(app, ["jobs", "validate", str(write_spec(tmp_path, bad))])
    assert result.exit_code == 1
    assert "harness.max_tokens" in combined_output(result)


def test_jobs_validate_example_spec_in_repo() -> None:
    example = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "weekly-incidents-digest"
        / "job.toml"
    )
    result = runner.invoke(app, ["jobs", "validate", str(example)])
    assert result.exit_code == 0, combined_output(result)
    assert "OK" in result.output


def test_jobs_validate_missing_file_errors() -> None:
    result = runner.invoke(app, ["jobs", "validate", "/nonexistent/job.toml"])
    assert result.exit_code != 0
