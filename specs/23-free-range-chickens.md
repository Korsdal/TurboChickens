# 23: Free-Range chickens — no hard TTL, attested death only by hand or shutdown

**What to build:** Long-lived ("Free-Range") chickens. `resources.free_range = true` removes the hard wall-clock kill: `timeout_sec` becomes optional, and the validator enforces the pairing — `free_range = false` requires `timeout_sec > 0` (as today), `free_range = true` allows it to be absent. A Free-Range chicken dies only via `run_stop` (slice 22) or daemon shutdown, both with the Abattoir's full attested teardown. The baseline decision-log amendment for invariants 1/2 is recorded when this slice lands (timeout moves from "always enforced" to "enforced unless free-range"; new terminal states from slice 22).

**Blocked by:** 22 (reuses the stop path and teardown attestation; shutdown teardown generalizes it)

**Status:** ready-for-agent (split from spec 21 via to-tickets, 2026-11-14)

- [ ] Spec model: `ResourcesSection` gains `free_range: bool = False`; `timeout_sec` becomes optional with the validator rule above (field-named errors; `extra = "forbid"` intact)
- [ ] E2E (fake vibe binary): a Free-Range chicken with no `timeout_sec` is NOT hard-killed at the old TTL — still running and visible after it; a non-free-range chicken with `timeout_sec` still times out as today
- [ ] `run_stop` on a Free-Range chicken → `stopped` with teardown attestation (1,1,1), audit `chicken.stopped`
- [ ] Daemon shutdown with a Free-Range chicken in flight → attested teardown before exit, same attestation + audit as any terminal state; missing attestation remains an alert condition
- [ ] Existing specs without `free_range` validate unchanged (default false, `timeout_sec` still required) — no regression for specs 01–21 fixtures
- [ ] Decision-log amendment for baseline invariants 1/2 recorded (free-range timeout semantics, `cancelled`/`stopped` states) — in this slice's spec file and `STATUS.md`
- [ ] Tests: pure seam for the new validator rules; e2e for no-kill-at-TTL, attested stop, shutdown teardown; full suite green
- [ ] `STATUS.md` updated at the slice boundary
