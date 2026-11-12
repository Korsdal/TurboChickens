# 05: `mistral-api` adapter + `providers.toml` + backoff

**What to build:** The second harness adapter, speaking OpenAI-compatible HTTP (`httpx`) against Mistral with JSON-mode structured responses where supported, reusing the same adapter interface and therefore the same queue, validation, result store, and attestation path as the `vibe-cli` adapter. Exponential backoff on provider 429/5xx responses. A platform-level `providers.toml` holding provider/model presets and pricing, so other OpenAI-compatible providers are a config change, not a code change. API-adapter runs weigh 0.25 in admission control.

**Blocked by:** 03 (adapter interface + run pipeline), 04 (validation + result store, so the API path is demoed fully validated)

**Status:** ready-for-agent

- [ ] A job with adapter `mistral-api` executes over HTTP and its schema-validated output lands in the result store
- [ ] 429/5xx responses trigger exponential backoff, visible in the run logs
- [ ] `providers.toml` supplies model presets and per-token pricing, consumed later for expected-cost stats
