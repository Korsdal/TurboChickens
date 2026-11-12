# 19: Auth & access management in the coop GUI

**What to build:** A first-class **Auth / Access** view in the TurboChickens GUI (coop) for managing *how runs authenticate and what they may reach*. Today auth is CLI-only (`tc auth login/status/logout`, spec 09) and invisible in the GUI; access control does not exist yet beyond the per-run env allowlist. Goal: one UI surface where an operator can (a) see every provider's auth mode (`api-key` / `seat`) and seat-session state, (b) start/complete a seat device login and log out, and (c) see the declared access scope for tools, data, MCP servers, and connectors — setting up the access-control groundwork for the Sluse-style future where the host broker decides what a job may touch (baseline v0.2 §7 extension point).

**Blocked by:** 12 (coop skeleton: HTTP + auth shell + SPA + `/api` transport seam over the Unix socket) and 09 (seat broker + `tc auth` CLI)

**Status:** ready-for-agent (backlogged 2026-11-12 — raised with spec 09; the seat broker landed CLI-side, the GUI surface is the natural next step)

- [ ] `/api/auth/providers` lists each provider from providers.toml with its auth mode, and for seat providers the stored session state (expires_at, expired) — **never any token material**; backed by a new socket op (or a thin web-layer call into `SeatBroker.status()`), following the spec-12 transport seam
- [ ] `/api/auth/login` starts the OAuth device flow and relays the verification URI + user code to the browser; `/api/auth/login/status` polls until authorized/expired; on success the session is stored 0600 under `<data_dir>/seats/` exactly as the CLI path (same `SeatBroker`, no duplicated logic)
- [ ] `/api/auth/logout` deletes a provider's stored session
- [ ] Auth-mutating API ops require the coop auth token whenever the server is bound non-loopback (spec 12 rule); loopback stays open
- [ ] GUI: sidebar gains an "Auth" nav entry (design system shell, spec 00): provider table with mode badges, seat-session expiry countdown, login (shows the device code + link) and logout actions; runs view / audit view surface the per-run `auth_mode` from spec 09 manifests
- [ ] Access groundwork (read-only first): an "Access" section listing the declared scope — per-run env allowlist, network policy `none`, and placeholders for tools / data / MCP / connectors access — so a future access-control layer has a UI home; no enforcement beyond what specs 02–09 already enforce
- [ ] Audit: seat login/logout events append audit rows (`auth.login`, `auth.logout` — provider + mode only, never tokens or codes beyond the user-facing device code handling rules of spec 09)
- [ ] Tests: HTTP API against a thread daemon on a temp port with a hermetic loopback fake OAuth server — providers list, device login flow end-to-end (auto-approve), logout, auth-required-when-non-loopback, and an assertion that no response body ever contains token material; page-served smoke like spec 12; no browser automation
