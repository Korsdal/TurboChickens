# 14: Runs from the GUI — trigger, watch, inspect results and quarantine

**What to build:** the full runs path over HTTP — `POST /api/runs` (enqueue by in-container spec path, trigger=manual), `GET /api/runs?job=&status=`, `GET /api/runs/{id}` (full manifest), `GET /api/runs/{id}/logs`, `GET /api/results/{run_id}` (validated output, or quarantine schema errors + raw output, or expired) — with the SPA Runs view: filterable table with status egg-dots (queued hollow, running pulsing amber, succeeded green, failed red, quarantined amber), row click opening a detail pane with tabs Manifest / Logs / Result-or-Quarantine, a copy button on JSON, and 2 s polling while any run is non-terminal.

**Design:** `specs/00-design-system.md` — Runs view + state-dot system + mono data panes exactly as specified there.

**Blocked by:** 12 (coop skeleton)

**Status:** ready-for-agent (2026-11-12)

- [ ] A run enqueued via `POST /api/runs` appears in `tc runs list` with trigger=manual and flows through the normal pipeline to a terminal state
- [ ] Subprocess e2e with the hermetic fake vibe binary: run triggered over HTTP reaches `succeeded`; the results endpoint returns the validated JSON
- [ ] Invalid output → run failed; results endpoint returns quarantined + schema_errors; the UI shows the quarantine tab with raw output and errors, never presented as a valid result
- [ ] Expired artifacts (retention) render a clear "expired" state in the UI instead of the payload
- [ ] Logs tab renders the streamed, sanitized run events; Manifest tab shows spec hash, harness, provider/model/temperature, budget block, usage
- [ ] Job/status filters work; polling moves statuses without a reload; changed rows flash once
- [ ] Tests: thread daemon + HTTP client, subprocess e2e, and CLI parity (`tc runs list/show/logs`, `tc results get` see the same run)
