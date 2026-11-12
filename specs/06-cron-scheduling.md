# 06: Cron triggers + `tc schedule add/list/rm`

**What to build:** The internal cron scheduler (APScheduler) inside the daemon. Cron-triggered runs enqueue through the same queue and admission path as manual runs — one source of truth. CLI surface: `tc schedule add/list/rm` to manage a job's cron trigger declaratively.

**Blocked by:** 03 (run pipeline; scheduling only makes sense once manual runs work)

**Status:** ready-for-agent

- [ ] `tc schedule add` registers a cron schedule for a job; `tc schedule list` shows it; `tc schedule rm` removes it
- [ ] A firing cron schedule enqueues a run that appears in `tc runs list` with trigger `cron` and flows through the identical pipeline (workspace, validation, store, attestation)
