Status: ready-for-agent
Source: turbochickens-baseline-spec-v0-2.md (baseline spec v0.2)

# Spec: TurboChickens MVP

## Problem Statement

I want to run recurring, well-defined LLM tasks on my own machine without babysitting them. Today that means keeping a chat session or agent alive, trusting whatever it produces, and discovering cost only on the invoice. There is no local-first platform where an AI agent is a job, not a co-worker: spawned deterministically, given a fresh workspace, expected to produce schema-validated output, and verifiably destroyed afterwards — with the platform itself as the durable thing that keeps the queue, the results, the audit trail, and the cost statistics.

## Solution

TurboChickens: an always-on local supervisor daemon plus a `tc` CLI. Jobs are declared in TOML job specs (trigger, harness, model, inputs, output schema, budgets). Every run is claimed from a single queue — fed by both `tc run` and cron — provisioned into a fresh ephemeral workspace with a pinned environment recorded in a manifest, executed by a pluggable harness adapter, validated against its output schema, stored in an append-only result store, and closed only with a teardown attestation written to an append-only audit log. Wall-clock time and injected context size are enforced budgets; token usage is measured telemetry rolled into per-job statistics with expected cost derived from provider pricing.

## User Stories

1. As an operator, I want to define a job in a single declarative TOML job spec, so that the what, how, and when of every run is pinned in one reviewable artifact.
2. As an operator, I want the job spec to declare its trigger (manual or cron with a cron schedule), so that when a run fires is part of the job's definition, not an afterthought.
3. As an operator, I want to trigger a job manually with `tc run`, so that I can run it on demand without waiting for the schedule.
4. As an operator, I want to manage schedules with `tc schedule add/list/rm`, so that recurring jobs are declarative and reviewable from the CLI.
5. As an operator, I want manual and cron-triggered runs to flow through the same queue over the same local socket, so that there is one source of truth for what runs, in what order, and under what admission control.
6. As an operator, I want every run to execute in a fresh per-run workspace that shares nothing with any other run, so that no state ever leaks between runs.
7. As an operator, I want each run's environment pinned in a manifest (spec hash, harness and version, provider, model, temperature, token usage, timestamps), so that any run is reproducible and any deviation between manifest and outcome is visible.
8. As an operator, I want jobs to be idempotent by design, so that a re-run is always safe.
9. As an operator, I want a hard wall-clock timeout per job that kills the run, so that a stuck job can never hold resources forever.
10. As an operator, I want a hard cap on injected context size (prompt + attachments + injected state) per job, so that context cannot silently grow beyond what the job declared.
11. As an operator, I want no `max_tokens` knob in the job spec, so that token usage is treated as measured telemetry rather than a declared budget.
12. As an operator, I want each run's token usage recorded in its manifest and rolled up into per-job rolling statistics (n, mean/p50/p95 tokens in and out, expected cost per run from provider pricing), so that after a handful of runs I know what a run should cost and can treat deviations as alerts rather than invoice surprises.
13. As an operator, I want provider/model presets and pricing in a platform-level providers.toml, so that model choice and expected cost are config, not code.
14. As an operator, I want artifact size caps that default from the model's context window and are overridable per job, so that artifacts stay proportionate to the job.
15. As an operator, I want runs to execute with no ambient secrets — credentials injected as per-run env vars and cleared at teardown — so that a compromised run cannot exfiltrate my long-lived keys.
16. As an operator, I want a credential-provider extension point in the adapter interface, so that a host-side credential broker can replace env injection later without redesign.
17. As an operator, I want raw harness output treated as untrusted until it passes the job's JSON schema, so that only well-formed results ever enter the result store.
18. As an operator, I want invalid output quarantined — kept for inspection, never stored as valid — and the run marked failed, so that bad output is visible but can never be mistaken for a real result.
19. As an operator, I want every terminal state to require a teardown attestation (process reaped, workspace directory verified gone, env cleared, audit row written), so that destruction is verified, not assumed.
20. As an operator, I want a run that reaches a terminal state without a teardown attestation to be an alerting condition, so that silent leaks surface instead of accumulating.
21. As an operator, I want streamed harness logs sanitized of control characters and stored read-only, so that logs are data and no log line is ever interpreted by the platform.
22. As an operator, I want an append-only audit log of runs and teardown attestations, so that the platform's history cannot be rewritten.
23. As an operator, I want the result store to be append-only (SQLite index + on-disk artifacts), keyed by run ID, so that results survive the run that produced them without being mutable.
24. As an operator, I want to inspect runs with `tc runs list/show/logs` and fetch validated results with `tc results get`, so that I can see exactly what happened, what was produced, and what was proposed-but-not-committed.
25. As an operator, I want `tc status` to show daemon health, queue depth, and in-flight runs with their resource weights, so that I know the platform's state at a glance.
26. As an operator, I want resource-weight admission control — API runs weigh 0.25, CLI-agent runs weigh 1.0, budget derived from machine size with a settings override — so that lightweight API jobs never starve behind heavy CLI agents, and over-budget runs backlog in the queue instead of being rejected.
27. As an operator, I want a mistral-api adapter that speaks OpenAI-compatible HTTP with JSON-mode structured responses, so that raw model output is easy to schema-validate and other OpenAI-compatible providers are a config change, not a code change.
28. As an operator, I want a vibe-cli adapter that runs the Vibe CLI headless as a subprocess pinned to the run workspace, so that CLI-agent jobs get the same lifecycle guarantees as API jobs.
29. As an operator, I want exponential backoff on provider 429/5xx responses in the API adapter, so that provider-side rate limits are handled gracefully.
30. As an operator, I want failed runs (validation failure, harness error, budget or timeout exceeded) to still get teardown verified and quarantine/audit rows written, so that even failures leave a complete, attested record.
31. As an operator, I want retention TTLs managed via settings — audit log kept forever by default, results expiring after 6 months by default, every deletion audit-logged — so that disk usage is bounded without ever losing the audit trail.
32. As an operator, I want the job spec format and harness adapter interface free of any Sluse-specific concepts, so that the platform stays standalone and reusable.
33. As an operator, I want to pass one job's validated result to another job manually as an input, so that multi-step workflows can be composed by hand until there is a reason to automate chaining.
34. As an operator, I want the CLI to talk to the daemon over a local Unix domain socket using JSON-lines, so that all interaction is local, scriptable, and goes through one protocol.

