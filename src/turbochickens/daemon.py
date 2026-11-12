"""The always-on supervisor daemon.

A single asyncio process listening on a Unix domain socket speaking
JSON-lines: one JSON request object per line, one JSON response object per
line. This socket is the one IPC contract — the CLI and the future scheduler
both use it, so manual and cron runs flow through the same queue.

MVP ops (spec 02): ``ping``, ``status``, ``shutdown``. Run submission arrives
in spec 03; the queue and in-flight structures already exist and are reported
by ``status``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from turbochickens import __version__
from turbochickens.audit import AuditLog
from turbochickens.settings import Settings
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
        self.in_flight: dict[str, float] = {}  # run_id -> resource weight
        self.db: sqlite3.Connection | None = None
        self.audit: AuditLog | None = None
        self._server: asyncio.AbstractServer | None = None
        self._stop: asyncio.Event | None = None

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
        serve_task = asyncio.create_task(self._server.serve_forever())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._server.close()
            serve_task.cancel()
            stop_task.cancel()
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
        if op == "shutdown":
            self.request_stop()
            return {"ok": True, "op": "shutdown", "protocol": PROTOCOL_VERSION, "message": "shutting down"}
        return {"ok": False, "protocol": PROTOCOL_VERSION, "error": f"unknown op: {op!r}"}

    def _uptime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return round((utcnow() - self.started_at).total_seconds(), 3)

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
            "queue_depth": self.queue.qsize(),
            "in_flight": [
                {"run_id": run_id, "weight": weight} for run_id, weight in self.in_flight.items()
            ],
            "weight_budget": self.settings.weight_budget,
            "db_path": str(self.settings.db_path),
            "audit_log": str(self.settings.audit_log_path),
        }


async def run_daemon(settings: Settings) -> None:
    """Run the daemon until shutdown op, SIGINT, or SIGTERM."""
    daemon = Daemon(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve()
