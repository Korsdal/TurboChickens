# 10: Model discovery — "what models are actually available"

**What to build:** A way to answer "which models can I actually use right now?" per adapter. Today the answer is split and implicit: the `mistral-api` adapter can use anything in `providers.toml` (built-ins: mistral-large-latest, mistral-small-latest, ministral-8b-latest, ministral-3b-latest, plus user-added entries), while the `vibe-cli` adapter is limited to whatever the `vibe` binary's catalog offers (currently only `mistral-medium-3.5` in vibe 3.1.2, extendable via custom `providers`/`models` in `~/.vibe/config.toml`). There is no command that queries a provider's live catalog. Goal: `tc models list` showing, per provider in `providers.toml`, the models returned by `GET {base_url}/models` (marked with which are in the local pricing presets), and for vibe-cli jobs the models vibe itself reports (from its config/catalalog), so a job spec's `model` field can be chosen against reality, not guesswork.

**Blocked by:** 05 (providers.toml registry + mistral-api adapter; the /models query reuses the same base_url + credential env var)

**Status:** ready-for-agent (backlogged 2026-11-12 — raised because the vibe-cli harness rejected `mistral-small-latest` with "Unknown model … Available: mistral-medium-3.5", and there was no way to list available models from TurboChickens)

- [ ] `tc models list` prints, per provider: reachable or not, live model ids from `GET /models`, and which of them have pricing presets in providers.toml
- [ ] `tc models list --adapter vibe-cli` (or a `--harness` flag) prints the models the vibe binary itself offers (vibe config catalog + custom models), so vibe-cli job specs can be validated against the real list
- [ ] `tc jobs validate` warns (not fails) when a `mistral-api` job's model has no pricing preset, and errors clearly when a model is unknown to the chosen harness
- [ ] Credential handling matches spec 05: the /models call uses the allowlisted env var; no key is ever logged
- [ ] Caching: catalog fetch is on-demand per invocation (no daemon state), with a short TTL only if a schedule/cron path ever needs it
- [ ] Tests: fake OpenAI-compatible server on loopback serving a /models payload → `tc models list` shows them; unreachable provider → row marked unreachable, rc 0; vibe-catalog path tested with a fake vibe binary
