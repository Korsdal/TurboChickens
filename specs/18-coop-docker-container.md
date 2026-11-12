# 18: the coop Docker container

**What to build:** the `coop` image. A `Dockerfile` on `python:3.12-slim` installs the package and ships a default settings file (`data_dir = "/data"`, `coop_enabled = true`, `coop_host = "0.0.0.0"`, `coop_port = 8737`); CMD starts the daemon, which serves the TurboChickens GUI. `EXPOSE 8737`; `HEALTHCHECK` hits `GET /api/ping` on loopback; `/data` documented as the volume (socket, db, audit log, results, quarantine, uploads — everything stateful lives only under the data dir, so state survives container replacement). A compose example: `docker run -p 8737:8737 -v coop-data:/data coop`.

**Design:** `specs/00-design-system.md` (the served GUI is the designed app; the container adds no UI of its own). Naming per spec 11 as corrected: image/daemon = **coop**, GUI brand = **TurboChickens**.

**Blocked by:** 12 (the coop-serving daemon must exist; the full-flow smoke is meaningful once 14–15 have landed)

**Status:** done (2026-11-12)

- [x] Image builds; `docker run -p 8737:8737 -v coop-data:/data coop` serves the TurboChickens GUI, prints the ephemeral auth token exactly once at startup, healthcheck green
- [x] Replacing the container with the same volume keeps jobs, runs, results and schedules (restart persistence over the socket/DB in /data)
- [x] Docs: build, run, compose, token handling, and a reverse-proxy note (TLS termination is out of scope, a proxy's job)
- [x] No Docker daemon exists in the test environment: the manual smoke steps are documented, and everything the container runs is covered by the in-process tests from tickets 12–17

**Landed as:** `Dockerfile` (python:3.12-slim, `pip install .`, ships `docker/settings.toml` → `/etc/turbochickens/settings.toml` via `TURBOCHICKENS_SETTINGS`, `VOLUME ["/data"]`, `EXPOSE 8737`, `HEALTHCHECK` on loopback `GET /api/ping`, `CMD ["tc", "daemon", "start"]`), `.dockerignore`, `docker/settings.toml` (data_dir=/data, coop 0.0.0.0:8737, no fixed token), `docker/compose.yaml` example, `docs/coop-container.md` (build/run/compose/token/reverse-proxy/manual smoke), `tests/test_coop_container.py` (6 tests). Verified with a real Docker daemon on 2026-11-12: image built, container healthy, GUI served, token printed exactly once per start, 401/200 bearer auth, upload→run→result and schedule over HTTP, and full state (jobs/runs/results/schedules) surviving `docker rm -f` + re-run on the same `coop-data` volume.
