#!/usr/bin/env bash
# The nginx layout script (nginx/15-opennvr-layout.sh, issue #925).
#
# Port 443 can carry WebRTC media next to the web UI: a stream layer reads
# the first bytes and sends TLS to the web server (moved to 127.0.0.1:8443
# behind the PROXY protocol) and anything else to MediaMTX's ICE port. The
# script picks that layout or the old one at every container start, and a
# render that fails `nginx -t` must fall back to the old one: on a live site
# a bad render would otherwise take the web UI down.
#
# Runs the REAL script in the image compose uses, so it needs docker.
# Usage: test_nginx_layout.sh [certs-dir]   (a dir with server.crt/.key;
# a throwaway one is made with openssl when omitted)
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running nginx layout tests"
echo ""

# ── Static: compose wiring (no docker needed) ──
compose="$REPO_ROOT/docker-compose.yml"
start_test "nginx mounts the ./nginx folder and runs the layout script first"
if grep -q -- '- ./nginx:/opt/opennvr-nginx:ro' "$compose" \
    && grep -qE '15-opennvr-layout.sh; .*exec /docker-entrypoint.sh' "$compose"; then pass
else fail "the nginx service must mount ./nginx at /opt/opennvr-nginx and run the script before the stock entrypoint"; fi

start_test "the loopback hops (8443/8445) are never published"
if grep -nE '"[^"]*:(8443|8445)(/tcp)?"' "$REPO_ROOT"/docker-compose*.yml | grep -v '^\s*#'; then
    fail "a compose file publishes a loopback-only nginx port"
else pass; fi

start_test "core's single-port switch follows nginx's (WEBRTC_SINGLE_PORT)"
if grep -q 'WEBRTC_SINGLE_PORT=${NGINX_SINGLE_PORT_WEBRTC:-on}' "$compose"; then pass
else fail "core must get WEBRTC_SINGLE_PORT from NGINX_SINGLE_PORT_WEBRTC"; fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo ""
    echo "  (docker not available: skipping the render tests)"
    echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
    [ "$TESTS_FAILED" -eq 0 ]
    exit $?
fi

IMAGE=$(awk '/^  nginx:/{f=1} f && /image:/{print $2; exit}' "$compose")
[ -n "$IMAGE" ] || IMAGE=nginx:1.27-alpine

WORK=$(mktemp -d)
if [ -z "$WORK" ] || [ ! -d "$WORK" ]; then
    echo "✗ mktemp -d failed" >&2
    exit 2
fi
trap 'rm -rf "$WORK"' EXIT

CERTS="${1:-}"
if [ -z "$CERTS" ]; then
    CERTS="$WORK/certs"
    mkdir -p "$CERTS"
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=layout-test \
        -keyout "$CERTS/server.key" -out "$CERTS/server.crt" >/dev/null 2>&1 \
        || { echo "✗ openssl could not make a test cert" >&2; exit 2; }
fi

# A host path as docker wants it (Git Bash on Windows: D:/... not /d/...).
host_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

# run_layout <nginx-src-dir> <env...> -- <shell commands after the script>
run_layout() {
    local src="$1"; shift
    local envs=()
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do envs+=(-e "$1"); shift; done
    shift
    MSYS_NO_PATHCONV=1 docker run --rm "${envs[@]}" \
        -v "$(host_path "$src"):/opt/opennvr-nginx:ro" -v "$(host_path "$CERTS"):/etc/nginx/certs:ro" \
        --entrypoint /bin/sh "$IMAGE" -c "/bin/sh /opt/opennvr-nginx/15-opennvr-layout.sh 2>&1; $*"
}

# ── 1. "on" (the default) ──
start_test "default layout: stream layer on 443, web server behind it, nginx -t passes"
out=$(run_layout "$REPO_ROOT/nginx" WEBRTC_ICE_PORT=18189 -- \
    'nginx -t 2>&1 | tail -1; tr -d "\r" < /etc/nginx/conf.d/default.conf | grep -E "listen|real_ip"; grep -c "^stream {" /etc/nginx/nginx.conf; grep "set \$opennvr_ice" /etc/nginx/stream.d/opennvr.conf')
if grep -q 'WebRTC over 443 on' <<<"$out" \
    && grep -q 'test is successful' <<<"$out" \
    && grep -q 'listen 127.0.0.1:8443 ssl proxy_protocol default_server;' <<<"$out" \
    && grep -q 'real_ip_header proxy_protocol;' <<<"$out" \
    && ! grep -qE 'listen (\[::\]:)?443 ssl' <<<"$out" \
    && grep -q 'mediamtx:18189' <<<"$out"; then pass
else fail "$out"; fi

# ── 2. "off" ──
start_test "NGINX_SINGLE_PORT_WEBRTC=off: opennvr.conf as it is, no stream layer"
out=$(run_layout "$REPO_ROOT/nginx" NGINX_SINGLE_PORT_WEBRTC=off -- \
    'nginx -t 2>&1 | tail -1; cmp /opt/opennvr-nginx/opennvr.conf /etc/nginx/conf.d/default.conf && echo SAME; grep -c "^stream {" /etc/nginx/nginx.conf')
if grep -q 'test is successful' <<<"$out" && grep -q SAME <<<"$out" && grep -qx 0 <<<"$out"; then pass
else fail "$out"; fi

# ── 3. A broken render falls back ──
start_test "a render that fails nginx -t falls back to the direct layout"
mkdir -p "$WORK/broken"
cp "$REPO_ROOT"/nginx/* "$WORK/broken/"
echo "this is not nginx;" >> "$WORK/broken/stream.conf.template"
out=$(run_layout "$WORK/broken" -- \
    'nginx -t 2>&1 | tail -1; cmp /opt/opennvr-nginx/opennvr.conf /etc/nginx/conf.d/default.conf && echo SAME; ls /etc/nginx/stream.d/ | wc -l')
if grep -q 'SINGLE-PORT WEBRTC DISABLED' <<<"$out" && grep -q 'test is successful' <<<"$out" \
    && grep -q SAME <<<"$out" && grep -qx 0 <<<"$out"; then pass
else fail "$out"; fi

# ── 4. Restart-safe ──
start_test "running it twice (a container restart) leaves one stream block"
out=$(run_layout "$REPO_ROOT/nginx" -- \
    '/bin/sh /opt/opennvr-nginx/15-opennvr-layout.sh >/dev/null 2>&1; grep -c "^stream {" /etc/nginx/nginx.conf; nginx -t 2>&1 | tail -1')
if grep -qx 1 <<<"$out" && grep -q 'test is successful' <<<"$out"; then pass
else fail "$out"; fi

# ── 5. Switching back to off after on (same container) ──
start_test "on, then off in the same container: back to the stock nginx.conf"
out=$(run_layout "$REPO_ROOT/nginx" -- \
    'NGINX_SINGLE_PORT_WEBRTC=off /bin/sh /opt/opennvr-nginx/15-opennvr-layout.sh >/dev/null; grep -c "^stream {" /etc/nginx/nginx.conf; ls /etc/nginx/stream.d/ | wc -l; nginx -t 2>&1 | tail -1')
if [ "$(grep -cx 0 <<<"$out")" -eq 2 ] && grep -q 'test is successful' <<<"$out"; then pass
else fail "$out"; fi

echo ""
echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
[ "$TESTS_FAILED" -eq 0 ]
