# 24: Feed Budget cost cap — `max_cost_usd`, off by default, enforced at finalize

**What to build:** An optional per-egg cost cap. `resources.max_cost_usd: float | None = None` (USD — never a token count; the "no `max_tokens` knob" invariant is intact). Enforcement is finalize-only: if the run's measured `usage.cost_usd > max_cost_usd`, the run fails with `budget.outcome = "cost_cap"` in the same budget block as timeout/context-cap. Cost and token telemetry are recorded on every run regardless of whether a cap is set — absent field means no cap (off by default). No mid-run kill in this slice.

**Blocked by:** None — specs 05 (cost telemetry from providers) and 08 (usage stats) are landed. Can start immediately.

**Status:** done (2026-11-14)

- [x] Spec model: `ResourcesSection` gains `max_cost_usd: float | None = None` (`extra = "forbid"` intact; negative/zero values rejected with a field-named error)
- [x] Over cap (e2e, fake provider with priced usage): run fails at finalize with `budget.outcome = "cost_cap"`, manifest carries measured `usage.cost_usd` and the cap; teardown attested as usual
- [x] At cap exactly (`cost_usd == max_cost_usd`) → not over → run succeeds (cap is exclusive only when exceeded)
- [x] No cap set → behavior unchanged, but `usage.cost_usd` / token telemetry still recorded on the run and visible to stats (spec 08)
- [x] Seat runs (`auth = "seat"`, `cost_usd = 0.0`) never trip the cap
- [x] Tests: pure seam for the finalize cost-cap check (over / at / no cap / seat-with-cost-0); e2e via fake vibe binary + fake priced provider; full suite green
- [x] `STATUS.md` updated at the slice boundary
