# 29: The Beak — read-only live view into a running chicken

**What to build:** The Beak: a read-only live view into an in-flight chicken — streamed log tail plus workspace peek. New socket op `workspace_peek` returning the run's workspace file tree and single-file content, size-capped and path-locked inside that run's workspace, available only while the workspace exists (in-flight chickens). Mirrored over the transport seam as `GET /api/runs/{id}/workspace`. GUI: the run detail gains a Beak tab (live `runs_logs` tail + tree + file peek). The Beak is honest about ephemerality: after the Abattoir tears a chicken down the workspace is gone, so the Beak shows in-flight chickens only. No shell, no writes — invariant 9 (least-privilege spawn) untouched.

**Blocked by:** 22 (terminal states `cancelled`/`stopped` define when the workspace is gone)

**Status:** ready-for-agent (split from spec 21 via to-tickets, 2026-11-14)

- [ ] Socket op `workspace_peek`: file tree + single-file content for a running run; size-capped content; path-locked inside the run's workspace (traversal attempts refused); unknown run or terminal run → clear not-available response (workspace gone)
- [ ] `GET /api/runs/{id}/workspace` mirrors the socket op exactly (parity rule: identical payload; dead-socket transport returns `200 {ok:false, daemon.up:false}` like every other endpoint)
- [ ] GUI: run detail gains a Beak tab — live log tail (existing `runs_logs` polling), workspace tree, file peek; read-only (no shell, no write affordances); built to `specs/00-design-system.md`, 2 s poll with flash-on-change, empty state for terminal runs, daemon-down banner, bearer 401s like specs 12–19
- [ ] Secrecy: no token/seat material in tree, file contents served, or any response (workspace files are the chicken's own, but env dumps must never be exposed)
- [ ] Tests: socket + HTTP parity, path-lock and size-cap refusals, terminal-run behavior; served-markup smoke for the tab; full suite green
- [ ] `STATUS.md` updated at the slice boundary
