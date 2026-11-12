# STATUS.md — TurboChickens (internal anchor for agentic work)

> Single source of truth for "where are we and what must not be broken".
> Update this file at every spec boundary. Future agents: read this first,
> then the governing docs in §2, before touching code.

**Last updated:** 2026-11-12, after spec 02 landed (60/60 tests green, e2e smoke verified).

---

## 1. What this project is

TurboChickens — a local-first management platform for **ephemeral, just-in-time AI
agent jobs**. One always-on supervisor daemon + a `tc` CLI. Agents are jobs, not
co-workers: spawned deterministically, given a fresh workspace, expected to produce
schema-validated output, and verifiably destroyed afterwards. The platform is the
durable thing; the agents are not.

## 2. Governing docs (read in this order)

| Doc | Role |
| --- | --- |
| `specs/archive/turbochickens-baseline-spec-v0-2.md` | **Governing baseline.** Its decision log (§14) wins any conflict. |
| `specs/archive/turbochickens-mvp-spec.md` | Operational MVP spec derived from the baseline (user stories, decisions, test philosophy). |
| `specs/01-*.md` … `specs/08-*.md` | Implementation tickets. Each has `Status: ready-for-agent`, a "Blocked by" line, and a checkbox acceptance list. |

## 3. Current state — verified 2026-11-12

**Spec 01 and spec 02 are done** (commits `f8b8061` and the spec-02 commit).
Verified this session:

```
.venv/bin/python -m pytest -q          → 60 passed
.venv/bin/tc --help                     → works, shows `jobs`, `daemon`, `status`
.venv/bin/tc jobs validate examples/weekly-incidents-digest/job.toml
                                        → OK … sha256:18efbd8c…80b959
# e2e smoke: tc daemon start (background) → tc status → tc daemon stop
                                        → up / queue_depth 0 / in_flight 0, rc 0; down → rc 1
```

What exists:

```
src/turbochickens/
├── __main__.py            # python -m turbochickens → CLI
├── util.py                # utcnow / utcnow_iso (UTC, ms precision, Z suffix)
├── settings.py            # Settings (Pydantic, extra=forbid) + load_settings(); defaults below
├── storage.py             # init_db(): full MVP SQLite schema + append-only triggers + schema_meta
├── audit.py               # AuditLog: append-only JSONL (open "a" only, fsync per row)
├── daemon.py              # Daemon: asyncio process, Unix socket, JSON-lines ops ping/status/shutdown
├── client.py              # request(socket, op, **payload) → parsed response; DaemonUnreachable
├── spec/
│   ├── model.py           # Pydantic v2 model, all 6 sections, extra="forbid"
│   └── loader.py          # load_spec(), SpecValidationError (field-named issues), spec_hash()
└── cli/main.py            # Typer app `tc`: jobs validate | daemon start/stop | status
tests/
├── test_spec_model.py     # pure-function seam: parser/model/hash
├── test_cli_validate.py   # CLI seam: tc jobs validate behavior
├── test_settings.py       # defaults, overrides, env var, error cases, weight-budget formula
├── test_storage.py        # schema tables, append-only triggers, audit log ordering/append-only
└── test_daemon.py         # protocol + CLI tests on a thread daemon + subprocess e2e
examples/weekly-incidents-digest/   # baseline-format example job (job.toml, prompt, schema, csv)
pyproject.toml             # uv/hatchling, Python >=3.12, version 0.2.0, deps: pydantic, typer, rich, croniter; dev: pytest
```

Spec-02 deliverables, concretely:

- **Daemon** (`daemon.py`, `run_daemon`): single asyncio process; binds
  `<data_dir>/daemon.sock` (chmod 0600, stale socket reaped, double-start
  refused); JSON-lines protocol (`protocol: 1`), one JSON request object per
  line → one response per line; ops `ping`, `status`, `shutdown`; malformed
  lines get an error response without killing the connection; graceful stop on
  `shutdown`/SIGINT/SIGTERM, socket unlinked, `daemon.started`/`daemon.stopped`
  audit rows written. Holds the queue (`asyncio.Queue`) and in-flight map that
  spec 03 fills; `status` reports them.
- **IPC client** (`client.py`): `request()` used by all CLI commands;
  `DaemonUnreachable` when the socket doesn't answer.
