#!/usr/bin/env bash
# Regression tests for the Compose version gate and the post-up network
# repair in start.sh. Compose 2.24.x (Ubuntu 24.04's docker-compose-v2)
# randomly leaves a new container off one of its networks
# (docker/compose#11533, fixed in 2.26.1); opennvr-core then cannot reach
# db or nats while its health check stays green. start.sh warns below the
# minimum version and re-attaches missing networks after every up.
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
START_SH="${REPO_ROOT}/start.sh"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

# Pull named functions (and the COMPOSE_MIN_VERSION line) out of start.sh
# so they run here without executing the script.
extract_fn() { awk -v fn="$1" '$0 ~ "^"fn"\\(\\) \\{" {p=1} p {print} p && /^}/ {exit}' "$START_SH"; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
{
    grep -E '^COMPOSE_MIN_VERSION=' "$START_SH"
    extract_fn python3_usable
    extract_fn compose_version
    extract_fn version_ge
    extract_fn ensure_compose_networks
} > "$WORK/fns.sh"
YELLOW=""; RED=""; GRAY=""; NC=""
# shellcheck disable=SC1090
. "$WORK/fns.sh"

echo "Running compose-networks tests"
echo ""

# ── A fake docker on PATH: the python inside ensure_compose_networks
#    shells out to `docker inspect`, so a bash function is not enough. ──
mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
# Stub driven by files in $STUB_DIR: compose-version, config.json, ps.json,
# networks/<container> (one network per line), connects.log (appended).
case "$1 $2" in
    "compose version") cat "$STUB_DIR/compose-version" ;;
    "compose "*)
        # args: compose [-f file ...] <subcommand> ...
        for a in "$@"; do
            case "$a" in
                config) cat "$STUB_DIR/config.json"; exit 0 ;;
                ps) cat "$STUB_DIR/ps.json"; exit 0 ;;
            esac
        done
        exit 1 ;;
    "inspect -f")
        name="${@: -1}"
        [ -f "$STUB_DIR/networks/$name" ] || exit 1
        tr '\n' ' ' < "$STUB_DIR/networks/$name" ;;
    "network connect")
        shift 2; echo "$*" >> "$STUB_DIR/connects.log" ;;
    *) exit 1 ;;
esac
STUB
chmod +x "$WORK/bin/docker"
export PATH="$WORK/bin:$PATH"
export STUB_DIR="$WORK/stub"
mkdir -p "$STUB_DIR/networks"

# ── 1. version_ge ──
start_test "version_ge orders dotted versions numerically"
ok=1
version_ge 2.24.6 2.26.1 && ok=0
version_ge 2.26.1 2.26.1 || ok=0
version_ge 2.29.7 2.26.1 || ok=0
version_ge 2.30 2.26.1 || ok=0
version_ge 3.0.0 2.26.1 || ok=0
version_ge 2.9.9 2.26.1 && ok=0
[ $ok = 1 ] && pass || fail "version_ge gave a wrong answer for one of the pairs"

# ── 2. compose_version strips distro suffixes ──
start_test "compose_version parses the Ubuntu package string"
echo "2.24.6+ds1-0ubuntu2" > "$STUB_DIR/compose-version"
v=$(compose_version)
[ "$v" = "2.24.6" ] && pass || fail "expected 2.24.6, got '$v'"

start_test "compose_version parses a v-prefixed upstream string"
echo "v2.29.7" > "$STUB_DIR/compose-version"
v=$(compose_version)
[ "$v" = "2.29.7" ] && pass || fail "expected 2.29.7, got '$v'"

start_test "compose_version is empty when the plugin prints garbage"
echo "docker: 'compose' is not a docker command." > "$STUB_DIR/compose-version"
v=$(compose_version)
[ -z "$v" ] && pass || fail "expected empty, got '$v'"

# ── 3. the pinned minimum is the first fixed release ──
start_test "COMPOSE_MIN_VERSION is 2.26.1 (docker/compose#11533 fix)"
[ "${COMPOSE_MIN_VERSION:-}" = "2.26.1" ] && pass || fail "got '${COMPOSE_MIN_VERSION:-}'"

