# STATUS.md — TurboChickens (internal anchor for agentic work)

> Single source of truth for "where are we and what must not be broken".
> Update this file at every spec boundary. Future agents: read this first,
> then the governing docs in §2, before touching code.

**Last updated:** 2026-11-14, after spec 29 landed (the Beak — new module `beak.py`: the pure seam behind the new socket op `workspace_peek` — a running chicken's workspace file tree (capped at 1000 entries) or one file's size-capped content (64 KiB served, `truncated` flag, utf-8 or base64) — path-locked inside that run's workspace (`safe_relpath` + symlink-safe `resolve_locked`; `..`/absolute/backslash refused), available only while the workspace exists: terminal runs (`cancelled`/`stopped`/`succeeded`/`failed`, spec 22) answer a clear `the beak is closed … workspace_gone:true` error because the abattoir already removed the workspace, queued eggs report "no workspace yet", unknown runs `unknown chicken`; the chicken's private harness config (`.vibe/`, `.tc-farmhand-settings.toml` — where spec-27/28 resolved gate/env secrets live) is refused by name, and tree paths are workspace-relative so no host path leaves the daemon; mirrored over the transport seam as `GET /api/runs/{run_id}/workspace(?path=…)` (identical payload, 404 unknown run, 200 with the closed-beak payload for terminal runs, `200 {ok:false, daemon.up:false}` on a dead socket, bearer 401 when required); the SPA run detail gained a Beak tab (`tab-beak`/`pane-beak`: clickable workspace tree with flash-on-change, file peek pane + copy, 40-line live log tail from the existing `runs_logs` polling, empty state for terminal runs and daemon-down, private entries shown disabled) per specs/00-design-system.md, 2 s polling, no host paths in assets; lexicon gained Beak (moved out of Reserved); version bumped 0.0.20 → 0.0.21; full suite green, 402 passed (391 + 11 new tests/test_beak.py)). Earlier the same day, spec 28 landed (connectors registry — new module `connectors.py`: a `connectors.toml` registry resolved exactly like `providers.toml` (`settings.connectors_path` > `$TURBOCHICKENS_CONNECTORS` > `<data_dir>/connectors.toml`, no built-in gates) with `[connectors.NAME]` entries — `transport = "stdio"` (command/args/env) or `transport = "https"` (url + header env refs), strict schema with field-named errors (discriminated union on `transport`, tag stripped from error paths), `extra="forbid"` intact; new socket ops `connectors_list`/`connectors_add`/`connectors_rm` (CRUD over the registry file — atomic rewrite, always 0600, replace-on-re-add) audit-logged as `gate.registered`/`gate.removed`/`gate.rejected` with name+transport+field names only, plus `tc connectors list/add/rm`; `connectors_list` serves non-secret summaries only (stdio `env` as `env_keys`, https `headers` as refs) and `auth_access` now fills its `connectors` scope entry with the registered gate names; `HarnessSection` gains `connectors: list[str] = []` (`extra="forbid"` intact, `connectors = []` dropped from `spec_hash` so the shipped example stays `sha256:18efbd8c…80b959`); enqueue refuses unknown names with a field-named `harness.connectors` error (nothing queued) and refuses the list for non-vibe-cli adapters; the vibe-cli adapter injects the opted-in gates into the run's vibe config (`[mcp_servers.NAME]` entries appended to `<workspace>/.vibe/config.toml`, merged with the spec-27 Farmhand entry when both are present) — `$VAR`/`${VAR}` env/header refs are resolved against the daemon environment only into the ephemeral workspace config, and an unresolvable ref fails the run closed at provisioning (attested (1,1,1), no results row); the manifest mirrors `harness.connectors` as names only; lexicon gained Gate; version bumped 0.0.19 → 0.0.20; full suite green, 391 passed (374 + 17 new tests/test_connectors.py)). Earlier the same day, spec 27 landed (ChickenSubs — `harness.spawn_subs = true` attaches the Farmhand to the chicken's vibe-cli runtime over stdio: `attach_farmhand_to_run` writes an MCP server entry into the run's vibe config (project-local `.vibe/config.toml` in the workspace) spawning the farmhand serve command (the venv-sibling `tc`, else `python -m turbochickens`) plus a token-free settings file the per-run env points at via `TURBOCHICKENS_SETTINGS`, so the spawned Farmhand finds this daemon's socket; `RunContext`/`make_adapter` carry `parent_run_id` + settings, and the manifest harness block gains `spawn_subs` + `farmhand`); subs are ordinary eggs through the same socket/queue/admission (weight unchanged) with lineage `parent_run_id` recorded on the new `runs.parent_run_id` column (schema v5, in-place ALTER migration, old rows NULL), in the manifest, in the `egg.queued` Headcount row, and in `runs_show`/`runs_list` rows; `enqueue` accepts `parent_run_id` (unknown parent refused; depth capped at 1 — a sub enqueueing a `spawn_subs = true` egg is refused with a field-named `harness.spawn_subs` error; `spawn_subs` refused for non-vibe-cli adapters at enqueue); `run_stop` with `caller_run_id` refuses targets that are the caller's ancestors; `build_server` reads `TURBOCHICKENS_RUN_ID`/`TURBOCHICKENS_PARENT_RUN_ID` from the per-run env so a Farmhand inside a chicken tags `enqueue` with `parent_run_id` and `run_stop` with `caller_run_id`, and a sub-chicken's Farmhand serves the narrowed tool set (enqueue + read tools + the guarded `run_stop` — no jobs_validate/run_rerun/schedule_*/settings_get); `spec_hash` drops `spawn_subs=false` so the shipped example stays `sha256:18efbd8c…80b959`; version bumped 0.0.18 → 0.0.19; lexicon gained ChickenSub; full suite green, 374 passed (362 + 12 new tests/test_chickensubs.py)). Earlier same day: spec 26 landed (the Farmhand — new module `farmhand.py`: an MCP server on the official `mcp` Python SDK (new dependency, version bumped 0.0.17 → 0.0.18) whose 17 tools are thin wrappers over the daemon's existing socket ops via `client.request_async` — `status`, `jobs_list`, `jobs_validate` (wraps `jobs_upload`: candle a files bundle or a local `spec_path`), `enqueue`, `runs_list`, `runs_show`, `runs_logs`, `results_get`, `run_stop`, `run_rerun`, `schedule_add`/`schedule_list`/`schedule_rm`, `stats_list`/`stats_show`, `audit_tail`, `settings_get` — served over stdio (`tc farmhand serve --transport stdio|https|both`, informational output on stderr only so the protocol channel stays clean) and/or HTTPS (streamable-HTTP at `farmhand_https_path`, default `/mcp`, mounted into the coop's aiohttp listener via a small ASGI bridge `asgi_request` + the SDK's Starlette app with its lifespan run — no second port — behind the same bearer middleware as `/api/*`, loopback-open rule included, ephemeral token printed once like coop); settings gained `farmhand_transport` (`"stdio"|"https"|"both"`, default `"both"`) and `farmhand_https_path` (single absolute path segment, default `/mcp`) with `extra="forbid"` intact and both exposed in `public_dict()`; MCP scope is narrower than the GUI bearer — no `auth_*` ops, no `retention_sweep`, no `shutdown`/`ping`/raw `jobs_upload`, settings read-only; no tool payload ever contains token material (asserted with a configured token over both transports, and against the audit log); `"both"` serves stdio and HTTPS simultaneously in one `tc farmhand serve` process (when the stdio client hangs up the process exits); coopd binds its unix socket only after the coop (and the Farmhand endpoint) is up, so a ping OK implies the HTTP surface is up and its `coop.started` row is already audited; the coop/standalone aiohttp runners got `shutdown_timeout=15` so a hung MCP SSE stream can never wedge shutdown; full suite green, 362 passed (353 + 9 new tests/test_farmhand.py)). Same day earlier: spec 23 landed (Free-Range chickens — `resources.free_range = true` removes the hard wall-clock kill: `timeout_sec` becomes optional (`int | None`, `gt=0`, `validate_default=True`) with the pairing enforced at spec load (`free_range = false` — the default — requires `timeout_sec > 0`, field-named `resources.timeout_sec` error; `free_range = true` allows it absent and never hard-kills even if present; `extra="forbid"` intact; `spec_hash` drops `free_range=false` so pre-23 egg hashes are unchanged, the shipped example still `sha256:18efbd8c…80b959`); adapters take `timeout_sec: int | None` (`None` = no hard kill — `wait_for(..., None)` waits forever, the API adapter keeps only its 10s connect timeout); the Hatchery enforces `hard_timeout = None if free_range else timeout_sec` and the manifest `resources` + `budget` blocks gain `free_range`; a Free-Range chicken dies only via `run_stop` (spec 22) or daemon shutdown — on shutdown the daemon calls `pipeline.request_stop_free_range()` before the bounded drain, so an in-flight free-range chicken ends `stopped` via the SIGTERM/hard-kill path with attestation (1,1,1) + `chicken.stopped`/`abattoir.attested` audit rows before exit; decision-log amendment for baseline invariants 1/2 recorded in `specs/23-free-range-chickens.md` + §5 below; full suite green, 353 passed (340 + 13 new tests/test_free_range.py)). Same day: spec 25 landed (chaining — `attachments = ["nestbox://<run_id>/output.json"]` in `[input]`: URI shape validated at spec load with field-named errors (`input.attachments`), resolved at provisioning from the Nest Box via the new `chaining.py` seam — valid clutches only, quarantined/composted/missing/malformed → fail-closed provisioning failure before `running` with attested teardown (1,1,1) and no results row; resolved clutch bytes count toward `max_context_mb` (over-cap chain → `budget.outcome = "context_cap"` exactly like spec 07); lineage in the manifest `chaining` block (run id + artifact path + bytes) + `clutch.consumed` Headcount rows + a run-log line; `jobs_upload` bundles no longer need nestbox attachments shipped in them; lexicon gained Chaining; full suite green, 340 passed). Same day earlier: spec 24 landed (Feed Budget cost cap — optional per-egg `resources.max_cost_usd` in USD, off by default, enforced finalize-only: over cap → run failed with `budget.outcome = "cost_cap"` in the existing manifest budget block, cost/token telemetry still recorded, teardown attested as usual; pure seam `cost_cap_exceeded`; full suite green, 307 passed). Same day earlier: spec 22 landed (lifecycle ops — socket ops `run_stop`/`run_rerun`, CLI `tc runs stop`/`tc runs rerun`, terminal states `cancelled`/`stopped` with attested teardown on every stop path, `egg.cancelled`/`chicken.stopped` Headcount rows, `rerun_of` lineage, lexicon terms added first; full suite green, 298 passed). Same day earlier: spec 20 landed (lexicon adoption — every operator-facing surface speaks farm per `docs/lexicon.md`; machine interfaces byte-stable; full suite green, legacy Headcount rows aliased on read only). Same day: spec 21 overview v0.1 drafted (`specs/21-hatchery-farmhand-agent-lifecycle.md` — Hatchery deepening + Farmhand MCP + GUI-for-everything; parent spec to be split into slices 22+, not ready-for-agent; no code changed). Later same day: spec 21 split via to-tickets into slices 22–31 (`specs/22-*.md` … `specs/31-*.md`, all `Status: ready-for-agent`, blocking edges in each file's Blocked-by line; frontier then: 22, 24, 25); still no code changed.

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
| `docs/lexicon.md` | **Naming register** (spec 20): every docs/audit-event/error/UI string uses these terms; machine interfaces stay byte-stable. |
| `specs/archive/turbochickens-mvp-spec.md` | Operational MVP spec derived from the baseline (user stories, decisions, test philosophy). |
| `specs/01-*.md` … `specs/10-*.md` | Implementation tickets. Each has `Status: ready-for-agent`, a "Blocked by" line, and a checkbox acceptance list. |

## 3. Current state — verified 2026-11-12

**Spec 01 through spec 10, specs 12–20, and specs 22–29 are done** (commits `f8b8061`, the spec-02 commit, the spec-03 commit, the spec-04 commit, the spec-05 commit, the spec-06 commit, the spec-07 commit, the spec-08 commit, the spec-09 commit, the spec-10 commit, the spec-12 commit, the spec-13 commit, the spec-14 commit, the spec-15 commit, the spec-16 commit, the spec-17 commit, the spec-18 commit, the spec-19 commit, the spec-20 commit, the spec-22 commit, the spec-23 commit, the spec-24 commit, the spec-25 commit, the spec-26 commit, the spec-28 commit, and the spec-29 commit). Spec 11 is the parent GUI spec; its slices (spec 12, the skeleton; spec 13, Jobs/Audit/Settings views; spec 14, Runs; spec 15, upload/validate; spec 16, Schedules; spec 17, Stats + retention; spec 19, Auth & access) are landed, and its remaining container story landed via spec 18 (the coop Docker image).

Spec 09 landed seat auth: `mistral-api` runs can now run with no API key via a seat provider (`auth = "seat"` in providers.toml + `tc auth login`), and vibe-cli picks up a browser-auth provider from `~/.vibe/config.toml` via HOME passthrough. Spec 10 landed model discovery: `tc models list` queries live provider catalogs (and the vibe binary's own model list), so a job's `model` field can be chosen against reality. Spec 19 gave seat auth a GUI surface (see below).
Verified this session:

```
.venv/bin/python -m pytest -q          → 259 passed
.venv/bin/tc --help                     → works, shows `run`, `runs`, `jobs`, `daemon`, `results`, `schedule`, `stats`, `status`, `auth`, `models`
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
# spec-08 smoke (fake vibe binary via $TURBOCHICKENS_VIBE_BIN, temp data_dir, subprocess daemon):
#   → 3x tc run <job> --wait → tc stats list/show <job> → n=3 tokens_in mean/p50/p95=100, tokens_out=40,
#     tokens_total_p95=140, expected_cost_usd=0.00018; manifest.usage carries tokens per run
#   → backdate runs.finished_at 400d, retention_sweep op → result.expired audit row, artifacts dir gone,
#     append-only results row kept, tc results get <run> → rc 1 "expired"; audit log untouched; tc status
#     prints retention: audit_log=forever results_retention_months=6; sweep fires automatically on daemon start
# spec-09 smoke (loopback fake OAuth + fake provider via $TURBOCHICKENS_PROVIDERS, no *_API_KEY in env):
#   → tc auth login seatco → device flow against fake OAuth server, session stored at
#     <data_dir>/seats/seatco.json (0600); tc auth status shows mode=seat + expiry, never tokens
#   → tc run <seatco/seat-model job> --wait → succeeded; provider saw "Bearer seat-access-1";
#     manifest.auth={mode: seat, token_expires_at: …}, env_keys has SEATCO_TOKEN, no token in manifest/audit;
#     audit has auth.resolved(mode=seat) + run.finished(auth_mode=seat); usage cost_usd=0.0 billed_via=seat;
#     stats expected_cost_usd=0.0
#   → expired session → refresh grant → run succeeds with the NEW bearer token, session rotated on disk
#   → revoked refresh (400 invalid_grant) → run failed "SeatError … tc auth login seatco", zero API requests,
#     session file deleted, teardown still attested (1,1,1)
#   → vibe-cli job with no MISTRAL_API_KEY → succeeded; manifest.auth.mode=none; fake vibe saw home=set,
#     mistral_api_key=unset (browser-auth profile in ~/.vibe/config.toml is vibe's own mechanism)
# spec-10 smoke (loopback fake OpenAI-compatible server serving GET /v1/models via $TURBOCHICKENS_PROVIDERS,
#   second provider pointed at a dead port):
#   → tc models list → provider=mistral reachable=yes cred=env models=2 with per-model [priced] /
#     [no-pricing-preset] marks; provider=dead reachable=no error=transport error (ConnectError); rc 0;
#     the fake server saw "Authorization: Bearer <key>" and the key never appears in the output
#   → no key in env → cred=none, request sent unauthenticated (or 401 → reachable=no row, rc 0)
#   → seat provider (auth="seat", stored session) → cred=seat, /models authorized with the seat token,
#     token never printed; no session → row reachable=no with a `tc auth login <provider>` hint, zero requests
#   → tc models list --adapter vibe-cli → probes the vibe binary with a sentinel --model and prints the
#     models from vibe's own "Unknown model … Available: …" diagnostic (real vibe 3.1.2: 4 models, rc 0)
#   → tc jobs validate <mistral-api job, model without pricing preset> → warning on stderr + OK, rc 0;
#     unknown provider → error harness.model … known providers, rc 1; vibe-cli model not in the probed
#     catalog → error naming the offered models, rc 1
# spec-12 smoke (temp settings, coop_enabled=true, coop_host="0.0.0.0", no coop_auth_token; `tc daemon start`):
#   → stdout prints exactly one "coop auth token: …" line at startup (never in audit/status)
#   → GET /api/ping → 200 unauthenticated; GET /api/status → 401 without bearer, 200 with bearer,
#     body == socket `status` response incl. coop={enabled, url, auth_required}
#   → GET / → 200 text/html (shell: sidebar, wordmark + egg dot, nav, login card, Dashboard stat row +
#     flock diagram); /app.js → application/javascript; /styles.css → text/css; /icons/*.svg → image/svg+xml;
#     unknown path → 404; traversal → 404; loopback+no-token → open (200 without auth)
#   → tc status prints "coop: <url> auth=required|open"; tc coop start (coop_enabled forced) serves the same;
#     tc daemon stop → rc 0, HTTP port refuses connections afterwards, audit has coop.started/coop.stopped
#   → daemon-socket-unreachable seam (CoopServer with dead-socket transport): /api/status → 200 with
#     daemon.up=false, /api/ping → 503 — the SPA renders a banner, not a broken page
# spec-13 smoke (temp settings, coop_enabled=true, `tc coop start` subprocess, fake vibe binary):
#   → GET /api/jobs → jobs=[] before, then one row {job_id, name, spec_hash, registered_at} after
#     `tc run <job.toml>` (jobs_list mirrors the socket op exactly; HTTP payload == socket payload)
#   → GET /api/audit?limit=3 → newest-first tail of audit.jsonl; ?event=run.queued filters;
#     ?limit=abc → 400; ?limit=0 → ok:false from the socket op; parity with audit_tail over the socket
#   → GET /api/settings → effective settings incl. coop block {enabled,url,auth_required};
#     coop_auth_token never appears in the payload (asserted with a configured token)
#   → auth-required mode (0.0.0.0 + token): /api/jobs|/api/audit|/api/settings → 401 without bearer,
#     200 with; /api/ping stays open; dead-socket transport → 200 {ok:false, daemon.up=false} on all
#     three + 503 ping — the SPA renders empty states, not broken pages
#   → GET / serves the SPA with the Jobs table (jobs-tbody), Audit mono tail + event chips
#     (audit-chips/audit-log) and Settings rows (settings-list); app.js polls /api/jobs, /api/audit,
#     /api/settings every 2 s on the active view
# spec-14 smoke (temp settings, coop_enabled=true, `tc coop start` subprocess, fake vibe binary):
#   → POST /api/runs {spec_path} → 200 {ok, run_id, status=queued, weight=1.0}; `tc runs list` shows
#     the run trigger=manual; poll GET /api/runs/{id} → succeeded; GET /api/runs/{id} == socket
#     runs_show (manifest: spec hash, harness vibe-cli 9.9.9-fake, model, temperature, budget.outcome=ok,
#     usage tokens); GET /api/runs/{id}/logs == runs_logs (sanitized harness lines)
#   → GET /api/runs?job=&status= == socket runs_list with the same filters; rows carry result_valid
#   → GET /api/results/{run_id} → 200 {ok, output} == socket results_get == `tc results get` (rc 0, same JSON)
#   → strict-schema job → run failed; GET /api/results/{run_id} → 200 {ok:false, quarantined, schema_errors,
#     raw_output}; `tc results get` rc 1, never the payload; POST with bad body → 400, missing file → 422,
#     unknown run/result → 404; run without a result yet → 404 "no result stored"
#   → backdated run + retention_sweep → GET /api/results/{run_id} → 200 {ok:false, expired:true}, no payload
#   → auth-required mode: all /api/runs* + /api/results/* → 401 without bearer, 200/404 with; /api/ping open;
#     dead-socket transport → 200 {ok:false, daemon.up=false} on all of them (POST included, not 500)
#   → GET / serves the Runs view (run-form, runs-tbody, filter chips, run-detail with Manifest/Logs/Result
#     tabs, copy buttons); app.js polls /api/runs (+ open run's show/logs/results) every 2 s, row flash on change
# spec-15 smoke (temp settings, coop_enabled=true, `tc coop start` subprocess, fake vibe binary):
#   → POST /api/jobs/validate {files: {job.toml, prompts/…, schemas/… (base64)}} → 200 {ok, valid, job_id,
#     spec_hash, spec_path=<data_dir>/uploads/<job>-<spec_hash>/job.toml}; files on disk with exact contents
#   → POST /api/runs {spec_path: <stored path>} → 200 run queued; also POST /api/runs {files: <bundle>}
#     → store+validate+enqueue in one call → run succeeded, GET /api/results/{run_id} over HTTP == socket
#   → invalid bundle (cron without schedule, schema missing from bundle, prompt missing) → 422 with
#     field-named errors (trigger.schedule: …, output.schema_file: …) — nothing stored, no job registered;
#     >10 MiB decoded → 413 "bundle too large"; malformed body → 400; ../ paths → rejected; staging paths
#     rewritten to <bundle> so no host path leaves the daemon
#   → auth-required mode: both POSTs 401 without bearer, 200 with; dead-socket transport → 200
#     {ok:false, daemon.up=false} (never 500); GET / serves the Jobs upload form (48px fields, labels,
#     per-field errors, one primary Validate→Run job button), no host paths in the assets
# spec-16 smoke (temp settings, coop_enabled=true, `tc coop start` subprocess, fake vibe binary):
#   → POST /api/schedule {spec_path: <cron job.toml>, spec_hash} → 200 {ok, job_id, schedule, next_run};
#     GET /api/schedule == socket schedule_list (job, cron, live next_run, added_at); `tc schedule list`
#     shows the row with next_run; DB schedules row + audit schedule.added; status schedules=1;
#     jobs_list rows now carry spec_path (the registered in-container path)
#   → re-POST with an edited cron → 200, still one row with the new expression (replace semantics)
#   → DELETE /api/schedule/<job_id> → 200; schedule_list empty; `tc schedule list` → "no schedules";
#     audit schedule.removed; DELETE again → 404 "no schedule registered"
#   → manual-trigger spec → 422 "only cron-triggered jobs"; bad cron → 422 "trigger.schedule: …";
#     hash mismatch → 422; {} / non-string spec_path / non-JSON → 400; POST {files: <bundle>} →
#     store+validate+schedule in one call (200); invalid bundle → 422 field-named, nothing scheduled
#   → next-minute cron added over HTTP fires within 90s → run trigger=cron → succeeded, attested,
#     result over GET /api/results/<run> 200; audit schedule.fired + run.queued(trigger=cron)
#   → auth-required mode: GET/POST/DELETE /api/schedule* → 401 without bearer, 200 with; /api/ping open;
#     dead-socket transport → 200 {ok:false, daemon.up=false} on all three (never 500)
#   → GET / serves the Schedules view (sched-form with spec select + "Add schedule" primary button,
#     sched-error inline error, schedules table with mono cron + next run + per-row Remove, empty
#     state); app.js polls /api/schedule + /api/jobs every 2 s, flash on change; no host paths in assets
# spec-17 smoke (temp settings, coop_enabled=true, `tc coop start` subprocess, fake vibe binary):
#   → POST /api/runs → succeeded → GET /api/stats == socket stats_list (n=1, tokens_in p95=12,
#     tokens_out p95=34, tokens_total_p95=46, expected_cost_usd=0.001 fallback); GET /api/stats/<job>
#     == stats_show; unknown job → 404 "no usage stats"; `tc stats list/show` print the same numbers
#   → backdate runs.finished_at 7-8 months → POST /api/retention/sweep → 200 {ok, deleted, expired[]}
#     with kind=result/quarantine; artifacts dirs gone; audit result.expired per deletion; results rows
#     kept append-only; re-sweep → deleted=0; GET /api/results/<run> → 200 {ok:false, expired:true},
#     payload never served; `tc results get` rc 1 expired; status.retention.last_sweep carries the outcome
#   → auth-required mode: /api/stats|/api/stats/{job}|/api/retention/sweep → 401 without bearer, 200/404
#     with; /api/ping open; dead-socket transport → 200 {ok:false, daemon.up=false} on all three (POST
#     included, never 500)
#   → GET / serves the Stats view (stats-grid cards + stats-empty + stats-detail) and the Dashboard
#     retention card (retention-summary + "Sweep now" + sweep-outcome/sweep-list of result.expired
#     entries); app.js polls /api/stats every 2 s, loadStatsDetail uses /api/stats/{job}, flash on
#     change; no host paths in assets
# spec-18 smoke (real Docker daemon this session): docker build -t coop . → image built from
#   Dockerfile + .dockerignore; docker run -p 8737:8737 -v coop-data:/data coop → container
#   healthy (HEALTHCHECK GET http://127.0.0.1:8737/api/ping), GET / serves TurboChickens,
#   exactly one "coop auth token:" line in docker logs per start; /api/ping 200 unauthenticated,
#   /api/status 401 without bearer / 200 with bearer (token never in the payload or audit log)
#   → POST /api/jobs/validate {files} → stored under /data/uploads/<job>-<hash>/; POST /api/runs
#   → run succeeded; GET /api/results/<run> → 200 validated JSON; POST /api/schedule → smoke-cron
#   with live next_run; docker rm -f coop + re-run on the same coop-data volume → healthy again,
#   fresh ephemeral token printed once, jobs + runs + result + schedule all still there over HTTP
#   and via `docker exec coop tc schedule list` / `tc runs list`; docker compose -f
#   docker/compose.yaml config → valid
# spec-19 smoke (temp settings, coop_enabled=true, seat provider `seatco` in providers.toml against a
#   hermetic loopback fake OAuth server, no *_API_KEY in env):
#   → GET /api/auth/providers → rows for every provider with mode; seatco mode=seat session=null;
#     HTTP payload == socket auth_providers; no access_token/refresh_token keys, no token values
#   → POST /api/auth/login {provider: seatco} → 200 {user_code, verification_uri, expires_at,
#     interval} — device_code never in the response; POST /api/auth/login/status → 200
#     status=authorized (auto-approve; pending_once → pending then authorized); session stored
#     0600 at <data_dir>/seats/seatco.json; audit auth.login {provider, mode} only — never tokens
#     or the user code; GET /api/auth/providers now shows session {expires_at, expired=false}
#   → POST /api/auth/logout {provider: seatco} → 200, session file gone, providers session=null,
#     audit auth.logout; second logout → 422; login on api-key provider / unknown provider → 422;
#     poll without a start → 422; {} / non-string / non-JSON body → 400
#   → GET /api/auth/access → {env_allowlist: [MISTRAL_API_KEY] (enforced), base_env_keys,
#     network_policy: none (declared, enforcement post-MVP), scope: tools/data/mcp_servers/
#     connectors placeholders} == socket auth_access
#   → seat run after HTTP login → succeeded (fake provider saw "Bearer seat-access-1");
#     runs_list rows carry auth_mode (seat for the seat run, none for a keyless vibe-cli run),
#     GET /api/runs == socket runs_list; run.finished audit row has auth_mode
#   → auth-required mode (0.0.0.0 + coop_auth_token): all five /api/auth/* endpoints → 401 without
#     bearer, 200/422 with; /api/ping open; dead-socket transport → 200 {ok:false, daemon.up=false}
#     on all five (POSTs included, never 500)
#   → GET / serves the Auth view (nav entry + key icon, provider table with mode badges + session
#     countdown, device-code login card, logout actions, Access scope card); app.js polls
#     /api/auth/providers + /api/auth/access every 2 s and the device status every 2 s while a
#     login is in flight; runs table has an Auth column; no host paths in the assets;
#     no response body or audit row ever contains token/device-code material
#   → subprocess e2e (`tc coop start` + fake OAuth): full login → status → providers → logout flow
#     over HTTP, session 0600 on disk between, audit perch.claimed/perch.released, SPA smoke, clean stop
# spec-20 smoke (temp settings, coop on 127.0.0.1:8791, fake vibe binary, subprocess `tc coop start`):
#   → tc run <hello-vibe-cli> --wait → "egg queued run-… (job '…') — pecking order depth 1, weight 1.0"
#     → "chicken run-… → succeeded"; raw audit.jsonl for the run reads, in order:
#     egg.queued → egg.hatched → perch.resolved → clutch.stored → abattoir.attested → chicken.finished
#   → tc status prints "coopd: up (pid=…)" / "pecking order: 0" /
#     "retention: audit_log=forever (the headcount, forever) results_retention_months=6 (…)"
#   → GET /api/audit chips show lexicon labels; a legacy "run.queued" row appended to audit.jsonl
#     still matches ?event=egg.queued and displays as egg.queued over HTTP, raw bytes untouched
#   → GET / serves nav Dashboard/Chickens/Eggs/Rooster/Feed/Headcount/Settings/Perch,
#     "Pecking order", "Queue egg", "Turn the compost"; ids/data-*/api paths unchanged
#   → full suite: 291 passed (286 + 5 new tests/test_lexicon.py: alias-on-read-only with raw-bytes
#     assertion, quoted-string grep guard over src/, SPA lexicon markup, socket+HTTP old/new event
#     filters, lifecycle Headcount order on a real run)
# spec-22 smoke (subprocess daemon, temp data_dir, budget 1, fake vibe binary via
#   $TURBOCHICKENS_VIBE_BIN, SLEEP-30 job enqueued twice so the 2nd backlogs):
#   → tc runs stop <queued> → rc 0 "chicken run-… → cancelled (cancelled while queued — the egg never
#     hatched)"; runs row terminal cancelled + attestation (1,1,1); audit egg.cancelled +
#     abattoir.attested + chicken.finished(status=cancelled); no harness events, no egg.hatched
#   → tc runs stop <running> → rc 0 "chicken run-… → stopped (stopped by operator: SIGTERM (reaped
#     within 5s grace))"; audit chicken.stopped; attestation (1,1,1); workspace gone; no clutch;
#     alerts: []
#   → a SIGTERM-trapping fake vibe → reason "SIGTERM, then hard kill after Ns grace" + the harness's
#     SIGTERM marker line in runs logs (thread-daemon test, grace monkeypatched to 1s)
#   → tc runs rerun <stopped> → rc 0 "egg re-queued run-… — re-run of run-…, pecking order depth 1,
#     weight 1.0"; new run trigger=rerun, manifest rerun_of=<old>, egg.queued row carries rerun_of;
#     goes through queue/admission/pipeline to succeeded with a stored clutch
#   → tc runs list shows status=cancelled / status=stopped with reasons; runs_list rows carry
#     status_reason; audit_tail ?event=chicken.stopped / egg.cancelled filters work; stop on an
#     unknown or already-terminal chicken → rc 1 "unknown chicken" / "already terminal"
#   → full suite: 298 passed (291 + 7 new tests/test_lifecycle_ops.py)
# spec-24 smoke (subprocess daemon, temp data_dir, fake priced provider on loopback via
#   $TURBOCHICKENS_PROVIDERS + fake vibe binary via $TURBOCHICKENS_VIBE_BIN):
#   → tc run <api job max_cost_usd=0.0001> --wait → rc 1 "chicken run-… → failed (feed budget
#     exceeded at finalize: measured cost_usd=0.00018 > max_cost_usd=0.0001)"; manifest
#     budget={outcome=cost_cap, cost_exceeded=true, max_cost_usd=0.0001} + usage cost_usd/tokens;
#     teardown attested (1,1,1); no results row; tc results get → rc 1; tc runs show prints cost_cap;
#     stats_show still rolls n=1 (telemetry recorded); alerts: 0
#   → tc run <api job max_cost_usd=0.00018 (== measured cost)> --wait → succeeded rc 0,
#     budget.outcome=ok, clutch stored, tc results get rc 0
#   → tc run <vibe-cli job max_cost_usd=0.0005, harness cost 0.001> --wait → failed cost_cap
#   → tc run <api job, no max_cost_usd> --wait → succeeded; budget.max_cost_usd=null;
#     tc stats show → expected_cost_usd_per_run: 0.00018
#   → tc jobs validate examples/weekly-incidents-digest/job.toml → sha256:18efbd8c… (unchanged,
#     None excluded from the spec hash); tc daemon stop → rc 0
#   → full suite: 307 passed (298 + 9 new tests/test_cost_cap.py)
# spec-25 smoke (subprocess daemon, temp data_dir, fake vibe binary via $TURBOCHICKENS_VIBE_BIN):
#   → tc run <producer job> --wait → succeeded; tc results get <A> → rc 0 (valid clutch in the nest box)
#   → tc run <consumer job attachments=["nestbox://<A>/output.json"]> --wait → succeeded; tc runs show <B>
#     prints manifest chaining=[{attachment, run_id=<A>, artifact_path=…/results/<A>/output.json,
#     size_bytes=63}] + budget.injected_context_bytes=73 (prompt 10 + clutch 63); audit clutch.consumed
#     (source_run_id=<A>); tc runs logs <B> shows "chained clutch from chicken <A>: nestbox://… →
#     nestbox-<A>-output.json (63 bytes)"; tc results get <B> → rc 0
#   → chain a quarantined clutch (invalid-output job) → rc 1 "failed (provisioning failed: clutch of
#     chicken '…' is quarantined — it failed candling; only valid clutches can be chained)"; never reached
#     running (no harness events); budget.outcome=ok; attestation (1,1,1); no results row; workspace gone
#   → chain a nonexistent chicken → rc 1 "provisioning failed: no clutch in the nest box for chicken …";
#     same fail-closed shape (attested (1,1,1), no results row)
#   → over-cap chain (max_context_mb=1, 1 MiB-10 prompt + 63-byte clutch) → rc 1 failed
#     "budget exceeded at provisioning: injected context 1048629 bytes (1.000 MB) > max_context_mb=1";
#     budget.outcome=context_cap, injected_context_bytes=1048629, attested (1,1,1), no results row
#   → tc jobs validate <attachments=["nestbox://run-1/extra/output.json"] job> → rc 1
#     "error input.attachments: … malformed nestbox URI …"; well-formed nestbox:// URI → OK rc 0
#   → alerts: 0; tc daemon stop → rc 0
#   → full suite: 340 passed (307 + 33 new tests/test_chaining.py)
# spec-23 smoke (subprocess daemon, temp data_dir, fake vibe binary via $TURBOCHICKENS_VIBE_BIN,
#   free-range job with no timeout_sec):
#   → tc jobs validate <free-range job> → OK rc 0; tc jobs validate examples/weekly-incidents-digest/job.toml
#     → sha256:18efbd8c…80b959 (unchanged — free_range=false stays out of the hash)
#   → 2x tc run <SLEEP 8 free-range job> → both status=running after 3s (no hard kill, no TTL)
#   → tc runs stop <one> → rc 0 "chicken run-… → stopped (stopped by operator: SIGTERM (reaped within
#     5s grace))"; manifest resources.free_range=true timeout_sec=None budget.free_range=true
#   → tc daemon stop with the other in flight → rc 0 in <1s; both runs terminal stopped with
#     attestation (1,1,1); audit chicken.stopped + abattoir.attested + coopd.stopped; no results rows
#   → fenced control (timeout_sec=1, SLEEP 30) → failed budget.outcome=timeout as today
#   → full suite: 353 passed (340 + 13 new tests/test_free_range.py)
# spec-26 smoke (subprocess daemon on a temp socket + fake vibe binary; MCP driven by the SDK's own client):
#   → `tc farmhand serve --transport stdio` subprocess + mcp stdio_client: list_tools = the exact 17-tool
#     surface (no auth_*/retention_sweep/shutdown/ping/jobs_upload); status == socket status (uptime aside);
#     jobs_list/runs_list/runs_show/runs_logs/results_get/settings_get/audit_tail/schedule_list/stats_list
#     MCP payloads == socket payloads; enqueue <job> → succeeded; results_get → validated JSON
#     ("hello from fake vibe"); jobs_validate by files bundle and by spec_path → valid + stored under
#     uploads/, enqueueable by the returned spec_path; schedule_add/list/rm round-trip; run_stop on a
#     queued egg (budget 1) → cancelled "never hatched"; run_rerun → fresh succeeded run with rerun_of;
#     settings_get shows farmhand_transport=both farmhand_https_path=/mcp and never the coop token;
#     with coop_auth_token configured, no tool payload (nor the audit log) contains it
#   → `tc coop start` subprocess with coop_auth_token: POST /mcp initialize → 401 without bearer, 200 with;
#     SDK streamable-HTTP client with the bearer: 17 tools, enqueue → succeeded → results_get over HTTPS
#     == socket results_get; token absent from all sampled tool payloads
#   → loopback + no token: POST /mcp initialize → 200 (open, like /api/*); farmhand_transport="stdio" →
#     /mcp not mounted (404/405) while the GUI still serves
#   → 0.0.0.0 + no token: ephemeral "coop auth token:" line printed exactly once; /mcp → 401 without the
#     bearer, 200 + MCP tools with it; /api/ping stays open; token never in payloads/audit
#   → `tc farmhand serve --transport both`: stdio session (tools + enqueue → results_get) and the HTTPS
#     endpoint answer simultaneously; when the stdio client hangs up the process exits (port refuses)
#   → tc --help shows farmhand; tc jobs validate examples/weekly-incidents-digest/job.toml →
#     sha256:18efbd8c…80b959 (unchanged); full suite: 362 passed (353 + 9 new tests/test_farmhand.py)
# spec-27 smoke (subprocess daemon, temp data_dir, fake vibe binary that itself drives the attached
#   Farmhand over stdio via the SDK's MCP client, driven through the real `tc` CLI):
#   → tc run <spawn_subs parent whose prompt says SUBSPAWN <sub-egg path>> --wait → rc 0 succeeded;
#     parent manifest harness={spawn_subs=true, farmhand={attached=true, transport=stdio,
#     config=.vibe/config.toml}}; parent clutch reports sub_run_id + sub_status=succeeded + weight=1.0 +
#     sub_parent_run_id=<parent> and the full 17-tool list (parent is not a sub)
#   → sub (spawn_subs=true, prompt SUBSCOPE <grand-egg path>) → succeeded; runs.parent_run_id=<parent>,
#     sub manifest parent_run_id=<parent>, egg.queued audit row parent_run_id=<parent>, runs_list rows
#     carry parent_run_id; sub clutch shows the 11-tool sub scope (no schedule_*/settings_get/jobs_validate/
#     run_rerun), depth_refusal='harness.spawn_subs: sub-chicken … cannot queue an egg with spawn_subs =
#     true — ChickenSub depth is capped at 1' and stop_refusal='refused: chicken <parent> is an ancestor of
#     the calling sub-chicken …'; exactly two runs exist (no sub-sub); both attested (1,1,1), valid clutches
#   → socket: enqueue <spawn_subs egg> parent_run_id=<sub> → ok:false, errors[0].field=harness.spawn_subs,
#     nothing queued; enqueue parent_run_id=run-nope → 'unknown chicken'; run_stop run_id=<running parent>
#     caller_run_id=<sub> → refused '… is an ancestor …' while the operator stop (no caller) still works
#   → secrecy with coop_auth_token configured: token absent from manifests, results (incl. the settings_get
#     payload the parent's farmhand read), run logs, runs_list/status, and audit.jsonl
#   → tc jobs validate examples/weekly-incidents-digest/job.toml → sha256:18efbd8c…80b959 (unchanged);
#     tc status → alerts: 0; tc daemon stop → rc 0
#   → full suite: 374 passed (362 + 12 new tests/test_chickensubs.py)
# spec-28 smoke (subprocess daemon, temp data_dir, fake vibe binary via $TURBOCHICKENS_VIBE_BIN,
#   TC_GATE_SECRET=<secret> in the daemon env):
#   → tc connectors list → "no gates registered"
#   → tc connectors add toolx --transport stdio --command /bin/toolx --arg --serve --env GATE_TOKEN=$TC_GATE_SECRET
#     → "registered gate 'toolx' (transport=stdio) — registry: <data_dir>/connectors.toml" (file 0600,
#     secret at rest only); tc connectors add webx --transport https --url https://mcp.example.com/mcp
#     --header 'Authorization=Bearer $TC_GATE_SECRET' → registered
#   → tc connectors list → gate=toolx … env_keys=['GATE_TOKEN', 'LOG'] + gate=webx … headers={'Authorization':
#     'Bearer $TC_GATE_SECRET'} — the secret value appears in neither line
#   → tc run <job with harness.connectors = ["toolx", "webx"], prompt GATES> --wait → rc 0 succeeded;
#     tc results get <run> → the fake vibe's sanitized probe shows mcp_servers toolx (command/args/env_keys,
#     env_resolved=true) + webx (url/header_keys, header_resolved=true) — both gates injected into the run's
#     .vibe/config.toml with refs resolved; tc runs show → manifest harness.connectors=["toolx","webx"]
#   → tc run <connectors = ["nope"] job> → rc 1 "error harness.connectors: unknown connector 'nope' — …"
#   → audit.jsonl: gate.registered rows {name, transport} only; the secret is absent from audit.jsonl and
#     daemon.log; tc status → alerts: 0; tc jobs validate examples/… → sha256:18efbd8c…80b959 (unchanged)
#   → tc connectors rm webx → removed; list shows toolx only; tc daemon stop → rc 0
#   → full suite: 391 passed (374 + 17 new tests/test_connectors.py)
# spec-29 smoke (subprocess daemon, temp data_dir, coop on, fake vibe binary that writes notes.txt
#   into its workspace and SLEEPs, via $TURBOCHICKENS_VIBE_BIN):
#   → tc run <SLEEP 25 job> → running → socket workspace_peek <run> → ok tree [notes.txt, prompt.txt];
#     workspace_peek path=notes.txt → utf-8 content; path=../turbochickens.db → refused
#     "unsafe workspace path"; path=.vibe/config.toml → refused private; run-nope → "unknown chicken"
#   → GET /api/runs/{id}/workspace == socket payload (tree + ?path=notes.txt parity);
#     /api/runs/run-nope/workspace → 404; ?path=..%2F..%2Fdb → refused unsafe
#   → GET / serves the Beak tab (tab-beak/pane-beak/beak-tree/beak-pre/beak-log); app.js polls
#     /workspace (+?path=) with the 2 s detail poll
#   → tc runs stop <run> → "chicken run-… → stopped"; socket + GET /api/runs/{id}/workspace →
#     ok:false workspace_gone=true status=stopped "the beak is closed … abattoir"
#   → tc jobs validate examples/weekly-incidents-digest/job.toml → sha256:18efbd8c…80b959 (unchanged);
#     tc status → alerts: 0; tc daemon stop → rc 0
#   → full suite: 402 passed (391 + 11 new tests/test_beak.py)
```

What exists:

```
src/turbochickens/
├── __main__.py            # python -m turbochickens → CLI
├── util.py                # utcnow / utcnow_iso (UTC, ms precision, Z suffix)
├── settings.py            # Settings (Pydantic, extra=forbid) + load_settings(); defaults below;
│                          # spec 12: coop_enabled/coop_host/coop_port/coop_auth_token + coop_url;
│                          # spec 13: public_dict() — effective settings for the GUI, coop_auth_token omitted;
│                          # spec 15: uploads_dir + UPLOAD_MAX_BYTES (10 MiB bundle cap),
│                          # UPLOAD_MAX_HTTP_BODY (aiohttp client_max_size backstop),
│                          # SOCKET_STREAM_LIMIT (one JSON-lines request may carry a whole bundle);
│                          # spec 26: farmhand_transport ("stdio"|"https"|"both", default "both") +
│                          # farmhand_https_path (single absolute path segment, default "/mcp"),
│                          # both exposed in public_dict();
│                          # spec 28: connectors_path — explicit connectors.toml path (else
│                          # $TURBOCHICKENS_CONNECTORS, else <data_dir>/connectors.toml, else an
│                          # empty registry), exposed in public_dict()
├── coop.py                # spec 12: CoopServer — aiohttp HTTP server inside the daemon's event loop;
│                          # /api/ping (unauthenticated healthcheck) + /api/status (bearer unless loopback+
│                          # no token; ephemeral token minted+printed once when non-loopback and none
│                          # configured); spec 13: /api/jobs + /api/audit?limit=&event= + /api/settings;
│                          # spec 14: POST /api/runs (enqueue by in-container spec path, trigger=manual; 400 bad
│                          # body, 422 daemon-refused), GET /api/runs?job=&status=, /api/runs/{id},
│                          # /api/runs/{id}/logs, /api/results/{run_id} (200 with quarantined/expired flags,
│                          # 404 unknown resource) — thin mirrors of the socket ops, 200 {daemon.up=false}
│                          # when the socket is dead; static SPA at /; transport seam = client.request_async
│                          # over the daemon socket (web layer never opens DB/artifacts); coop_auth() seam;
│                          # spec 29 (the Beak): GET /api/runs/{run_id}/workspace(?path=…) — mirror of the
│                          # workspace_peek socket op (workspace tree / size-capped file, 404 unknown run,
│                          # 200 closed-beak payload for terminal runs, 200 {daemon.up=false} when the
│                          # socket is dead, bearer 401 when required) — the web layer never touches the
│                          # workspace itself;
│                          # spec 15: POST /api/jobs/validate (bundle {files: {path: base64}} → daemon
│                          # jobs_upload; 200 valid+stored / 422 field-named errors / 413 cap / 400 malformed)
│                          # and POST /api/runs also accepts {files: …} (store+validate+enqueue by the stored
│                          # path); client_max_size backstop from UPLOAD_MAX_HTTP_BODY;
│                          # spec 16: GET /api/schedule (schedule_list mirror), POST /api/schedule (by
│                          # spec_path or {files} bundle → store+validate+schedule_add; 200 ok / 422
│                          # daemon-refused with the inline error / 413 cap / 400 malformed), DELETE
│                          # /api/schedule/{job_id} (schedule_rm; 404 unknown job) — 200 {daemon.up=false}
│                          # when the socket is dead on all three;
│                          # spec 17: GET /api/stats (stats_list mirror), GET /api/stats/{job_id}
│                          # (stats_show mirror; 404 "no usage stats" when the job has none), POST
│                          # /api/retention/sweep (retention_sweep; 200 with the expired/deleted
│                          # summary, 422 refused, 200 {daemon.up=false} when the socket is dead);
│                          # spec 19: GET /api/auth/providers (auth_providers mirror — mode + seat
│                          # session expiry, never token material), GET /api/auth/access (declared
│                          # access scope), POST /api/auth/login (auth_login_start — relays the
│                          # user-facing user code + verification URI only), POST
│                          # /api/auth/login/status (auth_login_poll — pending/authorized/expired),
│                          # POST /api/auth/logout (auth_logout) — 200 ok / 422 refused / 400
│                          # malformed / 200 {daemon.up=false} when the socket is dead; all behind
│                          # the bearer middleware like every /api/* op;
│                          # spec 26: when farmhand_transport includes "https", the Farmhand MCP
│                          # endpoint (streamable-HTTP at farmhand_https_path) is mounted on this
│                          # same listener via farmhand.FarmhandHTTP + the asgi_request bridge,
│                          # behind the same bearer middleware (loopback-open rule included);
│                          # AppRunner runs with shutdown_timeout=15 so a hung MCP SSE stream
│                          # cannot wedge daemon shutdown
│   └── coop_static/       # the dependency-free SPA (index.html + app.js + styles.css + lucide icons as
│                          # local SVGs, no build step, no runtime CDN): 240px sidebar shell, wordmark +
│                          # egg dot, nav, login card (sessionStorage bearer, no cookies), Dashboard with
│                          # stat row + pulsing flock diagram, spec 13: Jobs table, Audit mono tail with
│                          # event-type chips, Settings read-only rows, spec 14: Runs view (run-by-spec-path
│                          # form, job/status filter chips, table with status egg-dots — queued hollow,
│                          # in-flight pulsing amber, succeeded green, failed red, quarantined amber — and a
│                          # detail pane with Manifest/Logs/Result-or-Quarantine tabs, copy buttons, quarantine
│                          # + expired states) — each view polls its /api/* every 2 s, flash-on-change,
│                          # daemon-down banner + per-view empty states; spec 15: Jobs view upload form
│                          # (job.toml + prompt + schema + attachments file inputs with bundle-path fields,
│                          # 48px fields, per-field errors rendered under their fields, one primary
│                          # Validate→"Run job" button that re-POSTs the bundle to /api/runs);
│                          # spec 16: Schedules view (pick-a-spec select fed by /api/jobs' spec_path +
│                          # one "Add schedule" primary button + inline error, table with mono cron +
│                          # live next_run + per-row Remove, 2 s polling);
│                          # spec 17: Stats view (per-job cards from /api/stats — n, mean/p50/p95 tokens
│                          # in+out, total p95, expected cost per run — card click opens a detail card
│                          # from /api/stats/{job}, 2 s polling, flash on change) + Dashboard retention
│                          # card ("Sweep now" primary button → POST /api/retention/sweep, outcome lists
│                          # the result.expired entries inline; last sweep also shown from
│                          # status.retention.last_sweep) — built to specs/00-design-system.md;
│                          # spec 19: Auth view (nav entry + key-round icon, provider table with mode
│                          # badges + seat-session expiry countdown + Login/Logout actions, device-code
│                          # login card polling /api/auth/login/status every 2 s, read-only Access scope
│                          # card from /api/auth/access) — app.js polls /api/auth/providers +
│                          # /api/auth/access every 2 s; spec 19: the Runs table gained an Auth column
│                          # and the run detail header shows the per-run auth mode (runs_list rows
│                          # carry auth_mode from the manifest's spec-09 auth block); spec 29 (the Beak):
│                          # the run detail gained a Beak tab (tab-beak/pane-beak) — clickable workspace
│                          # tree (flash-on-change) + file peek pane with copy + a 40-line live log tail
│                          # from the existing runs_logs polling, polled every 2 s with the rest of the
│                          # detail, empty state for terminal runs ("the beak is closed") and daemon-down,
│                          # private harness-config entries shown disabled — read-only, no host paths in
│                          # the assets
├── storage.py             # init_db(): full MVP SQLite schema + `schedules` table (spec 06) +
│                          # `job_usage_stats` percentile columns (spec 08, schema_version=3→4 migration)
│                          # + `job_specs.spec_path` (spec 16, schema_version=4, in-place ALTER migration
│                          # of v3 DBs) + append-only triggers + schema_meta; spec 27: `runs.parent_run_id`
│                          # (schema_version 5, in-place ALTER migration of v4 DBs; old rows NULL)
├── audit.py               # AuditLog: append-only JSONL, the Headcount (open "a" only, fsync per row) — never TTL-deleted;
│                          # spec 13: tail(limit, event) — last N rows newest first, defensive read;
│                          # spec 20: LEGACY_EVENT_ALIASES maps pre-20 event names to lexicon names on
│                          # read/display only (tail() normalizes + matches old and new names; read_all()
│                          # and the raw file stay untouched)
├── farmhand.py            # spec 26: the Farmhand — MCP server (official `mcp` SDK, MCPServer) whose
│                          # 17 tools are thin wrappers over the daemon's socket ops via
│                          # client.request_async (status, jobs_list, jobs_validate→jobs_upload,
│                          # enqueue, runs_list, runs_show, runs_logs, results_get, run_stop,
│                          # run_rerun, schedule_add/list/rm, stats_list/show, audit_tail,
│                          # settings_get — no auth_*, no retention_sweep, no shutdown; settings
│                          # read-only); tool results are the socket responses verbatim, daemon-down
│                          # → {ok:false, daemon.up:false}; spec_bundle_b64 reads a local spec_path
│                          # into a jobs_upload bundle; asgi_request bridges aiohttp→ASGI so the
│                          # SDK's streamable-HTTP Starlette app mounts into the coop listener;
│                          # FarmhandHTTP owns that endpoint (path, coop bearer rule, ASGI lifespan);
│                          # serve_stdio / serve_https / serve_transport power
│                          # `tc farmhand serve --transport stdio|https|both` ("both" = the two at
│                          # once in one process; when the stdio client hangs up, it exits); spec 27
│                          # (ChickenSubs): `attach_farmhand_to_run` writes the `[mcp_servers.farmhand]`
│                          # entry into the run's vibe config (`<workspace>/.vibe/config.toml`) spawning
│                          # the farmhand serve command (venv-sibling `tc` else `python -m turbochickens`)
│                          # + a token-free settings file (`settings_toml_for_farmhand`) the per-run env
│                          # points at via TURBOCHICKENS_SETTINGS; `build_server` reads
│                          # TURBOCHICKENS_RUN_ID / TURBOCHICKENS_PARENT_RUN_ID from the env so a Farmhand
│                          # inside a chicken tags enqueue with parent_run_id and run_stop with
│                          # caller_run_id, and a sub-chicken's server serves the narrowed tool set
│                          # (enqueue + reads + guarded run_stop; no jobs_validate/run_rerun/schedule_*/
│                          # settings_get)
├── chaining.py            # spec 25: chaining — `nestbox://<run_id>/output.json` attachments: the URI-shape
│                          # seams (parse_nestbox_uri / is_nestbox_uri / looks_like_uri — exactly
│                          # nestbox://<run_id>/output.json, anything else URI-shaped is a spec-load error),
│                          # attachment_workspace_name (nestbox-<run_id>-output.json in the workspace), and
│                          # the Nest-Box resolver resolve_nestbox_clutch — valid clutches only;
│                          # quarantined / composted (retention-swept) / missing / wrong-shape →
│                          # NestboxError(kind=…) — fail closed, never serves those bytes
├── connectors.py          # spec 28: the connectors registry (gates) — connectors.toml resolved exactly
│                          # like providers.toml (settings.connectors_path > $TURBOCHICKENS_CONNECTORS >
│                          # <data_dir>/connectors.toml; no built-in gates): StdioConnector (command/args/env)
│                          # / HttpsConnector (url + header env refs) discriminated on `transport`,
│                          # extra="forbid", field-named errors; load_connectors/resolve_connectors_path;
│                          # validate_connector_payload + entry_to_dict for `connectors_add`; save_registry
│                          # (atomic write, always 0600) + dump_registry_toml; public_entry (non-secret
│                          # summary: stdio env as key names, https headers as refs); mcp_config_entry +
│                          # attach_connectors_to_run — inject the opted-in gates as [mcp_servers.NAME]
│                          # entries into the run's vibe config, resolving $VAR refs against the daemon env
│                          # only into the ephemeral workspace config (unresolvable ref → ConnectorsError,
│                          # fail closed)
├── stats.py               # spec 08 pure seam: percentile (linear interpolation) + rollup_usage
│                          # (n, mean/p50/p95 tokens in/out/total over a job's finished runs)
├── retention.py           # spec 08: sweep_expired_results — deletes on-disk result/quarantine
│                          # artifacts older than results_retention_months (calendar months),
│                          # audit-logs every deletion as result.expired; never touches the audit log
├── seat.py                # spec 09: SeatBroker — the CredentialProvider impl for providers with
│                          # auth="seat": OAuth device-flow login (tc auth login), session stored
│                          # 0600 under <data_dir>/seats/, refresh of expired tokens (fail-closed on
│                          # revoked), token injected per-run under the provider's api_key_env;
│                          # auth_telemetry() exposes only mode + token expiry (never tokens);
│                          # bearer_token() (spec 10) hands the session token to `tc models list`;
│                          # spec 19: login() is composed from start_device_flow() +
│                          # poll_device_flow() (DeviceGrant holds the device_code server-side
│                          # only) so the GUI can drive the same device flow one socket op at
│                          # a time — same broker, same 0600 session store, no duplicated logic
├── adapter.py             # HarnessAdapter Protocol (prepare/execute/teardown) + CredentialProvider
│                          # extension point + VibeCLIAdapter (headless subprocess pinned to the
│                          # workspace, sanitized streamed logs) + MistralAPIAdapter (spec 05:
│                          # OpenAI-compatible HTTP via httpx, JSON mode, exp backoff on 429/5xx)
│                          # + adapter weights (API 0.25 / CLI 1.0); PreparedRun carries non-secret
│                          # auth telemetry (spec 09); seat runs report cost_usd=0.0 billed_via=seat;
│                          # module-level resolve_vibe_bin/vibe_version helpers (spec 10 probe);
│                          # spec 25: _messages reads each attachment from the workspace under
│                          # attachment_workspace_name (chained clutches land there at provisioning);
│                          # spec 23: execute(..., timeout_sec: int | None) — None = no hard kill
│                          # (Free-Range), wait_for(..., None) waits forever; the API adapter keeps
│                          # only its 10s connect timeout; spec 27: RunContext.parent_run_id;
│                          # VibeCLIAdapter takes `settings` and, when harness.spawn_subs, attaches the
│                          # Farmhand to the run's vibe-cli runtime over stdio (see farmhand.py) — the
│                          # manifest harness block gains spawn_subs + farmhand; make_adapter passes settings;
│                          # spec 28: when harness.connectors is non-empty, VibeCLIAdapter also injects the
│                          # opted-in gates into the run's vibe config via connectors.attach_connectors_to_run
│                          # (merged with the Farmhand entry); the manifest harness block gains
│                          # connectors (names only) on both adapters
├── providers.py           # spec 05: providers.toml registry — provider/model presets, base_url,
│                          # credential env var, context_window, per-token pricing; resolution:
│                          # settings.providers_path > $TURBOCHICKENS_PROVIDERS > <data_dir>/providers.toml
│                          # > built-in mistral defaults (overlaid per provider). Spec 09: ProviderProfile
│                          # gains auth="api-key"|"seat" + oauth_* endpoints (required for seat)
├── runner.py              # RunPipeline (the Hatchery): lifecycle queued→…→succeeded/failed, fresh workspace +
│                          # per-run env, manifest (+ spec-07 `budget` outcome block, spec-09 `auth`
│                          # telemetry block), run_events, spec-04 result store / quarantine at
│                          # `collecting`, spec-07 context cap at `provisioning`, teardown attestation
│                          # + audit rows (incl. spec-09 auth.resolved + auth_mode), spec-08 per-job
│                          # usage-stats rollup at finalize; spec 09: builds the SeatBroker and hands
│                          # it to make_adapter as the CredentialProvider; spec 22: TERMINAL_STATES
│                          # gains `cancelled`/`stopped` (a missing attestation on them stays an alert);
│                          # request_stop()/stop-event race in execute — SIGTERM then hard kill after
│                          # STOP_TERM_GRACE_SEC, terminal `stopped`, output never collected, audit
│                          # chicken.stopped; cancel_queued() terminal-cancels a pecking-order egg
│                          # (manifest + attestation + egg.cancelled, never hatches); manifests carry
│                          # `rerun_of` lineage for run_rerun; spec 24: the optional Feed Budget cost
│                          # cap `resources.max_cost_usd` (USD, off by default) enforced finalize-only
│                          # via the pure seam `cost_cap_exceeded` — over cap → failed with
│                          # budget.outcome="cost_cap" (budget block gains max_cost_usd/cost_exceeded,
│                          # resources mirror gains max_cost_usd), no clutch stored, telemetry +
│                          # attestation as usual; at cap exactly succeeds; seat runs (cost 0.0) never trip;
│                          # spec 25 (chaining): `_provision` resolves `nestbox://<run_id>/output.json`
│                          # attachments from the Nest Box via chaining.resolve_nestbox_clutch — valid
│                          # clutches only; quarantined/composted/missing/malformed → NestboxError →
│                          # the run fails at provisioning before `running` (attested teardown as usual,
│                          # no results row); resolved clutch bytes count toward the spec-07 context cap;
│                          # the manifest gains a `chaining` block (attachment URI + run id + artifact
│                          # path + size_bytes) and the Headcount gains `clutch.consumed` rows;
│                          # spec 23 (Free-Range): `hard_timeout = None if spec.resources.free_range
│                          # else timeout_sec` — no wall-clock kill for free-range chickens; the
│                          # manifest `resources` + `budget` blocks gain `free_range`; active
│                          # free-range chickens are tracked (`_active_free_range`) and
│                          # `request_stop_free_range()` stops them via the spec-22 stop path; spec 27:
│                          # the queue item's parent_run_id lands on RunContext → the manifest
│                          # (also on cancelled queued eggs) + a run-log line when the Farmhand is
│                          # attached to the harness runtime
├── scheduler.py           # spec 06: CronScheduler over APScheduler AsyncIOScheduler; standard
│                          # POSIX cron semantics (aps_trigger resolves the day-of-week field via
│                          # croniter because APScheduler numbers 0=Monday); schedules re-registered
│                          # from the DB on daemon start; misfires coalesce (grace 300s)
├── daemon.py              # Daemon: asyncio process, Unix socket, JSON-lines ops
│                          # ping/status/shutdown/enqueue/runs_list/runs_show/runs_logs/results_get/
│                          # schedule_add/schedule_list/schedule_rm (spec 06)/stats_list/stats_show/
│                          # retention_sweep (spec 08)/jobs_list/audit_tail/settings_get (spec 13,
│                          # read-only GUI ops; settings_get never includes the coop token;
│                          # spec 16: jobs_list rows also carry spec_path, the in-container path the
│                          # job was registered from (persisted into job_specs at enqueue/schedule_add);
│                          # spec 14: runs_list takes optional job=/status= filters + result_valid per row;
│                          # results_get returns quarantined raw_output + schema_errors (never valid) and
│                          # reports expired for artifacts the retention sweep removed (valid or quarantined);
│                          # spec 15: jobs_upload op — validate a base64-file bundle with the existing spec
│                          # loader (field-named errors like `tc jobs validate`, staging paths rewritten to
│                          # <bundle>), cap UPLOAD_MAX_BYTES decoded, safe bundle-relative paths only, and
│                          # store valid bundles under uploads/<job>-<spec_hash>/ (audit job.uploaded /
│                          # job.upload_rejected, never file contents); socket streams raised to
│                          # SOCKET_STREAM_LIMIT so one request line can carry a bundle;
│                          # spec 25: jobs_upload no longer requires `nestbox://` attachments to be
│                          # shipped in the bundle (they resolve from the nest box at provisioning)
│                          # admission-control scheduler (over-budget
│                          # backlogs, never rejects); a firing cron submits enqueue trigger=cron over
│                          # the daemon's own socket; retention sweep at startup + hourly (spec 08);
│                          # results_get reports expired artifacts; status carries a retention block
│                          # + a coop block (spec 12: enabled/url/auth_required, never the token);
│                          # spec 12: with coop_enabled, serve() also starts the coop HTTP server in
│                          # the same loop and shutdown tears it down (audit coop.started/stopped);
│                          # spec 19: auth & access ops for the GUI — auth_providers (SeatBroker.status()
│                          # rows: mode + session expiry, never tokens), auth_access (declared scope:
│                          # env allowlist, network policy, tools/data/MCP/connectors placeholders),
│                          # auth_login_start / auth_login_poll (device flow via the same SeatBroker;
│                          # grants held in daemon memory, sessions stored 0600, audit auth.login with
│                          # provider+mode only), auth_logout (deletes the session, audit auth.logout);
│                          # runs_list rows also carry auth_mode (spec 19) from the manifest auth block;
│                          # spec 22 (lifecycle ops): run_stop (queued egg → terminal `cancelled` via the
│                          # pipeline's cancel_queued — never hatches, still attested (1,1,1), audit
│                          # egg.cancelled; running chicken → the pipeline's stop event → SIGTERM then hard
│                          # kill after STOP_TERM_GRACE_SEC, terminal `stopped`, audit chicken.stopped —
│                          # the op waits for the terminal state and returns it with its reason; clear
│                          # errors for unknown/already-terminal runs) and run_rerun (re-enqueues the same
│                          # egg from job_specs.spec_path as a fresh run, trigger=rerun, lineage
│                          # `rerun_of` on the item → manifest + the egg.queued row); the scheduler checks
│                          # the cancel set when popping/admitting so a cancelled egg is never hatched;
│                          # runs_list rows also carry status_reason (spec 22);
│                          # spec 23 (Free-Range): on shutdown, serve() calls
│                          # pipeline.request_stop_free_range() before the bounded drain, so an
│                          # in-flight free-range chicken is stopped via the spec-22 path (SIGTERM →
│                          # hard kill → terminal `stopped`, attested (1,1,1)) before exit;
│                          # spec 26: with coop_enabled, the coop (incl. the Farmhand MCP endpoint)
│                          # is started BEFORE the unix socket accepts, so a ping OK implies the
│                          # HTTP surface is up and its coop.started row is already audited;
│                          # spec 27 (ChickenSubs): enqueue accepts parent_run_id (unknown parent refused;
│                          # depth cap 1 — a sub enqueueing a spawn_subs=true egg → field-named
│                          # harness.spawn_subs error; spawn_subs refused for non-vibe-cli adapters) and
│                          # records lineage on the runs row + queue item + egg.queued row + response;
│                          # run_stop with caller_run_id refuses ancestor targets (_ancestor_run_ids);
│                          # runs_show/runs_list rows carry parent_run_id;
│                          # spec 28 (connectors registry): ops connectors_list/connectors_add/connectors_rm
│                          # — CRUD over connectors.toml (atomic 0600 rewrite, replace-on-re-add), audit
│                          # gate.registered/gate.removed/gate.rejected (name+transport+fields only, never
│                          # env/header material), list serves non-secret summaries (stdio env_keys,
│                          # https header refs); enqueue refuses unknown harness.connectors names
│                          # (field-named harness.connectors error, nothing queued) and refuses the list
│                          # for non-vibe-cli adapters; auth_access fills its connectors scope entry with
│                          # the registered gate names;
│                          # spec 29 (the Beak): op `workspace_peek` (run_id, optional workspace-relative
│                          # path) — the run's workspace tree or one size-capped file via beak.py,
│                          # path-locked inside <data_dir>/workspaces/<run_id>, available only while the
│                          # workspace exists (terminal runs → closed-beak `workspace_gone` error,
│                          # unknown run → `unknown chicken`); the socket op is the single place that
│                          # touches workspace files
├── beak.py                # spec 29: the Beak — the pure seam behind the `workspace_peek` socket op:
│                          # safe_relpath (relative POSIX only) + resolve_locked (symlink-safe containment
│                          # inside the run's workspace), is_private_relpath (the chicken's private harness
│                          # config — `.vibe/`, `.tc-farmhand-settings.toml` — never served; resolved gate/env
│                          # secrets live there per specs 27/28), workspace_tree (entries capped at
│                          # WORKSPACE_PEEK_MAX_ENTRIES, paths+sizes only), peek_file (content capped at
│                          # WORKSPACE_PEEK_MAX_BYTES served, `truncated` flag, utf-8 else base64)
├── client.py              # request(socket, op, **payload) → parsed response; DaemonUnreachable;
│                          # spec 12: request_async(...) — the same exchange awaitable inside a running
│                          # loop (used by the coop web layer's SocketTransport); spec 15: the stream
│                          # limit is SOCKET_STREAM_LIMIT (bundle-sized request lines)
├── models.py              # spec 10: model discovery, on-demand per invocation (no daemon state) —
│                          # fetch_models (GET {base_url}/models, bearer from the allowlisted env var
│                          # or the seat session token; token never printed/logged), list_provider_catalogs
│                          # (per-provider reachable row + live ids marked priced/no-pricing-preset),
│                          # vibe_catalog (headless sentinel-model probe parsing vibe's own
│                          # "Unknown model … Available:" list), model_issues (validate checks)
├── validate.py            # spec 04 pure seam: validate_output(raw, schema_path) → OutputValidation
│                          # (jsonschema Draft 2020-12 + format checker; never raises on bad output)
├── spec/
│   ├── model.py           # Pydantic v2 model, all 6 sections, extra="forbid"
│   │                      # (spec 23: resources.free_range — bool, default false, declared before
│   │                      #  timeout_sec which is now int | None (gt=0, validate_default=True) with the
│   │                      #  pairing validator: free_range=false requires timeout_sec > 0, true allows
│   │                      #  it absent — field-named resources.timeout_sec errors)
│   │                      # (spec 24: resources.max_cost_usd — optional USD cost cap,
│   │                      #  positive-only, default None = off; still no max_tokens)
│   │                      # (spec 25: [input].attachments entries that are URI-shaped must be
│   │                      #  exactly nestbox://<run_id>/output.json — field-named error otherwise);
│   │                      # (spec 27: harness.spawn_subs — bool, default false; spec_hash drops
│   │                      #  spawn_subs=false so pre-27 egg hashes are unchanged)
│   │                      # (spec 28: harness.connectors — list[str] of gate names from
│   │                      #  connectors.toml, default []; spec_hash drops the empty list so
│   │                      #  pre-28 egg hashes are unchanged; unknown names refused at enqueue)
│   └── loader.py          # load_spec(), SpecValidationError (field-named issues), spec_hash()
│                          # (spec_hash excludes None optionals and free_range=false, so pre-23
│                          #  spec hashes are unchanged)
└── cli/main.py            # Typer app `tc`: run [--wait] | runs list/show/logs/stop/rerun (spec 22:
                           # stop = run_stop — queued egg → cancelled, running chicken → SIGTERM then hard
                           # kill → stopped, rc 0 / clear error for unknown or already-terminal; rerun =
                           # run_rerun — same egg as a fresh run with lineage) | results get |
                           # jobs validate (spec 10: model checks) | daemon start/stop | status |
                           # schedule add/list/rm | stats list/show (spec 08) |
                           # auth login/status/logout (spec 09, local) | models list (spec 10, local) |
                           # coop start (spec 12: daemon with the GUI forced on; status prints coop:) |
                           # farmhand serve --transport stdio|https|both (spec 26: the Farmhand MCP
                           # server; --transport defaults to the farmhand_transport setting; stdio is a
                           # protocol channel so informational output goes to stderr only) |
                           # connectors list/add/rm (spec 28: gates CRUD over connectors.toml over the
                           # socket, non-secret summaries only)
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
├── test_stats_retention.py # spec 08: pure seams (percentile/rollup math, months_before, schema v3
│                          # migration), stats e2e over socket + CLI (API pricing + vibe-cli cost
│                          # fallback), retention e2e (backdated runs, result.expired audit rows,
│                          # quarantine expiry, custom TTL, startup sweep, audit log never deleted)
├── test_seat_auth.py      # spec 09: provider auth-field seams, SeatStore perms/fail-closed, broker +
│                          # adapter contract over MockTransport (seat token, refresh, revoked,
│                          # no session), device flow over loopback fake OAuth, tc auth CLI, and
│                          # daemon e2e: seat run with no *_API_KEY, expired→refresh, revoked→fail
│                          # closed, vibe-cli via HOME passthrough
└── test_models.py         # spec 10: fetch_models contract over MockTransport (bearer sent only with
                           # a token, token never in errors), model_issues pure seam, tc models list
                           # e2e over a loopback fake /models server (priced marks, unreachable row
                           # rc 0, env-key/401/seat-session credential paths), vibe catalog via a fake
                           # vibe binary (probe parse + broken binary), and tc jobs validate
                           # warn/error model checks
├── test_coop.py           # spec 12: coop auth seam (loopback/token/ephemeral), HTTP API against a thread
│                          # daemon on a temp port (ping/status mirrors, pages + content types, 401/200 auth,
│                          # token never in status/audit, ephemeral token printed once, shutdown tears HTTP
│                          # down, coop-disabled status block, daemon-down payload via dead-socket transport),
│                          # and subprocess e2e of `tc coop start` (open + non-loopback ephemeral-token modes)
└── test_coop_views.py     # spec 13: the jobs_list/audit_tail/settings_get socket ops standalone (empty →
                           # registered after enqueue, newest-first tail, event filter, limit bounds,
                           # token never served), AuditLog.tail pure seam, HTTP parity with the socket ops
                           # (/api/jobs, /api/audit?limit=&event=, /api/settings), auth-required mode,
                           # empty-state + filter behaviour over HTTP, dead-socket daemon-down payloads,
                           # and the served SPA markup for the three views
└── test_coop_runs.py      # spec 14: runs_list job/status filters + result_valid over the socket; POST
                           # /api/runs enqueue over HTTP with CLI parity (`tc runs list` trigger=manual) and
                           # pipeline to terminal; /api/runs(+filters)/{id}/{id}/logs /api/results/{id} HTTP ==
                           # socket parity; quarantine over HTTP (schema_errors + raw_output, never valid);
                           # expired artifacts → 200 expired state; 400/422/404 request handling; auth-required
                           # 401s; dead-socket daemon-down payloads incl. POST; served Runs SPA markup; and a
                           # subprocess e2e (`tc coop start` + fake vibe): HTTP-triggered run → succeeded →
                           # validated result over HTTP + CLI
└── test_coop_schedule.py  # spec 16: GET/POST /api/schedule + DELETE /api/schedule/{job_id} against a
                           # thread daemon (HTTP == socket parity, CLI parity, re-add replaces, 404 on
                           # unknown rm, 422 inline rejections for manual/invalid/mismatch, {files}
                           # bundle add, auth-required 401s, dead-socket daemon-down payloads), one
                           # real next-minute cron firing over HTTP → trigger=cron run → succeeded +
                           # attested + audited + result over HTTP, served SPA Schedules view markup,
                           # and a subprocess e2e (`tc coop start` + fake vibe): add/list/rm over HTTP
└── test_coop_upload.py    # spec 15: jobs_upload socket op standalone (valid bundle stored under
                           # uploads/<job>-<hash>/ + enqueueable by that path; invalid → field-named errors,
                           # nothing stored/registered; cap, unsafe paths, base64, shape), POST
                           # /api/jobs/validate + POST /api/runs {files} over HTTP (200/422/413/400, HTTP ==
                           # socket parity, auth 401s, dead-socket daemon-down payloads), served SPA upload
                           # form + no host paths in assets, and a subprocess e2e creating + running a
                           # brand-new job entirely over HTTP with the fake vibe binary
└── test_coop_stats.py     # spec 17: GET /api/stats + /api/stats/{job} + POST /api/retention/sweep against a
                           # thread daemon (HTTP == socket parity, CLI parity with tc stats list/show, 404
                           # unknown job, sweep over HTTP with backdated runs → result.expired audit rows +
                           # artifacts gone + expired over /api/results + idempotent re-sweep + stats
                           # unaffected + status last_sweep, auth-required 401s, dead-socket daemon-down
                           # payloads on all three, served SPA Stats view + Dashboard retention card), and
                           # a subprocess e2e (`tc coop start` + fake vibe) running + sweeping over HTTP
├── test_coop_auth.py      # spec 19: /api/auth/* against a thread coop daemon with a hermetic loopback
                           # fake OAuth server behind a seat provider (providers list + socket parity,
                           # device login flow over HTTP e2e auto-approve + pending-then-authorized,
                           # session stored 0600, logout, 422/400 refusals, socket ops standalone,
                           # runs_list auth_mode over HTTP == socket incl. a real seat run, no
                           # token/device-code material in any response or audit row, auth-required
                           # 401s when non-loopback, dead-socket daemon-down payloads on all five
                           # endpoints, served SPA Auth view + runs Auth column, and a subprocess
                           # e2e of `tc coop start` running the whole login → logout flow over HTTP
├── test_coop_container.py # spec 18: the shipped docker/settings.toml defaults (data_dir=/data, coop
                           # 0.0.0.0:8737, no fixed token, all stateful paths under /data), the
                           # Dockerfile contract (base, EXPOSE, VOLUME, loopback /api/ping HEALTHCHECK,
                           # ENV, CMD), .dockerignore, compose example + docs coverage, and a subprocess
                           # e2e of the container CMD (`tc daemon start` with the shipped settings,
                           # data_dir/port redirected) — GUI served, ephemeral token printed exactly
                           # once, ping 200 + 401/200 bearer, run over HTTP to succeeded with result,
                           # and jobs/runs/results/schedules surviving a daemon restart on the data dir
├── test_api_adapter.py    # spec 05: providers seam, MistralAPIAdapter contract seam (MockTransport:
                           # JSON-mode shape, exp backoff on 429/5xx, no retry on 4xx, Retry-After,
                           # timeout, missing credential), e2e via daemon thread + subprocess against
                           # a hermetic loopback fake provider (result store, quarantine, weight 0.25)
└── test_lifecycle_ops.py   # spec 22: run_stop on a queued egg (socket + CLI parity: cancelled, never
                           # hatched, attestation (1,1,1), audit egg.cancelled, no harness events, list/
                           # show/audit-tail surfaces, unknown + already-terminal errors), run_stop on a
                           # running chicken (SIGTERM path + SIGTERM-ignoring hard-kill escalation with a
                           # streamed marker, stopped, attestation (1,1,1), audit chicken.stopped, no
                           # clutch, no alerts, no token material in the new rows), run_rerun (fresh run,
                           # trigger=rerun, rerun_of in manifest + egg.queued, normal queue/admission/
                           # pipeline to succeeded, rerun-after-cancel, CLI parity + errors), and a
                           # subprocess-daemon e2e (fake vibe) doing cancel + stop + rerun end to end
└── test_cost_cap.py         # spec 24: the finalize cost-cap seam (over / at cap / no cap / seat cost 0 /
                           # unmeasured), the ResourcesSection.max_cost_usd model checks (optional,
                           # positive-only, extra=forbid intact, field-named errors via load_spec), and
                           # e2e over a thread daemon: over-cap API run (fake priced provider) → failed
                           # cost_cap + manifest cap/usage + attestation + no clutch + stats rollup +
                           # tc runs show; at-cap run → succeeded; no-cap run → telemetry + stats;
                           # vibe-cli over/at cap via the fake vibe binary; seat run with a tiny cap →
                           # succeeded (cost_usd 0.0)
├── test_free_range.py      # spec 23: the free_range/timeout_sec pairing seam (default false requires
                           #  timeout_sec, true allows absent/present-but-positive, extra=forbid intact,
                           #  field-named load_spec + tc jobs validate errors, spec_hash stable for pre-23
                           #  eggs incl. the shipped example), and e2e over a thread daemon + subprocess
                           #  daemon with the fake vibe binary: free-range chicken not hard-killed at any
                           #  TTL while a fenced control times out, free_range + timeout present still not
                           #  killed, run_stop → stopped with attestation (1,1,1) + chicken.stopped audit
                           #  (socket + CLI), and daemon shutdown with a free-range chicken in flight →
                           #  attested stopped before exit
└── test_chaining.py        # spec 25: the nestbox:// URI parse seam (valid / 13 malformed shapes), the
                           # spec-load validator (field-named input.attachments errors via load_spec +
                           # tc jobs validate, extra=forbid intact), the pure Nest-Box resolver seam
                           # (valid / quarantined / composted / missing / wrong-shape), and e2e over a
                           # thread daemon with the fake vibe binary: producer run lays a clutch, consumer
                           # chains it → succeeded with manifest chaining lineage + clutch.consumed audit +
                           # run-log line + injected_context_bytes counting the clutch bytes; quarantined /
                           # missing / composted references → fail-closed provisioning failure (never
                           # running, attested (1,1,1), no results row, workspace gone); over-cap chain →
                           # budget.outcome=context_cap; jobs_upload accepts a nestbox:// attachment
                           # without shipping it in the bundle and the uploaded job runs chained
└── test_farmhand.py        # spec 26: the Farmhand MCP server — the farmhand settings block pure seam
                           # (defaults both//mcp, enum + extra=forbid + path shape, public_dict), the
                           # CLI --transport validation, and e2e with a real MCP client (the SDK's
                           # stdio_client / streamable_http_client): stdio against `tc farmhand serve`
                           # (exact 17-tool surface — no auth_*/retention_sweep/shutdown/jobs_upload —
                           # with MCP payloads == socket payloads for every op, enqueue→succeeded→
                           # results_get, jobs_validate by files and by spec_path, schedules, run_stop
                           # on a queued egg → cancelled + run_rerun → succeeded, secrecy with a
                           # configured coop token), HTTPS on a `tc coop start` subprocess (401 without
                           # the bearer / 200 + MCP with it, loopback-open without a token, /mcp not
                           # mounted when farmhand_transport="stdio", non-loopback ephemeral token
                           # gating /mcp and never appearing in any tool payload or the audit log), and
                           # `--transport both` serving stdio and HTTPS simultaneously in one process
├── test_connectors.py     # spec 28: connectors registry — parser/resolution/strict-schema pure seams,
│                          # non-secret public_entry + payload validation + save/0600 round-trip + ref
│                          # resolution (incl. fail-closed), the harness.connectors spec field + hash
│                          # stability (example hash unchanged), socket CRUD with audit rows + secrecy,
│                          # $TURBOCHICKENS_CONNECTORS source, auth_access gate names, enqueue refusals
│                          # (unknown gate, non-vibe-cli adapter), e2e injection into the run's vibe config
│                          # via the fake vibe binary (stdio + https, merged with a Farmhand entry, no-key →
│                          # no config, unresolvable ref → fail-closed attested), CLI round-trip
└── test_chickensubs.py     # spec 27: ChickenSubs — spawn_subs field/hash pure seams, schema v5 migration
                           # (v4 DB gains runs.parent_run_id, old rows NULL), the attach seam (vibe config
                           # entry + token-free settings file + per-run env), socket-level lineage/depth-cap/
                           # ancestor-refusal tests, and the e2e: a fake vibe binary that itself drives the
                           # attached Farmhand over stdio (SDK MCP client) — parent enqueues a sub (weight 1.0,
                           # parent_run_id in runs/manifest/runs_list/egg.queued), sub gets the 11-tool sub
                           # scope, its spawn_subs enqueue refused (harness.spawn_subs) and its run_stop on
                           # the parent refused (ancestor); stdio-level sub/full scope tests with the run env;
                           # secrecy with a configured coop token over manifests/results/logs/audit
└── test_beak.py           # spec 29: the Beak — the beak.py pure seams (path lock incl. symlink escape,
                           # private-config blocklist, tree caps, size-capped utf-8/base64 peek), the
                           # workspace_peek socket op standalone (unknown/queued/terminal closed-beak,
                           # live tree + file peek + cap + traversal refusals on a running chicken via a
                           # fake vibe that writes its workspace — incl. a secret planted in
                           # .vibe/config.toml that never leaves), HTTP==socket parity + 404/401/
                           # daemon-down payloads, secrecy with a configured coop token (payloads +
                           # audit), served SPA Beak-tab markup with no host paths, and a subprocess
                           # e2e (peek a running chicken over HTTP, stop it, watch the beak close)
examples/weekly-incidents-digest/   # baseline-format example job (job.toml, prompt, schema, csv; mistral-api)
examples/hello-vibe-cli/            # manual vibe-cli example job (job.toml, prompt, schema)
examples/providers.toml             # spec 05 reference provider registry (mistral presets + pricing,
                           # commented other-provider example) — copy to ~/.turbochickens/providers.toml
examples/connectors.toml            # spec 28 reference connectors registry (stdio command/args/env and
                           # https url/headers examples, $ENV ref semantics documented) — copy to
                           # ~/.turbochickens/connectors.toml
pyproject.toml             # uv/hatchling, Python >=3.12, version 0.0.20, deps: pydantic, typer, rich, croniter,
                           # jsonschema (+ rfc3339-validator for format checks), httpx, apscheduler (spec 06),
                           # aiohttp (spec 12, coop web server), mcp (spec 26, the Farmhand MCP server);
                           # dev: pytest
Dockerfile                 # spec 18: the coop image — python:3.12-slim, pip install . (the wheel ships
                           # the SPA), ships docker/settings.toml → /etc/turbochickens/settings.toml via
                           # $TURBOCHICKENS_SETTINGS, PYTHONUNBUFFERED=1, VOLUME ["/data"], EXPOSE 8737,
                           # HEALTHCHECK GET /api/ping on loopback, CMD ["tc", "daemon", "start"]
.dockerignore              # spec 18: build context trimmed to pyproject.toml + src/ + docker/
docker/settings.toml       # spec 18: shipped container defaults — data_dir="/data", coop_enabled=true,
                           # coop_host="0.0.0.0", coop_port=8737, no fixed coop_auth_token (ephemeral,
                           # printed once at startup)
docker/compose.yaml        # spec 18: compose example — image coop, 8737:8737, named volume coop-data:/data
docs/coop-container.md     # spec 18: build/run/compose docs, /data volume map, token handling,
                           # healthcheck + settings overrides, reverse-proxy (TLS) note, manual smoke
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

Spec-08 deliverables, concretely:

- **Usage stats rollup** (`stats.py` + `runner.py`): every finalized run rolls its `usage_json` into
  the mutable `job_usage_stats` row — n plus mean/p50/p95 of tokens in, out, and total (linear-
  interpolation percentiles; pure seam in `stats.py`). `SCHEMA_VERSION` bumped to 3 with the new
  percentile columns (`tokens_in_p50/p95`, `tokens_out_p50/p95`) added in place to existing v2 DBs.
  Expected cost per run is derived from `providers.toml` pricing for the job's model
  (`providers.cost_usd` over the mean tokens); when the model doesn't resolve (e.g. a vibe-cli
  harness model), it falls back to the mean of the per-run `cost_usd` the harness reported.
  Token usage stays telemetry — no `max_tokens` anywhere (still rejected by `extra="forbid"`).
- **Inspection:** socket ops `stats_list` / `stats_show` + CLI `tc stats list` / `tc stats show
  <job>` printing n, mean/p50/p95 tokens in+out, total p95, and `expected_cost_usd_per_run`.
- **Retention** (`retention.py` + `daemon.py`): `sweep_expired_results` deletes on-disk artifacts
  (`results/<run_id>/` and `quarantine/<run_id>/` — only ever paths inside those two dirs) whose run
  finished more than `results_retention_months` ago (calendar-month arithmetic); every deletion is
  audit-logged as `result.expired` (run_id, kind, artifacts_dir, retention_months). The append-only
  `results` rows stay as the permanent index, and the audit log is **never** TTL-deleted
  (`audit_retention` only accepts `"forever"`). The daemon sweeps at startup and hourly, plus an
  on-demand `retention_sweep` socket op; `status` carries a `retention` block; `results_get` /
  `tc results get` report expired artifacts (rc 1) instead of serving them.

Spec-09 deliverables, concretely:

- **Provider profiles** (`providers.py`): `ProviderProfile` gains `auth: "api-key" | "seat"` (default `api-key`, unchanged) plus `oauth_device_url` / `oauth_token_url` / `oauth_client_id` / `oauth_scope`. A validator requires the three OAuth endpoints when `auth = "seat"` and rejects the fields for `api-key`; `extra="forbid"` still rejects unknown keys. Built-in defaults stay `api-key`.
- **Credential broker** (`seat.py`): `SeatBroker` implements the existing `CredentialProvider` protocol. `tc auth login <provider>` runs the OAuth device flow (prints verification URI + user code, polls the token endpoint with `authorization_pending`/`slow_down` handling) and stores the session via `SeatStore` — one 0600 JSON file per provider under `<data_dir>/seats/` (dir 0700; `Settings.seats_dir`). Sessions are never written to job specs, manifests, or the audit log. Before a run, `credentials()` refreshes an expired token (30s leeway) via the refresh grant and injects the access token under the provider's usual `api_key_env` — so `build_per_run_env`'s allowlist discipline and both adapters are untouched. Fail-closed: no session, unreadable session, or revoked refresh → `SeatError` with a `tc auth login` hint; the run fails before any HTTP request. `auth_telemetry()` exposes only `{mode, token_expires_at}`.
- **Adapter/runner wiring**: `RunPipeline` builds the broker and passes it to `make_adapter`; `PreparedRun` gains an `auth` block. The run manifest gains top-level `auth: {mode: "api-key"|"seat"|"none", token_expires_at}`; the audit log gains `auth.resolved` (mode + expiry) per run and `auth_mode` on `run.finished`. No token ever appears in either. `mistral-api` sends the seat-derived bearer exactly like an API key; missing credential still fails closed.
- **vibe-cli**: no code needed — `HOME`/`XDG_CONFIG_HOME` already pass through `BASE_ENV_KEYS`, so a browser-auth provider registered in vibe's own `~/.vibe/config.toml` (`browser_auth_base_url`/`browser_auth_api_base_url`) is used by the vibe binary itself; the manifest records `auth.mode = "none"` when no env credential was injected (e2e-tested with a fake vibe binary).
- **Cost/stats** (spec 08 intact): seat runs record `usage.cost_usd = 0.0` + `usage.billed_via = "seat"` (never a fabricated per-token price); `job_usage_stats.expected_cost_usd` is 0.0 for seat providers.
- **CLI**: `tc auth login <provider>` / `tc auth status` / `tc auth logout <provider>` — local (no daemon), never print or log tokens.
- **Tests** (`test_seat_auth.py`, 21): provider auth-field seams; store round-trip/perms/corrupt-fail-closed; broker + adapter contract over `MockTransport` (seat bearer sent, expired→refresh→new token, revoked→fail closed + session deleted, no session→no request, api-key passthrough unchanged); device flow over a loopback fake OAuth server (auto-approve, pending-then-success, api-key provider rejected); CLI login/status/logout; and four daemon e2e cases over loopback fake OAuth+API servers with no `*_API_KEY` in the environment.

Spec-10 deliverables, concretely:

- **Discovery module** (`models.py`): all on-demand per invocation, no daemon state, no caching. `fetch_models()` does `GET {base_url}/models` per provider in `providers.toml` and parses the OpenAI-compatible `{"data": [{"id": …}]}` payload (sorted, deduped; malformed → error). `list_provider_catalogs()` returns one row per provider — reachable or not + error — and marks each live model id `[priced]`/`[no-pricing-preset]` based on the provider's presets. `vibe_catalog()` probes the vibe binary headless with a sentinel `--model` (`__turbochickens_model_probe__`, never valid, so vibe fails before any API call) and parses its own `Unknown model … Available: …` diagnostic — the exact list a vibe-cli run validates `--model` against — run with the full user env so vibe's keyring/session credential resolution works. `model_issues()` backs the validate checks.
- **Credentials (spec 05/09):** the /models call sends `Authorization: Bearer …` from the provider's allowlisted `api_key_env` when set (spec 05), or from the seat session token for `auth = "seat"` providers via the new public `SeatBroker.bearer_token()` (refreshing first when expired; no/revoked session → the row is marked unreachable with a `tc auth login <provider>` hint and zero requests are sent — fail closed). No key is ever printed, logged, or present in error strings (asserted in tests); without a key the request goes out unauthenticated (`cred=none`).
- **CLI:** `tc models list [--adapter mistral-api|vibe-cli] [--settings …]` — local, no daemon. Default lists every provider (rows `provider=… auth=… cred=… base_url=… reachable=yes|no`, model lines with preset marks); unreachable providers are rows, not failures — rc stays 0 (rc 1 only for an invalid providers file or a bad `--adapter`). `--adapter vibe-cli` prints the vibe binary's own catalog (`bin=… version=… reachable=… models=N` + ids).
- **`tc jobs validate`** (now takes `--settings` for providers resolution): a `mistral-api` model whose *provider* is unknown → `error harness.model: unknown provider …` (exit 1); a known provider without a pricing preset for the model → `warning harness.model: … no pricing preset …` on stderr, still `OK` exit 0 (the live catalog may offer it — check `tc models list`); a `vibe-cli` model not in the probed catalog → `error harness.model: unknown model … the vibe binary offers: …` (exit 1), skipped when the probe can't determine the catalog. Enqueue-time behavior is unchanged (spec 05: unresolvable model/provider still fails fast at the daemon).
- **Tests** (`test_models.py`, 22): `fetch_models` contract over `MockTransport` (id parsing, bearer only with a token, 500/malformed/transport errors, token never in errors); `model_issues` pure seam; `tc models list` e2e over a hermetic loopback fake /models server (live ids + pricing marks, dead-port provider row rc 0, env-key vs no-key vs 401 vs seat-session credential paths, key/token never in output); vibe catalog via a fake vibe binary (probe parse, broken binary → reachable=no rc 0); and the `tc jobs validate` warn/error cases.