## Implementation Decisions

- **Modules to build:** supervisor daemon (queue, internal cron scheduler, process watcher, resource-weight admission control); provisioner (fresh workspace, pinned harness, per-run env); harness adapter layer with two MVP adapters (`vibe-cli` subprocess, `mistral-api` HTTP); output validator (JSON-schema) plus quarantine; result store (SQLite index + on-disk artifacts); teardown attestation writer; append-only JSONL audit log; run manifest writer; per-job usage statistics rollup; retention/TTL settings enforcement; `tc` CLI; TOML job spec parser and validator.
- **Stack:** Python 3.12+, `uv` + `pyproject.toml`, single installable package exposing the `tc` entry point; asyncio single-process daemon; APScheduler for cron; Typer + Rich for the CLI; Pydantic v2 for spec parsing; `jsonschema` for output validation; stdlib `sqlite3` for the store; `httpx` for the API adapter; `asyncio.subprocess` for CLI harnesses. Linux-only for the MVP.
- **IPC contract:** the CLI and the cron scheduler both submit to the daemon over a Unix domain socket speaking JSON-lines; one queue, one admission path, one source of truth.
- **Run lifecycle:** queued → provisioning → running → collecting → finalizing → succeeded; collecting → failed on output validation failure; running → failed on error, budget, or timeout; every failed run still gets teardown verified plus quarantine/audit written before it goes terminal.
- **Harness adapter interface** (from the baseline spec; the Protocol shape is the decision): each adapter implements `prepare(run) -> PreparedRun` (pin harness identity — version, model, provider — and build the per-run env template, no secrets in the workspace), `execute(prepared) -> HarnessResult` (run inside the workspace, stream log lines to the supervisor, return raw output + usage + exit status), and `teardown(run) -> TeardownReport` (confirm process reaped, workspace gone, env cleared; return an attestation record). A credential-provider extension point is reserved on this interface.
- **Admission control:** resource weights — API runs 0.25, CLI-agent runs 1.0 — against a weight budget of `max(1, min(vCPU, floor(RAM_GB / 1.5)))` with a settings override; over-budget runs backlog, nothing is rejected.
- **Budgets:** the only enforced quantities are wall-clock timeout (hard kill) and injected-context size cap; token usage is recorded per run and aggregated, never capped by the spec.
- **Usage statistics:** per-job rolling stats over runs — count, mean/p50/p95 tokens in and out, expected cost per run computed from `providers.toml` pricing.
- **Security posture:** least-privilege spawn with per-run env credentials cleared at teardown; outputs untrusted until schema-validated, invalid output quarantined; teardown attestation required for every terminal state and its absence is an alert; logs sanitized and read-only; result store and audit log append-only.
- **Data model (SQLite):** tables for job specs (id, name, spec hash, spec TOML, created_at), runs (id, job, status, trigger, timestamps, manifest JSON, usage JSON), run events (streamed logs), results (valid flag, output JSON, artifacts dir, schema errors), teardown attestations, per-job usage stats. Trajectory and trajectory-draft tables are deliberately not built in the MVP.
- **Job spec format:** TOML with `[job]`, `[trigger]` (cron/manual), `[harness]` (adapter, model, temperature), `[resources]` (timeout_sec, max_context_mb, declared network policy), `[input]` (prompt file, attachments), `[output]` (format, schema file). No `max_tokens` key.

