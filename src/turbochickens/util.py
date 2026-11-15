"""Shared helpers."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """ISO-8601 UTC timestamp with millisecond precision, e.g. 2026-11-12T13:30:00.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def strip_control_chars(line: str) -> str:
    """Remove anything in Unicode category C* (control/format chars — ANSI/OSC
    escape sequences included), except tab/newline. Logs are data, never
    instructions: a hostile Feeder's bytes must never reach a terminal raw."""
    return "".join(
        ch for ch in line if ch in ("\t", "\n") or not unicodedata.category(ch).startswith("C")
    )


def sanitize_log_line(line: str, max_len: int | None = 4000) -> str:
    """Make a streamed log line safe to store: strip control/format characters
    (anything in Unicode category C*, except tab/newline) and cap the length
    (`max_len=None` strips only). Logs are data, never instructions."""
    cleaned = strip_control_chars(line)
    if max_len is not None and len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…[truncated]"
    return cleaned


#: Fallbacks used when a Feeder/OAuth error response carries no parsed `error`
#: field. The raw body is never repeated — it may echo the posted credentials
#: or smuggle terminal escape sequences (the Fox does not repeat untrusted
#: feeder bytes into errors, logs, or served payloads).
WITHHELD_NON_JSON_BODY = "non-JSON error body withheld by the fox"
WITHHELD_NO_ERROR_FIELD = "error body carried no parsed error field — withheld by the fox"


def http_error_detail(resp: Any, max_len: int = 200) -> str:
    """Extract a safe detail from a Feeder/OAuth HTTP error response: the
    parsed `error` field only (a string code, or a dict's `message`), run
    through the log sanitizer and capped. The raw response body (`resp.text`)
    is never returned — it is untrusted bytes that can echo the request's
    credentials (a token endpoint reflecting the refresh token) or carry
    ANSI/OSC escape sequences. Non-JSON bodies and bodies without a parsed
    error field yield a fixed withheld-marker string."""
    try:
        data = resp.json()
    except ValueError:
        return WITHHELD_NON_JSON_BODY
    err = data.get("error") if isinstance(data, dict) else None
    msg: str | None = None
    if isinstance(err, str) and err:
        msg = err
    elif isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
        msg = err["message"]
    if msg is None:
        return WITHHELD_NO_ERROR_FIELD
    return sanitize_log_line(msg, max_len)
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
