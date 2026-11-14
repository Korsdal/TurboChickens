"""Load, validate, and hash TOML job specs.

File references inside a spec (e.g. ``output.schema_file``) resolve relative
to the directory containing the spec file.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from turbochickens.spec.model import JobSpec


@dataclass(frozen=True)
class SpecIssue:
    """One validation problem, addressed by dotted field path."""

    loc: tuple[str, ...]
    msg: str

    def __str__(self) -> str:
        path = ".".join(self.loc) if self.loc else "<spec>"
        return f"{path}: {self.msg}"


class SpecValidationError(Exception):
    def __init__(self, issues: list[SpecIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues))


def load_spec(path: str | Path) -> JobSpec:
    """Parse and validate a job spec file.

    Raises SpecValidationError with field-named issues on any problem.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise SpecValidationError([SpecIssue((), f"cannot read spec file: {e}")]) from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SpecValidationError([SpecIssue((), f"invalid TOML: {e}")]) from e
    try:
        spec = JobSpec.model_validate(data)
    except ValidationError as e:
        raise SpecValidationError(
            [SpecIssue(tuple(str(x) for x in err["loc"]), err["msg"]) for err in e.errors()]
        ) from e
    issues = _check_file_refs(path, spec)
    if issues:
        raise SpecValidationError(issues)
    return spec


def _check_file_refs(spec_path: Path, spec: JobSpec) -> list[SpecIssue]:
    issues: list[SpecIssue] = []
    schema = spec_path.parent / spec.output.schema_file
    if not schema.is_file():
        issues.append(
            SpecIssue(
                ("output", "schema_file"),
                f"schema file not found: {spec.output.schema_file!r} (resolved to {schema})",
            )
        )
    return issues


def spec_hash(spec: JobSpec) -> str:
    """Stable sha256 hash of the parsed spec.

    Hashes the canonical JSON of the validated model (sorted keys, compact
    separators, unset optionals omitted), so formatting and key order in the
    TOML source do not affect the hash. Used later for run manifests.
    """
    data = spec.model_dump(mode="json", exclude_none=True)
    # spec 23: `free_range = false` is the pre-23 default — like the None
    # optionals, it stays out of the hash so existing egg hashes are unchanged
    resources = data.get("resources")
    if isinstance(resources, dict) and resources.get("free_range") is False:
        del resources["free_range"]
    # spec 27: `spawn_subs = false` is the pre-27 default — same treatment, so
    # existing egg hashes are unchanged
    harness = data.get("harness")
    if isinstance(harness, dict) and harness.get("spawn_subs") is False:
        del harness["spawn_subs"]
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
