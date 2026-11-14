"""The Hatchery: one queued Egg driven through its lifecycle into a Chicken.

queued → provisioning → running → collecting → finalizing → succeeded,
with running → failed on harness error / timeout and collecting → failed on
candling failure, and every terminal state closed by an abattoir attestation
plus Headcount rows (baseline v0.2 §5, §10; spec 20 lexicon).

Spec 22 (lifecycle ops) adds two terminal states: a queued Egg can be
**cancelled** (`cancel_queued` — it never hatches, still attested) and a
running Chicken can be **stopped** by the operator (`request_stop` — the
harness process gets SIGTERM, then a hard kill after `STOP_TERM_GRACE_SEC`).
Both stop paths end in the same teardown attestation as any terminal state, and
`run_rerun` lineage lands in the manifest as `rerun_of`.

Spec 24 adds the optional Feed Budget cost cap (`resources.max_cost_usd`, USD,
off by default): enforced finalize-only — a chicken whose measured
`usage.cost_usd` exceeds the egg's cap fails with `budget.outcome = "cost_cap"`
(no clutch stored, telemetry still recorded, teardown attested as usual).

Spec 25 (chaining) lets an egg consume an earlier clutch:
`attachments = ["nestbox://<run_id>/output.json"]` is resolved at provisioning
from the Nest Box — valid clutches only; quarantined, composted, or missing →
the run fails at provisioning before `running` (attested teardown as usual).
Resolved clutch bytes count toward `max_context_mb` like any injected context,
and the manifest records the resolved sources (`chaining`) plus a
`clutch.consumed` Headcount row.

Spec 23 (Free-Range) adds `resources.free_range`: a Free-Range chicken has no
fence — the hard wall-clock kill is removed (`timeout_sec` is optional in the
egg, and never enforced as a kill for a free-range chicken) — so it dies only
via `run_stop` (spec 22) or daemon shutdown, both ending in the same attested
`stopped` teardown as any terminal state.

Spec 27 (ChickenSubs) adds `harness.spawn_subs`: such a chicken gets the
Farmhand attached to its vibe-cli runtime over stdio (the adapter writes the
MCP server entry into the run's vibe config and points the per-run env at a
token-free settings file), so it can enqueue sub-eggs. Sub-chickens are
ordinary queue items: the daemon records `parent_run_id` on the run (schema
v5), in the manifest, and in the `egg.queued` Headcount row; depth is capped
at 1 at enqueue.

The pipeline owns all SQLite writes for a chicken: status transitions on
`runs`, streamed `run_events`, the manifest, the `results` row (spec 04: a
valid clutch is stored in the nest box, an invalid one quarantined), and the
`teardown_attestations` row.
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
    auth_telemetry_of,
    make_adapter,
    sanitize_log_line,
)
from turbochickens.audit import AuditLog
from turbochickens.chaining import (
    NestboxError,
    attachment_workspace_name,
    parse_nestbox_uri,
    resolve_nestbox_clutch,
)
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.seat import SeatBroker
from turbochickens.settings import Settings
from turbochickens.spec.loader import load_spec
from turbochickens.spec.model import JobSpec
from turbochickens.stats import rollup_usage
from turbochickens.util import utcnow_iso
from turbochickens.validate import OutputValidation, validate_output

#: terminal states — every one requires a teardown attestation (baseline §5).
#: spec 22 adds `cancelled` (queued egg pulled from the pecking order) and
#: `stopped` (running chicken ended by the operator via `run_stop`).
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "stopped")

#: spec 22: grace between SIGTERM and the hard kill when stopping a chicken
STOP_TERM_GRACE_SEC = 5.0

OUTPUT_EVENT_MAX_LEN = 20_000


def cost_cap_exceeded(usage: dict[str, Any], max_cost_usd: float | None) -> bool:
    """Spec 24 pure seam: the finalize-only Feed Budget cost check. Returns
    True only when a cap is set AND the run's measured ``usage.cost_usd`` is
    strictly over it — at the cap exactly is allowed, no cap set means never.
    A missing/unmeasured cost can never trip a cap, and seat runs (spec 09)
    report ``cost_usd = 0.0``, which no positive cap can exceed. USD only —
    never a token count."""
    if max_cost_usd is None:
        return False
    cost = usage.get("cost_usd") if isinstance(usage, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return False
    return float(cost) > float(max_cost_usd)


class RunPipeline:
    """Executes queued runs against the daemon's db/audit. One instance per
    daemon; DB writes are serialized through an asyncio lock."""

    def __init__(
        self,
        settings: Settings,
        db: sqlite3.Connection,
        audit: AuditLog,
        providers: ProvidersConfig | None = None,
        credential_provider: Any = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.audit = audit
        self.providers = providers or load_providers(settings.providers_path, settings.data_dir)
        # spec 09: the credential broker behind the CredentialProvider seam —
        # seat providers get broker-minted tokens, api-key providers keep the
        # env-allowlist behavior
        self.credential_provider = credential_provider or SeatBroker(settings, self.providers)
        self._db_lock = asyncio.Lock()
        # spec 22: run_id → stop event for every chicken the pipeline is
        # actively driving; `request_stop` sets the event and the run ends
        # `stopped` (SIGTERM, then hard kill) instead of collecting output
        self._active: dict[str, asyncio.Event] = {}
        # spec 23: the active Free-Range chickens (no fence — they die only via
        # `run_stop` or daemon shutdown), so shutdown can ask them to stop
        self._active_free_range: set[str] = set()

    def request_stop(self, run_id: str) -> bool:
        """Spec 22: ask an active chicken to stop. Returns True when the run
        is in the pipeline right now (its stop event is set); False when the
        run is not active (queued, already terminal, or not yet started)."""
        event = self._active.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def request_stop_free_range(self) -> list[str]:
        """Spec 23: daemon shutdown asks every active Free-Range chicken to
        stop through the spec-22 stop path (SIGTERM, then hard kill → terminal
        `stopped` with the abattoir's attested teardown). Returns the run ids
        that were signalled."""
        return [run_id for run_id in sorted(self._active_free_range) if self.request_stop(run_id)]

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

    def _provision(self, ctx: RunContext) -> tuple[int, list[dict[str, Any]]]:
        """Fresh per-run workspace with the job inputs copied in. Nothing is
        shared between runs; nothing from the ambient environment either.

        Spec 25 (chaining): a `nestbox://<run_id>/output.json` attachment is
        resolved here from the Nest Box — valid clutches only; quarantined,
        composted, or missing clutches raise NestboxError and the run fails
        at provisioning. Returns the injected-context size in bytes (prompt +
        attachments, chained clutch bytes included — the spec-07 context cap
        cannot be bypassed by chaining) and the resolved chaining sources for
        the manifest (injected trajectory state is post-MVP)."""
        ws = ctx.workspace
        ws.mkdir(parents=True, exist_ok=True)
        spec_dir = ctx.spec_path.parent
        prompt_src = spec_dir / ctx.spec.input.prompt_file
        shutil.copyfile(prompt_src, ws / "prompt.txt")
        injected = prompt_src.stat().st_size
        chaining: list[dict[str, Any]] = []
        try:
            for att in ctx.spec.input.attachments:
                if parse_nestbox_uri(att) is not None:
                    # spec 25: fetch the referenced chicken's clutch from the
                    # nest box and inject its bytes into the workspace
                    clutch = resolve_nestbox_clutch(att, self.db)
                    name = attachment_workspace_name(att)
                    (ws / name).write_bytes(clutch.data)
                    injected += len(clutch.data)
                    chaining.append(
                        {
                            "attachment": att,
                            "run_id": clutch.run_id,
                            "artifact_path": clutch.artifact_path,
                            "size_bytes": len(clutch.data),
                        }
                    )
                else:
                    src = spec_dir / att
                    shutil.copyfile(src, ws / attachment_workspace_name(att))
                    injected += src.stat().st_size
        except NestboxError as e:
            e.injected_bytes = injected
            raise
        return injected, chaining

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
            {"event": "clutch.stored", "run_id": run_id, "artifacts_dir": str(artifacts_dir)}
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
                "event": "clutch.quarantined",
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
        job's model (cost is predicted telemetry, never a budget). Seat-auth
        providers (spec 09) bill via the seat, not per token → zero, never a
        fabricated per-token price. If the model does not resolve (e.g. a
        vibe-cli harness model), fall back to the mean of the per-run
        `cost_usd` the harness itself reported."""
        try:
            _, _, provider, profile = self.providers.resolve(spec.harness.model)
        except ProvidersError:
            costs = [float(u["cost_usd"]) for u in usages if isinstance(u.get("cost_usd"), (int, float))]
            return sum(costs) / len(costs) if costs else None
        if provider.auth == "seat":
            return 0.0
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
        auth: dict[str, Any],
        chaining: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        spec = ctx.spec
        manifest: dict[str, Any] = {
            "run_id": ctx.run_id,
            "job_id": ctx.job_id,
            "job_name": spec.job.name,
            "spec_hash": f"sha256:{ctx.spec_hash}",
            "trigger": ctx.trigger,
            "harness": harness,
            # spec 09: non-secret auth telemetry — mode + token expiry only
            "auth": auth,
            "resources": {
                # spec 23: Free-Range flag mirrored in the manifest
                "free_range": spec.resources.free_range,
                "timeout_sec": spec.resources.timeout_sec,
                "max_context_mb": spec.resources.max_context_mb,
                "max_cost_usd": spec.resources.max_cost_usd,
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
            # never a budget. Spec 24 adds the optional USD cost cap
            # (`max_cost_usd`), enforced finalize-only.
            "budget": budget,
        }
        # spec 25: chaining lineage — the nestbox:// sources this chicken
        # consumed at provisioning (run id + artifact path + bytes)
        if chaining:
            manifest["chaining"] = chaining
        # spec 22: re-run lineage — this chicken was enqueued by `run_rerun`
        # from another run of the same egg
        if item.get("rerun_of"):
            manifest["rerun_of"] = item["rerun_of"]
        # spec 27: sub-chicken lineage — this chicken was enqueued through a
        # spawn_subs chicken's Farmhand
        if item.get("parent_run_id"):
            manifest["parent_run_id"] = item["parent_run_id"]
        return manifest

    # --- the lifecycle ----------------------------------------------------

    async def execute(self, item: dict[str, Any]) -> None:
        """Drive one queued run to a terminal state, always with a teardown
        attestation and audit rows."""
        run_id = item["run_id"]
        spec = load_spec(item["spec_path"])
        adapter = make_adapter(
            spec, credential_provider=self.credential_provider, providers=self.providers, settings=self.settings
        )
        ctx = RunContext(
            run_id=run_id,
            job_id=item["job_id"],
            spec=spec,
            spec_path=Path(item["spec_path"]),
            spec_hash=item["spec_hash"],
            trigger=item["trigger"],
            workspace=self.settings.data_dir / "workspaces" / run_id,
            # spec 27 (ChickenSubs): sub-chicken lineage
            parent_run_id=item.get("parent_run_id"),
        )
        status = "failed"
        reason = "internal error"
        result: HarnessResult | None = None
        harness: dict[str, Any] = {
            "adapter": spec.harness.adapter,
            "model": spec.harness.model,
            "temperature": spec.harness.temperature,
            # spec 27: mirrored in the manifest
            "spawn_subs": spec.harness.spawn_subs,
        }
        budget: dict[str, Any] = {
            # spec 23: a Free-Range chicken has no fence — timeout_sec is the
            # declared value (possibly None) and no hard kill is enforced
            "free_range": spec.resources.free_range,
            "timeout_sec": spec.resources.timeout_sec,
            "max_context_mb": spec.resources.max_context_mb,
            "max_cost_usd": spec.resources.max_cost_usd,
            "injected_context_bytes": None,
            "timed_out": False,
            "context_exceeded": False,
            "cost_exceeded": False,
            "outcome": "ok",
        }
        # spec 23: the enforced wall-clock budget — None for a Free-Range
        # chicken, which dies only via run_stop or daemon shutdown (both
        # attested), never by the clock
        hard_timeout: int | None = None if spec.resources.free_range else spec.resources.timeout_sec
        if spec.resources.free_range:
            self._active_free_range.add(run_id)
        # spec 09: auth telemetry for the manifest/audit — mode + token expiry,
        # never a token. Refreshed from the credential provider when available.
        auth: dict[str, Any] = auth_telemetry_of(self.credential_provider, ctx)
        # spec 22: operator stop — `run_stop` sets this event; a running chicken
        # then gets SIGTERM + hard kill and ends `stopped` instead of collecting
        stop_event = asyncio.Event()
        self._active[run_id] = stop_event
        stopped_reason: str | None = None
        # spec 25: resolved nestbox:// chaining sources, recorded in the manifest
        chaining: list[dict[str, Any]] = []
        try:
            await self._set_status(run_id, "provisioning")
            # spec 20: the Hatchery starts turning the Egg into a Chicken
            self.audit.append(
                {
                    "event": "egg.hatched",
                    "run_id": run_id,
                    "job_id": ctx.job_id,
                    "spec_hash": f"sha256:{ctx.spec_hash}",
                    "trigger": ctx.trigger,
                }
            )
            provision_error: str | None = None
            try:
                injected_bytes, chaining = self._provision(ctx)
                budget["injected_context_bytes"] = injected_bytes
                # spec 25: lineage in the Headcount + run log — this chicken
                # consumed an earlier clutch from the nest box
                for c in chaining:
                    self.audit.append(
                        {
                            "event": "clutch.consumed",
                            "run_id": run_id,
                            "source_run_id": c["run_id"],
                            "attachment": c["attachment"],
                            "artifact": c["artifact_path"],
                        }
                    )
                    await self._event(
                        run_id,
                        "supervisor",
                        "info",
                        f"chained clutch from chicken {c['run_id']}: {c['attachment']}"
                        f" → {attachment_workspace_name(c['attachment'])} ({c['size_bytes']} bytes)",
                    )
            except NestboxError as e:
                # spec 25 fail-closed: a quarantined / composted / missing /
                # malformed clutch fails the run at provisioning, before any
                # harness work — attested teardown as usual
                provision_error = str(e)
                budget["injected_context_bytes"] = e.injected_bytes
                status, reason = "failed", f"provisioning failed: {e}"
                await self._event(run_id, "supervisor", "error", sanitize_log_line(reason))
            cap_bytes = spec.resources.max_context_mb * 1024 * 1024
            if provision_error is not None:
                pass  # chaining resolution failed — status/reason set above
            elif injected_bytes > cap_bytes:
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
                auth = prepared.auth or auth
                if isinstance(harness.get("farmhand"), dict):
                    # spec 27 (ChickenSubs): the Farmhand is attached to this
                    # chicken's harness runtime over stdio
                    await self._event(
                        run_id,
                        "supervisor",
                        "info",
                        f"farmhand attached to the harness runtime over stdio"
                        f" (spawn_subs=true, config={harness['farmhand'].get('config')})",
                    )
                # audit the mode per chicken (non-secret: mode + expiry only)
                self.audit.append(
                    {
                        "event": "perch.resolved",
                        "run_id": run_id,
                        "job_id": ctx.job_id,
                        "mode": auth.get("mode"),
                        "token_expires_at": auth.get("token_expires_at"),
                    }
                )
                if stop_event.is_set():
                    # spec 22: stopped while provisioning — the harness never starts
                    stopped_reason = "stopped by operator before the harness started"
                    result = None
                    await self._event(run_id, "supervisor", "info", stopped_reason)
                else:
                    await self._set_status(run_id, "running")
                    result, stopped_reason = await self._execute_harness(
                        adapter, prepared, ctx, stop_event, hard_timeout
                    )
            if stopped_reason is not None:
                status, reason = "stopped", stopped_reason
            elif result is not None and result.timed_out:
                budget["timed_out"] = True
                budget["outcome"] = "timeout"
                status, reason = "failed", f"timeout: hard-killed after {hard_timeout}s"
            elif result is not None and result.error:
                status, reason = "failed", result.error
            elif result is None:
                pass  # provisioning failure (context cap or chained clutch) — status/reason already set
            elif result.exit_code != 0:
                status, reason = "failed", f"harness exited rc={result.exit_code}"
            else:
                await self._set_status(run_id, "collecting")
                await self._event(
                    run_id, "harness", "output", sanitize_log_line(result.raw_output, OUTPUT_EVENT_MAX_LEN)
                )
                if cost_cap_exceeded(result.usage, spec.resources.max_cost_usd):
                    # spec 24: finalize-only Feed Budget cost cap — the chicken
                    # ran to completion, but its measured cost exceeded the
                    # egg's cap, so the run fails here at finalize (no clutch
                    # stored, like a timeout); telemetry stays recorded
                    budget["cost_exceeded"] = True
                    budget["outcome"] = "cost_cap"
                    cost = float(result.usage.get("cost_usd") or 0.0)
                    status, reason = "failed", (
                        f"feed budget exceeded at finalize: measured cost_usd={cost:.6g}"
                        f" > max_cost_usd={spec.resources.max_cost_usd:.6g}"
                    )
                    await self._event(run_id, "supervisor", "error", reason)
                else:
                    # spec 04: raw output is untrusted until it passes the job schema
                    schema_path = ctx.spec_path.parent / ctx.spec.output.schema_file
                    outcome = validate_output(result.raw_output, schema_path)
                    if outcome.valid:
                        await self._store_result(run_id, outcome)
                        await self._event(
                            run_id, "validator", "info", f"clutch matches schema {ctx.spec.output.schema_file}"
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
                        status, reason = "failed", f"clutch failed candling (schema validation): {outcome.schema_errors[0]}"
        except Exception as e:
            status, reason = "failed", f"{type(e).__name__}: {e}"
            with contextlib.suppress(Exception):
                await self._event(run_id, "supervisor", "error", sanitize_log_line(str(e)))
        finally:
            self._active.pop(run_id, None)
            self._active_free_range.discard(run_id)
            await self._finalize(item, ctx, harness, result, status, reason, adapter, budget, auth, chaining)

    async def _execute_harness(
        self,
        adapter: Any,
        prepared: PreparedRun,
        ctx: RunContext,
        stop_event: asyncio.Event,
        timeout_sec: int | None,
    ) -> tuple[HarnessResult, str | None]:
        """Spec 22: run the harness to completion, racing an operator stop.
        When `run_stop` fires mid-run, the harness process gets SIGTERM first
        and a hard kill after `STOP_TERM_GRACE_SEC`; the run then ends
        `stopped` and its output is never collected or candled. Returns
        (harness result, stopped reason or None). `timeout_sec=None` (spec 23,
        Free-Range) means no wall-clock kill — only the stop event ends it."""
        run_id = ctx.run_id

        async def sink(source: str, level: str, line: str) -> None:
            await self._event(run_id, source, level, line)

        exec_task = asyncio.create_task(adapter.execute(prepared, sink, timeout_sec=timeout_sec))
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait({exec_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
        if stop_event.is_set() and not exec_task.done():
            proc = ctx.proc
            if proc is not None and proc.returncode is None:
                # SIGTERM first, then the hard kill after the grace period
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                hard_killed = False
                try:
                    await asyncio.wait_for(proc.wait(), timeout=STOP_TERM_GRACE_SEC)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    hard_killed = True
                    with contextlib.suppress(Exception):
                        await proc.wait()
                reason = (
                    f"stopped by operator: SIGTERM, then hard kill after {STOP_TERM_GRACE_SEC:g}s grace"
                    if hard_killed
                    else f"stopped by operator: SIGTERM (reaped within {STOP_TERM_GRACE_SEC:g}s grace)"
                )
                result = await exec_task  # completes now that the process is dead
            else:
                # no live subprocess to signal (e.g. an HTTP adapter) — cancel
                # the in-flight call itself
                exec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await exec_task
                result = HarnessResult(exit_code=None, raw_output="", usage={}, error="stopped by operator")
                reason = "stopped by operator (in-flight harness call cancelled)"
            return result, reason
        result = await exec_task
        return result, None

    async def cancel_queued(self, item: dict[str, Any]) -> None:
        """Spec 22: terminal-cancel an egg that was still in the pecking order.
        It never hatches (no harness events, no `egg.hatched`), but it still
        gets a manifest and the abattoir's attestation exactly like any
        terminal state — nothing was spawned or provisioned, so the
        attestation verifies that (1, 1, 1)."""
        run_id = item["run_id"]
        reason = "cancelled while queued — the egg never hatched"
        now = utcnow_iso()
        spec = load_spec(item["spec_path"])
        manifest: dict[str, Any] = {
            "run_id": run_id,
            "job_id": item["job_id"],
            "job_name": spec.job.name,
            "spec_hash": f"sha256:{item['spec_hash']}",
            "trigger": item["trigger"],
            "harness": {
                "adapter": spec.harness.adapter,
                "model": spec.harness.model,
                "temperature": spec.harness.temperature,
                # spec 27: mirrored in the manifest
                "spawn_subs": spec.harness.spawn_subs,
            },
            "started_at": item.get("started_at"),
            "finished_at": now,
            "usage": {"tokens_in": 0, "tokens_out": 0},
            "status": "cancelled",
            "status_reason": reason,
            "timed_out": False,
        }
        if item.get("rerun_of"):
            manifest["rerun_of"] = item["rerun_of"]
        # spec 27: sub-chicken lineage
        if item.get("parent_run_id"):
            manifest["parent_run_id"] = item["parent_run_id"]
        # nothing was provisioned for a queued egg — verify anyway and attest
        ws = self.settings.data_dir / "workspaces" / run_id
        workspace_removed = True
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
            workspace_removed = not ws.exists()
        async with self._db_lock:
            row = self.db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None or row[0] in TERMINAL_STATES:
                return  # lost a race with a real terminal state — never overwrite
            self.db.execute(
                "UPDATE runs SET status = 'cancelled', finished_at = ?, manifest_json = ?, usage_json = ? WHERE id = ?",
                (now, json.dumps(manifest), json.dumps(manifest["usage"]), run_id),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO teardown_attestations"
                " (run_id, process_reaped, workspace_removed, env_cleared, verified_at)"
                " VALUES (?, 1, ?, 1, ?)",
                (run_id, int(workspace_removed), now),
            )
            self.db.commit()
        self.audit.append(
            {
                "event": "egg.cancelled",
                "run_id": run_id,
                "job_id": item["job_id"],
                "spec_hash": f"sha256:{item['spec_hash']}",
                "trigger": item["trigger"],
                "reason": reason,
            }
        )
        self.audit.append(
            {
                "event": "abattoir.attested",
                "run_id": run_id,
                "process_reaped": True,
                "workspace_removed": workspace_removed,
                "env_cleared": True,
            }
        )
        self.audit.append(
            {
                "event": "chicken.finished",
                "run_id": run_id,
                "job_id": item["job_id"],
                "status": "cancelled",
                "reason": reason,
            }
        )
        await self._event(run_id, "supervisor", "info", f"status → cancelled: {reason}")

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
        auth: dict[str, Any],
        chaining: list[dict[str, Any]] | None = None,
    ) -> None:
        """Manifest + terminal status + teardown attestation + audit rows.
        Runs for every path out of `execute`, including exceptions."""
        run_id = ctx.run_id
        manifest = self._manifest(item, ctx, harness, result, status, reason, budget, auth, chaining)
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
        # spec 22: a chicken ended deliberately by the operator gets its own
        # Headcount row on top of the usual attestation + terminal rows
        if status == "stopped":
            self.audit.append(
                {
                    "event": "chicken.stopped",
                    "run_id": run_id,
                    "job_id": ctx.job_id,
                    "reason": reason,
                }
            )
        self.audit.append(
            {
                "event": "abattoir.attested",
                "run_id": run_id,
                "process_reaped": report.process_reaped,
                "workspace_removed": report.workspace_removed,
                "env_cleared": report.env_cleared,
            }
        )
        self.audit.append(
            {
                "event": "chicken.finished",
                "run_id": run_id,
                "job_id": ctx.job_id,
                "status": status,
                "reason": reason,
                "auth_mode": (auth or {}).get("mode"),
            }
        )
        await self._event(run_id, "supervisor", "info", f"chicken {status}: {reason}")
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
            {"event": "chicken.finished", "run_id": run_id, "status": "failed", "reason": "internal error (guard)"}
        )