Spec-12 deliverables, concretely:

- **Settings** (`settings.py`): `coop_enabled` (false), `coop_host` (`127.0.0.1`), `coop_port` (8737), `coop_auth_token` (none), derived `coop_url`; `extra="forbid"` intact (bad ports rejected).
- **Web module** (`coop.py`): `CoopServer` runs an aiohttp server **inside the daemon's event loop** when `coop_enabled` (spec 11's single new dependency, asyncio-native). Routes: `GET /api/ping` (unauthenticated healthcheck; 503 structured payload if the socket is unreachable), `GET /api/status` (mirror of the socket `status` op; 200 with `daemon.up=false` when the socket is unreachable so the SPA shows a banner), `GET /` + static assets with correct content types (traversal blocked). `coop_auth()` pure seam: loopback + no token → open; non-loopback or any configured token → bearer required; configured token wins, else an ephemeral `secrets.token_urlsafe` token is **printed exactly once to stdout** at startup — never written to the audit log or the status payload. Auth middleware covers `/api/*` except `/api/ping`; static assets stay open so the SPA can load and show its login card on 401. No cookies; the SPA keeps the token in `sessionStorage` and sends `Authorization: Bearer …`.
- **Transport seam:** `SocketTransport.request(op, **payload)` calls the new `client.request_async` over the daemon's own Unix socket — the web layer never opens the SQLite DB or artifact files, so no GUI path can bypass queue/admission/validation/quarantine/attestation/append-only.
- **Daemon integration** (`daemon.py`): `serve()` starts/stops the coop server with the daemon (audit `coop.started`/`coop.stopped`, token never audited); `status` gains a `coop` block `{enabled, url, auth_required}`; shutdown tears the HTTP server down cleanly. CLI: `tc coop start` (forces `coop_enabled`) behaves like `tc daemon start` with `coop_enabled = true`; `tc status` prints the `coop:` line.
- **SPA** (`coop_static/`, bundled in the wheel): dependency-free `index.html` + `app.js` + `styles.css` + lucide icons as local SVGs (no build step, no runtime CDN), built to `specs/00-design-system.md` — 240px sidebar on the paper ground with hairline, TurboChickens wordmark + egg dot, nav (Dashboard/Runs/Jobs/Schedules/Stats/Audit/Settings), daemon pill at the foot ("coop · up 3h 12m"); login card on 401 (token field, one primary button); Dashboard = stat row (uptime, queue depth, in-flight vs weight budget, alerts, schedules, retention) + the flock diagram (daemon hub, queue/schedules in, in-flight lanes out, pulses in 2.5–4 s cycles restarting on hover), polling `/api/status` every 2 s with changed values flashing once and a clear daemon-down banner. Other views render their headers + honest empty states until specs 13–17.
- **Tests** (`test_coop.py`, 22 + 3 settings): pure `coop_auth`/`is_loopback` seam; HTTP API against a thread daemon on a temp port (ping/status mirror the socket ops, pages + content types, 404/traversal, auth 401/200, token never in status/audit, ephemeral token printed exactly once, shutdown tears HTTP down, coop-disabled status block); the daemon-down payload through a dead-socket transport; and subprocess e2e for both `tc coop start` (open mode, no token printed) and non-loopback (ephemeral token from stdout, 401/200, clean stop). No browser automation.

