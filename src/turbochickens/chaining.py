"""Spec 25: chaining — eggs consuming earlier clutches via `nestbox://` attachments.

An `[input].attachments` entry may be a ``nestbox://<run_id>/output.json`` URI
instead of a spec-relative file. The URI *shape* is validated at spec load
(anything URI-shaped that is not exactly that form is a field-named error —
see ``spec/model.py``); the *resolution* happens at provisioning in the
Hatchery: the referenced chicken's clutch is fetched from the Nest Box —
**valid clutches only**. Quarantined, composted (retention-swept), or missing
clutches raise :class:`NestboxError`, and the run fails at provisioning before
``running``, with the usual attested teardown. Resolved bytes count toward
``max_context_mb`` like any other injected context (spec 07), so chaining
cannot bypass the context cap.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

NESTBOX_SCHEME = "nestbox"
NESTBOX_ARTIFACT = "output.json"

#: run ids are `run-<hex>`; accept a conservative id charset in the URI
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def parse_nestbox_uri(att: str) -> str | None:
    """Pure seam: the run id when `att` is a well-formed
    ``nestbox://<run_id>/output.json`` URI, else ``None``. Well-formed means
    exactly that shape — no other scheme, no extra (or missing) path segments,
    no query/fragment, no userinfo/port in the authority, and a non-empty
    run id."""
    if not isinstance(att, str) or "://" not in att:
        return None
    parts = urlsplit(att)
    if parts.scheme != NESTBOX_SCHEME or parts.path != f"/{NESTBOX_ARTIFACT}":
        return None
    if parts.query or parts.fragment:
        return None
    run_id = parts.netloc
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    return run_id


def is_nestbox_uri(att: str) -> bool:
    """True when `att` is a well-formed nestbox:// URI."""
    return parse_nestbox_uri(att) is not None


def looks_like_uri(att: str) -> bool:
    """True for anything URI-shaped (``scheme://…`` or a ``nestbox:`` prefix).
    Such an attachment must parse as a nestbox URI, or spec load rejects it
    with a field-named error."""
    return isinstance(att, str) and ("://" in att or att.startswith(f"{NESTBOX_SCHEME}:"))


def attachment_workspace_name(att: str) -> str:
    """The file name an attachment lands under in the per-run workspace. Plain
    spec-relative paths keep their basename; a nestbox:// URI becomes
    ``nestbox-<run_id>-output.json`` so chained clutches never collide with
    each other or with plain attachments."""
    run_id = parse_nestbox_uri(att)
    if run_id is not None:
        return f"{NESTBOX_SCHEME}-{run_id}-{NESTBOX_ARTIFACT}"
    return Path(att).name


class NestboxError(Exception):
    """A `nestbox://` attachment that cannot be resolved at provisioning.
    ``kind`` is one of ``wrong_shape`` / ``missing`` / ``quarantined`` /
    ``expired`` — fail closed in every case. ``injected_bytes`` carries the
    context size measured before the failure, so the manifest's budget block
    still reports it."""

    def __init__(self, kind: str, message: str, injected_bytes: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.injected_bytes = injected_bytes


@dataclass(frozen=True)
class ResolvedClutch:
    """A chained clutch pulled live from the Nest Box at provisioning time."""

    uri: str
    run_id: str
    artifact_path: str
    data: bytes


def resolve_nestbox_clutch(att: str, db: sqlite3.Connection) -> ResolvedClutch:
    """Pure seam: resolve one ``nestbox://<run_id>/output.json`` attachment
    against the Nest Box — the append-only ``results`` index plus the on-disk
    artifacts it points at. Valid clutches only: a quarantined row
    (``valid=0``), a composted clutch (artifacts swept by retention), or no
    row at all raises :class:`NestboxError`; a malformed URI raises
    ``wrong_shape``. Quarantined and expired payload bytes are never served."""
    run_id = parse_nestbox_uri(att)
    if run_id is None:
        raise NestboxError(
            "wrong_shape",
            f"malformed nestbox attachment {att!r} — expected nestbox://<run_id>/{NESTBOX_ARTIFACT}",
        )
    row = db.execute("SELECT valid, artifacts_dir FROM results WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise NestboxError(
            "missing",
            f"no clutch in the nest box for chicken {run_id!r} (unknown chicken, or it never laid one)",
        )
    valid, artifacts_dir = row
    if not valid:
        raise NestboxError(
            "quarantined",
            f"clutch of chicken {run_id!r} is quarantined — it failed candling; only valid clutches can be chained",
        )
    artifact = Path(artifacts_dir) / NESTBOX_ARTIFACT if artifacts_dir else None
    if artifact is None or not artifact.is_file():
        raise NestboxError(
            "expired",
            f"clutch of chicken {run_id!r} is composted (retention swept the artifacts) — it can no longer be chained",
        )
    data = artifact.read_bytes()
    return ResolvedClutch(uri=att, run_id=run_id, artifact_path=str(artifact), data=data)
