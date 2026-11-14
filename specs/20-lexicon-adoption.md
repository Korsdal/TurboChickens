# 20: Lexicon adoption — every surface speaks farm

**What to build:** Roll `docs/lexicon.md` out across everything an operator reads or watches. The lexicon's rule 5 is the mandate: *docs, audit events, error messages, and UI copy all use lexicon terms* (`egg.hatched`, `clutch.stored`, `abattoir.attested`). Today the code speaks platform ("run", "queue", "teardown", `run.queued`); the farm vocabulary (Egg, Pecking Order, Hatchery, Chicken, Clutch, Nest Box, Abattoir, Compost, Rooster, Perch, Headcount, Coop, Coopd) exists only in `docs/lexicon.md`. Goal: one register everywhere a human looks, with zero changes to machine interfaces — the socket ops, HTTP paths, CLI subcommands, DB schema, settings keys, and on-disk file names stay byte-stable, because existing volumes, append-only Headcount logs, and scripts depend on them. The lexicon's own "Maps to" column already anchors each term to the module that owns it (`validate.py` = Candling, `runner.py` = Hatchery, `daemon.py` = Coopd, …), so modules keep their names; the *vocabulary* around them changes.

**Blocked by:** none (lexicon.md is landed; builds on specs 01–19)

**Status:** done (2026-11-14) — 291/291 tests green, e2e smoke verified (see STATUS.md §3)

## Scope: what changes vs. what stays

