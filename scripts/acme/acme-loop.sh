#!/bin/sh
# Keeps a Let's Encrypt certificate for PUBLIC_HOSTNAME in ./nginx-certs.
#
# Runs in the `acme` service (docker-compose.yml, profile "acme", which the
# launchers add when PUBLIC_HOSTNAME is set). Uses lego's HTTP-01 challenge
# through nginx: lego writes the token under the shared webroot and nginx
# serves /.well-known/acme-challenge/ on port 80 (nginx/opennvr.conf), so
# nothing but the existing 80 forward is needed.
#
# Each round `lego run` obtains the certificate, or renews it when it is due
# and does nothing otherwise. A new certificate is copied over
# ./nginx-certs/server.{crt,key} atomically; nginx's cert watcher
# (nginx/20-opennvr-cert-watch.sh) reloads it. The self-signed pair is kept
# once as server.{crt,key}.selfsigned, so the site can go back to it.
#
# --reuse-key keeps the same key across renewals: a site agent that pinned
# this server's key (cloud mode) keeps trusting it.
#
# Failure never touches the installed certificate. Let's Encrypt allows a
# handful of failed validations per hostname per hour, so a failing site
# waits an hour and more between tries (a DNS name not pointing here yet,
# port 80 not forwarded).
#
# Environment:
#   PUBLIC_HOSTNAME       the DNS name browsers use (required)
#   ACME_EMAIL            registration / expiry contact (optional)
#   ACME_SERVER           ACME directory (default: Let's Encrypt production)
#   ACME_DISABLE          true: do nothing (keep the self-signed certificate)
#   ACME_TLS_SKIP_VERIFY  1: do not verify the ACME server (tests, Pebble)
#   ACME_ONESHOT          1: one round, then exit with its status (tests)
set -u

HOST="$(printf '%s' "${PUBLIC_HOSTNAME:-}" | tr 'A-Z' 'a-z' | sed 's/^ *//; s/ *$//; s/\.$//')"
SERVER="${ACME_SERVER:-https://acme-v02.api.letsencrypt.org/directory}"
DATA=/acme
WEBROOT=/webroot
CERTS=/nginx-certs
TAG="acme:"

idle() {
    echo "$TAG $1"
    [ "${ACME_ONESHOT:-}" = 1 ] && exit 0
    while :; do sleep 86400; done
}

case "$(printf '%s' "${ACME_DISABLE:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) idle "ACME_DISABLE is set; keeping the current certificate" ;;
esac
[ -n "$HOST" ] || idle "PUBLIC_HOSTNAME is not set; keeping the self-signed certificate"
# A public DNS name only: Let's Encrypt issues for names, and an IP or a
# single label can never validate.
if ! printf '%s' "$HOST" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$'; then
    idle "PUBLIC_HOSTNAME '$HOST' is not a public DNS name; keeping the self-signed certificate"
fi

install_cert() {
    src_crt="$DATA/certificates/$HOST.crt"
    src_key="$DATA/certificates/$HOST.key"
    [ -s "$src_crt" ] && [ -s "$src_key" ] || return 1
    if cmp -s "$src_crt" "$CERTS/server.crt" && cmp -s "$src_key" "$CERTS/server.key"; then
        return 0  # already installed
    fi
    # The first time only: keep the self-signed pair to fall back to.
    if [ -f "$CERTS/server.crt" ] && [ ! -f "$CERTS/server.crt.selfsigned" ] \
        && ! grep -q "ACME-INSTALLED" "$CERTS/.acme-state" 2>/dev/null; then
        cp -p "$CERTS/server.crt" "$CERTS/server.crt.selfsigned"
        cp -p "$CERTS/server.key" "$CERTS/server.key.selfsigned"
    fi
    # Key first, then certificate, each by rename: nginx's watcher keys on
    # the certificate, so it never reloads a new cert with the old key.
    cp "$src_key" "$CERTS/.server.key.new" && chmod 600 "$CERTS/.server.key.new" \
        && mv -f "$CERTS/.server.key.new" "$CERTS/server.key" \
        && cp "$src_crt" "$CERTS/.server.crt.new" && chmod 644 "$CERTS/.server.crt.new" \
        && mv -f "$CERTS/.server.crt.new" "$CERTS/server.crt" \
        || return 1
    echo "ACME-INSTALLED $HOST $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$CERTS/.acme-state"
    echo "$TAG installed the certificate for $HOST"
}

mkdir -p "$DATA" "$WEBROOT/.well-known/acme-challenge"
set -- --path "$DATA" --server "$SERVER" --accept-tos -d "$HOST" \
    --http --http.webroot "$WEBROOT"
[ -n "${ACME_EMAIL:-}" ] && set -- "$@" --email "$ACME_EMAIL"
[ "${ACME_TLS_SKIP_VERIFY:-}" = 1 ] && set -- "$@" --tls-skip-verify

failures=0
while :; do
    # lego v5: every flag belongs to the `run` command, after it.
    if /lego run "$@" --reuse-key; then
        failures=0
        if install_cert; then
            status=0
        else
            echo "$TAG lego succeeded but the certificate could not be installed" >&2
            status=1
        fi
        wait_s=43200  # twice a day; lego renews when a third of the lifetime is left
    else
        status=1
        failures=$((failures + 1))
        # 1 h, 2 h, 4 h, then every 12 h: never more than a few failed
        # validations an hour against Let's Encrypt's limits.
        case "$failures" in 1) wait_s=3600 ;; 2) wait_s=7200 ;; 3) wait_s=14400 ;; *) wait_s=43200 ;; esac
        echo "$TAG could not get a certificate for $HOST (attempt $failures); next try in $((wait_s / 60)) min." \
            "Check that $HOST resolves to this site's public address and that port 80 reaches it." >&2
    fi
    [ "${ACME_ONESHOT:-}" = 1 ] && exit "$status"
    sleep "$wait_s"
done
