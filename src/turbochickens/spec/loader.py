"""Load, validate, and hash TOML job specs.

File references inside a spec (e.g. ``output.schema_file``) resolve relative
to the directory containing the spec file — and are contained to it: spec 32
(G1) refuses at Candling any ref (`prompt_file`, plain `attachments`,
`schema_file`) that would leave the egg's own directory (the Fence rule in
``fence.py``).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from turbochickens.chaining import is_nestbox_uri
from turbochickens.fence import UnsafeRefError, contained_ref
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
        # spec 32 (G17): the issue is served over the socket/HTTP, so it names
        # the spec file only — never the host absolute path (which stays in
        # coopd's own log)
        raise SpecValidationError(
            [SpecIssue((), f"cannot read spec file {path.name!r}: {e.strerror or type(e).__name__}")]
        ) from e
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


def load_spec_text(raw: str | bytes, *, source: str = "stored spec") -> JobSpec:
    """Parse and validate spec TOML held in memory (no egg directory).

    Used to recover stale rows whose spec file may be gone (spec 32 ticket
    20, G16): the stored ``spec_toml`` in ``job_specs`` was already candled
    at enqueue, so model validation applies but file-ref containment does
    not (there is no egg directory to contain against). Raises
    SpecValidationError on any problem, exactly like ``load_spec``.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SpecValidationError([SpecIssue((), f"invalid {source} (not utf-8): {e}")])
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise SpecValidationError([SpecIssue((), f"invalid TOML in {source}: {e}")])
    try:
        return JobSpec.model_validate(data)
    except ValidationError as e:
        raise SpecValidationError(
            [SpecIssue(tuple(str(x) for x in err["loc"]), err["msg"]) for err in e.errors()]
        )


def _check_file_refs(spec_path: Path, spec: JobSpec) -> list[SpecIssue]:
    """Candling-time containment (spec 32, G1): every file ref in the spec —
    ``input.prompt_file``, each non-nestbox ``input.attachments`` entry, and
    ``output.schema_file`` — must stay inside the egg's own directory. The
    shared Fence rule applies (lexical safe-ref + resolved containment,
    symlink-safe), so a ref can never name a host file outside the egg dir
    (`..`, absolute paths, backslashes, and symlink escapes are all refused).
    Refusals are field-named errors with Fox copy per ``docs/lexicon.md``.

    This is validation only — it never alters the spec's fields, so the spec
    hash (and the shipped example's ``sha256:18efbd8c…80b959``) is unchanged.
    Nestbox ``nestbox://…`` attachments carry no host path and are skipped here.
    """
    issues: list[SpecIssue] = []
    base = spec_path.parent
    refs: list[tuple[str, str]] = [("input.prompt_file", spec.input.prompt_file)]
    for att in spec.input.attachments:
        # spec 25: a nestbox:// attachment is resolved from the nest box at
        # provisioning — it names no host file, so the fence does not apply
        if not is_nestbox_uri(att):
            refs.append(("input.attachments", att))
    refs.append(("output.schema_file", spec.output.schema_file))
    refused: set[str] = set()
    for field, ref in refs:
        try:
            contained_ref(base, ref, field)
        except UnsafeRefError as e:
            refused.add(field)
            issues.append(SpecIssue(tuple(field.split(".")), e.fox))
    schema = contained_ref(base, spec.output.schema_file, "output.schema_file") if "output.schema_file" not in refused else None
    if schema is not None and not schema.is_file():
        issues.append(
            SpecIssue(
                ("output", "schema_file"),
                # spec 32 (G17): the spec-relative ref only — the resolved host
                # path is never embedded in the served error
                f"schema file not found: {spec.output.schema_file!r} (not present next to the egg)",
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
    # spec 28: `connectors = []` is the pre-28 default — same treatment, so
    # existing egg hashes are unchanged
    if isinstance(harness, dict) and harness.get("connectors") == []:
        del harness["connectors"]
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