**Changes (the four registers of rule 5, plus docs):**
1. Audit event names (the Headcount's `event` values)
2. Coop UI copy (`coop_static/index.html`, `app.js`, `styles.css` — labels, headers, empty states, buttons, diagram labels; never `data-*` hooks, ids, `/api/*` paths, or JSON field names)
3. CLI human-readable output and help text (`cli/main.py` print strings)
4. Error message copy (same failure semantics, farm register)
5. Docs: `STATUS.md`, `docs/coop-container.md`, specs 20+, docstrings/comments in touched modules

**Stays (machine interfaces — do not rename in this spec):**
- Python modules and files — the lexicon Maps-to anchors them (`validate.py`, `runner.py`, `providers.py`, `storage.py`, `retention.py`, `scheduler.py`, `seat.py`, `audit.py`, `daemon.py`, `coop.py`, `coop_static/`)
- Socket op names (`enqueue`, `runs_list`, `results_get`, `schedule_*`, `auth_*`, `audit_tail`, …) and HTTP paths (`/api/runs`, …) and JSON field names (`run_id`, `job_id`, `status`, the `event` key itself)
- CLI subcommand names (`tc run`, `tc runs`, `tc results`, `tc jobs`, `tc schedule`, `tc auth`, `tc coop`, `tc daemon`) — scripts and muscle memory; a later spec may add lexicon aliases
- DB tables/columns, settings keys (`coop_auth_token`, `results_retention_months`, …), env vars, on-disk paths (`audit.jsonl`, `daemon.sock`, `results/`, `quarantine/`, `uploads/`, `seats/`, `workspaces/`), package name, the `tc` binary

## Mapping table — Headcount (audit events)

The Headcount is append-only and kept forever, so **historical rows keep their old event names — never rewrite the log.** `audit.py` gains a legacy→lexicon alias map applied on read/display only: `tail()` normalizes the event for filtering/display, `?event=` accepts both old and new names, and the raw rows on disk are untouched.

| Current event | Lexicon event | Note |
|---|---|---|
| `run.queued` | `egg.queued` | Egg enters the Pecking Order |
| — (new) | `egg.hatched` | emitted at provisioning start in `runner.py` — the Hatchery turns the Egg into a Chicken (the lexicon's example event; today hatching has no audit row, only `run_events`) |
| `run.finished` | `chicken.finished` | terminal state reached |
| `result.stored` | `clutch.stored` | Clutch lands in the Nest Box |
| `result.quarantined` | `clutch.quarantined` | failed output held for inspection |
| `result.expired` | `clutch.composted` | Compost = retention; old Clutches rot away |
| `teardown.attested` | `abattoir.attested` | attested teardown only (lexicon rule 4) |
| `schedule.added` | `rooster.scheduled` | the Rooster keeps time |
| `schedule.removed` | `rooster.unscheduled` | |
| `schedule.fired` | `rooster.crowed` | the Rooster crows — wakes an Egg into the Pecking Order |
| `schedule.fire_failed` | `rooster.missed` | |
| `job.uploaded` | `egg.candled` | passed Candling, stored |
| `job.upload_rejected` | `egg.rejected` | failed Candling — quarantined Egg |
| `auth.login` | `perch.claimed` | seat claimed on the Perch |
| `auth.logout` | `perch.released` | |
| `auth.resolved` | `perch.resolved` | per-run seat resolution |
| `daemon.started` / `daemon.stopped` | `coopd.started` / `coopd.stopped` | Coopd is the supervisor daemon |
| `coop.started` / `coop.stopped` | unchanged | already lexicon |

## Mapping table — Coop UI copy

State dots + short state labels (`queued`/`running`/`succeeded`/`failed`/`quarantined`) are settled in spec 00 and stay. Nouns and verbs around them change:

| Current copy | Lexicon copy |
|---|---|
| Nav: Runs / Jobs / Schedules / Stats / Audit / Auth | Chickens / Eggs / Rooster / Feed / Headcount / Perch (Dashboard, Settings stay) |
| Dashboard stats: Queue depth / Schedules / Retention | Pecking order / Rooster / Compost ("In flight", "Alerts", "Uptime" stay) |
| Retention card "Sweep now", "N expired" | "Turn the compost", "N composted" |
| Flock diagram labels: daemon / queue / results | coopd / pecking order / nest box ("in flight" stays) |
| Runs table: Run / Job columns; "Run job" button; "No runs yet — run a job above, or with `tc run`" | Chicken / Egg; "Queue egg"; "No chickens yet — queue an egg above, or with `tc run`" |
| Run detail tabs: Result-or-Quarantine; "quarantined raw output" | Clutch-or-Quarantine; "quarantined clutch" |
| Jobs view: "Job spec (job.toml)"; "No registered jobs yet…"; primary "Validate" → "Run job" | "Egg (job.toml)"; "No eggs yet — upload a bundle above, or register one with `tc run`"; "Validate" → "Queue egg" |
| Schedules view: "Add schedule"; "Next run"; "No schedules yet…" | "Add a crow"; "Next crow"; "The rooster has no crows yet…" (the cron expression itself stays mono machine text) |
| Stats view: "expected cost / run"; "No usage stats yet — run a job to completion…" | "expected feed / chicken"; "No feed recorded yet — let a chicken finish…" |
| Auth view: "Seat session" | "Perch seat" (provider `mode` values `api-key`/`seat` are machine and stay) |
| Settings view keys (`audit_log_path`, `results_dir`, `quarantine_dir`, …) | keys stay verbatim; add a lexicon gloss where shown — the Headcount / the Nest Box / candling quarantine |
| Daemon-down banner | "coopd unreachable" wording |

## Mapping table — CLI copy (subcommand names unchanged)

| Current string | Lexicon copy |
|---|---|
| `tc status`: `daemon: up (pid=…)`, `queue_depth: N`, retention line | `coopd: up (pid=…)`, `pecking order: N`, retention line keeps its keys with a gloss (headcount forever / clutches rot after N months) |
| ALERT: "terminal run without a teardown attestation" | "terminal chicken without an abattoir attestation" |
| `tc run`: "queued run {id} job '{job}' status=queued weight=… queue_depth=…" | "egg queued {id} (job '{job}') — pecking order depth {n}, weight {w}" |
| `tc results get`: "no result stored", "expired" | "no clutch in the nest box", "composted" ("quarantined" state stays) |
| `tc runs list` headers RUN / JOB | CHICKEN / EGG |
| Help text: "Submit a manual run of a job to the daemon queue…" | register wording, e.g. "Queue an egg (job spec) — the Hatchery hatches it into a chicken" |

## Terms to add to `docs/lexicon.md` first

Per the lexicon's own rule ("if a term is missing, add it here first, then use it"), landing this spec adds **Feed Ledger** — the per-job rollup of what the flock was actually fed (tokens in/out, expected cost per chicken), mapping to `stats.py` and the Stats view (nav label "Feed"). No other new terms; everything else maps to existing entries.

## Acceptance

- [x] Every audit emission site uses the lexicon event names per the table (`runner.py`, `daemon.py`, `scheduler.py`, `retention.py`, `coop.py`), and `egg.hatched` is emitted when the Hatchery starts provisioning
- [x] `audit.py` carries the legacy→lexicon alias map, applied on read/display only: `audit_tail`/`?event=` match old and new names, raw rows never rewritten (test: a log written with legacy names still filters and displays correctly, bytes on disk unchanged)
- [x] Coop SPA copy follows the UI table — nav, dashboard stats, cards, forms, empty states, flock diagram labels; ids, `data-*` hooks, `/api/*` paths, and JSON field names untouched; spec-00 state dots/labels untouched
- [x] CLI output and help text follow the CLI table; subcommands, flags, socket ops, HTTP paths, JSON fields unchanged
- [x] Error message copy uses the register with identical failure semantics (e.g. output-validation failure → clutch/candling wording; `no daemon at …` → coopd wording); no secret material anywhere, as today
- [x] `docs/lexicon.md` gains Feed Ledger; `STATUS.md` §2 lists `docs/lexicon.md` as a governing doc; `docs/coop-container.md` gets a register pass; specs 20+ and touched docstrings are written in the register; new code follows lexicon rule 1 (concrete nouns, no prefixes/suffixes)
- [x] Tests updated (legacy event names asserted in 10 test files) plus new coverage: legacy-log aliasing, a grep guard that no legacy event string is emitted anywhere in `src/` outside the alias map, SPA markup shows the lexicon labels, `tc status` prints `coopd:` / `pecking order:`; full suite green (currently 286 tests)
- [x] e2e smoke: one run → Headcount shows `egg.queued` → `egg.hatched` → `clutch.stored` → `abattoir.attested` → `chicken.finished`; GUI Headcount chips show lexicon labels for both new and legacy rows

## Testing decisions

- Assert only external, observable behavior: audit.jsonl rows, HTTP bodies and served markup, CLI stdout — never DOM internals or handler internals, per the spec-11 testing rules
- Prior art: `test_coop_views.py` (served SPA markup), `test_run_e2e.py` / `test_daemon.py` (event assertions over socket + subprocess), `test_coop_schedule.py` (rooster events over HTTP)
- The legacy-alias test is the critical one: append rows with legacy names to a real `audit.jsonl`, assert `tail()` filtering by the new name and the raw file bytes

## Out of scope

- Renaming modules, files, DB tables/columns, socket ops, HTTP paths, CLI subcommands, settings keys, env vars, or on-disk paths (listed above as "stays")
- Rewriting historical Headcount rows (aliased on read, never rewritten)
- Wholesale rewrite of STATUS.md's existing prose — boundary updates only, register going forward
- The Farmhand (MCP), the Fox (security), the Beak (in-GUI terminal) — their own future specs; this spec only makes sure they are *named* per the lexicon when they land
- State dot/label names settled in spec 00
