#!/usr/bin/env bash
# ============================================================
# Functional tests for start.sh's published-port resolver (#368).
#
# The resolver is what turns "this port is unbindable" from a hard stop
# into a self-healing event. Its behaviour has three rules that are easy
# to regress and expensive to debug in the field:
#
#   1. An OPERATOR value always wins and is NEVER auto-overridden. It is
#      usually mirrored by a firewall rule or a router port-forward, so
#      moving off it breaks remote access in a way nobody traces back here.
#   2. A "shift" row walks its candidate list, so a WinNAT range that
#      re-rolls at every boot cannot keep the stack down.
#   3. A "pin" row (the web edge) never moves on its own, because that
#      would invalidate every bookmark, doc URL and the TLS certificate.
#
# The resolver functions are extracted from start.sh and sourced with
# stubbed probes, so this tests the real code without running the launcher.
# ============================================================
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
START_SH="${REPO_ROOT}/start.sh"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running published-port resolver tests"
echo ""

ALL_VARS="HTTPS_PORT HTTP_PORT WEBRTC_ICE_PORT RTSPS_PORT CORE_HOST_PORT LOGS_PORT AGENT_PORT HLS_PORT WEBRTC_HTTP_PORT PLAYBACK_PORT MEDIAMTX_API_PORT"

# Extract the resolver out of start.sh. Top-level functions close with a
# "}" in column 0, so this is unambiguous.
HARNESS="$(mktemp)"
trap 'rm -f "$HARNESS"' EXIT
{
    echo 'GREEN=; YELLOW=; RED=; CYAN=; BRIGHT_CYAN=; GRAY=; WHITE=; NC='
    echo 'OS="Linux"'
    echo "PORT_TABLE_FILE='${REPO_ROOT}/scripts/ports.conf'"
    # Stubs. BLOCKED is the set of ports the "host" refuses to bind.
    cat <<'STUB'
port_usable() {
    local p="$1"
    case " ${BLOCKED:-} " in
        *" $p "*) return 1 ;;
    esac
    return 0
}
get_env_var() { eval "printf '%s' \"\${ENVFILE_$1:-}\""; }
STUB
    for fn in load_port_table port_help resolve_one_port resolve_ports; do
        sed -n "/^${fn}() {/,/^}/p" "$START_SH"
    done
} > "$HARNESS"

for fn in load_port_table port_help resolve_one_port resolve_ports; do
    if ! grep -q "^${fn}() {" "$HARNESS"; then
        echo "  ✗ could not extract ${fn}() from start.sh — the resolver was renamed or removed." >&2
        exit 1
    fi
done

# Run resolve_ports in a clean subshell.
#   $1 = space-separated blocked ports
#   $2 = space-separated VAR=VALUE operator settings ("env:" prefix = process
#        environment, otherwise treated as a value in .env)
# Prints "rc=<code>" then one VAR=value line per resolved variable.
run_case() (
    BLOCKED="$1"
    for assign in ${2:-}; do
        case "$assign" in
            env:*) eval "export ${assign#env:}" ;;
            *) eval "export ENVFILE_${assign}" ;;
        esac
    done
    # shellcheck disable=SC1090
    . "$HARNESS"
    resolve_ports >/dev/null 2>&1
    echo "rc=$?"
    for v in $ALL_VARS OPENNVR_HTTPS_SUFFIX; do
        eval "printf '%s=%s\n' \"\$v\" \"\${$v-<unset>}\""
    done
)

field() { printf '%s\n' "$1" | grep "^${2}=" | head -1 | cut -d= -f2-; }

