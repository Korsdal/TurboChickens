"""Output validation — the pure-function seam for spec 04.

Raw harness output is untrusted data (baseline v0.2 §10.2): it must parse as
JSON and validate against the job's JSON schema before it may enter the result
store. Anything else is a validation failure and the output lands in quarantine
— never stored as valid, never discarded.

This module never raises on bad output: bad output is data, and the outcome is
always an `OutputValidation`. A broken/missing schema file also reports as
schema errors so the run fails closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@dataclass
class OutputValidation:
    """Outcome of validating one raw harness output against a job schema."""

    valid: bool
    output: Any = None  # the parsed JSON value, set when valid
    schema_errors: list[str] = field(default_factory=list)


def _error_path(error: Any) -> str:
    return "/".join(str(p) for p in error.absolute_path) or "<root>"


def validate_output(raw_output: str, schema_path: str | Path, schema_name: str | None = None) -> OutputValidation:
    """Validate `raw_output` against the JSON schema at `schema_path`.

    Draft 2020-12 with format assertions enabled (e.g. `format: date-time`).
    Each schema error is reported as ``<json path>: <message>``.

    Spec 32 (G17): schema errors are served (results, run logs, Headcount), so
    they report the schema by its spec-relative `schema_name` (default: the
    file name) — never the host absolute path, which stays in coopd's own log.
    """
    name = schema_name if schema_name is not None else Path(schema_path).name
    try:
        parsed = json.loads(raw_output)
    except ValueError as e:
        return OutputValidation(valid=False, schema_errors=[f"<output>: not valid JSON: {e}"])
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except OSError as e:
        return OutputValidation(
            valid=False,
            schema_errors=[f"<schema>: cannot load schema {name!r}: {e.strerror or type(e).__name__}"],
        )
    except ValueError as e:
        return OutputValidation(
            valid=False, schema_errors=[f"<schema>: cannot load schema {name!r}: {e}"]
        )
    try:
        Draft202012Validator.check_schema(schema)  # raises on a broken schema (incl. UnknownType)
        validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
        errors = sorted(validator.iter_errors(parsed), key=lambda e: (list(e.absolute_path), e.message))
    except Exception as e:  # fail closed: a broken schema must never pass output
        return OutputValidation(valid=False, schema_errors=[f"<schema>: invalid schema: {e}"])
    if errors:
        return OutputValidation(
            valid=False, schema_errors=[f"{_error_path(e)}: {e.message}" for e in errors]
        )
    return OutputValidation(valid=True, output=parsed)
