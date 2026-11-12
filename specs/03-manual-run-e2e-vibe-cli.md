# 03: Manual run end-to-end via the `vibe-cli` adapter

**What to build:** The first complete vertical path through the platform: `tc run <job>` submits over the socket into the daemon's queue with resource-weight admission control (API runs weigh 0.25, CLI-agent runs 1.0, budget `max(1, min(vCPU, floor(RAM_GB/1.5)))` with settings override; over-budget runs backlog, never reject). The provisioner creates a fresh per-run workspace with a per-run environment (no ambient secrets). The harness adapter interface (`prepare` / `execute` / `teardown`, with the credential-provider extension point reserved) is implemented by the `vibe-cli` adapter running the Vibe CLI headless as a subprocess pinned to the workspace, streaming sanitized log lines. The run follows the lifecycle queued → provisioning → running → collecting → finalizing → succeeded/failed, writes a manifest (spec hash, harness + version, provider, model, temperature, timestamps, usage), and every terminal state gets a teardown attestation (process reaped, workspace verified gone, env cleared) plus audit rows. CLI surface: `tc run`, `tc runs list/show/logs`.

**Blocked by:** 02 (daemon, socket, storage, `tc status`)

**Status:** ready-for-agent

- [ ] `tc run <job>` executes the job via headless `vibe-cli` in a fresh workspace and the run reaches `succeeded`
- [ ] The run manifest records the pinned environment; streamed logs land in run events, sanitized of control characters
- [ ] A teardown attestation and audit rows exist for the run; a run without an attestation is detectable as an alert condition
- [ ] `tc runs list/show/logs` display the run; an over-budget run waits in the queue instead of being rejected
