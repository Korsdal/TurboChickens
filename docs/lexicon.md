# Lexicon — TurboChickens Naming Convention

> One source of truth for what things on this farm are called. Future agents: match this vocabulary in code, specs, docs, audit events, and UI copy. If a term is missing, add it here first, then use it.

## The Story

A **Egg** (job spec) is **Candled** (validated) and waits in the **Pecking Order** (queue). The **Hatchery** hatches it into a **Chicken** (a running agent) — optionally **Free-Range** (no max TTL) — fed by the **Feeders** (model providers) within its **Feed Budget**. When the Chicken finishes, it lays a **Clutch** (its output artifacts) in the **Nest Box** (results store). Then the **Abattoir** performs the attested teardown of the Chicken, and old Clutches eventually rot away in the **Compost** (retention). The **Rooster** keeps time (schedules), the **Fox** watches the fence (security), the **Perch** is where seats are claimed (auth), and the **Headcount** (audit log) records everything. The **Coop** is the management interface, run by **Coopd**; outsiders ask the **Farmhand** (MCP) for chores, and registered **Gates** are the openings in the fence to outside tool services.

The loop is deliberate: eggs make chickens, chickens make eggs. Jobs beget results, results feed future jobs.

## Core Lifecycle

| Term | Meaning | Maps to |
|---|---|---|
| **Egg** | A job spec — dormant, awaiting hatch | job spec / `job.toml` |
| **Candling** | Validation (and quarantine) of an Egg before incubation | `validate.py` |
| **Pecking Order** | The queue of Eggs waiting to hatch | queue |
| **Hatchery** | Agent provisioning — hatches an Egg into a Chicken | provisioning / `runner.py` |
| **Chicken** | A running agent instance | run / agent process |
| **Free-Range** | "No max TTL" mode in the Hatchery — a Chicken with no fence | max-TTL toggle |
| **Feeders** | Model providers — who feeds the Chickens | `providers.py` |
| **Feed Budget** | Per-run budgets: timeout, context cap, cost | run budgets / `budget.outcome` |
| **Feed Ledger** | The per-egg rollup of what the flock was actually fed — tokens in/out, expected feed per Chicken | `stats.py`, the Feed view |
| **Clutch** | A run's output artifacts — the eggs a Chicken lays; size-capped — an over-cap Clutch is stored truncated with a `truncated` marker in the manifest budget, never unbounded in memory, DB, or disk | run results / artifacts |
| **Nest Box** | Results store — where Clutches are found | `storage.py` (results) |
| **Abattoir** | De-provisioning with attested, verifiable teardown | teardown / destruction |
| **Compost** | Retention — old Clutches rot away; audit records stay forever | `retention.py` |
| **Cancelled** | An Egg pulled from the Pecking Order before it hatches — terminal, never hatches, still abattoir-attested | `cancelled` run state / `run_stop` on a queued egg / `egg.cancelled` |
| **Recovered** | A stale row left behind by a dead Coopd, re-resolved at startup — a queued Egg becomes `cancelled` with attestation exactly like `run_stop` (at shutdown the still-queued Eggs are cancelled the same way), and any non-terminal row the live Coopd does not own shows up in the status alerts | startup stale-row recovery / shutdown queue drain / Headcount `coopd.recovered` |
| **Stopped** | A Chicken ended deliberately by the operator — SIGTERM, then hard kill, with the Abattoir's attested teardown | `stopped` run state / `run_stop` on a running chicken / `chicken.stopped` |
| **Re-run** | Hatching the same Egg again as a fresh Chicken, new run ID, lineage recorded | `run_rerun` / manifest `rerun_of` |
| **Chaining** | An Egg consuming an earlier Chicken's Clutch via a `nestbox://<run_id>/output.json` attachment — resolved from the Nest Box at provisioning, valid clutches only | `chaining.py`, manifest `chaining`, Headcount `clutch.consumed` |
| **ChickenSub** | A sub-chicken — a Chicken spawned by another Chicken through its attached Farmhand (stdio), enqueued as an ordinary Egg in the same Pecking Order with lineage `parent_run_id`, depth capped at 1, and a narrower Farmhand tool set (enqueue + read only; it cannot stop its ancestors) | `harness.spawn_subs`, `runs.parent_run_id`, `farmhand.py` |
| **Gate** | A registered external MCP connector — an opening in the fence to an outside tool service. The platform only *registers* gates (connectors.toml, CRUD via `connectors_*` ops, Headcount `gate.registered`/`gate.removed`/`gate.rejected`); it never provisions or pays for the third party. A Chicken opts in per egg via `[harness] connectors = ["name", …]` and gets the chosen gates injected into its harness runtime's MCP config. Env/header values are secrets: refs (`$VAR`) stay refs in every served payload, values never appear in any response, audit row, or manifest. An https header value must be a ref — a literal (non-`$ref`) header secret is refused at registration with a field-named `connectors.<name>.headers` error, and a literal at rest in a hand-edited registry is withheld (`<withheld>`) from every listing (the Fox keeps gate credentials out of the Coop, the Gates view, and the Headcount) | `connectors.toml`, `connectors.py`, `harness.connectors` |

## Support Cast

