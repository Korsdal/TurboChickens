# 04: Output validation + result store + quarantine + `tc results get`

**What to build:** The validated-results channel: raw harness output is treated as untrusted until it passes the job's JSON schema (`jsonschema`). Valid output is stored in the result store — SQLite row plus on-disk artifacts directory, keyed by run ID, append-only — and the run finalizes as succeeded. Invalid output is quarantined: kept for inspection with its schema errors recorded, the run marked failed, and the teardown still attested. A `tc results get <run>` command fetches validated results.

**Blocked by:** 03 (manual run pipeline end-to-end)

**Status:** done (2026-11-12)

- [x] A run whose output matches the job schema stores the result and `tc results get` returns it
- [x] A run whose output violates the schema is marked failed, lands in quarantine with schema errors recorded, and is never stored as valid
- [x] Quarantined output is never returned by `tc results get` as a valid result
