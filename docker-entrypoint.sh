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

# Apps-bus users file (opennvr_nats_auth volume, shared with nats-apps).
# nats-apps runs as root and creates the directory root:root 755 when it
# seeds users.conf, so core (uid opennvr) could never replace the seed
# with the real per-app users: every app's own NATS user stayed unknown
# to the bus and it refused them all ("authentication error - User
# <app>"). Core writes atomically (tempfile + rename in this directory),
# so it needs to own the directory, not just the file.
if [ -d "/var/lib/opennvr/nats" ]; then
    chown -R opennvr:opennvr /var/lib/opennvr/nats 2>/dev/null || true
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

# ──────────────────────────────────────────────────────────────────────
# #547: Surface startup failures to docker logs.
# ──────────────────────────────────────────────────────────────────────
# The forwarder above solves ONE line of output. Everything else a
# process writes to stderr — including the traceback that kills it —
# lands in a file inside the container and reaches nobody.
#
# That is not hypothetical. In #547 the backend died at import on every
# install: `docker logs opennvr_core` showed only supervisord restarting
# it in a loop, the container reported "Started", and the published port
# still accepted TCP because Docker binds it whether or not anything is
# listening. The actual ValidationError sat in
# /app/logs/opennvr-backend-error.log, which an operator has to already
# know about to look in. The reporter found it; most would have filed
# "doesn't work" or walked away.
#
# The project had noticed twice and worked around it twice rather than
# fixing it — tests/e2e/harness/evidence.py collects these files
# specially because "grepping core's Docker output returns nothing,
# every time", and tests/host-hardening/ carries the banner forwarder
# above. Two workarounds for one missing pipe.
#
# Volume is not a concern, which is worth stating because it is the
# obvious objection. uvicorn's LOGGING_CONFIG sends the ACCESS logger to
# stdout and only the default logger — startup, warnings, tracebacks —
# to stderr. So this carries the lines an operator needs and not one
# line per HTTP request.
#
# The files stay exactly as they were: this tees, it does not move.
# evidence.py still collects them, supervisord still rotates them.
for _log in /app/logs/opennvr-backend-error.log /app/logs/kai-c-error.log; do
    _tag="$(basename "$_log" -error.log)"
    (
        mkdir -p /app/logs
        touch "$_log"
        chown opennvr:opennvr "$_log"
        tail -n 0 -F "$_log" | sed -u "s|^|[$_tag] |"
    ) &
done

# Switch to opennvr user and run supervisord
exec gosu opennvr /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf

