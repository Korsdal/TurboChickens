"""The run pipeline: one queued run driven through its lifecycle.

queued → provisioning → running → collecting → finalizing → succeeded,
with running → failed on harness error / timeout and collecting → failed on
schema-validation failure, and every terminal state closed by a teardown
attestation plus audit rows (baseline v0.2 §5, §10).

The pipeline owns all SQLite writes for a run: status transitions on `runs`,
streamed `run_events`, the manifest, the `results` row (spec 04: valid output
stored, invalid output quarantined), and the `teardown_attestations` row.
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
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.settings import Settings
from turbochickens.spec.loader import load_spec
from turbochickens.spec.model import JobSpec
from turbochickens.stats import rollup_usage
from turbochickens.util import utcnow_iso
from turbochickens.validate import OutputValidation, validate_output

TERMINAL_STATES = ("succeeded", "failed")

OUTPUT_EVENT_MAX_LEN = 20_000


class RunPipeline:
    """Executes queued runs against the daemon's db/audit. One instance per
    daemon; DB writes are serialized through an asyncio lock."""

    def __init__(
        self,
        settings: Settings,
        db: sqlite3.Connection,
        audit: AuditLog,
        providers: ProvidersConfig | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self.providers = providers or load_providers(settings.providers_path, settings.data_dir)
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

    def _provision(self, ctx: RunContext) -> int:
        """Fresh per-run workspace with the job inputs copied in. Nothing is
        shared between runs; nothing from the ambient environment either.
        Returns the injected-context size in bytes (prompt + attachments;
        injected trajectory state is post-MVP) so the spec-07 context cap can
        be enforced at provisioning time."""
        ws = ctx.workspace
        ws.mkdir(parents=True, exist_ok=True)
        spec_dir = ctx.spec_path.parent
        prompt_src = spec_dir / ctx.spec.input.prompt_file
        shutil.copyfile(prompt_src, ws / "prompt.txt")
        injected = prompt_src.stat().st_size
        for att in ctx.spec.input.attachments:
            src = spec_dir / att
            shutil.copyfile(src, ws / Path(att).name)
            injected += src.stat().st_size
        return injected

    # --- result store + quarantine (spec 04) --------------------------------

    async def _store_result(self, run_id: str, outcome: OutputValidation) -> None:
        """Valid output enters the append-only result store: one `results` row
        plus on-disk artifacts keyed by run ID (baseline v0.2 §10.2, §12).
        Artifacts live under data_dir, not the workspace — the workspace is
        destroyed at teardown, the result survives."""
        artifacts_dir = self.settings.results_dir / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "output.json").write_text(
            json.dumps(outcome.output, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        async with self._db_lock:
            self.db.execute(
                "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
                " VALUES (?, 1, ?, ?, NULL)",
                (run_id, json.dumps(outcome.output, ensure_ascii=False), str(artifacts_dir)),
            )
            self.db.commit()
        self.audit.append(
            {"event": "result.stored", "run_id": run_id, "artifacts_dir": str(artifacts_dir)}
        )

    async def _quarantine(self, run_id: str, raw_output: str, schema_errors: list[str]) -> None:
        """Invalid output is quarantined: kept on disk with its schema errors
        recorded in a `results` row with valid=0 — never stored as valid, never
        discarded (baseline v0.2 §10.2)."""
        qdir = self.settings.quarantine_dir / run_id
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "raw_output.txt").write_text(raw_output, encoding="utf-8")
        (qdir / "schema_errors.json").write_text(
            json.dumps(schema_errors, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        async with self._db_lock:
            self.db.execute(
                "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
                " VALUES (?, 0, ?, ?, ?)",
                (run_id, raw_output, str(qdir), json.dumps(schema_errors, ensure_ascii=False)),
            )
            self.db.commit()
        self.audit.append(
            {
                "event": "result.quarantined",
                "run_id": run_id,
                "artifacts_dir": str(qdir),
                "schema_errors": schema_errors,
            }
        )

    # --- per-job usage stats rollup (spec 08) -------------------------------

    def _expected_cost_per_run(
        self, spec: JobSpec, usages: list[dict[str, Any]], mean_in: float, mean_out: float
    ) -> float | None:
        """Expected cost per run derived from `providers.toml` pricing for the
        job's model (cost is predicted telemetry, never a budget). If the
        model does not resolve (e.g. a vibe-cli harness model), fall back to
        the mean of the per-run `cost_usd` the harness itself reported."""
        try:
            _, _, _, profile = self.providers.resolve(spec.harness.model)
        except ProvidersError:
            costs = [float(u["cost_usd"]) for u in usages if isinstance(u.get("cost_usd"), (int, float))]
            return sum(costs) / len(costs) if costs else None
        return self.providers.cost_usd(profile, int(round(mean_in)), int(round(mean_out)))

    async def _rollup_usage_stats(self, job_id: str, spec: JobSpec) -> None:
        """Recompute the job's `job_usage_stats` row over all its finished runs
        (every finalized run carries usage_json). Mutable rollup table."""
        async with self._db_lock:
            rows = self.db.execute(
                "SELECT usage_json FROM runs WHERE job_id = ? AND usage_json IS NOT NULL", (job_id,)
            ).fetchall()
            usages = [json.loads(r[0]) for r in rows if r[0]]
            stats = rollup_usage(usages)
            expected_cost = self._expected_cost_per_run(
                spec, usages, stats["tokens_in_mean"] or 0.0, stats["tokens_out_mean"] or 0.0
            )
            self.db.execute(
                "INSERT OR REPLACE INTO job_usage_stats"
                " (job_id, n_runs, tokens_in_mean, tokens_out_mean, tokens_p95, expected_cost,"
                "  updated_at, tokens_in_p50, tokens_in_p95, tokens_out_p50, tokens_out_p95)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    stats["n_runs"],
                    stats["tokens_in_mean"],
                    stats["tokens_out_mean"],
                    stats["tokens_p95"],
                    expected_cost,
                    utcnow_iso(),
                    stats["tokens_in_p50"],
                    stats["tokens_in_p95"],
                    stats["tokens_out_p50"],
                    stats["tokens_out_p95"],
                ),
            )
            self.db.commit()

    # --- manifest ---------------------------------------------------------

    def _manifest(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        harness: dict[str, Any],
        result: HarnessResult | None,
        status: str,
        reason: str,
        budget: dict[str, Any],
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
            # spec 07: the enforced budget outcome — wall-clock timeout and
            # injected-context cap. Token usage stays telemetry (usage above),
            # never a budget.
            "budget": budget,
        }

    # --- the lifecycle ----------------------------------------------------

    async def execute(self, item: dict[str, Any]) -> None:
        """Drive one queued run to a terminal state, always with a teardown
        attestation and audit rows."""
        run_id = item["run_id"]
        spec = load_spec(item["spec_path"])
        adapter = make_adapter(spec, providers=self.providers)
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
        budget: dict[str, Any] = {
            "timeout_sec": spec.resources.timeout_sec,
            "max_context_mb": spec.resources.max_context_mb,
            "injected_context_bytes": None,
            "timed_out": False,
            "context_exceeded": False,
            "outcome": "ok",
        }
        try:
            await self._set_status(run_id, "provisioning")
            injected_bytes = self._provision(ctx)
            budget["injected_context_bytes"] = injected_bytes
            cap_bytes = spec.resources.max_context_mb * 1024 * 1024
            if injected_bytes > cap_bytes:
                # spec 07: hard cap on injected context, enforced at
                # provisioning — the run fails before any harness work
                budget["context_exceeded"] = True
                budget["outcome"] = "context_cap"
                status = "failed"
                reason = (
                    f"budget exceeded at provisioning: injected context {injected_bytes} bytes"
                    f" ({injected_bytes / 1024 / 1024:.3f} MB) > max_context_mb={spec.resources.max_context_mb}"
                    f" ({cap_bytes} bytes)"
                )
                await self._event(run_id, "supervisor", "error", reason)
            else:
                prepared = adapter.prepare(ctx)
                harness = prepared.harness
                await self._set_status(run_id, "running")
                result = await adapter.execute(
                    prepared,
                    lambda source, level, line: self._event(run_id, source, level, line),
                    timeout_sec=spec.resources.timeout_sec,
                )
            if result is not None and result.timed_out:
                budget["timed_out"] = True
                budget["outcome"] = "timeout"
                status, reason = "failed", f"timeout: hard-killed after {spec.resources.timeout_sec}s"
            elif result is not None and result.error:
                status, reason = "failed", result.error
            elif result is None:
                pass  # context-cap failure at provisioning; status/reason already set
            elif result.exit_code != 0:
                status, reason = "failed", f"harness exited rc={result.exit_code}"
            else:
                await self._set_status(run_id, "collecting")
                await self._event(
                    run_id, "harness", "output", sanitize_log_line(result.raw_output, OUTPUT_EVENT_MAX_LEN)
                )
                # spec 04: raw output is untrusted until it passes the job schema
                schema_path = ctx.spec_path.parent / ctx.spec.output.schema_file
                outcome = validate_output(result.raw_output, schema_path)
                if outcome.valid:
                    await self._store_result(run_id, outcome)
                    await self._event(
                        run_id, "validator", "info", f"output matches schema {ctx.spec.output.schema_file}"
                    )
                    await self._set_status(run_id, "finalizing")
                    status, reason = "succeeded", "ok"
                else:
                    await self._quarantine(run_id, result.raw_output, outcome.schema_errors)
                    await self._event(
                        run_id,
                        "validator",
                        "error",
                        sanitize_log_line("; ".join(outcome.schema_errors), OUTPUT_EVENT_MAX_LEN),
                    )
                    status, reason = "failed", f"output failed schema validation: {outcome.schema_errors[0]}"
        except Exception as e:
            status, reason = "failed", f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await self._event(run_id, "supervisor", "error", sanitize_log_line(str(e)))
        finally:
            await self._finalize(item, ctx, harness, result, status, reason, adapter, budget)

    async def _finalize(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        harness: dict[str, Any],
        result: HarnessResult | None,
        status: str,
        reason: str,
        adapter: Any,
        budget: dict[str, Any],
    ) -> None:
        """Manifest + terminal status + teardown attestation + audit rows.
        Runs for every path out of `execute`, including exceptions."""
        run_id = ctx.run_id
        manifest = self._manifest(item, ctx, harness, result, status, reason, budget)
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
        # spec 08: roll the run's token usage up into the per-job stats
        with contextlib.suppress(Exception):
            await self._rollup_usage_stats(ctx.job_id, ctx.spec)

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
