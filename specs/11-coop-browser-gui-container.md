# 11: TurboChickens GUI in the coop container

**Status:** ready-for-agent
**Blocked by:** none (builds on the landed daemon/socket contract, specs 02–08; surfaces specs 09/10 when they land)

# Spec: the TurboChickens browser GUI, served by the coop container

## Problem Statement

TurboChickens is operable only from a shell today: `tc status`, `tc runs list`, `tc results get`, `tc schedule add`. An operator who wants to check whether the platform is healthy, trigger a job, watch a run, inspect a quarantined output, or see what a job costs has to open a terminal, remember commands, and read raw JSON. There is no at-a-glance view of the flock: queue depth, in-flight chickens, alerts, schedules, retention state, per-job cost. The baseline roadmap already names this gap ("web UI / TUI dashboard over the same local socket"), and the operator's mental model is missing its coop — one place where you see and manage all your turbochickens.

On top of that, deployment is a Python install plus a manually started daemon. The operator wants the whole platform — daemon, queue, store, audit log, and now the GUI — to run as one Docker container called **coop**, with state on a volume, so managing the chickens is `docker run` plus a browser tab.

## Solution

The **GUI (branded TurboChickens)** is served by the daemon itself and packaged as a Docker image named `coop`. One container runs one daemon process; when coop is enabled, that same process also serves a small HTTP server on a configurable port. The web layer is a *thin client of the existing Unix-socket protocol*: every GUI action maps to a socket op (`status`, `enqueue`, `runs_list`, `runs_show`, `runs_logs`, `results_get`, `schedule_add/list/rm`, `stats_list/show`, `retention_sweep`, plus three new read-only ops: `jobs_list`, `audit_tail`, `settings_get`). The GUI therefore cannot bypass a single invariant — queueing, admission control, schema validation, quarantine, attestation, append-only audit all stay exactly where they are.

The frontend is a dependency-free single-page app (vanilla JS, hash-routed sections, polling) served at `/`: Dashboard, Jobs, Runs, Schedules, Results, Stats, Audit, Settings. A shared bearer token protects the HTTP surface when it is not loopback-only (the default inside the published container). State lives under the data dir (`/data` in the container, a Docker volume), so the coop remembers and the chickens stay ephemeral.

## User Stories

