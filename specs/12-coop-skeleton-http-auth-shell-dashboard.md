# 12: Coop skeleton — the daemon serves the TurboChickens GUI (HTTP, auth, shell, Dashboard)

**What to build:** `tc coop start` (and `coop_enabled = true` in settings with `tc daemon start`) runs a small HTTP server inside the daemon's own asyncio event loop, serving the TurboChickens GUI at `/` and a JSON API under `/api`. The API is a thin client of the existing Unix-socket protocol: `/api/ping` (unauthenticated, for the container healthcheck) and `/api/status` (mirrors the `status` socket op) land first. Auth rule: bound to loopback with no token configured → open; bound non-loopback (the container case) → a bearer token is required — the configured `coop_auth_token`, else an ephemeral token generated at startup and printed once to stdout; 401 without `Authorization: Bearer …`, and the SPA shows a login card. The frontend is a dependency-free static SPA (index + app.js + styles.css, no build step) implementing the design-system shell: 240px sidebar on the paper ground with hairline (TurboChickens wordmark + egg dot, nav for Dashboard/Runs/Jobs/Schedules/Stats/Audit/Settings, daemon pill at the foot), and the Dashboard view with the stat row (uptime, queue depth, in-flight vs weight budget, alerts, schedules, retention) plus the flock diagram, polling every 2 s.

**Design:** `specs/00-design-system.md` governs everything visual here — light paper ground `#FCFAF5`, yolk `#C87F0A` action, Geist/Geist Mono, soft radius, solid button pair, sidebar same-ground, Dashboard = stat row + pulsing flock diagram + 2 s polling.

**Blocked by:** none (builds on the landed daemon/socket/settings; adds the `coop` web module, settings fields, `tc coop start`)

**Status:** done (2026-11-12, commit pending at time of writing; 213/213 tests green)

- [x] Settings gains `coop_enabled` (false), `coop_host` (`127.0.0.1`), `coop_port` (`8737`), `coop_auth_token` (none); `extra="forbid"` intact
- [x] `tc coop start` and `coop_enabled=true` + `tc daemon start` both serve `/` and `/api/*`; the `status` op gains a `coop` block (url, auth required); shutdown tears the HTTP server down cleanly
- [x] Transport seam: web handlers call `request(op, **payload)` over the existing socket client (`client.request_async`); the web layer never opens the SQLite DB or artifact files directly
- [x] Auth: loopback + no token → open; non-loopback → 401 without Bearer, 200 with; ephemeral token printed exactly once at startup when none configured; no cookies, token in sessionStorage only
- [x] SPA shell served with correct content types: sidebar, wordmark + egg dot, nav, login view on 401
- [x] Dashboard renders the stat row and flock diagram from `/api/status`, polls every 2 s, changed values flash once; daemon-down renders a clear banner state, not a broken page
- [x] Tests: HTTP API against a thread daemon on a temp port (ping/status/auth/pages served), plus a subprocess e2e; no browser automation (`tests/test_coop.py`, 22 tests)
