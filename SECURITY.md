# Security

TurboChickens is a local-first platform for ephemeral, just-in-time AI agent jobs: one
always-on daemon (`coopd`) plus the `tc` CLI, a Coop GUI, and a Farmhand MCP surface.
This document describes the trust boundary (the Fence), what the Fox enforces, how
secrets are handled, and how to report a vulnerability. Every claim below was verified
against the code at version 0.0.24; the relevant module is named inline.

## Reporting a vulnerability

Report privately through a **GitHub security advisory** on
[Korsdal/TurboChickens](https://github.com/Korsdal/TurboChickens) (Security tab →
"Report a vulnerability"). Do not open a public issue for security reports.

Include:

- the affected version or commit, and the surface you used (coopd unix socket, Coop
  HTTP API, Farmhand MCP endpoint, the container);
- a reproduction: the op or request, the settings involved (e.g. `coop_host`,
  `coop_loopback_open`), and what you expected the Fence to refuse;
- the impact you see — which boundary was crossed (peer-identity, path containment,
  secret disclosure, resource exhaustion).

Never paste into a report: bearer tokens, seat sessions, gate secrets or their `$VAR`
refs' resolved values, OAuth device codes, or raw Headcount (audit) rows. Redact them
before attaching anything.

## Threat model: the Fence

The Fence is the trust boundary the Fox watches. There are exactly two operator
surfaces, and both mean **Operator**:

- the **coopd unix socket**, created `0600` at bind (`src/turbochickens/daemon.py:553`),
  with the kernel's peer credentials read via `SO_PEERCRED` on **every** connection
  (`daemon.py:332-344`) — pid, uid, gid of the connecting process;
- the **Coop HTTP API**, where a bearer token is required on every bind unless the
  operator explicitly opts out with `coop_loopback_open = true` on a loopback bind
  with no token configured (`src/turbochickens/coop.py:290-296`).

A **Chicken is not an operator**. The daemon records each chicken's pid at hatch and
resolves the `SO_PEERCRED` peer pid to the owning chicken on every connection. On a
chicken-owned connection:

- only the narrowed sub-chicken op set is served — `enqueue`, the read ops, the Beak's
  `workspace_peek`, and the guarded `run_stop` (`CHICKEN_OPS_ALLOWED`,
  `daemon.py:293`). Operator-only ops — shutdown, settings, gates, auth, upload,
  re-run, schedule, retention, Farmhand info — are refused server-side before any
  handler runs, with a field-named error. The refusal string (an f-string in
  `daemon.py:317-321`) reads, byte-exact:

  > `the fox refuses op {op!r} — a chicken is not an operator: this connection belongs to chicken {caller.run_id!r}, and coopd serves a chicken-owned connection only the sub-chicken op set (enqueue + read ops + the guarded run_stop)`

- lineage is forced from the authenticated peer: on `enqueue` the daemon sets
  `parent_run_id` from the peer and ignores any client-sent value; `run_stop` gets
  `caller_run_id` the same way and refuses targets that are the caller's ancestors
  (`daemon.py:985` for `enqueue` lineage; `daemon.py:1645-1659` for `run_stop`);
- `spec_path` on `enqueue`, `run_rerun`, and `schedule_add` is allowlisted to the
  Uploads area plus already-registered egg paths (`_spec_path_allowlisted`,
  `daemon.py:646-666`). The check only resolves paths — it never stats, opens, or
  reads the target — so a refused path answers identically whether or not the host
  file exists: `spec_path` is never a host-file existence or read oracle. The refusal
  (`daemon.py:674-677`) reads, byte-exact:

  > `spec_path: the fox refuses — a chicken is not an operator: only registered egg paths inside the coop uploads area can be queued, re-run, or scheduled over a chicken's connection; the fence holds`

Known limit, stated plainly: the spec's `resources.network` policy is **declared, not
enforced** (`network_policy: none` in `auth_access`) — enforcement is post-MVP. A
chicken's network egress is not fenced off by the platform today.

## What the Fox enforces

All of this lives in `src/turbochickens/fox.py`, `fence.py`, `coop.py`, and the cap
constants in `src/turbochickens/settings.py`.

