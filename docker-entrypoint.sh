#!/bin/bash
set -e

# Fix permissions on mounted volumes (run as root)
# The shared_frames volume may be mounted with root ownership
# We need to ensure opennvr user can write to it
if [ -d "/app/AI-adapters/AIAdapters/frames" ]; then
    echo "Fixing permissions on frames directory..."
    chown -R opennvr:opennvr /app/AI-adapters/AIAdapters/frames 2>/dev/null || true
fi

# ISSUE-FIX: Surface the first-time setup token to docker logs.
# Supervisord redirects backend stdout to a file, so it doesn't reach docker logs.
# We run a background tail that filters for the token and prints it to the container's stdout.
(
    mkdir -p /app/logs
    touch /app/logs/opennvr-backend.log
    chown opennvr:opennvr /app/logs/opennvr-backend.log
    tail -n 0 -F /app/logs/opennvr-backend.log | grep --line-buffered -A 10 "first-time setup token"
) &


# Switch to opennvr user and run supervisord
exec gosu opennvr /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf

