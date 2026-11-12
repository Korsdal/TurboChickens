"""JSON-lines client for the daemon's Unix domain socket."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from turbochickens.daemon import PROTOCOL_VERSION


class DaemonUnreachable(Exception):
    """The daemon socket does not answer (daemon down or wrong path)."""


def request(socket_path: str | Path, op: str, timeout: float = 5.0, **payload: Any) -> dict[str, Any]:
    """Send one JSON-lines request, return the parsed response object."""

    async def _go() -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            req = {"op": op, "protocol": PROTOCOL_VERSION, **payload}
            writer.write(json.dumps(req).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise DaemonUnreachable("daemon closed the connection without a response")
            try:
                return json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise DaemonUnreachable(f"invalid response from daemon: {e}") from e
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    try:
        return asyncio.run(asyncio.wait_for(_go(), timeout=timeout))
    except (OSError, asyncio.TimeoutError) as e:
        raise DaemonUnreachable(f"no daemon at {socket_path}: {e}") from e