# ── 1. Nothing blocked: every port keeps its default ──
start_test "clean host resolves every port to its default"
out=$(run_case "" "")
bad=""
[ "$(field "$out" rc)" = "0" ] || bad="rc=$(field "$out" rc); "
for pair in HTTPS_PORT:443 HTTP_PORT:80 WEBRTC_ICE_PORT:8189 RTSPS_PORT:8322 \
            CORE_HOST_PORT:8000 LOGS_PORT:9999 AGENT_PORT:9100 HLS_PORT:8888 \
            WEBRTC_HTTP_PORT:8889 PLAYBACK_PORT:9996 MEDIAMTX_API_PORT:9997; do
    v=${pair%%:*}; want=${pair#*:}; got=$(field "$out" "$v")
    [ "$got" = "$want" ] || bad="${bad}${v}=${got} want ${want}; "
done
[ "$(field "$out" OPENNVR_HTTPS_SUFFIX)" = "" ] || bad="${bad}suffix not empty; "
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 2. The reported bug: 9100 inside a WinNAT block ──
# The whole 9011-9110 range is reserved, exactly as observed.
start_test "blocked 9100 (WinNAT block) shifts AGENT_PORT to 19100"
blocked=""
i=9011; while [ "$i" -le 9110 ]; do blocked="${blocked}${i} "; i=$((i + 1)); done
out=$(run_case "$blocked" "")
got=$(field "$out" AGENT_PORT)
if [ "$(field "$out" rc)" = "0" ] && [ "$got" = "19100" ]; then
    pass
else
    fail "rc=$(field "$out" rc) AGENT_PORT=${got}, expected 19100.
This is the reported failure: the stack must come up on the next candidate."
fi

# ── 3. An operator's explicit port is never moved ──
start_test "explicit AGENT_PORT that is blocked fails instead of drifting"
out=$(run_case "9100" "AGENT_PORT=9100")
if [ "$(field "$out" rc)" != "0" ]; then
    pass
else
    fail "resolver returned 0 and set AGENT_PORT=$(field "$out" AGENT_PORT).
An explicit value usually has a firewall rule behind it — moving it
silently is the failure mode this rule exists to prevent."
fi

# ── 4. An operator's explicit port is honoured when bindable ──
start_test "explicit AGENT_PORT=25000 is used verbatim"
out=$(run_case "9100" "AGENT_PORT=25000")
if [ "$(field "$out" rc)" = "0" ] && [ "$(field "$out" AGENT_PORT)" = "25000" ]; then
    pass
else
    fail "rc=$(field "$out" rc) AGENT_PORT=$(field "$out" AGENT_PORT), expected 25000"
fi

# ── 5. The process environment beats .env ──
start_test "process env overrides the .env value"
out=$(run_case "" "AGENT_PORT=25000 env:AGENT_PORT=26000")
if [ "$(field "$out" AGENT_PORT)" = "26000" ]; then
    pass
else
    fail "AGENT_PORT=$(field "$out" AGENT_PORT), expected 26000"
fi

# ── 6. Garbage is rejected, not coerced ──
start_test "non-numeric and out-of-range values are rejected"
bad=""
for v in abc 0 70000 ""; do
    out=$(run_case "" "AGENT_PORT=${v}")
    # An empty value means "unset" and must fall through to the default.
    if [ -z "$v" ]; then
        [ "$(field "$out" AGENT_PORT)" = "9100" ] || bad="${bad}empty should fall back to 9100; "
    else
        [ "$(field "$out" rc)" != "0" ] || bad="${bad}accepted '${v}'; "
    fi
done
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 7. Privileged ports are valid operator choices ──
# Regression: a 1024 lower bound would reject an explicit HTTPS_PORT=443,
# i.e. reject the default. Docker's proxy binds privileged ports itself.
start_test "explicit HTTPS_PORT=443 is accepted (no 1024 floor)"
out=$(run_case "" "HTTPS_PORT=443")
if [ "$(field "$out" rc)" = "0" ] && [ "$(field "$out" HTTPS_PORT)" = "443" ]; then
    pass
else
    fail "rc=$(field "$out" rc) HTTPS_PORT=$(field "$out" HTTPS_PORT); a 1024 floor would reject the default."
fi

# ── 8. "pin" rows never relocate themselves ──
start_test "blocked 443 fails rather than moving the web edge"
out=$(run_case "443" "")
got=$(field "$out" HTTPS_PORT)
if [ "$(field "$out" rc)" != "0" ]; then
    pass
else
    fail "resolver returned 0 with HTTPS_PORT=${got}.
Moving the web edge silently would invalidate every bookmark, every URL in
the docs and the TLS certificate."
fi

# ── 9. A moved HTTPS_PORT produces the nginx redirect suffix ──
# $host carries no port, so without this the 80->443 redirect sends a
# browser on http://host:8080 to https://host and the connection breaks.
start_test "explicit HTTPS_PORT=8443 sets OPENNVR_HTTPS_SUFFIX=:8443"
out=$(run_case "" "HTTPS_PORT=8443")
if [ "$(field "$out" OPENNVR_HTTPS_SUFFIX)" = ":8443" ]; then
    pass
else
    fail "OPENNVR_HTTPS_SUFFIX='$(field "$out" OPENNVR_HTTPS_SUFFIX)', expected ':8443'"
fi

# ── 10. Exhausting every candidate is a clear failure ──
start_test "all candidates blocked fails instead of starting broken"
out=$(run_case "9100 19100 29100 39100" "")
if [ "$(field "$out" rc)" != "0" ]; then
    pass
else
    fail "resolver returned 0 with AGENT_PORT=$(field "$out" AGENT_PORT)"
fi

# ── 11. strict mode: nothing shifts, on any row ──
# Production sites encode these ports in firewall rules, router
# port-forwards, upstream proxies and monitoring checks. There a port that
# quietly moves after a reboot is worse than a start that refuses and says
# why, so strict promotes every row to pin.
start_test "OPENNVR_PORT_POLICY=strict refuses to shift AGENT_PORT"
out=$(run_case "$blocked" "OPENNVR_PORT_POLICY=strict")
if [ "$(field "$out" rc)" != "0" ]; then
    pass
else
    fail "strict mode returned 0 with AGENT_PORT=$(field "$out" AGENT_PORT)"
fi

# ── 12. auto mode still shifts (control for the test above) ──
start_test "OPENNVR_PORT_POLICY=auto still shifts AGENT_PORT"
out=$(run_case "$blocked" "OPENNVR_PORT_POLICY=auto")
if [ "$(field "$out" rc)" = "0" ] && [ "$(field "$out" AGENT_PORT)" = "19100" ]; then
    pass
else
    fail "rc=$(field "$out" rc) AGENT_PORT=$(field "$out" AGENT_PORT), expected 19100"
fi

# ── 13. A typo in the policy is rejected, not treated as 'auto' ──
start_test "an unrecognised OPENNVR_PORT_POLICY is rejected"
out=$(run_case "" "OPENNVR_PORT_POLICY=sometimes")
if [ "$(field "$out" rc)" != "0" ]; then
    pass
else
    fail "an unrecognised policy silently behaved as 'auto' — a site that
meant 'strict' would get drifting ports without knowing."
fi

# ── 14. ICE keeps probing both protocols ──
# The ICE port is published as UDP *and* TCP; a UDP-only reservation must
# still move it, which only happens if proto 'both' reaches port_usable.
start_test "WEBRTC_ICE_PORT row still probes protocol 'both'"
proto=$(sed -e 's/#.*$//' "${REPO_ROOT}/scripts/ports.conf" \
        | awk -F'|' '{gsub(/^[ \t]+|[ \t]+$/,"",$1); gsub(/^[ \t]+|[ \t]+$/,"",$3)}
                     $1=="WEBRTC_ICE_PORT" {print $3}')
if [ "$proto" = "both" ]; then pass; else fail "WEBRTC_ICE_PORT proto='${proto}', expected 'both'"; fi

echo ""
if [ "$TESTS_FAILED" -eq 0 ]; then
    echo "✓ All ${TESTS_RUN} published-port resolver tests passed"
    exit 0
else
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} published-port resolver tests failed"
    exit 1
fi
