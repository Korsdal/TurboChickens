"""Append-only JSONL Headcount (the audit log).

The log file is only ever opened in append mode — rows are never rewritten,
truncated, or deleted (retention default: forever, baseline v0.2 decision 3).
Every line is one JSON object carrying a UTC ``ts`` timestamp.

Spec 20 (lexicon adoption): rows are written with the farm event names
(``egg.queued``, ``clutch.stored``, ``abattoir.attested``, …). Rows written by
older versions keep their legacy names on disk forever — the append-only log
is never rewritten — so `LEGACY_EVENT_ALIASES` maps them to the lexicon names
on read/display only (`tail()` normalizes the event for display and matches
old and new names alike when filtering).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from turbochickens.util import utcnow_iso

#: legacy (pre-spec-20) event names → lexicon event names. Applied on
#: read/display only; rows on disk are never rewritten.
LEGACY_EVENT_ALIASES: dict[str, str] = {
    "run.queued": "egg.queued",
    "run.finished": "chicken.finished",
    "result.stored": "clutch.stored",
    "result.quarantined": "clutch.quarantined",
    "result.expired": "clutch.composted",
    "teardown.attested": "abattoir.attested",
    "schedule.added": "rooster.scheduled",
    "schedule.removed": "rooster.unscheduled",
    "schedule.fired": "rooster.crowed",
    "schedule.fire_failed": "rooster.missed",
    "job.uploaded": "egg.candled",
    "job.upload_rejected": "egg.rejected",
    "auth.login": "perch.claimed",
    "auth.logout": "perch.released",
    "auth.resolved": "perch.resolved",
    "daemon.started": "coopd.started",
    "daemon.stopped": "coopd.stopped",
}


def normalize_event(event: Any) -> Any:
    """Map a legacy event name to its lexicon name (identity for lexicon
    names and non-strings). Read/display only — never used when writing."""
    if isinstance(event, str):
        return LEGACY_EVENT_ALIASES.get(event, event)
    return event


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """Append one event row and return it (with its ts)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": utcnow_iso(), **event}
        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as f:  # append-only: never opened for write/truncate
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return row

    def read_all(self) -> list[dict[str, Any]]:
        """Read the whole log in order, raw rows exactly as written on disk
        (inspection/tests) — legacy event names are *not* aliased here."""
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def tail(self, limit: int, event: str | None = None) -> list[dict[str, Any]]:
        """Last `limit` rows, newest first, optionally filtered by event type
        (spec 13 `audit_tail`). Defensive read: blank or unparseable lines (e.g.
        a torn final line after a crash) are skipped, never fatal.

        Spec 20: display read — each row's `event` is normalized to its lexicon
        name (legacy rows included), and the `event` filter matches old and new
        names alike. The rows on disk are never rewritten."""
        rows: list[dict[str, Any]] = []
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event is None or normalize_event(row.get("event")) == normalize_event(event):
                        if "event" in row:
                            row = {**row, "event": normalize_event(row["event"])}
                        rows.append(row)
        return rows[-limit:][::-1] if limit > 0 else []
