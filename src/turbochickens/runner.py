"""The run pipeline: one queued run driven through its lifecycle.

queued → provisioning → running → collecting → finalizing → succeeded,
with running → failed on harness error / timeout, and every terminal state
closed by a teardown attestation plus audit rows (baseline v0.2 §5, §10).

The pipeline owns all SQLite writes for a run: status transitions on `runs`,
streamed `run_events`, the manifest, and the `teardown_attestations` row.
Schema validation of the raw output is spec 04 and plugs in at `collecting`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from turbochickens.adapter import (
    HarnessResult,
    RunContext,
    TeardownReport,
    make_adapter,
    sanitize_log_line,
)
from turbochickens.audit import AuditLog
from turbochickens.settings import Settings
from turbochickens.spec.loader import load_spec
from turbochickens.util import utcnow_iso

TERMINAL_STATES = ("succeeded", "failed")

OUTPUT_EVENT_MAX_LEN = 20_000


class RunPipeline:
    """Executes queued runs against the daemon's db/audit. One instance per
    daemon; DB writes are serialized through an asyncio lock."""

    def __init__(self, settings: Settings, db: sqlite3.Connection, audit: AuditLog) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self._db_lock = asyncio.Lock()

    # --- db helpers -------------------------------------------------------

    async def _event(self, run_id: str, source: str, level: str, line: str) -> None:
        async with self._db_lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            self.db.execute(
                "INSERT INTO run_events (run_id, seq, ts, level, source, line) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, row[0], utcnow_iso(), level, source, line),
            )
            self.db.commit()

    async def _set_status(self, run_id: str, status: str) -> None:
        async with self._db_lock:
            self.db.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
            self.db.commit()
        await self._event(run_id, "supervisor", "info", f"status → {status}")

    # --- provisioning -----------------------------------------------------

    def _provision(self, ctx: RunContext) -> None:
        """Fresh per-run workspace with the job inputs copied in. Nothing is
        shared between runs; nothing from the ambient environment either."""
        ws = ctx.workspace
        ws.mkdir(parents=True, exist_ok=True)
        spec_dir = ctx.spec_path.parent
        shutil.copyfile(spec_dir / ctx.spec.input.prompt_file, ws / "prompt.txt")
        for att in ctx.spec.input.attachments:
            shutil.copyfile(spec_dir / att, ws / Path(att).name)

    # --- manifest ---------------------------------------------------------

    def _manifest(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        harness: dict[str, Any],
        result: HarnessResult | None,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        spec = ctx.spec
        return {
            "run_id": ctx.run_id,
            "job_id": ctx.job_id,
            "job_name": spec.job.name,
            "spec_hash": f"sha256:{ctx.spec_hash}",
            "trigger": ctx.trigger,
            "harness": harness,
            "resources": {
                "timeout_sec": spec.resources.timeout_sec,
                "max_context_mb": spec.resources.max_context_mb,
                "network": spec.resources.network,
            },
            "input": {
                "prompt_file": spec.input.prompt_file,
                "attachments": list(spec.input.attachments),
            },
            "started_at": item.get("started_at"),
            "finished_at": utcnow_iso(),
            "usage": result.usage if result else {"tokens_in": 0, "tokens_out": 0},
            "workspace": str(ctx.workspace),
            "env_keys": sorted(ctx.env.keys()),
            "status": status,
            "status_reason": reason,
            "timed_out": bool(result and result.timed_out),
        }

    # --- the lifecycle ----------------------------------------------------

    async def execute(self, item: dict[str, Any]) -> None:
        """Drive one queued run to a terminal state, always with a teardown
        attestation and audit rows."""
        run_id = item["run_id"]
        spec = load_spec(item["spec_path"])
        adapter = make_adapter(spec)
        ctx = RunContext(
            run_id=run_id,
            job_id=item["job_id"],
            spec=spec,
            spec_path=Path(item["spec_path"]),
            spec_hash=item["spec_hash"],
            trigger=item["trigger"],
            workspace=self.settings.data_dir / "workspaces" / run_id,
        )
        status = "failed"
        reason = "internal error"
        result: HarnessResult | None = None
        harness: dict[str, Any] = {
            "adapter": spec.harness.adapter,
            "model": spec.harness.model,
            "temperature": spec.harness.temperature,
        }
        try:
            await self._set_status(run_id, "provisioning")
            self._provision(ctx)
            prepared = adapter.prepare(ctx)
            harness = prepared.harness
            await self._set_status(run_id, "running")
            result = await adapter.execute(
                prepared,
                lambda source, level, line: self._event(run_id, source, level, line),
                timeout_sec=spec.resources.timeout_sec,
            )
            if result.timed_out:
                status, reason = "failed", f"timeout: hard-killed after {spec.resources.timeout_sec}s"
            elif result.error:
                status, reason = "failed", result.error
            elif result.exit_code != 0:
                status, reason = "failed", f"harness exited rc={result.exit_code}"
            else:
                await self._set_status(run_id, "collecting")
                await self._event(
                    run_id, "harness", "output", sanitize_log_line(result.raw_output, OUTPUT_EVENT_MAX_LEN)
                )
                await self._set_status(run_id, "finalizing")
                status, reason = "succeeded", "ok"
        except Exception as e:
            status, reason = "failed", f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await self._event(run_id, "supervisor", "error", sanitize_log_line(str(e)))
        finally:
            await self._finalize(item, ctx, harness, result, status, reason, adapter)

    async def _finalize(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        harness: dict[str, Any],
        result: HarnessResult | None,
        status: str,
        reason: str,
        adapter: Any,
    ) -> None:
        """Manifest + terminal status + teardown attestation + audit rows.
        Runs for every path out of `execute`, including exceptions."""
        run_id = ctx.run_id
        manifest = self._manifest(item, ctx, harness, result, status, reason)
        # teardown first: destruction is verified, not assumed
        try:
            report: TeardownReport = await adapter.teardown(ctx)
        except Exception as e:
            report = TeardownReport(process_reaped=False, workspace_removed=False, env_cleared=False)
            with contextlib.suppress(Exception):
                await self._event(run_id, "supervisor", "error", f"teardown error: {e}")
        async with self._db_lock:
            self.db.execute(
                "UPDATE runs SET status = ?, finished_at = ?, manifest_json = ?, usage_json = ? WHERE id = ?",
                (status, manifest["finished_at"], json.dumps(manifest), json.dumps(manifest["usage"]), run_id),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO teardown_attestations"
                " (run_id, process_reaped, workspace_removed, env_cleared, verified_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    int(report.process_reaped),
                    int(report.workspace_removed),
                    int(report.env_cleared),
                    utcnow_iso(),
                ),
            )
            self.db.commit()
        self.audit.append(
            {
                "event": "teardown.attested",
                "run_id": run_id,
                "process_reaped": report.process_reaped,
                "workspace_removed": report.workspace_removed,
                "env_cleared": report.env_cleared,
            }
        )
        self.audit.append({"event": "run.finished", "run_id": run_id, "job_id": ctx.job_id, "status": status, "reason": reason})
        await self._event(run_id, "supervisor", "info", f"run {status}: {reason}")

    async def execute_guard(self, run_id: str) -> None:
        """Last-resort guard for a run whose execute() itself blew up: make it
        terminal and record an attestation (with unverified flags, which the
        daemon surfaces as an alert)."""
        async with self._db_lock:
            row = self.db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return
            if row[0] not in TERMINAL_STATES:
                self.db.execute(
                    "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?",
                    (utcnow_iso(), run_id),
                )
            self.db.execute(
                "INSERT OR IGNORE INTO teardown_attestations"
                " (run_id, process_reaped, workspace_removed, env_cleared, verified_at)"
                " VALUES (?, 0, 0, 0, ?)",
                (run_id, utcnow_iso()),
            )
            self.db.commit()
        self.audit.append(
            {"event": "run.finished", "run_id": run_id, "status": "failed", "reason": "internal error (guard)"}
        )
