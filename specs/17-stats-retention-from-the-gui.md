# 17: Stats and retention from the GUI

**What to build:** `GET /api/stats`, `GET /api/stats/{job}`, `POST /api/retention/sweep`, with the Stats view: per-job cards showing n, mean/p50/p95 tokens in and out, total p95, and expected cost per run. The Dashboard retention block gains a "Sweep now" action whose outcome (the `result.expired` events) is listed in the UI.

**Design:** `specs/00-design-system.md` — Stats view (cards, stat numerals under the h2 size) and Dashboard retention block + sweep action as specified there.

**Blocked by:** 12 (coop skeleton)

**Status:** ready-for-agent (2026-11-12)

- [ ] Stats endpoints mirror the socket ops; the Stats view renders cards for jobs whose runs were produced over HTTP
- [ ] Sweep via HTTP deletes expired artifacts, audit-logs each as `result.expired`, and the UI lists what expired
- [ ] Runs/Results views show the "expired" state for swept runs (ties into ticket 14's result view)
- [ ] Tests: HTTP against a temp daemon with backdated runs; CLI parity with `tc stats list/show`