| Spec | Title | Status | Blocked by |
| --- | --- | --- | --- |
| 01 | Scaffold + job spec model + `tc jobs validate` | **done** (`f8b8061`) | — |
| 02 | Daemon + Unix socket (JSON-lines) + SQLite schema + audit log + settings + `tc status` | **done** (2026-11-12) | 01 |
| 03 | Manual run e2e via `vibe-cli` adapter (queue, admission, workspace, manifest, attestation, `tc run`/`tc runs`) | **done** (2026-11-12) | 02 |
| 04 | Output validation + result store + quarantine + `tc results get` | **done** (2026-11-12) | 03 |
| 05 | `mistral-api` adapter + `providers.toml` + backoff on 429/5xx | **done** (2026-11-12) | 03, 04 |
| 06 | Cron scheduling (APScheduler) + `tc schedule add/list/rm` | **done** (2026-11-12) | 03 |
| 07 | Run budgets: wall-clock hard kill + injected-context cap | **done** (2026-11-12) | 03 |
| 08 | Usage stats + expected cost + retention TTLs | **done** (2026-11-12) | 04, 05 |
| 00 | Design system — TurboChickens GUI | **settled** (2026-11-12; governs 12–18) | — |
| 09 | Seat/subscription auth, no API key | **done** (2026-11-12) | 05 |
| 10 | Model discovery (`tc models list`) | **done** (2026-11-12) | 05 |
| 11 | TurboChickens GUI in the coop container (parent spec) | in progress — skeleton slice landed via 12 | 02–08 |
| 12 | Coop skeleton: daemon serves GUI (HTTP + auth + shell + Dashboard) | **done** (2026-11-12) | — |

Spec-13 deliverables, concretely:

- **Socket ops** (`daemon.py`): `jobs_list` (registered rows of the append-only `job_specs` table:
  `job_id`, `name`, `spec_hash`, `registered_at`), `audit_tail` (last N rows of the audit log, newest
  first, optional `event` filter; `limit` defaults to 100, must be an int in 1..1000 — otherwise an
  error response; read-only, the log stays append-only), `settings_get` (the effective settings via
  `Settings.public_dict()` plus the `coop` status block — `coop_auth_token` is never included).
  `AuditLog.tail(limit, event)` (audit.py) is the defensive read seam: blank/torn lines skipped,
  newest-first slice.
- **HTTP mirrors** (`coop.py`): `GET /api/jobs`, `GET /api/audit?limit=&event=`, `GET /api/settings` —
  thin mirrors over the transport seam (`_mirror` helper), identical payloads to the socket ops under
  the same auth rules as spec 12 (401 without bearer when required; `/api/ping` stays open; non-integer
  `limit` → 400). When the daemon socket is unreachable all three answer 200 with
  `{ok: false, daemon: {up: false}, error}` so the SPA renders empty states, not broken pages.
- **SPA** (`coop_static/`): Jobs view = surface table (job, mono spec hash truncated with the full
  `sha256:…` in the title, registered-at); Audit view = event-type filter chips (built from an
  unfiltered tail) + mono log tail, newest first, `?event=` filter on the rows; Settings view =
  read-only label/value rows of the effective settings incl. the coop line. Each view polls its
  `/api/*` endpoint every 2 s while active, flashes its card once on change, and renders a clear
  daemon-down/empty state. Built to `specs/00-design-system.md` (mono data, muted labels, surface
  panels, chips are outlined — the one filled yolk button per view stays reserved).
- **Tests** (`test_coop_views.py`, 13): socket ops standalone (empty → registered after `enqueue`;
  newest-first tail vs the on-disk log; event filter; limit bounds; `settings_get` contents and token
  absence), the `AuditLog.tail` pure seam (incl. a torn final line), HTTP/socket payload parity for
  all three mirrors, auth-required mode (401/200), empty-state and filter behaviour over HTTP,
  dead-socket daemon-down payloads (200 structured + 503 ping), and the served SPA markup.
Spec-14 deliverables, concretely:

- **Socket ops** (`daemon.py`): `runs_list` accepts optional `job` (matches job id or
  name) and `status` (exact) filters and adds `result_valid` (None until the run
  reaches collecting) to every row, so the runs table can show the quarantined
  state. `results_get` now returns the quarantined `raw_output` alongside
  `schema_errors` (held in the append-only row — still never a valid result) and
  reports `expired` for on-disk artifacts the retention sweep removed, for valid
  and quarantined rows alike; the payload itself is never served once expired.
- **HTTP API** (`coop.py`): `POST /api/runs` (JSON body `{"spec_path": …, "spec_hash"?}` →
  `enqueue` with `trigger="manual"`; 400 on a malformed body, 422 with the socket
  response when the daemon refuses, e.g. invalid/missing spec), `GET /api/runs?job=&status=`
  (mirror of `runs_list` with filters), `GET /api/runs/{run_id}` (full manifest via
  `runs_show`), `GET /api/runs/{run_id}/logs` (`runs_logs`), `GET /api/results/{run_id}`
  (`results_get`). Single-resource GETs answer 200 with the payload — including the
  `quarantined`/`expired` flags the SPA renders — and 404 when the resource itself is
  missing ("unknown run" / "no result stored"); all of them answer 200 with a
  structured `daemon.up=false` payload when the socket is dead (POST included). Same
  bearer auth rules as every other `/api/*` route; `/api/ping` stays open.
