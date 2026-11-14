# 21: The Hatchery & the Farmhand — end-to-end agent lifecycle management (overview v0.1)

**Status:** v0.1 overview — agreed in brainstorm 2026-11-14; **not ready-for-agent**. To be split into slice tickets (proposed: 22+) via `to-tickets` (or repo-style numbered slices, same as 11 → 12–20) before any implementation.
**Blocked by:** 01–20 (all landed). Slices only depend on the slices named in the slice map below.

# Spec: Hatchery deepening + the Farmhand (MCP) + GUI for everything

## Problem Statement

The operator can create, schedule, and watch chickens, but cannot manage their **whole lifecycle**: there is no way to stop or cancel a run, no way to re-run one, and no long-lived ("free-range") chickens — every chicken is born with a hard TTL and dies by the clock. There is no cost guardrail: token usage is telemetry, and a looping chicken can only be noticed after the fact. Eggs cannot consume each other's clutches — chaining is manual copy-paste. And although the coop GUI now covers jobs/runs/schedules/stats/auth, there is no **MCP surface**: no agent can manage the farm through tools, and no chicken can manage sub-chickens. A locally running agent (e.g. a Mistral-seat vibe-cli agent on the operator's PC) has to shell out to `tc`; a chicken that should orchestrate other chickens cannot do so at all. Chickens also cannot reach outside the box: the `mcp_servers`/`connectors` access-scope placeholders from spec 19 are still placeholders. Finally, a running chicken is a black box — its workspace is invisible until the abattoir tears it down.

The end state of this phase: **the operator manages the full agent lifecycle end-to-end from the GUI, and agents manage agents through the Farmhand** — an MCP server that exposes the farm's own operations as tools, reachable over stdio (same machine) and over HTTPS (bearer-authed), transport decided in settings.

## Solution

Three pillars, all speaking farm lexicon (`docs/lexicon.md`):

1. **Hatchery deepening** — lifecycle ops (`run_stop`, `run_rerun`), **Free-Range** chickens (`resources.free_range = true`, no hard TTL, attested teardown on manual stop and on daemon shutdown), a **Feed Budget cost cap** (`resources.max_cost_usd`, **off by default**, enforced at finalize — never a `max_tokens` knob), **chaining** (`attachments = ["nestbox://<run_id>/output.json"]` resolved from valid clutches at provisioning), and the **Beak** — a read-only live view into a running chicken (log tail + workspace peek).
2. **The Farmhand** — an MCP server (`farmhand.py`, official `mcp` Python SDK) that wraps the daemon's existing socket ops as MCP tools. Transport setting `farmhand_transport = "stdio" | "https" | "both"` (default `"both"`): stdio via `tc farmhand serve --transport stdio` for agents on the same machine (gated by the 0600 socket); HTTPS as streamable-HTTP at `/mcp` on the coop server behind the existing bearer. MCP clients get a **narrower scope** than the GUI bearer: no auth/session mutation, no retention sweep, settings read-only. **ChickenSubs**: a per-chicken toggle (`[harness] spawn_subs = true`) that attaches the Farmhand to the chicken over stdio, so a chicken can enqueue sub-chickens — ordinary eggs through the same queue and admission control, with `parent_run_id` lineage, depth capped at 1, and a narrower sub tool set (no stopping an ancestor, no schedule/auth/settings changes).
3. **GUI for literally everything** — new Farmhand view (transport mode, HTTPS URL, exposed tools, ready-to-paste client config), Connectors CRUD view over a new `connectors.toml` registry, per-job hatchery knobs in the Jobs upload form, Stop / Re-run buttons and lineage in the Runs view, and the Beak tab on the run detail. All of it over the existing `/api/*` transport seam — the web layer never bypasses the socket contract.

**Standing rule, unchanged:** we do **not** provision third-party AI providers or their APIs — keys and seats are the user's own (they carry cost). The same rule applies to connectors: the platform *registers* MCP connectors, it never provisions or pays for third-party services.

## User Stories

