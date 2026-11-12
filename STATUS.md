# STATUS.md — TurboChickens (internal anchor for agentic work)

> Single source of truth for "where are we and what must not be broken".
> Update this file at every spec boundary. Future agents: read this first,
> then the governing docs in §2, before touching code.

**Last updated:** 2026-11-12, after spec 07 landed (133/133 tests green, e2e smoke verified).

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

**Spec 01, spec 02, spec 03, spec 04, spec 05, spec 06, and spec 07 are done** (commits `f8b8061`, the spec-02 commit, the spec-03 commit, the spec-04 commit, the spec-05 commit, the spec-06 commit, and the spec-07 commit).
Verified this session:

```
.venv/bin/python -m pytest -q          → 127 passed
.venv/bin/tc --help                     → works, shows `run`, `runs`, `jobs`, `daemon`, `results`, `status`
.venv/bin/tc jobs validate examples/weekly-incidents-digest/job.toml
                                        → OK … sha256:18efbd8c…80b959 (unchanged)
# e2e smoke (fake vibe binary via $TURBOCHICKENS_VIBE_BIN): tc daemon start (background)
#   → tc run <valid-output job> --wait → succeeded, rc 0
#   → tc results get <run> → validated JSON on stdout, rc 0; results row valid=1 + data/results/<run>/output.json
#   → tc run <invalid-output job> --wait → failed "output failed schema validation", rc 1
#   → tc results get <run> → rc 1 "quarantined" + schema errors; results row valid=0 + data/quarantine/<run>/{raw_output.txt,schema_errors.json}
#   → tc runs list/show/logs → manifest + validator events + attestation; tc status → alerts: 0; tc daemon stop → rc 0
# spec-05 smoke (hermetic fake OpenAI-compatible server on loopback via $TURBOCHICKENS_PROVIDERS):
#   → tc run <mistral-api job> --wait → succeeded, weight=0.25; manifest harness.adapter=mistral-api,
#     usage tokens + cost_usd from providers.toml pricing; tc results get → validated JSON, rc 0
#   → 429 x2 then 200 → succeeded, retry/backoff warn lines in tc runs logs; 400 → failed, no retry
# spec-06 smoke (fake vibe binary via $TURBOCHICKENS_VIBE_BIN, temp data_dir):
#   → tc daemon start → tc schedule add <cron job> → "scheduled job '…' cron='0 7 * * 1' next_run=<next Monday 07:00>"
#   → tc schedule list → row with cron + next_run; tc status → schedules count; tc schedule rm → "no schedules"
#   → a cron expr firing at the next minute boundary enqueues a run with trigger=cron → succeeded, attested,
#     result stored; audit shows schedule.fired + run.queued(trigger=cron); schedules survive a daemon restart
# spec-07 smoke (fake vibe binary via $TURBOCHICKENS_VIBE_BIN, temp data_dir, subprocess daemon):
#   → tc run <SLEEP 30 job, timeout_sec=1> --wait → failed "timeout: hard-killed after 1s", rc 1, in ~1s;
#     manifest budget.outcome=timeout + timed_out=true; teardown attestation (1,1,1); audit teardown.attested +
#     run.finished(reason contains timeout); tc runs show prints the budget block
#   → tc run <1.5 MiB attachment job, max_context_mb=1> --wait → failed at provisioning "injected context
#     1500005 bytes (1.431 MB) > max_context_mb=1", rc 1; never reached running (no harness events); manifest
#     budget.outcome=context_cap + injected_context_bytes; attestation (1,1,1); no results row; workspace gone
#   → tc run <exactly-1-MiB job, max_context_mb=1> --wait → succeeded (cap is inclusive), budget.outcome=ok
```

What exists:

