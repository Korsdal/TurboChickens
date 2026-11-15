# coop — the TurboChickens GUI container (specs 11 + 18; hardened in spec 32/G12,
# supply-chain pinned in spec 32 ticket 16 / G22 + G23).
#
#   docker build -t coop .
#   docker run -p 127.0.0.1:8737:8737 -v coop-data:/data coop
#   # hardened (same as docker/compose.yaml):
#   docker run -p 127.0.0.1:8737:8737 -v coop-data:/data \
#     --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true \
#     --read-only --tmpfs /tmp --init --memory 1g --cpus 2 coop
#
# The port is published on the host's loopback only (127.0.0.1:8737), exactly
# like docker/compose.yaml — expose it on all interfaces only deliberately,
# behind a TLS reverse proxy.
#
# One container runs one daemon process; with the shipped settings
# (coop_enabled = true, coop_host = "0.0.0.0") that same process serves the
# TurboChickens GUI on 8737. All state lives under /data (the volume), so
# container replacement with the same volume loses nothing.
#
# Supply chain (G22): the base image is pinned by digest (no floating tag), the
# installer uv is pulled by digest from ghcr.io, dependencies are installed
# ONLY from the hash-pinned uv.lock (`uv export --frozen` + `--require-hashes`),
# and the build backend is pinned in pyproject.toml ([build-system]
# hatchling==…). The image therefore cannot drift from the dependency graph the
# test suite runs against; CI diffs the two (tests/test_ci.py +
# tests/test_supply_chain.py) and runs `uv audit` on the lock.
#
# The container never runs as root (G12): the image switches to the
# unprivileged `coop` user below, /data is owned by that user, and the
# runtime hardening (all capabilities dropped, no-new-privileges, read-only
# root filesystem with a tmpfs /tmp, an init process reaping PID 1, and
# memory/CPU limits) is applied by docker/compose.yaml — or by the matching
# `docker run` flags above. A coop compromise therefore yields neither root
# nor host OOM.

# Base image pinned by digest — python:3.12-slim (multi-arch OCI index).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# Pinned installer: uv itself comes from a digest-pinned ghcr.io image, so the
# build tool cannot drift either.
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 \
    /uv /usr/local/bin/uv

# Unprivileged runtime user: no login shell, no home — the daemon needs
# neither, and with a read-only rootfs nothing outside /data and /tmp is
# writable anyway.
RUN groupadd --system --gid 10001 coop \
 && useradd --system --uid 10001 --gid 10001 \
      --home-dir /nonexistent --shell /usr/sbin/nologin --no-create-home coop

# Unbuffered stdout so the one-time ephemeral GUI auth token appears in
# `docker logs` immediately; no bytecode writes so the read-only rootfs
# never blocks the interpreter; settings come from the file shipped below.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TURBOCHICKENS_SETTINGS=/etc/turbochickens/settings.toml

WORKDIR /app

# Dependencies come ONLY from the hash-pinned lock: uv.lock is part of the
# build context (see .dockerignore) and is exported frozen with hashes, then
# installed with hash checking enforced — anything not in the lock, or a
# different build of a locked package, fails the build.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project \
        --format requirements.txt --output-file /tmp/requirements.txt \
 && uv pip install --system --require-hashes --no-cache \
        --python /usr/local/bin/python -r /tmp/requirements.txt

# The project itself is built from source by the pinned build backend
# (hatchling==1.33.0 in pyproject.toml [build-system]); its dependencies are
# already installed above, so --no-deps. The wheel ships the SPA
# (src/turbochickens/coop_static) — the container adds no UI of its own.
# This layer depends on src/ only, so dependency layers cache independently
# of source edits.
COPY src/ src/
RUN uv pip install --system --no-deps --no-cache --python /usr/local/bin/python .

# Default container settings: data_dir=/data, coop on 0.0.0.0:8737, no fixed
# auth token (an ephemeral one is minted and printed once at startup).
COPY docker/settings.toml /etc/turbochickens/settings.toml

# Everything stateful — socket, db, audit log, results, quarantine, uploads,
# seat sessions — lives only under the data dir. /data is owned by the
# runtime user (a named volume inherits the image directory's ownership), so
# the non-root daemon can create its socket, DB and workspaces there while
# the root filesystem stays read-only at run time.
RUN mkdir -p /data && chown coop:coop /data
VOLUME ["/data"]

# The coop runs unprivileged from here on.
USER coop

EXPOSE 8737

# /api/ping is the unauthenticated healthcheck; hit it on loopback.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8737/api/ping', timeout=2).status == 200 else 1)"

# Exec form: SIGTERM goes straight to the daemon, which shuts down gracefully
# (coop HTTP torn down, socket unlinked, in-flight runs bounded). With
# compose `init: true` (docker run --init) PID 1 is tini, which reaps
# orphaned children instead of letting the daemon be PID 1 un-reaped.
CMD ["tc", "daemon", "start"]
