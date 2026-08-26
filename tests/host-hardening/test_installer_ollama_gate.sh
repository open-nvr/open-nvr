#!/usr/bin/env bash
# Regression tests for the installer's Ollama-availability gate.
#
# The field bug: on Linux, the operator picked "Ollama on this machine",
# Ollama was not installed, and the installer printed the download URL and
# CARRIED ON — producing a finished install whose agent pointed at :11434
# with nothing listening. A warning is not a runtime.
#
# The second field bug, same shape: the gate then started accepting
# `command -v ollama` as proof of a runtime. A CUDA box whose ollama.service
# was still coming up got "installed but not answering yet — start it",
# and the installer walked on and finished. A binary is not a runtime
# either.
#
# The rules these tests defend:
#   * endpoint on THIS machine + no Ollama → offer to install; on decline or
#     failure, offer the bundled container; never silently proceed broken.
#   * a REMOTE endpoint (LAN box) must not nag about a local install at all.
#   * ONLY A LIVE ENDPOINT COUNTS. Installed-but-silent means try to START
#     it and WAIT — and if it still will not answer, it is not a runtime.
#   * the one exception: a pending NVIDIA driver reboot is a known cause
#     with a known remedy, and takes an explicit yes.
#   * a host-local probe is not enough — the agent reaches Ollama from a
#     CONTAINER, through the bridge gateway.
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
# exercise the shipped logic rather than a copy that can drift. The helpers
# the gate leans on come along the same way, for the same reason.
GATE=$(awk '/--- ollama-availability gate/,/--- end ollama-availability gate ---/' scripts/install.sh)
HELPERS=$(awk '/--- ollama runtime helpers/,/^choose_example\(\) \{/' scripts/install.sh | sed '$d')
if [[ -z "$GATE" ]]; then
    echo "✗ could not extract the ollama gate from scripts/install.sh" >&2
    exit 1
fi
if [[ -z "$HELPERS" ]] || ! grep -q 'ollama_endpoint_up()' <<< "$HELPERS"; then
    echo "✗ could not extract the ollama runtime helpers from scripts/install.sh" >&2
    exit 1
fi