```
src/turbochickens/
├── __main__.py            # python -m turbochickens → CLI
├── util.py                # utcnow / utcnow_iso (UTC, ms precision, Z suffix)
├── settings.py            # Settings (Pydantic, extra=forbid) + load_settings(); defaults below
├── storage.py             # init_db(): full MVP SQLite schema + `schedules` table (spec 06,
│                          # schema_version=2) + append-only triggers + schema_meta
├── audit.py               # AuditLog: append-only JSONL (open "a" only, fsync per row)
├── adapter.py             # HarnessAdapter Protocol (prepare/execute/teardown) + CredentialProvider
│                          # extension point + VibeCLIAdapter (headless subprocess pinned to the
│                          # workspace, sanitized streamed logs) + MistralAPIAdapter (spec 05:
│                          # OpenAI-compatible HTTP via httpx, JSON mode, exp backoff on 429/5xx)
│                          # + adapter weights (API 0.25 / CLI 1.0)
├── providers.py           # spec 05: providers.toml registry — provider/model presets, base_url,
│                          # credential env var, context_window, per-token pricing; resolution:
│                          # settings.providers_path > $TURBOCHICKENS_PROVIDERS > <data_dir>/providers.toml
│                          # > built-in mistral defaults (overlaid per provider)
├── runner.py              # RunPipeline: lifecycle queued→…→succeeded/failed, fresh workspace +
│                          # per-run env, manifest (+ spec-07 `budget` outcome block), run_events,
│                          # spec-04 result store / quarantine at `collecting`, spec-07 context cap
│                          # at `provisioning`, teardown attestation + audit rows
├── scheduler.py           # spec 06: CronScheduler over APScheduler AsyncIOScheduler; standard
│                          # POSIX cron semantics (aps_trigger resolves the day-of-week field via
│                          # croniter because APScheduler numbers 0=Monday); schedules re-registered
│                          # from the DB on daemon start; misfires coalesce (grace 300s)
├── daemon.py              # Daemon: asyncio process, Unix socket, JSON-lines ops
│                          # ping/status/shutdown/enqueue/runs_list/runs_show/runs_logs/results_get/
│                          # schedule_add/schedule_list/schedule_rm (spec 06);
│                          # admission-control scheduler (over-budget backlogs, never rejects);
│                          # a firing cron submits enqueue trigger=cron over the daemon's own socket
├── client.py              # request(socket, op, **payload) → parsed response; DaemonUnreachable
├── validate.py            # spec 04 pure seam: validate_output(raw, schema_path) → OutputValidation
│                          # (jsonschema Draft 2020-12 + format checker; never raises on bad output)
├── spec/
│   ├── model.py           # Pydantic v2 model, all 6 sections, extra="forbid"
│   └── loader.py          # load_spec(), SpecValidationError (field-named issues), spec_hash()
└── cli/main.py            # Typer app `tc`: run [--wait] | runs list/show/logs | results get |
                           # jobs validate | daemon start/stop | status | schedule add/list/rm
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
├── test_schedule.py       # spec 06: aps_trigger POSIX-cron parity seam (vs croniter), schedule
│                          # add/list/rm over socket+CLI, manual-trigger rejection, hash/spec checks,
│                          # replace-on-re-add, persistence across daemon restart, and a real cron firing
│                          # (next-minute expression) driving a trigger=cron run through the full pipeline
├── test_budgets.py        # spec 07: timeout hard-kill e2e (vibe-cli + mistral-api), injected-context
│                          # cap failure at provisioning, at-cap boundary, manifest budget outcome via
│                          # tc runs show, no-token-cap model check
├── test_api_adapter.py    # spec 05: providers seam, MistralAPIAdapter contract seam (MockTransport:
                           # JSON-mode shape, exp backoff on 429/5xx, no retry on 4xx, Retry-After,
                           # timeout, missing credential), e2e via daemon thread + subprocess against
                           # a hermetic loopback fake provider (result store, quarantine, weight 0.25)
examples/weekly-incidents-digest/   # baseline-format example job (job.toml, prompt, schema, csv; mistral-api)
examples/hello-vibe-cli/            # manual vibe-cli example job (job.toml, prompt, schema)
examples/providers.toml             # spec 05 reference provider registry (mistral presets + pricing,
                           # commented other-provider example) — copy to ~/.turbochickens/providers.toml
pyproject.toml             # uv/hatchling, Python >=3.12, version 0.6.0, deps: pydantic, typer, rich, croniter,
                           # jsonschema (+ rfc3339-validator for format checks), httpx, apscheduler (spec 06);
                           # dev: pytest
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
  `$TURBOCHICKENS_VIBE_BIN` else PATH. (`mistral-api` was rejected at enqueue
  until spec 05 landed it.)
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

Spec-05 deliverables, concretely:

- **Provider registry** (`providers.py`): `providers.toml` holds provider presets —
  `base_url`, credential env var (`api_key_env`), and per-model `context_window`,
  per-token pricing (`input_usd_per_mtok`/`output_usd_per_mtok`), and `json_mode` support.
  Resolution: `settings.providers_path` > `$TURBOCHICKENS_PROVIDERS` >
  `<data_dir>/providers.toml` > built-in mistral defaults, overlaid **per provider**
  (a `[providers.X]` table replaces provider X wholesale). `extra="forbid"`; a job picks a
  provider via `model = "provider/model"`, bare names use mistral. Reference file:
  `examples/providers.toml`. Pricing feeds per-run `usage.cost_usd` telemetry now and
  per-job expected-cost stats in spec 08.
- **`MistralAPIAdapter`** (`adapter.py`): same `HarnessAdapter` protocol, so API runs flow
  through the same queue (weight **0.25**), validation, result store, quarantine, and
  teardown attestation as CLI runs. `POST {base_url}/chat/completions` via `httpx` with
  `response_format: {"type": "json_object"}` where the model profile supports it; the
  credential comes from the per-run env (`api_key_env`, added to the credential allowlist) —
  no request is sent without it. **Exponential backoff** on 429/5xx and transport errors
  (`base * 2^attempt`, capped, honoring `Retry-After`), each retry streamed as a warn line to
  the run logs; other 4xx fail immediately without retry. The whole call (incl. backoff) is
  bounded by the wall-clock `timeout_sec` hard kill. Usage (`tokens_in`/`tokens_out` +
  `cost_usd` from pricing) lands in the manifest; the model/provider must resolve from
  `providers.toml` or enqueue fails fast (`ProviderConfigError`).
- **Tests** (`test_api_adapter.py`): providers pure seam; adapter contract seam over
  `httpx.MockTransport` (request shape, backoff, Retry-After, 4xx no-retry, exhaustion,
  transport error, missing credential, timeout, malformed 200); e2e through a real daemon
  (thread + subprocess) against a hermetic loopback fake provider — validated result in the
  store, quarantine on invalid output, backoff visible in `tc runs logs`, 0.25-weight
  admission (4 API runs fit a budget of 1, the 5th backlogs).

Spec-06 deliverables, concretely:

- **Scheduler** (`scheduler.py`): `CronScheduler` wraps APScheduler's `AsyncIOScheduler` in the
  daemon's event loop. `aps_trigger()` builds the `CronTrigger` from a **standard POSIX** 5-field
  expression — APScheduler's own `from_crontab` numbers day-of-week 0=Monday, so the DOW field is
  resolved via croniter into unambiguous day names (verified against croniter: `0 7 * * 1` → Monday
  07:00). Misfires coalesce (grace 300s). Dependency `apscheduler` added to `pyproject.toml`.
- **Persistence** (`storage.py`): new mutable `schedules` table (job_id PK, spec_path, schedule,
  spec_hash, added_at) — `SCHEMA_VERSION` bumped to 2. On every daemon start, persisted schedules
  are re-registered with APScheduler, so a restart loses no schedule.
- **One queue, one IPC contract:** a firing cron does **not** touch the queue directly — it sends
  `enqueue` with `trigger="cron"` over the daemon's own Unix socket (loopback), so cron runs go
  through the exact same validation, queue, admission, pipeline, and audit path as `tc run`.
  Manual and cron runs are indistinguishable downstream except for `runs.trigger` (`manual`/`cron`).
  Audit events: `schedule.added` / `schedule.removed` / `schedule.fired` / `schedule.fire_failed`.
- **Socket ops:** `schedule_add` (spec_path + optional spec_hash; re-validates the spec, requires
  `trigger.type="cron"`, INSERT OR REPLACE so re-adding an edited spec updates the schedule),
  `schedule_list` (rows + live `next_run` from APScheduler), `schedule_rm` (job_id; error if none).
  `status` gains a `schedules` count.
- **CLI:** `tc schedule add <job.toml>` (local validation first — invalid cron is a field-named
  `trigger.schedule` error; manual-trigger jobs are refused), `tc schedule list`, `tc schedule rm
  <job>` — all over the socket like every other command.
- **Tests** (`test_schedule.py`): pure seam `aps_trigger` vs croniter next-fire parity
  (incl. `0 7 * * 1`, `0 0 * * 0`/`7`, steps, ranges, names); add/list/rm over socket + CLI with DB
  and audit assertions; manual-trigger and bad-spec rejection; hash mismatch; replace on re-add;
  persistence across a daemon restart; and one real firing (cron at the next minute boundary)
  producing a `trigger=cron` run that reaches `succeeded` with result store + attestation + audit.

Spec-07 deliverables, concretely:

- **Wall-clock hard kill** (spec 03 adapters, verified e2e here): a run exceeding `timeout_sec` is hard-killed
  (`proc.kill()` / `asyncio.wait_for` over the whole API call incl. backoff), marked `failed` with reason
  `timeout: hard-killed after Ns`, and still gets a full teardown attestation + `teardown.attested` /
  `run.finished` audit rows. Tested for both `vibe-cli` (fake binary sleeping past the budget) and
  `mistral-api` (fake provider slower than the budget).
- **Injected-context cap** (`runner.py` at provisioning): `_provision` now returns the injected-context size in
  bytes (prompt + attachments; injected trajectory state is post-MVP). If it exceeds
  `max_context_mb * 1MiB`, the run fails at provisioning — before any harness work — with a recorded reason
  (`budget exceeded at provisioning: injected context N bytes (X MB) > max_context_mb=M`), no `running`
  transition, no harness events, no result row; teardown attestation + audit rows still happen and the
  workspace is verified gone. The cap is inclusive: exactly-at-cap runs proceed.
- **Budget outcome in the manifest** (`runner.py` `_manifest`): every run manifest gains a `budget` block —
  `{timeout_sec, max_context_mb, injected_context_bytes, timed_out, context_exceeded, outcome}` with
  `outcome ∈ ok | timeout | context_cap` (top-level `timed_out` kept for compatibility) — visible in
  `runs_show` / `tc runs show`. The spec model still has **no token cap**: `max_tokens` anywhere is rejected
  by `extra="forbid"` (covered in `test_spec_model.py` / `test_budgets.py`); token usage remains telemetry
  in `manifest.usage`.
- **Tests** (`test_budgets.py`): timeout hard-kill e2e for both adapters (failed + attested + audited +
  manifest outcome, hard-kill latency asserted), context-over-cap failure at provisioning (clear reason,
  never reached running, attested, audited, no result, workspace gone, `tc runs show` shows `context_cap`),
  exactly-at-cap success, success manifest `budget` block, and the pure no-token-cap model check.

| Spec | Title | Status | Blocked by |
| --- | --- | --- | --- |
| 01 | Scaffold + job spec model + `tc jobs validate` | **done** (`f8b8061`) | — |
| 02 | Daemon + Unix socket (JSON-lines) + SQLite schema + audit log + settings + `tc status` | **done** (2026-11-12) | 01 |
| 03 | Manual run e2e via `vibe-cli` adapter (queue, admission, workspace, manifest, attestation, `tc run`/`tc runs`) | **done** (2026-11-12) | 02 |
| 04 | Output validation + result store + quarantine + `tc results get` | **done** (2026-11-12) | 03 |
| 05 | `mistral-api` adapter + `providers.toml` + backoff on 429/5xx | **done** (2026-11-12) | 03, 04 |
| 06 | Cron scheduling (APScheduler) + `tc schedule add/list/rm` | **done** (2026-11-12) | 03 |
| 07 | Run budgets: wall-clock hard kill + injected-context cap | **done** (2026-11-12) | 03 |
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

## 7. Next up — spec 08 (`specs/08-usage-stats-retention.md`)

Usage stats + expected cost + retention TTLs (blocked by 04, 05 — both done).
Spec 07 (run budgets) landed: wall-clock `timeout_sec` hard kill (attested +
audited on both adapters) and the `max_context_mb` injected-context cap
enforced at provisioning; every manifest now carries a `budget` outcome block
(`ok` / `timeout` / `context_cap`). Token usage stays telemetry — no
`max_tokens` anywhere.
