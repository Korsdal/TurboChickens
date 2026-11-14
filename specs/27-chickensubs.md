# 27: ChickenSubs — a chicken spawning sub-chickens through the Farmhand

**What to build:** Per-chicken sub-spawning. `HarnessSection` gains `spawn_subs: bool = False`. A chicken with `spawn_subs = true` gets the Farmhand attached to its harness runtime over stdio (for vibe-cli: an MCP server entry in the run's vibe config spawning `tc farmhand serve --transport stdio`), so the chicken can enqueue sub-eggs and read the flock through the farm's own contract. Sub-chickens are ordinary eggs enqueued through the same socket/queue/admission weights — no bypass. The daemon records `parent_run_id` on the run (new `runs.parent_run_id` column, schema bump), in the manifest, and in the audit. Depth is capped at 1: enqueue refuses a spec with `spawn_subs = true` when the enqueuing run is itself a sub (field-named error). Sub tool scope is narrower: when the caller is a sub-chicken, the Farmhand exposes enqueue + read tools only, and `run_stop` refuses targets that are its ancestors.

**Blocked by:** 22 (stop op + lineage groundwork), 26 (the Farmhand server and its stdio transport)

**Status:** done (2026-11-14)

- [x] Spec model: `HarnessSection` gains `spawn_subs: bool = False` (`extra = "forbid"` intact)
- [x] E2E (fake vibe binary that itself calls the attached Farmhand over stdio): a `spawn_subs = true` chicken enqueues a sub-egg; the sub runs as an ordinary egg through the same queue/admission (same weights), with `parent_run_id` set in the runs store, manifest, and audit
- [x] Depth cap 1: a sub-chicken enqueueing a spec with `spawn_subs = true` is refused with a field-named error; no sub-sub-chickens exist
- [x] Sub scope: from a sub-chicken's Farmhand, only enqueue + read tools are exposed; `run_stop` against an ancestor run is refused; schedule/auth/settings tools are not available
- [x] Schema: `runs.parent_run_id` column added with a schema bump; existing rows read as NULL; migrations keep the suite green on old data dirs
- [x] Secrecy: manifest/audit/MCP results for subs carry no token/seat material
- [x] Tests: e2e for lineage recorded, depth-1 refusal, sub-scope refusal of ancestor-stop; pure seam for the spec field; full suite green
- [x] Lexicon: ChickenSub term confirmed/added in `docs/lexicon.md`; `STATUS.md` updated at the slice boundary
