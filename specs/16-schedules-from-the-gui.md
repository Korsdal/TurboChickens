# 16: Schedules from the GUI

**What to build:** `GET /api/schedule`, `POST /api/schedule` (by registered or uploaded spec path), `DELETE /api/schedule/{job_id}`, with the Schedules view: table (job, cron, live next run), an add form (pick a spec), and a remove action per row. Rejections — manual-trigger spec, invalid cron, spec-hash mismatch — come back as clear inline errors in the UI.

**Design:** `specs/00-design-system.md` — Schedules view spec there (table + add form + inline errors, mono cron, 2 s polling for next_run).

**Blocked by:** 12 (coop skeleton), 13 (Jobs view lists the registered specs to pick from)

**Status:** done (2026-11-12) — 259/259 tests green, e2e smoke verified

- [x] A schedule added via HTTP appears in `tc schedule list` with its next_run; re-adding an edited spec replaces it
- [x] Remove via HTTP removes it; `tc schedule list` shows none
- [x] A schedule added via HTTP fires a `trigger=cron` run through the normal pipeline (next-minute cron e2e, audit shows schedule.fired)
- [x] Manual-trigger or invalid specs are rejected with a clear inline error in the form
- [x] Tests: socket + HTTP parity, CLI parity (`tc schedule add/list/rm`), one real cron firing
