#!/usr/bin/env bash
# ============================================================
# Parity guard for the two launchers' port logic (#368).
#
# start.sh and start.ps1 are independent, hand-mirrored implementations of
# the same logic, synced only by comments ("Mirrors compose_args in
# start.sh"). They HAVE drifted before, and every drift is a bug an
# operator hits on exactly one OS:
#
#   * start.sh printed Windows `netsh excludedportrange` instructions to
#     macOS and Linux operators, sending them after a facility their OS
#     does not have.
#   * Both checked a hardcoded port list (8000 8554 8888 8889 9997) that no
#     longer matched compose: 8554 is not published at all, 8888/8889/9997
#     only under the opt-in debug overlay, while the ports that ARE
#     published by default (443, 80, 8322, 9100) were absent — so the check
#     printed a reassuring green tick for port sets it never looked at.
#
# scripts/ports.conf is the fix: one table, two thin readers. These tests
# guard the properties that keep it that way.
#
# NOTE: the *behavioural* PowerShell equivalent of test_port_resolution.sh
# lives in test_port_resolution_ps1.ps1 (run it on Windows). This file is
# static analysis so it runs everywhere, including Linux CI.
# ============================================================
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SH="${REPO_ROOT}/start.sh"
PS="${REPO_ROOT}/start.ps1"
TABLE="${REPO_ROOT}/scripts/ports.conf"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running launcher port-parity tests"
echo ""

# ── 1. Both launchers read the shared table ──
start_test "both launchers read scripts/ports.conf"
bad=""
grep -q 'scripts/ports.conf' "$SH" || bad="${bad}start.sh does not reference scripts/ports.conf; "
grep -q 'scripts\\ports.conf' "$PS" || bad="${bad}start.ps1 does not reference scripts\\ports.conf; "
if [ -z "$bad" ]; then
    pass
else
    fail "$bad
A launcher with its own hardcoded port list is exactly the drift this
table exists to prevent."
fi

# ── 2. Neither launcher carries the stale hardcoded list ──
start_test "the stale 8554/8888/8889/9997 check list is gone"
bad=""
grep -qE '\(8000[, ]+8554' "$PS" && bad="${bad}start.ps1 still has the hardcoded busy-port list; "
grep -qE 'ports=\(8000 8554' "$SH" && bad="${bad}start.sh still has the hardcoded busy-port list; "
if [ -z "$bad" ]; then
    pass
else
    fail "$bad
That list drifted out of step with compose and reported success for ports
it never checked. The compose-derived pre-flight supersedes it."
fi

# ── 3. Both implement the same resolver surface ──
start_test "both launchers implement the resolver entry points"
bad=""
for fn in load_port_table resolve_one_port resolve_ports port_help; do
    grep -q "^${fn}() {" "$SH" || bad="${bad}start.sh missing ${fn}(); "
done
for fn in Import-PortTable Resolve-OnePort Resolve-Ports Show-PortHelp; do
    grep -q "function ${fn}" "$PS" || bad="${bad}start.ps1 missing ${fn}; "
done
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 4. Both honour the same operator switches ──
start_test "both launchers honour OPENNVR_PORT_POLICY"
bad=""
grep -q 'OPENNVR_PORT_POLICY' "$SH" || bad="${bad}start.sh ignores OPENNVR_PORT_POLICY; "
grep -q 'OPENNVR_PORT_POLICY' "$PS" || bad="${bad}start.ps1 ignores OPENNVR_PORT_POLICY; "
if [ -z "$bad" ]; then
    pass
else
    fail "$bad
A production site that sets strict on one OS and gets drifting ports on
the other is worse than having no switch at all."
fi

# ── 5. Both derive the nginx redirect suffix ──
# $host in the 80->443 redirect carries no port, so a moved HTTPS_PORT has
# to be re-attached or the redirect silently points at the wrong port.
start_test "both launchers export OPENNVR_HTTPS_SUFFIX"
bad=""
grep -q 'OPENNVR_HTTPS_SUFFIX' "$SH" || bad="${bad}start.sh does not set it; "
grep -q 'OPENNVR_HTTPS_SUFFIX' "$PS" || bad="${bad}start.ps1 does not set it; "
grep -q 'OPENNVR_HTTPS_SUFFIX' "${REPO_ROOT}/nginx/opennvr.conf" \
    || bad="${bad}nginx/opennvr.conf does not consume it; "
