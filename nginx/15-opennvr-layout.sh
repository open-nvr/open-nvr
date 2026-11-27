#!/bin/sh
# Picks nginx's layout at container start (issue #925).
#
# NGINX_SINGLE_PORT_WEBRTC=on (default)
#     Port 443 is a stream layer (stream.conf.template): TLS goes to the web
#     server, anything else (a browser's ICE-TCP check) to MediaMTX's ICE
#     port, so WebRTC works wherever the page loads. The web server is
#     opennvr.conf with its 443 listeners moved to 127.0.0.1:8443 behind the
#     PROXY protocol, so it still sees each client's real address.
# NGINX_SINGLE_PORT_WEBRTC=off
#     opennvr.conf as it is: the web server listens on 443 directly.
#
# Whatever "on" renders is checked with `nginx -t` before nginx starts. If it
# fails, the "off" layout goes in instead and the reason is logged: a bad
# render must never take the web UI down on a live site.
#
# Run by compose before the image's own entrypoint (docker-compose.yml), so
# it needs no exec bit on the mounted file. Idempotent: a container restart
# runs it again over its own output.
set -u

SRC=/opt/opennvr-nginx
MAIN=/etc/nginx/nginx.conf
STOCK=/etc/nginx/nginx.conf.opennvr-stock
STREAM_DIR=/etc/nginx/stream.d
SITE=/etc/nginx/conf.d/default.conf
TAG="opennvr-layout:"

MODE="${NGINX_SINGLE_PORT_WEBRTC:-on}"
ICE_PORT="${WEBRTC_ICE_PORT:-8189}"
case "$ICE_PORT" in ''|*[!0-9]*) ICE_PORT=8189 ;; esac

# The image's own nginx.conf, kept once: every later render starts from it.
[ -f "$STOCK" ] || cp "$MAIN" "$STOCK"

direct() {
    cp "$STOCK" "$MAIN"
    rm -f "$STREAM_DIR/opennvr.conf"
    cp "$SRC/opennvr.conf" "$SITE"
}

if [ "$MODE" != "on" ]; then
    direct
    echo "$TAG web server on 443 directly (NGINX_SINGLE_PORT_WEBRTC=$MODE)"
    exit 0
fi

mkdir -p "$STREAM_DIR"
{
    cat "$STOCK"
    printf '\nstream {\n    include %s/*.conf;\n}\n' "$STREAM_DIR"
} > "$MAIN"
sed "s/\${WEBRTC_ICE_PORT}/$ICE_PORT/g" "$SRC/stream.conf.template" > "$STREAM_DIR/opennvr.conf"
# The web server moves behind the stream layer: the 443 listeners become one
# loopback listener that takes the PROXY header and trusts it for the
# client's address. Everything else in opennvr.conf is used as it is.
sed -e 's|^\( *\)listen 443 ssl default_server;|\1listen 127.0.0.1:8443 ssl proxy_protocol default_server;\
\1set_real_ip_from 127.0.0.1;\
\1real_ip_header proxy_protocol;|' \
    -e '/^ *listen \[::\]:443 ssl default_server;/d' \
    "$SRC/opennvr.conf" > "$SITE"

problem=""
if ! grep -q 'listen 127.0.0.1:8443 ssl proxy_protocol' "$SITE"; then
    problem="opennvr.conf has no 'listen 443 ssl default_server;' line to move"
elif ! out=$(nginx -t 2>&1); then
    problem="$out"
fi
if [ -n "$problem" ]; then
    direct
    echo "$TAG SINGLE-PORT WEBRTC DISABLED, web server on 443 directly: $problem" >&2
    exit 0
fi
echo "$TAG WebRTC over 443 on (web server behind the stream layer; ICE port $ICE_PORT)"
