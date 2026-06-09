#!/usr/bin/env bash
# ============================================================================
# run-examples.sh — generic demo runner for everything under examples/
#
# The examples are independent template apps (mostly NATS subscribers) that
# ride alongside a running OpenNVR stack. This script discovers them by
# scanning examples/*/ (NO hardcoded list — new examples are picked up
# automatically) and can list, smoke-test, or run them against the demo
# stack started by docker-compose.tier0.yml.
#
# Usage:
#   scripts/run-examples.sh list              # show discovered examples
#   scripts/run-examples.sh smoke [name...]   # launch each, verify it stays
#                                             # up SMOKE_SECONDS, then stop
#   scripts/run-examples.sh run <name>        # run one example in foreground
#
# Env knobs:
#   SMOKE_SECONDS=8   how long a smoke-launched example must survive to PASS
#   WITH_DOCKER=0     set 1 to also build+run Dockerfile-based examples
#   NO_STACK_CHECK=0  set 1 to skip the "is the tier0 stack up?" check
# ============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLES_DIR="$REPO_ROOT/examples"
SMOKE_SECONDS="${SMOKE_SECONDS:-8}"
WITH_DOCKER="${WITH_DOCKER:-0}"
NO_STACK_CHECK="${NO_STACK_CHECK:-0}"

c_green=$'\033[0;32m'; c_red=$'\033[0;31m'; c_yellow=$'\033[1;33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
info() { printf '%s\n' "$*"; }
ok()   { printf '%s✓ %s%s\n' "$c_green" "$*" "$c_off"; }
err()  { printf '%s✗ %s%s\n' "$c_red" "$*" "$c_off"; }
warn() { printf '%s! %s%s\n' "$c_yellow" "$*" "$c_off"; }

# Pick a Python runner: prefer uv (handles per-example deps), else fall back to python3.
if command -v uv >/dev/null 2>&1; then PYRUN="uv"; else PYRUN="python3"; fi

