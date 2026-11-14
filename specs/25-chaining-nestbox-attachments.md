# 25: Chaining — eggs consuming earlier clutches via `nestbox://` attachments

**What to build:** Eggs can attach a previous run's validated clutch as input: `attachments = ["nestbox://<run_id>/output.json"]` in `[input]`. The URI shape is validated at spec load (anything else → field-named error). Resolution happens at provisioning: the Hatchery fetches the referenced run's clutch from the Nest Box — valid clutches only; quarantined, expired, or missing → provisioning failure, the run fails before `running`, with attested teardown as usual. Resolved bytes count toward `max_context_mb` (chaining cannot bypass the context cap), and the manifest records the resolved sources.

**Blocked by:** None — specs 04 (validation results/quarantine) and 07 (run budgets / context cap) are landed. Can start immediately.

**Status:** done (2026-11-14)

- [x] Spec load: `nestbox://<run_id>/output.json` entries in `[input].attachments` validate; malformed URIs (wrong scheme, missing run id, extra path segments) → field-named validation error; `extra = "forbid"` intact
- [x] E2E: run A produces a valid clutch; run B with `attachments = ["nestbox://<A>/output.json"]` provisions with A's clutch bytes injected; manifest records the resolved source (run id + artifact path); run B succeeds
- [x] Fail closed: referencing a quarantined clutch, an expired (composted) clutch, or a nonexistent run id → provisioning failure before `running` (no harness events), attested teardown (1,1,1), no results row, workspace gone
- [x] Context cap: resolved attachment bytes count toward `max_context_mb` — an over-cap chain fails at provisioning with `budget.outcome = "context_cap"` exactly like spec 07
- [x] Tests: pure seam for the resolver (valid / quarantined / expired / missing / wrong-shape); e2e for the happy path and the quarantined refusal (real clutch as attachment via fake vibe binary); full suite green
- [x] `STATUS.md` updated at the slice boundary
