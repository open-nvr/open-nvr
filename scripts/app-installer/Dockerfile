# syntax=docker/dockerfile:1.7
# OpenNVR App Installer — the single privileged reconciler.
#
# This is the ONLY OpenNVR container that gets the Docker socket mounted
# (see docker-compose.installer.yml, profile: app-installer). It is NOT
# network-facing: it opens no listening ports and only talks to the DB
# (to read desired state) and the Docker socket (to apply it).
#
# It ships the Docker CLI + compose plugin so it can shell out to
# ``docker compose`` against the host daemon over the mounted socket.
#
# Build (from the repo root):
#   docker build -f scripts/app-installer/Dockerfile -t opennvr/app-installer:local .
#
# Opt in via the overlay (see docs/APPS_INSTALL.md):
#   docker compose -f docker-compose.yml -f docker-compose.installer.yml \
#       --profile app-installer up -d app-installer

FROM python:3.11-slim AS runtime

# Runs as root (the base image default — no USER directive). This is
# deliberate and required: the container talks to the host daemon over
# the bind-mounted /var/run/docker.sock, whose group ownership varies by
# host, so it needs root to open the socket reliably. This is the SINGLE
# privileged component in the stack, it is opt-in (only started via the
# app-installer overlay), and it is NON-network-facing (no EXPOSE, no
# listening ports) — its only reachable surfaces are the DB and the
# socket. See docs/APPS_INSTALL.md → "The single privileged mount".

# Docker CLI + compose plugin only (NOT the daemon) — the reconciler
# drives the HOST daemon through the mounted /var/run/docker.sock.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        docker.io \
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SQLAlchemy for the intent store; PyYAML for the baked curated index.
# Kept minimal on purpose.
RUN pip install --no-cache-dir \
        "sqlalchemy>=2,<3" "psycopg2-binary>=2.9" "PyYAML>=6.0,<7.0"

# cosign, for verifying catalog image signatures before an install
# (scripts/app-installer/signing.py). Taken from the Sigstore project's
# own multi-arch image, pinned; no network fetch at build time.
COPY --from=ghcr.io/sigstore/cosign/cosign:v2.4.1 /ko-app/cosign /usr/local/bin/cosign

COPY scripts/app-installer/reconciler.py ./reconciler.py
COPY scripts/app-installer/signing.py    ./signing.py
COPY scripts/app-installer/store.py      ./store.py
COPY scripts/app-installer/main.py       ./main.py

# The installer's TRUST ANCHOR: its own copy of the curated index, baked
# at build time (same release ⇒ same bytes as the server's copy). Intent
# rows from the DB are only selectors into this file — the image/digest
# that actually deploy come from here, so a compromised web app (which
# can write arbitrary intent rows) still cannot make the installer run
# an uncurated image. The installer REFUSES TO START without it.
COPY server/config/apps_index.yml ./apps_index.yml
ENV APPS_INDEX_PATH=/app/apps_index.yml

ENV PYTHONUNBUFFERED=1

# No EXPOSE — the installer is never network-facing.

ENTRYPOINT ["python", "main.py"]