## Testing Decisions

- **Good test:** tests assert only external, observable behavior — run status transitions, result-store contents, quarantine outcomes, attestation and audit rows, CLI output, socket protocol responses. Never internal state, mock call counts, or implementation details.
- **Seams (proposed — please confirm):** the single highest seam is end-to-end through the `tc` CLI against a real daemon over the Unix socket — every feature test drives the platform the way a user does. Because the codebase is greenfield there are no existing seams to prefer; this one seam covers queueing, scheduling, provisioning, validation, attestation, and stats. A second, pure-function seam is used only for the TOML spec parser and the JSON-schema output validator, tested directly on inputs and outputs since they are pure transforms. A test-only harness adapter implementing the same adapter interface lets end-to-end tests run without real provider credentials or network access.
- **Modules under test:** all of the above through the two seams; the adapter layer is additionally exercised via the fake adapter and via contract-shaped tests for the two real adapters (backoff behavior, JSON-mode request shape, subprocess pinning).
- **Prior art:** none — this is a greenfield repo, so these tests establish the pattern: pytest with tmp-path workspaces, a fixture that boots the daemon on a temp socket, and hermetic fakes for provider HTTP.

## Out of Scope

- Event/webhook triggers (manual and cron only)
- Container/VM-level workspace isolation; the declared `network` policy is recorded but not enforced
- Multi-node operation, web UI/TUI dashboard, notifications, RBAC
- Cross-job automated chaining (validated outputs are passed manually)
- Credential brokering beyond per-run env vars (interface reserved, not implemented)
- Trajectory loops (agent-proposed, human-accepted job continuity) — designed in the baseline spec, explicitly post-MVP
- Cost-deviation alerting on top of the usage statistics (stats ship; alerting is roadmap)
- Per-job-type retention policies (global TTL settings ship instead)
- Native Windows support (Linux MVP; Windows users run an Ubuntu VM)
- Additional harness adapters beyond `vibe-cli` and `mistral-api`

## Further Notes

- This spec operationalizes baseline spec v0.2 (`turbochickens-baseline-spec-v0-2.md`); where this spec and the baseline disagree, the baseline's decision log (v0.2, decisions 1–6) governs.
- Related to Sluse: keep the job spec format and harness adapter interface free of Sluse-specific concepts and keep the credential-provider extension point, so a Gate Core–style host broker can slot in later without redesign.
- The trajectory design (platform-owned, versioned, human-accepted) is deliberately not precluded by the MVP's data model even though its tables are post-MVP.
- Kept at `specs/archive/turbochickens-mvp-spec.md`, marked `Status: ready-for-agent`. When an issue tracker is configured (`/setup-matt-pocock-skills`), publish from here with the `ready-for-agent` label.
