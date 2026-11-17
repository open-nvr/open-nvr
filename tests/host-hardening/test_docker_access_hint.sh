#!/usr/bin/env bash
# Regression tests: a Docker socket this user may not open is reported as
# such, with the docker-group fix, not as "Docker is not running".
#
# Seen on a fresh Ubuntu box (2026-11-16): `systemctl status docker` said
# active, the installer said "Docker is not running", because the user was
# not in the docker group and every `docker info` failure was read as a
# daemon that is down. The same run then died on
# "cp: cannot create regular file '.env': Permission denied" because the
# checkout had been cloned with sudo; the installer now names that too.
#
# Never touches real Docker: `docker` resolves to a shim whose answer is
# chosen by FAKE_DOCKER_MODE (denied | down | ok).
#
# Run with: bash tests/host-hardening/test_docker_access_hint.sh
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
skip() { echo "SKIP ($1)"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running Docker access-hint tests"
echo ""

# ── the shim ─────────────────────────────────────────────────────────
# The two client errors as the real docker CLI prints them; everything that
# only needs the client (compose version) always works.
BIN="$WORK/bin"
mkdir -p "$BIN"
cat > "$BIN/docker" <<'SHIM'
#!/usr/bin/env bash
daemon() {
    case "${FAKE_DOCKER_MODE:-ok}" in
        denied) echo "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info\": dial unix /var/run/docker.sock: connect: permission denied" >&2; exit 1 ;;
        down)   echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2; exit 1 ;;
    esac
}
case "$1" in
    compose) [ "${2:-}" = "version" ] && { echo "Docker Compose version v2.29.0"; exit 0; }; daemon; exit 0 ;;
    info|version) daemon; echo "ok"; exit 0 ;;
    *) daemon; exit 0 ;;
esac
SHIM
chmod +x "$BIN/docker"
# The scripts refuse anything but Linux/macOS; the test may run elsewhere.
cat > "$BIN/uname" <<'SHIM'
#!/usr/bin/env bash
case "${1:-}" in -m) echo x86_64 ;; *) echo Linux ;; esac
SHIM
chmod +x "$BIN/uname"

ME="$(id -un)"

# install.sh refuses to run without a terminal on stdin, before it looks at
# Docker. util-linux `script` lends it a pseudo-terminal; without that
# (macOS ships a different script(1)) the installer cases are skipped.
HAVE_PTY=0
if script --version >/dev/null 2>&1; then HAVE_PTY=1; fi
with_tty() {   # run "$@" on a pty, output on stdout, exit code preserved
    script -qec "$(printf '%q ' "$@")" /dev/null </dev/null 2>&1 | tr -d '\r'
    return "${PIPESTATUS[0]}"
}

run_script() {   # $1 = mode, rest = command line, run from the repo root
    local mode="$1"; shift
    ( cd "$REPO_ROOT" && env FAKE_DOCKER_MODE="$mode" PATH="${BIN}:${PATH}" "$@" </dev/null 2>&1 )
}
run_installer() {   # $1 = mode, $2 = folder to run in
    ( cd "$2" && FAKE_DOCKER_MODE="$1" PATH="${BIN}:${PATH}" with_tty bash scripts/install.sh )
}

expect_denied_hint() {   # $1 = output, $2 = what ran
    if echo "$1" | grep -q "permission denied on the Docker socket" \
       && echo "$1" | grep -q "usermod -aG docker ${ME}" \
       && ! echo "$1" | grep -q "Docker is not running"; then
        pass
    else
        fail "$2 should name the socket permission and the usermod fix, not a stopped daemon:
$1"
    fi
}

# ── 1-2. the installer ───────────────────────────────────────────────
start_test "install.sh: a socket this user may not open gets the docker-group hint"
if [ "$HAVE_PTY" -eq 0 ]; then skip "no util-linux script(1) for a pty"; else
    out=$(run_installer denied "$REPO_ROOT"); rc=$?
    if [ "$rc" -ne 0 ]; then expect_denied_hint "$out" "install.sh"; else fail "expected a non-zero exit; output:
$out"; fi
fi

start_test "install.sh: a daemon that is down is still 'not running'"
if [ "$HAVE_PTY" -eq 0 ]; then skip "no util-linux script(1) for a pty"; else
    out=$(run_installer down "$REPO_ROOT"); rc=$?
    if [ "$rc" -ne 0 ] && echo "$out" | grep -q "Docker is not running" \
       && ! echo "$out" | grep -q "usermod"; then
        pass
    else
        fail "expected 'Docker is not running' without the group hint; rc=${rc}, output:
$out"
    fi
fi

# ── 3. the uninstaller ───────────────────────────────────────────────
start_test "uninstall.sh: the same hint, and it says nothing was changed"
out=$(run_script denied bash scripts/uninstall.sh --yes); rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "Nothing has been changed"; then
    expect_denied_hint "$out" "uninstall.sh"
else
    fail "expected exit 1 and 'Nothing has been changed'; rc=${rc}, output:
$out"
fi

# ── 4. start.sh validate ─────────────────────────────────────────────
start_test "start.sh validate: the Docker check names the socket permission"
out=$(run_script denied bash start.sh validate); rc=$?
expect_denied_hint "$out" "start.sh validate"

start_test "start.sh validate: a daemon that is down is still 'not running'"
out=$(run_script down bash start.sh validate); rc=$?
if echo "$out" | grep -q "Docker is not running" && ! echo "$out" | grep -q "usermod"; then
    pass
else
    fail "expected 'Docker is not running' without the group hint; output:
$out"
fi

# ── 5. a checkout the user cannot write to ───────────────────────────
# A `sudo git clone` leaves the folder root-owned; the installer used to get
# as far as `cp .env.example .env` and die there. Root (and Windows, where
# chmod does not bite) cannot produce the condition, so the test is skipped.
start_test "install.sh: a read-only checkout is named before anything is written"
RO="$WORK/checkout"
mkdir -p "$RO/scripts"
cp "$REPO_ROOT/scripts/install.sh" "$RO/scripts/"
cp "$REPO_ROOT/docker-compose.yml" "$RO/"
chmod 555 "$RO"
if [ -w "$RO" ]; then
    skip "cannot make a folder read-only here"
elif [ "$HAVE_PTY" -eq 0 ]; then
    skip "no util-linux script(1) for a pty"
else
    out=$(run_installer ok "$RO"); rc=$?
    if [ "$rc" -ne 0 ] && echo "$out" | grep -q "is not writable by ${ME}" \
       && echo "$out" | grep -q "chown -R ${ME}:$(id -gn)" \
       && ! echo "$out" | grep -q "cannot create regular file"; then
        pass
    else
        fail "expected the chown hint and no cp error; rc=${rc}, output:
$out"
    fi
fi

echo ""
echo "Tests run: ${TESTS_RUN}, failed: ${TESTS_FAILED}"
[ "$TESTS_FAILED" -eq 0 ]
