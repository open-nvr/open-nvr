#!/bin/sh
# Start the apps bus and reload it whenever core rewrites the users file.
#
# 1. Render the config. apps.conf is a template: nats-server does NOT
#    expand $VAR inside a URL, so the leaf link's password must be put in
#    by us — percent-encoded, since INTERNAL_API_KEY may be base64 with
#    '/', '+' or '=' (nats-server URL-decodes it on the way out).
#    `sh apps-entrypoint.sh --render` prints the rendered config and
#    exits (what the tests exercise). The rendered file lives NEXT TO
#    the users file because nats-server resolves every `include` — even
#    an absolute path — relative to the config file's directory.
# 2. Seed the users file. It lives on a volume core writes to. On a fresh
#    volume it does not exist yet, so seed a valid file with no matchable
#    user; core replaces it on its first start
#    (services/nats_users.write_users_conf) and after every key issue /
#    rotate / revoke. nats-server re-reads its config on SIGHUP, which
#    `nats-server --signal reload` sends.
set -eu
TEMPLATE="${APPS_CONF_TEMPLATE:-/etc/nats/apps.conf}"
USERS="${APPS_USERS_CONF:-/var/lib/opennvr/nats/users.conf}"
RENDERED="${APPS_CONF_RENDERED:-$(dirname "$USERS")/apps.conf}"

url_encode() {
  # Every character that is not unreserved (RFC 3986) — the userinfo
  # part of a URL cannot carry '/', '@', ':', '#', '?', '+' or '=' raw.
  printf '%s' "$1" | sed \
    -e 's/%/%25/g' -e 's|/|%2F|g' -e 's/+/%2B/g' -e 's/=/%3D/g' \
    -e 's/@/%40/g' -e 's/:/%3A/g' -e 's/#/%23/g' -e 's/?/%3F/g' \
    -e 's/ /%20/g' -e 's/&/%26/g' -e 's/\[/%5B/g' -e 's/\]/%5D/g' \
    -e 's/;/%3B/g' -e 's/\$/%24/g' -e 's/,/%2C/g' -e "s/'/%27/g" \
    -e 's/"/%22/g' -e 's/</%3C/g' -e 's/>/%3E/g' -e 's/\\/%5C/g' \
    -e 's/\^/%5E/g' -e 's/`/%60/g' -e 's/{/%7B/g' -e 's/}/%7D/g' \
    -e 's/|/%7C/g' -e 's/!/%21/g' -e 's/\*/%2A/g' -e 's/(/%28/g' -e 's/)/%29/g'
}

render() {
  if [ -z "${INTERNAL_API_KEY:-}" ]; then
    echo "apps bus: INTERNAL_API_KEY is not set — the leaf link to the platform bus cannot authenticate" >&2
    exit 1
  fi
  enc="$(url_encode "$INTERNAL_API_KEY")"
  # '&' and '|' are special on sed's right-hand side; both were encoded
  # above, so the only remaining risk is '\' — also encoded.
  sed "s|@@INTERNAL_API_KEY_URL@@|$enc|g" "$TEMPLATE"
}

if [ "${1:-}" = "--render" ]; then
  render
  exit 0
fi

mkdir -p "$(dirname "$USERS")"
render > "$RENDERED"
chmod 600 "$RENDERED" 2>/dev/null || true
if grep -v '^[[:space:]]*#' "$RENDERED" | grep -q -e '@@INTERNAL_API_KEY_URL@@' -e '$INTERNAL_API_KEY'; then
  echo "apps bus: config render left the key placeholder in place — refusing to start" >&2
  exit 1
fi
if [ ! -s "$USERS" ]; then
  cat > "$USERS" <<'SEED'
# seeded by apps-entrypoint.sh — core replaces this file
authorization {
  users: [
    { user: "_no_apps_yet", password: "$2b$10$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", permissions: { publish: { deny: [">"] }, subscribe: { deny: [">"] } } }
  ]
}
SEED
fi

# Fail fast with nats-server's own parse error rather than a restart loop.
nats-server -c "$RENDERED" -t

nats-server -c "$RENDERED" &
PID=$!
last="$(md5sum "$USERS" | cut -d' ' -f1)"
while kill -0 "$PID" 2>/dev/null; do
  sleep 2
  now="$(md5sum "$USERS" 2>/dev/null | cut -d' ' -f1 || true)"
  if [ -n "$now" ] && [ "$now" != "$last" ]; then
    last="$now"
    if nats-server --signal reload="$PID"; then
      echo "apps bus: users file changed — reloaded"
    else
      echo "apps bus: reload FAILED — previous users stay in force" >&2
    fi
  fi
done
wait "$PID"
