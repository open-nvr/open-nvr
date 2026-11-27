#!/bin/sh
# Reloads nginx when its certificate changes (built-in Let's Encrypt, the
# `acme` service, replaces /etc/nginx/certs/server.crt by rename).
#
# Started in the background by compose before nginx itself
# (docker-compose.yml). Checks once a minute; a reload is graceful (open
# connections finish on the old certificate), and is skipped when the new
# files do not pass `nginx -t`, so a half-written pair never breaks the
# running server.
set -u
CERT=/etc/nginx/certs/server.crt
TAG="opennvr-cert-watch:"

EVERY="${OPENNVR_CERT_WATCH_S:-60}"
case "$EVERY" in ''|*[!0-9]*) EVERY=60 ;; esac

mtime() { stat -c %Y "$CERT" 2>/dev/null || echo 0; }

last=$(mtime)
while :; do
    sleep "$EVERY"
    now=$(mtime)
    [ "$now" = "$last" ] && continue
    # Recorded either way: a broken pair is reported once, and the next
    # change (the fix) is a new mtime again.
    last=$now
    if out=$(nginx -t 2>&1); then
        nginx -s reload && echo "$TAG certificate changed; nginx reloaded"
    else
        echo "$TAG certificate changed but nginx -t fails; not reloading: $out" >&2
    fi
done
