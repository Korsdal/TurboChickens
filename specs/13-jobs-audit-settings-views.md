# 13: Jobs, Audit and Settings views (read-only socket ops + GUI mirrors)

**What to build:** three new read-only socket ops so the GUI stays behind the one IPC contract — `jobs_list` (registered job specs from the `job_specs` table, with spec hashes), `audit_tail` (last N lines of the audit log, newest first, optional event-type filter), `settings_get` (effective settings) — mirrored at `GET /api/jobs`, `GET /api/audit?limit=&event=`, `GET /api/settings`, with the SPA views: Jobs table (name, spec hash, registered-at), Audit mono log tail with event-type filter chips, Settings read-only rows. All rendered per the design system.

**Design:** `specs/00-design-system.md` — Jobs/Audit/Settings view specs there (mono for hashes and audit lines, muted labels, surface panels, 2 s polling on Audit).

**Blocked by:** 12 (coop skeleton: HTTP server, auth, shell)

**Status:** done (2026-11-12, 226/226 tests green)

- [x] The three socket ops work standalone over the Unix socket (jobs_list / audit_tail / settings_get)
- [x] HTTP mirrors return identical payloads under the same auth rules as ticket 12
- [x] Jobs view lists registered specs with hashes; Audit view is mono, newest first, filterable by event type; Settings view shows effective settings rows
- [x] All three views render a clean daemon-down / empty state
- [x] Tests: socket + HTTP parity on a temp daemon; empty-state and filter behaviour over HTTP
