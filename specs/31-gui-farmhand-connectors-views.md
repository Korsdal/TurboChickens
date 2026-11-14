# 31: GUI — Farmhand view & Connectors CRUD view

**What to build:** Two new coop views. **Farmhand**: shows the active transport(s) from `farmhand_transport`, the HTTPS endpoint (`farmhand_https_path` on the coop host), the exposed tool list, and a ready-to-paste MCP client config (stdio command `tc farmhand serve --transport stdio`, or HTTPS URL + bearer note) — the bearer token itself never appears in the payload or the snippet. **Connectors**: CRUD over the registry — list registered connectors (name, transport, non-secret summary), add (stdio command/args or HTTPS URL), remove. Nav gains Farmhand and Connectors entries. Both views follow the specs 12–19 pattern: 2 s polling with flash-on-change, empty states, daemon-down banner, bearer 401s, design-system shell. Farmhand config changes go through the Settings surface (read-only over MCP, per slice 26).

**Blocked by:** 26 (Farmhand server + settings block), 28 (connectors registry + socket ops)

**Status:** done (2026-11-14)

- [x] Farmhand view: active transport mode(s), HTTPS `/mcp` URL, exposed tools list, copy-paste client config snippets (stdio command; HTTPS URL + "send the coop bearer" note); settings form to switch `farmhand_transport` among `stdio`/`https`/`both` with `extra=forbid` validation server-side
- [x] The bearer token never appears in any Farmhand view payload, page markup, or client-config snippet
- [x] Connectors view: list (name + transport + non-secret summary), add form (name, transport=stdio → command/args/env, or transport=https → url), remove action; inline errors for unknown-transport or missing-field entries; changes audit-logged via the socket ops
- [x] Nav: sidebar gains Farmhand and Connectors entries (design system, spec 00)
- [x] All new `/api/*` endpoints mirror their socket ops exactly, with dead-socket `200 {ok:false, daemon.up:false}` and bearer 401 handling like specs 12–19
- [x] Tests: HTTP parity for every new endpoint, served-markup smoke for both views, secrecy assertion (no bearer/token material in any response); full suite green
- [x] `STATUS.md` updated at the slice boundary — this completes the spec 21 slice map
