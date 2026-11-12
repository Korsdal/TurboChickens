"""Coop — the TurboChickens GUI served by the daemon itself (spec 12).

When ``coop_enabled`` is set (``tc coop start``, or ``tc daemon start`` with
``coop_enabled = true`` — the coop container's CMD), the daemon process also
runs a small aiohttp server **in the same asyncio event loop**, serving the
static SPA at ``/`` and a JSON API under ``/api``.

Transport seam: every API handler is a thin client of the existing Unix-socket
protocol — it calls ``request(op, **payload)`` over the socket (spec 12) and
never opens the SQLite DB or artifact files directly, so the GUI cannot bypass
queueing, admission, validation, quarantine, attestation, or the append-only
stores.

Auth rule: bound to loopback with no token configured → open (same trust domain
as the socket). Bound non-loopback (the container case) — or a token configured
on any bind — a bearer token is required: the configured ``coop_auth_token``
wins, else an ephemeral token is generated at startup and printed exactly once
to stdout. 401 without ``Authorization: Bearer …``; the SPA shows a login card.
``/api/ping`` stays unauthenticated for the container healthcheck. No cookies;
the browser keeps the token in sessionStorage only. The token is never written
to the audit log.
"""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from turbochickens.client import DaemonUnreachable, request_async
from turbochickens.settings import Settings

#: static SPA bundled with the package (index + app.js + styles.css + icons)
STATIC_ROOT = files("turbochickens") / "coop_static"

CONTENT_TYPES: dict[str, tuple[str, str | None]] = {
    ".html": ("text/html", "utf-8"),
    ".js": ("application/javascript", "utf-8"),
    ".css": ("text/css", "utf-8"),
    ".svg": ("image/svg+xml", None),
}


def is_loopback(host: str) -> bool:
    """True when the bind host is a loopback address (or the name localhost)."""
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return host.strip().lower() == "localhost"


@dataclass(frozen=True)
class CoopAuth:
    """The auth decision for the coop HTTP surface (pure seam)."""

    required: bool
    token: str | None  # effective token when required
    ephemeral: bool  # True when generated at startup, not configured


def coop_auth(settings: Settings) -> CoopAuth:
    """Loopback + no token → open. Non-loopback (or any configured token) →
    bearer required; configured token wins, else an ephemeral one is minted."""
    configured = settings.coop_auth_token
    if configured is None and is_loopback(settings.coop_host):
        return CoopAuth(required=False, token=None, ephemeral=False)
    if configured is not None:
        return CoopAuth(required=True, token=configured, ephemeral=False)
    return CoopAuth(required=True, token=secrets.token_urlsafe(32), ephemeral=True)


class CoopTransport(Protocol):
    """The seam between the web layer and the daemon: same shape as the socket
    client's ``request(op, **payload)``. Production impl talks to the daemon
    over its own Unix socket (loopback); the web layer never touches the DB."""

    async def request(self, op: str, **payload: Any) -> dict[str, Any]: ...


class SocketTransport:
    """CoopTransport over the daemon's own Unix socket."""

    def __init__(self, socket_path: str | Path) -> None:
        self.socket_path = socket_path

    async def request(self, op: str, **payload: Any) -> dict[str, Any]:
        return await request_async(self.socket_path, op, **payload)


def _load_static() -> dict[str, tuple[bytes, str, str | None]]:
    """Read the bundled SPA files once at startup: {relpath: (body, content_type, charset)}."""
    out: dict[str, tuple[bytes, str, str | None]] = {}

    def walk(prefix: str, node: Any) -> None:
        for child in node.iterdir():
            rel = prefix + child.name
            if child.is_dir():
                walk(rel + "/", child)
            else:
                suffix = Path(child.name).suffix
                ct, cs = CONTENT_TYPES.get(suffix, ("application/octet-stream", None))
                out[rel] = (child.read_bytes(), ct, cs)

    walk("", STATIC_ROOT)
    return out


class CoopServer:
    """The HTTP server running inside the daemon's event loop (spec 12)."""

    def __init__(self, settings: Settings, transport: CoopTransport | None = None) -> None:
        self.settings = settings
        self.transport: CoopTransport = transport or SocketTransport(settings.socket_path)
        self.auth = coop_auth(settings)
        self.url = settings.coop_url
        self._static = _load_static()
        self._runner: web.AppRunner | None = None

    def status_block(self) -> dict[str, Any]:
        """The `coop` block of the status op — never contains the token."""
        return {"enabled": True, "url": self.url, "auth_required": self.auth.required}

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/api/ping", self._api_ping)
        app.router.add_get("/api/status", self._api_status)
        app.router.add_get("/", self._index)
        app.router.add_get("/{tail:.*}", self._static_file)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.coop_host, self.settings.coop_port)
        await site.start()
        if self.auth.required and self.auth.ephemeral:
            # printed exactly once, at startup; never logged or audited
            print(f"coop auth token: {self.auth.token}", flush=True)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- auth ---------------------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # static assets stay open so the SPA can load and show its login card
        # on a 401 from /api/*; /api/ping is the unauthenticated healthcheck
        if self.auth.required and request.path.startswith("/api/") and request.path != "/api/ping":
            if request.headers.get("Authorization", "") != f"Bearer {self.auth.token}":
                return web.json_response(
                    {"ok": False, "error": "unauthorized: send Authorization: Bearer <token>"}, status=401
                )
        return await handler(request)

    # --- /api/* — thin mirrors of socket ops over the transport seam --------

    async def _api_ping(self, request: web.Request) -> web.Response:
        try:
            resp = await self.transport.request("ping")
        except DaemonUnreachable as e:
            # healthcheck semantics: 503 when the supervisor socket is unreachable
            return web.json_response(
                {"ok": False, "op": "ping", "pong": False, "error": str(e)}, status=503
            )
        return web.json_response(resp)

    async def _api_status(self, request: web.Request) -> web.Response:
        try:
            resp = await self.transport.request("status")
        except DaemonUnreachable as e:
            # graceful degradation: structured daemon.up=false so the SPA can
            # render a clear banner instead of a broken page (spec 11, story 24)
            return web.json_response(
                {"ok": False, "op": "status", "daemon": {"up": False}, "error": str(e)}
            )
        return web.json_response(resp)

    # --- static SPA ----------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        return self._respond_static("index.html")

    async def _static_file(self, request: web.Request) -> web.Response:
        tail = request.match_info["tail"]
        if not tail or tail.startswith("/") or ".." in tail.split("/"):
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return self._respond_static(tail)

    def _respond_static(self, name: str) -> web.Response:
        entry = self._static.get(name)
        if entry is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        body, content_type, charset = entry
        return web.Response(body=body, content_type=content_type, charset=charset)
