"""Tests for the TOML job spec model and loader (pure-function seam)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.spec.model import JobSpec

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
attachments = ["inputs/incidents.csv"]

[output]
format = "json"
schema_file = "schemas/digest.schema.json"
"""

MANUAL_TOML = VALID_TOML.replace('type = "cron"\nschedule = "0 7 * * 1"', 'type = "manual"')


def write_spec(tmp_path: Path, toml_text: str, schema: bool = True) -> Path:
    spec = tmp_path / "job.toml"
    spec.write_text(toml_text)
    if schema:
        schema_dir = tmp_path / "schemas"
        schema_dir.mkdir(exist_ok=True)
        (schema_dir / "digest.schema.json").write_text('{"type": "object"}')
    return spec


def issues_of(exc: SpecValidationError) -> list[tuple[tuple[str, ...], str]]:
    return [(i.loc, i.msg) for i in exc.issues]


def test_valid_baseline_spec_parses(tmp_path: Path) -> None:
    spec = load_spec(write_spec(tmp_path, VALID_TOML))
    assert isinstance(spec, JobSpec)
    assert spec.job.name == "weekly-incidents-digest"
    assert spec.trigger.type == "cron"
    assert spec.harness.adapter == "mistral-api"
    assert spec.resources.network == "none"
    assert spec.output.format == "json"


def test_manual_trigger_without_schedule_is_valid(tmp_path: Path) -> None:
    spec = load_spec(write_spec(tmp_path, MANUAL_TOML))
    assert spec.trigger.type == "manual"
    assert spec.trigger.schedule is None


def test_spec_hash_is_stable_across_loads(tmp_path: Path) -> None:
    h1 = spec_hash(load_spec(write_spec(tmp_path, VALID_TOML)))
    (tmp_path / "job.toml").write_text(VALID_TOML + "\n# comment\n")
    h2 = spec_hash(load_spec(tmp_path / "job.toml"))
    assert h1 == h2
    assert len(h1) == 64


def test_spec_hash_changes_with_content(tmp_path: Path) -> None:
    h1 = spec_hash(load_spec(write_spec(tmp_path, VALID_TOML)))
    changed = VALID_TOML.replace("timeout_sec = 600", "timeout_sec = 300")
    h2 = spec_hash(load_spec(write_spec(tmp_path, changed)))
    assert h1 != h2


def test_bad_cron_expression_names_trigger_schedule(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('schedule = "0 7 * * 1"', 'schedule = "not a cron"')
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("trigger", "schedule") in [loc for loc, _ in issues_of(exc_info.value)]


def test_six_field_cron_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('schedule = "0 7 * * 1"', 'schedule = "0 0 7 * * 1"')
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("trigger", "schedule") in [loc for loc, _ in issues_of(exc_info.value)]


def test_cron_trigger_requires_schedule(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('schedule = "0 7 * * 1"\n', "")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("trigger", "schedule") in [loc for loc, _ in issues_of(exc_info.value)]


def test_manual_trigger_with_schedule_rejected(tmp_path: Path) -> None:
    bad = MANUAL_TOML + '\nschedule = "0 7 * * 1"\n'
    bad = bad.replace('[trigger]\ntype = "manual"\n', '[trigger]\ntype = "manual"\nschedule = "0 7 * * 1"\n')
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("trigger", "schedule") in [loc for loc, _ in issues_of(exc_info.value)]


def test_unknown_adapter_names_harness_adapter(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('adapter = "mistral-api"', 'adapter = "claude-code"')
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("harness", "adapter") in [loc for loc, _ in issues_of(exc_info.value)]


def test_missing_schema_key_names_output_schema_file(tmp_path: Path) -> None:
    bad = VALID_TOML.replace('schema_file = "schemas/digest.schema.json"\n', "")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("output", "schema_file") in [loc for loc, _ in issues_of(exc_info.value)]


def test_missing_schema_reference_names_output_schema_file(tmp_path: Path) -> None:
    # schema key present, but the referenced file does not exist
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, VALID_TOML, schema=False))
    locs = [loc for loc, _ in issues_of(exc_info.value)]
    assert ("output", "schema_file") in locs
    msg = next(m for loc, m in issues_of(exc_info.value) if loc == ("output", "schema_file"))
    assert "not found" in msg


@pytest.mark.parametrize(
    "section,extra",
    [
        ("[harness]\nadapter = \"mistral-api\"", "max_tokens = 1000"),
        ("[resources]\ntimeout_sec = 600", "max_tokens = 1000"),
        ("[output]\nformat = \"json\"", "max_tokens = 1000"),
    ],
)
def test_stray_max_tokens_in_section_is_rejected(tmp_path: Path, section: str, extra: str) -> None:
    # insert the stray key at the end of the named section
    lines = VALID_TOML.splitlines()
    out: list[str] = []
    current = None
    inserted = False
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            if current is not None and not inserted and current in section:
                out.append(extra)
                inserted = True
            current = line
        out.append(line)
    if not inserted:
        out.append(extra)
    bad = "\n".join(out) + "\n"
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert any(loc and loc[-1] == "max_tokens" for loc, _ in issues_of(exc_info.value))


def test_stray_max_tokens_at_root_is_rejected(tmp_path: Path) -> None:
    bad = 'max_tokens = 1000\n' + VALID_TOML
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("max_tokens",) in [loc for loc, _ in issues_of(exc_info.value)]


def test_unknown_top_level_section_rejected(tmp_path: Path) -> None:
    bad = VALID_TOML + "\n[limits]\nmax_tokens = 100\n"
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("limits",) in [loc for loc, _ in issues_of(exc_info.value)]


def test_missing_section_names_section(tmp_path: Path) -> None:
    bad = VALID_TOML.replace("[resources]\ntimeout_sec = 600\nmax_context_mb = 2\nnetwork = \"none\"\n\n", "")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert ("resources",) in [loc for loc, _ in issues_of(exc_info.value)]


@pytest.mark.parametrize(
    "old,new,loc",
    [
        ("temperature = 0.1", "temperature = 5.0", ("harness", "temperature")),
        ("timeout_sec = 600", "timeout_sec = 0", ("resources", "timeout_sec")),
        ("max_context_mb = 2", "max_context_mb = -1", ("resources", "max_context_mb")),
        ('network = "none"', 'network = "full"', ("resources", "network")),
        ('format = "json"', 'format = "yaml"', ("output", "format")),
    ],
)
def test_out_of_range_values_name_the_field(tmp_path: Path, old: str, new: str, loc: tuple) -> None:
    bad = VALID_TOML.replace(old, new)
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(write_spec(tmp_path, bad))
    assert loc in [l for l, _ in issues_of(exc_info.value)]


def test_invalid_toml_reports_parse_error(tmp_path: Path) -> None:
    spec = tmp_path / "job.toml"
    spec.write_text("[job\nname = ")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(spec)
    assert "TOML" in issues_of(exc_info.value)[0][1]


def test_model_rejects_max_tokens_directly() -> None:
    import tomllib

    data = tomllib.loads(VALID_TOML)
    data["harness"]["max_tokens"] = 500
    with pytest.raises(ValidationError) as exc_info:
        JobSpec.model_validate(data)
    assert ("harness", "max_tokens") in [tuple(e["loc"]) for e in exc_info.value.errors()]
