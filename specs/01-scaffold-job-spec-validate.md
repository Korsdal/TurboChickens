# 01: Package scaffold + job spec model + `tc jobs validate`

**What to build:** The installable foundation of the platform: a Python 3.12+ package `turbochickens` managed with `uv`/`pyproject.toml` exposing a `tc` CLI entry point, plus the full TOML job spec data model parsed and validated with Pydantic v2 — sections `[job]`, `[trigger]` (manual/cron), `[harness]` (adapter, model, temperature), `[resources]` (timeout_sec, max_context_mb, declared network policy), `[input]` (prompt file, attachments), `[output]` (format, schema file) — with no `max_tokens` knob anywhere. A `tc jobs validate <spec.toml>` command reports OK or precise, field-named validation errors. No daemon yet; this is the layer every other ticket builds on.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] `tc --help` works from an installed environment (`uv`-managed)
- [ ] A valid job spec matching the baseline format validates OK via `tc jobs validate`
- [ ] Invalid specs (bad cron expression, unknown adapter, missing schema reference, a stray `max_tokens` key) fail with clear errors naming the offending field
- [ ] The parsed spec can produce a stable spec hash, needed later for run manifests
