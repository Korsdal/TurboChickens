"""Append-only JSONL audit log.

The log file is only ever opened in append mode — rows are never rewritten,
truncated, or deleted (retention default: forever, baseline v0.2 decision 3).
Every line is one JSON object carrying a UTC ``ts`` timestamp.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from turbochickens.util import utcnow_iso


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
        """Read the whole log in order (inspection/tests)."""
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