1. As an operator, I want to open the coop GUI in my browser and see daemon health at a glance — up/down, pid, version, uptime — so that I know the platform is alive without running `tc status`.
2. As an operator, I want to see queue depth, the in-flight runs with their resource weights, and the weight budget on the dashboard, so that I can see backlog and load at a glance.
3. As an operator, I want alerts (terminal runs missing a teardown attestation) displayed prominently on the dashboard, so that silent leaks surface immediately instead of accumulating.
4. As an operator, I want to see the active schedule count and the retention block (audit log forever, results TTL in months, last sweep) on the dashboard, so that I know the platform's hygiene state.
5. As an operator, I want to log in to the GUI with a single shared token when it is exposed beyond loopback, so that the container's published port is not wide open.
6. As an operator, I want to trigger a manual run of a job from the GUI by picking a job spec file inside the container, so that I can run a job on demand without a shell.
7. As an operator, I want to upload a new job spec bundle (job TOML plus prompt, schema, attachments) from the browser into the data dir, so that I can create jobs without mounting files into the container.
8. As an operator, I want to validate a job spec from the GUI and see field-named errors (e.g. `trigger.schedule: ...`), so that I can check a spec before scheduling or running it.
9. As an operator, I want to see all registered jobs with their spec hashes, so that I know what the platform has seen and can detect drift by hash.
10. As an operator, I want a runs list in the GUI showing run id, job, status, trigger, and timestamps, filterable by job and status, so that I can see the history of the flock.
11. As an operator, I want a run detail view with the full manifest — spec hash, harness and version, provider, model, temperature, resource budgets and their outcome, token usage, timestamps — so that any run is reproducible and deviations are visible.
12. As an operator, I want to watch a run's streamed logs in the GUI with auto-refresh and auto-scroll, so that I can follow a running chicken live.
13. As an operator, I want the run detail to show the terminal outcome and reason (succeeded, validation failure, timeout, context cap), so that failures are self-explanatory.
14. As an operator, I want to view a succeeded run's validated result JSON in the GUI and copy it to the clipboard, so that I can pass outputs to other jobs manually.
15. As an operator, I want to view a quarantined run's schema errors and raw output side by side, so that I can inspect bad output without it ever being presented as a valid result.
16. As an operator, I want to fetch any result by run id from the GUI, including a clear "expired" state when retention has removed the artifacts, so that I understand what the store still holds.
17. As an operator, I want to add a cron schedule from the GUI by selecting a registered or uploaded cron-trigger job spec, so that recurring jobs are declarative from the browser.
18. As an operator, I want to see all schedules with their cron expression and next run time, so that I know what will fire and when.
19. As an operator, I want to remove a schedule from the GUI, so that I can stop a recurring job without a shell.
20. As an operator, I want to see per-job usage stats in the GUI — n, mean/p50/p95 tokens in/out, total p95, expected cost per run — so that after a handful of runs I know what a run should cost.
21. As an operator, I want to browse the audit log tail in the GUI (most recent events first, filterable by event type), so that I can audit the platform's history without a shell.
22. As an operator, I want to view the effective settings (data dir, socket/db/audit paths, weight budget, retention, coop URL) in the GUI, so that I can verify configuration, especially inside the container.
23. As an operator, I want to trigger a retention sweep from the GUI and see what expired, so that disk hygiene is operable from the browser.
24. As an operator, I want the GUI to keep working when the daemon is down — showing a clear "daemon not reachable" state instead of a broken page — so that I can distinguish "coop up, chickens' supervisor down" from "coop down".
25. As an operator, I want the GUI to poll and refresh automatically while I watch it, so that run states move from queued to succeeded without me reloading.
26. As an operator, I want to run the entire platform as a single Docker container named `coop` with a named volume for the data dir, so that deployment is one command and state survives container replacement.
27. As an operator, I want the coop container to expose one published port with a healthcheck, so that orchestrators and `docker ps` can see it is healthy.
28. As an operator, I want the container to generate and print an ephemeral GUI token at startup when none is configured, so that the published GUI is never unauthenticated.
29. As an operator, I want to run the exact same GUI locally without Docker via a `tc coop start` command, so that I can develop and test against a local daemon.
30. As an operator, I want the GUI to talk to the daemon only through the socket protocol (never touching the database or files directly), so that no view or action can bypass validation, admission, or the append-only guarantees.
31. As an operator, I want all GUI file interactions (spec uploads, result viewing) to go through the data dir, so that a Docker volume carries everything the coop needs.
32. As an operator, I want the coop design to not preclude splitting the web tier and the daemon into separate containers later (shared socket volume or a TCP transport), so that today's single-container choice is not a dead end.
33. As an operator, I want the GUI to surface model discovery and provider/seat-auth state once specs 09/10 land, so that choosing a model in a job spec is against reality, not guesswork.

## Implementation Decisions