# ── 4. ensure_compose_networks ──
cat > "$STUB_DIR/config.json" <<'JSON'
{
  "networks": {
    "opennvr_internal": {"name": "open-nvr_opennvr_internal"},
    "opennvr_apps": {"name": "open-nvr_opennvr_apps"},
    "default": {"name": "open-nvr_default"}
  },
  "services": {
    "opennvr-core": {"container_name": "opennvr_core", "networks": {"opennvr_internal": {"aliases": ["core-alt"]}, "opennvr_apps": null}},
    "nats": {"container_name": "opennvr_nats", "networks": ["opennvr_internal", "opennvr_apps"]},
    "db": {"container_name": "opennvr_db", "networks": {"opennvr_internal": null}},
    "plain": {"container_name": "opennvr_plain"},
    "hostnet": {"container_name": "opennvr_hostnet", "network_mode": "host"}
  }
}
JSON
# Compose >= 2.21 prints one object per line. opennvr_fakecams belongs to
# the project but not to this run's files (an overlay start.sh was not told
# about); opennvr_plain declares no networks; db declares one: none of
# those can lose a secondary network and must be left alone.
cat > "$STUB_DIR/ps.json" <<'JSON'
{"Name":"opennvr_core","Service":"opennvr-core","State":"running"}
{"Name":"opennvr_nats","Service":"nats","State":"running"}
{"Name":"opennvr_db","Service":"db","State":"running"}
{"Name":"opennvr_plain","Service":"plain","State":"running"}
{"Name":"opennvr_hostnet","Service":"hostnet","State":"running"}
{"Name":"opennvr_old","Service":"opennvr-core","State":"exited"}
{"Name":"opennvr_fakecams","Service":"fakecams","State":"running"}
JSON
printf 'open-nvr_opennvr_apps\n' > "$STUB_DIR/networks/opennvr_core"            # lost internal
printf 'open-nvr_opennvr_internal\n' > "$STUB_DIR/networks/opennvr_nats"        # lost apps
printf 'open-nvr_opennvr_internal\n' > "$STUB_DIR/networks/opennvr_db"          # fine
printf 'open-nvr_default\n' > "$STUB_DIR/networks/opennvr_plain"                # fine
printf '' > "$STUB_DIR/networks/opennvr_hostnet"                                # host mode, ignored
printf 'open-nvr_opennvr_internal\n' > "$STUB_DIR/networks/opennvr_fakecams"    # not in this config, ignored
: > "$STUB_DIR/connects.log"

start_test "re-attaches exactly the secondary networks a running container is missing"
out=$(ensure_compose_networks "-f docker-compose.yml" 2>&1)
got=$(sort "$STUB_DIR/connects.log" | tr '\n' ';')
want="--alias nats --alias opennvr_nats open-nvr_opennvr_apps opennvr_nats;--alias opennvr-core --alias opennvr_core --alias core-alt open-nvr_opennvr_internal opennvr_core;"
[ "$got" = "$want" ] && pass || fail "connects: '$got' (expected '$want')
      output: $out"

# A bare `docker network connect` registers no DNS name on the network: the
# re-attached core was reachable by IP only, and the detect-pipeline (which
# dials http://opennvr-core:8000) lost camera discovery until core was
# re-attached by hand with its aliases (Ventuno Q, 2026-11-02).
start_test "re-attaches with the service name, container name and declared aliases"
echo "$got" | tr ';' '\n' | grep -q -- "--alias opennvr-core --alias opennvr_core --alias core-alt open-nvr_opennvr_internal opennvr_core" \
    && echo "$got" | tr ';' '\n' | grep -q -- "--alias nats --alias opennvr_nats open-nvr_opennvr_apps opennvr_nats" \
    && pass || fail "connects: '$got'"

start_test "says what it did and why"
echo "$out" | grep -q "Re-attached opennvr_core to open-nvr_opennvr_internal" \
    && echo "$out" | grep -q "Upgrade Docker Compose to 2.26.1" \
    && pass || fail "output: $out"

start_test "a healthy stack is a no-op with no output"
printf 'open-nvr_opennvr_apps\nopen-nvr_opennvr_internal\n' > "$STUB_DIR/networks/opennvr_core"
printf 'open-nvr_opennvr_apps\nopen-nvr_opennvr_internal\n' > "$STUB_DIR/networks/opennvr_nats"
: > "$STUB_DIR/connects.log"
out=$(ensure_compose_networks "-f docker-compose.yml" 2>&1)
[ ! -s "$STUB_DIR/connects.log" ] && [ -z "$out" ] && pass || fail "connects: $(cat "$STUB_DIR/connects.log"); output: $out"

start_test "accepts the older array form of 'compose ps --format json'"
printf 'open-nvr_opennvr_apps\n' > "$STUB_DIR/networks/opennvr_core"
cat > "$STUB_DIR/ps.json" <<'JSON'
[{"Name":"opennvr_core","Service":"opennvr-core","State":"running"}]
JSON
: > "$STUB_DIR/connects.log"
ensure_compose_networks "-f docker-compose.yml" >/dev/null 2>&1
[ "$(cat "$STUB_DIR/connects.log")" = "--alias opennvr-core --alias opennvr_core --alias core-alt open-nvr_opennvr_internal opennvr_core" ] && pass || fail "connects: $(cat "$STUB_DIR/connects.log")"

start_test "never fails the start when compose config cannot be read"
rm -f "$STUB_DIR/config.json"
ensure_compose_networks "-f docker-compose.yml" >/dev/null 2>&1 && pass || fail "returned non-zero"

# ── 5. wiring: validate warns, both up paths repair ──
start_test "run_validate checks the Compose version against COMPOSE_MIN_VERSION"
awk '/^run_validate\(\) \{/,/^}/' "$START_SH" | grep -q 'version_ge "\$compose_ver" "\$COMPOSE_MIN_VERSION"' && pass || fail "no version_ge call in run_validate"

start_test "run_up and run_build call ensure_compose_networks right after 'up -d'"
n=$(grep -cE '^\s*ensure_compose_networks "\$ARGS"' "$START_SH")
[ "$n" = 2 ] && grep -A1 -E 'docker compose \$ARGS up -d' "$START_SH" | grep -cq 'ensure_compose_networks' && pass || fail "found $n call(s)"

echo ""
echo "${TESTS_RUN} tests, ${TESTS_FAILED} failed"
[ "$TESTS_FAILED" -eq 0 ]
