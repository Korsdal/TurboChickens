# 10: Model discovery — "what models are actually available"

**What to build:** A way to answer "which models can I actually use right now?" per adapter. Today the answer is split and implicit: the `mistral-api` adapter can use anything in `providers.toml` (built-ins: mistral-large-latest, mistral-small-latest, ministral-8b-latest, ministral-3b-latest, plus user-added entries), while the `vibe-cli` adapter is limited to whatever the `vibe` binary's catalog offers (currently only `mistral-medium-3.5` in vibe 3.1.2, extendable via custom `providers`/`models` in `~/.vibe/config.toml`). There is no command that queries a provider's live catalog. Goal: `tc models list` showing, per provider in `providers.toml`, the models returned by `GET {base_url}/models` (marked with which are in the local pricing presets), and for vibe-cli jobs the models vibe itself reports (from its config/catalalog), so a job spec's `model` field can be chosen against reality, not guesswork.

**Blocked by:** 05 (providers.toml registry + mistral-api adapter; the /models query reuses the same base_url + credential env var)

**Status:** done (2026-11-12 — implemented in `models.py` + `tc models list`; raised because the vibe-cli harness rejected `mistral-small-latest` with "Unknown model … Available: mistral-medium-3.5", and there was no way to list available models from TurboChickens)

- [x] `tc models list` prints, per provider: reachable or not, live model ids from `GET /models`, and which of them have pricing presets in providers.toml
- [x] `tc models list --adapter vibe-cli` prints the models the vibe binary itself offers, via a headless probe with a sentinel `--model` parsed out of vibe's own `Unknown model … Available: …` diagnostic (the same list a vibe-cli run validates against), so vibe-cli job specs can be validated against the real list
- [x] `tc jobs validate` warns (not fails) when a `mistral-api` job's model has no pricing preset, and errors clearly when a model is unknown to the chosen harness (unknown provider for mistral-api; model not in the probed vibe catalog for vibe-cli)
- [x] Credential handling matches spec 05: the /models call uses the allowlisted env var (plus the seat session token for `auth = "seat"` providers, spec 09); no key is ever printed or included in error strings
- [x] Caching: catalog fetch is on-demand per invocation — `tc models list` is local, no daemon state, no TTL
- [x] Tests: fake OpenAI-compatible server on loopback serving a /models payload → `tc models list` shows them; unreachable provider → row marked unreachable, rc 0; vibe-catalog path tested with a fake vibe binary
