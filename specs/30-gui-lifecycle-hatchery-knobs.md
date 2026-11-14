# 30: GUI — lifecycle controls & hatchery knobs (Stop / Re-run, lineage, upload form, cost vs cap)

**What to build:** The coop GUI surfaces the hatchery lifecycle. `/api/*` mirrors for the new socket ops (`POST /api/runs/{id}/stop`, `POST /api/runs/{id}/rerun`, `/api/connectors/*`) with exact payload parity and the dead-socket `200 {ok:false, daemon.up:false}` behavior on all of them, POSTs included. Runs view: Stop / Re-run buttons per row (queued → cancel; running → stop; any terminal → re-run) and a lineage indicator (parent/children) from `parent_run_id`. Run detail: per-run cost against its cap, so looping chickens are visible as cost outliers even without a cap set. Feed Ledger (stats) shows the same. Jobs upload form gains all per-egg hatchery knobs: `free_range`, `max_cost_usd`, `timeout_sec`, `spawn_subs`, `connectors`. All of it behind the same bearer auth and daemon-down handling as the existing `/api/*` surface (spec 27 of spec 21), built to `specs/00-design-system.md`.

**Blocked by:** 22 (stop/re-run ops), 23 (free_range), 24 (cost cap), 25 (chaining knobs), 27 (spawn_subs + lineage), 28 (connectors CRUD ops)

**Status:** done (2026-11-14)

- [x] `/api/runs/{id}/stop` and `/api/runs/{id}/rerun` mirror `run_stop`/`run_rerun` exactly (payload parity, dead-socket 200 `{ok:false,...}`, bearer 401 when required); `/api/connectors`, `/api/connectors/add`, `/api/connectors/rm` mirror the slice-28 ops
- [x] Runs view: per-row Stop / Re-run with correct enabled states per run status; after stop/cancel the row shows terminal state + reason; re-run enqueues and the new run appears
- [x] Lineage: runs view shows parent/children indicator from `parent_run_id`; run detail shows the parent run link
- [x] Jobs upload form: fields for `free_range`, `max_cost_usd`, `timeout_sec`, `spawn_subs`, `connectors`; invalid combinations (e.g. `free_range=false` without `timeout_sec`) surface as inline field-named errors from the validator
- [x] Run detail + Feed Ledger: measured `usage.cost_usd` shown against `max_cost_usd` when set; `budget.outcome = "cost_cap"` visible on capped runs
- [x] GUI behavior identical to specs 12–19: 2 s polling with flash-on-change, empty states, daemon-down banner, bearer 401 handling, design-system shell; served-markup smoke tests, no browser automation
- [x] Secrecy: no token/seat material in any new payload
- [x] Tests: socket + HTTP parity for every new endpoint, CLI parity unaffected, full suite green; `STATUS.md` updated at the slice boundary
