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
| **Clutch** | A run's output artifacts — the eggs a Chicken lays | run results / artifacts |
| **Nest Box** | Results store — where Clutches are found | `storage.py` (results) |
| **Abattoir** | De-provisioning with attested, verifiable teardown | teardown / destruction |
| **Compost** | Retention — old Clutches rot away; audit records stay forever | `retention.py` |
| **Cancelled** | An Egg pulled from the Pecking Order before it hatches — terminal, never hatches, still abattoir-attested | `cancelled` run state / `run_stop` on a queued egg / `egg.cancelled` |
| **Stopped** | A Chicken ended deliberately by the operator — SIGTERM, then hard kill, with the Abattoir's attested teardown | `stopped` run state / `run_stop` on a running chicken / `chicken.stopped` |
| **Re-run** | Hatching the same Egg again as a fresh Chicken, new run ID, lineage recorded | `run_rerun` / manifest `rerun_of` |
| **Chaining** | An Egg consuming an earlier Chicken's Clutch via a `nestbox://<run_id>/output.json` attachment — resolved from the Nest Box at provisioning, valid clutches only | `chaining.py`, manifest `chaining`, Headcount `clutch.consumed` |
| **ChickenSub** | A sub-chicken — a Chicken spawned by another Chicken through its attached Farmhand (stdio), enqueued as an ordinary Egg in the same Pecking Order with lineage `parent_run_id`, depth capped at 1, and a narrower Farmhand tool set (enqueue + read only; it cannot stop its ancestors) | `harness.spawn_subs`, `runs.parent_run_id`, `farmhand.py` |
| **Gate** | A registered external MCP connector — an opening in the fence to an outside tool service. The platform only *registers* gates (connectors.toml, CRUD via `connectors_*` ops, Headcount `gate.registered`/`gate.removed`/`gate.rejected`); it never provisions or pays for the third party. A Chicken opts in per egg via `[harness] connectors = ["name", …]` and gets the chosen gates injected into its harness runtime's MCP config. Env/header values are secrets: refs (`$VAR`) stay refs in every served payload, values never appear in any response, audit row, or manifest | `connectors.toml`, `connectors.py`, `harness.connectors` |

## Support Cast

| Term | Meaning | Maps to |
|---|---|---|
| **Rooster** | Cron-style scheduler — it wakes the jobs up | `scheduler.py` |
| **Fox** | Security module — foxes eat Eggs *and* Chickens | security (future) |
| **Perch** | Seat-based auth — a seat is a perch | `seat.py` |
| **Headcount** | Audit log — a farmer's headcount. Every mutating row also carries a `via` actor — `socket` (local CLI / direct socket), `http` (the Coop's API), `farmhand` (the Farmhand's tools), `rooster` (a cron crow) — stamped server-side from the verified connection, never taken from a client request | `audit.py` |

## The Interface Layer

| Term | Meaning | Maps to |
|---|---|---|
| **Coop** | The management GUI / container | `coop.py`, `coop_static/` |
| **Coopd** | The always-on supervisor daemon | `daemon.py` |
| **Farmhand** | The MCP — a hired helper who does chores through the farm's tools | MCP (future) |
| **Beak** | The read-only live view into a running Chicken — workspace file tree + single-file peek + log tail (`workspace_peek` socket op, `GET /api/runs/{id}/workspace`, the Beak tab in the coop). The Beak is honest about ephemerality: it is open only while the workspace exists (in-flight chickens); once the Abattoir tears the chicken down, the Beak is closed. It never serves the chicken's private harness config (`.vibe/`, the farmhand settings file) — resolved gate/env secrets live there | `beak.py`, `workspace_peek`, the Beak tab |

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
