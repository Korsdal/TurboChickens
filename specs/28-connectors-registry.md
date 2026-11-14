# 28: Connectors registry — register external MCP connectors, chickens opt in

**What to build:** A `connectors.toml` registry resolved exactly like `providers.toml` (`settings.connectors_path` > `$TURBOCHICKENS_CONNECTORS` > `<data_dir>/connectors.toml`). Entries: `[connectors.NAME]` with `transport = "stdio"` (`command`, `args`, `env`) or `transport = "https"` (`url`, optional header env refs). New socket ops `connectors_list` / `connectors_add` / `connectors_rm` — CRUD over the registry file, audit-logged. Chickens opt in via `[harness] connectors = ["name", ...]`; the adapter injects the chosen connectors into the harness runtime's MCP config. Standing rule: the platform registers connectors, it never provisions or pays for third-party services. This registry is also the foundation for the future scoped connector service (out of scope here).

**Blocked by:** 26 (builds on the Farmhand's MCP injection pattern); spec 19's access-scope placeholders are the UI home this fills

**Status:** ready-for-agent (split from spec 21 via to-tickets, 2026-11-14)

- [ ] `connectors.toml` parser: resolution order as above, strict schema (field-named errors), stdio entries need `command`, https entries need `url`, unknown keys forbidden; pure-seam tests
- [ ] Socket ops `connectors_list` / `connectors_add` / `connectors_rm` mutate the registry file and append audit rows (names per lexicon); list never exposes env secret values — header env refs stay as references
- [ ] Spec model: `HarnessSection` gains `connectors: list[str] = []`; unknown connector names in the list → field-named error at spec load or enqueue
- [ ] E2E: a chicken with `[harness] connectors = ["x"]` gets connector "x" injected into its harness runtime MCP config (fake vibe binary asserts the entry); a chicken without the key gets none
- [ ] Secrecy: no connector env/header secret material in any response, audit row, or manifest
- [ ] Tests: pure seam for the parser + spec field; e2e for injection; full suite green
- [ ] `STATUS.md` updated at the slice boundary
