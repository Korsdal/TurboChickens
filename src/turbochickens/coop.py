"""Coop — the TurboChickens GUI served by the daemon itself (spec 12).

When ``coop_enabled`` is set (``tc coop start``, or ``tc daemon start`` with
``coop_enabled = true`` — the coop container's CMD), the daemon process also
runs a small aiohttp server **in the same asyncio event loop**, serving the
static SPA at ``/`` and a JSON API under ``/api`` — ``/api/ping`` +
``/api/status`` (spec 12), the spec 13 read-only mirrors ``/api/jobs``,
``/api/audit?limit=&event=``, ``/api/settings``, and the spec 14 runs path:
``POST /api/runs`` (enqueue by in-container spec path, trigger=manual),
``GET /api/runs?job=&status=``, ``GET /api/runs/{id}``, ``GET /api/runs/{id}/logs``,
``GET /api/results/{run_id}`` (validated output, or quarantine errors + raw
output, or expired).

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
import json
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
        # spec 13: read-only mirrors of the GUI socket ops
        app.router.add_get("/api/jobs", self._api_jobs)
        app.router.add_get("/api/audit", self._api_audit)
        app.router.add_get("/api/settings", self._api_settings)
        # spec 14: runs from the GUI
        app.router.add_post("/api/runs", self._api_run_enqueue)
        app.router.add_get("/api/runs", self._api_runs_list)
        app.router.add_get("/api/runs/{run_id}", self._api_run_show)
        app.router.add_get("/api/runs/{run_id}/logs", self._api_run_logs)
        app.router.add_get("/api/results/{run_id}", self._api_result_get)
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

    async def _call(self, op: str, **payload: Any) -> tuple[dict[str, Any] | None, web.Response | None]:
        """One socket op over the transport seam. Returns (resp, None), or
        (None, down_response) when the daemon socket is unreachable — the SPA
        renders a clear empty state/banner instead of a broken page (spec 11,
        story 24)."""
        try:
            return await self.transport.request(op, **payload), None
        except DaemonUnreachable as e:
            return None, web.json_response(
                {"ok": False, "op": op, "daemon": {"up": False}, "error": str(e)}
            )

    async def _mirror(self, op: str, **payload: Any) -> web.Response:
        """Forward one socket op over the transport seam as a 200 JSON response
        (ok:false payloads included), or the structured daemon-down payload."""
        resp, down = await self._call(op, **payload)
        return down if down is not None else web.json_response(resp)  # type: ignore[arg-type]

    async def _api_status(self, request: web.Request) -> web.Response:
        return await self._mirror("status")

    # --- spec 13: Jobs / Audit / Settings mirrors -----------------------------

    async def _api_jobs(self, request: web.Request) -> web.Response:
        return await self._mirror("jobs_list")

    async def _api_audit(self, request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit")
        payload: dict[str, Any] = {}
        if raw_limit is not None:
            try:
                payload["limit"] = int(raw_limit)
            except ValueError:
                return web.json_response(
                    {"ok": False, "op": "audit_tail", "error": f"limit must be an integer, got {raw_limit!r}"},
                    status=400,
                )
        event = request.query.get("event")
        if event:
            payload["event"] = event
        return await self._mirror("audit_tail", **payload)

    async def _api_settings(self, request: web.Request) -> web.Response:
        return await self._mirror("settings_get")

    # --- spec 14: runs from the GUI -------------------------------------------

    async def _api_run_enqueue(self, request: web.Request) -> web.Response:
        """POST /api/runs — enqueue a manual run by in-container spec path.
        The daemon re-validates the spec itself; a refused enqueue is a 422
        carrying the socket response, a malformed body a 400."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("spec_path"), str)
            or not body["spec_path"].strip()
        ):
            return web.json_response(
                {
                    "ok": False,
                    "op": "enqueue",
                    "error": "POST /api/runs needs a JSON body with a spec_path string (an in-container path)",
                },
                status=400,
            )
        payload: dict[str, Any] = {"spec_path": body["spec_path"].strip(), "trigger": "manual"}
        if body.get("spec_hash"):
            payload["spec_hash"] = str(body["spec_hash"])
        resp, down = await self._call("enqueue", **payload)
        if down is not None:
            return down
        assert resp is not None
        return web.json_response(resp, status=200 if resp.get("ok") else 422)

    async def _api_runs_list(self, request: web.Request) -> web.Response:
        """GET /api/runs?job=&status= — the runs table, filterable."""
        payload: dict[str, Any] = {}
        job = request.query.get("job")
        if job:
            payload["job"] = job
        status = request.query.get("status")
        if status:
            payload["status"] = status
        return await self._mirror("runs_list", **payload)

    async def _mirror_resource(self, op: str, **payload: Any) -> web.Response:
        """GET mirror of a single-resource op: 200 with the payload — including
        ok:false flags the SPA renders (quarantined/expired) — 404 when the
        daemon reports the resource itself missing, 200 structured when down."""
        resp, down = await self._call(op, **payload)
        if down is not None:
            return down
        assert resp is not None
        error = str(resp.get("error", ""))
        if not resp.get("ok") and ("unknown run" in error or "no result stored" in error):
            return web.json_response(resp, status=404)
        return web.json_response(resp)

    async def _api_run_show(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("runs_show", run_id=request.match_info["run_id"])

    async def _api_run_logs(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("runs_logs", run_id=request.match_info["run_id"])

    async def _api_result_get(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("results_get", run_id=request.match_info["run_id"])

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
