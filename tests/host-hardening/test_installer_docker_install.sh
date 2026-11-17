#!/usr/bin/env bash
# Regression tests: on a Linux host without Docker, scripts/install.sh offers
# to install it with Docker's own steps, then joins the docker group and
# re-runs itself under that group.
#
# Nothing here reaches the real system: `docker` is absent from PATH until a
# fake `apt-get install docker-ce` (or `sh get-docker.sh`) puts a shim there,
# and sudo, curl, systemctl, usermod, dpkg and sg are shims that only record
# what they were asked to do. The installer wants a terminal on stdin, so
# util-linux script(1) lends it one.
#
# Run with: bash tests/host-hardening/test_installer_docker_install.sh
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
skip() { echo "SKIP ($1)"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running installer Docker-install tests"
echo ""

if ! script --version >/dev/null 2>&1; then
    echo "  SKIP: needs util-linux script(1) for a pty (Linux)."
    exit 0
fi
case "$(uname -s)" in Linux*) ;; *) echo "  SKIP: Linux only."; exit 0 ;; esac

ME="$(id -un)"

# ── a PATH with only what the installer needs, and no docker ─────────
TOOLS="$WORK/tools"
mkdir -p "$TOOLS"
for t in bash sh env awk grep sed cut tr cat printf mktemp tee sort head tail \
         readlink dirname basename rm mv cp chmod mkdir ls touch script id hash; do
    p=$(command -v "$t" 2>/dev/null) && [ -n "$p" ] && ln -s "$p" "$TOOLS/$t" 2>/dev/null
done

# ── the shims ────────────────────────────────────────────────────────
SHIMS="$WORK/shims"
mkdir -p "$SHIMS"
STATE="$WORK/state"

cat > "$SHIMS/uname" <<'SHIM'
#!/usr/bin/env bash
case "${1:-}" in -m) echo x86_64 ;; *) echo "${FAKE_UNAME:-Linux}" ;; esac
SHIM
# sudo records the command and acts out the two installs: either one puts a
# `docker` on PATH. `tee` keeps what it was given, so the apt source line
# can be checked.
cat > "$SHIMS/sudo" <<'SHIM'
#!/usr/bin/env bash
S="$FAKE_STATE"
printf 'sudo %s\n' "$*" >> "$S/calls.log"
# FAKE_FAIL names a command line (prefix) that fails the way the real one would.
if [ -n "${FAKE_FAIL:-}" ] && case "$*" in "$FAKE_FAIL"*) true ;; *) false ;; esac; then
    echo "E: Unable to locate package docker-ce (fake)" >&2; exit 100
fi
case "$1" in
    tee) cat > "$S/$(basename "$2")" ;;
    apt-get) [ "$2" = install ] && case " $* " in *" docker-ce "*) install_docker=1 ;; esac ;;
    sh) install_docker=1 ;;
esac
if [ "${install_docker:-0}" = 1 ]; then
    printf '#!/usr/bin/env bash\necho ok\n' > "$FAKE_SHIMS/docker"; chmod +x "$FAKE_SHIMS/docker"
fi
exit 0
SHIM
cat > "$SHIMS/curl" <<'SHIM'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$FAKE_STATE/calls.log"
if [ "${FAKE_FAIL:-}" = curl ]; then echo "curl: (6) Could not resolve host: get.docker.com (fake)" >&2; exit 6; fi
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && printf '#!/bin/sh\n# fake get-docker.sh\n' > "$out"
exit 0
SHIM
for t in systemctl usermod dpkg sg apt-get; do
    cat > "$SHIMS/$t" <<SHIM