1. As an operator, I want to cancel a queued egg from the GUI, so that a mistake in the pecking order never hatches.
2. As an operator, I want to stop a running chicken from the GUI, so that a looping or runaway agent is ended deliberately, with the abattoir's attested teardown.
3. As an operator, I want to re-run a chicken from the GUI (same egg, new run), so that retrying a failed or succeeded job is one click.
4. As an operator, I want the same stop/re-run from the CLI (`tc runs stop`, `tc runs rerun`) and from the Farmhand as MCP tools, so that all three surfaces are equivalent.
5. As an operator, I want a stopped/cancelled run to show its terminal state and reason in the runs list and audit, so that lifecycle is fully visible.
6. As an operator, I want a free-range chicken (`free_range = true`, no `timeout_sec`), so that long-lived agents are possible and only die when I stop them or the daemon shuts down — with full teardown attestation in both cases.
7. As an operator, I want an optional per-egg cost cap (`max_cost_usd`, unset by default), so that a run whose measured cost exceeds the cap fails at finalize with `budget.outcome = "cost_cap"` — while token/cost telemetry is always recorded regardless.
8. As an operator, I want to see per-run cost against its cap in the run detail and Feed Ledger, so that looping chickens are visible as cost outliers even without a cap set.
9. As an operator, I want to attach a previous run's validated clutch as an input (`attachments = ["nestbox://<run_id>/output.json"]`), so that eggs can be chained without copy-paste.
10. As an operator, I want chaining to fail closed at provisioning when the referenced clutch is quarantined, expired, or missing, so that no untrusted or absent data ever enters a workspace.
11. As an operator, I want chained inputs to count toward the injected-context cap, so that chaining cannot bypass `max_context_mb`.
12. As a local agent on the operator's PC, I want to attach to the Farmhand over stdio (`tc farmhand serve --transport stdio`) and manage the farm with MCP tools — list eggs/runs, enqueue, watch, fetch clutches, stop, schedule — so that I can outsource farm work to an agent without a shell.
13. As a remote MCP client, I want to reach the Farmhand over HTTPS at `/mcp` with the coop bearer token, so that agents off-box can do chores through the farm's tools.
14. As an operator, I want the transport decided in settings (`farmhand_transport = "stdio" | "https" | "both"`), so that the coop can serve stdio locally and HTTPS at the same time or either alone.
15. As an operator, I want the Farmhand's tool surface scoped narrower than the GUI: MCP clients cannot mutate auth/seats, cannot sweep retention, and see settings read-only, so that an agent with farm tools cannot break the fence.
16. As an operator, I want a Farmhand view in the GUI showing the active transport(s), the HTTPS endpoint, the exposed tools, and a copy-paste MCP client config (stdio command / HTTPS URL + bearer note), so that connecting an agent is one copy.
17. As an operator, I want to give a chicken the ability to spawn ChickenSubs (`spawn_subs = true` in `[harness]`), so that an agent can orchestrate sub-agents.
18. As a chicken with `spawn_subs`, I want the Farmhand attached to me over stdio and tools to enqueue sub-eggs and read the flock, so that I can manage my sub-chickens through the farm's own contract.
19. As an operator, I want sub-chickens to be ordinary eggs in the same pecking order with the same admission weights, so that subs cannot bypass queue discipline.
20. As an operator, I want every sub-chicken to carry `parent_run_id` in its manifest and audit, so that lineage is traceable and the runs view shows the tree.
21. As an operator, I want the sub-depth capped at 1 (a sub-chicken cannot itself spawn subs), so that recursion cannot explode the flock.
22. As an operator, I want sub-chickens' Farmhand scope narrowed (enqueue + read only; they cannot stop an ancestor, change schedules, or touch auth), so that a runaway sub cannot dismantle the farm.
23. As an operator, I want to register external MCP connectors in `connectors.toml` (stdio command or HTTPS URL), manage them from a Connectors GUI view (CRUD), and have a chicken opt in via `[harness] connectors = [...]`, so that chickens can reach scoped outside tools — with the platform never provisioning or paying for the third party.
24. As an operator, I want to see a running chicken live in the Beak (streamed log tail + workspace file tree + file peek), read-only, so that I can manage what I can see.
25. As an operator, I want the Beak to be honest about ephemerality: after the abattoir tears a chicken down, its workspace is gone, so the Beak shows in-flight chickens only.
26. As an operator, I want all hatchery knobs per egg in the Jobs upload form (free_range, max_cost_usd, timeout, spawn_subs, connectors), so that provisioning is fully configurable from the browser.
27. As an operator, I want every new capability (stop/re-run, farmhand config, connectors, knobs, Beak) behind the same bearer auth and daemon-down handling as the existing `/api/*` surface, so that the GUI has one security and one failure model.
28. As an operator, I want no response, audit row, or MCP tool result to ever carry token or seat material, so that the spec-09/19 secrecy rule holds on the new surfaces too.
29. As an operator, I want the spec format to stay strict (`extra = "forbid"`): the new keys land in known sections and anything else is a field-named error.
30. As a future operator, I want the Farmhand to later become the cloud-VM connector for agents (scoped connector service reaching outside the container), so that today's same-machine stdio design is not a dead end.

