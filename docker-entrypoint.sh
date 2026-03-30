#!/bin/bash
set -e

# Fix permissions on mounted volumes (run as root)
# The shared_frames volume may be mounted with root ownership
# We need to ensure opennvr user can write to it
if [ -d "/app/AI-adapters/AIAdapters/frames" ]; then
    echo "Fixing permissions on frames directory..."
    chown -R opennvr:opennvr /app/AI-adapters/AIAdapters/frames 2>/dev/null || true
fi

# Switch to opennvr user and run supervisord
exec gosu opennvr /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
