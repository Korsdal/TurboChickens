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
output, or expired). Spec 15 adds the upload path: ``POST /api/jobs/validate``
(validates — and, when valid, stores — a spec bundle sent as JSON with
base64-encoded files, size-capped) and the bundle form of ``POST /api/runs``
(store + validate + enqueue in one call, so the browser never needs a path).
Spec 16 adds the schedule path: ``GET /api/schedule`` (schedule_list mirror),
``POST /api/schedule`` (schedule_add by in-container spec path or by a files
bundle — stored first, then scheduled by the stored path), and
``DELETE /api/schedule/{job_id}`` (schedule_rm). Spec 17 adds the stats path —
``GET /api/stats`` (stats_list mirror), ``GET /api/stats/{job_id}``
(stats_show mirror, 404 when the job has no stats yet) — and
``POST /api/retention/sweep`` (the on-demand retention_sweep op, whose
response lists every deletion as a ``result.expired`` entry).
Spec 19 adds the auth path: ``GET /api/auth/providers`` (each provider's auth
mode + seat session expiry, never token material), ``GET /api/auth/access``
(the declared access scope — env allowlist, network policy, tools/data/MCP/
connectors placeholders), ``POST /api/auth/login`` (starts the OAuth device
flow, relaying only the user-facing user code + verification URI),
``POST /api/auth/login/status`` (one poll step: pending / authorized /
expired; the daemon stores the session 0600 via the same SeatBroker as the
CLI), and ``POST /api/auth/logout`` — all thin mirrors of the new socket ops
over the transport seam, 422 on a refusal, 400 on a malformed body, and the
structured daemon-down payload (200) when the socket is dead. They sit behind
the bearer middleware like every other ``/api/*`` op, so auth-mutating ops
require the coop token whenever the server is bound non-loopback.

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
from turbochickens.settings import UPLOAD_MAX_BYTES, UPLOAD_MAX_HTTP_BODY, Settings

#: backstop for request bodies (spec 15): a bundle at the decoded cap encodes
#: to ~4/3 in base64 inside JSON — allow that plus slack; the daemon enforces
#: the real decoded-bytes cap and answers 413 with a clear error.
CLIENT_MAX_BODY = UPLOAD_MAX_HTTP_BODY

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
        app = web.Application(middlewares=[self._auth_middleware], client_max_size=CLIENT_MAX_BODY)
        app.router.add_get("/api/ping", self._api_ping)
        app.router.add_get("/api/status", self._api_status)
        # spec 13: read-only mirrors of the GUI socket ops
        app.router.add_get("/api/jobs", self._api_jobs)
        app.router.add_get("/api/audit", self._api_audit)
        app.router.add_get("/api/settings", self._api_settings)
        # spec 14: runs from the GUI
        app.router.add_post("/api/runs", self._api_run_enqueue)
        # spec 15: upload + validate job-spec bundles from the browser
        app.router.add_post("/api/jobs/validate", self._api_jobs_validate)
        app.router.add_get("/api/runs", self._api_runs_list)
        app.router.add_get("/api/runs/{run_id}", self._api_run_show)
        app.router.add_get("/api/runs/{run_id}/logs", self._api_run_logs)
        app.router.add_get("/api/results/{run_id}", self._api_result_get)
        # spec 16: schedules from the GUI
        app.router.add_get("/api/schedule", self._api_schedule_list)
        app.router.add_post("/api/schedule", self._api_schedule_add)
        app.router.add_delete("/api/schedule/{job_id}", self._api_schedule_rm)
        # spec 17: stats + retention from the GUI
        app.router.add_get("/api/stats", self._api_stats_list)
        app.router.add_get("/api/stats/{job_id}", self._api_stats_show)
        app.router.add_post("/api/retention/sweep", self._api_retention_sweep)
        # spec 19: auth & access management from the GUI
        app.router.add_get("/api/auth/providers", self._api_auth_providers)
        app.router.add_get("/api/auth/access", self._api_auth_access)
        app.router.add_post("/api/auth/login", self._api_auth_login)
        app.router.add_post("/api/auth/login/status", self._api_auth_login_status)
        app.router.add_post("/api/auth/logout", self._api_auth_logout)
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

    async def _json_body(self, request: web.Request) -> tuple[Any, web.Response | None]:
        """Read one JSON request body. Returns (body, None), or (None, response):
        413 when the raw body exceeds the client_max_size backstop (spec 15
        bundles are large), 400 when it is not JSON."""
        try:
            return await request.json(), None
        except web.HTTPRequestEntityTooLarge:
            return None, web.json_response(
                {
                    "ok": False,
                    "too_large": True,
                    "error": (
                        "request body too large: a spec bundle is capped at "
                        f"{UPLOAD_MAX_BYTES // (1024 * 1024)} MiB decoded"
                    ),
                },
                status=413,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, web.json_response({"ok": False, "error": "expected a JSON body"}, status=400)

    def _upload_error_response(
        self, resp: dict[str, Any] | None, down: web.Response | None
    ) -> web.Response | None:
        """Map a `jobs_upload` socket response to an HTTP error response, or
        None when the upload succeeded. 413 over the size cap, 422 carrying
        the field-named errors for an invalid bundle, 400 for a malformed
        request, and the structured daemon-down payload (200) when the socket
        is dead — never a 500."""
        if down is not None:
            return down
        assert resp is not None
        if resp.get("too_large"):
            return web.json_response(resp, status=413)
        if resp.get("valid") is False:
            return web.json_response(resp, status=422)
        if not resp.get("ok"):
            return web.json_response(resp, status=400)
        return None

    async def _api_run_enqueue(self, request: web.Request) -> web.Response:
        """POST /api/runs — enqueue a manual run, either by in-container spec
        path (``{"spec_path": …}``) or as a spec bundle (``{"files": {path:
        base64}}``, spec 15: stored + validated by the daemon's jobs_upload
        op, then enqueued by the stored path — the browser never needs one).
        The daemon re-validates the spec itself; a refused enqueue is a 422
        carrying the socket response, a malformed body a 400."""
        body, err = await self._json_body(request)
        if err is not None:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "op": "enqueue", "error": "POST /api/runs needs a JSON object body"},
                status=400,
            )
        if isinstance(body.get("files"), dict):
            upload_resp, upload_down = await self._call("jobs_upload", files=body["files"])
            mapped = self._upload_error_response(upload_resp, upload_down)
            if mapped is not None:
                return mapped
            assert upload_resp is not None and upload_resp.get("ok")
            payload: dict[str, Any] = {
                "spec_path": upload_resp["spec_path"],
                "spec_hash": upload_resp["spec_hash"],
                "trigger": "manual",
            }
        else:
            if not isinstance(body.get("spec_path"), str) or not body["spec_path"].strip():
                return web.json_response(
                    {
                        "ok": False,
                        "op": "enqueue",
                        "error": "POST /api/runs needs a JSON body with a spec_path string (an in-container path) or a files bundle",
                    },
                    status=400,
                )
            payload = {"spec_path": body["spec_path"].strip(), "trigger": "manual"}
            if body.get("spec_hash"):
                payload["spec_hash"] = str(body["spec_hash"])
        resp, down = await self._call("enqueue", **payload)
        if down is not None:
            return down
        assert resp is not None
        return web.json_response(resp, status=200 if resp.get("ok") else 422)

    # --- spec 15: upload + validate job-spec bundles from the browser ----------

    async def _api_jobs_validate(self, request: web.Request) -> web.Response:
        """POST /api/jobs/validate — validate a spec bundle ``{"files": {path:
        base64}}`` over the transport seam (the daemon's jobs_upload op, which
        also stores valid bundles under uploads/). 200 with the spec hash and
        the stored in-container spec path when valid, 422 with the field-named
        errors when not, 413 over the size cap, 400 for a malformed body."""
        body, err = await self._json_body(request)
        if err is not None:
            return err
        if not isinstance(body, dict) or not isinstance(body.get("files"), dict) or not body["files"]:
            return web.json_response(
                {
                    "ok": False,
                    "op": "jobs_upload",
                    "error": "POST /api/jobs/validate needs a JSON body with a 'files' object mapping bundle paths to base64 content",
                },
                status=400,
            )
        resp, down = await self._call("jobs_upload", files=body["files"])
        mapped = self._upload_error_response(resp, down)
        if mapped is not None:
            return mapped
        assert resp is not None
        return web.json_response(resp)

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
        if not resp.get("ok") and (
            "unknown run" in error or "no result stored" in error or "no usage stats" in error
        ):
            return web.json_response(resp, status=404)
        return web.json_response(resp)

    async def _api_run_show(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("runs_show", run_id=request.match_info["run_id"])

    async def _api_run_logs(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("runs_logs", run_id=request.match_info["run_id"])

    async def _api_result_get(self, request: web.Request) -> web.Response:
        return await self._mirror_resource("results_get", run_id=request.match_info["run_id"])

    # --- spec 16: schedules from the GUI ----------------------------------------

    async def _api_schedule_list(self, request: web.Request) -> web.Response:
        """GET /api/schedule — the schedules table with live next_run times
        (mirror of the schedule_list socket op)."""
        return await self._mirror("schedule_list")

    async def _api_schedule_add(self, request: web.Request) -> web.Response:
        """POST /api/schedule — add (or replace) a cron schedule, either by an
        in-container spec path (``{"spec_path": …, "spec_hash"?}`` — a
        registered or uploaded spec) or as a spec bundle (``{"files": …}``:
        stored + validated by the daemon's jobs_upload op, then scheduled by
        the stored path — the browser never needs one). The daemon
        re-validates the spec itself; a refusal (manual-trigger spec, invalid
        cron, hash mismatch, unreadable spec) is a 422 carrying the socket
        response so the form can show the error inline, a malformed body a
        400, and a dead socket the structured daemon-down payload (200)."""
        body, err = await self._json_body(request)
        if err is not None:
            return err
        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "op": "schedule_add", "error": "POST /api/schedule needs a JSON object body"},
                status=400,
            )
        if isinstance(body.get("files"), dict):
            upload_resp, upload_down = await self._call("jobs_upload", files=body["files"])
            mapped = self._upload_error_response(upload_resp, upload_down)
            if mapped is not None:
                return mapped
            assert upload_resp is not None and upload_resp.get("ok")
            payload: dict[str, Any] = {
                "spec_path": upload_resp["spec_path"],
                "spec_hash": upload_resp["spec_hash"],
            }
        else:
            if not isinstance(body.get("spec_path"), str) or not body["spec_path"].strip():
                return web.json_response(
                    {
                        "ok": False,
                        "op": "schedule_add",
                        "error": "POST /api/schedule needs a JSON body with a spec_path string (an in-container path) or a files bundle",
                    },
                    status=400,
                )
            payload = {"spec_path": body["spec_path"].strip()}
            if body.get("spec_hash"):
                payload["spec_hash"] = str(body["spec_hash"])
        resp, down = await self._call("schedule_add", **payload)
        if down is not None:
            return down
        assert resp is not None
        return web.json_response(resp, status=200 if resp.get("ok") else 422)

    async def _api_schedule_rm(self, request: web.Request) -> web.Response:
        """DELETE /api/schedule/{job_id} — remove a schedule; 404 when the job
        has no registered schedule, 200 with the socket payload otherwise, and
        the structured daemon-down payload (200) when the socket is dead."""
        resp, down = await self._call("schedule_rm", job_id=request.match_info["job_id"])
        if down is not None:
            return down
        assert resp is not None
        if not resp.get("ok") and "no schedule registered" in str(resp.get("error", "")):
            return web.json_response(resp, status=404)
        return web.json_response(resp)

    # --- spec 17: stats + retention from the GUI --------------------------------

    async def _api_stats_list(self, request: web.Request) -> web.Response:
        """GET /api/stats — per-job usage stats (mirror of the stats_list
        socket op): n, mean/p50/p95 tokens in+out, total p95, expected cost
        per run, per registered job with finished runs."""
        return await self._mirror("stats_list")

    async def _api_stats_show(self, request: web.Request) -> web.Response:
        """GET /api/stats/{job_id} — one job's usage stats (mirror of the
        stats_show socket op); 404 when the job has no stats yet, 200 with
        the structured daemon-down payload when the socket is dead."""
        return await self._mirror_resource("stats_show", job_id=request.match_info["job_id"])

    async def _api_retention_sweep(self, request: web.Request) -> web.Response:
        """POST /api/retention/sweep — run the on-demand retention sweep over
        the transport seam (the retention_sweep socket op): deletes expired
        on-disk artifacts, audit-logs each as result.expired, and answers 200
        with the summary (cutoff, expired entries, deleted count). A refused
        sweep is a 422 carrying the socket response; a dead socket gets the
        structured daemon-down payload (200), never a 500."""
        resp, down = await self._call("retention_sweep")
        if down is not None:
            return down
        assert resp is not None
        return web.json_response(resp, status=200 if resp.get("ok") else 422)

    # --- spec 19: auth & access management from the GUI -------------------------

    async def _api_auth_providers(self, request: web.Request) -> web.Response:
        """GET /api/auth/providers — every provider's auth mode plus its seat
        session state (expiry only, never token material): the auth_providers
        socket op mirrored over the transport seam."""
        return await self._mirror("auth_providers")

    async def _api_auth_access(self, request: web.Request) -> web.Response:
        """GET /api/auth/access — the declared access scope (env allowlist,
        network policy, tools/data/MCP/connector placeholders): the
        auth_access socket op mirrored, read-only."""
        return await self._mirror("auth_access")

    async def _api_auth_login_body(self, request: web.Request, op: str) -> tuple[str | None, web.Response | None]:
        """Shared body handling for the auth-mutating POSTs: a JSON object
        with a non-empty ``provider`` string. Returns (provider, None), or
        (None, response) with 400 on a malformed body."""
        body, err = await self._json_body(request)
        if err is not None:
            return None, err
        if not isinstance(body, dict) or not isinstance(body.get("provider"), str) or not body["provider"].strip():
            return None, web.json_response(
                {
                    "ok": False,
                    "op": op,
                    "error": "POST needs a JSON body with a provider string",
                },
                status=400,
            )
        return body["provider"].strip(), None

    async def _api_auth_mutation(self, request: web.Request, op: str) -> web.Response:
        """Run one auth-mutating socket op (auth_login_start / auth_login_poll /
        auth_logout) over the transport seam: 200 with the payload (pending /
        authorized / expired are all ok:true), 422 with the socket error on a
        refusal (unknown provider, not a seat provider, no login in progress,
        no stored session), 400 on a malformed body, and the structured
        daemon-down payload (200) when the socket is dead — never a 500. All
        of these sit behind the bearer middleware like every /api/* op, so
        they require the coop token whenever the server is non-loopback."""
        provider, err = await self._api_auth_login_body(request, op)
        if err is not None:
            return err
        assert provider is not None
        resp, down = await self._call(op, provider=provider)
        if down is not None:
            return down
        assert resp is not None
        return web.json_response(resp, status=200 if resp.get("ok") else 422)

    async def _api_auth_login(self, request: web.Request) -> web.Response:
        """POST /api/auth/login {provider} — start the OAuth device flow; the
        response carries the user-facing user code + verification URI to show
        in the browser (never the device code or any token material)."""
        return await self._api_auth_mutation(request, "auth_login_start")

    async def _api_auth_login_status(self, request: web.Request) -> web.Response:
        """POST /api/auth/login/status {provider} — one poll step of the
        started device flow: pending / authorized (session stored 0600 by the
        daemon, audited as auth.login) / expired."""
        return await self._api_auth_mutation(request, "auth_login_poll")

    async def _api_auth_logout(self, request: web.Request) -> web.Response:
        """POST /api/auth/logout {provider} — delete the provider's stored
        seat session (audited as auth.logout); 422 when none is stored."""
        return await self._api_auth_mutation(request, "auth_logout")

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