| Term | Meaning | Maps to |
|---|---|---|
| **Rooster** | Cron-style scheduler — it wakes the jobs up | `scheduler.py` |
| **Fox** | Security module — foxes eat Eggs *and* Chickens; it watches the fence: all state the daemon creates is restrictive (dirs 0700, files 0600, restrictive umask at start — no umask window) | `fox.py` (perms hardening), `fence.py` (the shared safe-ref rule) |
| **Fence** | The security boundary the Fox enforces — the Coop's network perimeter (bearer required on **every** bind unless the operator opts in via `coop_loopback_open`), filesystem containment (an Egg's file refs and a Chicken's workspace paths must stay inside their own directory; absolute, `..`-crossing, backslashed, or symlink-escaping refs are refused at the fence), and resource limits (connection, log, and output caps) | `fence.py` (the shared safe-ref rule), `beak.py` path lock, spec-loader ref checks, coop/farmhand bearer middleware, cap constants in `settings.py` |
| **Perch** | Seat-based auth — a seat is a perch | `seat.py` |
| **Headcount** | Audit log — a farmer's headcount. Every mutating row also carries a `via` actor — `socket` (local CLI / direct socket), `http` (the Coop's API), `farmhand` (the Farmhand's tools), `rooster` (a cron crow) — stamped server-side from the verified connection, never taken from a client request | `audit.py` |
| **Operator** | The human on the other end of the Coopd socket or the Coop HTTP API — socket/HTTP access means operator | CLI / coop user |

## The Fence — trust model

The **Fence** is the trust boundary the **Fox** watches. Access to the Coopd unix socket
(0600, peer credentials read on every connection) and to the Coop HTTP API (bearer when
required) means **Operator**. A **Chicken** is **not an operator**: the daemon reads the
kernel's peer credentials (SO_PEERCRED) on every socket connection, records each Chicken's
pid at hatch, and resolves the peer pid to the owning Chicken — so on a chicken-owned
connection, lineage (`parent_run_id` / `caller_run_id`) is forced from the authenticated
peer and client-sent lineage fields are ignored. A Chicken connecting directly gets only
the narrowed sub-chicken op set, can never enqueue without lineage, and cannot stop its
ancestors. Security refusals in this vocabulary say so plainly: *the Fox refuses — a chicken
is not an operator*.

## The Interface Layer

| Term | Meaning | Maps to |
|---|---|---|
| **Coop** | The management GUI / container | `coop.py`, `coop_static/` |
| **Coopd** | The always-on supervisor daemon | `daemon.py` |
| **Farmhand** | The MCP — a hired helper who does chores through the farm's tools | MCP (future) |
| **Beak** | The read-only live view into a running Chicken — workspace file tree + single-file peek + log tail (`workspace_peek` socket op, `GET /api/runs/{id}/workspace`, the Beak tab in the coop). The Beak is honest about ephemerality: it is open only while the workspace exists (in-flight chickens); once the Abattoir tears the chicken down, the Beak is closed. It never serves the chicken's private harness config (`.vibe/`, the farmhand settings file, the Scratch) — resolved gate/env secrets live there | `beak.py`, `workspace_peek`, the Beak tab |
| **Scratch** | The Chicken's per-run scratch home (`.tc-home/` inside its own workspace): HOME, TMPDIR, XDG_* and VIBE_HOME all point there, and only a sanitized vibe profile (browser-auth fields only) is materialized in it — so a Chicken cannot read or write the operator's real `$HOME` or coopd's `/proc/<pid>/environ`, and nothing it plants in its home survives the Abattoir (the workspace — Scratch included — is removed at teardown) | per-run env in `adapter.py`, `.tc-home/` in the workspace |

## Reserved

| Term | Intended for |
|---|---|
| **Silo** | If `storage.py` ever splits: Silo = the append-only permanent record; Nest Box = live, fetchable Clutches |
| **Clutch** (secondary sense) | A batch of jobs submitted together ("a clutch of runs") |

## Retired / Rejected

| Term | Was / would be | Why retired |
|---|---|---|
| **Poultryman** | The MCP's original name | Person-title that doesn't say what the MCP does; superseded by Farmhand |
| **Egg Basket** | The results store | Collides with Egg in UI/UX contexts; superseded by Nest Box |
| **Chick** | Candidate for job spec | A chick is by definition already hatched — biologically wrong for a dormant spec; Egg won |
| **Setting** | Candidate for job spec | Collides with `settings.py` |
| **Larder** | Candidate for results store | Flavor-overlap with the Abattoir (a larder is where meat goes) |

## Convention Rules

1. **Concrete nouns, no prefixes/suffixes.** Coop, not CoopService; Rooster, not RoosterModule.
2. **The metaphor must be functional, not decorative.** A name earns its place by mapping to what the thing does (Run = execution, Pecking Order = priority queue, Candling = inspecting before you incubate).
3. **No repeated nouns across lifecycle stages.** Egg (spec) and Clutch (results) are deliberately distinct.
4. **Death is unapologetic.** The Abattoir name is only used where teardown is guaranteed and attested; best-effort cleanup is not the Abattoir.
5. **Match the register everywhere.** Docs, audit events, error messages, and UI copy all use lexicon terms ("this run laid 3 eggs", `egg.hatched`, `clutch.stored`, `abattoir.attested`).
