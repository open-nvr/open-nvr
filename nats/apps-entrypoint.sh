#!/bin/sh
# Start the apps bus and reload it whenever core rewrites the users file.
#
# The users file lives on a volume core writes to. On a fresh volume it
# does not exist yet, so seed a valid file with no matchable user; core
# replaces it on its first start (services/nats_users.write_users_conf)
# and after every key issue / rotate / revoke. nats-server re-reads its
# config on SIGHUP, which `nats-server --signal reload` sends.
set -eu
USERS=/var/lib/opennvr/nats/users.conf
mkdir -p "$(dirname "$USERS")"
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

nats-server -c /etc/nats/apps.conf &
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