- **Storage** (`storage.py`): all MVP tables from baseline §12 — `job_specs`,
  `runs`, `run_events`, `results`, `teardown_attestations`, `job_usage_stats` —
  plus `schema_meta` (schema_version=1). Append-only enforced by SQLite triggers
  on `job_specs`, `run_events`, `results`, `teardown_attestations` (UPDATE/DELETE
  abort); `runs` and `job_usage_stats` stay mutable. No trajectory tables (post-MVP).
- **Audit log** (`audit.py`): `<data_dir>/audit.jsonl`, append-only by
  construction (only ever opened `"a"`, fsync per row), one JSON object per
  line with UTC `ts`.
- **Settings** (`settings.py`): resolution order = `--settings` path >
  `$TURBOCHICKENS_SETTINGS` > `~/.turbochickens/settings.toml` (if exists) >
  defaults. Fields: `data_dir` (default `~/.turbochickens`), derived
  `socket_path`/`db_path`/`audit_log_path`, `weight_budget_override`,
  `audit_retention = "forever"`, `results_retention_months = 6`; unknown keys
  rejected. `weight_budget` = override or `max(1, min(vCPU, floor(RAM_GB/1.5)))`.
- **CLI**: `tc daemon start` (foreground; `python -m turbochickens daemon start`
  works too), `tc daemon stop`, `tc status` — prints
  `daemon: up (pid=…, version=…, uptime=…s)`, `socket/db/audit_log` paths,
  `weight_budget`, `queue_depth: N`, `in_flight: N` (+ table when non-empty);
  exit 1 with `no daemon at …` when down. All socket commands take `--settings`
  (envvar `TURBOCHICKENS_SETTINGS`).

Spec-01 deliverables, concretely:

- Installable package `turbochickens` 0.1.0 with `tc` entry point (Typer + Rich).
- TOML job spec model with `[job]`, `[trigger]` (manual/cron, 5-field cron via
  croniter), `[harness]` (adapter `mistral-api`|`vibe-cli`, model, temperature 0–2),
  `[resources]` (timeout_sec, max_context_mb, network `none`), `[input]`
  (prompt_file, attachments), `[output]` (format `json`, schema_file).
- `extra="forbid"` on every model — a stray key (incl. `max_tokens`) anywhere fails
  with a dotted field path (e.g. `harness.max_tokens`).
- `tc jobs validate` prints `OK: job '<name>' — spec hash sha256:<hex>` or
  `error <dotted.path>: <msg>` lines on stderr, exit 1.
- `spec_hash()` = sha256 over canonical JSON of the validated model — stable across
  formatting/key order, sensitive to content. **This hash is the anchor for run
  manifests in later specs.**
- Schema file references resolve relative to the spec file; a missing referenced
  schema is a field-named error (`output.schema_file`).

## 4. Spec pipeline

| Spec | Title | Status | Blocked by |
| --- | --- | --- | --- |
| 01 | Scaffold + job spec model + `tc jobs validate` | **done** (`f8b8061`) | — |
| 02 | Daemon + Unix socket (JSON-lines) + SQLite schema + audit log + settings + `tc status` | **done** (2026-11-12) | 01 |
| 03 | Manual run e2e via `vibe-cli` adapter (queue, admission, workspace, manifest, attestation, `tc run`/`tc runs`) | **next — unblocked** | 02 |
| 04 | Output validation + result store + quarantine + `tc results get` | ready-for-agent | 03 |
| 05 | `mistral-api` adapter + `providers.toml` + backoff on 429/5xx | ready-for-agent | 03, 04 |
| 06 | Cron scheduling (APScheduler) + `tc schedule add/list/rm` | ready-for-agent | 03 |
| 07 | Run budgets: wall-clock hard kill + injected-context cap | ready-for-agent | 03 |
| 08 | Usage stats + expected cost + retention TTLs | ready-for-agent | 04, 05 |

## 5. Non-negotiable invariants (baseline v0.2 — do not regress these)

1. **No `max_tokens` knob anywhere.** Token usage is measured telemetry, never a
   declared budget. Enforced budgets are exactly two: wall-clock `timeout_sec` (hard
   kill) and injected-context `max_context_mb`.
