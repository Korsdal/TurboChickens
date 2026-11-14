"""The Farmhand — an MCP server over the farm's own socket ops (spec 26).

The Farmhand is a hired helper that does chores through the farm's tools: an
MCP server (official ``mcp`` Python SDK) whose tools are **thin wrappers** over
the daemon's existing JSON-lines socket ops, called via
``client.request_async`` over the 0600 daemon socket — the same ops the CLI
and the coop GUI use, so an MCP client can never bypass queueing, admission,
candling, quarantine, attestation, or the append-only stores.

Exposed tools (one per socket op): ``status``, ``jobs_list``, ``jobs_validate``
(the daemon's ``jobs_upload`` op — candle a bundle of egg files, storing a
valid one under uploads/), ``enqueue``, ``runs_list``, ``runs_show``,
``runs_logs``, ``results_get``, ``run_stop``, ``run_rerun``, ``schedule_add``,
``schedule_list``, ``schedule_rm``, ``stats_list``, ``stats_show``,
``audit_tail``, ``settings_get``. The MCP scope is deliberately **narrower**
than the GUI bearer: no ``auth_*`` mutations, no ``retention_sweep``, no
``shutdown``, and settings are read-only (``settings_get``).

Transports (settings ``farmhand_transport``, CLI ``--transport``):

- ``stdio`` — MCP on stdin/stdout in a ``tc farmhand serve`` process; gated by
  the 0600 daemon socket, so same-machine agents only.
- ``https`` — streamable-HTTP at ``farmhand_https_path`` (default ``/mcp``)
  served from the coop process on the **existing listener**, behind the same
  bearer middleware as ``/api/*`` (loopback + no token → open; non-loopback
  or any configured token → 401 without ``Authorization: Bearer …``). The
  SDK's Starlette app is mounted into the coop's aiohttp server through a
  small ASGI bridge (``asgi_request``), so no second server/port is needed.
- ``both`` — the two at once (``tc farmhand serve --transport both`` runs the
  stdio server and the HTTPS endpoint in one process; a coopd with
  ``coop_enabled`` serves ``/mcp`` alongside the GUI).

Secrecy: no tool result ever contains token/seat/device-code material — the
socket ops already guarantee that (``settings_get`` omits ``coop_auth_token``,
the ``auth_*`` ops are not exposed), and the coop bearer itself never appears
in any tool payload.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from aiohttp import web
from mcp.server.mcpserver import MCPServer

from turbochickens import client
from turbochickens.client import DaemonUnreachable
from turbochickens.settings import SOCKET_STREAM_LIMIT, UPLOAD_MAX_HTTP_BODY, Settings

#: the MCP server name advertised to clients
FARMHAND_NAME = "farmhand"

#: transports `tc farmhand serve --transport` accepts (settings enum mirrors it)
TRANSPORTS = ("stdio", "https", "both")

#: hop-by-hop headers never forwarded from an ASGI response into aiohttp
_HOP_BY_HOP = {
    b"connection",
    b"transfer-encoding",
    b"keep-alive",
    b"upgrade",
    b"content-length",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
}


def spec_bundle_b64(spec_path: Path) -> dict[str, str]:
    """Read an egg spec at ``spec_path`` into a ``jobs_upload`` bundle
    (base64-encoded files): job.toml plus the prompt, schema, and plain
    attachments the egg references. ``nestbox://`` chained clutches (spec 25)
    resolve from the Nest Box at provisioning, so they are not shipped.

    Raises ``SpecValidationError`` (field-named issues) or ``OSError`` — the
    caller turns those into a clean tool error."""
    from turbochickens.chaining import is_nestbox_uri
    from turbochickens.spec.loader import load_spec

    spec = load_spec(spec_path)
    base = spec_path.parent
    blobs: dict[str, bytes] = {"job.toml": spec_path.read_bytes()}
    refs = [spec.input.prompt_file, spec.output.schema_file]
    refs.extend(a for a in spec.input.attachments if not is_nestbox_uri(a))
    for rel in refs:
        blobs[rel] = (base / rel).read_bytes()
    return {key: base64.b64encode(blob).decode("ascii") for key, blob in blobs.items()}


def build_server(settings: Settings) -> MCPServer:
    """Build the Farmhand MCP server: one tool per socket op, each a thin
    wrapper that round-trips ``client.request_async`` over the daemon's 0600
    socket and returns the socket response verbatim as the tool result."""
    socket_path = settings.socket_path

    async def call(op: str, timeout: float = 10.0, **payload: Any) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(client.request_async(socket_path, op, **payload), timeout)
        except DaemonUnreachable as e:
            return {"ok": False, "op": op, "daemon": {"up": False}, "error": str(e)}
        except TimeoutError:
            return {"ok": False, "op": op, "error": f"coopd did not answer {op!r} within {timeout}s"}

    server = MCPServer(
        name=FARMHAND_NAME,
        instructions=(
            "The Farmhand — chores on the TurboChickens farm through coopd's own socket ops. "
            "Eggs (job specs) are candled, queued in the pecking order, and hatched into chickens "
            "(runs); chickens lay clutches (validated results) in the nest box; the rooster keeps "
            "cron crows (schedules); the headcount (audit log) records everything. Every tool "
            "returns the daemon's socket response verbatim."
        ),
    )

    @server.tool(name="status", description="Coopd status: uptime, pecking-order depth, in-flight chickens, weight budget, schedules, alerts, retention, coop block (socket op `status`).")
    async def status() -> dict[str, Any]:
        return await call("status")

    @server.tool(name="jobs_list", description="Registered eggs (job specs) with spec hashes and registered spec paths (socket op `jobs_list`).")
    async def jobs_list() -> dict[str, Any]:
        return await call("jobs_list")

    @server.tool(
        name="jobs_validate",
        description=(
            "Candle an egg: validate a job-spec bundle and, when valid, store it under uploads/ so it is "
            "enqueueable by the returned spec_path (socket op `jobs_upload`). Pass either `files` "
            "({bundle path: text content}, e.g. job.toml + prompts/… + schemas/…) or `spec_path` to an "
            "egg on this machine, which is read into a bundle locally."
        ),
    )
    async def jobs_validate(
        files: dict[str, str] | None = None, spec_path: str | None = None
    ) -> dict[str, Any]:
        if files is not None:
            if not files:
                return {
                    "ok": False,
                    "op": "jobs_upload",
                    "error": "jobs_validate needs a non-empty 'files' object (bundle path -> content) or a 'spec_path'",
                }
            payload = {
                str(key): base64.b64encode(str(value).encode("utf-8")).decode("ascii")
                for key, value in files.items()
            }
        elif spec_path:
            from turbochickens.spec.loader import SpecValidationError

            try:
                payload = spec_bundle_b64(Path(spec_path))
            except SpecValidationError as e:
                return {
                    "ok": False,
                    "op": "jobs_upload",
                    "valid": False,
                    "errors": [
                        {"field": ".".join(i.loc) if i.loc else "<spec>", "message": str(i)}
                        for i in e.issues
                    ],
                }
            except OSError as e:
                return {"ok": False, "op": "jobs_upload", "error": f"cannot read the egg at {spec_path!r}: {e}"}
        else:
            return {
                "ok": False,
                "op": "jobs_upload",
                "error": "jobs_validate needs a 'files' object (bundle path -> content) or a 'spec_path'",
            }
        return await call("jobs_upload", timeout=60.0, files=payload)

    @server.tool(name="enqueue", description="Queue an egg for hatching: enqueue a run by spec path (socket op `enqueue`, trigger defaults to manual). Returns run_id, status=queued, weight, pecking-order depth.")
    async def enqueue(spec_path: str, spec_hash: str | None = None, trigger: str = "manual") -> dict[str, Any]:
        payload: dict[str, Any] = {"spec_path": spec_path, "trigger": trigger}
        if spec_hash:
            payload["spec_hash"] = spec_hash
        return await call("enqueue", timeout=10.0, **payload)

    @server.tool(name="runs_list", description="List chickens (runs), most recent first; optional job and status filters (socket op `runs_list`).")
    async def runs_list(job: str | None = None, status: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if job:
            payload["job"] = job
        if status:
            payload["status"] = status
        return await call("runs_list", **payload)

    @server.tool(name="runs_show", description="Show one chicken: lifecycle fields, manifest, abattoir attestation, result row (socket op `runs_show`).")
    async def runs_show(run_id: str) -> dict[str, Any]:
        return await call("runs_show", run_id=run_id)

    @server.tool(name="runs_logs", description="One chicken's streamed events: sanitized harness lines + state transitions (socket op `runs_logs`).")
    async def runs_logs(run_id: str) -> dict[str, Any]:
        return await call("runs_logs", run_id=run_id)

    @server.tool(name="results_get", description="Fetch a chicken's clutch from the nest box: validated output only; quarantined clutches answer with schema errors + raw output (never valid), expired ones as expired (socket op `results_get`).")
    async def results_get(run_id: str) -> dict[str, Any]:
        return await call("results_get", run_id=run_id)

    @server.tool(name="run_stop", description="Stop a chicken deliberately (socket op `run_stop`): a queued egg is cancelled (never hatches), a running chicken gets SIGTERM then a hard kill — both attested. Waits for the terminal state.")
    async def run_stop(run_id: str) -> dict[str, Any]:
        return await call("run_stop", timeout=60.0, run_id=run_id)

    @server.tool(name="run_rerun", description="Re-queue the same egg as a fresh chicken with lineage recorded (socket op `run_rerun`).")
    async def run_rerun(run_id: str) -> dict[str, Any]:
        return await call("run_rerun", timeout=10.0, run_id=run_id)

    @server.tool(name="schedule_add", description="Register an egg's cron trigger as a crow with the rooster (socket op `schedule_add`; the egg must be cron-triggered).")
    async def schedule_add(spec_path: str, spec_hash: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"spec_path": spec_path}
        if spec_hash:
            payload["spec_hash"] = spec_hash
        return await call("schedule_add", timeout=10.0, **payload)

    @server.tool(name="schedule_list", description="The rooster's registered crows with live next crow times (socket op `schedule_list`).")
    async def schedule_list() -> dict[str, Any]:
        return await call("schedule_list")

    @server.tool(name="schedule_rm", description="Remove an egg's crow from the rooster (socket op `schedule_rm`).")
    async def schedule_rm(job_id: str) -> dict[str, Any]:
        return await call("schedule_rm", job_id=job_id)

    @server.tool(name="stats_list", description="The feed ledger: per-egg usage rollups (n, mean/p50/p95 tokens in+out, total p95, expected feed per chicken) (socket op `stats_list`).")
    async def stats_list() -> dict[str, Any]:
        return await call("stats_list")

    @server.tool(name="stats_show", description="One egg's feed ledger in detail (socket op `stats_show`).")
    async def stats_show(job_id: str) -> dict[str, Any]:
        return await call("stats_show", job_id=job_id)

    @server.tool(name="audit_tail", description="The headcount: last N audit rows, newest first, optional event-type filter (socket op `audit_tail`; limit 1..1000, default 100).")
    async def audit_tail(limit: int = 100, event: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"limit": limit}
        if event:
            payload["event"] = event
        return await call("audit_tail", **payload)

    @server.tool(name="settings_get", description="Effective settings, read-only (socket op `settings_get`) — the coop auth token is never included.")
    async def settings_get() -> dict[str, Any]:
        return await call("settings_get")

    return server


async def asgi_request(app: Any, request: web.Request) -> web.StreamResponse:
    """Serve one aiohttp request through an ASGI app — the bridge that mounts
    the MCP streamable-HTTP (Starlette) app into the coop's aiohttp server
    without a second listener."""
    body = await request.read()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": request.path,
        "raw_path": request.raw_path.encode("latin-1"),
        "query_string": request.query_string.encode("latin-1"),
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in request.headers.items()],
        "client": request.transport.get_extra_info("peername") if request.transport else None,
        "server": request.transport.get_extra_info("sockname") if request.transport else None,
        "state": {},
    }
    body_sent = False
    disconnect = asyncio.Event()
    resp: web.StreamResponse | None = None

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal resp
        if message["type"] == "http.response.start":
            headers = [
                (k.decode("latin-1"), v.decode("latin-1"))
                for k, v in message.get("headers", [])
                if k.lower() not in _HOP_BY_HOP
            ]
            resp = web.StreamResponse(status=message["status"], headers=headers)
            await resp.prepare(request)
        elif message["type"] == "http.response.body":
            assert resp is not None
            chunk = message.get("body", b"")
            if chunk:
                await resp.write(chunk)
            if not message.get("more_body"):
                await resp.write_eof()

    try:
        await app(scope, receive, send)
    finally:
        disconnect.set()
    if resp is None:  # the app returned without responding — never a broken page
        resp = web.StreamResponse(status=500)
        await resp.prepare(request)
        await resp.write_eof()
    return resp


class FarmhandHTTP:
    """The Farmhand's streamable-HTTP endpoint (spec 26): the MCP server's
    Starlette app mounted at ``farmhand_https_path`` on the coop listener,
    behind the same bearer rule as ``/api/*`` (loopback + no token → open).

    Used two ways: mounted into the coop's aiohttp app (``handler`` +
    ``wants``), or standalone as the HTTPS side of ``tc farmhand serve``
    (``auth_middleware`` + ``handler`` in its own app on coop_host:coop_port)."""

    def __init__(self, settings: Settings) -> None:
        from turbochickens.coop import coop_auth

        self.settings = settings
        self.path = settings.farmhand_https_path
        self.auth = coop_auth(settings)
        self.server = build_server(settings)
        self._asgi = self.server.streamable_http_app(
            streamable_http_path=self.path,
            max_request_body_size=SOCKET_STREAM_LIMIT,
            host=settings.coop_host,
        )
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._life_task: asyncio.Task | None = None

    def wants(self, path: str) -> bool:
        """True when an HTTP path belongs to the Farmhand endpoint."""
        return path == self.path or path.startswith(self.path + "/")

    async def start(self) -> None:
        """Run the ASGI lifespan (starts the SDK's streamable-HTTP session
        manager) and wait for it to be ready."""
        started = False

        async def _lifespan() -> None:
            nonlocal started

            async def receive() -> dict[str, Any]:
                nonlocal started
                if not started:
                    started = True
                    return {"type": "lifespan.startup"}
                await self._shutdown.wait()
                return {"type": "lifespan.shutdown"}

            async def send(message: dict[str, Any]) -> None:
                if message["type"] == "lifespan.startup.complete":
                    self._ready.set()
                elif message["type"] in ("lifespan.startup.failed", "lifespan.shutdown.failed"):
                    raise RuntimeError(str(message))

            await self._asgi(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}, "state": {}},
                receive,
                send,
            )

        self._life_task = asyncio.create_task(_lifespan())
        await asyncio.wait_for(self._ready.wait(), timeout=10.0)

    async def stop(self) -> None:
        self._shutdown.set()
        if self._life_task is not None:
            try:
                await asyncio.wait_for(self._life_task, timeout=10.0)
            except Exception:  # never block shutdown on a stuck lifespan
                self._life_task.cancel()
            self._life_task = None

    async def handler(self, request: web.Request) -> web.StreamResponse:
        """aiohttp handler: bridge the request into the MCP ASGI app."""
        return await asgi_request(self._asgi, request)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        """The coop bearer rule for the Farmhand endpoint (standalone mode):
        required when non-loopback or a token is configured; loopback + no
        token → open, exactly like /api/*. The token is never logged."""
        if self.auth.required and self.wants(request.path):
            if request.headers.get("Authorization", "") != f"Bearer {self.auth.token}":
                return web.json_response(
                    {"ok": False, "error": "unauthorized: send Authorization: Bearer <token>"}, status=401
                )
        return await handler(request)


async def serve_stdio(settings: Settings) -> None:
    """Serve the Farmhand over stdio (same-machine agents; the 0600 daemon
    socket gates who can reach coopd)."""
    await build_server(settings).run_stdio_async()


async def serve_https(settings: Settings) -> None:
    """Serve the Farmhand's streamable-HTTP endpoint on the coop listener
    (coop_host:coop_port at farmhand_https_path), same bearer rule as /api/*."""
    farmhand = FarmhandHTTP(settings)
    await farmhand.start()
    app = web.Application(middlewares=[farmhand.auth_middleware], client_max_size=UPLOAD_MAX_HTTP_BODY)
    app.router.add_route("*", farmhand.path, farmhand.handler)
    app.router.add_route("*", farmhand.path + "/", farmhand.handler)
    # shutdown_timeout bounds the handler drain at cleanup (a hung MCP SSE
    # stream must never wedge the process)
    runner = web.AppRunner(app, shutdown_timeout=15.0)
    await runner.setup()
    site = web.TCPSite(runner, settings.coop_host, settings.coop_port)
    await site.start()
    if farmhand.auth.required and farmhand.auth.ephemeral:
        # printed exactly once, at startup; never logged or audited
        print(f"coop auth token: {farmhand.auth.token}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await farmhand.stop()


async def serve_transport(settings: Settings, transport: str) -> None:
    """`tc farmhand serve --transport stdio|https|both` — "both" runs the
    stdio server and the HTTPS endpoint simultaneously in one process. When
    the first component ends (e.g. the stdio client hangs up), the other is
    cancelled and the process exits."""
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {TRANSPORTS}, got {transport!r}")
    tasks: list[asyncio.Task] = []
    if transport in ("stdio", "both"):
        tasks.append(asyncio.create_task(serve_stdio(settings)))
    if transport in ("https", "both"):
        tasks.append(asyncio.create_task(serve_https(settings)))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        if not t.cancelled() and t.exception() is not None:
            raise t.exception()  # type: ignore[misc]
