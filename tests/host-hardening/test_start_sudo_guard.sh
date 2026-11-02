#!/usr/bin/env bash
# Regression tests: start.sh refuses to run under sudo.
# A `sudo ./start.sh` leaves .env and the checkout root-owned, and every
# later run as the normal user fails with ".env: Permission denied".
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running start.sh sudo-guard tests"
echo ""

# A fake `id` on PATH makes the script believe it runs as uid 0 without
# needing root in the test environment; `id -gn <user>` answers too.
FAKE_BIN="$(mktemp -d)"
cat > "${FAKE_BIN}/id" <<'FAKE'
#!/usr/bin/env bash
case "$1" in
  -u) echo 0 ;;
  -gn) echo "${2:-root}" ;;
  *) echo 0 ;;
esac
FAKE
chmod +x "${FAKE_BIN}/id"
trap 'rm -rf "${FAKE_BIN}"' EXIT

run_start() {   # $1 = SUDO_USER value ("" = unset), $2 = command, rest = extra env
    local sudo_user="$1"; shift
    local cmd="$1"; shift
    if [ -n "$sudo_user" ]; then
        env "$@" SUDO_USER="$sudo_user" PATH="${FAKE_BIN}:${PATH}" \
            bash "${REPO_ROOT}/start.sh" "$cmd" </dev/null 2>&1
    else
        env "$@" -u SUDO_USER PATH="${FAKE_BIN}:${PATH}" \
            bash "${REPO_ROOT}/start.sh" "$cmd" </dev/null 2>&1
    fi
}

# ── 1. sudo + a real command is refused before anything runs ──
start_test "sudo ./start.sh up is refused with the repair hint"
out=$(run_start alice up); rc=$?
if [ "$rc" -eq 1 ] && echo "$out" | grep -q "Do not run start.sh with sudo" \
   && echo "$out" | grep -q "chown -R alice:alice"; then
    pass
else
    fail "expected exit 1 and the sudo refusal; rc=${rc}, output:
${out}"
fi

# ── 2. the refusal names the docker-group fix for the invoking user ──
start_test "refusal tells the user to join the docker group"
if echo "$out" | grep -q "usermod -aG docker alice"; then
    pass
else
    fail "expected 'usermod -aG docker alice' in:
${out}"
fi

# ── 3. OPENNVR_ALLOW_SUDO=1 skips the guard ──
# An unknown command stops the script right after the guard, so the test
# never reaches docker: it only proves the guard did not fire.
start_test "OPENNVR_ALLOW_SUDO=1 bypasses the guard"
out=$(run_start alice no-such-command OPENNVR_ALLOW_SUDO=1); rc=$?
if ! echo "$out" | grep -q "Do not run start.sh with sudo" && echo "$out" | grep -q "Unknown command"; then
    pass
else
    fail "guard fired despite OPENNVR_ALLOW_SUDO=1; rc=${rc}, output:
${out}"
fi

# ── 4. a plain root login (no SUDO_USER) is not refused ──
start_test "root without SUDO_USER passes the guard"
out=$(run_start "" no-such-command); rc=$?
if ! echo "$out" | grep -q "Do not run start.sh with sudo" && echo "$out" | grep -q "Unknown command"; then
    pass
else
    fail "guard fired for a plain root login; rc=${rc}, output:
${out}"
fi

echo ""
echo "Tests run: ${TESTS_RUN}, failed: ${TESTS_FAILED}"
[ "$TESTS_FAILED" -eq 0 ]
