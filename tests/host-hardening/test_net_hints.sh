#!/usr/bin/env bash
# Regression tests for the launcher's network hint files.
#
# The field bug (vms, 2026-11-27): MediaMTX advertised no WebRTC ICE host
# at all. Core builds that list from the admin's address, STUN and the
# launcher's seed; with STUN unanswered and the seed empty it pushed [],
# and every live view from another machine timed out into HLS.
#
# Core now also reads ./data/net-hints/advertise-ips on every refresh.
# The rules these tests defend:
#   * advertise-ips lists the LAN address first, then every other host
#     address INCLUDING VPN tunnels (wg/tailscale/tun), so a viewer on the
#     VPN gets a candidate it can reach;
#   * host-ips stays VPN-free: ONVIF discovery scans each entry as a /24
#     camera LAN;
#   * with nginx bound to one address (dual-NIC), advertise-ips holds that
#     address only — the media ports are published there alone;
#   * detect_lan_ip on a host with no default route picks the first
#     non-tunnel global address, not `hostname -I` ordering luck.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running net-hints tests"
echo ""

# ── The REAL start.sh functions ──
extract() { awk "/^$1\\(\\) \\{/,/^\\}/" "$REPO_ROOT/start.sh"; }
FUNCS=""
for fn in get_env_var is_vpn_tunnel_source detect_routable_nics detect_advertise_nics write_net_hints write_advertise_hints detect_lan_ip; do
    body=$(extract "$fn")
    if [[ -z "$body" ]]; then
        echo "✗ could not extract $fn from start.sh" >&2
        exit 2
    fi
    FUNCS="$FUNCS
$body"
done
YELLOW=""; NC=""
eval "$FUNCS"

WORK=$(mktemp -d)
# The harness must never write into the checkout (an empty WORK would
# leave us in it): assert the temp dir before using it.
if [ -z "$WORK" ] || [ ! -d "$WORK" ]; then
    echo "✗ mktemp -d failed" >&2
    exit 2
fi
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/bin"

# A fake `ip` that answers from $FAKE_ADDRS ("iface:ip" per line) and
# $FAKE_ROUTE (the `ip route get` line; empty = no default route).
cat > "$WORK/bin/ip" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "-4" ] && [ "$2" = "-o" ] && [ "$3" = "addr" ]; then
    printf '%s\n' "$FAKE_ADDRS" | while IFS=: read -r i a; do
        [ -n "$a" ] && echo "2: $i    inet $a/24 brd x scope global $i"
    done
    exit 0
fi
if [ "$1" = "-4" ] && [ "$2" = "route" ] && [ "$3" = "get" ]; then
    [ -n "${FAKE_ROUTE:-}" ] && echo "$FAKE_ROUTE"
    exit 0
fi
exit 1
SH
chmod +x "$WORK/bin/ip"
# `hostname -I` deliberately lists the VPN address first: the old fallback
# took it.
cat > "$WORK/bin/hostname" <<'SH'
#!/usr/bin/env bash
[ "$1" = "-I" ] && echo "${FAKE_HOSTNAME_I:-}"
SH
chmod +x "$WORK/bin/hostname"
PATH="$WORK/bin:$PATH"

reset_site() {
    rm -rf "$WORK/site"; mkdir -p "$WORK/site"; cd "$WORK/site" || exit 2
    : > .env
    unset NGINX_BIND_HOST OPENNVR_HOST_IP OPENNVR_LAN_IPS
}

FAKE_ADDRS="eth0:192.168.2.30
wg0:10.8.0.1
tailscale0:100.101.102.103
tun0:10.200.0.5
docker0:172.17.0.1
br-abc:172.28.0.1"
export FAKE_ADDRS FAKE_ROUTE="" FAKE_HOSTNAME_I="100.101.102.103 192.168.2.30"

# ── 1. advertise-ips: LAN first, VPN tunnels included, Docker dropped ──
start_test "advertise-ips lists the LAN address first and keeps every VPN tunnel"
reset_site
export OPENNVR_HOST_IP=192.168.2.30
write_net_hints
got=$(cat data/net-hints/advertise-ips 2>/dev/null)
want="192.168.2.30 10.8.0.1 100.101.102.103 10.200.0.5"
if [ "$got" = "$want" ]; then pass; else fail "got '$got', want '$want'"; fi

# ── 2. host-ips stays VPN-free ──
start_test "host-ips (ONVIF discovery's camera LANs) has no VPN address"
got=$(cat data/net-hints/host-ips 2>/dev/null)
if [ "$got" = "192.168.2.30" ]; then pass; else fail "host-ips is '$got'"; fi

# ── 3. Dual-NIC: only the bound address ──
start_test "with NGINX_BIND_HOST set, advertise-ips is that address alone"
reset_site
echo "NGINX_BIND_HOST=192.168.1.10" > .env
export OPENNVR_HOST_IP=192.168.1.10 OPENNVR_LAN_IPS=192.168.1.10,10.10.0.1
write_net_hints
got=$(cat data/net-hints/advertise-ips 2>/dev/null)
if [ "$got" = "192.168.1.10" ]; then pass; else fail "got '$got'"; fi

start_test "NGINX_BIND_HOST=0.0.0.0 is not a dual-NIC host"
reset_site
export NGINX_BIND_HOST=0.0.0.0 OPENNVR_HOST_IP=192.168.2.30
write_net_hints
got=$(cat data/net-hints/advertise-ips 2>/dev/null)
case "$got" in "192.168.2.30 "*10.8.0.1*) pass ;; *) fail "got '$got'" ;; esac

# ── 4. Written atomically, no .tmp left behind ──
start_test "no advertise-ips.tmp is left behind"
if [ ! -e data/net-hints/advertise-ips.tmp ]; then pass; else fail "tmp file left"; fi

# ── 5. detect_lan_ip without a default route ──
start_test "detect_lan_ip with no default route picks the LAN NIC, not the VPN"
reset_site
FAKE_ROUTE=""
got=$(detect_lan_ip)
if [ "$got" = "192.168.2.30" ]; then pass; else fail "got '$got' (hostname -I order would give the VPN)"; fi

start_test "detect_lan_ip still prefers the default route's source"
reset_site
FAKE_ROUTE="1.1.1.1 via 192.168.2.1 dev eth0 src 192.168.2.30 uid 1000"
got=$(detect_lan_ip)
if [ "$got" = "192.168.2.30" ]; then pass; else fail "got '$got'"; fi

start_test "a VPN default route is skipped for the first LAN NIC"
reset_site
FAKE_ROUTE="1.1.1.1 dev tailscale0 src 100.101.102.103 uid 1000"
got=$(detect_lan_ip)
if [ "$got" = "192.168.2.30" ]; then pass; else fail "got '$got'"; fi

cd "$REPO_ROOT" || exit 2
echo ""
echo "Ran $TESTS_RUN, failed $TESTS_FAILED"
[ "$TESTS_FAILED" -eq 0 ]
