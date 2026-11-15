"""The always-on supervisor daemon.

A single asyncio process listening on a Unix domain socket speaking
JSON-lines: one JSON request object per line, one JSON response object per
line. This socket is the one IPC contract — the CLI and the future scheduler
both use it, so manual and cron runs flow through the same queue.

Ops: ``ping``, ``status``, ``shutdown`` (spec 02) plus run submission and
inspection (spec 03): ``enqueue``, ``runs_list``, ``runs_show``, ``runs_logs``,
and validated results (spec 04): ``results_get`` — quarantined output is
never returned as a valid result. Spec 06 adds the internal cron scheduler
(APScheduler): ``schedule_add``, ``schedule_list``, ``schedule_rm``; a firing
cron schedule submits an ``enqueue`` with ``trigger="cron"`` over this same
socket, so manual and cron runs share one queue and one admission path.
Runs flow through one queue with resource-weight admission control; over-budget
runs backlog in ``pending``, never rejected.

Spec 13 adds three read-only ops for the GUI: ``jobs_list`` (registered job
specs with hashes), ``audit_tail`` (last N audit rows, newest first, optional
event-type filter), ``settings_get`` (effective settings, never the coop auth
token).

Spec 14 (runs from the GUI): ``runs_list`` accepts optional ``job``/``status``
filters and reports each row's ``result_valid``; ``results_get`` additionally
returns the quarantined ``raw_output`` with its schema errors (never as a
valid result) and reports ``expired`` for artifacts the retention sweep removed.

Spec 15 (upload + validate job specs from the browser): ``jobs_upload`` takes a
spec bundle as JSON with base64-encoded files (``{"files": {"job.toml": …,
"prompts/p.txt": …}}``), size-capped at ``UPLOAD_MAX_BYTES`` decoded bytes,
validates it with the existing spec loader (field-named errors, exactly like
``tc jobs validate``), and — only when valid — stores it under
``<data_dir>/uploads/<job>-<spec_hash>/`` so it is enqueueable by that
in-container path. Invalid bundles are never stored and never register a job.

Spec 12 (coop): when ``coop_enabled`` is set, the same process also serves the
TurboChickens GUI over HTTP (aiohttp, same event loop) — static SPA at ``/``
and ``/api/ping`` + ``/api/status`` under ``/api``, thin mirrors of the socket
ops called over the daemon's own socket. The bearer token is required on every
bind by default (spec 32, review G3); loopback + no token is open only with the
``coop_loopback_open`` opt-in, and a configured token (or an ephemeral one
printed once at startup) is required otherwise.

Spec 19 (auth & access management in the GUI) adds the ops behind the Auth
view: ``auth_providers`` (each provider's auth mode + perch seat expiry via
``SeatBroker.status()`` — never token material), ``auth_access`` (the declared
access scope: per-run env allowlist, network policy, tools/data/MCP/connectors
placeholders), ``auth_login_start`` / ``auth_login_poll`` (the OAuth device
flow driven one request at a time through the same ``SeatBroker`` — the
device_code stays in daemon memory, sessions land 0600 under
``<data_dir>/seats/`` exactly as the CLI path, Headcount rows ``perch.claimed`` /
``perch.released`` carry provider + mode only), and ``auth_logout``.
``runs_list`` rows also carry ``auth_mode`` from the run manifest.

Spec 22 (lifecycle ops) adds ``run_stop`` (a queued egg → terminal
``cancelled``, never hatches; a running chicken → SIGTERM, then hard kill,
terminal ``stopped`` — both paths with the abattoir's attested teardown and
Headcount rows ``egg.cancelled`` / ``chicken.stopped``) and ``run_rerun``
(enqueue the same egg as a fresh run with the lineage recorded as
``rerun_of`` in the manifest and the ``egg.queued`` row). ``TERMINAL_STATES``
gains ``cancelled``/``stopped``, so a missing attestation on them stays an
alert condition. ``runs_list`` rows also carry ``status_reason``.

Spec 30 (GUI lifecycle controls) extends ``runs_list`` rows with the
feed-budget telemetry from the manifest — ``cost_usd``, ``max_cost_usd``,
``budget_outcome`` — so the coop can show measured cost against the egg's cap.

Spec 23 (Free-Range) removes the hard wall-clock kill for eggs with
``resources.free_range = true`` (``timeout_sec`` optional there): such a
chicken dies only via ``run_stop`` or daemon shutdown — on shutdown the
daemon asks every active Free-Range chicken to stop through the same
spec-22 stop path (SIGTERM → hard kill → terminal ``stopped`` with the
abattoir's attested teardown) before exiting.

Spec 27 (ChickenSubs) adds ``harness.spawn_subs``: a chicken with
``spawn_subs = true`` gets the Farmhand attached to its vibe-cli runtime over
stdio (the adapter writes the MCP server entry into the run's vibe config), so
it can enqueue sub-eggs through the farm's own contract. ``enqueue`` accepts
``parent_run_id``: the sub is an ordinary egg through the same queue/admission
(weight unchanged), with lineage recorded on the `runs.parent_run_id` column
(schema v5), in the manifest, and in the ``egg.queued`` Headcount row. Depth
is capped at 1 — enqueue refuses a ``spawn_subs = true`` egg when the
enqueuing run is itself a sub (field-named ``harness.spawn_subs`` error) —
and ``run_stop`` with a ``caller_run_id`` refuses targets that are the caller's
ancestors. ``runs_show``/``runs_list`` rows carry ``parent_run_id``.

Spec 28 (connectors registry) adds the ops ``connectors_list`` /
``connectors_add`` / ``connectors_rm`` — CRUD over the ``connectors.toml``
registry (resolved exactly like ``providers.toml``: ``settings.connectors_path``
> ``$TURBOCHICKENS_CONNECTORS`` > ``<data_dir>/connectors.toml``), audit-logged
as ``gate.registered`` / ``gate.removed`` / ``gate.rejected`` with name +
transport only — never env/header material. ``connectors_list`` serves
non-secret summaries only (stdio env as key names, https headers as refs).
``enqueue`` refuses ``harness.connectors`` names that are not registered
(field-named ``harness.connectors`` error) and refuses the list for
non-vibe-cli adapters (no harness runtime to inject into); the vibe-cli
adapter injects the chosen gates into the run's vibe config at provisioning.
``auth_access`` now fills its ``connectors`` scope entry with the registered
gate names.

Spec 29 (the Beak) adds the read-only op ``workspace_peek``: the workspace
file tree of a run, or one file's size-capped content when ``path`` is given.
It is available only while the workspace exists (in-flight chickens) — a
terminal run answers a clear "the beak is closed" error, because the
abattoir's attested teardown already removed the workspace. Paths are locked
inside that run's workspace (traversal refused) and the chicken's private
harness config (``.vibe/``, the farmhand settings file) is never served — see
``beak.py`` for the pure seam.

Spec 31 (the Farmhand & Connectors GUI views) adds ``farmhand_info`` — the
Farmhand view's payload: active transport(s), the HTTPS endpoint URL on the
coop host, whether it is currently mounted, whether the coop requires the
bearer, the exposed tool list (read straight off the built MCP server, so the
GUI can never drift from the real tools), and ready-to-paste client-config
snippets whose bearer header is a placeholder only (the token is never in any
payload) — and ``settings_set``, the runtime settings update behind the view's
transport form: the patch is re-validated server-side through the ``Settings``
model with ``extra="forbid"`` semantics (an unknown key is a field-named
error, never silently ignored), only ``MUTABLE_SETTINGS_FIELDS`` may change
(everything else is read-only at runtime), the change is audited as
``settings.changed``, and the coop mounts/unmounts the Farmhand HTTPS endpoint
when ``farmhand_transport`` crosses the https boundary.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from turbochickens import __version__, beak
from turbochickens.adapter import (
    BASE_ENV_KEYS,
    DEFAULT_CREDENTIAL_ALLOWLIST,
    AdapterNotImplemented,
    ProviderConfigError,
    run_weight,
)
from turbochickens.audit import AuditLog
from turbochickens.chaining import is_nestbox_uri
from turbochickens.connectors import (
    ConnectorsConfig,
    ConnectorsError,
    entry_to_dict,
    fmt_error_loc,
    load_connectors,
    public_entry,
    read_registry_raw,
    resolve_connectors_path,
    save_registry,
    validate_connector_payload,
)
from turbochickens.providers import ProvidersConfig, load_providers
from turbochickens.retention import sweep_expired_results
from turbochickens.runner import TERMINAL_STATES, RunPipeline
from turbochickens.scheduler import CronScheduler
from turbochickens.seat import DeviceGrant, SeatBroker, SeatError, _iso
from turbochickens.settings import SOCKET_STREAM_LIMIT, UPLOAD_MAX_BYTES, Settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.spec.model import NETWORK_POLICIES
from turbochickens.storage import init_db
from turbochickens.util import utcnow, utcnow_iso

#: how often the retention sweeper re-runs after the startup sweep (spec 08)
RETENTION_SWEEP_INTERVAL_SEC = 3600.0

#: spec 13 `audit_tail` bounds: default page size and the hard cap
AUDIT_TAIL_DEFAULT = 100
AUDIT_TAIL_MAX = 1000

#: spec 15 `jobs_upload` bound on the number of files in one bundle
UPLOAD_MAX_FILES = 200

#: spec 31 `settings_set`: the settings mutable at runtime from the GUI (the
#: Farmhand view's transport switch). Everything else stays read-only there —
#: including ``coop_loopback_open`` (spec 32): the loopback-open opt-in is a
#: startup setting, never flippable from the GUI.
MUTABLE_SETTINGS_FIELDS = ("farmhand_transport",)

PROTOCOL_VERSION = 1


def _safe_bundle_relpath(key: str) -> str | None:
    """A bundle-relative path is safe when it stays inside the bundle dir:
    relative, POSIX, no empty/'.'/'..' segments, no backslash or NUL.
    Returns the normalized key, or None when unsafe (spec 15)."""
    if not isinstance(key, str) or not key or key != key.strip():
        return None
    if key.startswith("/") or "\\" in key or "\x00" in key:
        return None
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return key


def _safe_job_dir_name(name: str) -> str:
    """Filesystem-safe directory component for an uploaded job (spec 15);
    the spec hash in the dir name keeps distinct specs distinct."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return (cleaned or "job")[:64]