2. **Run lifecycle:** queued → provisioning → running → collecting → finalizing →
   succeeded; collecting → failed (validation) and running → failed (error/budget/
   timeout). Every terminal state requires a **teardown attestation** (process reaped,
   workspace gone, env cleared, audit row); a missing attestation is an alert condition.
3. **One queue, one IPC contract:** CLI and cron both submit over a **Unix domain
   socket speaking JSON-lines** to the single asyncio daemon. Manual and cron runs
   are indistinguishable downstream.
4. **Admission control by resource weight:** API runs weigh **0.25**, CLI-agent runs
   **1.0**; budget = `max(1, min(vCPU, floor(RAM_GB / 1.5)))` with a settings override.
   Over-budget runs **backlog, never reject**.
5. **Outputs are untrusted data:** raw harness output must pass the job's JSON schema
   before entering the result store. Invalid → **quarantine** (kept, schema errors
   recorded, run failed) — never stored as valid, never discarded.
6. **Append-only:** result store (SQLite index + on-disk artifacts, keyed by run ID)
   and the JSONL audit log. Audit log kept **forever** by default; results expire
   after **6 months** by default; every deletion is audit-logged.
7. **Stack pins:** Python 3.12+, `uv` + `pyproject.toml`, asyncio single-process
   daemon, APScheduler, Typer + Rich, Pydantic v2, `jsonschema`, stdlib `sqlite3`,
   `httpx`, `asyncio.subprocess`. Linux-only MVP.
8. **Sluse-independence:** job spec format and adapter interface carry no Sluse
   concepts; keep the **credential-provider extension point** on the adapter
   (`prepare`/`execute`/`teardown` Protocol) so a host-side broker can slot in later.
9. **Security posture:** least-privilege spawn (fresh workspace, per-run env
   credentials, no ambient secrets); streamed logs sanitized of control characters,
   stored read-only — logs are data, never instructions.
10. **Trajectory loops are post-MVP** (baseline §11) — do not build the tables, but
    don't preclude them.

SQLite MVP tables (baseline §12, to be created in spec 02): `job_specs`, `runs`,
`run_events`, `results`, `teardown_attestations`, `job_usage_stats` (trajectory
tables deliberately excluded).

## 6. Working conventions for agents

- **Environment:** `uv`-managed venv at `.venv/` (already built). Run tests with
  `.venv/bin/python -m pytest -q` (or `uv run pytest`). CLI: `.venv/bin/tc …`.
- **Tests assert only external, observable behavior** — CLI output, socket protocol
  responses, store/audit contents, status transitions. Never internal state or mock
  call counts (MVP spec, Testing Decisions). Two seams: pure-function (spec parser,
  later the JSON-schema validator) and end-to-end through `tc` against a real daemon
  on a temp socket (fixture pattern to be established in spec 02/03).
- **Spec files:** each ticket's checkbox list is the acceptance criterion — tick only
  when verified by execution. Keep `Status:` line current (`ready-for-agent` →
  `in-progress`/`done`).
- **Commits:** follow the existing convention — imperative subject, body describing
  what/why, trailer `Generated by Mistral Vibe.` + `Co-Authored-By: Mistral Vibe
  <vibe@mistral.ai>`.
- **Adding dependencies:** edit `pyproject.toml`, then `uv lock` / `uv sync`.

## 7. Next up — spec 03 (`specs/03-manual-run-e2e-vibe-cli.md`)

First complete vertical path: `tc run <job>` submits over the socket into the
daemon queue with weight admission (0.25 API / 1.0 CLI against
`settings.weight_budget`; over-budget backlogs, never rejects); provisioner makes
a fresh per-run workspace + per-run env; the adapter interface
(`prepare`/`execute`/`teardown`, credential-provider extension point reserved)
is implemented by `vibe-cli` (headless subprocess pinned to the workspace,
sanitized streamed logs); lifecycle queued→…→succeeded/failed; manifest with
spec hash + pinned env + usage; teardown attestation + audit rows on every
terminal state; CLI `tc run`, `tc runs list/show/logs`. Builds directly on what
spec 02 left: the socket protocol (add an `enqueue` op), the DB schema, the
audit log, `spec_hash` from spec 01, and the `Daemon.queue`/`in_flight` hooks.