- **SPA Runs view** (`coop_static/`): header action = spec-path input + the one filled
  "Run job" button (inline field error on 400/422/daemon-down; on success the new run's
  detail pane opens); job filter chips (from `/api/jobs`) + status filter chips; the
  runs table (run id button, job, status egg-dot + label, trigger, started, finished)
  with row click → detail pane; status egg-dots per the design system — queued hollow,
  in-flight pulsing yolk, succeeded green, failed red, quarantined amber (failed +
  `result_valid=false`); changed rows flash once; the detail pane has tabs Manifest
  (full run JSON incl. spec hash, harness/provider/model/temperature, budget block,
  usage, attestation), Logs (sanitized streamed events, follows the tail only when
  already at the bottom) and Result-or-Quarantine (validated JSON, or the quarantine
  note + schema errors + raw output, or the expired state) — mono panes with a copy
  button. The view polls `/api/runs` (plus the open run's three endpoints) every 2 s,
  so statuses move without a reload.
- **Tests** (`test_coop_runs.py`, 10): socket filter/`result_valid` seam; POST
  `/api/runs` over HTTP with CLI parity and pipeline-to-terminal, HTTP==socket parity
  for list/filters/show/logs/results; quarantine over HTTP (never valid); expired
  over HTTP; 400/422/404 handling; no-result-yet 404; auth-required 401s; dead-socket
  daemon-down payloads; served Runs SPA markup; subprocess e2e with the fake vibe
  binary (HTTP-triggered run → succeeded → validated result over HTTP + CLI).

Spec-15 deliverables, concretely:

- **Socket op** (`daemon.py`): `jobs_upload` takes `{"files": {bundle-path: base64}}`,
  requires a root `job.toml`, rejects unsafe bundle paths (absolute, `..`, empty
  segments, backslash/NUL) and non-base64 content with field-named errors, caps the
  total decoded size at `UPLOAD_MAX_BYTES` (10 MiB, `too_large` flag), stages under
  `uploads/.staging-<uuid>/`, validates with the existing `load_spec` (+ the prompt
  and attachments the spec references must be in the bundle; + `model_issues` against
  the daemon's providers, no vibe probe), and only when valid stores the bundle at
  `uploads/<job>-<spec_hash>/` and returns `{ok, valid, job_id, spec_hash,
  spec_path, size_bytes, files, warnings}`. Errors come back as
  `[{field, message}]` exactly like `tc jobs validate` (`trigger.schedule: …`),
  with staging paths rewritten to `<bundle>` so no host path leaves the daemon.
  Invalid bundles are never stored, never register a job; audit gets
  `job.uploaded` / `job.upload_rejected` (field names only, never contents).
  Socket streams raised to `SOCKET_STREAM_LIMIT` (one JSON-lines request can carry a
  whole bundle; over-limit lines get one error response, not a dead connection).
- **HTTP** (`coop.py`): `POST /api/jobs/validate` mirrors `jobs_upload` — 200 valid
  (with the stored in-container `spec_path`), 422 field-named errors, 413 over the
  cap (both the daemon's decoded cap and the aiohttp `client_max_size` backstop
  `UPLOAD_MAX_HTTP_BODY`), 400 malformed, 200 structured `daemon.up=false` when the
  socket is dead. `POST /api/runs` now also accepts `{"files": …}` — store + validate
  + enqueue by the stored path in one call, so the browser never needs a path. Same
  bearer rules as every `/api/*` route.
- **SPA Jobs view** (`coop_static/`): upload card with file inputs for job.toml,
  prompt, schema (each with its bundle path) and attachments, 48px fields with
  visible labels, per-field errors rendered under their fields (mapped by dotted
  field prefix: `input.prompt_file` → prompt, `output.schema_file` → schema,
  `input.attachments` → attachments, everything else → job.toml), one primary
  button that reads "Validate" and becomes "Run job" once the bundle validated —
  clicking it re-POSTs the bundle to `/api/runs` and opens the new run in the Runs
  view. No host paths enter the UI (the stored path stays server-side).
- **Tests** (`test_coop_upload.py`, 14): socket op standalone (store layout + exact
  contents + hash, enqueue by stored path to a succeeded run, invalid → field-named
  errors + nothing stored/registered + no path leaks, missing prompt/schema,
  cap/unsafe-path/base64/shape), HTTP (validate + runs-bundle, HTTP == socket parity,
  422/413/400 shapes, auth 401s, dead-socket payloads, served SPA markup, no host
  paths in assets), and a subprocess e2e creating + running a brand-new job entirely
  over HTTP with the fake vibe binary, result fetched over HTTP + CLI parity.

Spec-16 deliverables, concretely:

- **Registration path** (`storage.py` + `daemon.py`): `job_specs` gains a
  nullable `spec_path` column (schema_version 4, in-place `ALTER TABLE`
  migration of v3 DBs — the append-only triggers guard row UPDATE/DELETE, not
  DDL). Both registration sites (`enqueue`, `schedule_add`) persist the
  resolved in-container path, and `jobs_list` returns it per row (`None` for
  pre-v4 rows) — this is what lets the Schedules add form offer registered
  specs to pick from.
- **HTTP** (`coop.py`): `GET /api/schedule` (mirror of `schedule_list` — job,
  cron, live `next_run`, `added_at`, `spec_hash`), `POST /api/schedule` —
  `{"spec_path": …, "spec_hash"?}` for a registered/uploaded in-container
  spec, or `{"files": …}` to store+validate+schedule a bundle in one call —
  200 with the `schedule_add` response (job_id, schedule, next_run), 422
  carrying the socket error when the daemon refuses (manual-trigger spec,
  invalid cron, hash mismatch, unreadable spec) so the form shows it inline,
  413/422 via the upload mapping for bundles, 400 on a malformed body,
  200 structured `daemon.up=false` when the socket is dead. `DELETE
  /api/schedule/{job_id}` mirrors `schedule_rm` — 200, 404 when no schedule is
  registered, daemon-down payload 200. Same bearer rules as every `/api/*`
  route; `/api/ping` stays open.
- **SPA Schedules view** (`coop_static/`): header action = pick-a-spec
  `<select>` (populated from `/api/jobs` rows with a `spec_path`, labeled
  `name · sha256:…`, value = the path, carrying the spec hash for the
  mismatch check) + the one filled "Add schedule" button; the table (job,
  mono cron, mono live next_run, added_at) with a per-row "Remove" action;
  rejections render as an inline error under the header. The view polls
  `/api/schedule` + `/api/jobs` every 2 s, flashes the card once on change,
  and renders the daemon-down empty state. Re-adding an already-scheduled job
  replaces it (the daemon's INSERT OR REPLACE semantics), so the select keeps
  scheduled jobs too.
- **Tests** (`test_coop_schedule.py`, 9): HTTP==socket parity for
  list/add/rm + CLI parity (`tc schedule list` shows next_run; re-add of an
  edited spec replaces; rm → "no schedules", 404 on unknown) with DB/audit/
  status assertions and the new `jobs_list.spec_path`; one real next-minute
  cron firing added over HTTP → `trigger=cron` run through the full pipeline
  (succeeded, attested, `schedule.fired` + `run.queued(trigger=cron)` audit,
  result over HTTP, `tc runs list` shows it); rejections (manual 422, bad
  cron 422 field-named, hash mismatch 422, unreadable path 422, malformed 400,
  bundle add 200 + invalid bundle 422); auth-required 401s; dead-socket
  daemon-down payloads on all three routes (never 500); served SPA markup with
  no host paths; and a subprocess e2e (`tc coop start` + fake vibe) doing
  add/list/rm entirely over HTTP.

Spec-17 deliverables, concretely:

- **HTTP** (`coop.py`): `GET /api/stats` (mirror of `stats_list` — per-job n,
  mean/p50/p95 tokens in+out, total p95, expected cost per run, updated_at),
  `GET /api/stats/{job_id}` (mirror of `stats_show`; 404 when the job has no
  stats yet via the shared "no usage stats" resource-missing mapping), and
  `POST /api/retention/sweep` (the on-demand `retention_sweep` op: 200 with
  `{ok, cutoff, results_retention_months, audit_retention, expired[], deleted}`,
  422 when the daemon refuses, 200 structured `daemon.up=false` when the socket
  is dead — POST included, never a 500). Same bearer rules as every `/api/*`
  route; `/api/ping` stays open.
- **SPA Stats view** (`coop_static/`): per-job cards from `/api/stats` — job
  name (button) + job id, then stat numerals: runs n, tokens in mean·p50·p95,
  tokens out mean·p50·p95, tokens total p95, expected cost per run, updated —
  card click opens a detail card fed by `GET /api/stats/{job_id}` (label/value
  rows, incl. the 404 "no usage stats" case); empty state when no job has
  finished runs; polls `/api/stats` every 2 s, flashes the grid once on change,
  daemon-down empty state. Numbers formatted like the CLI (`%.6g`).
- **SPA Dashboard retention card** (`coop_static/`): summary line (audit
  retention, results TTL months, last sweep deleted count) from
  `status.retention`, the view's one filled primary "Sweep now" button →
  `POST /api/retention/sweep`; the response's `expired` entries (run id, kind,
  finished_at — the `result.expired` outcomes) are listed inline under the
  card, and the polled `status.retention.last_sweep` keeps the last sweep's
  outcome visible; inline error on daemon-down/refusal. The Runs view's
  Result tab keeps showing the expired state for swept runs (spec 14 seam).
- **Tests** (`test_coop_stats.py`, 7): HTTP==socket parity for
  `/api/stats` + `/api/stats/{job}` (404 unknown) + CLI parity
  (`tc stats list/show` print the same numbers) after runs triggered over
  HTTP with the fake vibe binary (n, tokens 12/34/46, expected_cost 0.001
  fallback); sweep over HTTP with backdated runs (valid + quarantined) →
  deleted=2, `result.expired` audit rows, artifacts gone, append-only results
  rows kept, idempotent re-sweep, `/api/results/{run}` → 200 expired (payload
  never served) + `tc results get` rc 1, stats unaffected, status carries the
  last sweep; auth-required 401s on all three routes; dead-socket
  daemon-down payloads on all three (never 500); served SPA markup for the
  Stats view + retention card with no host paths in assets; and a subprocess
  e2e (`tc coop start` + fake vibe) running a job and sweeping entirely over
  HTTP.

Spec-18 deliverables, concretely:

- **`Dockerfile`** (repo root, `python:3.12-slim`): `pip install .` from a trimmed
  context (`.dockerignore` keeps only `pyproject.toml`, `src/`, `docker/`), ships
  `docker/settings.toml` to `/etc/turbochickens/settings.toml` with
  `ENV TURBOCHICKENS_SETTINGS` pointing at it and `PYTHONUNBUFFERED=1` (so the
  one-time token line hits `docker logs` immediately), `VOLUME ["/data"]`,
  `EXPOSE 8737`, `HEALTHCHECK` doing `GET http://127.0.0.1:8737/api/ping`, and
  `CMD ["tc", "daemon", "start"]` (exec form: SIGTERM reaches the daemon for a
  graceful stop). With the shipped settings that CMD is exactly "daemon with
  coop enabled on 0.0.0.0:8737" — the container adds no UI of its own.
- **`docker/settings.toml`**: `data_dir = "/data"`, `coop_enabled = true`,
  `coop_host = "0.0.0.0"`, `coop_port = 8737`, no fixed `coop_auth_token` —
  everything stateful (socket, db, audit, results, quarantine, uploads, seats)
  lives under the data dir, so the `/data` volume carries it all.
- **`docker/compose.yaml`**: example service (image `coop`, build from the repo
  root, `8737:8737`, named volume `coop-data:/data`, `restart: unless-stopped`).
- **`docs/coop-container.md`**: build (`docker build -t coop .`), run
  (`docker run -p 8737:8737 -v coop-data:/data coop`), compose, the `/data`
  volume map, token handling (ephemeral token printed once via `docker logs`,
  bearer in sessionStorage, how to pin a fixed token via a mounted settings
  file, loopback-open mode), healthcheck + port-override note, settings
  overrides, a reverse-proxy note (TLS termination is a proxy's job; `/api/ping`
  stays open), `docker exec coop tc …` usage, and the manual smoke steps.
- **Tests** (`test_coop_container.py`, 6): shipped-settings defaults + token
  absence from `public_dict()`; the Dockerfile/.dockerignore/compose/docs
  contracts; and a subprocess e2e running the container CMD with the shipped
  settings (data_dir/port redirected to the sandbox) — GUI served, ephemeral
  token printed exactly once per start, `/api/ping` 200 on loopback, 401/200
  bearer auth, a run over HTTP to `succeeded` with the validated result served,
  and jobs/runs/results/schedules surviving a daemon restart on the same data
  dir. The real image smoke (build, healthy container, token-once, full HTTP
  flow, container replacement over the volume) ran on a real Docker daemon
  2026-11-12 and is documented step-by-step in the docs.

| 13 | Jobs / Audit / Settings views (read-only socket ops) | **done** (2026-11-12) | 12 |
| 14 | Runs from the GUI (trigger, watch, results/quarantine) | **done** (2026-11-12) | 12 |
| 15 | Upload + validate job specs from the browser | **done** (2026-11-12) | 14 |
| 16 | Schedules from the GUI | **done** (2026-11-12) | 12, 13 |
| 17 | Stats + retention from the GUI | **done** (2026-11-12) | 12 |
| 18 | coop Docker container | **done** (2026-11-12) | 12 |
| 19 | Auth & access management UI (seat sessions, provider auth modes, access scope for tools/data/MCP/connectors) | **done** (2026-11-12) | 09, 12 |
| 20 | Lexicon adoption — every surface speaks farm (`docs/lexicon.md` rolled out to Headcount events, GUI copy, CLI output, errors, docs; machine interfaces unchanged; legacy rows aliased on read) | **done** (2026-11-14) | 01–19 |
| 21 | Hatchery & Farmhand overview v0.1 — lifecycle ops (stop/re-run), Free-Range, cost cap (off by default), chaining, Farmhand MCP (stdio/https/both), ChickenSubs, connectors registry, GUI for everything | overview v0.1 — **not ready-for-agent**; split into slices 22+ via to-tickets before implementation | 01–20 |
| 22 | Lifecycle ops — `run_stop` (queued → `cancelled`, running → SIGTERM/hard kill → `stopped`, attested) + `run_rerun` (lineage) over socket, CLI, and audit | **done** (2026-11-14) | 02–04 |
| 23 | Free-Range chickens — `resources.free_range = true` removes the hard TTL (`timeout_sec` optional, pairing validated); death only by `run_stop` or daemon shutdown, both attested | **done** (2026-11-14) | 22 |
| 24 | Feed Budget cost cap — `resources.max_cost_usd` (USD, off by default, finalize-only `budget.outcome = "cost_cap"`, telemetry always recorded) | **done** (2026-11-14) | 05, 08 |
| 25 | Chaining — `nestbox://<run_id>/output.json` attachments resolved from the Nest Box at provisioning (valid clutches only), counted toward the context cap, lineage in manifest/audit | **done** (2026-11-14) | 04, 07 |
| 26 | The Farmhand — MCP server over the farm's own socket ops (stdio + HTTPS on the coop listener, bearer-gated; `tc farmhand serve --transport stdio|https|both`; `farmhand_transport`/`farmhand_https_path` settings) | **done** (2026-11-14) | 22, 12–19 |
| 27 | ChickenSubs — `harness.spawn_subs = true` attaches the Farmhand to the chicken's vibe-cli runtime over stdio (MCP entry in the run's vibe config); subs are ordinary eggs in the same queue/admission with `parent_run_id` lineage (schema v5) in store/manifest/audit, depth capped at 1 (field-named `harness.spawn_subs` refusal), sub tool scope narrowed (enqueue + reads + guarded `run_stop`; ancestors refused; no schedule/auth/settings tools) | **done** (2026-11-14) | 22, 26 |
| 28 | Connectors registry — `connectors.toml` (resolved like `providers.toml`) with stdio/https gate entries; socket ops `connectors_list`/`connectors_add`/`connectors_rm` (CRUD, audit `gate.*`, non-secret summaries only) + `tc connectors list/add/rm`; chickens opt in via `harness.connectors = […]` (unknown names refused at enqueue, field-named); the vibe-cli adapter injects the gates into the run's vibe config with `$VAR` refs resolved only into the ephemeral workspace; `auth_access` connectors entry filled with gate names | **done** (2026-11-14) | 26 |
| 29 | The Beak — read-only live view into a running chicken: socket op `workspace_peek` (workspace tree + size-capped single-file content, path-locked inside the run's workspace, only while the workspace exists — terminal → closed-beak error) + `GET /api/runs/{id}/workspace` mirror + the Beak tab in the coop run detail (tree, file peek, live log tail; 2 s poll, flash-on-change, empty/daemon-down states; private harness config never served) | **done** (2026-11-14) | 22 |

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

**Amendments to the baseline decision log (recorded at slice boundaries, per the
slice spec files):** spec 23 (2026-11-14) — invariant 1: the wall-clock
`timeout_sec` hard kill moves from "always enforced" to **"enforced unless
free-range"** (`resources.free_range = true` carries no hard TTL and dies only
via `run_stop` or daemon shutdown, both attested; `free_range = false` keeps
the pre-23 behavior; `max_context_mb` unchanged; still no `max_tokens`).
Invariant 2 (spec 22): terminal states additionally include `cancelled` (queued
egg pulled from the pecking order — never hatches, still attested) and
`stopped` (running chicken ended by operator or daemon shutdown) — every
terminal state still requires the teardown attestation. Full text:
`specs/23-free-range-chickens.md` §"Decision-log amendment".

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
  what/why. **One commit per ticket/feature/slice:** each spec ticket (or
  feature slice) lands as its own commit — never bundle two tickets into one commit,
  and never land a ticket with another ticket's unfinished work mixed in; when two
  slices touch the same file, split the diff by hunk (stage per-slice changes
  explicitly) so each commit is coherent and green on its own.
- **No agent as a contributor on GitHub (settled 2026-11-12):** never add a
  `Co-Authored-By: Mistral Vibe <vibe@mistral.ai>` trailer to a commit — GitHub
  maps that email to the `mistral-vibe` account and then lists it as a repo
  contributor and as co-author on every pushed commit. A plain
  `Generated by Mistral Vibe.` line is fine (GitHub does not parse it as
  attribution). The pushed `main` history was rewritten on 2026-11-12 to strip
  the trailer from every commit.
- **Adding dependencies:** edit `pyproject.toml`, then `uv lock` / `uv sync`.
- **Version management is a human responsibility.** Coding agents can bump the
  last part of a version number (`0.0.X`) — every other bump (minor, major) is
  human initiated. Current version: `0.0.21`.
- **No agent as a contributor on GitHub.** See the commits bullet above: the
  `Co-Authored-By` agent trailer is what made GitHub list `mistral-vibe` as a
  contributor — it is banned from commits; humans only in the contributor
  list / authorship.

## 7. Next up — the coop GUI roadmap

Spec 12 (coop skeleton) landed: with `coop_enabled = true` the daemon serves
the TurboChickens GUI over HTTP in its own event loop — `/api/ping` +
`/api/status` as thin mirrors of the socket ops (transport seam:
`client.request_async` over the daemon socket, never the DB), bearer auth
when bound non-loopback (ephemeral token printed once at startup, sessionStorage
in the SPA, no cookies), the design-system shell (240px sidebar, wordmark + egg
dot, nav, login card) and the Dashboard (stat row + flock diagram, 2 s polling)
per `specs/00-design-system.md`; `tc coop start` runs the same thing locally.

Spec 13 (Jobs / Audit / Settings views) landed: the read-only socket ops
`jobs_list` / `audit_tail` / `settings_get` plus their `/api` mirrors
(`/api/jobs`, `/api/audit?limit=&event=`, `/api/settings`) and the SPA views
(Jobs table, Audit mono tail with event-type chips, Settings rows, each polling
every 2 s with daemon-down/empty states).

Spec 14 (Runs from the GUI) landed: `POST /api/runs` enqueues a manual run by
in-container spec path (400/422 on bad input, daemon re-validates), `GET
/api/runs?job=&status=` + `/api/runs/{id}` + `/api/runs/{id}/logs` +
`/api/results/{run_id}` mirror the socket ops over the transport seam (404 on
unknown resources, structured `daemon.up=false` when down), and the SPA Runs
view is the full table with status egg-dots, job/status filters, a run-by-path
form, and the Manifest / Logs / Result-or-Quarantine detail pane — quarantine
shows schema errors + raw output (never a valid result), expired artifacts show
an expired state. `runs_list` gained the `job`/`status` filters + `result_valid`;
`results_get` gained the quarantined `raw_output` and the `expired` report.

Spec 15 (upload + validate job specs from the browser) landed: `jobs_upload`
validates a base64-file bundle with the existing spec loader (field-named errors
like `tc jobs validate`, 10 MiB cap, safe paths only) and stores valid bundles
under `uploads/<job>-<spec_hash>/`; `POST /api/jobs/validate` mirrors it and
`POST /api/runs` also takes a bundle (store + enqueue in one call); the Jobs view
gained the upload form (file inputs, per-field errors, one Validate → "Run job"
button).

Spec 16 (schedules from the GUI) landed: `job_specs` persists the registered
`spec_path` (schema v4, in-place migration) and `jobs_list` serves it, so the
Schedules view's add form picks a registered spec from a dropdown;
`GET /api/schedule` + `POST /api/schedule` (by spec path or `{files}` bundle)
+ `DELETE /api/schedule/{job_id}` mirror `schedule_list`/`schedule_add`/
`schedule_rm` over the transport seam (422 with the inline error on
manual-trigger/invalid-cron/hash-mismatch refusals, 404 on unknown remove,
structured `daemon.up=false` when down); the SPA Schedules view is the table
(job, mono cron, live next_run, per-row Remove) plus the add form with inline
errors, polling every 2 s. A schedule added over HTTP fires a `trigger=cron`
run through the normal pipeline (verified e2e with a next-minute cron).

Spec 17 (stats + retention from the GUI) landed: `GET /api/stats` +
`GET /api/stats/{job_id}` mirror `stats_list`/`stats_show` over the transport
seam (404 on a job with no stats yet), `POST /api/retention/sweep` runs the
on-demand sweep and answers with the `result.expired` outcome list; the Stats
view renders per-job cards (n, mean/p50/p95 tokens in+out, total p95,
expected cost per run, with a per-job detail card), and the Dashboard gained a
retention card whose "Sweep now" button lists the expired runs inline (verified
e2e with backdated runs over HTTP).

Spec 18 (the coop Docker container) landed: `Dockerfile` on `python:3.12-slim`
shipping `docker/settings.toml` (data_dir=/data, coop on 0.0.0.0:8737, no fixed
token) with `VOLUME ["/data"]`, `EXPOSE 8737`, a loopback `/api/ping`
`HEALTHCHECK`, and `CMD ["tc", "daemon", "start"]`; plus `.dockerignore`,
`docker/compose.yaml`, `docs/coop-container.md` (build/run/compose/token/
reverse-proxy/manual smoke), and `tests/test_coop_container.py`. Verified on a
real Docker daemon: image builds, container goes healthy, the ephemeral token
prints exactly once per start, the GUI + full upload→run→result→schedule flow
work over HTTP, and jobs/runs/results/schedules survive container replacement
on the same volume.

Spec 19 (auth & access management in the GUI) landed: new socket ops
`auth_providers` (each provider's mode + seat session expiry via
`SeatBroker.status()`, never token material), `auth_access` (the declared
access scope — per-run env allowlist, network policy `none`, and
tools/data/MCP/connectors placeholders for the future host-broker layer),
`auth_login_start` / `auth_login_poll` / `auth_logout` (the OAuth device flow
driven one request at a time through the same `SeatBroker` — `login()` is now
composed from the new `start_device_flow()` + `poll_device_flow()`, so the
CLI and the GUI share one code path; device codes stay in daemon memory,
sessions land 0600 under `<data_dir>/seats/`, audit rows `auth.login` /
`auth.logout` carry provider + mode only), mirrored over the transport seam
as `GET /api/auth/providers`, `GET /api/auth/access`, `POST /api/auth/login`,
`POST /api/auth/login/status`, `POST /api/auth/logout` (422 on refusals, 400
on malformed bodies, structured `daemon.up=false` when the socket is dead,
bearer-required when non-loopback via the existing middleware); `runs_list`
rows and the Runs view (new Auth column + detail header) surface the per-run
`auth_mode` from the spec-09 manifest block; the SPA gained the Auth view
(nav entry, provider table with mode badges + session expiry countdown,
device-code login card that polls `/api/auth/login/status`, logout actions,
and the read-only Access scope card) polling every 2 s — verified e2e over
HTTP with a hermetic fake OAuth server, including a seat run after a GUI
login and a subprocess `tc coop start` flow, with an explicit assertion that
no response body or audit row ever contains token or device-code material.

Spec 20 (lexicon adoption) landed: Headcount event names, Coop GUI copy, CLI
output/help, and error copy now speak farm per `docs/lexicon.md` (`egg.queued`,
`egg.hatched`, `clutch.stored`, `abattoir.attested`, `chicken.finished`,
`rooster.crowed`, `perch.claimed`, `coopd.started`, …; nav Chickens/Eggs/Rooster/
Feed/Headcount/Perch; `tc status` prints `coopd:` / `pecking order:`), while
socket ops, HTTP paths, CLI subcommands, DB schema, settings keys, and on-disk
file names stay byte-stable. `audit.py` carries the legacy→lexicon alias map
applied on read/display only (`tail()` normalizes, `?event=` accepts both old and
new names; raw rows on disk are never rewritten), and the Hatchery now emits
`egg.hatched` at provisioning start. `docs/lexicon.md` gained the Feed Ledger.

Spec 22 (lifecycle ops) landed: new socket ops `run_stop` and `run_rerun`.
`run_stop` on a queued egg pulls it from the pecking order → terminal
`cancelled` (never hatches — no harness events — but still gets a manifest and
the abattoir's (1,1,1) attestation, audit `egg.cancelled`); on a running
chicken the pipeline's stop event races the harness call → SIGTERM, then a hard
kill after `STOP_TERM_GRACE_SEC` (5s) → terminal `stopped`, output never
collected, audit `chicken.stopped`, same attested teardown as any terminal
state. `run_rerun` re-enqueues the same egg (from `job_specs.spec_path`) as a
fresh run with `trigger=rerun` and `rerun_of` lineage in the manifest and the
`egg.queued` row, through the normal queue/admission/pipeline. CLI parity:
`tc runs stop <id>` / `tc runs rerun <id>` (rc 0, clear errors for unknown or
already-terminal runs); `runs_list` rows carry `status_reason`, and the new
terminal states show in `tc runs list` / `runs_show` / `audit_tail`.
`TERMINAL_STATES` gains `cancelled`/`stopped`, so a missing attestation on a
stopped/cancelled chicken stays an alert condition. Lexicon first: Cancelled /
Stopped / Re-run + the `egg.cancelled` / `chicken.stopped` event names were
added to `docs/lexicon.md` before use. Verified e2e through a thread daemon and
a subprocess daemon with the fake vibe binary (incl. a SIGTERM-trapping fake
forcing the hard-kill path); full suite 298 passed.

Spec 24 (Feed Budget cost cap) landed: `resources.max_cost_usd` (USD, `float |
None`, off by default, positive-only with a field-named error on zero/negative;
`extra="forbid"` and the no-`max_tokens` invariant intact, and `spec_hash`
excludes the None default so existing egg hashes are unchanged). Enforcement is
finalize-only via the pure seam `cost_cap_exceeded(usage, cap)` in `runner.py`:
when a chicken's measured `usage.cost_usd` is strictly over the egg's cap, the
run fails at finalize with `budget.outcome = "cost_cap"` in the same manifest
budget block as timeout/context-cap (the block gains `max_cost_usd` +
`cost_exceeded`, the `resources` mirror gains `max_cost_usd`) — no clutch
stored, cost/token telemetry still recorded and rolled into the feed ledger,
teardown attested (1,1,1) as usual. At the cap exactly the run succeeds; with no
cap set, behavior is unchanged. Seat runs (spec 09, `cost_usd = 0.0`) can never
trip a positive cap. Operator-facing copy speaks farm ("feed budget exceeded at
finalize: measured cost_usd=… > max_cost_usd=…"); the machine value `cost_cap`
joins the budget outcomes. Verified e2e against a hermetic loopback fake priced
provider and the fake vibe binary (over / at / no cap, plus a seat run with a
tiny cap), full suite 307 passed.

Spec 25 (chaining) landed: `[input].attachments` entries may now be
`nestbox://<run_id>/output.json` URIs. The shape is validated at spec load by
the new `chaining.py` seam (`parse_nestbox_uri` — anything URI-shaped that is
not exactly that form is a field-named `input.attachments` error; `extra =
"forbid"` intact), and resolution happens at provisioning in the Hatchery via
`resolve_nestbox_clutch` — valid clutches only: a quarantined, composted
(retention-swept), or missing clutch raises `NestboxError` and the run fails at
provisioning before `running` (no harness events, attested teardown (1,1,1), no
results row, workspace gone). Resolved clutch bytes are written into the
workspace as `nestbox-<run_id>-output.json` (the API adapter injects them like
any attachment) and count toward `max_context_mb`, so an over-cap chain fails
with `budget.outcome = "context_cap"` exactly like spec 07. Lineage is recorded
in the manifest (`chaining`: attachment URI + source run id + artifact path +
size_bytes), the Headcount (`clutch.consumed` with `source_run_id`), and the run
log ("chained clutch from chicken …"). `jobs_upload` bundles no longer need
`nestbox://` attachments shipped in them. Lexicon gained **Chaining** first.
Verified e2e with the fake vibe binary (happy path + quarantined/missing/
composted refusals + over-cap chain + upload-and-run), full suite 340 passed.

Spec 23 (Free-Range chickens) landed: `resources.free_range = true` removes
the hard wall-clock kill — `timeout_sec` becomes optional in the egg and the
pairing is validated at spec load (`free_range = false`, the default, still
requires `timeout_sec > 0`; field-named `resources.timeout_sec` error;
`extra="forbid"` intact; `spec_hash` keeps pre-23 hashes stable). Adapters take
`timeout_sec: int | None` (`None` = no hard kill), and the Hatchery enforces
`hard_timeout = None` for free-range chickens; the manifest `resources` and
`budget` blocks carry `free_range`. A Free-Range chicken dies only via
`run_stop` (spec 22) or daemon shutdown: on shutdown the daemon asks every
active free-range chicken to stop through the spec-22 SIGTERM/hard-kill path
before the bounded drain, so it ends `stopped` with the abattoir's (1,1,1)
attestation and `chicken.stopped`/`abattoir.attested` Headcount rows before
exit (a missing attestation stays an alert condition). The baseline
invariants-1/2 decision-log amendment is recorded in
`specs/23-free-range-chickens.md` and §5 above. Verified e2e with the fake vibe
binary (no-kill past any TTL while a fenced control times out, attested
`run_stop`, attested shutdown teardown in a subprocess daemon), full suite
353 passed.

Spec 26 (the Farmhand) landed: `farmhand.py` serves an MCP server (official
`mcp` SDK, new dependency; version 0.0.18) whose 17 tools are thin wrappers
over the daemon's existing socket ops via `client.request_async` — tool
results are the socket responses verbatim, and a dead daemon answers
`{ok:false, daemon.up:false}`. Transports: stdio via
`tc farmhand serve --transport stdio|https|both` (same-machine agents, gated by
the 0600 daemon socket; informational output on stderr only), and HTTPS as
streamable-HTTP at `farmhand_https_path` (default `/mcp`) mounted into the
coop's existing aiohttp listener through a small ASGI bridge (the SDK's
Starlette app with its lifespan run) behind the same bearer middleware as
`/api/*` — 401 without the bearer when non-loopback or a token is configured,
loopback-open otherwise, ephemeral token printed exactly once. `"both"` serves
stdio and HTTPS simultaneously in one process. Settings gained
`farmhand_transport` (`stdio|https|both`, default `both`) and
`farmhand_https_path`, `extra="forbid"` intact, both in `public_dict()`. The
MCP scope is narrower than the GUI bearer: no `auth_*` mutations, no
`retention_sweep`, no `shutdown`, settings read-only — and no tool payload
ever contains token material (asserted over both transports and against the
audit log). Also hardened: coopd now binds its unix socket only after the coop
(and the Farmhand endpoint) is up, and the aiohttp runners run with
`shutdown_timeout=15` so a hung MCP SSE stream can never wedge shutdown.
Verified e2e with the SDK's own MCP client over stdio against
`tc farmhand serve` and over HTTPS against a `tc coop start` subprocess
(bearer 401/200, loopback-open, ephemeral-token mode, MCP==socket payload
parity for every tool, enqueue→succeeded→results_get, run_stop/run_rerun,
jobs_validate, schedules), full suite 362 passed.

Spec 27 (ChickenSubs) landed: a chicken whose egg sets `harness.spawn_subs = true` gets the Farmhand
attached to its vibe-cli runtime over stdio — an `[mcp_servers.farmhand]` entry in the run's vibe config
(project-local `.vibe/config.toml` in the workspace) spawning the farmhand serve command, with a
token-free generated settings file wired through `TURBOCHICKENS_SETTINGS` so the spawned process finds
the daemon's socket. Sub-chickens are ordinary eggs through the same socket/queue/admission (weight
unchanged), with `parent_run_id` lineage on the new `runs.parent_run_id` column (schema v5, in-place
migration), in the manifest, in the `egg.queued` Headcount row, and in `runs_show`/`runs_list` rows.
Depth is capped at 1: enqueue refuses a `spawn_subs = true` egg when the enqueuing run is itself a sub
(field-named `harness.spawn_subs` error), and `spawn_subs` is refused for non-vibe-cli adapters.
A Farmhand running inside a chicken reads `TURBOCHICKENS_RUN_ID`/`TURBOCHICKENS_PARENT_RUN_ID` from the
per-run env: `enqueue` passes `parent_run_id`, `run_stop` passes `caller_run_id` (ancestor targets
refused), and a sub-chicken's Farmhand serves only enqueue + read tools + the guarded `run_stop`. Lexicon
gained **ChickenSub**; version 0.0.19. Verified e2e with a fake vibe binary that itself drives the
attached Farmhand over stdio (SDK MCP client) through the `tc` CLI against a subprocess daemon
(lineage recorded everywhere, depth-1 and ancestor refusals, 11-tool sub scope, secrecy with a
configured token), full suite 374 passed.

Spec 28 (connectors registry) landed: new module `connectors.py` — a `connectors.toml` registry resolved
exactly like `providers.toml` (`settings.connectors_path` > `$TURBOCHICKENS_CONNECTORS` >
`<data_dir>/connectors.toml`, no built-in gates) with `[connectors.NAME]` entries: `transport = "stdio"`
(command/args/env) or `transport = "https"` (url + optional header env refs), strict schema with
field-named errors and `extra="forbid"`. New socket ops `connectors_list` / `connectors_add` /
`connectors_rm` (CRUD over the registry file — atomic rewrite always 0600, replace-on-re-add) plus
`tc connectors list/add/rm`, audit-logged as `gate.registered` / `gate.removed` / `gate.rejected`
(name + transport + field names only). `connectors_list` serves non-secret summaries only — stdio `env`
as key names, https `headers` as the refs themselves — and the spec-19 `auth_access` op now fills its
`connectors` scope entry with the registered gate names (tools/data/mcp_servers stay placeholders).
`HarnessSection` gained `connectors: list[str] = []` (`extra="forbid"` intact; the empty list stays out of
`spec_hash`, so the shipped example is still `sha256:18efbd8c…80b959`). Enqueue refuses unknown gate
names with a field-named `harness.connectors` error (nothing queued) and refuses the list for
non-vibe-cli adapters. The vibe-cli adapter injects the opted-in gates into the run's vibe config
(`[mcp_servers.NAME]` entries in `<workspace>/.vibe/config.toml`, merged with the spec-27 Farmhand entry
when both are present); `$VAR`/`${VAR}` env/header refs are resolved against the daemon environment only
into the ephemeral (attested) workspace config — never into any response, audit row, run log, or manifest
(the manifest carries names only) — and an unresolvable ref fails the run closed at provisioning
(attested (1,1,1), no results row). Lexicon gained **Gate**; `examples/connectors.toml` is the reference
registry; version 0.0.20. Verified e2e with the fake vibe binary (both transports injected and asserted,
merged farmhand+gate config, no-key → no config, fail-closed ref) and a subprocess-daemon CLI smoke,
full suite 391 passed.

Spec 29 (the Beak) landed: new socket op `workspace_peek` (behind it, the new pure seam `beak.py`) gives a
read-only live view into an in-flight chicken's workspace — the file tree (capped at 1000 entries, paths +
sizes only, workspace-relative so no host path leaves the daemon) or one file's content (capped at 64 KiB
served, `truncated` flag, utf-8 else base64). Paths are locked inside that run's workspace (`..`/absolute/
backslash refused; symlink escapes resolved and refused), and the chicken's private harness config (`.vibe/`,
`.tc-farmhand-settings.toml` — where spec-27/28 resolved gate/env secrets live) is refused by name. The Beak
is honest about ephemerality: it is open only while the workspace exists — terminal runs (spec 22's
`cancelled`/`stopped`, or any finished chicken) get a clear closed-beak `workspace_gone` error because the
Abattoir's attested teardown already removed the workspace, queued eggs report "no workspace yet". Over the
transport seam it is mirrored as `GET /api/runs/{run_id}/workspace(?path=…)` — identical payload, 404 unknown
run, 200 with the closed-beak payload for terminal runs, `200 {ok:false, daemon.up:false}` on a dead socket,
bearer 401 when required; the web layer never touches the workspace itself. The coop run detail gained a Beak
tab (tree with flash-on-change, clickable file peek with copy, 40-line live log tail from the existing
`runs_logs` polling — all on the 2 s detail poll, with empty states for terminal runs and daemon-down and
private entries shown disabled), built to `specs/00-design-system.md`; lexicon gained **Beak**; version 0.0.21.
Verified e2e with the fake vibe binary writing files into its workspace (live tree + peek over socket and HTTP
with parity, traversal/private/size-cap refusals, secrecy with a configured coop token, closed beak after
`run_stop`, SPA markup), full suite 402 passed.

The MVP spec pipeline (01–10) and the GUI spec 11 slices (12–20) are
complete; what remains is the post-MVP roadmap in baseline §13.

**Next items (the next session starts here):**
- Local test of the container and the UI — build the coop image and run it
  locally, then click through the GUI (including the new Auth view); the
  spec-18 container smoke ran on a real Docker daemon in-session, this is the
  repeatable local pass.
- CI flow — set up GitHub Actions CI (run the test suite on push/PR) and wire
  it into the repo.

**Deferred (backlog notes):**
- *Async seat broker:* `SeatBroker` does its HTTP synchronously (httpx.Client)
  inside the sync `prepare`, so a token refresh/login briefly blocks the
  daemon loop (bounded by a 15s timeout, rare). Revisit — e.g. move refresh
  into an async pre-flight step in the pipeline — if refresh latency ever
  matters. Deliberately deferred 2026-11-12.
