# Running TurboChickens in Docker — the `coop` container

The `coop` image packages the whole platform — coopd (the supervisor daemon),
the pecking order, the SQLite store, the append-only Headcount, and the
TurboChickens browser GUI — as one container. One container runs one coopd
process; with the shipped settings that same process also serves the GUI over
HTTP on port **8737**.
The image adds no UI of its own: it runs the same SPA the daemon has served
since spec 12 (`tc coop start` locally), installed from the same wheel.

Naming (spec 11, lexicon): the image and the daemon inside it are **coop** /
**coopd**; the GUI branded in the browser is **TurboChickens**. The register
comes from `docs/lexicon.md` — eggs, chickens, clutches, the rooster, the
perch, the headcount.

```
Dockerfile            # python:3.12-slim, pip install ., ships docker/settings.toml
docker/settings.toml  # default container settings → /etc/turbochickens/settings.toml
docker/compose.yaml   # compose example (build + named volume + published port)
```

## Build

From the repo root:

```sh
docker build -t coop .
```

The build context is kept minimal by `.dockerignore` (only `pyproject.toml`,
`src/`, and `docker/settings.toml` are sent). The image sets
`TURBOCHICKENS_SETTINGS=/etc/turbochickens/settings.toml`, so every `tc`
command inside the container — the CMD included — uses the shipped settings.

## Run

```sh
docker run --name coop -d -p 8737:8737 -v coop-data:/data coop
```

Then open <http://localhost:8737> and log in with the token from the logs
(see below). Equivalent one-liner from the spec:

```sh
docker run -p 8737:8737 -v coop-data:/data coop
```

## Compose

`docker/compose.yaml` is a ready example (builds from the repo root, publishes
8737, keeps state in the named volume `coop-data`):

```sh
docker compose -f docker/compose.yaml up -d --build
```

## The `/data` volume — the coop's memory

Everything stateful lives **only** under the data dir (`data_dir = "/data"` in
the shipped settings), and `/data` is the documented volume:

```
/data/daemon.sock          # the coopd JSON-lines IPC socket (0600)
/data/turbochickens.db     # job_specs, runs, results, schedules, stats, ...
/data/audit.jsonl          # the Headcount — append-only, kept forever
/data/results/<run_id>/    # the Nest Box — candled clutches (TTL: 6 months)
/data/quarantine/<run_id>/ # clutches that failed candling, kept for inspection
/data/uploads/<job>-<hash>/ # egg bundles uploaded from the browser
/data/seats/               # perch seats — auth sessions (0600)
/data/workspaces/          # per-chicken workspaces (ephemeral; the abattoir tears them down)
```

So replacing the container with the same volume keeps eggs, chickens, clutches
and crows — the socket is re-created on start (a stale one is reaped), the DB
and the Headcount are reopened in place, and persisted crows are re-registered.
"The coop remembers; the chickens don't."

You can drive the CLI inside a running container (the image's env already
points at the right settings):

```sh
docker exec coop tc status
docker exec coop tc schedule list
docker exec coop tc runs list
```

## Token handling (GUI auth)

The shipped settings bind `coop_host = "0.0.0.0"` with **no** configured
`coop_auth_token`, so the GUI is never unauthenticated on a published port:

- At startup coopd mints an **ephemeral token** and prints it **exactly
  once** to stdout:

  ```sh
  docker logs coop | grep "coop auth token:"
  ```

  Paste it into the GUI login card. The browser sends it as
  `Authorization: Bearer …` and keeps it in `sessionStorage` — no cookies, so
  no CSRF surface. The token is never written to the Headcount, the status
  payload, or the DB.
- `GET /api/ping` stays **unauthenticated** (it is the healthcheck); every
  other `/api/*` route requires the bearer.
- To pin a fixed token instead of the ephemeral one, mount your own settings
  file over `/etc/turbochickens/settings.toml` (or set
  `TURBOCHICKENS_SETTINGS` to a mounted path) with `coop_auth_token = "…"`.
- Loopback-only deployments may set `coop_host = "127.0.0.1"` with no token **and**
  `coop_loopback_open = true` — then the GUI is open on the loopback bind (the one
  deliberate opening in the Fence). Without that opt-in the bearer is required on
  every bind, loopback included, so any local user on a shared host is locked out.
  That is a local-dev choice; the default is the safe one.

## Healthcheck

The image declares:

```dockerfile
HEALTHCHECK ... CMD python -c "... urllib.request.urlopen('http://127.0.0.1:8737/api/ping') ..."
```

hitting `GET /api/ping` **on loopback** (coopd binds `0.0.0.0`, so
loopback answers). `docker ps` shows `healthy` once the GUI is up; the
healthcheck fails (503) if coopd's socket is unreachable. Note: if you
override `coop_port` in a mounted settings file, override the healthcheck
port to match.

## Overriding settings

`TURBOCHICKENS_SETTINGS=/etc/turbochickens/settings.toml` is set in the image,
so the cleanest override is a bind mount over that file:

```sh
docker run -p 8737:8737 \
  -v coop-data:/data \
  -v $PWD/my-settings.toml:/etc/turbochickens/settings.toml \
  coop
```

with e.g. `coop_auth_token = "…"`, a different `coop_port`, or
`results_retention_months`. Provider presets can live at
`/data/providers.toml` (the data dir is already on the providers resolution
path). Unknown keys are rejected (`extra = "forbid"`), same as everywhere.

## Reverse proxy / TLS

TLS termination is **out of scope** for the image — that is a proxy's job.
Put nginx, Caddy, Traefik, etc. in front, terminate TLS there, and proxy to
the container's published `8737`. The bearer token still protects every
`/api/*` route end to end; only `/api/ping` is open, and you can restrict it
at the proxy (e.g. allow only the Docker host's healthcheck source) if you do
not need external probes.

## Manual smoke (run where a Docker daemon is available)

The repo's test environment has no Docker daemon, so the image itself is
verified by these manual steps; everything the container runs is covered by
the in-process tests (tickets 12–17 plus `tests/test_coop_container.py`, since
the CMD is just `tc daemon start` with the shipped settings):

```sh
docker build -t coop .
docker run --name coop -d -p 8737:8737 -v coop-data:/data coop

# 1. GUI is served; healthcheck goes green
docker ps                                   # coop: (healthy)
curl -s http://localhost:8737/api/ping      # 200, unauthenticated
curl -s http://localhost:8737/ | grep -o TurboChickens

# 2. ephemeral token printed exactly once; /api/* require it
docker logs coop | grep -c "coop auth token:"   # 1
TOKEN=$(docker logs coop | sed -n 's/^coop auth token: //p')
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8737/api/status          # 401
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8737/api/status        # 200

# 3. one full flow over HTTP (upload a job bundle, run it, read the result),
#    or via the browser at http://localhost:8737

# 4. state survives container replacement on the same volume
docker exec coop tc schedule list               # after adding a crow
docker rm -f coop
docker run --name coop -d -p 8737:8737 -v coop-data:/data coop
docker exec coop tc schedule list               # same crow
docker exec coop tc runs list                   # same chickens
docker logs coop | grep -c "coop auth token:"   # 1 again (new ephemeral token)
```

## What the container runs

`CMD ["tc", "daemon", "start"]` with the shipped settings — i.e. exactly
`tc daemon start` with `coop_enabled = true`, `coop_host = "0.0.0.0"`,
`coop_port = 8737`, `data_dir = "/data"`. No extra process, no second coopd,
no extra UI: the GUI is served by coopd itself in the same event loop,
and every GUI action is a thin mirror of the existing Unix-socket ops.
