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

Spec 12 (coop): when ``coop_enabled`` is set, the same process also serves the
TurboChickens GUI over HTTP (aiohttp, same event loop) — static SPA at ``/``
and ``/api/ping`` + ``/api/status`` under ``/api``, thin mirrors of the socket
ops called over the daemon's own socket. Loopback + no token → open;
non-loopback (or any configured token) → bearer required, with an ephemeral
token printed once at startup when none is configured.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from turbochickens import __version__
from turbochickens.adapter import AdapterNotImplemented, ProviderConfigError, run_weight
from turbochickens.audit import AuditLog
from turbochickens.providers import ProvidersConfig, load_providers
from turbochickens.retention import sweep_expired_results
from turbochickens.runner import TERMINAL_STATES, RunPipeline
from turbochickens.scheduler import CronScheduler
from turbochickens.settings import Settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.storage import init_db
from turbochickens.util import utcnow, utcnow_iso

#: how often the retention sweeper re-runs after the startup sweep (spec 08)
RETENTION_SWEEP_INTERVAL_SEC = 3600.0

PROTOCOL_VERSION = 1


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
                "event": "daemon.started",
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
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(s.socket_path))
        os.chmod(s.socket_path, 0o600)
        # spec 12: coop — serve the GUI over HTTP in this same event loop
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
        serve_task = asyncio.create_task(self._server.serve_forever())
        stop_task = asyncio.create_task(self._stop.wait())
        self._sched_task = asyncio.create_task(self._scheduler())
        self._retention_task = asyncio.create_task(self._retention_loop())
        try:
            await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
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
                self.audit.append({"event": "daemon.stopped", "pid": os.getpid()})
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
                line = await reader.readline()
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
            return self._runs_list()
        if op == "runs_show":
            return self._runs_show(req)
        if op == "runs_logs":
            return self._runs_logs(req)
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
        try:
            spec = load_spec(spec_path)
        except SpecValidationError as e:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": "invalid job spec: " + "; ".join(str(i) for i in e.issues),
            }
        digest = spec_hash(spec)
        expected = req.get("spec_hash")
        if expected and str(expected) != digest:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"spec hash mismatch: client sent sha256:{expected}, file is sha256:{digest}",
            }
        try:
            weight = run_weight(spec.harness.adapter)
        except KeyError:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown adapter: {spec.harness.adapter!r}"}
        from turbochickens.adapter import make_adapter

        try:
            # fail fast at enqueue: unregistered adapter or unresolvable model/provider
            make_adapter(spec, providers=self.providers)
        except (AdapterNotImplemented, ProviderConfigError) as e:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": str(e)}
        job_id = spec.job.name
        run_id = "run-" + uuid.uuid4().hex[:12]
        started_at = utcnow_iso()
        async with self._db_lock:
            assert self.db is not None
            self.db.execute(
                "INSERT OR IGNORE INTO job_specs (id, name, spec_hash, spec_toml, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, spec.job.name, digest, spec_path.read_text(encoding="utf-8"), started_at),
            )
            self.db.execute(
                "INSERT INTO runs (id, job_id, status, trigger, started_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, job_id, "queued", trigger, started_at),
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
        await self.queue.put(item)
        assert self.audit is not None
        self.audit.append(
            {
                "event": "run.queued",
                "run_id": run_id,
                "job_id": job_id,
                "spec_hash": f"sha256:{digest}",
                "trigger": trigger,
                "weight": weight,
            }
        )
        return {
            "ok": True,
            "op": "enqueue",
            "protocol": PROTOCOL_VERSION,
            "run_id": run_id,
            "job_id": job_id,
            "status": "queued",
            "weight": weight,
            "queue_depth": self._queue_depth(),
        }

    # --- admission control + scheduler -------------------------------------

    def _admit(self, weight: float) -> bool:
        used = sum(self.in_flight.values())
        return weight <= self.settings.weight_budget - used + 1e-9

    async def _scheduler(self) -> None:
        """One queue, one admission path. Over-budget runs backlog in
        `pending` (FIFO) — nothing is ever rejected."""
        while self._stop is None or not self._stop.is_set():
            while self.pending and self._admit(self.pending[0]["weight"]):
                item = self.pending.pop(0)
                self.in_flight[item["run_id"]] = item["weight"]
                task = asyncio.create_task(self._run_item(item))
                self._run_tasks.add(task)
                task.add_done_callback(self._run_tasks.discard)
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=0.2)
                self.pending.append(item)
            except asyncio.TimeoutError:
                pass

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
            " r.manifest_json, r.usage_json FROM runs r JOIN job_specs j ON j.id = r.job_id WHERE r.id = ?",
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

    def _runs_list(self) -> dict[str, Any]:
        if self.db is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": "db not ready"}
        rows = self.db.execute(
            "SELECT r.id, r.job_id, j.name, r.status, r.trigger, r.started_at, r.finished_at,"
            " (SELECT COUNT(*) FROM teardown_attestations t WHERE t.run_id = r.id)"
            " FROM runs r JOIN job_specs j ON j.id = r.job_id ORDER BY r.rowid DESC"
        ).fetchall()
        runs = [
            {
                "id": r[0],
                "job_id": r[1],
                "job_name": r[2],
                "status": r[3],
                "trigger": r[4],
                "started_at": r[5],
                "finished_at": r[6],
                "attested": bool(r[7]),
            }
            for r in rows
        ]
        return {"ok": True, "op": "runs_list", "protocol": PROTOCOL_VERSION, "runs": runs}

    def _runs_show(self, req: dict[str, Any]) -> dict[str, Any]:
        run_id = str(req.get("run_id", ""))
        run = self._run_row(run_id) if run_id else None
        if run is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown run: {run_id!r}"}
        return {"ok": True, "op": "runs_show", "protocol": PROTOCOL_VERSION, "run": run}

    def _runs_logs(self, req: dict[str, Any]) -> dict[str, Any]:
        run_id = str(req.get("run_id", ""))
        if self.db is None or self._run_row(run_id) is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown run: {run_id!r}"}
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

    # --- validated results (spec 04) -----------------------------------------

    def _results_get(self, req: dict[str, Any]) -> dict[str, Any]:
        """Return the validated result for a run. Quarantined output (valid=0)
        is an error, never a result — it stays in quarantine for inspection."""
        run_id = str(req.get("run_id", ""))
        if self.db is None or self._run_row(run_id) is None:
            return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown run: {run_id!r}"}
        row = self.db.execute(
            "SELECT valid, output_json, artifacts_dir, schema_errors FROM results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"no result stored for run {run_id!r} (the run never reached collecting)",
            }
        valid, output_json, artifacts_dir, schema_errors = row
        if not valid:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"run {run_id!r} is quarantined: output failed schema validation",
                "quarantined": True,
                "schema_errors": json.loads(schema_errors) if schema_errors else [],
                "artifacts_dir": artifacts_dir,
            }
        # spec 08: the on-disk artifacts may have expired under the retention
        # TTL (the append-only row stays as the index)
        if artifacts_dir and not Path(artifacts_dir).exists():
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": (
                    f"result artifacts for run {run_id!r} expired"
                    f" (results_retention_months={self.settings.results_retention_months})"
                ),
                "expired": True,
                "run_id": run_id,
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
                "error": f"no usage stats for job {job_id!r} (no finished runs yet)",
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
                {"event": "schedule.fire_failed", "job_id": job_id, "error": f"{type(e).__name__}: {e}"}
            )
            return
        if resp.get("ok"):
            self.audit.append(
                {
                    "event": "schedule.fired",
                    "job_id": job_id,
                    "run_id": resp.get("run_id"),
                    "spec_hash": f"sha256:{spec_hash}",
                    "trigger": "cron",
                }
            )
        else:
            self.audit.append(
                {"event": "schedule.fire_failed", "job_id": job_id, "error": resp.get("error", "enqueue failed")}
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
                "error": "invalid job spec: " + "; ".join(str(i) for i in e.issues),
            }
        if spec.trigger.type != "cron" or not spec.trigger.schedule:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"job {spec.job.name!r} has trigger.type={spec.trigger.type!r}; only cron-triggered jobs carry a schedule",
            }
        digest = spec_hash(spec)
        expected = req.get("spec_hash")
        if expected and str(expected) != digest:
            return {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": f"spec hash mismatch: client sent sha256:{expected}, file is sha256:{digest}",
            }
        job_id = spec.job.name
        expr = spec.trigger.schedule
        resolved = str(spec_path.resolve())
        async with self._db_lock:
            assert self.db is not None
            self.db.execute(
                "INSERT OR IGNORE INTO job_specs (id, name, spec_hash, spec_toml, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (job_id, spec.job.name, digest, spec_path.read_text(encoding="utf-8"), utcnow_iso()),
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
                "event": "schedule.added",
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
                    "error": f"no schedule registered for job {job_id!r}",
                }
            self.db.execute("DELETE FROM schedules WHERE job_id = ?", (job_id,))
            self.db.commit()
        if self.cron is not None:
            self.cron.remove(job_id)
        assert self.audit is not None
        self.audit.append(
            {"event": "schedule.removed", "job_id": job_id, "schedule": row[0], "spec_hash": f"sha256:{row[1]}"}
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