#!/usr/bin/env bash
printf '$t %s\\n' "\$*" >> "\$FAKE_STATE/calls.log"
[ "$t" = dpkg ] && echo amd64
exit 0
SHIM
done
chmod +x "$SHIMS"/*

os_release() {   # $1 = flavour → path of a fake os-release
    local f="$WORK/os-release.$1"
    case "$1" in
        ubuntu) printf 'ID=ubuntu\nID_LIKE=debian\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\n' ;;
        mint)   printf 'ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_CODENAME=wilma\nUBUNTU_CODENAME=noble\n' ;;
        debian) printf 'ID=debian\nVERSION_CODENAME=bookworm\n' ;;
        fedora) printf 'ID=fedora\nVERSION_ID=41\n' ;;
        raspbian) printf 'ID=raspbian\nID_LIKE=debian\nVERSION_CODENAME=bookworm\n' ;;
    esac > "$f"
    printf '%s' "$f"
}

# run_installer <flavour> <stdin text> [VAR=value ...] [-- args...]
# Resets the shims (docker gone again), runs the real installer on a pty.
run_installer() {
    local flavour="$1" input="$2"; shift 2
    local envs=() args=()
    while [ $# -gt 0 ]; do
        case "$1" in --) shift; args=("$@"); break ;; *) envs+=("$1") ;; esac
        shift
    done
    rm -rf "$STATE"; mkdir -p "$STATE"; : > "$STATE/calls.log"
    rm -f "$SHIMS/docker"
    (
        cd "$REPO_ROOT" || exit 99
        printf '%s' "$input" | env -i HOME="$HOME" TERM=dumb \
            PATH="${SHIMS}:${TOOLS}" FAKE_STATE="$STATE" FAKE_SHIMS="$SHIMS" \
            OPENNVR_OS_RELEASE="$(os_release "$flavour")" "${envs[@]}" \
            script -qec "bash scripts/install.sh ${args[*]}" /dev/null 2>&1 | tr -d '\r'
        exit "${PIPESTATUS[1]}"
    )
}
calls() { cat "$STATE/calls.log"; }

# ── 1. Ubuntu: Docker's apt repository, then group and re-exec ───────
start_test "Ubuntu: apt repository, service, docker group, re-run under sg"
out=$(run_installer ubuntu "" OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -eq 0 ] \
   && calls | grep -q "^sudo apt-get install .*docker-ce .*docker-compose-plugin" \
   && grep -q "^deb \[arch=amd64 signed-by=/etc/apt/keyrings/docker.asc\] https://download.docker.com/linux/ubuntu noble stable$" "$STATE/docker.list" \
   && calls | grep -q "^sudo curl .*https://download.docker.com/linux/ubuntu/gpg" \
   && calls | grep -q "^sudo systemctl enable --now docker$" \
   && calls | grep -q "^sudo usermod -aG docker ${ME}$" \
   && calls | grep -q "^sg docker -c bash .*scripts/install.sh install" \
   && ! calls | grep -q "get.docker.com" \
   && ! calls | grep -qE "snap|docker\.io|docker-compose-v2"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
docker.list: $(cat "$STATE/docker.list" 2>/dev/null)
output:
$out"
fi

start_test "the offer is printed before anything runs, and says sudo"
if echo "$out" | grep -q "Docker is not installed" && echo "$out" | grep -q "through sudo" \
   && echo "$out" | grep -q "apt repository for ubuntu/noble"; then
    pass
else
    fail "expected the offer text; output:
$out"
fi

# ── 2. derivatives follow their parent ───────────────────────────────
start_test "Linux Mint uses Ubuntu's repository under the Ubuntu codename"
out=$(run_installer mint "" OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -eq 0 ] && grep -q "download.docker.com/linux/ubuntu noble stable" "$STATE/docker.list" 2>/dev/null; then
    pass
else
    fail "rc=${rc}; docker.list: $(cat "$STATE/docker.list" 2>/dev/null); output:
$out"
fi

start_test "Debian uses Debian's repository under its own codename"
out=$(run_installer debian "" OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -eq 0 ] && grep -q "download.docker.com/linux/debian bookworm stable" "$STATE/docker.list" 2>/dev/null; then
    pass
else
    fail "rc=${rc}; docker.list: $(cat "$STATE/docker.list" 2>/dev/null); output:
$out"
fi

# ── 3. everything else: Docker's convenience script ──────────────────
start_test "Fedora: get.docker.com, downloaded unprivileged and run with sudo sh"
out=$(run_installer fedora "" OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -eq 0 ] && calls | grep -q "^curl .*https://get.docker.com -o " \
   && calls | grep -q "^sudo sh .*get-docker" && ! calls | grep -q "apt-get" \
   && calls | grep -q "^sudo usermod -aG docker ${ME}$" && calls | grep -q "^sg docker -c"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

start_test "32-bit Raspberry Pi OS takes the script, not Debian's repository"
out=$(run_installer raspbian "" OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -eq 0 ] && calls | grep -q "get.docker.com" && ! calls | grep -q "apt-get"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)"
fi

# ── 4. saying no, or never asking ────────────────────────────────────
start_test "answering n installs nothing and prints the manual steps"
out=$(run_installer ubuntu $'n\n'); rc=$?
if [ "$rc" -ne 0 ] && [ ! -s "$STATE/calls.log" ] \
   && echo "$out" | grep -q "Install Docker now?" \
   && echo "$out" | grep -q "docs.docker.com/engine/install" \
   && echo "$out" | grep -q "usermod -aG docker ${ME}"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

start_test "OPENNVR_INSTALL_DOCKER=0 never asks"
out=$(run_installer ubuntu "" OPENNVR_INSTALL_DOCKER=0); rc=$?
if [ "$rc" -ne 0 ] && [ ! -s "$STATE/calls.log" ] && ! echo "$out" | grep -q "Install Docker now?" \
   && echo "$out" | grep -q "docs.docker.com/engine/install"; then
    pass
else
    fail "rc=${rc}; output:
$out"
fi

start_test "--install-docker answers yes and keeps the mode"
out=$(run_installer ubuntu "" -- --install-docker); rc=$?
if [ "$rc" -eq 0 ] && ! echo "$out" | grep -q "Install Docker now?" \
   && calls | grep -q "^sg docker -c bash .*scripts/install.sh install$"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

# ── 5. a step that fails says which, why, and every way out ──────────
start_test "apt-get install fails: the step, Ubuntu's docs page, the by-hand route, what was left"
out=$(run_installer ubuntu "" OPENNVR_INSTALL_DOCKER=1 FAKE_FAIL="apt-get install -y -qq docker-ce"); rc=$?
if [ "$rc" -ne 0 ] \
   && echo "$out" | grep -q "Unable to locate package docker-ce (fake)" \
   && echo "$out" | grep -q "Installing Docker failed at:  sudo apt-get install -y -qq docker-ce" \
   && echo "$out" | grep -q "https://docs.docker.com/engine/install/ubuntu/" \
   && echo "$out" | grep -q "usermod -aG docker ${ME}" \
   && echo "$out" | grep -q "OPENNVR_DOCKER_INSTALL=script" \
   && echo "$out" | grep -q "/etc/apt/sources.list.d/docker.list" \
   && ! calls | grep -q "usermod" && ! calls | grep -q "^sg "; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

start_test "the convenience script cannot be downloaded: the step, the generic docs page, nothing left"
out=$(run_installer fedora "" OPENNVR_INSTALL_DOCKER=1 FAKE_FAIL=curl); rc=$?
if [ "$rc" -ne 0 ] \
   && echo "$out" | grep -q "Could not resolve host" \
   && echo "$out" | grep -q "Installing Docker failed at:  curl https://get.docker.com" \
   && echo "$out" | grep -q "https://docs.docker.com/engine/install/$" \
   && echo "$out" | grep -q "Nothing was left" \
   && ! echo "$out" | grep -q "OPENNVR_DOCKER_INSTALL=script" \
   && ! calls | grep -q "^sudo "; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

start_test "OPENNVR_DOCKER_INSTALL=script forces the script on Ubuntu"
out=$(run_installer ubuntu "" OPENNVR_INSTALL_DOCKER=1 OPENNVR_DOCKER_INSTALL=script); rc=$?
if [ "$rc" -eq 0 ] && calls | grep -q "get.docker.com" && ! calls | grep -q "apt-get"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)"
fi

start_test "declining names the distribution's own install page"
out=$(run_installer debian $'n\n'); rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "https://docs.docker.com/engine/install/debian/"; then
    pass
else
    fail "rc=${rc}; output:
$out"
fi

# ── 6. hosts where it cannot be done ─────────────────────────────────
start_test "macOS: the Docker Desktop link, no install attempted"
out=$(run_installer ubuntu "" FAKE_UNAME=Darwin OPENNVR_INSTALL_DOCKER=1); rc=$?
if [ "$rc" -ne 0 ] && [ ! -s "$STATE/calls.log" ] && echo "$out" | grep -q "Docker Desktop"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

start_test "no sudo on the host: the manual steps, nothing run"
mv "$SHIMS/sudo" "$WORK/sudo.keep"
out=$(run_installer ubuntu "" OPENNVR_INSTALL_DOCKER=1); rc=$?
mv "$WORK/sudo.keep" "$SHIMS/sudo"
if [ "$rc" -ne 0 ] && [ ! -s "$STATE/calls.log" ] && echo "$out" | grep -q "no sudo"; then
    pass
else
    fail "rc=${rc}; calls:
$(calls)
output:
$out"
fi

echo ""
echo "Tests run: ${TESTS_RUN}, failed: ${TESTS_FAILED}"
[ "$TESTS_FAILED" -eq 0 ]
