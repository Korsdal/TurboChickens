"""JSON-lines client for the daemon's Unix domain socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from turbochickens.daemon import PROTOCOL_VERSION
from turbochickens.settings import SOCKET_STREAM_LIMIT


class DaemonUnreachable(Exception):
    """The coopd socket does not answer (coopd down or wrong path)."""


async def request_async(
    socket_path: str | Path, op: str, timeout: float = 5.0, **payload: Any
) -> dict[str, Any]:
    """Send one JSON-lines request from inside a running event loop (spec 12:
    the coop web layer calls the daemon over this socket from the daemon's own
    loop), return the parsed response object."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=SOCKET_STREAM_LIMIT)
        try:
            req = {"op": op, "protocol": PROTOCOL_VERSION, **payload}
            writer.write(json.dumps(req).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise DaemonUnreachable("coopd closed the connection without a response")
            try:
                return json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise DaemonUnreachable(f"invalid response from coopd: {e}") from e
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    except OSError as e:
        # spec 32 (G17): this message is served by the coop's dead-socket
        # payloads, so it never carries the host socket path (the local CLI
        # knows the configured path from its own settings)
        raise DaemonUnreachable(
            f"no coopd answering on the daemon socket: {e.strerror or type(e).__name__}"
        ) from e


def request(socket_path: str | Path, op: str, timeout: float = 5.0, **payload: Any) -> dict[str, Any]:
    """Send one JSON-lines request, return the parsed response object."""

    async def _go() -> dict[str, Any]:
        return await request_async(socket_path, op, timeout=timeout, **payload)

    try:
        return asyncio.run(asyncio.wait_for(_go(), timeout=timeout))
    except asyncio.TimeoutError as e:
        # spec 32 (G17): path-free — this error is served by the coop layer too
        raise DaemonUnreachable(f"no coopd answering on the daemon socket: timed out after {timeout}s") from e
