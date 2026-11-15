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
wall-clock cap — the hard wall-clock kill is removed (`timeout_sec` is optional in the
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

Spec 28 (connectors registry) adds `harness.connectors`: a list of gate names
from `connectors.toml` the chicken opts into. The daemon refuses unknown names
at enqueue (field-named `harness.connectors` error) and the vibe-cli adapter
injects the chosen gates into the run's vibe config (`[mcp_servers.NAME]`
entries, merged with a Farmhand entry when both are present) — env/header refs
are resolved against the daemon environment only into the ephemeral workspace
config, never into any payload, log, audit row, or manifest (the manifest
carries names only).

The pipeline owns all SQLite writes for a chicken: status transitions on
`runs`, streamed `run_events`, the manifest, the `results` row (spec 04: a
valid clutch is stored in the nest box, an invalid one quarantined), and the
`teardown_attestations` row.

Spec 32 (ticket 10, review finding G8) records each Chicken's pid at
hatch/spawn: the adapter's spawn hook writes the harness process pid into the
pipeline's shared pid registry (chicken pid → run id) and the manifest's
harness block carries `pid`. The daemon reads SO_PEERCRED on every socket
connection and resolves the peer pid (the chicken itself or one of its
descendants) to the owning run through that registry, so lineage
(`parent_run_id` / `caller_run_id`) is forced from the authenticated peer —
client-sent lineage fields are ignored on chicken-owned connections.

Spec 32 ticket 12 (review G9, spawn isolation) provisions the chicken's
Scratch: `adapter.prepare` materializes `.tc-home/` inside the workspace and
points the per-run HOME/TMPDIR/XDG_*/VIBE_HOME at it, with only a sanitized
vibe profile in it — nothing the chicken plants in its "home" survives the
abattoir's teardown, and the operator's real $HOME stays out of reach.

Spec 32 ticket 21 (review finding G18) re-verifies the spec hash at hatch:
the hash pinned when the egg was queued is recomputed when the egg is loaded
at hatch, and a mismatch (an egg swapped between enqueue and hatch) fails
closed — the run ends terminal `failed` with the abattoir's attestation, and
nothing is provisioned or sent to the feeder under the stale hash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import signal
import sqlite3
from pathlib import Path
from typing import Any

from turbochickens.adapter import (
    SCRATCH_HOME_DIRNAME,
    HarnessResult,
    PreparedRun,
    RunContext,
    TeardownReport,
    auth_telemetry_of,
    make_adapter,
    reap_process_group,
    signal_process_group,
)
from turbochickens import fox
from turbochickens.audit import AuditLog
from turbochickens.chaining import (
    NestboxError,
    attachment_workspace_name,
    parse_nestbox_uri,
    resolve_nestbox_clutch,
)
from turbochickens.fence import UnsafeRefError, contained_ref
from turbochickens.providers import ProvidersConfig, ProvidersError, load_providers
from turbochickens.seat import SeatBroker
from turbochickens.settings import (
    MAX_RUN_EVENTS,
    OUTPUT_MAX_BYTES,
    RUN_EVENT_LINE_MAX_LEN,
    Settings,
)
from turbochickens.spec.loader import load_spec, spec_hash
from turbochickens.spec.model import JobSpec
from turbochickens.stats import rollup_usage
from turbochickens.util import cap_text_bytes, display_relpath, redact_host_paths, sanitize_log_line, utcnow_iso
from turbochickens.validate import OutputValidation, validate_output

#: coopd's own log (stderr of the daemon process) — the full, unredacted detail
#: of a failure stays here; only the redacted form is ever served (spec 32, G17)
_log = logging.getLogger("turbochickens.coopd")

#: terminal states — every one requires a teardown attestation (baseline §5).
#: spec 22 adds `cancelled` (queued egg pulled from the pecking order) and
#: `stopped` (running chicken ended by the operator via `run_stop`).
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "stopped")

#: spec 22: grace between SIGTERM and the hard kill when stopping a chicken
STOP_TERM_GRACE_SEC = 5.0

#: spec 32 (review finding G15): cap on the post-kill `await` of the harness
#: task. The chicken runs in its own session and is killed as a whole process
#: group, so the task normally completes right away; if a pipe-holding
#: escapee keeps the pipes open past the kill, the bounded await times out and
#: a synthetic harness result is produced so teardown always completes and
#: the run always reaches a terminal state with its attestation.
POST_KILL_AWAIT_SEC = 5.0

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


class _HatchHashMismatch(Exception):
    """Spec 32 ticket 21 (review G18): the egg loaded at hatch no longer
    candles to the hash pinned at enqueue — the egg was swapped in between."""


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
        pid_registry: dict[int, str] | None = None,
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
        # spec 23: the active Free-Range chickens (no wall-clock cap — they die
        # only via `run_stop` or daemon shutdown), so shutdown can ask them to stop
        self._active_free_range: set[str] = set()
        # spec 32 (ticket 10): chicken pid → run_id registry shared with the
        # daemon — the daemon reads SO_PEERCRED on every socket connection and
        # resolves the peer pid (a chicken or one of its descendants) to the
        # owning run, so lineage is forced from the peer, never client-sent
        self._pid_registry = pid_registry
        # spec 32 (G17): run_id → the path roots redacted from every stored
        # run-event line of that chicken (the data dir + the egg's own dir) —
        # harness-streamed lines can echo the chicken's absolute HOME (the
        # Scratch inside its workspace), so the redaction happens at the
        # single _event choke point, not per call site
        self._run_path_roots: dict[str, tuple[Path, ...]] = {}

    def _register_chicken_pid(self, run_id: str, pid: int) -> None:
        """Spec 32 (ticket 10): a chicken's harness process pid is registered
        at spawn (called from the adapter's spawn hook) so the daemon can
        attribute socket connections from that pid (or its descendants) to
        this run."""
        if self._pid_registry is not None:
            self._pid_registry[pid] = run_id

    def _unregister_chicken_pids(self, run_id: str) -> None:
        if self._pid_registry is None:
            return
        for pid, rid in list(self._pid_registry.items()):
            if rid == run_id:
                del self._pid_registry[pid]

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

    def _path_roots(self, ctx: RunContext) -> tuple[Path, ...]:
        """Spec 32 (G17): the roots redacted from served text — the data dir
        (workspaces, uploads, results all live under it) and the egg's own
        directory (a CLI-run egg may live anywhere on the host)."""
        return (Path(self.settings.data_dir), Path(ctx.spec_path).parent)

    # --- db helpers -------------------------------------------------------

    async def _event(self, run_id: str, source: str, level: str, line: str) -> None:
        # G10: every streamed run-event line passes through the log sanitizer
        # at this single choke point — supervisor-authored lines included — so
        # a hostile Feeder's error bytes can never reach `tc runs logs` raw
        # (no ANSI/OSC escape sequences).
        # spec 32 (G17): the same choke point also redacts host paths — a
        # harness-streamed line can echo the chicken's absolute HOME (the
        # Scratch inside its workspace) or the egg's host directory, and none
        # of that may reach runs_logs
        roots = self._run_path_roots.get(run_id)
        if roots:
            line = redact_host_paths(line, *roots)
        # spec 32 (G20, the Fence): the line is also length-capped here, and
        # the per-chicken run-event log itself is capped at MAX_RUN_EVENTS
        # rows (the table is append-only, so the cap is enforced at insert —
        # a spamming chicken cannot grow the DB without bound; `runs_logs`
        # reports `capped=true` and serves the bounded tail)
        line = sanitize_log_line(line, RUN_EVENT_LINE_MAX_LEN)
        async with self._db_lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1, COUNT(*) FROM run_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            seq, count = row[0], row[1]
            if count >= MAX_RUN_EVENTS:
                return
            self.db.execute(
                "INSERT INTO run_events (run_id, seq, ts, level, source, line) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, seq, utcnow_iso(), level, source, line),
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
        # spec 32 (G11, the Fox): workspaces are created restrictive — the
        # workspaces root and the per-run workspace are both 0700 (also
        # tightening pre-existing looser dirs)
        fox.ensure_dir(ws.parent)
        fox.ensure_dir(ws)
        spec_dir = ctx.spec_path.parent
        # spec 32 (G1): defensive re-check at the fence BEFORE any file is
        # copied or read — Candling already enforced containment, but the egg
        # on disk may have changed since, so every host-path ref (prompt,
        # plain attachments, schema) is re-contained here. An escaping ref
        # (`..`, absolute, backslash, symlink out of the egg dir) raises
        # UnsafeRefError and fails the run closed at provisioning with a
        # field-named Fox refusal — nothing lands in the workspace.
        prompt_src = contained_ref(spec_dir, ctx.spec.input.prompt_file, "input.prompt_file")
        contained_ref(spec_dir, ctx.spec.output.schema_file, "output.schema_file")
        plain_sources: dict[str, tuple[Path, str]] = {}
        for att in ctx.spec.input.attachments:
            if parse_nestbox_uri(att) is None:
                plain_sources[att] = (
                    contained_ref(spec_dir, att, "input.attachments"),
                    attachment_workspace_name(att),
                )
        # (fox.ensure_dir(ws) above already created the workspace — no extra mkdir)
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
                            # spec 32 (G17): the manifest/Headcount carry the
                            # data-dir-relative artifact path, never the host path
                            "artifact_path": display_relpath(clutch.artifact_path, self.settings.data_dir),
                            "size_bytes": len(clutch.data),
                        }
                    )
                else:
                    src, name = plain_sources[att]
                    shutil.copyfile(src, ws / name)
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
        # spec 32 (G11, the Fox): clutch artifacts are state at rest — dirs
        # 0700, files 0600 at creation
        fox.ensure_dir(self.settings.results_dir)
        fox.ensure_dir(artifacts_dir)
        fox.write_text_0600(
            artifacts_dir / "output.json",
            json.dumps(outcome.output, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        async with self._db_lock:
            self.db.execute(
                "INSERT INTO results (run_id, valid, output_json, artifacts_dir, schema_errors)"
                " VALUES (?, 1, ?, ?, NULL)",
                (run_id, json.dumps(outcome.output, ensure_ascii=False), str(artifacts_dir)),
            )
            self.db.commit()
        self.audit.append(
            {
                "event": "clutch.stored",
                "run_id": run_id,
                # spec 32 (G17): no `artifacts_dir` in Headcount rows — the
                # on-disk location is daemon-internal state (DB column stays)
            }
        )

    async def _quarantine(self, run_id: str, raw_output: str, schema_errors: list[str]) -> None:
        """Invalid output is quarantined: kept on disk with its schema errors
        recorded in a `results` row with valid=0 — never stored as valid, never
        discarded (baseline v0.2 §10.2)."""
        qdir = self.settings.quarantine_dir / run_id
        # spec 32 (G11, the Fox): quarantine evidence is state at rest too —
        # dirs 0700, files 0600 at creation
        fox.ensure_dir(self.settings.quarantine_dir)
        fox.ensure_dir(qdir)
        fox.write_text_0600(qdir / "raw_output.txt", raw_output)
        fox.write_text_0600(
            qdir / "schema_errors.json",
            json.dumps(schema_errors, indent=2, ensure_ascii=False) + "\n",
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
                # spec 32 (G17): no `artifacts_dir` in Headcount rows
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
            # spec 32 (G17): the manifest never embeds the host workspace
            # path — a data-dir-relative identifier only
            "workspace": display_relpath(ctx.workspace, self.settings.data_dir),
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
        # spec 32 (ticket 10): the chicken's pid, recorded at hatch/spawn
        # (None when the harness runs no process of its own, e.g. the API
        # adapter) — mirrored in the harness block of the manifest
        manifest["harness"]["pid"] = ctx.pid
        return manifest

    # --- the lifecycle ----------------------------------------------------

    async def execute(self, item: dict[str, Any]) -> None:
        """Drive one queued run to a terminal state, always with a teardown
        attestation and audit rows."""
        run_id = item["run_id"]
        spec = load_spec(item["spec_path"])
        # spec 32 ticket 21 (review G18): the hash pinned at enqueue is
        # re-verified at hatch — the egg is loaded again here, and if it no
        # longer candles to the pinned hash it was swapped between enqueue
        # and hatch, so the run fails closed below (Fox refusal copy): nothing
        # is provisioned and nothing is sent to the feeder under the stale hash
        recomputed_hash = spec_hash(spec)
        hash_mismatch: str | None = None
        if recomputed_hash != item["spec_hash"]:
            hash_mismatch = (
                "the fox refuses to hatch a swapped egg — hash mismatch at hatch: the egg was queued"
                f" pinned as sha256:{item['spec_hash']} but the egg on disk now candles to"
                f" sha256:{recomputed_hash}; the egg changed after enqueue, so nothing was"
                " provisioned and nothing was sent to the feeder"
            )
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
        # spec 32 (G17): from here on, every run-event line of this chicken is
        # host-path-redacted against these roots at the _event choke point
        self._run_path_roots[run_id] = self._path_roots(ctx)
        # spec 32 (ticket 10): the spawn hook records the chicken's pid in
        # the shared registry at hatch/spawn, so the daemon can force lineage
        # from the socket peer's credentials
        if self._pid_registry is not None:
            ctx.on_spawn = lambda pid: self._register_chicken_pid(run_id, pid)
        status = "failed"
        reason = "internal error"
        result: HarnessResult | None = None
        harness: dict[str, Any] = {
            "adapter": spec.harness.adapter,
            "model": spec.harness.model,
            "temperature": spec.harness.temperature,
            # spec 27: mirrored in the manifest
            "spawn_subs": spec.harness.spawn_subs,
            # spec 28: gate names mirrored in the manifest (names only)
            "connectors": list(spec.harness.connectors),
        }
        budget: dict[str, Any] = {
            # spec 23: a Free-Range chicken has no wall-clock cap — timeout_sec
            # is the declared value (possibly None) and no hard kill is enforced
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
            injected_bytes = 0
            try:
                if hash_mismatch is not None:
                    # spec 32 ticket 21 (G18): fail closed at hatch — the egg
                    # on disk was swapped after enqueue; nothing is provisioned
                    # or sent to the feeder under the stale pinned hash
                    raise _HatchHashMismatch(hash_mismatch)
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
            except (NestboxError, UnsafeRefError) as e:
                # spec 25 fail-closed: a quarantined / composted / missing /
                # malformed clutch fails the run at provisioning, before any
                # harness work — attested teardown as usual.
                # spec 32 (G1): the same for a spec ref that crosses the fence
                # at the Hatchery re-check — nothing was copied, nothing is
                # shipped to the feeder, the run fails closed at provisioning
                provision_error = str(e)
                if isinstance(e, NestboxError):
                    budget["injected_context_bytes"] = e.injected_bytes
                status, reason = "failed", f"provisioning failed: {e}"
                # spec 32 (G17): the served event line is redacted of host paths
                await self._event(
                    run_id,
                    "supervisor",
                    "error",
                    sanitize_log_line(redact_host_paths(reason, *self._path_roots(ctx))),
                )
            except _HatchHashMismatch as e:
                # spec 32 ticket 21 (G18): a swapped egg fails closed at hatch —
                # terminal `failed` with the Fox refusal as the reason, before
                # any harness work; the abattoir attestation lands in _finalize
                # exactly like any provisioning failure
                provision_error = str(e)
                status, reason = "failed", str(e)
                await self._event(
                    run_id,
                    "supervisor",
                    "error",
                    sanitize_log_line(redact_host_paths(reason, *self._path_roots(ctx))),
                )
            cap_bytes = spec.resources.max_context_mb * 1024 * 1024
            if provision_error is not None:
                pass  # swapped egg (G18) / chaining resolution failed — status/reason set above
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
                # spec 32 ticket 12 (review G9): the chicken's HOME/XDG/TMPDIR
                # point at its Scratch inside the workspace — the operator's
                # real $HOME and coopd's /proc environ stay out of reach
                await self._event(
                    run_id,
                    "supervisor",
                    "info",
                    f"scratch home prepared at {SCRATCH_HOME_DIRNAME}/ inside the chicken's workspace"
                    " — HOME/XDG/TMPDIR point there, with only a sanitized vibe profile",
                )
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
                if harness.get("connectors"):
                    # spec 28: the opted-in gates were injected into the run's
                    # vibe config (names only — never env/header material)
                    await self._event(
                        run_id,
                        "supervisor",
                        "info",
                        f"gates attached to the harness runtime MCP config: {', '.join(harness['connectors'])}"
                        " (config=.vibe/config.toml)",
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
            # spec 32 (G20, the Fence): the output (Clutch) cap is enforced here
            # as well as at collection in the adapter — defense in depth, so no
            # path can store unbounded raw output in memory, the DB, or on disk;
            # the manifest budget records `truncated`
            if result is not None:
                # reviewer finding: the cap counts encoded bytes, not characters
                capped, was_capped = cap_text_bytes(result.raw_output, OUTPUT_MAX_BYTES)
                if was_capped:
                    result.raw_output = capped
                    result.truncated = True
            if result is not None and result.truncated:
                budget["output_truncated"] = True
                budget["output_cap_bytes"] = OUTPUT_MAX_BYTES
                await self._event(
                    run_id,
                    "supervisor",
                    "warn",
                    f"harness output hit the {OUTPUT_MAX_BYTES}-byte fence —"
                    " the clutch is stored truncated (truncated=true)",
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
                    # spec 32 (G1): re-check the fence right before the schema
                    # file is read — an escaping ref fails the run closed here
                    schema_path = contained_ref(
                        ctx.spec_path.parent, ctx.spec.output.schema_file, "output.schema_file"
                    )
                    # spec 32 (G17): schema errors report the spec-relative
                    # schema name, never the host path
                    outcome = validate_output(result.raw_output, schema_path, schema_name=ctx.spec.output.schema_file)
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
            # spec 32 (G17): an OSError here can carry host paths (workspace,
            # egg dir) — the full detail goes to coopd's own log (stderr), the
            # served reason/event get the redacted form
            _log.error("chicken %s failed: %s: %s", run_id, type(e).__name__, e)
            status, reason = "failed", redact_host_paths(f"{type(e).__name__}: {e}", *self._path_roots(ctx))
            with contextlib.suppress(Exception):
                await self._event(
                    run_id,
                    "supervisor",
                    "error",
                    sanitize_log_line(redact_host_paths(str(e), *self._path_roots(ctx))),
                )
        finally:
            self._active.pop(run_id, None)
            self._active_free_range.discard(run_id)
            self._run_path_roots.pop(run_id, None)
            # spec 32 (ticket 10): the chicken is gone — drop its pid from the
            # peer-identity registry so a reused pid is never attributed to it
            self._unregister_chicken_pids(run_id)
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
        When `run_stop` fires mid-run, the harness process group gets SIGTERM
        first and a hard kill after `STOP_TERM_GRACE_SEC` (spec 32 / G15: the
        chicken is spawned in its own session, so the signals reach the whole
        group, and the post-kill await of the harness task is bounded — on
        timeout a synthetic result is produced so the run always reaches a
        terminal state with its attestation). The run then ends `stopped`
        and its output is never collected or candled. Returns
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
                # spec 32 (G15): the chicken was spawned in its own session,
                # so SIGTERM goes to the whole process group (grandchildren
                # included), and after the grace the group gets a SIGKILL —
                # a pipe-holding grandchild can no longer survive the stop
                signal_process_group(proc, signal.SIGTERM)
                hard_killed = False
                try:
                    await asyncio.wait_for(proc.wait(), timeout=STOP_TERM_GRACE_SEC)
                except asyncio.TimeoutError:
                    hard_killed = True
                # reap the whole group either way: SIGTERM survivors (and
                # pipe-holders) must not outlive the stop; the reap await is
                # bounded so it can never wedge the run
                await reap_process_group(proc)
                reason = (
                    f"stopped by operator: SIGTERM, then hard kill after {STOP_TERM_GRACE_SEC:g}s grace"
                    if hard_killed
                    else f"stopped by operator: SIGTERM (reaped within {STOP_TERM_GRACE_SEC:g}s grace)"
                )
                try:
                    result = await asyncio.wait_for(asyncio.shield(exec_task), timeout=POST_KILL_AWAIT_SEC)
                except asyncio.TimeoutError:
                    # a pipe-holding escapee kept the harness task alive past
                    # the group kill — synthesize the result so teardown
                    # always completes and the run reaches terminal state
                    exec_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await exec_task
                    result = HarnessResult(
                        exit_code=proc.returncode,
                        raw_output="",
                        usage={},
                        error="stopped by operator (synthetic result: harness pipes still held after the process-group kill)",
                    )
                    reason = f"{reason}; harness output discarded (pipes still held after the group kill)"
            else:
                # no live subprocess to signal (e.g. an HTTP adapter) — cancel
                # the in-flight call itself, with a bounded await. The
                # cancelled task raises CancelledError (not TimeoutError)
                # through the shield, so both are caught here — otherwise a
                # stopped proc-less chicken would end failed/internal error
                # instead of `stopped`
                exec_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(exec_task), timeout=POST_KILL_AWAIT_SEC)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    # re-raise only when OUR task was cancelled (not the inner one)
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() > 0:
                        raise
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await exec_task
                result = HarnessResult(exit_code=None, raw_output="", usage={}, error="stopped by operator")
                reason = "stopped by operator (in-flight harness call cancelled)"
            return result, reason
        result = await exec_task
        return result, None

    async def cancel_queued(
        self,
        item: dict[str, Any],
        reason: str | None = None,
        spec: JobSpec | None = None,
    ) -> None:
        """Spec 22: terminal-cancel an egg that was still in the pecking order.
        It never hatches (no harness events, no `egg.hatched`), but it still
        gets a manifest and the abattoir's attestation exactly like any
        terminal state — nothing was spawned or provisioned, so the
        attestation verifies that (1, 1, 1).

        Spec 32 ticket 20 (G16): the same attested path also cancels eggs
        still queued at coopd shutdown and stale queued rows recovered at
        startup — pass ``reason`` for those; pass ``spec`` when the egg file
        may be gone (the stored ``spec_toml`` was already candled)."""
        run_id = item["run_id"]
        reason = sanitize_log_line(reason or "cancelled while queued — the egg never hatched")
        now = utcnow_iso()
        if spec is None:
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
                # spec 28: gate names mirrored in the manifest (names only)
                "connectors": list(spec.harness.connectors),
                # spec 32 (ticket 10): never hatched, so no pid
                "pid": None,
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
        # G10: the terminal reason lands in the manifest (status_reason), the
        # Headcount and run events — served via /api/runs* and printed by the
        # CLI. It may quote a Feeder/OAuth error, so it is sanitized here:
        # control/format characters stripped, never raw response bytes.
        # spec 32 (G17): one choke point — also redacted of host paths before
        # it lands in the manifest, the runs row, or any Headcount row; full
        # detail is in coopd's log
        reason = redact_host_paths(sanitize_log_line(reason), *self._path_roots(ctx))
        manifest = self._manifest(item, ctx, harness, result, status, reason, budget, auth, chaining)
        # teardown first: destruction is verified, not assumed
        try:
            report: TeardownReport = await adapter.teardown(ctx)
        except Exception as e:
            report = TeardownReport(process_reaped=False, workspace_removed=False, env_cleared=False)
            with contextlib.suppress(Exception):
                await self._event(
                    run_id,
                    "supervisor",
                    "error",
                    f"teardown error: {redact_host_paths(str(e), *self._path_roots(ctx))}",
                )
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
