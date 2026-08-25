#!/usr/bin/env bash
# Regression tests for the installer's Ollama-availability gate.
#
# The field bug: on Linux, the operator picked "Ollama on this machine",
# Ollama was not installed, and the installer printed the download URL and
# CARRIED ON — producing a finished install whose agent pointed at :11434
# with nothing listening. A warning is not a runtime.
#
# The rules these tests defend:
#   * endpoint on THIS machine + no Ollama → offer to install; on decline or
#     failure, offer the bundled container; never silently proceed broken.
#   * a REMOTE endpoint (LAN box) must not nag about a local install at all.
#   * Ollama installed-but-not-running counts as present (a start hint, not
#     a reinstall or a fallback).
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running installer ollama-gate tests"
echo ""

# Extract the REAL gate from install.sh (between its markers), so the tests
# exercise the shipped logic rather than a copy that can drift.
GATE=$(awk '/--- ollama-availability gate/,/--- end ollama-availability gate ---/' scripts/install.sh)
if [[ -z "$GATE" ]]; then
    echo "✗ could not extract the ollama gate from scripts/install.sh" >&2
    exit 1
fi

# run_gate <ext_url> <have_ollama:yes|no> <answers csv, consumed in order>
#   Drives the gate with everything stubbed. Emits a result line:
#   "where=<llm_where> url=<OLLAMA_EXTERNAL_URL> asked=<questions asked>|..."
run_gate() {
    local ext_url="$1" have_ollama="$2" answers="$3"
    bash -u -c '
        PLATFORM="Linux"; host_ollama=""; llm_where="host"
        EXT_URL="'"$ext_url"'"; HAVE_OLLAMA="'"$have_ollama"'"
        IFS="," read -r -a ANSWERS <<< "'"$answers"'"
        A_I=0; ASKED=""
        env_get() { printf "%s" "$EXT_URL"; }
        env_set() { [[ "$1" == "OLLAMA_EXTERNAL_URL" ]] && EXT_URL="$2"; }
        ask_yes_no() {
            ASKED="${ASKED}|$1"
            _a="${ANSWERS[$A_I]:-n}"; A_I=$((A_I+1))
            [[ "$_a" == "y" ]]
        }
        warn() { WARNED="${WARNED:-}|$1"; }
        ok() { :; }; info() { :; }
        command() {          # intercept `command -v ollama` / `command -v curl`
            if [[ "$1" == "-v" && "$2" == "ollama" ]]; then
                [[ "$HAVE_OLLAMA" == "yes" ]]; return
            fi
            [[ "$1" == "-v" && "$2" == "curl" ]] && return 0
            builtin command "$@"
        }
        curl() { return 1; }     # nothing answers anywhere in tests
        sleep() { :; }           # no real waiting in tests
        # The gate declares with `local`, which only exists inside functions.
        # The assignments must land in THIS scope, and `declare -g` needs
        # bash 4.2 (macOS ships 3.2) — so eval each one globally by hand.
        local() {
            for _arg in "$@"; do
                case "$_arg" in
                    *=*) eval "${_arg%%=*}=\"\${_arg#*=}\"" ;;
                    *)   eval "${_arg}=\"\"" ;;
                esac
            done
        }
        '"$GATE"'
        printf "where=%s url=%s asked=%s warned=%s\n" \
            "$llm_where" "$EXT_URL" "$ASKED" "${WARNED:-}"
    ' 2>/dev/null
}

LOCAL_URL="http://host.docker.internal:11434"
LAN_URL="http://192.168.0.50:11434"

# ── 1. the reported failure: decline install, accept the fallback ──
start_test "no Ollama + declined install -> bundled container fallback"
out=$(run_gate "$LOCAL_URL" no "n,y")
if [[ "$out" == *"where=container"* && "$out" == *"url= "* || "$out" == *"where=container"* ]]; then
    [[ "$out" == *"url="*"asked="* ]] && pass || fail "unexpected output: $out"
else
    fail "expected fallback to the container, got: $out"
fi

start_test "the fallback clears the dead external URL"
out=$(run_gate "$LOCAL_URL" no "n,y")
[[ "$out" == *"url= asked="* || "$out" =~ url=\ +asked= ]] && pass \
    || fail "OLLAMA_EXTERNAL_URL not cleared: $out"

# ── 2. refusing both is allowed, but never silent ──
start_test "declining install AND fallback proceeds only with a loud warning"
out=$(run_gate "$LOCAL_URL" no "n,n")
if [[ "$out" == *"where=host"* && "$out" == *"WITHOUT an LLM runtime"* ]]; then
    pass
else
    fail "expected an explicit WITHOUT-runtime warning, got: $out"
fi

# ── 3. a LAN endpoint must not demand a local install ──
start_test "remote endpoint: no install offer, no fallback question"
out=$(run_gate "$LAN_URL" no "")
if [[ "$out" == *"where=host"* && "$out" != *"asked=|"* ]]; then
    pass
else
    fail "remote endpoint should ask nothing, got: $out"
fi

start_test "remote endpoint that is down gets a reachability warning"
out=$(run_gate "$LAN_URL" no "")
[[ "$out" == *"not answering right now"* ]] && pass \
    || fail "expected a reachability warning, got: $out"

# ── 4. installed-but-not-running is a start hint, not a fallback ──
start_test "Ollama installed but not answering keeps the host runtime"
out=$(run_gate "$LOCAL_URL" yes "")
if [[ "$out" == *"where=host"* && "$out" == *"start it"* && "$out" != *"asked=|"* ]]; then
    pass
else
    fail "expected a start hint and no questions, got: $out"
fi

# ── 5. accepting the install offer keeps the host runtime ──
start_test "accepting the install offer never falls back behind your back"
# curl is stubbed to fail, so the official-installer path fails -> after a
# failed install the fallback question MUST still come.
out=$(run_gate "$LOCAL_URL" no "y,y")
[[ "$out" == *"where=container"* ]] && pass \
    || fail "failed install then accepted fallback should switch, got: $out"

# ── 6. the two installers must not drift apart ──
start_test "install.ps1 carries the container fallback"
grep -q "Use the bundled ollama container instead" scripts/install.ps1 && pass \
    || fail "install.ps1 lost the container-fallback question"

start_test "install.ps1 skips the nag for remote endpoints"
grep -q 'targetsHere' scripts/install.ps1 && pass \
    || fail "install.ps1 lost the remote-endpoint guard"

echo ""
if (( TESTS_FAILED > 0 )); then
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} tests failed"
    exit 1
fi
echo "✓ all ${TESTS_RUN} tests passed"