- **Naming:** the container image and the daemon instance inside it are called **coop** ("the place where you hold the chickens"); the GUI itself is branded **TurboChickens** (wordmark + egg dot, per `specs/00-design-system.md`). Default port `8737`.
- **Process model:** coop is not a second daemon. When enabled, the existing asyncio daemon process also runs the HTTP server in the same event loop — one process, one queue, one IPC contract. New CLI command `tc coop start` (foreground, like `tc daemon start`); `tc daemon start` with `coop_enabled = true` in settings behaves identically, which is what the container's CMD uses.
- **Web stack:** one new async HTTP dependency (aiohttp — single dependency, native to the existing asyncio loop; no separate server process). The frontend is static files (single `index.html` + `app.js` + `styles.css`, vanilla JS, hash routing, no build step) bundled in the package and served at `/`. Live updates are polling (every ~2 s on the active view) — no WebSocket/SSE in the MVP.
- **Transport seam:** all GUI backend handlers call a `CoopTransport` with the same shape as the existing socket client (`request(op, **payload)`); the production implementation wraps the existing Unix-socket client, talking to the daemon over loopback inside the same process. The web layer never opens the SQLite DB or artifact files directly. This is the seam that keeps containerization options open (story 32): a later TCP or remote transport slots in behind the same interface with zero GUI changes.
- **New read-only socket ops** (small daemon additions so the GUI stays behind the IPC contract): `jobs_list` (registered job specs with hashes), `audit_tail` (last N audit lines, newest first), `settings_get` (effective settings). `status` gains a `coop` block (URL, whether auth is required).
- **HTTP API contract** (JSON, all under `/api`, mirroring socket ops; bearer-token protected unless loopback-only):

  | Method & path | Socket op | Purpose |
  | --- | --- | --- |
  | `GET /api/ping` | `ping` | healthcheck (unauthenticated) |
  | `GET /api/status` | `status` | dashboard |
  | `GET /api/jobs` | `jobs_list` | registered jobs |
  | `POST /api/jobs/validate` | local `load_spec` on an uploaded/pathed spec | validate a spec, field-named errors |
  | `POST /api/runs` | `enqueue` (spec_path, trigger) | manual run |
  | `GET /api/runs?job=&status=` | `runs_list` | runs table |
  | `GET /api/runs/{id}` | `runs_show` | manifest + result row |
  | `GET /api/runs/{id}/logs` | `runs_logs` | streamed events |
  | `GET /api/results/{run_id}` | `results_get` | validated output or quarantine/expired |
  | `GET /api/schedule` | `schedule_list` | schedules + next run |
  | `POST /api/schedule` | `schedule_add` | add cron schedule |
  | `DELETE /api/schedule/{job_id}` | `schedule_rm` | remove schedule |
  | `GET /api/stats` / `GET /api/stats/{job}` | `stats_list` / `stats_show` | per-job usage stats |
  | `GET /api/audit?limit=&event=` | `audit_tail` | audit log tail |
  | `GET /api/settings` | `settings_get` | effective settings |
  | `POST /api/retention/sweep` | `retention_sweep` | on-demand sweep |

- **Spec upload:** `POST /api/jobs/validate` and `POST /api/runs` accept a spec bundle as JSON with base64-encoded files (job TOML + prompt + schema + attachments, size-capped, e.g. 10 MB total). Uploaded bundles are stored under `<data_dir>/uploads/<job>-<spec_hash>/` and enqueued by that in-container path — no host paths ever enter the UI. Validation reuses the existing spec loader, so `extra="forbid"` and field-named errors behave identically to `tc jobs validate`.
- **Auth:** settings gain `coop_enabled` (default false), `coop_host` (default `127.0.0.1`), `coop_port` (default `8737`), `coop_auth_token` (default none). Loopback without a token = no auth (local single operator, same trust domain as the socket). Non-loopback (the container case, `coop_host = "0.0.0.0"`) requires a token: configured one wins; otherwise an ephemeral token is generated at startup and printed once to stdout. The browser sends `Authorization: Bearer <token>`, kept in `sessionStorage`; a 401 shows a login page. No cookies, so no CSRF surface. `/api/ping` stays unauthenticated for the container healthcheck.
- **Settings model:** new fields above, still `extra="forbid"`. Resolution order unchanged.
- **Container:** a `Dockerfile` at the repo root on `python:3.12-slim`, installing the package, shipping a default settings file with `data_dir = "/data"`, `coop_enabled = true`, `coop_host = "0.0.0.0"`, and a CMD that starts the daemon (which serves coop). Image name `coop`. `VOLUME`-documented `/data`; `EXPOSE 8737`; `HEALTHCHECK` hitting `GET /api/ping` on loopback. Usage: `docker run -p 8737:8737 -v coop-data:/data coop`. A compose example ships in the docs.
- **Containerization compatibility (the explicit requirement):** (a) one container runs daemon + GUI today; (b) every stateful path lives under the data dir, so the volume carries socket, DB, audit, results, quarantine, uploads; (c) the GUI reaches the daemon only via the transport seam, so a future split — web container and daemon container sharing the socket through a mounted volume, or a TCP transport — needs no frontend changes; (d) per-run workspace isolation (baseline roadmap item 1, container-isolated chickens) is deliberately independent of coop containerization and is not touched here.
- **Frontend sections:** Dashboard (status, alerts, queue/in-flight, schedules count, retention, coop URL), Jobs (registered jobs table, validate-upload, upload-new-spec), Runs (filterable table; detail pane with tabs: Manifest / Logs / Result-or-Quarantine), Schedules (table with cron + next run, add, remove), Stats (per-job table + detail), Audit (scrollable tail with event filter), Settings (effective settings read-only). All views render from `/api/*` JSON only.
- **Graceful degradation:** when the daemon socket is unreachable, `/api/status` returns a structured `daemon.up = false` payload and the SPA shows a single clear banner; every other view shows an empty state with the reason.

