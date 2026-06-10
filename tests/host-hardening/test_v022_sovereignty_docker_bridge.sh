#!/usr/bin/env bash
# ============================================================
# Regression test for the V-022 sovereignty validator's
# Docker-bridge handling (ISSUE-28).
#
# CONTEXT
# -------
# Operator hit ``RuntimeError: V-022: AI_SOVEREIGNTY=local_only
# requires every adapter URL to be loopback`` on a tier0 deploy
# because the registry has ``ADAPTER_URL=http://yolov8-adapter:9002``
# (Docker service DNS) which the validator's loopback-only check
# rejected.
#
# The V-022 claim is "all AI inference on this physical machine."
# Docker bridge networks are confined to a single host — packets
# never leave the kernel networking stack. So bridge-network
# adapter URLs are equally "on this machine" for sovereignty
# purposes; the validator was just narrowed too aggressively
# during the host-networking era.
#
# CONTRACT
# --------
# kai-c/main.py's host check (renamed ``_host_is_on_this_machine``)
# must accept:
#   * loopback hostnames + IPs
#   * IPs / hostnames resolving into OPENNVR_DOCKER_SUBNET
#
# and reject:
#   * non-bridge RFC1918 (peer host on LAN)
#   * public IPs
#
# Run with: bash tests/host-hardening/test_v022_sovereignty_docker_bridge.sh
# ============================================================

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running V-022 sovereignty / Docker-bridge tests"
echo ""

# ── 1. function exists with the new name + signature ─────────
start_test "kai-c/main.py defines _host_is_on_this_machine"
result=$(python3 - "${REPO_ROOT}" <<'PY'
import ast, sys
from pathlib import Path
tree = ast.parse((Path(sys.argv[1]) / "kai-c/main.py").read_text())
names = {node.name for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef)}
sys.exit(0 if "_host_is_on_this_machine" in names else 1)
PY
)
if [ $? -eq 0 ]; then
    pass
else
    fail "kai-c/main.py must define _host_is_on_this_machine (ISSUE-28)"
fi

# ── 2. function logic accepts Docker bridge IPs + rejects peer hosts ──
# Inline-port the function so we can exercise it without importing the
# full main.py (which pulls heavy runtime deps we don't have in test).
start_test "_host_is_on_this_machine logic: bridge ✓, loopback ✓, LAN peer ✗, public ✗"
result=$(python3 - <<'PY'
import socket, ipaddress, os

# Same defaults as kai-c/main.py.
os.environ.setdefault("OPENNVR_DOCKER_SUBNET", "172.28.0.0/16")
_DOCKER_BRIDGE_SUBNET = os.getenv("OPENNVR_DOCKER_SUBNET", "172.28.0.0/16")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_real = socket.getaddrinfo
def _mock(host, *a, **k):
    if host == "yolov8-adapter":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.28.0.7", 0))]
    if host == "external-vm.lan":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.50", 0))]
    return _real(host, *a, **k)
socket.getaddrinfo = _mock

def _host_is_on_this_machine(host):
    if not host: return False
    h = host.strip("[]").lower()
    if h in _LOOPBACK_HOSTS: return True
    try:
        ip = ipaddress.ip_address(h)
        if ip.is_loopback: return True
        try:
            if ip in ipaddress.ip_network(_DOCKER_BRIDGE_SUBNET): return True
        except (ValueError, TypeError):
            pass
        return False
    except ValueError:
        pass
    saved = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(2.0)
        try:
            infos = socket.getaddrinfo(h, None)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(saved)
    if not infos: return False
    try:
        bridge_net = ipaddress.ip_network(_DOCKER_BRIDGE_SUBNET)
    except (ValueError, TypeError):
        bridge_net = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback: continue
        if bridge_net is not None and ip in bridge_net: continue
        return False
    return True

# Required behaviour table — host : expected result.
cases = [
    ("yolov8-adapter",  True),   # the original failure case
    ("127.0.0.1",        True),
    ("::1",              True),
    ("localhost",        True),
    ("172.28.0.7",       True),  # direct bridge IP
    ("external-vm.lan",  False), # peer host on LAN — must reject
    ("8.8.8.8",          False), # public — must reject
]
fails = [(h, w, _host_is_on_this_machine(h)) for h, w in cases
         if _host_is_on_this_machine(h) != w]
print("ok" if not fails else f"fails: {fails}")
PY
)
if echo "$result" | grep -q "^ok"; then
    pass
else
    fail "${result}
V-022 host check must accept bridge URLs and reject peer hosts. See
ISSUE-28 — the check was previously loopback-only which broke tier0."
fi

# ── 3. the user-facing error message names the bridge subnet ─
# So operators who DO hit a true violation (e.g. ADAPTER_URL pointing
# at a peer VM) get a clear, actionable error mentioning their
# bridge subnet, not just "must be loopback."
start_test "V-022 error message references the bridge subnet"
if grep -q "Docker bridge subnet" "${REPO_ROOT}/kai-c/main.py"; then
    pass
else
    fail "kai-c/main.py's V-022 error must reference the Docker bridge subnet
so operators know what URLs are accepted in tier0 mode."
fi

# ── 4. AdapterRegistry's default URL is sovereignty-compatible ──
# tier0.yml ships ADAPTER_URL=http://yolov8-adapter:9002 which must
# pass the bridge check. Verify the compose value would survive the
# validator's parse step.
start_test "tier0.yml's default ADAPTER_URL is sovereignty-local"
adapter_url=$(python3 - "${REPO_ROOT}" <<'PY'
import sys, yaml
from pathlib import Path
c = yaml.safe_load((Path(sys.argv[1]) / "docker-compose.tier0.yml").read_text())
env = c["services"]["opennvr-core"].get("environment", []) or []
for entry in env:
    if isinstance(entry, str) and entry.startswith("ADAPTER_URL="):
        print(entry.split("=", 1)[1])
        break
PY
)
case "$adapter_url" in
    http://yolov8-adapter:*|http://localhost:*|http://127.0.0.1:*)
        pass
        ;;
    *)
        fail "tier0.yml ships ADAPTER_URL='${adapter_url}' — must be a
sovereignty-local URL (Docker service DNS, loopback, or bridge IP)."
        ;;
esac

# ── Summary ────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────"
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
if [ "$TESTS_FAILED" -eq 0 ]; then echo "Result       : all green"; exit 0
else echo "Result       : failures"; exit 1; fi