# run_gate <ext_url> <have_ollama:yes|no> <answers csv> [appears_after]
#          [platform] [have_brew:yes|no] [brew_ok:yes|no] [KEY=VALUE ...]
#   Drives the gate with everything stubbed; answers feed BOTH yes/no
#   questions and the re-check loop prompt, in call order. When the answers
#   run out, yes/no questions answer "n" and the loop prompt reports EOF (a
#   dried-up stdin). ``appears_after`` makes `command -v ollama` start
#   succeeding after that many checks — an install completing in another
#   terminal mid-loop.
#
#   Trailing KEY=VALUE knobs, all optional:
#     HAVE_SYSTEMCTL=yes|no   is there a systemd to start (default: Linux=yes)
#     START_OK=yes|no         does `systemctl start ollama` succeed
#     CURL_UP_AFTER=<n>       :11434 starts answering after n probes — the
#                             service finally coming up during the wait
#     DOCKER_GW=<ip>          the bridge gateway `docker network inspect`
#                             reports (empty: no bridge, probe skipped)
#     BRIDGE_OK=yes|no        does :11434 answer on that gateway
#     NVIDIA_REBOOT_PENDING=yes      a CUDA driver install is awaiting a reboot
#
#   Emits: "where=<llm_where> url=<OLLAMA_EXTERNAL_URL> asked=<questions>|..."
run_gate() {
    local ext_url="$1" have_ollama="$2" answers="$3" appears="${4:-}"
    local platform="${5:-Linux}" have_brew="${6:-no}" brew_ok="${7:-no}"
    shift $(( $# < 7 ? $# : 7 ))
    local extras="" kv
    for kv in "$@"; do extras="${extras}${kv%%=*}=\"${kv#*=}\"; "; done
    local sysctl_default="yes"
    [[ "$platform" == "macOS" ]] && sysctl_default="no"
    bash -u -c '
        PLATFORM="'"$platform"'"; host_ollama=""; llm_where="host"
        PROJECT_ROOT="/repo"
        EXT_URL="'"$ext_url"'"; HAVE_OLLAMA="'"$have_ollama"'"
        HAVE_BREW="'"$have_brew"'"; BREW_OK="'"$brew_ok"'"
        APPEARS_AFTER="'"$appears"'"; OLLAMA_CHECKS=0
        HAVE_SYSTEMCTL="'"$sysctl_default"'"; START_OK="no"
        CURL_UP_AFTER=""; CURL_CALLS=0
        DOCKER_GW=""; BRIDGE_OK="no"; NVIDIA_REBOOT_PENDING=""
        IFS="," read -r -a ANSWERS <<< "'"$answers"'"
        A_I=0; ASKED=""
        env_get() { printf "%s" "$EXT_URL"; }
        env_set() { [[ "$1" == "OLLAMA_EXTERNAL_URL" ]] && EXT_URL="$2"; }
        ask_yes_no() {
            ASKED="${ASKED}|$1"
            _a="${ANSWERS[$A_I]:-n}"; A_I=$((A_I+1))
            [[ "$_a" == "y" ]]
        }
        read() {             # the re-check loop prompts with the builtin
            _var=""
            while [[ $# -gt 0 && "$1" == -* ]]; do
                case "$1" in -p) ASKED="${ASKED}|$2"; shift 2 ;; *) shift ;; esac
            done
            _var="${1:-REPLY}"
            _a="${ANSWERS[$A_I]:-__EOF__}"; A_I=$((A_I+1))
            [[ "$_a" == "__EOF__" ]] && return 1     # stdin ran dry
            [[ "$_a" == "ENTER" ]] && _a=""          # bare Enter (bash drops
                                                     # empty CSV fields)
            eval "$_var=\"\$_a\""
            return 0
        }
        warn() { WARNED="${WARNED:-}|$1"; }
        ok() { :; }; info() { :; }
        command() {          # intercept `command -v <tool>`
            if [[ "$1" == "-v" && "$2" == "ollama" ]]; then
                OLLAMA_CHECKS=$((OLLAMA_CHECKS+1))
                [[ "$HAVE_OLLAMA" == "yes" ]] && return 0
                [[ -n "$APPEARS_AFTER" ]] && (( OLLAMA_CHECKS > APPEARS_AFTER )) && return 0
                return 1
            fi
            [[ "$1" == "-v" && "$2" == "curl" ]] && return 0
            if [[ "$1" == "-v" && "$2" == "brew" ]]; then
                [[ "$HAVE_BREW" == "yes" ]]; return
            fi
            if [[ "$1" == "-v" && ( "$2" == "systemctl" || "$2" == "nvidia-smi" \
                                   || "$2" == "lspci" ) ]]; then
                [[ "$2" == "systemctl" && "$HAVE_SYSTEMCTL" == "yes" ]]; return
            fi
            builtin command "$@"
        }
        brew() {             # brew install / brew services start
            [[ "$BREW_OK" == "yes" ]]
        }
        # Nothing answers until CURL_UP_AFTER probes have gone by; the
        # bridge gateway answers on its own switch, so the two failures
        # (dead endpoint vs. loopback-only bind) stay distinguishable.
        curl() {
            _url=""
            for _a in "$@"; do case "$_a" in http*) _url="$_a" ;; esac; done
            if [[ -n "$DOCKER_GW" && "$_url" == *"$DOCKER_GW"* ]]; then
                [[ "$BRIDGE_OK" == "yes" ]]; return
            fi
            CURL_CALLS=$((CURL_CALLS+1))
            [[ -n "$CURL_UP_AFTER" ]] && (( CURL_CALLS > CURL_UP_AFTER )) && return 0
            return 1
        }
        systemctl() {
            [[ "$1" == "is-active" ]] && { echo inactive; return 0; }
            [[ "$START_OK" == "yes" ]]
        }
        sudo() { "$@"; }
        open() { return 1; }
        docker() { printf "%s" "$DOCKER_GW"; }
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
        '"$HELPERS"'
        '"$extras"'
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

# ── 2. there is NO proceed-broken exit any more ──
start_test "refusing install and container does not proceed — it loops"
# decline install, decline container, press Enter once (still nothing),
# then give in and take the container. The gate must never exit "host"
# without a runtime, and the old warn-and-continue text must be gone.
out=$(run_gate "$LOCAL_URL" no "n,n,ENTER,container")
if [[ "$out" == *"where=container"* && "$out" != *"WITHOUT an LLM runtime"* \
      && "$out" == *"press Enter to re-check"* ]]; then
    pass
else
    fail "expected the re-check loop then container, got: $out"
fi

start_test "a dried-up stdin takes the container, never a hang, never broken"
# Answers exhaust after the two declines: the loop prompt hits EOF. An
# unattended run must end with a WORKING runtime, not an infinite loop
# and not a dead endpoint.
out=$(run_gate "$LOCAL_URL" no "n,n")
if [[ "$out" == *"where=container"* && "$out" == *"No interactive input"* ]]; then
    pass
else
    fail "expected the container on EOF, got: $out"
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

# ── 4. a binary on PATH is NOT a runtime ──
# This is the second field bug, inverted into a test: the gate used to set
# ollama_ready=yes here and finish the install pointing at a dead port.
start_test "installed but silent: not accepted, not called ready"
out=$(run_gate "$LOCAL_URL" yes "")
if [[ "$out" == *"where=container"* && "$out" != *"where=host"* \
      && "$out" == *"installed but not answering"* ]]; then
    pass
else
    fail "a binary alone must not count as a runtime, got: $out"
fi

start_test "installed but silent: the gate tries to START it"
out=$(run_gate "$LOCAL_URL" yes "y" "" Linux no no START_OK=yes CURL_UP_AFTER=1)
if [[ "$out" == *"where=host"* && "$out" == *"Start Ollama now"* \
      && "$out" != *"bundled ollama container instead"* ]]; then
    pass
else
    fail "a successful start should keep the host runtime, got: $out"
fi

start_test "installed but silent: a FAILED start still cannot proceed"
out=$(run_gate "$LOCAL_URL" yes "y,y" "" Linux no no START_OK=no)
if [[ "$out" == *"where=container"* && "$out" == *"systemctl start ollama failed"* ]]; then
    pass
else
    fail "a failed start must fall back, got: $out"
fi

start_test "no systemd: the gate diagnoses instead of guessing"
out=$(run_gate "$LOCAL_URL" yes "y" "" Linux no no HAVE_SYSTEMCTL=no)
if [[ "$out" == *"where=container"* && "$out" == *"ollama serve"* \
      && "$out" != *"Start Ollama now"* ]]; then
    pass
else
    fail "expected a serve hint and no systemctl prompt, got: $out"
fi

start_test "the diagnosis points at the journal, not at 'start it'"
out=$(run_gate "$LOCAL_URL" yes "n,n")
[[ "$out" == *"journalctl -u ollama"* ]] && pass \
    || fail "expected a journalctl pointer, got: $out"

start_test "the loop no longer accepts an Ollama that appears but stays silent"
# decline install, decline container, press Enter — by then `command -v
# ollama` starts succeeding (appears_after=1: the operator installed it in
# another terminal). It still has to ANSWER.
out=$(run_gate "$LOCAL_URL" no "n,n,ENTER" 1)
if [[ "$out" == *"where=container"* && "$out" == *"still not answering"* ]]; then
    pass
else
    fail "a mid-loop install that stays silent must not pass, got: $out"
fi

start_test "the loop DOES accept one that comes up during the wait"
out=$(run_gate "$LOCAL_URL" no "n,n,ENTER,y" 1 Linux no no START_OK=yes CURL_UP_AFTER=1)
[[ "$out" == *"where=host"* ]] && pass \
    || fail "an Ollama that answers should be accepted, got: $out"

# ── 5. accepting the install offer keeps the host runtime ──
start_test "accepting the install offer never falls back behind your back"
# curl is stubbed to fail, so the official-installer path fails -> after a
# failed install the fallback question MUST still come.
out=$(run_gate "$LOCAL_URL" no "y,y")
[[ "$out" == *"where=container"* ]] && pass \
    || fail "failed install then accepted fallback should switch, got: $out"

# ── 6. a pending NVIDIA driver reboot: the ONE known-cause exception ──
start_test "pending driver reboot keeps the host runtime on an explicit yes"
out=$(run_gate "$LOCAL_URL" yes "n,y" "" Linux no no NVIDIA_REBOOT_PENDING=yes)
if [[ "$out" == *"where=host"* && "$out" == *"NEEDS A REBOOT"* ]]; then
    pass
else
    fail "a pending reboot should be able to keep the host runtime, got: $out"
fi

start_test "the reboot exception still says what to do after the reboot"
out=$(run_gate "$LOCAL_URL" yes "n,y" "" Linux no no NVIDIA_REBOOT_PENDING=yes)
if [[ "$out" == *"sudo reboot"* && "$out" == *"start.sh up"* ]]; then
    pass
else
    fail "expected post-reboot instructions, got: $out"
fi

start_test "the reboot exception is an offer, not an assumption"
# Declining it must land on a runtime that works TODAY.
out=$(run_gate "$LOCAL_URL" yes "n,n,y" "" Linux no no NVIDIA_REBOOT_PENDING=yes)
[[ "$out" == *"where=container"* ]] && pass \
    || fail "declining the reboot exception should fall back, got: $out"

# ── 7. the probe that matters: from the CONTAINER, not from this shell ──
start_test "a loopback-only bind is caught and named"
out=$(run_gate "$LOCAL_URL" yes "y,n" "" Linux no no START_OK=yes CURL_UP_AFTER=1 \
        DOCKER_GW=172.17.0.1 BRIDGE_OK=no)
if [[ "$out" == *"where=host"* && "$out" == *"REFUSES 172.17.0.1:11434"* \
      && "$out" == *"Apply that now?"* ]]; then
    pass
else
    fail "expected the bridge probe to catch the bind, got: $out"
fi

start_test "declining the bind fix is a warning, not a fallback"
out=$(run_gate "$LOCAL_URL" yes "y,n" "" Linux no no START_OK=yes CURL_UP_AFTER=1 \
        DOCKER_GW=172.17.0.1 BRIDGE_OK=no)
if [[ "$out" == *"where=host"* && "$out" == *"apply it before first use"* ]]; then
    pass
else
    fail "declining should warn and keep the runtime, got: $out"
fi

start_test "a reachable bridge asks nothing at all"
out=$(run_gate "$LOCAL_URL" yes "y" "" Linux no no START_OK=yes CURL_UP_AFTER=1 \
        DOCKER_GW=172.17.0.1 BRIDGE_OK=yes)
if [[ "$out" == *"where=host"* && "$out" != *"Apply that now?"* \
      && "$out" != *"REFUSES"* ]]; then
    pass
else
    fail "a working bridge should be silent, got: $out"
fi

start_test "a LAN endpoint is never bridge-probed — not ours to fix"
out=$(run_gate "$LAN_URL" no "" "" Linux no no DOCKER_GW=172.17.0.1 BRIDGE_OK=no)
if [[ "$out" != *"REFUSES"* && "$out" != *"Apply that now?"* ]]; then
    pass
else
    fail "a remote endpoint must not get the local bind lecture, got: $out"
fi

# ── 8. macOS: the same gate, the right words ──
start_test "macOS + Homebrew: an install that answers keeps the host runtime"
out=$(run_gate "$LOCAL_URL" no "y" "" macOS yes yes CURL_UP_AFTER=0)
if [[ "$out" == *"where=host"* && "$out" == *"Install it now with Homebrew?"* \
      && "$out" != *"press Enter to re-check"* ]]; then
    pass
else
    fail "expected a clean brew install, got: $out"
fi

start_test "macOS + Homebrew: brew succeeding but nothing answering is not ready"
out=$(run_gate "$LOCAL_URL" no "y,y" "" macOS yes yes)
[[ "$out" == *"where=container"* ]] && pass \
    || fail "a silent endpoint after brew must not pass, got: $out"

start_test "macOS + Homebrew: failed brew install still cannot proceed broken"
out=$(run_gate "$LOCAL_URL" no "y,y" "" macOS yes no)
[[ "$out" == *"where=container"* ]] && pass \
    || fail "failed brew install then container should switch, got: $out"

start_test "macOS without Homebrew: no scripted-installer offer, loop still guards"
# Ollama's install.sh is Linux-only, so a brewless Mac gets no auto-install
# offer — just the container question and then the loop.
out=$(run_gate "$LOCAL_URL" no "n,ENTER,container" "" macOS no no)
if [[ "$out" == *"where=container"* && "$out" != *"official installer"* \
      && "$out" != *"Homebrew"* && "$out" == *"press Enter to re-check"* ]]; then
    pass
else
    fail "brewless Mac should skip install offers but keep the loop, got: $out"
fi

start_test "macOS start hint names the app, never systemctl"
out=$(run_gate "$LOCAL_URL" no "n,ENTER" 1 macOS no no)
if [[ "$out" == *"Ollama app"* && "$out" != *"systemctl"* ]]; then
    pass
else
    fail "expected a macOS start hint, got: $out"
fi

start_test "macOS is never bridge-probed — Docker Desktop proxies the loopback"
out=$(run_gate "$LOCAL_URL" no "y" "" macOS yes yes CURL_UP_AFTER=0 \
        DOCKER_GW=172.17.0.1 BRIDGE_OK=no)
if [[ "$out" == *"where=host"* && "$out" != *"REFUSES"* ]]; then
    pass
else
    fail "the bridge probe is Linux-only, got: $out"
fi

# ── 9. the two installers must not drift apart ──
start_test "install.ps1 carries the container fallback"
grep -q "Use the bundled ollama container instead" scripts/install.ps1 && pass \
    || fail "install.ps1 lost the container-fallback question"

start_test "install.ps1 skips the nag for remote endpoints"
grep -q 'targetsHere' scripts/install.ps1 && pass \
    || fail "install.ps1 lost the remote-endpoint guard"

start_test "install.ps1 carries the no-proceed re-check loop"
if grep -q "press Enter to re-check" scripts/install.ps1 \
   && ! grep -q "Continuing WITHOUT an LLM runtime" scripts/install.ps1; then
    pass
else
    fail "install.ps1 lost the re-check loop or kept the proceed-broken exit"
fi

start_test "install.ps1 also refuses to call a bare binary a runtime"
# Every place it finds an ollama binary must go through the wait, and the
# old 'found it, good enough' assignments must be gone.
if grep -q 'function Wait-ForOllama' scripts/install.ps1 \
   && grep -q 'function Start-OllamaHost' scripts/install.ps1 \
   && ! grep -q 'Ollama found but not answering yet' scripts/install.ps1 \
   && ! grep -q 'launch it once so it starts serving' scripts/install.ps1; then
    pass
else
    fail "install.ps1 still accepts a binary as a runtime"
fi

echo ""
if (( TESTS_FAILED > 0 )); then
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} tests failed"
    exit 1
fi
echo "✓ all ${TESTS_RUN} tests passed"
