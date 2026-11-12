# 02: Daemon heartbeat + socket + storage + `tc status`

**What to build:** The always-on supervisor daemon as a single asyncio process listening on a Unix domain socket speaking JSON-lines (the one IPC contract the CLI and the future scheduler both use), the SQLite schema for all MVP tables (job specs, runs, run events, results, teardown attestations, job usage stats), the append-only JSONL audit log, and settings loading with defaults (weight-budget override; audit log kept forever, results expiring after 6 months). A `tc status` command round-trips to the daemon and shows daemon health, queue depth, and in-flight runs.

**Blocked by:** 01 (package scaffold, job spec model, `tc` CLI)

**Status:** ready-for-agent

- [ ] Daemon starts, binds the Unix socket, and answers JSON-lines requests
- [ ] `tc status` shows daemon up, empty queue, zero in-flight runs
- [ ] SQLite schema is created with all MVP tables; the audit log appends rows and is append-only
- [ ] Settings load with the documented defaults and allow overrides
