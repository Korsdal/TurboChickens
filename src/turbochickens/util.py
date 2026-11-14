"""Shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision, e.g. 2026-11-12T13:30:00.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def display_relpath(path: str | Path, base: str | Path) -> str:
    """The served form of a filesystem path (spec 32, review G17): relative to
    `base` (the data dir) when the path lives under it — e.g.
    ``workspaces/run-…`` — otherwise just the file name. A host absolute path
    never appears in a served payload, so a remote GUI user does not learn the
    host filesystem layout. Keys and on-disk names are unchanged; only the
    served value is relative."""
    p = Path(path)
    try:
        return p.resolve().relative_to(Path(base).resolve()).as_posix()
    except (ValueError, OSError):
        return p.name


def redact_host_paths(text: str, *roots: str | Path) -> str:
    """Replace absolute path prefixes rooted at any of `roots` in free text
    (error messages, run-event log lines) with their root-relative remainder,
    so served payloads never carry the host filesystem layout (spec 32, G17).
    The full detail stays in coopd's own log; only the served copy is redacted.
    Roots are resolved to absolute form first (a relative root would otherwise
    redact unrelated text) and replaced longest-first so nested roots redact
    correctly."""
    out = text
    resolved = []
    for root in roots:
        try:
            r = Path(root).resolve()
        except OSError:
            continue
        if str(r) in ("/", ""):
            continue  # never redact against the filesystem root
        resolved.append(str(r))
    for rs in sorted(set(resolved), key=len, reverse=True):
        if rs not in out:
            continue
        out = out.replace(rs + "/", "").replace(rs, Path(rs).name or rs)
    return out
