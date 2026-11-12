# 08: Usage stats + expected cost + retention TTLs

**What to build:** Cost as telemetry, not a knob: each run's token usage (in/out) is recorded in its manifest and rolled up into per-job usage statistics — count, mean/p50/p95 tokens, and expected cost per run computed from `providers.toml` pricing — surfaced via the CLI. Plus settings-managed retention: the audit log is kept forever by default, results expire after 6 months by default, and every deletion is itself audit-logged.

**Blocked by:** 04 (results + store), 05 (`providers.toml` pricing and API usage reporting)

**Status:** done (2026-11-12, 145/145 tests green)

- [x] After several runs of a job, per-job stats show n, mean/p50/p95 tokens in+out and an expected cost per run derived from provider pricing
- [x] Each run's manifest carries its token usage; nothing in the job spec caps tokens
- [x] Retention: expired results are deleted and each deletion is audit-logged; the audit log itself is never TTL-deleted
