# coop — the TurboChickens GUI container (specs 11 + 18; hardened in spec 32/G12).
#
#   docker build -t coop .
#   docker run -p 8737:8737 -v coop-data:/data coop
#   # hardened (same as docker/compose.yaml):
#   docker run -p 8737:8737 -v coop-data:/data \
#     --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true \
#     --read-only --tmpfs /tmp --init --memory 1g --cpus 2 coop
#
# One container runs one daemon process; with the shipped settings
# (coop_enabled = true, coop_host = "0.0.0.0") that same process serves the
# TurboChickens GUI on 8737. All state lives under /data (the volume), so
# container replacement with the same volume loses nothing.
#
# The container never runs as root (G12): the image switches to the
# unprivileged `coop` user below, /data is owned by that user, and the
# runtime hardening (all capabilities dropped, no-new-privileges, read-only
# root filesystem with a tmpfs /tmp, an init process reaping PID 1, and
# memory/CPU limits) is applied by docker/compose.yaml — or by the matching
# `docker run` flags above. A coop compromise therefore yields neither root
# nor host OOM.

FROM python:3.12-slim

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

# Install the package before copying anything else so dependency layers
# cache independently of source edits. The wheel ships the SPA
# (src/turbochickens/coop_static) — the container adds no UI of its own.
COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

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
