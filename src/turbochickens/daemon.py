"""The always-on supervisor daemon.

A single asyncio process listening on a Unix domain socket speaking
JSON-lines: one JSON request object per line, one JSON response object per
line. This socket is the one IPC contract — the CLI and the future scheduler
both use it, so manual and cron runs flow through the same queue.

Ops: ``ping``, ``status``, ``shutdown`` (spec 02) plus run submission and
inspection (spec 03): ``enqueue``, ``runs_list``, ``runs_show``, ``runs_logs``.
Runs flow through one queue with resource-weight admission control; over-budget
runs backlog in ``pending``, never rejected.
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
from turbochickens.adapter import AdapterNotImplemented, run_weight
from turbochickens.audit import AuditLog
from turbochickens.runner import TERMINAL_STATES, RunPipeline
from turbochickens.settings import Settings
from turbochickens.spec.loader import SpecValidationError, load_spec, spec_hash
from turbochickens.storage import init_db
from turbochickens.util import utcnow, utcnow_iso

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
        self._server: asyncio.AbstractServer | None = None
        self._stop: asyncio.Event | None = None
        self._db_lock = asyncio.Lock()
        self._sched_task: asyncio.Task | None = None
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
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(s.socket_path))
        os.chmod(s.socket_path, 0o600)
        self.pipeline = RunPipeline(s, self.db, self.audit)
        serve_task = asyncio.create_task(self._server.serve_forever())
        stop_task = asyncio.create_task(self._stop.wait())
        self._sched_task = asyncio.create_task(self._scheduler())
        try:
            await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._server.close()
            serve_task.cancel()
            stop_task.cancel()
            if self._sched_task is not None:
                self._sched_task.cancel()
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
            "alerts": self._alerts(),
        }

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
            make_adapter(spec)  # fail fast at enqueue if no implementation is registered
        except AdapterNotImplemented as e:
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


async def run_daemon(settings: Settings) -> None:
    """Run the daemon until shutdown op, SIGINT, or SIGTERM."""
    daemon = Daemon(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve()
