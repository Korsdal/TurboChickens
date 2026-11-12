# STATUS.md — TurboChickens (internal anchor for agentic work)

> Single source of truth for "where are we and what must not be broken".
> Update this file at every spec boundary. Future agents: read this first,
> then the governing docs in §2, before touching code.

**Last updated:** 2026-11-12, after spec 15 landed (250/250 tests green, e2e smoke verified).

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
| `specs/01-*.md` … `specs/10-*.md` | Implementation tickets. Each has `Status: ready-for-agent`, a "Blocked by" line, and a checkbox acceptance list. |

## 3. Current state — verified 2026-11-12

**Spec 01 through spec 10 and specs 12–15 are done** (commits `f8b8061`, the spec-02 commit, the spec-03 commit, the spec-04 commit, the spec-05 commit, the spec-06 commit, the spec-07 commit, the spec-08 commit, the spec-09 commit, the spec-10 commit, the spec-12 commit, the spec-13 commit, the spec-14 commit, and the spec-15 commit). Spec 11 is the parent GUI spec; its first four slices (spec 12, the skeleton; spec 13, Jobs/Audit/Settings views; spec 14, Runs; spec 15, upload/validate) are landed, the rest of its stories land via specs 16–18.

**Spec 19 (auth & access management UI) is backlogged** — `specs/19-auth-access-management-ui.md`, status ready-for-agent, not started. Note spec 09 landed seat auth: `mistral-api` runs can now run with no API key via a seat provider (`auth = "seat"` in providers.toml + `tc auth login`), and vibe-cli picks up a browser-auth provider from `~/.vibe/config.toml` via HOME passthrough. Spec 10 landed model discovery: `tc models list` queries live provider catalogs (and the vibe binary's own model list), so a job's `model` field can be chosen against reality.
Verified this session:

```
.venv/bin/python -m pytest -q          → 188 passed
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
│                          # SOCKET_STREAM_LIMIT (one JSON-lines request may carry a whole bundle)
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
│                          # spec 15: POST /api/jobs/validate (bundle {files: {path: base64}} → daemon
│                          # jobs_upload; 200 valid+stored / 422 field-named errors / 413 cap / 400 malformed)
│                          # and POST /api/runs also accepts {files: …} (store+validate+enqueue by the stored
│                          # path); client_max_size backstop from UPLOAD_MAX_HTTP_BODY
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
│                          # Validate→"Run job" button that re-POSTs the bundle to /api/runs) — built to
│                          # specs/00-design-system.md
├── storage.py             # init_db(): full MVP SQLite schema + `schedules` table (spec 06) +
│                          # `job_usage_stats` percentile columns (spec 08, schema_version=3, with
│                          # in-place migration of v2 DBs) + append-only triggers + schema_meta
├── audit.py               # AuditLog: append-only JSONL (open "a" only, fsync per row) — never TTL-deleted;
│                          # spec 13: tail(limit, event) — last N rows newest first, defensive read
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
│                          # bearer_token() (spec 10) hands the session token to `tc models list`
├── adapter.py             # HarnessAdapter Protocol (prepare/execute/teardown) + CredentialProvider
│                          # extension point + VibeCLIAdapter (headless subprocess pinned to the
│                          # workspace, sanitized streamed logs) + MistralAPIAdapter (spec 05:
│                          # OpenAI-compatible HTTP via httpx, JSON mode, exp backoff on 429/5xx)
│                          # + adapter weights (API 0.25 / CLI 1.0); PreparedRun carries non-secret
│                          # auth telemetry (spec 09); seat runs report cost_usd=0.0 billed_via=seat;
│                          # module-level resolve_vibe_bin/vibe_version helpers (spec 10 probe)
├── providers.py           # spec 05: providers.toml registry — provider/model presets, base_url,
│                          # credential env var, context_window, per-token pricing; resolution:
│                          # settings.providers_path > $TURBOCHICKENS_PROVIDERS > <data_dir>/providers.toml
│                          # > built-in mistral defaults (overlaid per provider). Spec 09: ProviderProfile
│                          # gains auth="api-key"|"seat" + oauth_* endpoints (required for seat)
├── runner.py              # RunPipeline: lifecycle queued→…→succeeded/failed, fresh workspace +
│                          # per-run env, manifest (+ spec-07 `budget` outcome block, spec-09 `auth`
│                          # telemetry block), run_events, spec-04 result store / quarantine at
│                          # `collecting`, spec-07 context cap at `provisioning`, teardown attestation
│                          # + audit rows (incl. spec-09 auth.resolved + auth_mode), spec-08 per-job
│                          # usage-stats rollup at finalize; spec 09: builds the SeatBroker and hands
│                          # it to make_adapter as the CredentialProvider
├── scheduler.py           # spec 06: CronScheduler over APScheduler AsyncIOScheduler; standard
│                          # POSIX cron semantics (aps_trigger resolves the day-of-week field via
│                          # croniter because APScheduler numbers 0=Monday); schedules re-registered
│                          # from the DB on daemon start; misfires coalesce (grace 300s)
├── daemon.py              # Daemon: asyncio process, Unix socket, JSON-lines ops
│                          # ping/status/shutdown/enqueue/runs_list/runs_show/runs_logs/results_get/
│                          # schedule_add/schedule_list/schedule_rm (spec 06)/stats_list/stats_show/
│                          # retention_sweep (spec 08)/jobs_list/audit_tail/settings_get (spec 13,
│                          # read-only GUI ops; settings_get never includes the coop token);
│                          # spec 14: runs_list takes optional job=/status= filters + result_valid per row;
│                          # results_get returns quarantined raw_output + schema_errors (never valid) and
│                          # reports expired for artifacts the retention sweep removed (valid or quarantined);
│                          # spec 15: jobs_upload op — validate a base64-file bundle with the existing spec
│                          # loader (field-named errors like `tc jobs validate`, staging paths rewritten to
│                          # <bundle>), cap UPLOAD_MAX_BYTES decoded, safe bundle-relative paths only, and
│                          # store valid bundles under uploads/<job>-<spec_hash>/ (audit job.uploaded /
│                          # job.upload_rejected, never file contents); socket streams raised to
│                          # SOCKET_STREAM_LIMIT so one request line can carry a bundle;
│                          # admission-control scheduler (over-budget
│                          # backlogs, never rejects); a firing cron submits enqueue trigger=cron over
│                          # the daemon's own socket; retention sweep at startup + hourly (spec 08);
│                          # results_get reports expired artifacts; status carries a retention block
│                          # + a coop block (spec 12: enabled/url/auth_required, never the token);
│                          # spec 12: with coop_enabled, serve() also starts the coop HTTP server in
│                          # the same loop and shutdown tears it down (audit coop.started/stopped)
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
│   └── loader.py          # load_spec(), SpecValidationError (field-named issues), spec_hash()
└── cli/main.py            # Typer app `tc`: run [--wait] | runs list/show/logs | results get |
                           # jobs validate (spec 10: model checks) | daemon start/stop | status |
                           # schedule add/list/rm | stats list/show (spec 08) |
                           # auth login/status/logout (spec 09, local) | models list (spec 10, local) |
                           # coop start (spec 12: daemon with the GUI forced on; status prints coop:)
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
└── test_coop_upload.py    # spec 15: jobs_upload socket op standalone (valid bundle stored under
                           # uploads/<job>-<hash>/ + enqueueable by that path; invalid → field-named errors,
                           # nothing stored/registered; cap, unsafe paths, base64, shape), POST
                           # /api/jobs/validate + POST /api/runs {files} over HTTP (200/422/413/400, HTTP ==
                           # socket parity, auth 401s, dead-socket daemon-down payloads), served SPA upload
                           # form + no host paths in assets, and a subprocess e2e creating + running a
                           # brand-new job entirely over HTTP with the fake vibe binary
├── test_api_adapter.py    # spec 05: providers seam, MistralAPIAdapter contract seam (MockTransport:
                           # JSON-mode shape, exp backoff on 429/5xx, no retry on 4xx, Retry-After,
                           # timeout, missing credential), e2e via daemon thread + subprocess against
                           # a hermetic loopback fake provider (result store, quarantine, weight 0.25)
examples/weekly-incidents-digest/   # baseline-format example job (job.toml, prompt, schema, csv; mistral-api)
examples/hello-vibe-cli/            # manual vibe-cli example job (job.toml, prompt, schema)
examples/providers.toml             # spec 05 reference provider registry (mistral presets + pricing,
                           # commented other-provider example) — copy to ~/.turbochickens/providers.toml
pyproject.toml             # uv/hatchling, Python >=3.12, version 0.0.11, deps: pydantic, typer, rich, croniter,
                           # jsonschema (+ rfc3339-validator for format checks), httpx, apscheduler (spec 06),
                           # aiohttp (spec 12, coop web server); dev: pytest
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

| 13 | Jobs / Audit / Settings views (read-only socket ops) | **done** (2026-11-12) | 12 |
| 14 | Runs from the GUI (trigger, watch, results/quarantine) | **done** (2026-11-12) | 12 |
| 15 | Upload + validate job specs from the browser | **done** (2026-11-12) | 14 |
| 16 | Schedules from the GUI | ready-for-agent | 12, 13 |
| 17 | Stats + retention from the GUI | ready-for-agent | 12 |
| 18 | coop Docker container | ready-for-agent | 12 |
| 19 | Auth & access management UI (seat sessions, provider auth modes, access scope for tools/data/MCP/connectors) | ready-for-agent (backlog) | 09, 12 |

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
  <vibe@mistral.ai>`. **One commit per ticket/feature/slice:** each spec ticket (or
  feature slice) lands as its own commit — never bundle two tickets into one commit,
  and never land a ticket with another ticket's unfinished work mixed in; when two
  slices touch the same file, split the diff by hunk (stage per-slice changes
  explicitly) so each commit is coherent and green on its own.
- **Adding dependencies:** edit `pyproject.toml`, then `uv lock` / `uv sync`.
- **Version management is a human responsibility.** Coding agents can bump the
  last part of a version number (`0.0.X`) — every other bump (minor, major) is
  human initiated. Current version: `0.0.11`.

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

Next backlog item is **spec 16** (schedules from the GUI), then 17–18, with
**spec 19** (auth & access management UI) queued behind 12. The
MVP spec pipeline (01–10) is complete; what remains beyond the GUI is the
post-MVP roadmap in baseline §13.

**Deferred (backlog notes):**
- *Async seat broker:* `SeatBroker` does its HTTP synchronously (httpx.Client)
  inside the sync `prepare`, so a token refresh/login briefly blocks the
  daemon loop (bounded by a 15s timeout, rare). Revisit — e.g. move refresh
  into an async pre-flight step in the pipeline — if refresh latency ever
  matters. Deliberately deferred 2026-11-12.
