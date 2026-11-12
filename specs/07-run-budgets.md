# 07: Run budgets — wall-clock hard kill + injected-context cap

**What to build:** The two enforced budget quantities from the job spec. `timeout_sec` is a hard wall-clock kill: the harness process is terminated, the run marked failed, and teardown is still verified and attested. `max_context_mb` caps injected context (prompt + attachments + injected state) at provisioning; a run over the cap fails with a clear reason before doing work. Token usage remains telemetry, not a budget — no `max_tokens` anywhere. Budget outcomes are visible in the run manifest and `tc runs show`.

**Blocked by:** 03 (run pipeline and provisioner)

**Status:** ready-for-agent

- [ ] A run exceeding `timeout_sec` is hard-killed, marked failed, and still has a teardown attestation plus audit row
- [ ] A run whose injected context exceeds `max_context_mb` fails at provisioning with a clear recorded reason
- [ ] The manifest records the budget outcome; the spec model has no token cap
