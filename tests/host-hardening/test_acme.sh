#!/usr/bin/env bash
# Built-in Let's Encrypt (the `acme` service, scripts/acme/acme-loop.sh),
# end to end against Pebble, Let's Encrypt's test CA.
#
# Real HTTP-01 validation: Pebble resolves the test hostname to our nginx (a
# network alias) and fetches the token from port 80, which nginx serves from
# the shared webroot (nginx/opennvr.conf). Then the certificate lands in the
# certs dir, the self-signed pair is kept, nginx's cert watcher reloads and
# serves the new certificate, a second round changes nothing, and a round
# that fails leaves the installed certificate alone.
#
# Needs docker. Usage: test_acme.sh [certs-dir-with-a-self-signed-pair]
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running ACME (Let's Encrypt) tests"
echo ""

compose="$REPO_ROOT/docker-compose.yml"

# ── Static ──
start_test "port 80 serves only the ACME challenge path and redirects the rest"
conf=$(tr -d '\r' < "$REPO_ROOT/nginx/opennvr.conf")
if grep -q 'location ^~ /.well-known/acme-challenge/' <<<"$conf" \
    && grep -q 'root /var/www/acme;' <<<"$conf" \
    && ! awk '/listen 80 default_server/{f=1} f&&/^}/{exit} f' <<<"$conf" | grep -qE '^    return 301'; then pass
else fail "the 80 server must serve /.well-known/acme-challenge/ from /var/www/acme and redirect only inside location /"; fi

start_test "the acme service is opt-in (profile) and pinned"
if awk '/^  acme:/{f=1} f&&/^  [a-z]/&&!/^  acme:/{exit} f' "$compose" | grep -q 'profiles: \[acme\]' \
    && grep -qE 'image: goacme/lego:v[0-9]+\.[0-9]+\.[0-9]+' "$compose"; then pass
else fail "acme must sit behind profile 'acme' with a pinned lego tag"; fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo ""; echo "  (docker not available: skipping the Pebble tests)"
    echo "Ran $TESTS_RUN, failed $TESTS_FAILED"; [ "$TESTS_FAILED" -eq 0 ]; exit $?
fi

host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
export MSYS_NO_PATHCONV=1

NGINX_IMAGE=$(awk '/^  nginx:/{f=1} f && /image:/{print $2; exit}' "$compose")
LEGO_IMAGE=$(awk '/^  acme:/{f=1} f && /image:/{print $2; exit}' "$compose")
PEBBLE_IMAGE=ghcr.io/letsencrypt/pebble:latest
HOST=nvr.example.test
NET="opennvr-acme-test-$$"

WORK=$(mktemp -d)
if [ -z "$WORK" ] || [ ! -d "$WORK" ]; then echo "✗ mktemp -d failed" >&2; exit 2; fi
cleanup() {
    docker rm -f "acmetest-pebble-$$" "acmetest-nginx-$$" >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
    # Files written as root inside the containers: remove them from a container.
    docker run --rm -v "$(host_path "$WORK"):/w" alpine:3.20 rm -rf /w/acme /w/webroot /w/certs >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/acme" "$WORK/webroot" "$WORK/certs" "$WORK/cfg"

if [ -n "${1:-}" ]; then
    cp "$1/server.crt" "$1/server.key" "$WORK/certs/"
else
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=opennvr-ui \
        -keyout "$WORK/certs/server.key" -out "$WORK/certs/server.crt" >/dev/null 2>&1 \
        || { echo "✗ openssl could not make a self-signed pair" >&2; exit 2; }
fi
selfsigned_sum=$(cksum < "$WORK/certs/server.crt")

cat > "$WORK/cfg/pebble.json" <<'JSON'
{"pebble": {"listenAddress": "0.0.0.0:14000", "managementListenAddress": "0.0.0.0:15000",
  "certificate": "test/certs/localhost/cert.pem", "privateKey": "test/certs/localhost/key.pem",
  "httpPort": 80, "tlsPort": 5001, "ocspResponderURL": "", "externalAccountBindingRequired": false}}
JSON

docker network create "$NET" >/dev/null
docker run -d --name "acmetest-pebble-$$" --network "$NET" --network-alias pebble \
    -e PEBBLE_VA_NOSLEEP=1 -e PEBBLE_WFE_NONCEREJECT=0 \
    -v "$(host_path "$WORK/cfg"):/cfg:ro" "$PEBBLE_IMAGE" -config /cfg/pebble.json >/dev/null
docker run -d --name "acmetest-nginx-$$" --network "$NET" --network-alias "$HOST" \
    -e OPENNVR_CERT_WATCH_S=2 \
    -v "$(host_path "$REPO_ROOT/nginx"):/opt/opennvr-nginx:ro" \
    -v "$(host_path "$WORK/certs"):/etc/nginx/certs:ro" \
    -v "$(host_path "$WORK/webroot"):/var/www/acme:ro" \
    --entrypoint /bin/sh "$NGINX_IMAGE" \
    -c '/bin/sh /opt/opennvr-nginx/15-opennvr-layout.sh; /bin/sh /opt/opennvr-nginx/20-opennvr-cert-watch.sh & exec nginx -g "daemon off;"' >/dev/null
sleep 3

acme_round() {  # extra env... ; prints output, returns the script's status
    docker run --rm --network "$NET" \
        -e PUBLIC_HOSTNAME="$HOST" -e ACME_SERVER=https://pebble:14000/dir \
        -e ACME_TLS_SKIP_VERIFY=1 -e ACME_ONESHOT=1 "$@" \
        -v "$(host_path "$REPO_ROOT/scripts/acme"):/opt/opennvr-acme:ro" \
        -v "$(host_path "$WORK/acme"):/acme" \
        -v "$(host_path "$WORK/webroot"):/webroot" \
        -v "$(host_path "$WORK/certs"):/nginx-certs" \
        --entrypoint /bin/sh "$LEGO_IMAGE" /opt/opennvr-acme/acme-loop.sh 2>&1
}

served_subject() {
    docker run --rm --network "$NET" --entrypoint sh alpine/openssl:3.3.2 -c \
        "echo | openssl s_client -connect $HOST:443 -servername $HOST 2>/dev/null | openssl x509 -noout -issuer -ext subjectAltName 2>/dev/null"
}

# ── 1. Port 80 serves the challenge path only ──
start_test "nginx serves a token under /.well-known/acme-challenge/ and redirects the rest"
docker run --rm -v "$(host_path "$WORK/webroot"):/w" alpine:3.20 sh -c 'mkdir -p /w/.well-known/acme-challenge && echo tok123 > /w/.well-known/acme-challenge/t1' >/dev/null
tok=$(docker exec "acmetest-nginx-$$" wget -qO- http://127.0.0.1/.well-known/acme-challenge/t1 2>&1)
redir=$(docker exec "acmetest-nginx-$$" sh -c 'printf "GET /cameras HTTP/1.0\r\nHost: %s\r\n\r\n" "$0" | nc -w 3 127.0.0.1 80 | grep -i "^Location"' "$HOST")
if [ "$tok" = tok123 ] && grep -qi 'location: https://' <<<"$redir"; then pass
else fail "token='$tok' redirect='$redir'"; fi

# ── 2. First round: real HTTP-01 through nginx, certificate installed ──
start_test "first round obtains a certificate through nginx's port 80 and installs it"
out=$(acme_round); rc=$?
if [ "$rc" -eq 0 ] && grep -q "installed the certificate for $HOST" <<<"$out" \
    && [ "$(cksum < "$WORK/certs/server.crt")" != "$selfsigned_sum" ] \
    && [ -f "$WORK/certs/server.crt.selfsigned" ] && [ -f "$WORK/certs/server.key.selfsigned" ] \
    && [ "$(cksum < "$WORK/certs/server.crt.selfsigned")" = "$selfsigned_sum" ]; then pass
else fail "rc=$rc $(tail -5 <<<"$out")"; fi

# ── 3. nginx picks it up by itself ──
start_test "nginx's cert watcher reloads and serves the new certificate"
sleep 6
subj=$(served_subject)
if grep -qi 'pebble' <<<"$subj" && grep -q "DNS:$HOST" <<<"$subj" \
    && docker logs "acmetest-nginx-$$" 2>&1 | grep -q 'certificate changed; nginx reloaded'; then pass
else fail "served: $subj"; fi

# ── 4. Not due: nothing changes ──
start_test "a second round, not due, leaves the installed certificate untouched"
before=$(cksum < "$WORK/certs/server.crt")
out=$(acme_round); rc=$?
if [ "$rc" -eq 0 ] && [ "$(cksum < "$WORK/certs/server.crt")" = "$before" ] \
    && ! grep -q "installed the certificate" <<<"$out"; then pass
else fail "rc=$rc $(tail -5 <<<"$out")"; fi

# ── 5. A failing round keeps what is installed ──
start_test "an unreachable CA fails the round and keeps the installed certificate"
before=$(cksum < "$WORK/certs/server.crt")
out=$(acme_round -e ACME_SERVER=https://pebble:1/dir); rc=$?
if [ "$rc" -ne 0 ] && [ "$(cksum < "$WORK/certs/server.crt")" = "$before" ] \
    && grep -q "could not get a certificate" <<<"$out"; then pass
else fail "rc=$rc $(tail -5 <<<"$out")"; fi

# ── 6. Not a public name: nothing happens ──
start_test "an IP address as PUBLIC_HOSTNAME is refused without contacting the CA"
out=$(acme_round -e PUBLIC_HOSTNAME=192.168.1.10); rc=$?
if [ "$rc" -eq 0 ] && grep -q "is not a public DNS name" <<<"$out"; then pass
else fail "rc=$rc $(tail -3 <<<"$out")"; fi

echo ""
echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
[ "$TESTS_FAILED" -eq 0 ]