def _issue_dict(issue, staging_dir: str) -> dict[str, str]:
    """One SpecIssue as a field-named error dict; the staging path is rewritten
    to ``<bundle>`` so no host path leaves the daemon (spec 15)."""
    field = ".".join(issue.loc) if issue.loc else "<spec>"
    message = str(issue).replace(staging_dir, "<bundle>")
    return {"field": field, "message": message}


class DaemonAlreadyRunning(Exception):
    """Another daemon already owns the socket."""


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at: datetime | None = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending: list[dict[str, Any]] = []  # admitted-waiting (backlog), FIFO
        self.in_flight: dict[str, float] = {}  # run_id -> resource weight
        self.db: sqlite3.Connection | None = None
        self.audit: AuditLog | None = None
        self.pipeline: RunPipeline | None = None
        self.providers: ProvidersConfig | None = None
        self.cron: CronScheduler | None = None
        self._server: asyncio.AbstractServer | None = None
        self.coop: Any | None = None  # CoopServer when coop_enabled (spec 12)
        self._stop: asyncio.Event | None = None
        self._db_lock = asyncio.Lock()
        self._sched_task: asyncio.Task | None = None
        self._retention_task: asyncio.Task | None = None
        self._last_retention: dict[str, Any] | None = None
        self._run_tasks: set[asyncio.Task] = set()
        # spec 22: run_ids whose queued egg was pulled from the pecking order
        # via `run_stop` — the scheduler cancels them instead of admitting
        self._cancel_requested: set[str] = set()
        # spec 19: seat broker for the auth ops + in-flight device grants
        self._broker: SeatBroker | None = None
        self._device_grants: dict[str, DeviceGrant] = {}

    def request_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    async def _socket_alive(self) -> bool:
        try:
            _reader, writer = await asyncio.open_unix_connection(str(self.settings.socket_path))
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    async def serve(self) -> None:
        s = self.settings
        s.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = init_db(s.db_path)
        self.audit = AuditLog(s.audit_log_path)
        self.providers = load_providers(s.providers_path, s.data_dir)
        self.started_at = utcnow()
        self._stop = asyncio.Event()
        self.audit.append(
            {
                "event": "coopd.started",
                "pid": os.getpid(),
                "version": __version__,
                "socket": str(s.socket_path),
            }
        )
        if s.socket_path.exists():
            if await self._socket_alive():
                raise DaemonAlreadyRunning(f"daemon already running on {s.socket_path}")
            s.socket_path.unlink()  # stale socket from a dead daemon
        self.pipeline = RunPipeline(s, self.db, self.audit, providers=self.providers)
        # spec 08: retention — sweep expired result artifacts once at startup
        # (the audit log itself is never TTL-deleted), then periodically
        self._last_retention = sweep_expired_results(s, self.db, self.audit)
        # spec 06: internal cron scheduler; persisted schedules are re-registered
        # before the socket accepts, so schedule ops never race scheduler setup
        self.cron = CronScheduler(fire=self._cron_fire)
        self.cron.start()
        self._load_schedules()
        # spec 12: coop — serve the GUI over HTTP in this same event loop.
        # Started before the socket accepts, so a ping OK implies the whole
        # HTTP surface (incl. the spec-26 Farmhand MCP endpoint) is up and
        # its startup audit row is already in the Headcount.
        if s.coop_enabled:
            from turbochickens.coop import CoopServer  # lazy: coop imports client, client imports this module

            self.coop = CoopServer(s)
            await self.coop.start()
            self.audit.append(
                {
                    "event": "coop.started",
                    "url": self.coop.url,
                    "auth_required": self.coop.auth.required,
                    # the token itself is never audited — printed once to stdout only
                }
            )
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(s.socket_path), limit=SOCKET_STREAM_LIMIT
        )
        os.chmod(s.socket_path, 0o600)
        serve_task = asyncio.create_task(self._server.serve_forever())
        stop_task = asyncio.create_task(self._stop.wait())
        self._sched_task = asyncio.create_task(self._scheduler())
        self._retention_task = asyncio.create_task(self._retention_loop())
        try:
            await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # spec 23: a Free-Range chicken has no fence — daemon shutdown is
            # one of its two attested death paths, so ask every active one to
            # stop via the spec-22 stop path (SIGTERM → hard kill → `stopped`)
            # instead of waiting out the bounded drain below
            if self.pipeline is not None:
                self.pipeline.request_stop_free_range()
            if self.coop is not None:
                with contextlib.suppress(Exception):
                    await self.coop.stop()
                if self.audit is not None:
                    self.audit.append({"event": "coop.stopped", "url": self.coop.url})
                self.coop = None
            self._server.close()
            serve_task.cancel()
            stop_task.cancel()
            if self._sched_task is not None:
                self._sched_task.cancel()
            if self._retention_task is not None:
                self._retention_task.cancel()
            if self.cron is not None:
                self.cron.stop()
            if self._run_tasks:  # graceful: let in-flight runs finish (bounded)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*self._run_tasks, return_exceptions=True), timeout=60
                    )
                for t in self._run_tasks:
                    if not t.done():
                        t.cancel()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            if self.audit is not None:
                self.audit.append({"event": "coopd.stopped", "pid": os.getpid()})
            if self.db is not None:
                self.db.close()
                self.db = None
            with contextlib.suppress(OSError):
                s.socket_path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while self._stop is None or not self._stop.is_set():
                try:
                    line = await reader.readline()
                except ValueError:
                    # over SOCKET_STREAM_LIMIT: answer once, then close — the
                    # stream is desynced after a LimitOverrunError
                    resp = {
                        "ok": False,
                        "protocol": PROTOCOL_VERSION,
                        "error": f"request line exceeds the {SOCKET_STREAM_LIMIT}-byte protocol limit",
                    }
                    writer.write(json.dumps(resp).encode("utf-8") + b"\n")
                    with contextlib.suppress(Exception):
                        await writer.drain()
                    break
                if not line:
                    break
                resp = await self._respond(line)
                writer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _respond(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "malformed request: expected one JSON object per line"}
        if not isinstance(req, dict):
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "request must be a JSON object"}
        try:
            return await self._dispatch(req)
        except Exception as e:  # never let one bad request kill the connection loop
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"{type(e).__name__}: {e}"}

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "ping":
            return {
                "ok": True,
                "op": "ping",
                "protocol": PROTOCOL_VERSION,
                "pong": True,
                "version": __version__,
                "pid": os.getpid(),
                "uptime_sec": self._uptime_sec(),
            }
        if op == "status":
            return self._status()
        if op == "enqueue":
            return await self._enqueue(req)
        if op == "runs_list":
            return self._runs_list(req)
        if op == "runs_show":
            return self._runs_show(req)
        if op == "runs_logs":
            return self._runs_logs(req)
        if op == "workspace_peek":
            return self._workspace_peek(req)
        if op == "run_stop":
            return await self._run_stop(req)
        if op == "run_rerun":
            return await self._run_rerun(req)
        if op == "results_get":
            return self._results_get(req)
        if op == "stats_list":
            return self._stats_list()
        if op == "stats_show":
            return self._stats_show(req)
        if op == "retention_sweep":
            return self._retention_sweep()
        if op == "schedule_add":
            return await self._schedule_add(req)
        if op == "schedule_list":
            return self._schedule_list()
        if op == "schedule_rm":
            return await self._schedule_rm(req)
        if op == "jobs_list":
            return self._jobs_list()
        if op == "jobs_upload":
            return self._jobs_upload(req)
        if op == "audit_tail":
            return self._audit_tail(req)
        if op == "settings_get":
            return self._settings_get()
        # spec 31: the Farmhand view's info + runtime settings updates from the GUI
        if op == "farmhand_info":
            return await self._farmhand_info()
        if op == "settings_set":
            return await self._settings_set(req)
        # spec 19: auth & access management for the GUI
        if op == "auth_providers":
            return self._auth_providers()
        if op == "auth_access":
            return self._auth_access()
        if op == "auth_login_start":
            return self._auth_login_start(req)
        if op == "auth_login_poll":
            return self._auth_login_poll(req)
        if op == "auth_logout":
            return self._auth_logout(req)
        # spec 28: connectors registry CRUD (gates)
        if op == "connectors_list":
            return self._connectors_list()
        if op == "connectors_add":
            return self._connectors_add(req)
        if op == "connectors_rm":
            return self._connectors_rm(req)
        if op == "shutdown":
            self.request_stop()
            return {"ok": True, "op": "shutdown", "protocol": PROTOCOL_VERSION, "message": "shutting down"}
        return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown op: {op!r}"}

    def _uptime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return round((utcnow() - self.started_at).total_seconds(), 3)

    def _queue_depth(self) -> int:
        return self.queue.qsize() + len(self.pending)

    def _alerts(self) -> list[str]:
        """Terminal runs without a teardown attestation — an alert condition."""
        if self.db is None:
            return []
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        rows = self.db.execute(
            "SELECT r.id FROM runs r LEFT JOIN teardown_attestations t ON t.run_id = r.id"
            f" WHERE r.status IN ({placeholders})"
            " AND (t.run_id IS NULL OR t.process_reaped = 0 OR t.workspace_removed = 0 OR t.env_cleared = 0)",
            TERMINAL_STATES,
        ).fetchall()
        return [r[0] for r in rows]

    def _status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "op": "status",
            "protocol": PROTOCOL_VERSION,
            "daemon": {
                "up": True,
                "pid": os.getpid(),
                "version": __version__,
                "started_at": utcnow_iso() if self.started_at is None else self.started_at.isoformat(),
                "uptime_sec": self._uptime_sec(),
            },
            "socket": str(self.settings.socket_path),
            "queue_depth": self._queue_depth(),
            "in_flight": [
                {"run_id": run_id, "weight": weight} for run_id, weight in self.in_flight.items()
            ],
            "weight_budget": self.settings.weight_budget,
            "db_path": str(self.settings.db_path),
            "audit_log": str(self.settings.audit_log_path),
            "schedules": self._schedule_count(),
            "alerts": self._alerts(),
            "retention": {
                "audit_retention": self.settings.audit_retention,
                "results_retention_months": self.settings.results_retention_months,
                "last_sweep": self._last_retention,
            },
            "coop": self._coop_block(),
        }

    def _coop_block(self) -> dict[str, Any]:
        """The coop block of the status op — url + whether auth is required;
        never the token (spec 12)."""
        if self.coop is not None:
            return self.coop.status_block()
        return {"enabled": False, "url": None, "auth_required": False}

    def _schedule_count(self) -> int:
        if self.db is None:
            return 0
        return self.db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]

    # --- run submission (spec 03) ------------------------------------------

    async def _enqueue(self, req: dict[str, Any]) -> dict[str, Any]:
        spec_path_raw = req.get("spec_path")
        if not spec_path_raw:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "enqueue requires spec_path"}
        spec_path = Path(str(spec_path_raw))
        trigger = str(req.get("trigger", "manual"))
        rerun_of = req.get("rerun_of")  # spec 22: `run_rerun` lineage
        parent_run_id = req.get("parent_run_id")  # spec 27: ChickenSub lineage
        try:
            spec = load_spec(spec_path)
        except SpecValidationError as e:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": "egg failed candling: " + "; ".join(str(i) for i in e.issues),
            }
        digest = spec_hash(spec)
        expected = req.get("spec_hash")
        if expected and str(expected) != digest:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"egg hash mismatch: client sent sha256:{expected}, the spec on disk is sha256:{digest}",
            }
        try:
            weight = run_weight(spec.harness.adapter)
        except KeyError:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown adapter: {spec.harness.adapter!r}"}
        if spec.harness.spawn_subs and spec.harness.adapter != "vibe-cli":
            # spec 27: only the vibe-cli harness has a runtime to attach the
            # Farmhand to — fail closed instead of a silent no-op
            msg = (
                f"harness.spawn_subs: adapter {spec.harness.adapter!r} has no harness runtime to attach"
                " the Farmhand to — spawn_subs = true is only supported with adapter 'vibe-cli'"
            )
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": msg,
                "errors": [{"field": "harness.spawn_subs", "message": msg}],
            }
        connector_names = list(spec.harness.connectors)
        if connector_names:
            # spec 28: gates are injected into the harness runtime's MCP
            # config — only vibe-cli has one; unknown gate names fail closed
            if spec.harness.adapter != "vibe-cli":
                msg = (
                    f"harness.connectors: adapter {spec.harness.adapter!r} has no harness runtime MCP"
                    " config to inject connectors into — connectors are only supported with"
                    " adapter 'vibe-cli'"
                )
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": msg,
                    "errors": [{"field": "harness.connectors", "message": msg}],
                }
            try:
                registry = load_connectors(self.settings.connectors_path, self.settings.data_dir)
            except ConnectorsError as e:
                return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
            unknown = [n for n in connector_names if n not in registry.connectors]
            if unknown:
                msg = (
                    f"harness.connectors: unknown connector {', '.join(repr(n) for n in unknown)} —"
                    f" known connectors: {', '.join(sorted(registry.connectors)) or '(none)'};"
                    " register gates in connectors.toml (connectors_add)"
                )
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": msg,
                    "errors": [{"field": "harness.connectors", "message": msg}],
                }
        from turbochickens.adapter import make_adapter

        try:
            # fail fast at enqueue: unregistered adapter or unresolvable model/provider
            make_adapter(spec, providers=self.providers)
        except (AdapterNotImplemented, ProviderConfigError) as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        if parent_run_id is not None:
            parent_run_id = str(parent_run_id)
            if not parent_run_id:
                return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "enqueue parent_run_id must be a non-empty run id"}
            assert self.db is not None
            prow = self.db.execute("SELECT parent_run_id FROM runs WHERE id = ?", (parent_run_id,)).fetchone()
            if prow is None:
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": (
                        f"unknown chicken: parent_run_id {parent_run_id!r} — a sub-egg must be enqueued"
                        " by an existing chicken (through its Farmhand)"
                    ),
                }
            if prow[0] is not None and spec.harness.spawn_subs:
                # spec 27: depth cap 1 — the enqueuing run is itself a sub, so
                # it cannot queue an egg that would itself spawn subs
                msg = (
                    f"harness.spawn_subs: sub-chicken {parent_run_id!r} (itself a sub of {prow[0]!r})"
                    " cannot queue an egg with spawn_subs = true — ChickenSub depth is capped at 1"
                )
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": msg,
                    "errors": [{"field": "harness.spawn_subs", "message": msg}],
                }
        job_id = spec.job.name
        run_id = "run-" + uuid.uuid4().hex[:12]
        started_at = utcnow_iso()
        async with self._db_lock:
            assert self.db is not None
            self.db.execute(
                "INSERT OR IGNORE INTO job_specs (id, name, spec_hash, spec_toml, created_at, spec_path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, spec.job.name, digest, spec_path.read_text(encoding="utf-8"), started_at, str(spec_path.resolve())),
            )
            self.db.execute(
                "INSERT INTO runs (id, job_id, status, trigger, started_at, parent_run_id) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, job_id, "queued", trigger, started_at, parent_run_id),
            )
            self.db.commit()
        item = {
            "run_id": run_id,
            "job_id": job_id,
            "spec_path": str(spec_path.resolve()),
            "spec_hash": digest,
            "trigger": trigger,
            "weight": weight,
            "started_at": started_at,
        }
        if rerun_of:
            item["rerun_of"] = str(rerun_of)
        if parent_run_id:
            item["parent_run_id"] = parent_run_id
        await self.queue.put(item)
        assert self.audit is not None
        queued_row: dict[str, Any] = {
            "event": "egg.queued",
            "run_id": run_id,
            "job_id": job_id,
            "spec_hash": f"sha256:{digest}",
            "trigger": trigger,
            "weight": weight,
        }
        if rerun_of:
            queued_row["rerun_of"] = str(rerun_of)
        if parent_run_id:
            # spec 27: sub-chicken lineage in the Headcount
            queued_row["parent_run_id"] = parent_run_id
        self.audit.append(queued_row)
        resp: dict[str, Any] = {
            "ok": True,
            "op": "enqueue",
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "job_id": job_id,
            "status": "queued",
            "weight": weight,
            "queue_depth": self._queue_depth(),
        }
        if rerun_of:
            resp["rerun_of"] = str(rerun_of)
        if parent_run_id:
            resp["parent_run_id"] = parent_run_id
        return resp

    # --- admission control + scheduler -------------------------------------

    def _admit(self, weight: float) -> bool:
        used = sum(self.in_flight.values())
        return weight <= self.settings.weight_budget - used + 1e-9

    async def _scheduler(self) -> None:
        """One queue, one admission path. Over-budget runs backlog in
        `pending` (FIFO) — nothing is ever rejected. A queued egg whose
        `run_stop` arrived (spec 22) is cancelled instead of admitted."""
        while self._stop is None or not self._stop.is_set():
            while self.pending and self._admit(self.pending[0]["weight"]):
                item = self.pending.pop(0)
                if item["run_id"] in self._cancel_requested:
                    await self._cancel_item(item)
                    continue
                self.in_flight[item["run_id"]] = item["weight"]
                task = asyncio.create_task(self._run_item(item))
                self._run_tasks.add(task)
                task.add_done_callback(self._run_tasks.discard)
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if item["run_id"] in self._cancel_requested:
                await self._cancel_item(item)
            else:
                self.pending.append(item)

    async def _cancel_item(self, item: dict[str, Any]) -> None:
        """Spec 22: cancel a queued egg pulled from the pecking order by
        `run_stop` — terminal `cancelled`, never hatched, still attested."""
        self._cancel_requested.discard(item["run_id"])
        assert self.pipeline is not None
        await self.pipeline.cancel_queued(item)

    async def _run_item(self, item: dict[str, Any]) -> None:
        run_id = item["run_id"]
        try:
            assert self.pipeline is not None
            await self.pipeline.execute(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            # the pipeline guarantees teardown+attestation itself; this is a
            # last-resort guard so a run never disappears from in_flight
            with contextlib.suppress(Exception):
                assert self.pipeline is not None
                await self.pipeline.execute_guard(run_id)
        finally:
            self.in_flight.pop(run_id, None)

    # --- run inspection ops --------------------------------------------------

    def _run_row(self, run_id: str) -> dict[str, Any] | None:
        if self.db is None:
            return None
        row = self.db.execute(
            "SELECT r.id, r.job_id, j.name, r.status, r.trigger, r.started_at, r.finished_at,"
            " r.manifest_json, r.usage_json, r.parent_run_id FROM runs r JOIN job_specs j ON j.id = r.job_id WHERE r.id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        att = self.db.execute(
            "SELECT process_reaped, workspace_removed, env_cleared, verified_at"
            " FROM teardown_attestations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        res = self.db.execute(
            "SELECT valid, artifacts_dir, schema_errors FROM results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return {
            "id": row[0],
            "job_id": row[1],
            "job_name": row[2],
            "status": row[3],
            "trigger": row[4],
            "started_at": row[5],
            "finished_at": row[6],
            # spec 27: sub-chicken lineage (None for ordinary chickens)
            "parent_run_id": row[9],
            "manifest": json.loads(row[7]) if row[7] else None,
            "usage": json.loads(row[8]) if row[8] else None,
            "attestation": (
                {
                    "process_reaped": bool(att[0]),
                    "workspace_removed": bool(att[1]),
                    "env_cleared": bool(att[2]),
                    "verified_at": att[3],
                }
                if att
                else None
            ),
            "result": (
                {
                    "valid": bool(res[0]),
                    "artifacts_dir": res[1],
                    "schema_errors": json.loads(res[2]) if res[2] else None,
                }
                if res
                else None
            ),
        }

    def _runs_list(self, req: dict[str, Any] | None = None) -> dict[str, Any]:
        """Runs, most recent first. Optional filters (spec 14, used by the GUI):
        `job` matches the job id or name, `status` is an exact status match.
        Each row also carries `result_valid` (None when the run never reached
        collecting) so the runs table can show the quarantined state, and
        `auth_mode` (spec 19) from the run manifest's spec-09 auth block.
        Spec 30 (GUI lifecycle): rows also carry the feed-budget telemetry —
        `cost_usd` (measured usage), `max_cost_usd` (the egg's cap, None when
        unset) and `budget_outcome` — from the manifest, so the coop can show
        measured cost against the cap without opening the manifest."""
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        req = req or {}
        conds: list[str] = []
        params: list[Any] = []
        job = req.get("job")
        if job:
            conds.append("(r.job_id = ? OR j.name = ?)")
            params.extend([str(job), str(job)])
        status = req.get("status")
        if status:
            conds.append("r.status = ?")
            params.append(str(status))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self.db.execute(
            "SELECT r.id, r.job_id, j.name, r.status, r.trigger, r.started_at, r.finished_at,"
            " (SELECT COUNT(*) FROM teardown_attestations t WHERE t.run_id = r.id),"
            " (SELECT valid FROM results WHERE run_id = r.id),"
            " r.manifest_json, r.parent_run_id"
            f" FROM runs r JOIN job_specs j ON j.id = r.job_id{where} ORDER BY r.rowid DESC",
            params,
        ).fetchall()
        runs = []
        for r in rows:
            # spec 19: surface the per-run auth mode (spec 09 manifest block)
            # in the runs table — a mode string only, never token material.
            # spec 22: surface the terminal status reason (stopped/cancelled/…)
            auth_mode = None
            status_reason = None
            cost_usd = None
            max_cost_usd = None
            budget_outcome = None
            if r[9]:
                try:
                    manifest = json.loads(r[9])
                    if isinstance(manifest, dict):
                        auth = manifest.get("auth")
                        if isinstance(auth, dict):
                            auth_mode = auth.get("mode")
                        status_reason = manifest.get("status_reason")
                        # spec 30: feed-budget telemetry for the coop table
                        usage = manifest.get("usage")
                        if isinstance(usage, dict) and usage.get("cost_usd") is not None:
                            cost_usd = usage.get("cost_usd")
                        budget = manifest.get("budget")
                        if isinstance(budget, dict):
                            max_cost_usd = budget.get("max_cost_usd")
                            budget_outcome = budget.get("outcome")
                except (ValueError, TypeError):
                    auth_mode = None
            runs.append(
                {
                    "id": r[0],
                    "job_id": r[1],
                    "job_name": r[2],
                    "status": r[3],
                    "trigger": r[4],
                    "started_at": r[5],
                    "finished_at": r[6],
                    "attested": bool(r[7]),
                    "result_valid": None if r[8] is None else bool(r[8]),
                    "auth_mode": auth_mode,
                    "status_reason": status_reason,
                    # spec 30: measured cost vs the egg's feed-budget cap
                    "cost_usd": cost_usd,
                    "max_cost_usd": max_cost_usd,
                    "budget_outcome": budget_outcome,
                    # spec 27: sub-chicken lineage (None for ordinary chickens)
                    "parent_run_id": r[10],
                }
            )
        return {"ok": True, "op": "runs_list", "protocol": PROTOCOL_VERSION, "runs": runs}

    def _runs_show(self, req: dict[str, Any]) -> dict[str, Any]:
        run_id = str(req.get("run_id", ""))
        run = self._run_row(run_id) if run_id else None
        if run is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        return {"ok": True, "op": "runs_show", "protocol": PROTOCOL_VERSION, "run": run}

    def _runs_logs(self, req: dict[str, Any]) -> dict[str, Any]:
        run_id = str(req.get("run_id", ""))
        if self.db is None or self._run_row(run_id) is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        events = self.db.execute(
            "SELECT seq, ts, level, source, line FROM run_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return {
            "ok": True,
            "op": "runs_logs",
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "events": [
                {"seq": e[0], "ts": e[1], "level": e[2], "source": e[3], "line": e[4]} for e in events
            ],
        }

    # --- spec 29: the Beak — read-only live view into a running chicken -------

    def _workspace_peek(self, req: dict[str, Any]) -> dict[str, Any]:
        """The Beak: a running chicken's workspace file tree (no ``path``), or
        one file's size-capped content (``path`` given, workspace-relative).
        Available only while the workspace exists — terminal runs answer a
        clear closed-beak error because the abattoir already removed the
        workspace. This socket op is the single place that touches workspace
        files; the coop web layer only mirrors it over the transport seam.
        Path lock, size caps, and the private-config blocklist live in
        ``beak.py``; tree paths are workspace-relative, so no host path ever
        leaves the daemon in a payload."""
        run_id = str(req.get("run_id", ""))
        if not run_id:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "workspace_peek requires run_id"}
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        row = self.db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        status = row[0]
        if status in TERMINAL_STATES:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"the beak is closed: chicken {run_id!r} is terminal ({status}) —"
                    " the abattoir already removed its workspace"
                ),
                "workspace_gone": True,
                "run_id": run_id,
                "status": status,
            }
        ws = self.settings.data_dir / "workspaces" / run_id
        if not ws.is_dir():
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"the beak is closed: chicken {run_id!r} has no workspace yet (status={status}) —"
                    " the Hatchery provisions one when the egg hatches"
                ),
                "workspace_gone": True,
                "run_id": run_id,
                "status": status,
            }
        rel = req.get("path")
        try:
            if rel is None:
                return {
                    "ok": True,
                    "op": "workspace_peek",
                    "protocol": PROTOCOL_VERSION,
                    "run_id": run_id,
                    "status": status,
                    "mode": "tree",
                    "tree": beak.workspace_tree(ws),
                }
            rel = beak.safe_relpath(str(rel))
            if rel is None:
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": (
                        f"unsafe workspace path: {str(req.get('path'))!r} — the beak only serves"
                        " relative paths inside the chicken's workspace (no absolute paths, no '..')"
                    ),
                    "refused": "path",
                    "run_id": run_id,
                }
            peeked = beak.peek_file(ws, rel)
            if "error" in peeked:
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": peeked["error"],
                    "refused": peeked.get("refused"),
                    "run_id": run_id,
                    "path": rel,
                }
            return {
                "ok": True,
                "op": "workspace_peek",
                "protocol": PROTOCOL_VERSION,
                "run_id": run_id,
                "status": status,
                "mode": "file",
                **peeked,
            }
        except OSError as e:
            # raced with the abattoir: the workspace vanished mid-peek
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"the beak is closed: chicken {run_id!r} workspace disappeared mid-peek ({e})",
                "workspace_gone": True,
                "run_id": run_id,
            }

    # --- spec 22: lifecycle ops — stop & re-run a chicken --------------------

    def _ancestor_run_ids(self, run_id: str) -> set[str]:
        """Spec 27: the ancestor chain of a chicken via `parent_run_id`
        (a sub-chicken's parent, its parent's parent, …). Depth is capped at
        1 by enqueue, so this is short; the loop guards against cycles anyway."""
        if self.db is None:
            return set()
        ancestors: set[str] = set()
        seen = {run_id}
        cur = run_id
        while True:
            row = self.db.execute("SELECT parent_run_id FROM runs WHERE id = ?", (cur,)).fetchone()
            if row is None or row[0] is None or row[0] in seen:
                break
            ancestors.add(row[0])
            seen.add(row[0])
            cur = row[0]
        return ancestors

    async def _run_stop(self, req: dict[str, Any]) -> dict[str, Any]:
        """End a chicken deliberately. A queued egg is pulled from the pecking
        order → terminal `cancelled` (it never hatches); a chicken already in
        the Hatchery gets SIGTERM, then a hard kill → terminal `stopped`. Both
        paths get the abattoir's attested teardown and Headcount rows. Waits
        for the terminal state and returns it with its reason.

        Spec 27: when the request carries `caller_run_id` (a Farmhand running
        inside a sub-chicken), a target that is one of the caller's ancestors
        is refused — a sub-chicken cannot stop its ancestors."""
        run_id = str(req.get("run_id", ""))
        if not run_id:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "run_stop requires run_id"}
        if self.db is None or self.pipeline is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        row = self.db.execute("SELECT status, job_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        status, job_id = row
        if status in TERMINAL_STATES:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"chicken {run_id!r} is already terminal ({status}) — nothing to stop",
            }
        caller_run_id = req.get("caller_run_id")
        if caller_run_id:
            caller_run_id = str(caller_run_id)
            if run_id in self._ancestor_run_ids(caller_run_id):
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": (
                        f"refused: chicken {run_id!r} is an ancestor of the calling sub-chicken"
                        f" {caller_run_id!r} — a sub-chicken cannot stop its ancestors"
                    ),
                }
        # mark for cancellation in case the scheduler has not popped/admitted
        # the egg yet, and cancel it right away when it is sitting in the backlog
        self._cancel_requested.add(run_id)
        for i, it in enumerate(self.pending):
            if it["run_id"] == run_id:
                del self.pending[i]
                await self._cancel_item(it)
                break
        # wait for the terminal state, asking an active chicken to stop as soon
        # as it is (covers the race where the scheduler admitted it just now)
        deadline = time.monotonic() + 45.0
        final: str | None = None
        while time.monotonic() < deadline:
            row = self.db.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                break
            if row[0] in TERMINAL_STATES:
                final = row[0]
                break
            if row[0] != "queued":
                self.pipeline.request_stop(run_id)
            await asyncio.sleep(0.05)
        self._cancel_requested.discard(run_id)
        if final is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"chicken {run_id!r} did not reach a terminal state in time",
            }
        mrow = self.db.execute("SELECT manifest_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        reason = None
        if mrow and mrow[0]:
            with contextlib.suppress(ValueError, TypeError):
                reason = json.loads(mrow[0]).get("status_reason")
        return {
            "ok": True,
            "op": "run_stop",
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "job_id": job_id,
            "status": final,
            "reason": reason,
        }

    async def _run_rerun(self, req: dict[str, Any]) -> dict[str, Any]:
        """Spec 22: enqueue the same egg as a fresh run (new run_id), with the
        lineage recorded (`rerun_of` on the queue item → manifest + the
        `egg.queued` Headcount row). The new run goes through the normal
        queue/admission/pipeline exactly like any enqueue."""
        run_id = str(req.get("run_id", ""))
        if not run_id:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "run_rerun requires run_id"}
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        row = self.db.execute(
            "SELECT r.job_id, j.spec_path FROM runs r JOIN job_specs j ON j.id = r.job_id WHERE r.id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        job_id, spec_path = row
        if not spec_path:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"no spec path recorded for egg {job_id!r}; queue it again with `tc run <job.toml>`",
            }
        resp = await self._enqueue({"spec_path": spec_path, "trigger": "rerun", "rerun_of": run_id})
        if resp.get("ok"):
            resp["op"] = "run_rerun"
            resp["rerun_of"] = run_id
        return resp

    # --- validated results (spec 04) -----------------------------------------

    def _results_get(self, req: dict[str, Any]) -> dict[str, Any]:
        """Return the validated result for a run. Quarantined output (valid=0)
        is an error, never a result — but spec 14 lets the GUI inspect it: the
        quarantined payload carries the recorded schema errors and the raw
        output (held in the append-only row), never presented as valid.
        Expired on-disk artifacts (spec 08) are reported as expired, never served."""
        run_id = str(req.get("run_id", ""))
        if self.db is None or self._run_row(run_id) is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown chicken: {run_id!r}"}
        row = self.db.execute(
            "SELECT valid, output_json, artifacts_dir, schema_errors FROM results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"no clutch in the nest box for chicken {run_id!r} (it never reached collecting)",
            }
        valid, output_json, artifacts_dir, schema_errors = row
        # spec 08: the on-disk artifacts may have expired under the retention
        # TTL (the append-only row stays as the index) — expired, never served
        if artifacts_dir and not Path(artifacts_dir).exists():
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"clutch for chicken {run_id!r} composted"
                    f" (results_retention_months={self.settings.results_retention_months})"
                ),
                "expired": True,
                "run_id": run_id,
                "valid": bool(valid),
            }
        if not valid:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"chicken {run_id!r} quarantined: the clutch failed candling (schema validation)",
                "quarantined": True,
                "schema_errors": json.loads(schema_errors) if schema_errors else [],
                "raw_output": output_json,
                "artifacts_dir": artifacts_dir,
            }
        return {
            "ok": True,
            "op": "results_get",
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "valid": True,
            "output": json.loads(output_json),
            "artifacts_dir": artifacts_dir,
        }

    # --- per-job usage stats (spec 08) ---------------------------------------

    def _stats_list(self) -> dict[str, Any]:
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        rows = self.db.execute(
            "SELECT s.job_id, j.name, s.n_runs, s.tokens_in_mean, s.tokens_in_p50, s.tokens_in_p95,"
            " s.tokens_out_mean, s.tokens_out_p50, s.tokens_out_p95, s.tokens_p95, s.expected_cost,"
            " s.updated_at FROM job_usage_stats s JOIN job_specs j ON j.id = s.job_id"
            " ORDER BY s.job_id"
        ).fetchall()
        stats = [
            {
                "job_id": r[0],
                "job_name": r[1],
                "n_runs": r[2],
                "tokens_in_mean": r[3],
                "tokens_in_p50": r[4],
                "tokens_in_p95": r[5],
                "tokens_out_mean": r[6],
                "tokens_out_p50": r[7],
                "tokens_out_p95": r[8],
                "tokens_p95": r[9],
                "expected_cost_usd": r[10],
                "updated_at": r[11],
            }
            for r in rows
        ]
        return {"ok": True, "op": "stats_list", "protocol": PROTOCOL_VERSION, "stats": stats}

    def _stats_show(self, req: dict[str, Any]) -> dict[str, Any]:
        job_id = str(req.get("job_id", ""))
        if not job_id:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "stats_show requires job_id"}
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        row = self.db.execute(
            "SELECT s.job_id, j.name, s.n_runs, s.tokens_in_mean, s.tokens_in_p50, s.tokens_in_p95,"
            " s.tokens_out_mean, s.tokens_out_p50, s.tokens_out_p95, s.tokens_p95, s.expected_cost,"
            " s.updated_at FROM job_usage_stats s JOIN job_specs j ON j.id = s.job_id WHERE s.job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"no feed recorded for egg {job_id!r} (no finished chickens yet)",
            }
        return {
            "ok": True,
            "op": "stats_show",
            "protocol": PROTOCOL_VERSION,
            "stats": {
                "job_id": row[0],
                "job_name": row[1],
                "n_runs": row[2],
                "tokens_in_mean": row[3],
                "tokens_in_p50": row[4],
                "tokens_in_p95": row[5],
                "tokens_out_mean": row[6],
                "tokens_out_p50": row[7],
                "tokens_out_p95": row[8],
                "tokens_p95": row[9],
                "expected_cost_usd": row[10],
                "updated_at": row[11],
            },
        }

    # --- retention (spec 08) --------------------------------------------------

    async def _retention_loop(self) -> None:
        """Periodic retention sweep after the startup sweep (spec 08)."""
        assert self._stop is not None
        while not self._stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=RETENTION_SWEEP_INTERVAL_SEC)
                break
            assert self.db is not None and self.audit is not None
            self._last_retention = sweep_expired_results(self.settings, self.db, self.audit)

    def _retention_sweep(self) -> dict[str, Any]:
        """On-demand retention sweep over the socket: expire result artifacts
        older than `results_retention_months`, audit-log every deletion. The
        audit log itself is never TTL-deleted."""
        if self.db is None or self.audit is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        self._last_retention = sweep_expired_results(self.settings, self.db, self.audit)
        return {
            "ok": True,
            "op": "retention_sweep",
            "protocol": PROTOCOL_VERSION,
            "audit_retention": self.settings.audit_retention,
            **self._last_retention,
        }

    # --- read-only inspection for the GUI (spec 13) --------------------------

    def _jobs_list(self) -> dict[str, Any]:
        """Registered job specs (the append-only `job_specs` table) with their
        spec hashes — the Jobs view (spec 13) — and, since spec 16, the
        in-container spec path they were registered from (jobs can be picked
        by path in the GUI)."""
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        rows = self.db.execute(
            "SELECT id, name, spec_hash, created_at, spec_path FROM job_specs ORDER BY created_at, id"
        ).fetchall()
        jobs = [
            {
                "job_id": r[0],
                "name": r[1],
                "spec_hash": r[2],
                "registered_at": r[3],
                # spec 16: the in-container path the spec was registered from
                # (None for pre-v4 rows), so the Schedules view can offer it
                "spec_path": r[4],
            }
            for r in rows
        ]
        return {"ok": True, "op": "jobs_list", "protocol": PROTOCOL_VERSION, "jobs": jobs}

    def _audit_tail(self, req: dict[str, Any]) -> dict[str, Any]:
        """Last N audit rows, newest first, optional event-type filter (spec 13).
        Read-only: the log itself stays append-only."""
        if self.audit is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "audit log not ready"}
        raw_limit = req.get("limit", AUDIT_TAIL_DEFAULT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"audit_tail limit must be an integer between 1 and {AUDIT_TAIL_MAX}, got {raw_limit!r}",
            }
        if not 1 <= limit <= AUDIT_TAIL_MAX:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"audit_tail limit must be between 1 and {AUDIT_TAIL_MAX}, got {limit}",
            }
        event = req.get("event")
        if event is not None and not isinstance(event, str):
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "audit_tail event must be a string"}
        event = event or None
        events = self.audit.tail(limit, event)
        return {
            "ok": True,
            "op": "audit_tail",
            "protocol": PROTOCOL_VERSION,
            "limit": limit,
            "event": event,
            "count": len(events),
            "events": events,
        }

    def _settings_get(self) -> dict[str, Any]:
        """Effective settings for the Settings view (spec 13). The coop auth
        token is never included — `public_dict()` omits it, and the coop block
        carries only enabled/url/auth_required."""
        return {
            "ok": True,
            "op": "settings_get",
            "protocol": PROTOCOL_VERSION,
            "settings": {**self.settings.public_dict(), "coop": self._coop_block()},
        }

    # --- spec 31: the Farmhand view + runtime settings updates from the GUI ----

    async def _farmhand_info(self) -> dict[str, Any]:
        """The Farmhand view's payload (spec 31): the active transport mode(s)
        from ``farmhand_transport``, the HTTPS endpoint (``farmhand_https_path``
        on the coop host) and whether it is currently mounted, whether the coop
        requires the bearer, the exposed tool list (introspected from the MCP
        server built for the operator — the full 17-tool scope), and
        ready-to-paste client-config snippets. The bearer token never appears:
        the https snippet carries a placeholder header only."""
        from turbochickens.farmhand import TRANSPORTS, client_config_snippets, farmhand_tool_surface

        s = self.settings
        coop = self._coop_block()
        transport = s.farmhand_transport
        transports = [t for t in TRANSPORTS if t != "both" and (transport == "both" or t == transport)]
        https_url = s.coop_url.rstrip("/") + s.farmhand_https_path if s.coop_enabled else None
        return {
            "ok": True,
            "op": "farmhand_info",
            "protocol": PROTOCOL_VERSION,
            "transport": transport,
            "transports": transports,
            "https_path": s.farmhand_https_path,
            "https_url": https_url,
            "https_mounted": self.coop is not None and transport in ("https", "both"),
            "coop_enabled": bool(coop.get("enabled")),
            "auth_required": bool(coop.get("auth_required")),
            "tools": await farmhand_tool_surface(s),
            "stdio_command": {"command": "tc", "args": ["farmhand", "serve", "--transport", "stdio"]},
            "client_config": client_config_snippets(https_url),
        }

    async def _settings_set(self, req: dict[str, Any]) -> dict[str, Any]:
        """Apply one runtime settings update from the GUI (spec 31 — the
        Farmhand view's transport form). The patch is re-validated server-side
        through the ``Settings`` model, so ``extra="forbid"`` stays intact: an
        unknown key (e.g. ``max_tokens``) is a field-named error, never
        silently ignored. Only ``MUTABLE_SETTINGS_FIELDS`` may actually change;
        every other key is read-only at runtime (field-named refusal). When
        ``farmhand_transport`` changes, the coop mounts/unmounts the Farmhand
        HTTPS endpoint accordingly. The change is audited as
        ``settings.changed`` (field names + the new values — none of them
        secret), and the response is the fresh ``settings_get`` payload (the
        coop token is never included)."""
        patch = {k: v for k, v in req.items() if k not in ("op", "protocol")}
        if not patch or not all(isinstance(k, str) and k for k in patch):
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": 'settings_set requires a JSON object of setting updates, e.g. {"farmhand_transport": "https"}',
            }
        merged = self.settings.model_dump(mode="json")
        merged.update(patch)
        try:
            validated = Settings.model_validate(merged)
        except ValidationError as e:
            errors = [
                {"field": ".".join(str(x) for x in err["loc"]) or "<root>", "message": err["msg"]}
                for err in e.errors()
            ]
            return {"ok": False, "valid": False, "protocol": PROTOCOL_VERSION, "errors": errors}
        read_only = sorted(k for k in patch if k not in MUTABLE_SETTINGS_FIELDS)
        if read_only:
            errors = [
                {
                    "field": f"settings.{k}",
                    "message": (
                        f"{k} is read-only at runtime — mutable from the GUI:"
                        f" {', '.join(MUTABLE_SETTINGS_FIELDS)}"
                    ),
                }
                for k in read_only
            ]
            return {"ok": False, "valid": False, "protocol": PROTOCOL_VERSION, "errors": errors}
        changed: dict[str, Any] = {}
        for k in MUTABLE_SETTINGS_FIELDS:
            if k in patch and getattr(self.settings, k) != getattr(validated, k):
                changed[k] = getattr(validated, k)
                setattr(self.settings, k, getattr(validated, k))
        if "farmhand_transport" in changed and self.coop is not None:
            # mount/unmount the Farmhand HTTPS endpoint on the coop listener
            await self.coop.set_farmhand_transport(self.settings.farmhand_transport)
        if changed:
            assert self.audit is not None
            self.audit.append({"event": "settings.changed", "changes": changed})
        return {
            "ok": True,
            "op": "settings_set",
            "protocol": PROTOCOL_VERSION,
            "changed": changed,
            "settings": {**self.settings.public_dict(), "coop": self._coop_block()},
        }

    # --- spec 19: auth & access management for the GUI -------------------------

    def _seat_broker(self) -> SeatBroker:
        """The one SeatBroker for the auth ops — the same broker class the
        runner hands to the adapters, so the GUI path stores sessions 0600
        under <data_dir>/seats/ exactly as `tc auth login` does."""
        if self._broker is None:
            assert self.providers is not None
            self._broker = SeatBroker(self.settings, self.providers)
        return self._broker

    def _auth_providers(self) -> dict[str, Any]:
        """Every provider from providers.toml with its auth mode, plus the
        stored perch seat state (expiry only) — `SeatBroker.status()` never
        returns token material, and neither does this response."""
        return {
            "ok": True,
            "op": "auth_providers",
            "protocol": PROTOCOL_VERSION,
            "providers": self._seat_broker().status(),
        }

    def _auth_access(self) -> dict[str, Any]:
        """The declared access scope (spec 19, read-only groundwork): the
        per-run env allowlist the pipeline actually enforces, the declared
        network policy, and placeholders for tools / data / MCP so a future
        host-broker access layer (baseline v0.2 §7) has a home. The
        ``connectors`` entry (spec 28) is no longer a placeholder: it lists
        the registered gate names from connectors.toml — names only, never
        env/header material. No enforcement beyond what specs 02–09 already
        enforce."""
        placeholder = (
            "declared: none — placeholder for the host broker deciding what a job may touch (baseline v0.2 §7)"
        )
        try:
            gate_names = sorted(load_connectors(self.settings.connectors_path, self.settings.data_dir).connectors)
        except ConnectorsError:
            gate_names = []  # an unreadable registry never breaks the Auth view
        scope = [
            {"kind": kind, "declared": None, "note": placeholder} for kind in ("tools", "data", "mcp_servers")
        ]
        scope.append(
            {
                "kind": "connectors",
                "declared": gate_names,
                "note": (
                    "registered gates (connectors.toml); a chicken opts in via harness.connectors and gets"
                    " the gates injected into its harness runtime MCP config — names only, never env/header"
                    " material"
                ),
            }
        )
        return {
            "ok": True,
            "op": "auth_access",
            "protocol": PROTOCOL_VERSION,
            "access": {
                "env_allowlist": list(DEFAULT_CREDENTIAL_ALLOWLIST),
                "env_allowlist_enforced": True,
                "base_env_keys": list(BASE_ENV_KEYS),
                "network_policy": "none",
                "network_allowed": list(NETWORK_POLICIES),
                "network_enforced": False,
                "network_note": "declared in resources.network; enforcement is post-MVP",
                "scope": scope,
            },
        }

    def _auth_login_start(self, req: dict[str, Any]) -> dict[str, Any]:
        """Start the OAuth device flow for a seat provider. Returns only the
        user-facing user_code + verification_uri (+ expiry/interval) — the
        device_code stays in daemon memory. The session itself is stored by
        `auth_login_poll` on authorization, through the same SeatBroker path
        as the CLI."""
        provider = str(req.get("provider", ""))
        if not provider:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "auth_login_start requires provider"}
        try:
            grant = self._seat_broker().start_device_flow(provider)
        except SeatError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        self._device_grants[provider] = grant
        return {
            "ok": True,
            "op": "auth_login_start",
            "protocol": PROTOCOL_VERSION,
            "provider": provider,
            "mode": "seat",
            "user_code": grant.user_code,
            "verification_uri": grant.verification_uri,
            "interval": grant.interval,
            "expires_at": _iso(grant.expires_at),
        }

    def _auth_login_poll(self, req: dict[str, Any]) -> dict[str, Any]:
        """One poll step of a started device flow: pending / authorized /
        expired. On authorized the session is stored 0600 (same SeatBroker
        path as the CLI) and audited as `perch.claimed` — provider + mode only,
        never tokens or codes."""
        provider = str(req.get("provider", ""))
        if not provider:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "auth_login_poll requires provider"}
        grant = self._device_grants.get(provider)
        if grant is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"no device login in progress for provider {provider!r}; start one first",
            }
        if grant.expired:
            del self._device_grants[provider]
            return {
                "ok": True,
                "op": "auth_login_poll",
                "protocol": PROTOCOL_VERSION,
                "provider": provider,
                "status": "expired",
            }
        try:
            outcome = self._seat_broker().poll_device_flow(grant)
        except SeatError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        if outcome["status"] == "authorized":
            del self._device_grants[provider]
            session = outcome["session"]
            assert self.audit is not None
            self.audit.append({"event": "perch.claimed", "provider": provider, "mode": "seat"})
            return {
                "ok": True,
                "op": "auth_login_poll",
                "protocol": PROTOCOL_VERSION,
                "provider": provider,
                "status": "authorized",
                "session": {
                    "expires_at": _iso(session.expires_at),
                    "expired": session.expired,
                    "token_type": session.token_type,
                },
            }
        return {
            "ok": True,
            "op": "auth_login_poll",
            "protocol": PROTOCOL_VERSION,
            "provider": provider,
            "status": "pending",
            "interval": grant.interval,
            "expires_at": _iso(grant.expires_at),
        }

    def _auth_logout(self, req: dict[str, Any]) -> dict[str, Any]:
        """Release a provider's stored perch seat (tokens removed, not
        revoked) and audit `perch.released` — provider + mode only."""
        provider = str(req.get("provider", ""))
        if not provider:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "auth_logout requires provider"}
        try:
            self._seat_broker().logout(provider)
        except SeatError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        assert self.audit is not None
        self.audit.append({"event": "perch.released", "provider": provider, "mode": "seat"})
        return {"ok": True, "op": "auth_logout", "protocol": PROTOCOL_VERSION, "provider": provider}

    # --- spec 28: connectors registry CRUD (gates) ------------------------------

    def _connectors_list(self) -> dict[str, Any]:
        """The registered gates with non-secret summaries only: stdio gates
        expose command/args and the *names* of env keys; https gates expose the
        url and the header refs as references. Env values and resolved headers
        are never served. Audit is untouched (read-only)."""
        try:
            path = resolve_connectors_path(self.settings.connectors_path, self.settings.data_dir)
            config = load_connectors(self.settings.connectors_path, self.settings.data_dir)
        except ConnectorsError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        return {
            "ok": True,
            "op": "connectors_list",
            "protocol": PROTOCOL_VERSION,
            "source": str(path) if path is not None else None,
            "connectors": [public_entry(n, e) for n, e in sorted(config.connectors.items())],
        }

    def _connectors_add(self, req: dict[str, Any]) -> dict[str, Any]:
        """Register (or replace) one gate in the registry file. The payload is
        `{name, transport, ...}` validated strictly (field-named errors); the
        file is rewritten atomically (created 0600) and the change is audited
        as `gate.registered` — name + transport only, never env/header
        material. Re-adding an existing name replaces its entry."""
        name = str(req.get("name", ""))
        payload = {k: v for k, v in req.items() if k not in ("op", "protocol", "name")}
        entry, errors = validate_connector_payload(name, payload)
        if entry is None:
            assert self.audit is not None
            self.audit.append(
                {"event": "gate.rejected", "name": name or None, "fields": [e["field"] for e in errors]}
            )
            return {"ok": False, "valid": False, "protocol": PROTOCOL_VERSION, "errors": errors}
        try:
            path = resolve_connectors_path(self.settings.connectors_path, self.settings.data_dir)
            config = load_connectors(self.settings.connectors_path, self.settings.data_dir)
            raw = read_registry_raw(path)
        except ConnectorsError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        raw.setdefault("connectors", {})[name] = entry_to_dict(entry)
        # the whole file must still validate before it is written
        try:
            ConnectorsConfig.model_validate(raw)
        except ValidationError as e:
            return {
                "ok": False,
                "valid": False,
                "protocol": PROTOCOL_VERSION,
                "errors": [
                    {"field": fmt_error_loc(err["loc"]), "message": err["msg"]}
                    for err in e.errors()
                ],
            }
        source = save_registry(path, raw, self.settings.data_dir)
        assert self.audit is not None
        self.audit.append({"event": "gate.registered", "name": name, "transport": entry.transport})
        return {
            "ok": True,
            "op": "connectors_add",
            "protocol": PROTOCOL_VERSION,
            "name": name,
            "transport": entry.transport,
            "entry": public_entry(name, entry),
            "source": str(source),
        }

    def _connectors_rm(self, req: dict[str, Any]) -> dict[str, Any]:
        """Remove one gate from the registry file (audit `gate.removed`, name
        + transport only). Unknown names are a clean error."""
        name = str(req.get("name", ""))
        if not name:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "connectors_rm requires name"}
        try:
            path = resolve_connectors_path(self.settings.connectors_path, self.settings.data_dir)
            config = load_connectors(self.settings.connectors_path, self.settings.data_dir)
        except ConnectorsError as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        entry = config.connectors.get(name)
        if entry is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"unknown gate: no connector named {name!r} registered —"
                    f" known connectors: {', '.join(sorted(config.connectors)) or '(none)'}"
                ),
            }
        raw = read_registry_raw(path)
        raw.get("connectors", {}).pop(name, None)
        if path is not None:
            save_registry(path, raw, self.settings.data_dir)
        assert self.audit is not None
        self.audit.append({"event": "gate.removed", "name": name, "transport": entry.transport})
        return {"ok": True, "op": "connectors_rm", "protocol": PROTOCOL_VERSION, "name": name, "transport": entry.transport}

    # --- spec 15: upload + validate job-spec bundles from the GUI -------------

    def _jobs_upload(self, req: dict[str, Any]) -> dict[str, Any]:
        """Validate a job-spec bundle sent as JSON with base64-encoded files —
        ``{"files": {"job.toml": "…", "prompts/p.txt": "…"}}`` — and, only when
        valid, store it under ``<data_dir>/uploads/<job>-<spec_hash>/`` so it
        is enqueueable by that in-container spec path.

        Validation reuses the existing spec loader, so errors come back
        field-named exactly like ``tc jobs validate`` (``trigger.schedule: …``);
        the bundle must additionally contain the files the spec references
        (prompt, plain attachments — the loader itself checks the schema file;
        spec 25 `nestbox://` attachments are resolved from the nest box at
        provisioning, not shipped in the bundle). The
        total decoded size is capped at ``UPLOAD_MAX_BYTES``. Invalid bundles
        are never stored and never register a job; staging paths never leave
        the daemon (messages are rewritten to ``<bundle>``)."""
        files = req.get("files")
        if not isinstance(files, dict) or not files:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": "jobs_upload requires a 'files' object mapping bundle paths to base64 content",
            }
        if len(files) > UPLOAD_MAX_FILES:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"too many files in bundle: {len(files)} > {UPLOAD_MAX_FILES}",
            }
        if "job.toml" not in files:
            return {
                "ok": False,
                "valid": False,
                "protocol": PROTOCOL_VERSION,
                "errors": [
                    {"field": "job.toml", "message": "job.toml: the bundle must include a root-level job.toml"}
                ],
            }
        decoded: dict[str, bytes] = {}
        for key, b64 in files.items():
            rel = _safe_bundle_relpath(key)
            if rel is None:
                return {
                    "ok": False,
                    "valid": False,
                    "protocol": PROTOCOL_VERSION,
                    "errors": [
                        {
                            "field": str(key),
                            "message": (
                                f"{key}: unsafe bundle path — must be a relative path inside the "
                                "bundle (no absolute paths, no '..')"
                            ),
                        }
                    ],
                }
            if not isinstance(b64, str):
                return {
                    "ok": False,
                    "valid": False,
                    "protocol": PROTOCOL_VERSION,
                    "errors": [{"field": rel, "message": f"{rel}: content must be a base64 string"}],
                }
            try:
                decoded[rel] = base64.b64decode(b64, validate=True)
            except ValueError:
                return {
                    "ok": False,
                    "valid": False,
                    "protocol": PROTOCOL_VERSION,
                    "errors": [{"field": rel, "message": f"{rel}: content is not valid base64"}],
                }
        total = sum(len(blob) for blob in decoded.values())
        if total > UPLOAD_MAX_BYTES:
            return {
                "ok": False,
                "too_large": True,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"bundle too large: {total} bytes decoded > cap of {UPLOAD_MAX_BYTES} bytes "
                    f"({UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)"
                ),
                "size_bytes": total,
                "cap_bytes": UPLOAD_MAX_BYTES,
            }
        # stage the bundle, validate with the existing loader, store on success
        uploads = self.settings.uploads_dir
        staging = uploads / f".staging-{uuid.uuid4().hex[:12]}"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            for rel, blob in decoded.items():
                dest = staging / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
            try:
                spec = load_spec(staging / "job.toml")
            except SpecValidationError as e:
                errors = [_issue_dict(i, str(staging)) for i in e.issues]
                self._audit_upload_rejected(errors)
                return {
                    "ok": False,
                    "valid": False,
                    "protocol": PROTOCOL_VERSION,
                    "errors": errors,
                }
            # the spec references files by bundle-relative path — the runner
            # needs them present, so require them here with field-named errors
            errors = []
            if spec.input.prompt_file not in decoded:
                errors.append(
                    {
                        "field": "input.prompt_file",
                        "message": (
                            f"input.prompt_file: prompt file {spec.input.prompt_file!r} is not in the bundle"
                        ),
                    }
                )
            for att in spec.input.attachments:
                # spec 25: a nestbox:// attachment is resolved from the nest
                # box at provisioning, not shipped inside the bundle
                if is_nestbox_uri(att):
                    continue
                if att not in decoded:
                    errors.append(
                        {
                            "field": "input.attachments",
                            "message": f"input.attachments: attachment {att!r} is not in the bundle",
                        }
                    )
            from turbochickens.models import model_issues

            model_errors, warnings = model_issues(spec, self.providers, None)
            errors.extend(
                {
                    "field": e.split(":", 1)[0].strip(),
                    "message": e,  # verbatim, e.g. "harness.model: unknown provider …"
                }
                for e in model_errors
            )
            if errors:
                self._audit_upload_rejected(errors)
                return {"ok": False, "valid": False, "protocol": PROTOCOL_VERSION, "errors": errors, "warnings": warnings}
            digest = spec_hash(spec)
            target = uploads / f"{_safe_job_dir_name(spec.job.name)}-{digest}"
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        spec_path = target / "job.toml"
        assert self.audit is not None
        self.audit.append(
            {
                "event": "egg.candled",
                "job_id": spec.job.name,
                "spec_hash": f"sha256:{digest}",
                "spec_path": str(spec_path),
                "size_bytes": total,
                "n_files": len(decoded),
            }
        )
        return {
            "ok": True,
            "op": "jobs_upload",
            "protocol": PROTOCOL_VERSION,
            "valid": True,
            "job_id": spec.job.name,
            "spec_hash": digest,
            "spec_path": str(spec_path),
            "size_bytes": total,
            "files": sorted(decoded),
            "warnings": warnings,
        }

    def _audit_upload_rejected(self, errors: list[dict[str, Any]]) -> None:
        """Audit a rejected bundle — field names only, never file contents or
        host paths (spec 15)."""
        if self.audit is None:
            return
        self.audit.append(
            {"event": "egg.rejected", "fields": [e["field"] for e in errors]}
        )

    # --- cron scheduling (spec 06) -------------------------------------------

    async def _loopback_request(self, op: str, **payload: Any) -> dict[str, Any]:
        """Send one JSON-lines request to the daemon's own socket. The cron
        scheduler uses this so a firing schedule flows through the exact same
        op path (validation, queue, admission, audit) as a CLI `tc run`."""
        reader, writer = await asyncio.open_unix_connection(str(self.settings.socket_path))
        try:
            req = {"op": op, "protocol": PROTOCOL_VERSION, **payload}
            writer.write(json.dumps(req, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise ConnectionError("loopback: daemon closed the connection without a response")
            return json.loads(line.decode("utf-8"))
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _cron_fire(self, job_id: str, spec_path: str, spec_hash: str) -> None:
        """APScheduler callback: submit a cron-triggered run over the socket."""
        assert self.audit is not None
        try:
            resp = await self._loopback_request(
                "enqueue", spec_path=spec_path, spec_hash=spec_hash, trigger="cron"
            )
        except Exception as e:
            self.audit.append(
                {"event": "rooster.missed", "job_id": job_id, "error": f"{type(e).__name__}: {e}"}
            )
            return
        if resp.get("ok"):
            self.audit.append(
                {
                    "event": "rooster.crowed",
                    "job_id": job_id,
                    "run_id": resp.get("run_id"),
                    "spec_hash": f"sha256:{spec_hash}",
                    "trigger": "cron",
                }
            )
        else:
            self.audit.append(
                {"event": "rooster.missed", "job_id": job_id, "error": resp.get("error", "enqueue failed")}
            )

    def _load_schedules(self) -> None:
        """Re-register every persisted schedule with APScheduler on daemon start."""
        assert self.db is not None and self.cron is not None
        rows = self.db.execute("SELECT job_id, spec_path, spec_hash, schedule FROM schedules").fetchall()
        for job_id, spec_path, spec_hash, expr in rows:
            self.cron.add(job_id, spec_path, spec_hash, expr)

    async def _schedule_add(self, req: dict[str, Any]) -> dict[str, Any]:
        """Register a job's cron trigger (from its spec) with the scheduler."""
        spec_path_raw = req.get("spec_path")
        if not spec_path_raw:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "schedule_add requires spec_path"}
        spec_path = Path(str(spec_path_raw))
        try:
            spec = load_spec(spec_path)
        except SpecValidationError as e:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": "egg failed candling: " + "; ".join(str(i) for i in e.issues),
            }
        if spec.trigger.type != "cron" or not spec.trigger.schedule:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"egg {spec.job.name!r} has trigger.type={spec.trigger.type!r}; only cron-triggered eggs carry a crow",
            }
        digest = spec_hash(spec)
        expected = req.get("spec_hash")
        if expected and str(expected) != digest:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"egg hash mismatch: client sent sha256:{expected}, the spec on disk is sha256:{digest}",
            }
        job_id = spec.job.name
        expr = spec.trigger.schedule
        resolved = str(spec_path.resolve())
        async with self._db_lock:
            assert self.db is not None
            self.db.execute(
                "INSERT OR IGNORE INTO job_specs (id, name, spec_hash, spec_toml, created_at, spec_path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, spec.job.name, digest, spec_path.read_text(encoding="utf-8"), utcnow_iso(), resolved),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO schedules (job_id, spec_path, schedule, spec_hash, added_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, resolved, expr, digest, utcnow_iso()),
            )
            self.db.commit()
        assert self.cron is not None
        next_run = self.cron.add(job_id, resolved, digest, expr)
        assert self.audit is not None
        self.audit.append(
            {
                "event": "rooster.scheduled",
                "job_id": job_id,
                "schedule": expr,
                "spec_hash": f"sha256:{digest}",
                "next_run": next_run.isoformat() if next_run else None,
            }
        )
        return {
            "ok": True,
            "op": "schedule_add",
            "protocol": PROTOCOL_VERSION,
            "job_id": job_id,
            "schedule": expr,
            "spec_hash": digest,
            "next_run": next_run.isoformat() if next_run else None,
        }

    def _schedule_list(self) -> dict[str, Any]:
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        rows = self.db.execute(
            "SELECT job_id, spec_path, schedule, spec_hash, added_at FROM schedules ORDER BY added_at, job_id"
        ).fetchall()
        schedules = []
        for job_id, spec_path, expr, digest, added_at in rows:
            next_run = self.cron.next_run(job_id) if self.cron is not None else None
            schedules.append(
                {
                    "job_id": job_id,
                    "spec_path": spec_path,
                    "schedule": expr,
                    "spec_hash": digest,
                    "added_at": added_at,
                    "next_run": next_run.isoformat() if next_run else None,
                }
            )
        return {"ok": True, "op": "schedule_list", "protocol": PROTOCOL_VERSION, "schedules": schedules}

    async def _schedule_rm(self, req: dict[str, Any]) -> dict[str, Any]:
        job_id = str(req.get("job_id", ""))
        if not job_id:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "schedule_rm requires job_id"}
        async with self._db_lock:
            assert self.db is not None
            row = self.db.execute(
                "SELECT schedule, spec_hash FROM schedules WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return {
                    "ok": False,
                    "protocol": PROTOCOL_VERSION,
                    "error": f"no crow registered for egg {job_id!r}",
                }
            self.db.execute("DELETE FROM schedules WHERE job_id = ?", (job_id,))
            self.db.commit()
        if self.cron is not None:
            self.cron.remove(job_id)
        assert self.audit is not None
        self.audit.append(
            {"event": "rooster.unscheduled", "job_id": job_id, "schedule": row[0], "spec_hash": f"sha256:{row[1]}"}
        )
        return {"ok": True, "op": "schedule_rm", "protocol": PROTOCOL_VERSION, "job_id": job_id}


async def run_daemon(settings: Settings) -> None:
    """Run the daemon until shutdown op, SIGINT, or SIGTERM."""
    daemon = Daemon(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve()