grep -q 'NGINX_ENVSUBST_FILTER' "${REPO_ROOT}/docker-compose.yml" \
    || bad="${bad}compose does not set NGINX_ENVSUBST_FILTER — envsubst would
      also expand nginx's own \$host and \$request_uri into empty strings; "
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 6. start.sh must not hand netsh advice to macOS/Linux ──
# WSL is the one legitimate exception: it runs this script but sits behind
# the Windows network stack, so WinNAT genuinely applies.
start_test "start.sh mentions netsh only inside its WSL branch"
# Only lines that PRINT matter; explanatory comments about why WinNAT
# behaves this way are welcome anywhere.
printed=$(grep -nE '^[^#]*echo .*netsh' "$SH" | grep -c . || true)
if [ "${printed:-0}" -eq 0 ]; then
    pass
else
    helper=$(sed -n '/^port_help() {/,/^}/p' "$SH")
    inside=$(printf '%s\n' "$helper" | grep -cE '^[^#]*echo .*netsh' || true)
    if [ "${printed:-0}" != "${inside:-0}" ]; then
        fail "netsh is printed outside port_help() — ${printed} total vs ${inside} inside.
macOS and Linux operators must never be told to run a Windows-only command."
    elif printf '%s\n' "$helper" | grep -q 'microsoft /proc/version'; then
        pass
    else
        fail "port_help() prints netsh without gating on a WSL check.
Only WSL sits behind the Windows network stack; native Linux and macOS
have no netsh."
    fi
fi

# ── 7. The EACCES asymmetry is deliberate and documented ──
# start.sh must treat EACCES on a port <1024 as usable: on Linux and macOS
# that is the privileged-port rule, and Docker binds as root anyway, so
# counting it as a conflict hard-fails every non-root start on 443/80.
# start.ps1 must NOT: on Windows the same errno means a WinNAT reservation,
# which is a real conflict. This is the kind of asymmetry someone "tidies
# up" later, so pin it down.
start_test "the privileged-port carve-out is bash-only and explained"
bad=""
probe=$(sed -n '/^port_bindable() {/,/^}/p' "$SH")
printf '%s\n' "$probe" | grep -q 'errno.EACCES' \
    || bad="start.sh port_bindable() lost the EACCES carve-out — every
      non-root ./start.sh on Linux/macOS would now die on 443/80; "
printf '%s\n' "$probe" | grep -q 'p < 1024' \
    || bad="${bad}start.sh must scope the carve-out to privileged ports; "
grep -q 'errno' "$PS" \
    && bad="${bad}start.ps1 copied the carve-out — on Windows EACCES is a
      WinNAT reservation and must stay a hard failure; "
grep -q 'WinNAT reservation' "$PS" \
    || bad="${bad}start.ps1 should say why it deliberately differs; "
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 8. Both scope resolution to what compose actually publishes ──
# The table covers opt-in services; resolving rows nothing publishes warns
# about ports that will never be bound, and can abort a start outright.
start_test "both launchers resolve only published ports"
bad=""
grep -q 'published_port_entries' "$SH" || bad="${bad}start.sh does not query the published set; "
grep -q 'Get-PublishedPortEntries' "$PS" || bad="${bad}start.ps1 does not query the published set; "
sed -n '/^resolve_ports() {/,/^}/p' "$SH" | grep -q 'published_port_entries' \
    || bad="${bad}start.sh resolve_ports does not filter by it; "
if [ -z "$bad" ]; then pass; else fail "$bad"; fi

# ── 9. Every table row is resolvable by both launchers ──
# Neither launcher may special-case a variable: they iterate the table.
start_test "neither launcher hardcodes a per-port special case"
bad=""
while IFS='|' read -r var rest; do
    var=$(printf '%s' "$var" | tr -d ' \t')
    [ -n "$var" ] || continue
    # WEBRTC_ICE_PORT legitimately appears in compose comments and docs, but
    # neither launcher should branch on any specific variable name.
    for f in "$SH" "$PS"; do
        n=$(grep -c "\"${var}\"" "$f" 2>/dev/null || true)
        if [ "${n:-0}" -gt 0 ]; then
            bad="${bad}$(basename "$f") references ${var} by name (${n}x); "
        fi
    done
done <<EOF
$(sed -e 's/#.*$//' "$TABLE" | awk -F'|' 'NF==6 {print $1}')
EOF
if [ -z "$bad" ]; then
    pass
else
    fail "$bad
Ports must come from the table, not from launcher-side special cases —
otherwise adding a port means editing three files and the launchers drift."
fi

echo ""
if [ "$TESTS_FAILED" -eq 0 ]; then
    echo "✓ All ${TESTS_RUN} launcher port-parity tests passed"
    exit 0
else
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} launcher port-parity tests failed"
    exit 1
fi
