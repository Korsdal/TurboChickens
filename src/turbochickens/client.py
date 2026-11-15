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


#: default round-trip budget for one socket request (spec 32 / G14: the async
#: client enforces this around connect + write + response read)
DEFAULT_TIMEOUT: float = 5.0


async def _round_trip(socket_path: str | Path, op: str, **payload: Any) -> dict[str, Any]:
    """One JSON-lines round trip on the daemon's Unix socket. Every failure
    mode maps to DaemonUnreachable — an over-limit response line (readline
    raises ValueError on LimitOverrunError), a closed connection, a malformed
    line, or an OSError — never an uncaught error."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=SOCKET_STREAM_LIMIT)
    try:
        req = {"op": op, "protocol": PROTOCOL_VERSION, **payload}
        writer.write(json.dumps(req).encode("utf-8") + b"\n")
        await writer.drain()
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as e:
            # response line longer than the protocol limit — the stream is
            # desynced after a LimitOverrunError, so treat coopd as unreachable
            raise DaemonUnreachable(
                f"response from coopd exceeds the {SOCKET_STREAM_LIMIT}-byte protocol limit"
            ) from e
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


async def request_async(
    socket_path: str | Path, op: str, timeout: float = DEFAULT_TIMEOUT, **payload: Any
) -> dict[str, Any]:
    """Send one JSON-lines request from inside a running event loop (spec 12:
    the coop web layer calls the daemon over this socket from the daemon's own
    loop), return the parsed response object. The timeout bounds the whole
    round trip (connect + write + read), so a wedged coopd can never hang the
    caller: on expiry the request raises DaemonUnreachable like any other
    unreachable-daemon case (spec 32 / G14)."""
    try:
        return await asyncio.wait_for(_round_trip(socket_path, op, **payload), timeout=timeout)
    except asyncio.TimeoutError as e:
        # spec 32 (G17): path-free like the OSError branch below — this
        # message is served by the coop's dead-socket 200/503 payloads, so it
        # never carries the host socket path (the local CLI knows the
        # configured path from its own settings)
        raise DaemonUnreachable(f"no coopd answering on the daemon socket: timed out after {timeout}s") from e
    except DaemonUnreachable:
        raise
    except OSError as e:
        # spec 32 (G17): this message is served by the coop's dead-socket
        # payloads, so it never carries the host socket path (the local CLI
        # knows the configured path from its own settings)
        raise DaemonUnreachable(
            f"no coopd answering on the daemon socket: {e.strerror or type(e).__name__}"
        ) from e


def request(
    socket_path: str | Path, op: str, timeout: float = DEFAULT_TIMEOUT, **payload: Any
) -> dict[str, Any]:
    """Send one JSON-lines request, return the parsed response object. The
    timeout is enforced inside ``request_async`` around the whole round trip."""

    async def _go() -> dict[str, Any]:
        return await request_async(socket_path, op, timeout=timeout, **payload)

    # request_async already maps every failure mode — including its own
    # round-trip timeout — to DaemonUnreachable, so no TimeoutError can
    # escape asyncio.run here (spec 32 / ticket 18)
    return asyncio.run(_go())
