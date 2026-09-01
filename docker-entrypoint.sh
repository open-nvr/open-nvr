#!/bin/bash
set -e

# Fix permissions on mounted volumes (run as root)
# The shared_frames volume may be mounted with root ownership
# We need to ensure opennvr user can write to it
if [ -d "/app/AI-adapters/AIAdapters/frames" ]; then
    echo "Fixing permissions on frames directory..."
    chown -R opennvr:opennvr /app/AI-adapters/AIAdapters/frames 2>/dev/null || true
fi

# The opennvr_jwt_keys volume may be mounted with root ownership on first
# use — the backend (running as opennvr) must be able to write the
# MediaMTX signing keypair there.
if [ -d "/app/keys" ]; then
    chown -R opennvr:opennvr /app/keys 2>/dev/null || true
fi

# KAI-C adapter-registration state volume (#371) — first mount is
# root-owned; KAI-C (running as opennvr under supervisord) must be able
# to write its receipts file there or persistence silently degrades to
# the old restart-amnesia behaviour (it WARNs, but still).
if [ -d "/app/kai-c-state" ]; then
    chown -R opennvr:opennvr /app/kai-c-state 2>/dev/null || true
fi

# Recordings tree: MediaMTX used to run as root, so segment dirs it created
# under the shared mount were root-owned — unlinkable by the backend
# (uid 1000), which broke retention aging and the camera hard-delete purge
# (#243). MediaMTX now runs as uid 1000 too (docker-compose user:), so new
# files are fine; this migrates whatever an older stack left behind. Scoped
# to files NOT already owned by opennvr, so on a healthy tree it is a
# metadata-only scan, not a full re-chown.
if [ -d "/app/recordings" ]; then
    echo "Fixing ownership of non-opennvr files in recordings tree..."
    find /app/recordings ! -user opennvr -exec chown opennvr:opennvr {} + 2>/dev/null || true
fi

# ──────────────────────────────────────────────────────────────────────
# ISSUE-29: Surface the first-time setup token banner to docker logs.
# ──────────────────────────────────────────────────────────────────────
# Supervisord redirects the backend's stdout to a log file
# (/app/logs/opennvr-backend.log) — see supervisord.conf
# [program:opennvr-backend] stdout_logfile. As a result, print()
# output from server/services/first_time_setup_service.py:maybe_arm()
# never reaches the container's stdout (PID 1), which is what
# `docker compose logs opennvr-core` reads.
#
# start.sh's print_first_time_setup_token() greps docker logs for
# the banner. Without this forwarder the banner IS minted correctly
# in the DB-pending-admin case but is invisible to the operator —
# start.sh prints "First-time setup is already complete" even when
# the token is sitting unread in the backend log file.
#
# The background tail follows the backend log from EOF and, on every
# line matching the banner header, prints it plus the next 6 lines to
# stdout. This matches start.sh's grep contract:
#     grep -A 6 "first-time setup token" | tail -7
# so what surfaces here is exactly the 7-line block start.sh expects
# to read and forward to the operator's terminal.
(
    mkdir -p /app/logs
    touch /app/logs/opennvr-backend.log
    chown opennvr:opennvr /app/logs/opennvr-backend.log
    tail -n 0 -F /app/logs/opennvr-backend.log \
      | grep --line-buffered -A 6 "first-time setup token"
) &

# Switch to opennvr user and run supervisord
exec gosu opennvr /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf

