#!/usr/bin/env bash
# ============================================================
# Regression tests for scripts/ports.conf — the shared published-port
# table (#368).
#
# The table is the single source of truth both launchers read. The failure
# it exists to prevent: a published host port hardcoded in compose cannot
# be moved when the host refuses to bind it (WinNAT reserved ranges on
# Windows re-roll at every boot; AirPlay on macOS; ip_local_reserved_ports
# on Linux), and because Docker publishes all of a service's ports or none,
# ONE such port takes the whole service down.
#
# So the invariants worth guarding are:
#   * every table row is well-formed and self-consistent
#   * every table VAR is actually consumed by compose (no dead rows)
#   * every compose host-port interpolation has a table row (no port the
#     launcher cannot resolve)
#   * the defaults never drift from what the docs advertise
# ============================================================
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TABLE="${REPO_ROOT}/scripts/ports.conf"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running published-port table tests"
echo ""

# Same parse as load_port_table() in start.sh.
parse_table() {
    sed -e 's/#.*$//' "$TABLE" \
        | awk -F'|' 'NF==6 {
              for (i = 1; i <= 6; i++) { gsub(/^[ \t]+|[ \t]+$/, "", $i) }
              if ($1 != "") { print $1 "|" $2 "|" $3 "|" $4 "|" $5 "|" $6 }
          }'
}

COMPOSE_FILES="docker-compose.yml docker-compose.camera-agent.yml docker-compose.debug-ports.yml"

# ── 1. The table exists and parses ──
start_test "scripts/ports.conf exists and yields rows"
if [ ! -f "$TABLE" ]; then
    fail "scripts/ports.conf is missing — both launchers depend on it."
else
    row_count=$(parse_table | grep -c . || true)
    if [ "${row_count:-0}" -ge 1 ]; then
        pass
    else
        fail "ports.conf parsed to 0 rows; the 6-field VAR|DEFAULT|PROTO|CANDIDATES|POLICY|LABEL format is broken."
    fi
fi

# ── 2. Every row is internally consistent ──
start_test "every row is well-formed (proto, policy, ranges, candidates)"
bad=""
while IFS='|' read -r var def proto cands policy label; do
    [ -n "$var" ] || continue
    case "$proto" in
        tcp|udp|both) ;;
        *) bad="${bad}${var}: proto '${proto}' must be tcp|udp|both; " ;;
    esac
    case "$policy" in
        shift|pin) ;;
        *) bad="${bad}${var}: policy '${policy}' must be shift|pin; " ;;
    esac
    case "$def" in
        ''|*[!0-9]*) bad="${bad}${var}: default '${def}' is not numeric; " ;;
        *) if [ "$def" -lt 1 ] || [ "$def" -gt 65535 ]; then
               bad="${bad}${var}: default ${def} out of range; "
           fi ;;
    esac
    [ -n "$label" ] || bad="${bad}${var}: empty label (used in warnings); "
    # The first candidate must BE the default, or the launcher would move
    # the port on a perfectly healthy host.
    first_cand=$(printf '%s' "$cands" | cut -d, -f1)
    if [ "$first_cand" != "$def" ]; then
        bad="${bad}${var}: first candidate ${first_cand} != default ${def}; "
    fi
done <<EOF
$(parse_table)
EOF
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 3. "shift" rows need somewhere to shift TO ──
# Candidates spread far apart on purpose: reserved ranges are handed out as
# runs of contiguous 100-port blocks, so 9100 -> 9101 lands in the same
# block that just rejected us.
start_test "shift rows have >1 candidate, spread >100 apart"
bad=""
while IFS='|' read -r var def proto cands policy label; do
    [ -n "$var" ] || continue
    [ "$policy" = "shift" ] || continue
    n=$(printf '%s' "$cands" | tr ',' '\n' | grep -c . || true)
    if [ "${n:-0}" -lt 2 ]; then
        bad="${bad}${var}: only ${n} candidate(s) — nothing to fall back to; "
        continue
    fi
    prev=""
    for c in $(printf '%s' "$cands" | tr ',' ' '); do
        if [ -n "$prev" ]; then
            gap=$((c - prev)); [ "$gap" -lt 0 ] && gap=$((-gap))
            if [ "$gap" -le 100 ]; then
                bad="${bad}${var}: candidates ${prev} and ${c} are within one reserved block; "
            fi
        fi
        prev="$c"
    done
done <<EOF
$(parse_table)
EOF
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 4. The web edge is pinned, everything else shifts ──
# Moving 443/80 automatically would invalidate every bookmark, every
# documented URL and the certificate story.
start_test "HTTPS_PORT and HTTP_PORT use policy 'pin'"
bad=""
for v in HTTPS_PORT HTTP_PORT; do
    pol=$(parse_table | awk -F'|' -v v="$v" '$1==v {print $5}')
    if [ -z "$pol" ]; then
        bad="${bad}${v} missing from table; "
    elif [ "$pol" != "pin" ]; then
        bad="${bad}${v} has policy '${pol}', expected 'pin'; "
    fi
done
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 5. No dead rows: every VAR is consumed by compose ──
start_test "every table VAR is interpolated by a compose file"
missing=""
while IFS='|' read -r var def proto cands policy label; do
    [ -n "$var" ] || continue
    found=0
    for f in $COMPOSE_FILES; do
        if grep -q "\${${var}:-" "${REPO_ROOT}/${f}" 2>/dev/null; then found=1; break; fi
    done
    [ "$found" -eq 1 ] || missing="${missing}${var} "
done <<EOF
$(parse_table)
EOF
if [ -z "$missing" ]; then
    pass
else
    fail "table rows nothing consumes: ${missing}
A row the launcher resolves but compose ignores is silently useless."
fi

# ── 6. No unresolvable ports: every compose *_PORT has a row ──
start_test "every compose host-port variable has a table row"
orphans=""
for f in $COMPOSE_FILES; do
    # Host-port interpolations live inside a quoted "ports:" entry.
    vars=$(grep -oE '^\s*-\s*"[^"]*"' "${REPO_ROOT}/${f}" 2>/dev/null \
           | grep -oE '\$\{[A-Z0-9_]+:-[0-9]+\}' \
           | grep -oE '[A-Z0-9_]+:-' | sed 's/:-$//' | sort -u)
    for v in $vars; do
        # NGINX_BIND_HOST is an address, not a port.
        [ "$v" = "NGINX_BIND_HOST" ] && continue
        if ! parse_table | awk -F'|' -v v="$v" '$1==v {found=1} END {exit !found}'; then
            orphans="${orphans}${v}(${f}) "
        fi
    done
done
if [ -z "$orphans" ]; then
    pass
else
    fail "compose publishes ports the launcher cannot resolve: ${orphans}
Add a row to scripts/ports.conf or the port cannot be moved when the host
refuses to bind it."
fi

# ── 7. Defaults match what compose falls back to ──
# If these drift, a bare `docker compose up` and `./start.sh up` publish
# different ports on the same machine.
start_test "table defaults match compose's :- fallbacks"
bad=""
while IFS='|' read -r var def proto cands policy label; do
    [ -n "$var" ] || continue
    for f in $COMPOSE_FILES; do
        for got in $(grep -oE "\\\$\{${var}:-[0-9]+\}" "${REPO_ROOT}/${f}" 2>/dev/null \
                     | grep -oE '[0-9]+' | sort -u); do
            if [ "$got" != "$def" ]; then
                bad="${bad}${var}: table default ${def} vs ${f} fallback ${got}; "
            fi
        done
    done
done <<EOF
$(parse_table)
EOF
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 8. Container-side ports must NOT be parameterised ──
# They are baked into nginx/opennvr.conf, mediamtx.docker.yml, supervisord
# and the healthchecks; moving one silently breaks the proxy.
start_test "container side of each publication stays a literal"
bad=""
for f in $COMPOSE_FILES; do
    while IFS= read -r line; do
        # "<host-ip>:<host-port>:<container-port>" — take the last field.
        entry=$(printf '%s' "$line" | sed -e 's/^[^"]*"//' -e 's/".*$//')
        container=$(printf '%s' "$entry" | sed 's#/.*$##' | awk -F: '{print $NF}')
        case "$container" in
            *'${'*)
                # WEBRTC_ICE_PORT is the one legitimate exception: ICE
                # advertises the port inside SDP candidates, so host and
                # container port must stay equal.
                case "$container" in
                    *WEBRTC_ICE_PORT*) ;;
                    *) bad="${bad}${f}: ${entry}; " ;;
                esac
                ;;
        esac
    done <<EOF
$(grep -E '^\s*-\s*"[0-9.]*:?[^"]*:[^"]*"' "${REPO_ROOT}/${f}" 2>/dev/null)
EOF
done
if [ -z "$bad" ]; then
    pass
else
    fail "container-side ports must stay literal: ${bad}"
fi

echo ""
if [ "$TESTS_FAILED" -eq 0 ]; then
    echo "✓ All ${TESTS_RUN} published-port table tests passed"
    exit 0
else
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} published-port table tests failed"
    exit 1
fi
