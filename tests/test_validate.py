"""Tests for the spec 04 pure-function seam: JSON-schema output validation.

`validate_output` is tested directly on raw outputs and schemas — it is a pure
transform and never raises on bad output (bad output is data; the outcome is
always an OutputValidation).
"""

from __future__ import annotations

import json
from pathlib import Path

from turbochickens.validate import validate_output

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["title", "generated_at", "incidents"],
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "generated_at": {"type": "string", "format": "date-time"},
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                },
            },
        },
    },
}


def write_schema(tmp_path: Path, schema=SCHEMA) -> Path:
    p = tmp_path / "out.schema.json"
    p.write_text(json.dumps(schema))
    return p


VALID_OUTPUT = {
    "title": "Weekly digest",
    "generated_at": "2026-11-12T07:00:00Z",
    "incidents": [{"id": "inc-1", "severity": "high"}],
}


def test_valid_output_passes_and_returns_parsed_value(tmp_path) -> None:
    outcome = validate_output(json.dumps(VALID_OUTPUT), write_schema(tmp_path))
    assert outcome.valid is True
    assert outcome.output == VALID_OUTPUT
    assert outcome.schema_errors == []


def test_non_json_output_fails_with_parse_error(tmp_path) -> None:
    outcome = validate_output("this is not json {", write_schema(tmp_path))
    assert outcome.valid is False
    assert outcome.output is None
    assert len(outcome.schema_errors) == 1
    assert "not valid JSON" in outcome.schema_errors[0]


def test_missing_required_field_fails_with_path(tmp_path) -> None:
    bad = {k: v for k, v in VALID_OUTPUT.items() if k != "title"}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert any(e.startswith("<root>") and "title" in e for e in outcome.schema_errors)


def test_wrong_type_fails_with_field_path(tmp_path) -> None:
    bad = {**VALID_OUTPUT, "title": 42}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert any(e.startswith("title:") for e in outcome.schema_errors)


def test_additional_property_fails(tmp_path) -> None:
    bad = {**VALID_OUTPUT, "surprise": True}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert any("surprise" in e for e in outcome.schema_errors)


def test_enum_violation_inside_array_item_reports_nested_path(tmp_path) -> None:
    bad = {**VALID_OUTPUT, "incidents": [{"id": "inc-1", "severity": "fatal"}]}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert any(e.startswith("incidents/0/severity") for e in outcome.schema_errors)


def test_format_date_time_is_enforced(tmp_path) -> None:
    bad = {**VALID_OUTPUT, "generated_at": "not-a-date"}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert any(e.startswith("generated_at:") for e in outcome.schema_errors)


def test_multiple_errors_all_recorded(tmp_path) -> None:
    bad = {"title": 42, "incidents": [{"id": "x", "severity": "fatal"}], "extra": 1}
    outcome = validate_output(json.dumps(bad), write_schema(tmp_path))
    assert outcome.valid is False
    assert len(outcome.schema_errors) >= 3  # title type, generated_at missing, severity enum, extra prop


def test_missing_schema_file_fails_closed(tmp_path) -> None:
    outcome = validate_output(json.dumps(VALID_OUTPUT), tmp_path / "nope.json")
    assert outcome.valid is False
    assert any("cannot load schema" in e for e in outcome.schema_errors)


def test_invalid_schema_file_fails_closed(tmp_path) -> None:
    p = tmp_path / "bad.schema.json"
    p.write_text("{not json")
    outcome = validate_output(json.dumps(VALID_OUTPUT), p)
    assert outcome.valid is False
    assert outcome.schema_errors


def test_schema_that_is_not_a_schema_fails_closed(tmp_path) -> None:
    p = write_schema(tmp_path, schema={"type": "not-a-real-type"})
    outcome = validate_output(json.dumps(VALID_OUTPUT), p)
    assert outcome.valid is False
    assert any("invalid schema" in e for e in outcome.schema_errors)