# Discover example dirs: any examples/* that contains a pyproject.toml or Dockerfile.
discover() {
    for d in "$EXAMPLES_DIR"/*/; do
        [ -f "${d}pyproject.toml" ] || [ -f "${d}Dockerfile" ] || continue
        printf '%s\n' "$(basename "$d")"
    done
}

# Resolve an example's main module file. Convention (uniform across every
# example): the entrypoint is <dir-name-with-underscores>.py, run as
# `python <main>.py --config config.yml`. Echoes the basename, or nothing.
main_module() {
    local dir="$1" name; name="$(basename "$dir")"
    local cand="${name//-/_}.py"
    [ -f "$dir/$cand" ] && { printf '%s' "$cand"; return 0; }
    return 1
}

# Build the python invocation for an example (uv handles per-example deps;
# plain python3 is the fallback). Runs in a subshell from the example dir.
py_invoke() {  # args: dir main.py
    local dir="$1" main="$2"
    if [ "$PYRUN" = "uv" ]; then ( cd "$dir" && uv run python "$main" --config config.yml )
    else ( cd "$dir" && python3 "$main" --config config.yml ); fi
}

# Ensure a runnable config.yml exists (copy from the shipped example).
ensure_config() {
    local dir="$1"
    if [ ! -f "$dir/config.yml" ] && [ -f "$dir/config.example.yml" ]; then
        cp "$dir/config.example.yml" "$dir/config.yml"
        warn "  created config.yml from config.example.yml (review for real creds)"
    fi
}

stack_up() {
    [ "$NO_STACK_CHECK" = "1" ] && return 0
    curl -fsS "http://127.0.0.1:9997/v3/paths/list" >/dev/null 2>&1
}

cmd_list() {
    info "Examples under $EXAMPLES_DIR:"
    info ""
    while read -r name; do
        local dir="$EXAMPLES_DIR/$name"
        local entry; entry="$(main_module "$dir" 2>/dev/null || true)"
        local docker="-"; [ -f "$dir/Dockerfile" ] && docker="docker"
        printf "  %-28s %-10s entry=%s\n" "$name" "$docker" "${entry:-<none>}"
    done < <(discover)
}

# Launch one example in the background, require it to survive SMOKE_SECONDS.
smoke_one() {
    local name="$1" dir="$EXAMPLES_DIR/$name"
    ensure_config "$dir"

    local main; main="$(main_module "$dir" 2>/dev/null || true)"
    if [ -n "$main" ]; then
        local log; log="$(mktemp)"
        py_invoke "$dir" "$main" >"$log" 2>&1 &
        local pid=$!
        sleep "$SMOKE_SECONDS"
        if kill -0 "$pid" 2>/dev/null; then
            # Still running after the window → a healthy long-lived daemon.
            kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
            ok "$name: stayed up ${SMOKE_SECONDS}s (connected to stack)"; rm -f "$log"; return 0
        fi
        wait "$pid" 2>/dev/null; local rc=$?
        if [ "$rc" -eq 0 ]; then
            # Clean exit before the window → a one-shot example (e.g. --once). PASS.
            ok "$name: completed cleanly (exit 0)"; rm -f "$log"; return 0
        fi
        err "$name: exited with code $rc — last lines:"; printf '%s' "$c_dim"; tail -n 6 "$log"; printf '%s' "$c_off"; rm -f "$log"; return 1
    elif [ -f "$dir/Dockerfile" ] && [ "$WITH_DOCKER" = "1" ]; then
        local tag="opennvr-example-$name"
        info "  building $tag ..."
        docker build -q -t "$tag" "$dir" >/dev/null || { err "$name: docker build failed"; return 1; }
        ok "$name: image built ($tag)"; return 0
    elif [ -f "$dir/Dockerfile" ]; then
        warn "$name: Dockerfile-only (set WITH_DOCKER=1 to build) — skipped"; return 2
    else
        warn "$name: no <name>.py entry and no Dockerfile — skipped"; return 2
    fi
}

cmd_smoke() {
    if ! stack_up; then
        err "Demo stack not reachable on 127.0.0.1:9997."
        info "  Start it first:  docker compose -f docker-compose.tier0.yml up -d"
        info "  (or set NO_STACK_CHECK=1 to smoke-test offline)"
        exit 1
    fi
    local -a targets
    if [ "$#" -gt 0 ]; then targets=("$@"); else mapfile -t targets < <(discover); fi
    local pass=0 fail=0 skip=0
    for name in "${targets[@]}"; do
        info ""; info "── $name ─────────────────────────────"
        smoke_one "$name"; case $? in 0) pass=$((pass+1));; 1) fail=$((fail+1));; *) skip=$((skip+1));; esac
    done
    info ""; info "════════════════════════════════════════"
    printf "  %s%d passed%s  %s%d failed%s  %s%d skipped%s\n" "$c_green" "$pass" "$c_off" "$c_red" "$fail" "$c_off" "$c_yellow" "$skip" "$c_off"
    [ "$fail" -eq 0 ]
}

cmd_run() {
    local name="${1:-}"; [ -z "$name" ] && { err "usage: run-examples.sh run <name>"; exit 2; }
    local dir="$EXAMPLES_DIR/$name"; [ -d "$dir" ] || { err "no such example: $name"; exit 2; }
    ensure_config "$dir"
    local main; main="$(main_module "$dir")" || { err "$name: no <name>.py entry module"; exit 2; }
    info "Running $name ($main --config config.yml) in foreground — Ctrl-C to stop."
    py_invoke "$dir" "$main"
}

case "${1:-smoke}" in
    list)  cmd_list ;;
    smoke) shift || true; cmd_smoke "$@" ;;
    run)   shift || true; cmd_run "$@" ;;
    *)     err "unknown command: $1"; info "commands: list | smoke [name...] | run <name>"; exit 2 ;;
esac
