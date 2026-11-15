"""Shared helpers."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
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
