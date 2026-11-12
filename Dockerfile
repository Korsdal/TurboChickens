# coop — the TurboChickens GUI container (specs 11 + 18).
#
#   docker build -t coop .
#   docker run -p 8737:8737 -v coop-data:/data coop
#
# One container runs one daemon process; with the shipped settings
# (coop_enabled = true, coop_host = "0.0.0.0") that same process serves the
# TurboChickens GUI on 8737. All state lives under /data (the volume), so
# container replacement with the same volume loses nothing.

FROM python:3.12-slim

# Unbuffered stdout so the one-time ephemeral GUI auth token appears in
# `docker logs` immediately; settings come from the file shipped below.
ENV PYTHONUNBUFFERED=1 \
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
# seat sessions — lives only under the data dir.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8737

# /api/ping is the unauthenticated healthcheck; hit it on loopback.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8737/api/ping', timeout=2).status == 200 else 1)"

# Exec form: SIGTERM goes straight to the daemon, which shuts down gracefully
# (coop HTTP torn down, socket unlinked, in-flight runs bounded).
CMD ["tc", "daemon", "start"]