**Restrictive permissions on all created state.** The daemon sets a restrictive umask
(`0o077`) at start, before any state is created — no umask window (`fox.py:49,56-62`,
applied in `run_daemon`). On top of that, every named piece of state gets an explicit
mode: dirs `0700`, files `0600` (`fox.py:52-53`), enforced with `chmod`/`fchmod` so
pre-existing looser files are tightened too. That covers the data dir, the socket
(`0600` at bind, `daemon.py:553`), the SQLite DB (`0600`, `storage.py:121`) and the
Headcount file (`0600`, `audit.py:105`), workspaces, injected config, uploads, and seat
sessions.

**Security headers on every response**, including error responses (`fox.py:41-44`,
applied in `coop.py:375`): `Content-Security-Policy: default-src 'self'`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`. The shipped SPA uses a
system font stack and local icons only — no runtime CDN, so nothing third-party is
fetched over plain http.

**JSON-only bodies.** A request body is accepted only as `application/json`
(`coop.py:649-655`); a CSRF-style `text/plain` POST is refused:

> `the Fox only takes JSON delivered as application/json — this request arrived as {request.content_type!r}`

**Origin / Referer / Host validation** before auth or any handler runs
(`coop.py:474-509`): the `Host` header must name the coop's own host:port, and any
`Origin` or `Referer` whose host is not the coop's own is refused:

> `the Fox refuses this Host — the coop answers only to its own host:port`

> `the Fox refuses a foreign Origin — the coop serves only its own yard`

> `the Fox refuses a foreign Referer — the coop serves only its own yard`

Requests with no `Origin`/`Referer` at all (curl, the MCP client, the container
healthcheck) pass.

**Constant-time bearer compare.** The bearer is the sole network credential and is
compared with `hmac.compare_digest` (`coop.py:276-285`). Bearer is required on every
coop bind unless `coop_loopback_open` is set **and** the bind is loopback **and** no
token is configured (`coop.py:294`); when required and unset, an ephemeral token is
minted and printed once at startup, and it never appears in any payload, log, or
Headcount row.

**Bare pong.** `GET /api/ping` is the unauthenticated healthcheck and answers only
`{"ok": true, "op": "ping", "pong": true}` (`coop.py:578-587`) — never a version, pid,
uptime, or host path. A dead daemon socket answers the same bare shape with
`"pong": false` and status 503.

**Resource caps** (`settings.py:35-56`), enforced in the daemon and the coop:

| Cap | Constant | Value |
|---|---|---|
| Upload bundle (decoded) | `UPLOAD_MAX_BYTES` | 10 MiB |
| HTTP body backstop | `UPLOAD_MAX_HTTP_BODY` | ~15.3 MiB — 16,078,165 B (aiohttp `client_max_size`) |
| Socket stream (one JSON-lines request) | `SOCKET_STREAM_LIMIT` | ~16.3 MiB — 17,126,741 B |
| Coop connections in flight | `COOP_MAX_CONNECTIONS` | 32 |
| Coop handler time | `COOP_HANDLER_TIMEOUT_SEC` | 30 s |
| Coop idle keep-alive | `COOP_KEEPALIVE_TIMEOUT_SEC` | 30 s |
| Socket clients | `SOCKET_MAX_CONNECTIONS` | 16 (over-cap connections refused once, then closed — `daemon.py:686-696`) |
| Socket idle / handler time | `SOCKET_IDLE_TIMEOUT_SEC` / `SOCKET_HANDLER_TIMEOUT_SEC` | 300 s / 60 s |
| Stored run events per chicken | `MAX_RUN_EVENTS` | 5000 |
| Event line length | `RUN_EVENT_LINE_MAX_LEN` | 4000 |
| Log tail served | `RUNS_LOGS_TAIL_MAX` | 5000 lines |
| Clutch (harness output) collected/stored | `OUTPUT_MAX_BYTES` | 1 MiB, stored truncated with a marker |

## Secrets handling

- **Gate refs stay refs.** In `connectors.toml`, env/header values may be `$VAR` /
  `${VAR}` refs. Listings serve `env` as key names only and https `headers` as refs
  only — never values (`src/turbochickens/connectors.py`). A **literal** (non-ref)
  https header value is refused at registration, fail closed, with a field-named
  `connectors.<name>.headers` error (`connectors.py:289-300`); the message names the
  header, never the value:

  > `header {literal[0]!r} must be an env ref ($VAR / ${{VAR}}) like "Bearer $TOKEN" — literal header secrets are refused at registration; the Fox keeps gate credentials out of every listing, the Gates view, and the Headcount`

  A literal already at rest in a hand-edited registry loads (the operator owns the
  `0600` file) but is served as `<withheld>` in every listing. Refs are resolved
  against the daemon environment only into the ephemeral per-run workspace config, and
  an unresolvable ref fails the run closed at provisioning.
- **Seat sessions** (the Perch) are stored `0600` under `<data_dir>/seats/`
  (`src/turbochickens/seat.py:156-167`).
- **Tokens, seat sessions, device codes, and provider/OAuth response bytes never
  reach logs, manifests, Headcount rows, or served payloads.** This is asserted with a
  configured token across socket, HTTP, and both Farmhand transports, and against the
  audit log — see `tests/test_gate_secrecy.py`, `tests/test_provider_bytes_hygiene.py`,
  `tests/test_coop_bearer_default.py`, `tests/test_host_path_stripping.py`.
- **Per-run Scratch HOME.** Every chicken gets a scratch home `.tc-home/` inside its
  own workspace; `HOME`, `TMPDIR`, `XDG_*`, and `VIBE_HOME` all point there and only a
  sanitized vibe profile is materialized in it (`src/turbochickens/adapter.py:69`,
  `:268`). The chicken can neither read nor write the operator's real `$HOME`, and
  coopd's `/proc/<pid>/environ` is out of reach from the run. The workspace — Scratch
  included — is removed at teardown by the Abattoir.
- **No host paths in served payloads.** Socket and HTTP error strings and manifests
  are run through `redact_host_paths` (`daemon.py:793`, also 1589 and 2610); grep-guard tests in
  `tests/test_host_path_stripping.py` assert no host absolute paths in any socket or
  HTTP payload.

## Workspace containment

- Egg file refs (prompt, attachments, schema) are checked by the shared safe-ref rule
  in `src/turbochickens/fence.py` (`safe_ref`, `resolve_contained`,
  `contained_ref`): absolute, `..`-crossing, or backslashed refs are refused at the
  fence, inside the egg's own directory.
- The Beak (`src/turbochickens/beak.py`) locks peek paths with `safe_relpath` +
  `resolve_locked` (`beak.py:77-96`): `..`, absolute, and backslash paths are refused.
  Files are opened `O_NOFOLLOW` component by component from the workspace dir fd
  (`beak.py:164-180`), so a resolve→open race cannot serve anything outside the
  workspace. Peek targets that are symlinks, or whose inode has `nlink > 1` (a
  hardlink alias), are refused outright before a byte is read (`beak.py:163-206` for the
  `O_NOFOLLOW` walk, `beak.py:285` for the `nlink > 1` check).
- The chicken's **private config is refused by resolved target**: `.vibe/`, `.tc-home/`
  (the Scratch), and `.tc-farmhand-settings.toml` (`BEAK_PRIVATE_DIRS` /
  `BEAK_PRIVATE_FILES`,
  `beak.py:73-74`) — where resolved gate/env secrets live — so a chicken-planted
  symlink or hardlink alias into it resolves into private config and is refused too.
- The workspace tree is capped at 1000 entries (`WORKSPACE_PEEK_MAX_ENTRIES`,
  `beak.py:64`) and runs off the event loop; a single file peek serves at most 64 KiB
  (`WORKSPACE_PEEK_MAX_BYTES`, `beak.py:61`) with a `truncated` flag. Tree paths are
  workspace-relative, so no host path leaves the daemon.

## Uploads area rules

`jobs_upload` (`daemon.py:2337+`) accepts a bundle as `{"files": {path: base64}}` and
enforces, in order:

- every bundle path passes `_safe_bundle_relpath` — relative, inside the bundle, no
  absolute paths, no `..` (`daemon.py:403`);
- at most `UPLOAD_MAX_FILES` files, and at most 10 MiB decoded total
  (`UPLOAD_MAX_BYTES`, `settings.py:35`); over-cap answers `bundle too large`;
- the bundle must carry its own `schema_file` as a bundle member, checked before
  candling, so candling of an uploaded egg cannot be aimed at a host file;
- only a valid bundle is stored, under `uploads/<job>-<spec_hash>/`, created
  restrictive (dirs `0700`, files `0600`); invalid bundles are never stored and never
  register a job, and staging paths in error messages are rewritten to `<bundle>` so
  no host path leaves the daemon.

## Container and supply chain

- The image (`Dockerfile`) never runs as root: `USER coop` (uid/gid 10001, no login
  shell, no home). The base image is pinned **by digest**
  (`python:3.12-slim@sha256:5e2bdb1b…`, `Dockerfile:37`), the uv installer is pulled by
  digest from ghcr.io (`Dockerfile:41`), and dependencies are installed **only** from
  the hash-pinned `uv.lock` (`uv export --frozen` + `uv pip install
  --require-hashes`, `Dockerfile:64-68`); the build backend is pinned exactly in
  `pyproject.toml` `[build-system]`.
- `docker/compose.yaml` publishes the GUI port on the host's **loopback only**
  (`127.0.0.1:8737:8737`) and hardens the service: `cap_drop: ALL`,
  `no-new-privileges`, read-only root filesystem with a tmpfs `/tmp`, `init: true`,
  and memory/CPU limits. To expose the coop beyond loopback, do it deliberately and
  put a TLS reverse proxy in front — see [docs/coop-container.md](docs/coop-container.md).
- CI (`.github/workflows/ci.yml`) runs the full test suite on every push and pull
  request on Python 3.12 and 3.13 from the frozen lock, and a separate **audit** job
  runs `uv lock --check`, `uv audit`, and
  `pytest tests/test_ci.py tests/test_supply_chain.py`, which diff the image's
  dependency contract (digest-pinned base and uv, hash-checked lock install, pinned
  backend, `.dockerignore` exclusions, loopback-only publish, commented fixed-token
  mount) against the actual files. All Actions are pinned by commit SHA.

## Headcount tamper-evidence model

The Headcount (`audit.jsonl`, `src/turbochickens/audit.py`) is **append-only**: the
file is only ever opened in append mode, rows are never rewritten, truncated, or
deleted, and it is created `0600` and `fchmod`-ed on every append (`audit.py:93-105`).
Every row carries a UTC `ts` and an additive `via` actor (`socket`, `http`,
`farmhand`, `rooster`) stamped server-side from the verified connection — never taken
from a client-writable request body (`audit.py:38-52`, `daemon.py:800-804`).

The model is **cooperative-writers-only**: there is no hash chain or signature, so
tamper-evidence holds against readers and non-writers, not against a process that can
write the file (the daemon's owner). Rows written by older versions keep their legacy
names on disk forever; aliasing happens on read only.

## Retention and the Compost

TTLs live in settings (`src/turbochickens/settings.py:68-69`): the Headcount is kept
**forever** (`audit_retention` only accepts `"forever"` — the log is never
TTL-deleted), and result artifacts on disk expire after `results_retention_months`
(default 6). The sweep (`src/turbochickens/retention.py`) deletes only the on-disk
artifacts (`results/<run_id>/`, quarantine), audit-logs every deletion as
`clutch.composted` (legacy `result.expired` rows are aliased on read only,
`audit.py:65`), and leaves the append-only DB rows in place; an expired clutch is
never served (`results_get` answers `expired`). The sweep runs automatically at
daemon start (`daemon.py:524,1874`) and on demand via the `retention_sweep` op.

## SBOM

The software bill of materials is [docs/sbom.cdx.json](docs/sbom.cdx.json) — CycloneDX
1.5 JSON, one component per `[[package]]` entry in `uv.lock` (61 at version 0.0.24),
with purls and the lock's sha256 hashes. Regenerate it from the repo root with:

```bash
.venv/bin/python docs/make_sbom.py
```

The script is stdlib-only, needs no network, derives a deterministic serial from the
sha256 of `uv.lock`, and validates the BOM it writes (`docs/make_sbom.py`).
