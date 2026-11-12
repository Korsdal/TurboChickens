"""Retention / TTL enforcement (spec 08, baseline v0.2 decision 3).

Settings-managed TTLs: the audit log is kept **forever** (`audit_retention`
only accepts ``"forever"`` — this module never touches `audit.jsonl`), and
result artifacts on disk expire after `results_retention_months` (default 6).

The SQLite result index is append-only by construction (storage triggers), so
expiry deletes the **on-disk artifacts** (`results/<run_id>/`, and
`quarantine/<run_id>/` for invalid output) while the `results` row stays as
the permanent index. Every deletion is itself audit-logged (`result.expired`).
"""

from __future__ import annotations

import calendar
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from turbochickens.audit import AuditLog
from turbochickens.settings import Settings
from turbochickens.util import utcnow


def parse_ts(ts: str) -> datetime:
    """Parse a utcnow_iso() timestamp (``2026-11-12T13:30:00.123Z``) to aware UTC."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def months_before(dt: datetime, months: int) -> datetime:
    """Calendar-month subtraction (2026-03-31 minus 1 month → 2026-02-28)."""
    y, m = dt.year, dt.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=day)


def sweep_expired_results(
    settings: Settings,
    db: sqlite3.Connection,
    audit: AuditLog,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete on-disk result artifacts whose run finished longer ago than
    `results_retention_months`; audit-log every deletion. The append-only
    `results` rows and the audit log itself are never modified or deleted.

    Returns a summary dict (expired run_ids + deleted count).
    """
    now = now or utcnow()
    cutoff = months_before(now, settings.results_retention_months)
    rows = db.execute(
        "SELECT res.run_id, res.valid, res.artifacts_dir, r.finished_at"
        " FROM results res JOIN runs r ON r.id = res.run_id"
        " WHERE r.finished_at IS NOT NULL AND res.artifacts_dir IS NOT NULL"
    ).fetchall()
    # safety: only ever delete paths inside the results/quarantine dirs
    allowed_roots = (
        settings.results_dir.resolve(),
        settings.quarantine_dir.resolve(),
    )
    expired: list[dict[str, Any]] = []
    deleted = 0
    for run_id, valid, artifacts_dir, finished_at in rows:
        if parse_ts(finished_at) >= cutoff:
            continue
        path = Path(artifacts_dir)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            continue  # never delete anything outside the result store dirs
        if not resolved.exists():
            continue  # already expired by an earlier sweep — nothing to delete
        shutil.rmtree(resolved, ignore_errors=True)
        if resolved.exists():
            continue  # removal failed; do not claim a deletion
        deleted += 1
        entry = {
            "run_id": run_id,
            "kind": "result" if valid else "quarantine",
            "artifacts_dir": str(resolved),
            "finished_at": finished_at,
            "retention_months": settings.results_retention_months,
        }
        expired.append(entry)
        audit.append({"event": "result.expired", **entry})
    return {
        "cutoff": cutoff.isoformat(),
        "results_retention_months": settings.results_retention_months,
        "expired": expired,
        "deleted": deleted,
    }
