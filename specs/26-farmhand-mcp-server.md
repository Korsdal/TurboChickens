# 26: The Farmhand — MCP server over the farm's own socket ops (stdio + HTTPS)

**What to build:** The Farmhand, an MCP server (new module `farmhand.py`, official `mcp` Python SDK — new dependency in `pyproject.toml`) whose tools are thin wrappers over the daemon's existing socket ops via `client.request`: `status`, `jobs_list`, `jobs_validate`, `enqueue`, `runs_list`, `runs_show`, `runs_logs`, `results_get`, `run_stop`, `run_rerun`, `schedule_add`, `schedule_list`, `schedule_rm`, `stats_list`, `stats_show`, `audit_tail`, `settings_get`. Transport decided in settings: new `farmhand` block with `farmhand_transport = "stdio" | "https" | "both"` (default `"both"`) and `farmhand_https_path = "/mcp"` (`extra = "forbid"` stays; `public_dict()` exposes it — no secrets). Stdio via `tc farmhand serve --transport stdio`, gated by the 0600 daemon socket (same-machine agents only). HTTPS as streamable-HTTP at `/mcp` served from the coop process on the existing listener, behind the same bearer middleware as `/api/*` (loopback-open rule included). MCP scope is narrower than the GUI bearer: no `auth_*` mutations, no `retention_sweep`, settings read-only.

**Blocked by:** 22 (the tool surface includes `run_stop` / `run_rerun`); specs 12–19 landed (coop listener, bearer, settings, socket ops)

**Status:** done (2026-11-14)

- [x] `tc farmhand serve --transport stdio` speaks MCP over stdio; a real MCP client (the SDK's) can call every listed tool against a running daemon and get the same payloads as the socket ops
- [x] HTTPS: streamable-HTTP at `/mcp` on the coop listener; bearer required when bound non-loopback (401 without), loopback open like `/api/*`; 200 + MCP responses with the coop bearer
- [x] Settings: `farmhand` block parses with `extra = "forbid"`; `farmhand_transport` enum enforced; `public_dict()` exposes it; `"both"` serves stdio and HTTPS simultaneously
- [x] Narrower MCP scope: MCP-exposed tools exclude auth mutations, `retention_sweep`, and settings mutation; `settings_get` is read-only over MCP
- [x] Secrecy: no MCP tool result ever contains token/seat/device-code material (spec 09/19 rule); the bearer never appears in any tool payload
- [x] Tests: MCP tool → socket-op mapping through a real MCP client over stdio against `tc farmhand serve`, and over HTTPS against a coop subprocess with bearer 401/200 checks; pure seam for the settings block; full suite green (362 passed)
- [x] `STATUS.md` updated at the slice boundary
