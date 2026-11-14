# 22: Lifecycle ops — stop & re-run a chicken from CLI, socket, and audit

**What to build:** The operator can end a chicken deliberately and retry it in one step, on every surface. New socket ops `run_stop` (queued egg → terminal `cancelled`; running chicken → SIGTERM, then hard kill, with the Abattoir's attested teardown, terminal state `stopped`) and `run_rerun` (enqueue the same egg as a fresh run, lineage recorded), CLI parity (`tc runs stop <id>`, `tc runs rerun <id>`), new terminal states `cancelled`/`stopped` visible in the runs list and Headcount (audit events `egg.cancelled`, `chicken.stopped`), and the same teardown attestation on every stop path as any terminal state — a missing attestation stays an alert condition. Per spec 20's rule, the new lexicon terms and audit event names land in `docs/lexicon.md` first.

**Blocked by:** None — specs 02–04 (daemon/socket/storage, e2e runs, validation & quarantine) are landed. Can start immediately.

**Status:** done (2026-11-14)

- [x] `run_stop` on a queued run → terminal state `cancelled`, audit `egg.cancelled`, never hatches (no harness events)
- [x] `run_stop` on a running chicken (e2e, fake vibe binary via `$TURBOCHICKENS_VIBE_BIN`) → SIGTERM then hard kill, terminal state `stopped`, audit `chicken.stopped`, teardown attestation (1,1,1) exactly like any terminal state
- [x] `run_rerun` enqueues the same spec as a fresh run (new run_id, lineage recorded in manifest/audit) and the new run goes through the normal queue/admission/pipeline
- [x] Stopped/cancelled runs show terminal state + reason in `tc runs list` / `runs_show` and in the audit tail
- [x] CLI parity: `tc runs stop <id>`, `tc runs rerun <id>` talk to the same socket ops (rc 0 / clear error for unknown or already-terminal runs)
- [x] Audit: `egg.cancelled` / `chicken.stopped` rows per lexicon, no token/seat material anywhere (spec 09/19 secrecy rule)
- [x] Lexicon: new terms + audit event names added to `docs/lexicon.md` before use
- [x] Tests: external behavior only — socket/CLI responses, store + audit contents, status transitions, attestation; e2e through thread + subprocess daemon with the fake vibe binary for stop/cancel/re-run
- [x] Full suite green; `STATUS.md` updated at the slice boundary