## Implementation Decisions

- **Lifecycle ops:** new socket ops `run_stop` (queued → `cancelled`; running → SIGTERM, then hard kill, attested teardown, terminal state `stopped`) and `run_rerun` (enqueue the same spec as a fresh run, lineage recorded). CLI: `tc runs stop <id>`, `tc runs rerun <id>`. Audit events per lexicon (new names, e.g. `egg.cancelled`, `chicken.stopped`, to be added to `docs/lexicon.md` when the slice lands). Every stop path gets the same teardown attestation as any terminal state — a missing attestation remains an alert condition.
- **Free-Range:** `ResourcesSection` gains `free_range: bool = False` and `timeout_sec` becomes optional; validator: `free_range = false` requires `timeout_sec > 0` (as today), `free_range = true` allows it to be absent (no hard kill). Free-range chickens are stopped by `run_stop` or at daemon shutdown, both with attested teardown. Baseline invariant 1/2 wording gets an explicit decision-log amendment when this slice lands (timeout moves from "always enforced" to "enforced unless free-range").
- **Feed Budget cost cap:** `ResourcesSection` gains `max_cost_usd: float | None = None` (USD, never a token count — invariant 1 intact). Enforcement is **finalize-only** in v0.1: if the run's measured `usage.cost_usd > max_cost_usd`, the run fails with `budget.outcome = "cost_cap"` (same budget block as timeout/context-cap). Cost and token telemetry are recorded on every run regardless of whether a cap is set. Off by default: absent field = no cap.
- **Chaining:** `InputSection.attachments` entries may be `nestbox://<run_id>/output.json`. The URI shape is validated at spec load (field-named error otherwise); resolution happens at provisioning: the daemon fetches the referenced run's clutch from the Nest Box — **valid clutches only** (quarantined, expired, or missing → provisioning failure, run failed before `running`, attested teardown as usual). Resolved bytes count toward `max_context_mb`; the manifest records the resolved sources.
- **Farmhand server:** new module `farmhand.py` built on the official `mcp` Python SDK (new dependency in `pyproject.toml`). Tools are thin wrappers over the existing socket ops via `client.request`: `status`, `jobs_list`, `jobs_validate`, `enqueue` (run an egg), `runs_list`, `runs_show`, `runs_logs`, `results_get`, `run_stop`, `run_rerun`, `schedule_add`, `schedule_list`, `schedule_rm`, `stats_list`, `stats_show`, `audit_tail`, `settings_get`. Transport: settings block `farmhand_transport = "stdio" | "https" | "both"` (default `"both"`), `farmhand_https_path = "/mcp"`. HTTPS is streamable-HTTP served from the coop process at `/mcp` behind the same bearer middleware as `/api/*` (loopback-open rule included). Stdio: `tc farmhand serve --transport stdio`, gated by the 0600 daemon socket — same-machine agents only. **MCP scope is narrower than the GUI**: MCP-exposed tools exclude `auth_*` mutations, `retention_sweep`, and any settings mutation; settings is read-only over MCP.
- **ChickenSubs:** `HarnessSection` gains `spawn_subs: bool = False` and `connectors: list[str] = []`. A chicken with `spawn_subs = true` gets the Farmhand attached to its harness runtime over stdio (for vibe-cli: an MCP server entry in the run's vibe config spawning `tc farmhand serve --transport stdio`). Subs are ordinary eggs enqueued through the same socket/queue/admission; the daemon records `parent_run_id` on the run (new `runs.parent_run_id` column, schema bump) and in the manifest/audit. **Depth cap 1:** enqueue refuses a spec with `spawn_subs = true` when the enqueuing run is itself a sub (field-named error). **Sub tool scope:** when the caller is a sub-chicken, the Farmhand exposes enqueue + read tools only, and `run_stop` refuses targets that are its ancestors.
- **Connectors registry:** new `connectors.toml`, resolved exactly like `providers.toml` (`settings.connectors_path` > `$TURBOCHICKENS_CONNECTORS` > `<data_dir>/connectors.toml`). Entries: `[connectors.NAME]` with `transport = "stdio"` (`command`, `args`, `env`) or `transport = "https"` (`url`, optional header env refs). Chickens opt in via `[harness] connectors = ["name", ...]`; the adapter injects the chosen connectors into the harness runtime's MCP config. New socket ops `connectors_list` / `connectors_add` / `connectors_rm` (CRUD over the registry file, audit-logged) mirrored as `/api/connectors/*`. The later "scoped connector service for chickens reaching outside the container" builds on this registry — out of this phase.
- **Beak:** read-only. New socket op `workspace_peek` (workspace file tree + single-file content, size-capped, path-locked inside the run's workspace; only while the workspace exists — i.e. in-flight chickens). GUI: run detail gains a Beak tab (live `runs_logs` tail + tree + peek), mirrored as `/api/runs/{id}/workspace`. No shell, no writes — invariant 9 (least-privilege spawn) is untouched.
- **GUI sweep (slice 29):** nav gains **Farmhand** and **Connectors**; Jobs upload form gains the resources/harness knobs; Runs view gains Stop / Re-run per row and a lineage indicator (parent/children); run detail gains the Beak tab. All views poll their `/api/*` every 2 s with flash-on-change, empty states, daemon-down banner, and bearer 401s exactly like specs 12–19. Built to `specs/00-design-system.md`.
- **Settings:** new `farmhand` block (`farmhand_transport`, `farmhand_https_path`) in `Settings` (extra=forbid stays); `public_dict()` exposes it (no secrets in it). `farmhand_transport = "both"` is the correct default term.
- **Seams preserved:** the web layer and the Farmhand both talk to the daemon only over the socket contract; MCP tools are socket ops wearing a protocol. Append-only audit/results, quarantine, attestation, admission weights — all unchanged. New state changes are limited to: `runs.parent_run_id` column, new terminal states `cancelled`/`stopped`, the new spec fields, the connectors registry.

## Proposed slice map (starting point for the to-tickets split)

| Slice | Content | Blocked by |
| --- | --- | --- |
| 22 | Lifecycle ops: `run_stop` / `run_rerun` socket ops + CLI + audit + attestation on stop | 02–04 |
| 23 | Free-Range chickens (`free_range`, optional `timeout_sec`, shutdown teardown, invariant amendment) | 22 |
| 24 | Feed Budget cost cap (`max_cost_usd`, off by default, finalize-only enforcement, telemetry always) | 05, 08 |
| 25 | Chaining (`nestbox://<run_id>/output.json` attachments, provisioning-time resolution, context cap) | 04, 07 |
| 26 | Farmhand MCP server: SDK, tool surface + narrower MCP scope, stdio + HTTPS transports, settings, `tc farmhand serve` | 12–19 |
| 27 | ChickenSubs: `spawn_subs` toggle, stdio attach, `parent_run_id` lineage, depth cap 1, sub tool scope | 22, 26 |
| 28 | Connectors registry (`connectors.toml`) + per-chicken opt-in + socket ops | 26 |
| 29 | GUI for all of the above: Farmhand view, Connectors CRUD, upload-form knobs, Stop/Re-run + lineage, Beak tab (`workspace_peek`) | 22–28 |

## Testing Decisions

- **What makes a good test here:** external, observable behavior only — socket/CLI/HTTP/MCP responses, store and audit contents, status transitions, file artifacts. Never internal state or mock call counts (repo convention).
- **Seams, highest first:** MCP tool → socket-op mapping tested through a real MCP client (the SDK's) over stdio against `tc farmhand serve`, and over HTTPS against a coop subprocess with bearer 401/200 checks; pure seams for the new spec-model fields/validators, the `nestbox://` resolver (valid / quarantined / expired / missing / wrong-shape), the finalize cost-cap check (over cap / at cap / no cap / seat runs with cost 0), and the `connectors.toml` parser; e2e through a thread + subprocess daemon with the fake vibe binary (`$TURBOCHICKENS_VIBE_BIN`) for stop/cancel/re-run, free-range (no kill at old TTL, attested stop + shutdown teardown), chaining (real clutch as attachment; quarantined-refusal), and ChickenSubs (lineage recorded; depth-1 refusal; sub-scope refusal of ancestor-stop).
- **Secrecy assertions, same as spec 19:** no MCP response, HTTP body, or audit row ever contains token/seat/device-code material; the bearer token never appears in the Farmhand view payload.
- **GUI:** served-markup smoke for each new view (like specs 12–19), no browser automation.
- **Parity rule:** every new `/api/*` endpoint must equal its socket op's payload (spec 13–19 pattern); dead-socket transport returns `200 {ok:false, daemon.up:false}` on all new endpoints, POSTs included.

## Out of Scope

- Provisioning third-party AI providers or their API keys/APIs — user's responsibility (they carry cost). Same for paid MCP connectors: the platform registers them, never provisions or pays.
- Mid-run cost kill — finalize-only in v0.1.
- Sub-sub-chickens — depth capped at 1.
- An interactive shell in the Beak — read-only live view only (invariant 9).
- The Fox (network-policy enforcement) — `network = "none"` stays declared, not enforced.
- The cloud-VM Farmhand connector / scoped connector service for chickens reaching outside the container — future phase, built on the slice-28 registry.
- Trajectory loops (baseline §11), RBAC/multi-user, webhooks/notifications, multi-node, event triggers — still out per baseline.
- Changing the admission weights or the weight-budget formula.

## Further Notes

- This is **v0.1**: principles agreed in the brainstorm (2026-11-14) — transports `stdio|https|both` with `both` the right term; Farmhand is both an MCP server (now, same-machine focus) and later the scoped connector service (future); ChickenSubs toggleable per chicken; cost cap exists but is **not on by default** (token usage stays the loop indicator); Beak is read-only; finalize-only cost enforcement; "everything goes in now — if we break something we follow up" (sole operator is the user). Split into slices via `to-tickets` (or repo-style 22+) before implementation; each slice gets the usual `Status` / `Blocked by` / checkbox list.
- When slices land: add the new lexicon terms and audit event names to `docs/lexicon.md` first (spec 20's rule — e.g. ChickenSub, `egg.cancelled`, `chicken.stopped`); record the baseline decision-log amendment for invariant 1/2 (free-range timeout, `cancelled`/`stopped` states, cost cap) in the slice-23 ticket; update `STATUS.md` at each slice boundary.
- Open details left to slices, not to this overview: exact MCP tool names/schemas, whether `/mcp` shares the coop listener or gets its own port (prefer shared, behind bearer), the `spawn_subs` field name, `connectors.toml` exact schema, and the Farmhand GUI's client-config snippet format.