## Testing Decisions

- **Good test:** assert only external, observable behavior — HTTP status codes and JSON bodies, served pages/assets, auth enforcement (401 without token, 200 with), and *effects verified through the existing external surfaces* (e.g. a run enqueued via `POST /api/runs` must appear in `tc runs list` and reach `succeeded` with a stored result). Never web-layer internals, handler call counts, or DOM internals.
- **Seams (proposed):** the single highest seam is the **HTTP API against a real daemon** — boot the daemon (coop enabled) on a temp data dir and temp port in a thread, drive it with an async HTTP client, exactly the way the browser does. Secondary seams, following existing prior art: a subprocess e2e (daemon+coop as a subprocess with the hermetic fake vibe binary via `$TURBOCHICKENS_VIBE_BIN`, full run triggered over HTTP and result fetched over HTTP), and pure-function tests only where new pure logic exists (e.g. upload-bundle validation, token decision logic).
- **Modules under test:** the coop web module (routes, auth, upload handling, status mapping), the three new socket ops (`jobs_list`, `audit_tail`, `settings_get`) through both the socket and the HTTP mirror, and the daemon integration (coop starts and stops with the daemon; `status` carries the coop block; shutdown tears the HTTP server down).
- **Prior art in the codebase:** `test_daemon.py` (thread + subprocess daemon on a temp socket, protocol tests), `test_run_e2e.py` (hermetic fake vibe binary, full lifecycle), `test_api_adapter.py` (loopback HTTP fakes), `test_schedule.py` (socket+CLI parity). The coop tests follow the same fixture pattern: temp data dir, temp port, real daemon, real HTTP.
- **Container verification:** no Docker daemon in the test environment — the image gets a documented manual smoke (`docker build -t coop . && docker run -p 8737:8737 -v coop-data:/data coop`, then `curl /api/ping` and one browser pass). Everything the container runs is already covered by the in-process tests, since the container CMD is just `tc daemon start` with coop enabled.
- **Frontend:** verified at the HTTP level (index and assets served with correct content types, API JSON shapes consumed by the SPA). No browser automation in this environment — one manual browser pass is part of the acceptance checklist.

## Out of Scope

- Multi-user accounts, RBAC, OAuth for the GUI (single shared bearer token is the MVP; spec 09 seat auth concerns provider credentials, not the GUI)
- TLS termination (a reverse proxy's job; documented in the container notes)
- Live log streaming over SSE/WebSocket (polling in the MVP)
- Event/webhook triggers, trajectory-draft review UI, cross-job chaining, notifications/alerting channels
- Remote or multi-node management (one coop manages exactly one daemon)
- Per-run container-isolated workspaces (baseline roadmap item 1 — separate effort, deliberately untouched)
- Editing job specs in place in the GUI (upload a new version; re-adding a schedule replaces it, as today)
- Mobile-specific layouts (basic responsiveness only)
- Specs 09 (seat auth) and 10 (model discovery) themselves — coop only *surfaces* them later (story 33)

## Further Notes

- Invariant preservation is the design's spine: coop is a read/trigger-only client of the socket protocol. Queueing, admission, schema validation, quarantine, teardown attestation, and the append-only stores remain enforced solely by the daemon; the GUI adds no new write path to any of them.
- "The coop remembers; the chickens don't" maps directly onto the container model: the volume-mounted data dir is the coop's memory; runs stay ephemeral.
- No issue tracker is configured in this environment yet — when `/setup-matt-pocock-skills` has been run, publish this spec with the `ready-for-agent` triage label. Until then it lives here as `specs/11-coop-browser-gui-container.md`, following the repo's spec convention.
