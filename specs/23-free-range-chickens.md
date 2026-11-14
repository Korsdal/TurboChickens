# 23: Free-Range chickens — no hard TTL, attested death only by hand or shutdown

**What to build:** Long-lived ("Free-Range") chickens. `resources.free_range = true` removes the hard wall-clock kill: `timeout_sec` becomes optional, and the validator enforces the pairing — `free_range = false` requires `timeout_sec > 0` (as today), `free_range = true` allows it to be absent. A Free-Range chicken dies only via `run_stop` (slice 22) or daemon shutdown, both with the Abattoir's full attested teardown. The baseline decision-log amendment for invariants 1/2 is recorded when this slice lands (timeout moves from "always enforced" to "enforced unless free-range"; new terminal states from slice 22).

**Blocked by:** 22 (reuses the stop path and teardown attestation; shutdown teardown generalizes it)

**Status:** done (2026-11-14)

- [x] Spec model: `ResourcesSection` gains `free_range: bool = False`; `timeout_sec` becomes optional with the validator rule above (field-named errors; `extra = "forbid"` intact)
- [x] E2E (fake vibe binary): a Free-Range chicken with no `timeout_sec` is NOT hard-killed at the old TTL — still running and visible after it; a non-free-range chicken with `timeout_sec` still times out as today
- [x] `run_stop` on a Free-Range chicken → `stopped` with teardown attestation (1,1,1), audit `chicken.stopped`
- [x] Daemon shutdown with a Free-Range chicken in flight → attested teardown before exit, same attestation + audit as any terminal state; missing attestation remains an alert condition
- [x] Existing specs without `free_range` validate unchanged (default false, `timeout_sec` still required) — no regression for specs 01–21 fixtures
- [x] Decision-log amendment for baseline invariants 1/2 recorded (free-range timeout semantics, `cancelled`/`stopped` states) — in this slice's spec file and `STATUS.md`
- [x] Tests: pure seam for the new validator rules; e2e for no-kill-at-TTL, attested stop, shutdown teardown; full suite green
- [x] `STATUS.md` updated at the slice boundary

## Decision-log amendment (baseline v0.2 §14, invariants 1/2 — recorded when this slice landed, 2026-11-14)

- **Invariant 1 (budgets):** the wall-clock `timeout_sec` hard kill moves from
  "always enforced" to **"enforced unless free-range"** — an egg with
  `resources.free_range = true` carries no hard TTL (`timeout_sec` optional,
  and never enforced as a kill when free-range; a Free-Range chicken dies only
  via `run_stop` or daemon shutdown, both with the Abattoir's attested
  teardown). `free_range = false` (the default) keeps the pre-23 behavior:
  `timeout_sec > 0` required and hard-killed. The injected-context cap
  `max_context_mb` is unchanged, and there is still **no `max_tokens`** knob.
- **Invariant 2 (lifecycle):** terminal states now include `cancelled` (spec 22,
  a queued egg pulled from the pecking order — never hatches, still attested)
  and `stopped` (spec 22/23, a running chicken ended by the operator or by
  daemon shutdown — SIGTERM then hard kill, attested). Every terminal state,
  including these, requires the teardown attestation; a missing one remains an
  alert condition.

## Implementation notes (what landed)

- `ResourcesSection` (spec/model.py): `free_range: bool = False` declared before
  `timeout_sec: int | None` (default None, `gt=0`, `validate_default=True`); a
  field validator on `timeout_sec` raises the field-named pairing error when
  `free_range` is false and the key is absent. `extra = "forbid"` intact.
- `spec_hash` (spec/loader.py) drops `free_range` when false, so pre-23 egg
  hashes are unchanged (verified: `examples/weekly-incidents-digest` still
  `sha256:18efbd8c…80b959`).
- Adapters (adapter.py): `execute(..., timeout_sec: int | None)` — `None` means
  no hard kill (`asyncio.wait_for(..., timeout=None)` waits forever; the API
  adapter keeps only its 10 s connect timeout).
- Hatchery (runner.py): `hard_timeout = None if free_range else timeout_sec`;
  the manifest `resources` mirror and `budget` block gain `free_range`; active
  Free-Range chickens are tracked and `request_stop_free_range()` stops them
  through the spec-22 stop path.
- Daemon (daemon.py): on shutdown, `pipeline.request_stop_free_range()` fires
  before the bounded drain, so an in-flight Free-Range chicken ends `stopped`
  with attestation (1,1,1) + `chicken.stopped`/`abattoir.attested` audit rows
  before exit.
- Tests: `tests/test_free_range.py` (13) — pure pairing seam, hash stability,
  CLI validate, no-kill-at-TTL e2e with fenced control, stop e2e (socket +
  CLI), shutdown teardown e2e (thread + subprocess daemon). Full suite:
  353 passed.
