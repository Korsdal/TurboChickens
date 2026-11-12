# STATUS.md — TurboChickens (internal anchor for agentic work)

> Single source of truth for "where are we and what must not be broken".
> Update this file at every spec boundary. Future agents: read this first,
> then the governing docs in §2, before touching code.

**Last updated:** 2026-11-12, after spec 04 landed (89/89 tests green, e2e smoke verified).

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

**Spec 01, spec 02, spec 03, and spec 04 are done** (commits `f8b8061`, the spec-02 commit, the spec-03 commit, and the spec-04 commit).
Verified this session:

```
.venv/bin/python -m pytest -q          → 89 passed
.venv/bin/tc --help                     → works, shows `run`, `runs`, `jobs`, `daemon`, `results`, `status`
.venv/bin/tc jobs validate examples/weekly-incidents-digest/job.toml
                                        → OK … sha256:18efbd8c…80b959
# e2e smoke (fake vibe binary via $TURBOCHICKENS_VIBE_BIN): tc daemon start (background)
#   → tc run <valid-output job> --wait → succeeded, rc 0
#   → tc results get <run> → validated JSON on stdout, rc 0; results row valid=1 + data/results/<run>/output.json
#   → tc run <invalid-output job> --wait → failed "output failed schema validation", rc 1
#   → tc results get <run> → rc 1 "quarantined" + schema errors; results row valid=0 + data/quarantine/<run>/{raw_output.txt,schema_errors.json}
#   → tc runs list/show/logs → manifest + validator events + attestation; tc status → alerts: 0; tc daemon stop → rc 0
```

What exists:

```
src/turbochickens/
├── __main__.py            # python -m turbochickens → CLI
├── util.py                # utcnow / utcnow_iso (UTC, ms precision, Z suffix)
├── settings.py            # Settings (Pydantic, extra=forbid) + load_settings(); defaults below
├── storage.py             # init_db(): full MVP SQLite schema + append-only triggers + schema_meta
├── audit.py               # AuditLog: append-only JSONL (open "a" only, fsync per row)
├── adapter.py             # HarnessAdapter Protocol (prepare/execute/teardown) + CredentialProvider
│                          # extension point + VibeCLIAdapter (headless subprocess pinned to the
│                          # workspace, sanitized streamed logs) + adapter weights (API 0.25 / CLI 1.0)
├── runner.py              # RunPipeline: lifecycle queued→…→succeeded/failed, fresh workspace +
│                          # per-run env, manifest, run_events, spec-04 result store / quarantine
│                          # at `collecting`, teardown attestation + audit rows
├── daemon.py              # Daemon: asyncio process, Unix socket, JSON-lines ops
│                          # ping/status/shutdown/enqueue/runs_list/runs_show/runs_logs/results_get;
│                          # admission-control scheduler (over-budget backlogs, never rejects)
├── client.py              # request(socket, op, **payload) → parsed response; DaemonUnreachable
├── validate.py            # spec 04 pure seam: validate_output(raw, schema_path) → OutputValidation
│                          # (jsonschema Draft 2020-12 + format checker; never raises on bad output)
├── spec/
│   ├── model.py           # Pydantic v2 model, all 6 sections, extra="forbid"
│   └── loader.py          # load_spec(), SpecValidationError (field-named issues), spec_hash()
└── cli/main.py            # Typer app `tc`: run [--wait] | runs list/show/logs | results get |
                           # jobs validate | daemon start/stop | status
tests/
├── test_spec_model.py     # pure-function seam: parser/model/hash
├── test_cli_validate.py   # CLI seam: tc jobs validate behavior
├── test_settings.py       # defaults, overrides, env var, error cases, weight-budget formula
├── test_storage.py        # schema tables, append-only triggers, audit log ordering/append-only
├── test_daemon.py         # protocol + CLI tests on a thread daemon + subprocess e2e
├── test_run_e2e.py        # spec 03 e2e: thread + subprocess daemons, hermetic fake vibe binary
│                          # ($TURBOCHICKENS_VIBE_BIN), full lifecycle/attestation/admission tests
├── test_validate.py       # spec 04 pure seam: validate_output on valid/invalid/non-JSON outputs,
│                          # nested error paths, format enforcement, fail-closed broken schemas
└── test_results.py        # spec 04 e2e: valid → results row + artifacts + tc results get; invalid →
                           # failed + quarantine + schema errors, never returned as valid; subprocess e2e
examples/weekly-incidents-digest/   # baseline-format example job (job.toml, prompt, schema, csv)
examples/hello-vibe-cli/            # manual vibe-cli example job (job.toml, prompt, schema)
pyproject.toml             # uv/hatchling, Python >=3.12, version 0.4.0, deps: pydantic, typer, rich, croniter,
                           # jsonschema (+ rfc3339-validator for format checks); dev: pytest
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

Spec-03 deliverables, concretely:

- **Run submission:** `tc run <spec> [--wait]` validates the spec locally, then sends
  `enqueue` (spec_path + spec_hash + trigger) over the socket. The daemon re-validates,
  registers the job (`job_specs`, INSERT OR IGNORE), inserts the `runs` row (`queued`),
  audit-logs `run.queued`, and returns `{run_id, status: queued, weight, queue_depth}`.
  `--wait` polls `runs_show` until terminal and exits 0/1 on succeeded/failed.
- **Admission control:** weights API 0.25 / CLI 1.0 against `settings.weight_budget`; the
  scheduler admits FIFO from `pending` only while `weight <= budget - in_flight_sum`.
  Over-budget runs backlog (visible in `status.queue_depth`), never rejected.
- **Adapter layer** (`adapter.py`): `HarnessAdapter` Protocol (`prepare`/`execute`/
  `teardown`) over a `RunContext`; `CredentialProvider` extension point reserved, MVP
  implementation injects only allowlisted env names (`MISTRAL_API_KEY`) as per-run env
  vars — the run env is built fresh from a base-key whitelist, never copied from
  `os.environ`. `VibeCLIAdapter` runs `vibe --prompt-file … --output text
  --output-dir … --model … --workdir <workspace>` headless with cwd pinned to the
  workspace, streams stdout/stderr as sanitized `run_events` (control chars stripped,
  length-capped), hard-kills on `timeout_sec`, and reads token usage from the
  harness `export.json` (telemetry only). Binary resolved via
  `$TURBOCHICKENS_VIBE_BIN` else PATH. `mistral-api` is rejected at enqueue until
  spec 05.
- **Pipeline** (`runner.py`): provisioning creates `<data_dir>/workspaces/<run_id>` and
  copies prompt + attachments in; lifecycle transitions are written to `runs.status`
  and mirrored as `run_events`; the manifest (spec hash, harness+version/provider/model/
  temperature, resources, usage, timestamps, workspace, `env_keys` — never secret
  values, `timed_out`, status reason) lands in `runs.manifest_json`/`usage_json`.
  Every terminal state — success, harness error, timeout, internal error — gets
  `adapter.teardown()` (process reaped, workspace rmtree'd and verified gone, env
  cleared) → `teardown_attestations` row + `teardown.attested` and `run.finished`
  audit rows.
- **Inspection:** socket ops `runs_list`/`runs_show`/`runs_logs` + CLI `tc runs
  list/show/logs`; `status` gains `alerts` (terminal runs missing an attestation or
  with a failed attestation flag) and `tc status` prints them.

Spec-04 deliverables, concretely:

- **Validation seam** (`validate.py`): `validate_output(raw_output, schema_path)` — pure function,
  `jsonschema` Draft 2020-12 with the format checker enabled (`date-time` etc. enforced via
  `rfc3339-validator`); never raises on bad output. Non-JSON output, schema violations, and even a
  broken/missing schema file all return `OutputValidation(valid=False, schema_errors=[...])` with
  `<json-path>: <message>` entries — fail closed.
- **Pipeline plug-in point** (`runner.py` at `collecting`): valid output → `_store_result` writes the
  append-only `results` row (`valid=1`, `output_json`, `artifacts_dir`, `schema_errors=NULL`) plus
  `<data_dir>/results/<run_id>/output.json`, audit `result.stored`, then `finalizing → succeeded`.
  Invalid output → `_quarantine` writes a `results` row with `valid=0` + recorded `schema_errors` and
  keeps the evidence on disk at `<data_dir>/quarantine/<run_id>/{raw_output.txt,schema_errors.json}`,
  audit `result.quarantined`, run `failed` with reason `output failed schema validation: …` —
  never stored as valid, never discarded. Teardown attestation still happens on both paths.
  `Settings` gains derived `results_dir` / `quarantine_dir`.
- **Inspection:** socket op `results_get` (valid → `{ok, output, artifacts_dir}`; quarantined →
  `ok:false, quarantined:true, schema_errors`; no result → error) + CLI `tc results get <run>`
  printing the validated JSON on stdout (exit 1 with the schema errors for quarantined runs —
  never the payload as a result). `runs_show`/`tc runs show` surface the result row
  (`valid` / `QUARANTINED` + schema errors).

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
| 03 | Manual run e2e via `vibe-cli` adapter (queue, admission, workspace, manifest, attestation, `tc run`/`tc runs`) | **done** (2026-11-12) | 02 |
| 04 | Output validation + result store + quarantine + `tc results get` | **done** (2026-11-12) | 03 |
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

## 7. Next up — spec 05 (`specs/05-mistral-api-adapter.md`)

The `mistral-api` adapter (blocked by 03+04, both done): OpenAI-compatible HTTP
via `httpx` with JSON-mode structured responses, `providers.toml` for
model/provider presets + pricing, and exponential backoff on 429/5xx. It plugs
into the same `HarnessAdapter` protocol and the same `collecting` validation
channel that spec 04 just built.
