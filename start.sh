#!/usr/bin/env bash
# ============================================================
# OpenNVR - Smart Launcher (Linux / macOS)
# ============================================================
# One command does it all: run ./start.sh with no arguments. On a fresh
# checkout it launches the interactive installer (creates and configures .env,
# builds, and starts). On later runs it asks whether to start as-is or
# reconfigure. The sub-commands below are for scripted / power use.
#
# Usage:
#   ./start.sh              # smart start: install on first run, else start/reconfigure
#   ./start.sh up           # start now using the existing .env (no prompt)
#   ./start.sh build        # rebuild images and start
#   ./start.sh install      # re-run the interactive installer (reconfigure)
#   ./start.sh reconfigure  # alias for install
#   ./start.sh down         # stop all services
#   ./start.sh logs         # tail logs
#   ./start.sh status       # show container status
#   ./start.sh validate     # run pre-flight checks only
#   ./start.sh token        # re-print the first-time setup token
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BRIGHT_CYAN='\033[1;36m'
GRAY='\033[38;5;245m'
WHITE='\033[1;37m'
NC='\033[0m'

# ── Detect OS ──────────────────────────────────────────────
# All supported platforms use the canonical bridge-networked stack.
# Camera discovery must use explicit IPs or unicast subnet scanning;
# multicast WS-Discovery and host-network Compose are not supported.
OS="$(uname -s)"
case "$OS" in
  Linux*)
    COMPOSE_FILE="docker-compose.yml"
    OS_LABEL="Linux (standard stack — bridge networking + TLS edge)"
    ;;
  Darwin*)
    COMPOSE_FILE="docker-compose.yml"
    OS_LABEL="macOS (bridge network mode)"
    ;;
  *)
    echo -e "${RED}Unsupported OS: $OS${NC}"
    echo "Please use start.ps1 on Windows."
    exit 1
    ;;
esac

# Operator escape hatch — pin a specific compose file regardless of
# OS detection. Useful for testing custom Compose overlays.
if [ -n "${OPENNVR_COMPOSE_FILE:-}" ]; then
    if [ -f "${OPENNVR_COMPOSE_FILE}" ]; then
        COMPOSE_FILE="${OPENNVR_COMPOSE_FILE}"
        OS_LABEL="${OS_LABEL} (overridden via OPENNVR_COMPOSE_FILE)"
    else
        echo -e "${RED}OPENNVR_COMPOSE_FILE=${OPENNVR_COMPOSE_FILE} not found${NC}"
        exit 1
    fi
fi

COMMAND="${1:-start}"

# ── Helper: read a value from .env ────────────────────────
get_env_var() {
    local key="$1"
    grep -E "^${key}=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'"
}

# ── Build Docker Compose profile args ─────────────────────
compose_args() {
    local args="-f $COMPOSE_FILE"
    local example_compose example_profile
    example_compose=$(get_env_var "OPENNVR_EXAMPLE_COMPOSE")
    example_profile=$(get_env_var "OPENNVR_EXAMPLE_PROFILE")
    if [[ -n "$example_compose" ]]; then
        [[ -f "$example_compose" ]] || {
            echo "Configured example Compose file not found: $example_compose" >&2
            if [[ "$example_compose" == *camera-agent-lite* ]]; then
                echo "camera-agent-lite was removed — the camera-agent example (with" >&2
                echo "OLLAMA_EXTERNAL_URL for host-GPU/CPU Ollama) replaces it." >&2
                echo "Fix: re-run ./scripts/install.sh reconfigure, or clear the" >&2
                echo "OPENNVR_EXAMPLE* lines in .env." >&2
            fi
            return 1
        }
        args="$args -f $example_compose"
        # External LLM runtime (.env OLLAMA_EXTERNAL_URL): overlay that
        # skips the bundled ollama container and points the agent at the
        # operator's endpoint instead — the GPU path on macOS/Windows,
        # where the in-VM container is CPU-only. Only meaningful for the
        # camera-agent profiles, and harmless to append with them.
        local external_llm
        external_llm=$(get_env_var "OLLAMA_EXTERNAL_URL")
        if [[ -n "$external_llm" && "$example_compose" == *camera-agent.yml ]]; then
            args="$args -f docker-compose.camera-agent.external-llm.yml"
        fi
    fi
    [[ -n "$example_profile" ]] && args="$args --profile $example_profile"
    # Default-on apps: occupancy-counting + footage-search ride the always-on
    # Tier-0 stream and need no extra adapter, model, or GPU, so a stock
    # install runs them (profile ``default-apps`` in docker-compose.apps.yml).
    # Opt out with OPENNVR_DEFAULT_APPS=off in .env. Lowercasing via ``tr``
    # (not ${var,,}) so macOS bash 3.2 works.
    local default_apps
    default_apps=$(get_env_var "OPENNVR_DEFAULT_APPS" 2>/dev/null)
    case "$(printf '%s' "$default_apps" | tr '[:upper:]' '[:lower:]')" in
        off|false|0|no) ;;
        *)
            [[ "$example_compose" != *docker-compose.apps.yml ]] &&                 args="$args -f docker-compose.apps.yml"
            args="$args --profile default-apps"
            ;;
    esac
    # Opt-in host-side debug ports for mediamtx. Off by default because
    # Docker's publishing is all-or-nothing per container — see the header
    # of docker-compose.debug-ports.yml and #298.
    local debug_ports
    debug_ports=$(get_env_var "OPENNVR_DEBUG_PORTS" 2>/dev/null)
    case "$(printf '%s' "$debug_ports" | tr '[:upper:]' '[:lower:]')" in
        1|true|on|yes) args="$args -f docker-compose.debug-ports.yml" ;;
    esac
    echo "$args"
}

# ── Helper: check if a TCP port is in use ─────────────────
port_in_use() {
    local port="$1"
    if command -v ss &>/dev/null; then
        ss -tuln 2>/dev/null | grep -q ":${port} "
    elif command -v lsof &>/dev/null; then
        lsof -iTCP:"$port" -sTCP:LISTEN &>/dev/null
    else
        return 1
    fi
}

# `command -v python3` is not enough: Windows ships a "python3" App Execution
# Alias stub that resolves on PATH but fails to run. Trusting it would make
# every port look unbindable and hard-fail the launcher, so confirm the
# interpreter actually works before relying on it.
python3_usable() {
    command -v python3 >/dev/null 2>&1 && python3 -c 'import socket' >/dev/null 2>&1
}

# ── Helper: can this port actually be BOUND? ──────────────────────
# Deliberately different from port_in_use(): a port can have no listener
# and still be unbindable. Windows/WSL hosts reserve ranges via WinNAT
# (`netsh interface ipv4 show excludedportrange`) that are re-rolled at
# every boot, and Docker cannot bind inside one — loopback publications
# included, which is why 127.0.0.1-only ports are no safer. Because a
# single failed binding aborts EVERY port publication for that container,
# one reserved port silently takes a whole service's port set down. Only
# a real bind() answers this.
#
# $2 selects the protocol (tcp|udp|both, default both). It has to match
# what compose actually publishes: probing UDP for a TCP-only publication
# would reject a perfectly good port whose UDP twin happens to be reserved.
port_bindable() {
    local port="$1" proto="${2:-both}"
    if python3_usable; then
        python3 -c '
import socket, sys
p, proto = int(sys.argv[1]), sys.argv[2]
types = {"tcp": [socket.SOCK_STREAM], "udp": [socket.SOCK_DGRAM]}.get(
    proto, [socket.SOCK_STREAM, socket.SOCK_DGRAM])
for typ in types:
    s = socket.socket(socket.AF_INET, typ)
    try:
        s.bind(("0.0.0.0", p))
    except OSError:
        sys.exit(1)
    finally:
        s.close()
' "$port" "$proto" >/dev/null 2>&1
        return $?
    fi
    # No python3: fall back to the weaker listener check. It cannot see
    # reserved-but-unbound ports, but that is a Windows phenomenon and
    # this branch only runs on hosts without python3.
    ! port_in_use "$port"
}

# A port held by our OWN stack is not a conflict: the stack is simply
# already running, and Docker frees the port when the container is
# recreated. Without this, re-running the launcher against a live stack
# would either refuse to start (explicit port) or silently drift to the
# next candidate and move the ICE port on every restart.
port_owned_by_stack() {
    local port="$1"
    docker ps --filter "publish=${port}" --format '{{.Names}}' 2>/dev/null \
        | grep -q '^opennvr_'
}

# Distinguishes a RESERVED port (nothing listening — waiting cannot help)
# from an IN-USE one (something listening — possibly a container teardown
# still in flight, since Docker frees published ports asynchronously).
# Without the wait, `down` immediately followed by `up` races: the container
# is already gone from `docker ps` while the proxy still holds the socket,
# so a routine stop/start would hard-fail or silently drift.
port_usable() {
    local port="$1" proto="${2:-both}" deadline
    port_bindable "$port" "$proto" && return 0
    port_owned_by_stack "$port" && return 0
    port_in_use "$port" || return 1
    deadline=$(( $(date +%s) + 6 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        sleep 1
        port_bindable "$port" "$proto" && return 0
    done
    return 1
}

# Pre-flight every port the resolved compose config will publish.
#
# The ICE port probe only protects one port, but Docker's publishing is
# all-or-nothing per container: a single unbindable port aborts every
# publication for that service, so a reserved 8322 would take the
# carefully-chosen ICE port down with it. These ports are referenced in
# docs and dev config, so they are deliberately NOT auto-moved — the goal
# is to replace an opaque daemon error
#     "ports are not available: ... bind: An attempt was made to access a
#      socket in a way forbidden by its access permissions."
# with a message naming the port and telling the operator what to do.
#
# Reads the resolved config so overlays and profiles are included and this
# stays correct as compose changes. Skipped (non-fatal) when no JSON parser
# is available — a missing pre-flight must never block a working start.
preflight_published_ports() {
    local args="$1"
    local json entries port proto blocked=""

    json=$(docker compose $args config --format json 2>/dev/null) || return 0
    [ -n "$json" ] || return 0

    if python3_usable; then
        entries=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception:
    sys.exit(0)
seen = set()
for svc in (cfg.get("services") or {}).values():
    for p in (svc.get("ports") or []):
        pub = str(p.get("published") or "")
        # Ranges ("8888-8889") are not worth expanding here; compose
        # normalises our entries to single ports anyway.
        if not pub.isdigit():
            continue
        key = (pub, p.get("protocol") or "tcp")
        if key not in seen:
            seen.add(key)
            print(key[0], key[1])
')
    elif command -v jq >/dev/null 2>&1; then
        entries=$(printf '%s' "$json" \
            | jq -r '.services[].ports[]? | select(.published != null)
                     | "\(.published) \(.protocol // "tcp")"' 2>/dev/null \
            | grep -E '^[0-9]+ ' | sort -u)
    else
        return 0
    fi

    [ -n "$entries" ] || return 0

    while read -r port proto; do
        [ -n "$port" ] || continue
        port_usable "$port" "$proto" || blocked="${blocked}${port}/${proto} "
    done <<EOF
$entries
EOF

    [ -n "$blocked" ] || return 0

    echo -e "  ${RED}These published ports cannot be bound on this host:${NC}" >&2
    echo -e "  ${RED}    ${blocked}${NC}" >&2
    echo -e "  ${GRAY}  Docker publishing is all-or-nothing per container, so the${NC}" >&2
    echo -e "  ${GRAY}  affected service would fail to publish ANY of its ports.${NC}" >&2
    echo -e "  ${GRAY}  A port can be unbindable while nothing is listening on it:${NC}" >&2
    echo -e "  ${GRAY}  on Windows, WinNAT reserves ranges that are re-rolled at every${NC}" >&2
    echo -e "  ${GRAY}  boot (loopback publications included). Inspect them with:${NC}" >&2
    echo -e "  ${GRAY}    netsh interface ipv4 show excludedportrange protocol=tcp${NC}" >&2
    echo -e "  ${GRAY}    netsh interface ipv4 show excludedportrange protocol=udp${NC}" >&2
    echo -e "  ${GRAY}  Free the port, or reserve it permanently (admin):${NC}" >&2
    echo -e "  ${GRAY}    netsh int ipv4 add excludedportrange protocol=tcp startport=<port> numberofports=1 store=persistent${NC}" >&2
    return 1
}

# Chooses the WebRTC ICE media port and exports WEBRTC_ICE_PORT for the
# compose interpolation in docker-compose.yml. Returns non-zero when no
# usable port exists, so the caller can fail with a clear message instead
# of letting `docker compose up` die on an opaque daemon error.
configure_webrtc_ice_port() {
    # Fixed, ordered candidate list rather than any free port: the set of
    # ports an operator may need to allow through a firewall has to stay
    # small and documentable. Spread far apart because the reserved ranges
    # are handed out as runs of contiguous 100-port blocks.
    local candidates="8189 18189 28189 38189"
    local preferred="8189"
    local explicit port

    explicit="${WEBRTC_ICE_PORT:-$(get_env_var WEBRTC_ICE_PORT 2>/dev/null || echo "")}"

    # An explicit operator choice is never silently overridden — it is
    # usually mirrored by a firewall rule or router port-forward, so moving
    # off it would break remote viewing in a way that is very hard to trace.
    if [ -n "$explicit" ]; then
        case "$explicit" in
            ''|*[!0-9]*)
                echo -e "  ${RED}WEBRTC_ICE_PORT='${explicit}' is not a valid port (1024-65535).${NC}" >&2
                return 1
                ;;
        esac
        if [ "$explicit" -lt 1024 ] || [ "$explicit" -gt 65535 ]; then
            echo -e "  ${RED}WEBRTC_ICE_PORT=${explicit} is out of range (1024-65535).${NC}" >&2
            return 1
        fi
        if ! port_usable "$explicit" both; then
            echo -e "  ${RED}WEBRTC_ICE_PORT=${explicit} cannot be bound on this host.${NC}" >&2
            echo -e "  ${GRAY}  MediaMTX would fail to publish ALL of its ports, not just this one,${NC}" >&2
            echo -e "  ${GRAY}  so Live View would break with no visible error.${NC}" >&2
            echo -e "  ${GRAY}  Free the port, or pick another one via WEBRTC_ICE_PORT in .env.${NC}" >&2
            return 1
        fi
        export WEBRTC_ICE_PORT="$explicit"
        return 0
    fi

    for port in $candidates; do
        if port_usable "$port" both; then
            export WEBRTC_ICE_PORT="$port"
            if [ "$port" != "$preferred" ]; then
                # Loud on purpose: a silently drifting port is how this
                # turns into an unexplainable firewall problem weeks later.
                echo -e "  ${YELLOW}WebRTC ICE port ${preferred} is unavailable — using ${port} for this run.${NC}" >&2
                echo -e "  ${GRAY}  To pin a stable port for firewall rules, set WEBRTC_ICE_PORT in .env.${NC}" >&2
            fi
            return 0
        fi
    done

    echo -e "  ${RED}No usable WebRTC ICE port found (tried: ${candidates}).${NC}" >&2
    echo -e "  ${GRAY}  Set WEBRTC_ICE_PORT in .env to a port that is free on this host.${NC}" >&2
    return 1
}

# ── Pre-flight validation ──────────────────────────────────
run_validate() {
    local errors=0
    local warnings=0

    echo -e "${BRIGHT_CYAN}  Running pre-flight checks...${NC}"
    echo ""

    # 1. Docker
    if ! docker info >/dev/null 2>&1; then
        echo -e "  ${RED}✗ Docker is not running${NC}"
        echo "      → Start Docker and retry."
        errors=$((errors + 1))
    else
        echo -e "  ${GREEN}✓ Docker is running${NC}"
    fi

    # 2. Compose file
    if [ ! -f "$COMPOSE_FILE" ]; then
        echo -e "  ${RED}✗ Compose file not found: $COMPOSE_FILE${NC}"
        errors=$((errors + 1))
    else
        echo -e "  ${GREEN}✓ Compose file: $COMPOSE_FILE${NC}"
    fi

    # 3. .env file
    if [ ! -f ".env" ]; then
        echo -e "  ${YELLOW}⚠ No .env file — run installer first: ./start.sh install${NC}"
        errors=$((errors + 1))
    else
        echo -e "  ${GREEN}✓ .env file found${NC}"

        # 4. Default secrets check
        local insecure_keys=()
        for key in SECRET_KEY CREDENTIAL_ENCRYPTION_KEY INTERNAL_API_KEY MEDIAMTX_SECRET POSTGRES_PASSWORD; do
            local val
            val=$(get_env_var "$key")
            if echo "$val" | grep -qiE "^(dev_|insecure_|change_me|your_|changeme|placeholder|dummy)"; then
                insecure_keys+=("$key")
            fi
        done
        if [ ${#insecure_keys[@]} -gt 0 ]; then
            echo -e "  ${YELLOW}⚠ Default dev secrets detected (not safe for production):${NC}"
            for k in "${insecure_keys[@]}"; do
                echo -e "      ${GRAY}- $k${NC}"
            done
            echo -e "      → Run: ${CYAN}./scripts/generate-secrets.sh --write${NC}"
            warnings=$((warnings + 1))
        else
            echo -e "  ${GREEN}✓ Secrets look non-default${NC}"
        fi

        # 5. (password managed via first-time setup page — no check needed)

        # 6. Recordings path
        local rec_path
        rec_path=$(get_env_var "RECORDINGS_PATH")
        if [ -n "$rec_path" ] && [ "$rec_path" != "./recordings" ] && [ ! -d "$rec_path" ]; then
            echo -e "  ${YELLOW}⚠ RECORDINGS_PATH does not exist: $rec_path${NC}"
            echo -e "      → Docker will attempt to create it."
            warnings=$((warnings + 1))
        elif [ -n "$rec_path" ]; then
            echo -e "  ${GREEN}✓ RECORDINGS_PATH: $rec_path${NC}"
        fi
    fi

    # 7. Port conflicts
    local ports=(8000 8554 8888 8889 9997)
    local busy_ports=()
    for p in "${ports[@]}"; do
        port_in_use "$p" && busy_ports+=("$p")
    done
    if [ ${#busy_ports[@]} -gt 0 ]; then
        echo -e "  ${YELLOW}⚠ Ports already in use on host: ${busy_ports[*]}${NC}"
        echo -e "      → Another service may conflict. Check: ss -tuln"
        warnings=$((warnings + 1))
    else
        echo -e "  ${GREEN}✓ Required ports appear free${NC}"
    fi

    echo ""
    if [ $errors -gt 0 ]; then
        echo -e "  ${RED}✗ $errors error(s) — cannot start.${NC}"
        return 1
    elif [ $warnings -gt 0 ]; then
        echo -e "  ${YELLOW}⚠ $warnings warning(s) — review above before production.${NC}"
    else
        echo -e "  ${GREEN}✓ All checks passed.${NC}"
    fi
    echo ""
    return 0
}

# ── Banner (for non-install commands) ────────────────────
print_banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════════╗"
    echo "  ║           OpenNVR - Smart Launcher           ║"
    echo "  ╚══════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "  OS detected   : ${GREEN}${OS_LABEL}${NC}"
    echo -e "  Compose file  : ${GREEN}${COMPOSE_FILE}${NC}"
    echo -e "  Command       : ${GREEN}${COMMAND}${NC}"
    echo ""
}

# ── NIC topology detection (ISSUE-6 v2) ────────────────────
#
# OpenNVR ships in two operational shapes:
#
#   single-NIC   — a Pi on home WiFi, one routable interface.
#                  Cameras and operators share that one network. nginx
#                  binds to 0.0.0.0 (the only NIC there is); there's
#                  no NIC-level isolation to offer.
#
#   dual-NIC     — paper-compliant deployment. eth0 = camera VLAN
#                  (cameras only, no default route, future V-016
#                  enforcement). eth1 = uplink/management (operators
#                  reach the UI here). nginx binds to eth1's IP only,
#                  so cameras physically cannot probe the management
#                  plane even if one is compromised.
#
# This function inspects the host's routable interfaces and decides
# which mode applies. It cannot tell which NIC is "camera-LAN" vs
# "uplink" automatically — that's a deployment choice the operator
# declares via CAMERA_NETWORK_INTERFACE / MGMT_NETWORK_INTERFACE in
# .env. With those set, we bind nginx to the management NIC's IP;
# without them on a multi-NIC host, we keep 0.0.0.0 and warn loudly
# so the operator knows they're not getting paper-compliant isolation.
#
# VLAN-tagged sub-interfaces (eth0.10, eth0.20) count as separate
# NICs and are handled the same way as physical separation, because
# the kernel presents them identically.

# Print "<iface>:<ip>" for every routable IPv4 interface, one per
# line. Excludes loopback, Docker bridges, virtual interfaces.
detect_routable_nics() {
    if command -v ip >/dev/null 2>&1; then
        # Linux iproute2 path.
        ip -4 -o addr show scope global 2>/dev/null \
            | awk '{print $2":"$4}' \
            | sed 's|/[0-9]*||' \
            | grep -Ev '^(docker|br-|veth|tun|tap)' \
            || true
    elif command -v ifconfig >/dev/null 2>&1; then
        # macOS / BSD fallback. The awk pairs each iface line with
        # the next "inet " line under it.
        ifconfig 2>/dev/null \
            | awk '/^[a-zA-Z]/{iface=$1; sub(/:$/,"",iface)}
                   /^[[:space:]]+inet /{print iface":"$2}' \
            | grep -v ':127\.' \
            | grep -Ev '^(lo|utun|awdl|llw|anpi|en[3-9]|bridge)' \
            || true
    fi
}

# Resolve a declared interface name (e.g. "eth1") to its IPv4 address.
# Returns empty string if the interface has no address or doesn't exist.
nic_ip() {
    local iface="$1"
    if [ -z "$iface" ]; then return; fi
    if command -v ip >/dev/null 2>&1; then
        ip -4 -o addr show dev "$iface" 2>/dev/null \
            | awk '{print $4}' | sed 's|/[0-9]*||' | head -1
    elif command -v ifconfig >/dev/null 2>&1; then
        ifconfig "$iface" 2>/dev/null \
            | awk '/^[[:space:]]+inet /{print $2; exit}'
    fi
}

# Returns "single", "dual-declared", or "multi-undeclared" on stdout.
# Side effects: exports NGINX_BIND_HOST so the subsequent
# `docker compose up -d --remove-orphans` picks it up via the compose interpolation
# in docker-compose.yml.
configure_nginx_bind_host() {
    local nics nic_count mode
    nics=$(detect_routable_nics)
    nic_count=$(echo "$nics" | grep -c ':' || true)

    if [ "$nic_count" -le 1 ]; then
        # Single-NIC mode — 0.0.0.0 is correct (= the one NIC).
        mode="single"
        export NGINX_BIND_HOST="${NGINX_BIND_HOST:-0.0.0.0}"
        # ISSUE-6 v8: even in single-NIC, browsers need a real host
        # in the WebRTC ICE candidates and the token-endpoint URLs.
        # detect_lan_ip prefers NGINX_BIND_HOST when set, falls back
        # to hostname / ipconfig discovery for single-NIC where the
        # bind host is the wildcard.
        local single_host
        single_host=$(detect_lan_ip 2>/dev/null || echo "")
        if [ -n "$single_host" ]; then
            set_env_var MEDIAMTX_PUBLIC_URL "https://${single_host}"
            set_env_var MEDIAMTX_WEBRTC_HOSTS "${single_host}"
            # ISSUE-6 v9: propagate to cert SAN — see dual-declared
            # branch for the rationale.
            if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
                export OPENNVR_HOST_IP="${single_host}"
            fi
        fi
        echo -e "  ${GRAY}NIC topology: single-NIC mode (one routable interface)${NC}" >&2
        echo -e "  ${GRAY}nginx will bind to 0.0.0.0:443 — the only network it can reach${NC}" >&2
        return 0
    fi

    # Multi-NIC host.
    local cam_iface mgmt_iface mgmt_ip existing_bind
    cam_iface=$(get_env_var "CAMERA_NETWORK_INTERFACE" 2>/dev/null || echo "")
    mgmt_iface=$(get_env_var "MGMT_NETWORK_INTERFACE" 2>/dev/null || echo "")
    existing_bind=$(get_env_var "NGINX_BIND_HOST" 2>/dev/null || echo "")

    if [ -n "$cam_iface" ] && [ -n "$mgmt_iface" ]; then
        # Operator declared the topology. Bind nginx to the management
        # NIC's IP so the camera VLAN cannot reach the UI.
        mgmt_ip=$(nic_ip "$mgmt_iface")
        if [ -z "$mgmt_ip" ]; then
            echo -e "  ${RED}MGMT_NETWORK_INTERFACE=${mgmt_iface} has no IPv4 address.${NC}" >&2
            echo -e "  ${YELLOW}Aborting before docker compose up.${NC}" >&2
            return 1
        fi
        mode="dual-declared"
        export NGINX_BIND_HOST="$mgmt_ip"
        # ISSUE-6 v8: tell opennvr-core and mediamtx where browsers
        # will reach them. MEDIAMTX_PUBLIC_URL → token endpoint
        # emits HTTPS URLs through nginx. MEDIAMTX_WEBRTC_HOSTS →
        # mediamtx advertises ICE candidates the LAN browser can
        # reach for the UDP/8189 media path.
        set_env_var MEDIAMTX_PUBLIC_URL "https://${mgmt_ip}"
        set_env_var MEDIAMTX_WEBRTC_HOSTS "${mgmt_ip}"
        # ISSUE-6 v9: propagate the IP to the cert init containers
        # so the TLS cert SAN list includes the IP browsers will
        # actually visit. Without this, the cert is generated with
        # only loopback in the SAN and the browser warns about both
        # CN/IP mismatch AND self-signed CA. With it, only the
        # CA-not-trusted warning fires (one click to accept).
        # We don't override an operator-set OPENNVR_HOST_IP — they
        # may have a specific reason to want a different SAN entry.
        if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
            export OPENNVR_HOST_IP="${mgmt_ip}"
        fi
        echo -e "  ${GREEN}NIC topology: dual-NIC (cameras isolated from operator network)${NC}" >&2
        echo -e "  ${GRAY}  camera network : ${WHITE}${cam_iface}${GRAY}  (UI not exposed here)${NC}" >&2
        echo -e "  ${GRAY}  operator uplink: ${WHITE}${mgmt_iface} (${mgmt_ip})${GRAY}  ← UI bound here${NC}" >&2
        echo -e "  ${WHITE}  Web UI:${NC} ${CYAN}https://${mgmt_ip}/${NC}" >&2
        return 0
    fi

    # Multi-NIC, operator hasn't declared. Honor explicit
    # NGINX_BIND_HOST if set (operator made a conscious choice
    # earlier and persisted it to .env). Otherwise, if we have a
    # TTY, walk the operator through a topology decision; if not
    # (CI / scripted invocation), fall back to 0.0.0.0 with a warning.
    if [ -n "$existing_bind" ]; then
        export NGINX_BIND_HOST="$existing_bind"
        echo -e "  ${GRAY}NIC topology: multi-NIC, NGINX_BIND_HOST=${existing_bind} (explicit choice)${NC}" >&2
        return 0
    fi

    if [ -t 0 ] && [ -t 1 ]; then
        prompt_nic_topology "$nics" || return $?
        return 0
    fi

    mode="multi-undeclared"
    export NGINX_BIND_HOST="0.0.0.0"
    # Best-effort browser-facing URLs even without a declared topology:
    # this branch previously exported no MEDIAMTX_PUBLIC_URL at all, so
    # compose fell back to https://localhost and live view broke for
    # every LAN client. detect_lan_ip is now route-aware (default-route
    # source IP), so its guess is the operator-facing NIC, not
    # enumeration luck.
    local fallback_host
    fallback_host=$(detect_lan_ip 2>/dev/null || echo "")
    if [ -n "$fallback_host" ]; then
        set_env_var MEDIAMTX_PUBLIC_URL "https://${fallback_host}"
        set_env_var MEDIAMTX_WEBRTC_HOSTS "${fallback_host}"
        if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
            export OPENNVR_HOST_IP="${fallback_host}"
        fi
    fi
    echo -e "  ${YELLOW}NIC topology: multi-NIC, undeclared (non-interactive)${NC}" >&2
    echo -e "  ${GRAY}  Detected ${nic_count} routable interfaces:${NC}" >&2
    echo "$nics" | while IFS=: read -r iface ip; do
        echo -e "  ${GRAY}    - ${WHITE}${iface}${GRAY} (${ip})${NC}" >&2
    done
    echo -e "  ${YELLOW}  nginx will bind to 0.0.0.0:443, which reaches ALL of these.${NC}" >&2
    echo -e "  ${YELLOW}  Re-run ./start.sh up interactively to configure NIC isolation,${NC}" >&2
    echo -e "  ${YELLOW}  or set NGINX_BIND_HOST/CAMERA_NETWORK_INTERFACE/MGMT_NETWORK_INTERFACE${NC}" >&2
    echo -e "  ${YELLOW}  in .env directly.${NC}" >&2
    return 0
}

# ── Interactive NIC topology walkthrough ───────────────────
#
# Triggered when start.sh detects multi-NIC undeclared AND has a
# TTY. Presents the detected NICs, asks the operator how their
# network is set up (single-LAN, dual-NIC, or "decide later"), and
# persists the choice to .env so subsequent runs skip the prompt.
#
# For dual-NIC, the prompt also offers to apply paper-compliant
# host hardening (nftables forward-drop between camera and uplink
# NICs) via the separate ./scripts/apply-camera-vlan-hardening.sh
# script — the operator sees every command before any sudo is
# requested, and a one-line revert is available.

write_env_var() {
    # Three behaviours, in priority order:
    #   1. ^KEY=...        (already uncommented)  → update in place
    #   2. ^#KEY=...       (commented placeholder) → uncomment + update
    #   3. neither present                        → append at end
    # This avoids the cosmetic foot-gun where .env ends up with both
    # the original commented placeholder AND a freshly-appended
    # uncommented duplicate after the NIC walkthrough writes.
    local key="$1" value="$2" file="${3:-.env}"
    if [ ! -f "$file" ]; then
        echo "${key}=${value}" > "$file"
        return 0
    fi
    # Portable sed -i across GNU and BSD via .bak suffix.
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$file" \
            && rm -f "${file}.bak"
    elif grep -qE "^#[[:space:]]*${key}=" "$file" 2>/dev/null; then
        # Uncomment-in-place: the original placeholder line (e.g.
        # ``#CAMERA_NETWORK_INTERFACE=eth0`` from .env.example) gets
        # rewritten as ``CAMERA_NETWORK_INTERFACE=<chosen-value>``.
        sed -i.bak -E "s|^#[[:space:]]*${key}=.*|${key}=${value}|" "$file" \
            && rm -f "${file}.bak"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

# export + persist in one step.
#
# Values detected from the host topology (the LAN IP browsers reach us on)
# used to be exported only, so they lived just in this script's process. Any
# `docker compose up -d` that recreated a container without them baked an
# empty value in — which silently killed live WebRTC, because MediaMTX then
# advertises only its own Docker-bridge address as an ICE candidate. Writing
# them to .env makes every compose entry point see the same value.
#
# Always overwrites: these are DETECTED, so a host whose LAN IP changed must
# refresh rather than keep a stale value. Operator-set values are read via
# get_env_var by the callers before they decide, so they are not clobbered.
set_env_var() {
    local key="$1" value="$2"
    export "${key}=${value}"
    write_env_var "$key" "$value"
}

prompt_nic_topology() {
    local nics="$1"
    local -a nic_array=()
    while IFS=: read -r iface ip; do
        [ -z "$iface" ] && continue
        nic_array+=("$iface:$ip")
    done < <(echo "$nics")
    local n=${#nic_array[@]}

    echo "" >&2
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo -e "  ${YELLOW}NIC topology: I see ${n} routable interfaces.${NC}" >&2
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
    echo "" >&2
    echo -e "  ${WHITE}Detected interfaces:${NC}" >&2
    local i
    for i in $(seq 1 "$n"); do
        local entry="${nic_array[$((i-1))]}"
        local iface="${entry%%:*}"
        local ip="${entry##*:}"
        printf "    %s%d)%s %-14s %s%s%s\n" \
            "$WHITE" "$i" "$NC" "$iface" "$GRAY" "$ip" "$NC" >&2
    done
    echo "" >&2
    echo -e "  ${WHITE}How is your network set up?${NC}" >&2
    echo "" >&2
    echo -e "    ${WHITE}1)${NC} ${WHITE}Simple${NC} ${GRAY}— one network for cameras, phone, and computer.${NC}" >&2
    echo -e "       ${GRAY}Most home / small-office setups.${NC}" >&2
    echo -e "       ${GRAY}Tip: change every camera's default password before connecting.${NC}" >&2
    echo "" >&2
    echo -e "    ${WHITE}2)${NC} ${WHITE}Advanced${NC} ${GRAY}— cameras on a separate network from operators.${NC}" >&2
    echo -e "       ${GRAY}Needs two network cables or a VLAN-aware managed switch.${NC}" >&2
    echo -e "       ${GRAY}Stronger isolation if a camera gets hacked.${NC}" >&2
    echo "" >&2
    echo -e "    ${WHITE}3)${NC} ${WHITE}Not sure${NC} ${GRAY}— I'll pick the safe default (Simple) for you.${NC}" >&2
    echo "" >&2

    local choice
    read -rp "  Your choice [1/2/3]: " choice
    choice=$(echo "$choice" | tr '[:upper:]' '[:lower:]')
    # Normalise numeric choices to letter aliases so the case block
    # below stays compact. "3" = Not sure → defaults to Simple (= 1 = s).
    case "$choice" in
        1) choice="s" ;;
        2) choice="d" ;;
        3) choice="s" ;;
    esac

    case "$choice" in
        s)
            write_env_var NGINX_BIND_HOST "0.0.0.0"
            export NGINX_BIND_HOST="0.0.0.0"
            local lan_hint
            lan_hint=$(detect_lan_ip 2>/dev/null || echo "")
            # ISSUE-6 v8: emit HTTPS URLs through nginx and tell
            # mediamtx where to advertise ICE candidates.
            # ISSUE-6 v9: also propagate to OPENNVR_HOST_IP so the
            # cert SAN includes the LAN IP — see dual-declared
            # branch for the rationale.
            if [ -n "$lan_hint" ]; then
                set_env_var MEDIAMTX_PUBLIC_URL "https://${lan_hint}"
                set_env_var MEDIAMTX_WEBRTC_HOSTS "${lan_hint}"
                if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
                    export OPENNVR_HOST_IP="${lan_hint}"
                fi
            fi
            echo "" >&2
            echo -e "  ${GREEN}✓ Single-LAN mode saved.${NC}" >&2
            echo -e "  ${GRAY}  nginx will bind to 0.0.0.0:443 (reachable from any${NC}" >&2
            echo -e "  ${GRAY}  device that can route to this host).${NC}" >&2
            if [ -n "$lan_hint" ]; then
                echo -e "  ${WHITE}  Web UI:${NC} ${CYAN}https://${lan_hint}/${NC}" >&2
            else
                echo -e "  ${WHITE}  Web UI:${NC} ${CYAN}https://<server-ip>/${NC}" >&2
            fi
            echo -e "  ${GRAY}  (Wrote NGINX_BIND_HOST=0.0.0.0 to .env)${NC}" >&2
            return 0
            ;;
        d)
            local cam_choice mgmt_choice cam_iface mgmt_iface mgmt_ip
            read -rp "  Which number is the CAMERA-LAN side?     [1-${n}]: " cam_choice
            read -rp "  Which number is the OPERATOR-UPLINK side? [1-${n}]: " mgmt_choice
            # Validation: numeric, in range, distinct.
            if ! [[ "$cam_choice" =~ ^[0-9]+$ ]] || \
               [ "$cam_choice" -lt 1 ] || [ "$cam_choice" -gt "$n" ]; then
                echo -e "  ${RED}Invalid camera-NIC choice: ${cam_choice}. Aborting.${NC}" >&2
                return 1
            fi
            if ! [[ "$mgmt_choice" =~ ^[0-9]+$ ]] || \
               [ "$mgmt_choice" -lt 1 ] || [ "$mgmt_choice" -gt "$n" ]; then
                echo -e "  ${RED}Invalid uplink-NIC choice: ${mgmt_choice}. Aborting.${NC}" >&2
                return 1
            fi
            if [ "$cam_choice" = "$mgmt_choice" ]; then
                echo -e "  ${RED}Same NIC chosen for both sides — that defeats isolation.${NC}" >&2
                echo -e "  ${RED}Aborting; nothing written to .env.${NC}" >&2
                return 1
            fi
            cam_iface="${nic_array[$((cam_choice-1))]%%:*}"
            mgmt_iface="${nic_array[$((mgmt_choice-1))]%%:*}"
            mgmt_ip="${nic_array[$((mgmt_choice-1))]##*:}"

            write_env_var CAMERA_NETWORK_INTERFACE "$cam_iface"
            write_env_var MGMT_NETWORK_INTERFACE "$mgmt_iface"
            export NGINX_BIND_HOST="$mgmt_ip"
            # First-run parity with the dual-declared branch of
            # configure_nginx_bind_host: that branch only runs on the
            # NEXT ./start.sh up (it reads the interface names we just
            # wrote to .env). Without these exports, the compose up
            # that follows THIS walkthrough falls back to
            # https://localhost for the browser-facing stream URLs and
            # advertises no reachable WebRTC ICE host — live view
            # broken until a restart nobody knows they need.
            set_env_var MEDIAMTX_PUBLIC_URL "https://${mgmt_ip}"
            set_env_var MEDIAMTX_WEBRTC_HOSTS "${mgmt_ip}"
            if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
                export OPENNVR_HOST_IP="${mgmt_ip}"
            fi
            echo "" >&2
            echo -e "  ${GREEN}✓ Dual-NIC mode saved.${NC}" >&2
            echo -e "  ${GRAY}  camera network : ${WHITE}${cam_iface}${GRAY}  (UI not exposed here)${NC}" >&2
            echo -e "  ${GRAY}  operator uplink: ${WHITE}${mgmt_iface} (${mgmt_ip})${GRAY}  ← UI bound here${NC}" >&2
            echo "" >&2
            echo -e "  ${WHITE}  Web UI:${NC} ${CYAN}https://${mgmt_ip}/${NC}" >&2
            echo -e "  ${GRAY}  (Wrote CAMERA_NETWORK_INTERFACE + MGMT_NETWORK_INTERFACE to .env)${NC}" >&2
            echo "" >&2

            # Offer the paper-compliant host hardening as a separate
            # consent step. The harden script is the only thing in
            # the OpenNVR install path that asks for sudo, so we
            # surface the choice loudly.
            local harden_script="./scripts/apply-camera-vlan-hardening.sh"
            if [ -x "$harden_script" ]; then
                echo -e "  ${WHITE}Apply host firewall rules to enforce the camera/uplink separation?${NC}" >&2
                echo -e "  ${GRAY}This installs Linux firewall (nftables) rules that block IP${NC}" >&2
                echo -e "  ${GRAY}forwarding between ${cam_iface} (cameras) and ${mgmt_iface} (uplink).${NC}" >&2
                echo -e "  ${GRAY}Effect: a compromised camera cannot use this host as a${NC}" >&2
                echo -e "  ${GRAY}stepping stone to reach your LAN. Requires sudo once.${NC}" >&2
                echo -e "  ${GRAY}Every command is printed before running. Reverse with:${NC}" >&2
                echo -e "  ${GRAY}    ./scripts/revert-camera-vlan-hardening.sh${NC}" >&2
                echo "" >&2
                local apply_choice
                read -rp "  Apply hardening now? [y/N]: " apply_choice
                if [[ "$apply_choice" =~ ^[Yy]$ ]]; then
                    bash "$harden_script" \
                        --camera-iface "$cam_iface" \
                        --mgmt-iface "$mgmt_iface" \
                        || echo -e "  ${YELLOW}Hardening failed/aborted; see output above.${NC}" >&2
                else
                    echo -e "  ${GRAY}Skipped. Run ${WHITE}${harden_script}${GRAY} later when ready.${NC}" >&2
                fi
            fi
            return 0
            ;;
        l|"")
            export NGINX_BIND_HOST="0.0.0.0"
            echo "" >&2
            echo -e "  ${GRAY}Skipping for now. nginx will bind to 0.0.0.0:443.${NC}" >&2
            echo -e "  ${GRAY}You'll see this prompt again next ./start.sh up.${NC}" >&2
            return 0
            ;;
        *)
            echo -e "  ${RED}Invalid choice: ${choice}. Aborting.${NC}" >&2
            return 1
            ;;
    esac
}

# ── Security posture surfacer (ISSUE-6 v5) ─────────────────
#
# Prints a banner *every* ./start.sh up/build flagging any
# actionable security limitation we can detect: single-LAN trust
# mode, dual-NIC declared but kernel-level hardening not applied,
# legacy escape-hatch env vars left set, etc. Silent when there's
# nothing to flag — operators with a fully-locked-down deployment
# don't get noise.
#
# Detection signals:
#   * NIC topology      — CAMERA_/MGMT_NETWORK_INTERFACE in .env
#   * Hardening applied — presence of ./host-hardening/snapshot-active
#                         symlink (created by apply, removed by revert)
#   * Legacy flags      — ALLOW_REMOTE_MEDIAMTX in env
#
# The function writes to stderr so it's visible alongside other
# boot output but doesn't pollute anything that captures stdout.

print_security_posture() {
    local cam_iface mgmt_iface
    cam_iface=$(get_env_var "CAMERA_NETWORK_INTERFACE" 2>/dev/null || echo "")
    mgmt_iface=$(get_env_var "MGMT_NETWORK_INTERFACE" 2>/dev/null || echo "")

    local warnings=""
    local has_warnings=0

    # 1. Single-LAN trust mode. NIC vars unset → operator picked
    #    Simple (or hasn't declared) → cameras and operators share
    #    one network. Same security finding as before, friendlier
    #    wording: lead with an actionable tip (change camera default
    #    passwords) rather than scary jargon.
    if [ -z "$cam_iface" ] && [ -z "$mgmt_iface" ]; then
        has_warnings=1
        warnings+="  ${CYAN}ℹ${NC}  ${WHITE}Simple network setup${NC} ${GRAY}(cameras and computer on one network)${NC}\n"
        warnings+="     ${GRAY}Tip: change every camera's default password before you${NC}\n"
        warnings+="     ${GRAY}connect it. That's how most home cameras get hacked.${NC}\n"
        warnings+="     ${GRAY}Want stronger isolation? See dual-NIC mode in ${WHITE}.env.example${GRAY}.${NC}\n"
    fi

    # 2. Dual-NIC declared but kernel-level hardening not active.
    #    Audience here is advanced (they picked dual-NIC), so keep
    #    the technical detail — but compact it.
    if [ -n "$cam_iface" ] && [ -n "$mgmt_iface" ]; then
        if [ ! -L "./host-hardening/snapshot-active" ]; then
            has_warnings=1
            warnings+="  ${YELLOW}⚠${NC}  ${WHITE}Camera/uplink isolation not enforced at the firewall${NC}\n"
            warnings+="     ${GRAY}nginx is bound to ${mgmt_iface} only (good), but the kernel${NC}\n"
            warnings+="     ${GRAY}doesn't yet block forwarding between ${cam_iface} and ${mgmt_iface}.${NC}\n"
            warnings+="     ${GRAY}Fix: ${WHITE}./scripts/apply-camera-vlan-hardening.sh${NC}\n"
        fi
    fi

    # 3. Legacy ALLOW_REMOTE_MEDIAMTX env var — compact, technical.
    if [ -n "${ALLOW_REMOTE_MEDIAMTX:-}" ] || \
       [ -n "$(get_env_var ALLOW_REMOTE_MEDIAMTX 2>/dev/null)" ]; then
        has_warnings=1
        warnings+="  ${YELLOW}⚠${NC}  ${WHITE}ALLOW_REMOTE_MEDIAMTX is set but ignored${NC} ${GRAY}(retired in V-015).${NC}\n"
        warnings+="     ${GRAY}Remove the line from .env to silence.${NC}\n"
    fi

    if [ "$has_warnings" -eq 1 ]; then
        echo "" >&2
        echo -e "  ${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
        echo -e "  ${WHITE}Heads up:${NC}" >&2
        echo -e "  ${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}" >&2
        printf "%b" "$warnings" >&2
        echo "" >&2
    fi
}

# ── Access URL surfacer (ISSUE-6) ──────────────────────────
#
# Until ISSUE-6 the script told the operator to hit
# http://localhost:8000 — which only works on the host machine
# because opennvr-core is bound to 127.0.0.1. With the nginx TLS
# reverse proxy in the compose, LAN clients hit https://<host-ip>/
# instead, and the host's own browser can use either.
#
# We try to detect the LAN-facing IP best-effort so the operator
# sees a clickable URL. Failure paths fall back to a generic
# "https://<server-ip>/" string so the message is never misleading.
# True if this source-IP / egress-interface pair looks like a VPN
# tunnel rather than the operator's LAN. Route-aware detection has one
# failure mode the heuristics it replaced did not: on a host running a
# full-tunnel VPN (Tailscale, WireGuard, ...) the default route IS the
# tunnel, so `ip route get` deterministically returns the tunnel IP —
# and MEDIAMTX_PUBLIC_URL, the WebRTC ICE hosts, and the cert SAN get
# pinned to an address LAN browsers can't reach. Two independent
# signals, either one disqualifies:
#   * egress device named like a tunnel (wg*, tun*, tap*, utun*,
#     tailscale*, zt* (ZeroTier), nebula*)
#   * source address in 100.64.0.0/10 — the CGNAT range Tailscale
#     allocates from; never a home/office LAN address in practice
# Operators who genuinely WANT the tunnel address (remote-only access)
# set OPENNVR_HOST_IP in .env, which wins before detection runs.
is_vpn_tunnel_source() {
    local src="$1" dev="$2"
    case "$dev" in
        wg*|tun*|tap*|utun*|tailscale*|zt*|nebula*) return 0 ;;
    esac
    case "$src" in
        100.*)
            local second
            second="${src#100.}"
            second="${second%%.*}"
            case "$second" in
                6[4-9]|7[0-9]|8[0-9]|9[0-9]|1[0-1][0-9]|12[0-7]) return 0 ;;
            esac
            ;;
    esac
    return 1
}

detect_lan_ip() {
    # Self-review M-1: on dual-NIC hosts, NGINX_BIND_HOST is the
    # *authoritative* answer for "which IP does the operator browse
    # to" — `configure_nginx_bind_host` already picked the right
    # NIC. Prefer it over any fallback so we don't show a URL that
    # nginx isn't actually bound to. Skip 0.0.0.0 because that's
    # "all interfaces" and not a real visit target.
    if [ -n "${NGINX_BIND_HOST:-}" ] && [ "$NGINX_BIND_HOST" != "0.0.0.0" ]; then
        echo "$NGINX_BIND_HOST"
        return
    fi
    # OPENNVR_HOST_IP from .env wins next — it's what the cert SAN
    # list was generated against, so it's the URL with the *least*
    # browser warning.
    local override
    override=$(get_env_var "OPENNVR_HOST_IP" 2>/dev/null || echo "")
    if [ -n "$override" ]; then
        echo "$override"
        return
    fi
    # Route-aware detection: the source IP the kernel would use to
    # reach the internet. On multi-NIC hosts this lands on the
    # operator-facing NIC by construction — a directly-attached
    # camera NIC has no default route — unlike the `hostname -I`
    # fallback below, whose ordering is interface-enumeration luck
    # and can pick the camera NIC (wrong MEDIAMTX_PUBLIC_URL, wrong
    # WebRTC ICE hosts, wrong cert SAN). No packet is sent: `ip
    # route get` is a pure routing-table lookup.
    if command -v ip >/dev/null 2>&1; then
        local route_out route_src route_dev
        route_out=$(ip -4 route get 1.1.1.1 2>/dev/null | head -n 1)
        route_src=$(printf '%s\n' "$route_out" \
            | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
        route_dev=$(printf '%s\n' "$route_out" \
            | awk '{for (i=1; i<=NF; i++) if ($i == "dev") {print $(i+1); exit}}')
        # A tunnel egress means "the default route is the VPN", not
        # "this is the operator-facing NIC" — skip to the LAN
        # heuristics below instead of pinning URLs/certs to an
        # address LAN browsers can't reach (see is_vpn_tunnel_source).
        if [ -n "$route_src" ] && ! is_vpn_tunnel_source "$route_src" "${route_dev:-}"; then
            echo "$route_src"
            return
        fi
    fi
    # Linux: hostname -I returns space-separated v4/v6 addresses on
    # configured interfaces. Take the first non-loopback v4.
    if command -v hostname >/dev/null 2>&1; then
        local first
        first=$(hostname -I 2>/dev/null | tr ' ' '\n' \
                | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' \
                | grep -v '^127\.' \
                | head -n 1 || true)
        if [ -n "$first" ]; then
            echo "$first"
            return
        fi
    fi
    # macOS fallback — ipconfig getifaddr en0 or en1.
    if command -v ipconfig >/dev/null 2>&1; then
        for iface in en0 en1 en2; do
            local ip
            ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
            if [ -n "$ip" ]; then echo "$ip"; return; fi
        done
    fi
    echo ""
}

# ── Camera-discovery LAN IPs (OPENNVR_LAN_IPS) ─────────────
# The bridge-networked core container can't see the host's NICs, so ONVIF
# discovery relies on the launcher exporting the host's LAN address(es).
# OPENNVR_HOST_IP (exported by configure_nginx_bind_host) covers the
# operator-facing NIC, but on a multi-NIC host the camera LAN is usually a
# *different* NIC — so export every routable private IPv4 as OPENNVR_LAN_IPS
# too and discovery scans each NIC's subnet. Compose passes both variables
# to opennvr-core; an operator-set .env value always wins.
export_camera_lan_ips() {
    if [ -n "${OPENNVR_LAN_IPS:-}" ] || [ -n "$(get_env_var OPENNVR_LAN_IPS 2>/dev/null)" ]; then
        return 0
    fi
    local ips="" iface ip
    while IFS=: read -r iface ip; do
        [ -n "$ip" ] || continue
        if is_vpn_tunnel_source "$ip" "$iface"; then continue; fi
        # RFC1918 only — a public or CGNAT address is never a camera LAN.
        case "$ip" in
            10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;;
            *) continue ;;
        esac
        case ",${ips}," in *",${ip},"*) continue ;; esac
        ips="${ips:+${ips},}${ip}"
    done <<EOF
$(detect_routable_nics)
EOF
    if [ -n "$ips" ]; then
        export OPENNVR_LAN_IPS="$ips"
        case "$ips" in
            *,*) echo -e "  ${GRAY}Camera discovery will cover subnets of: ${ips}${NC}" >&2 ;;
        esac
    fi
}

# ── Host LAN IP hint file (./data/net-hints/host-ips) ──────
# The OPENNVR_HOST_IP/OPENNVR_LAN_IPS env vars are frozen into the core
# container at creation, so they go stale when the host moves to another
# subnet. This file is bind-mounted read-only into the container (see
# docker-compose.yml) and rewritten on every launcher run; the server's
# detect_local_subnets() reads it first, so ONVIF discovery follows the
# host's *current* networks without a container recreate.
write_net_hints() {
    local hints="" candidate
    for candidate in \
        "${OPENNVR_HOST_IP:-}" "$(get_env_var OPENNVR_HOST_IP 2>/dev/null || true)" \
        "${OPENNVR_LAN_IPS:-}" "$(get_env_var OPENNVR_LAN_IPS 2>/dev/null || true)"; do
        local token
        for token in $(printf '%s' "$candidate" | tr ',' ' '); do
            [ -n "$token" ] || continue
            case " ${hints} " in *" ${token} "*) continue ;; esac
            hints="${hints}${hints:+ }${token}"
        done
    done
    # Best-effort: a permission problem (e.g. the dir was first created
    # root-owned by Docker) must not abort the launch under set -e.
    if [ -n "$hints" ]; then
        { mkdir -p ./data/net-hints \
            && printf '%s\n' "$hints" > ./data/net-hints/host-ips.tmp \
            && mv -f ./data/net-hints/host-ips.tmp ./data/net-hints/host-ips; } 2>/dev/null \
            || echo -e "  ${YELLOW}⚠ Couldn't write ./data/net-hints/host-ips (check permissions)${NC}" >&2
    else
        rm -f ./data/net-hints/host-ips 2>/dev/null || true
    fi
}

print_access_urls() {
    local admin_user="$1"
    local lan_ip
    lan_ip=$(detect_lan_ip)

    echo -e "  ${GREEN}✓ OpenNVR is running!${NC}"
    if [ -n "$lan_ip" ]; then
        echo -e "  Web UI (LAN)    → ${CYAN}https://${lan_ip}/${NC}  ${GRAY}(login: ${admin_user})${NC}"
    else
        echo -e "  Web UI (LAN)    → ${CYAN}https://<server-ip>/${NC}  ${GRAY}(login: ${admin_user})${NC}"
    fi
    echo -e "  Web UI (local)  → ${CYAN}https://localhost/${NC}"
    echo -e "  API Docs        → ${CYAN}https://localhost/docs${NC}"
    # If an agent example is active, surface its demo URL(s) too. The agents
    # serve their own https on the LAN (sign in with your OpenNVR account).
    _ex_profile="$(get_env_var OPENNVR_EXAMPLE_PROFILE 2>/dev/null)"
    _ex="$(get_env_var OPENNVR_EXAMPLE 2>/dev/null)"
    case "${_ex_profile}:${_ex}" in
        camera-agent:*|camera-agent-chat:*|*:camera-agent)
            echo -e "  Camera Agent    → ${CYAN}https://localhost:9100/demo${NC}  ${GRAY}(ask your cameras — voice or chat)${NC}"
            echo -e "  Camera Agent (LAN) → ${CYAN}https://<server-ip>:9100/demo${NC}  ${GRAY}(OpenNVR login)${NC}"
            ;;
    esac
    echo ""
    echo -e "  ${YELLOW}First visit:${NC} the browser will warn about a self-signed"
    echo -e "  certificate. Click ${WHITE}Advanced → Accept the risk${NC}. The cert is"
    echo -e "  generated locally and never leaves this machine."
    if [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null)" ]; then
        echo -e "  ${GRAY}Tip: set ${WHITE}OPENNVR_HOST_IP=${lan_ip:-<server-ip>}${GRAY} in .env to silence${NC}"
        echo -e "  ${GRAY}     the CN/IP-mismatch part of the warning on next regenerate.${NC}"
    fi
    echo -e "  ${GRAY}First-time setup page opens automatically on first visit.${NC}"
}

# ── First-time setup token surfacer ────────────────────────
#
# V-001 / M0 C-1 UX: the OpenNVR server mints a one-time setup token
# on first boot and prints it to its stdout (so /auth/first-time-setup
# can refuse anonymous LAN access). With `docker compose up -d --remove-orphans` the
# operator never sees that stdout — they have to grep the logs.
#
# ISSUE-5 fix: the previous version polled for 30s after `compose up
# -d`, but `compose up -d --remove-orphans` returns the moment containers are
# *scheduled*, not when they're *healthy*. Post-ISSUE-3 the
# yolov8-weights-init container takes ~3 min on x86 / ~10-15 min on a
# Pi 5 to export the ONNX model before opennvr-core even starts
# booting. A 30-second poll always lost that race on slow hardware and
# fell through to a misleading "either the admin is already activated
# or the server is still starting" message.
#
# Strategy now: wait for opennvr-core's Docker healthcheck to pass
# first (with progress feedback so the operator isn't staring at a
# silent terminal for 15 min), THEN extract the banner from the logs.
# Once healthy, the banner is unambiguously present — its absence then
# means the admin is already activated, which we report as such.
print_first_time_setup_token() {
    local compose_args="$1"
    local container="opennvr_core"   # container_name from docker-compose.*.yml
    # OPENNVR_SETUP_TOKEN_MAX_WAIT_S exists so a future smoke-test
    # harness can short-circuit the 20-minute production timeout with
    # something testable, e.g. OPENNVR_SETUP_TOKEN_MAX_WAIT_S=10.
    local max_wait_s="${OPENNVR_SETUP_TOKEN_MAX_WAIT_S:-1200}"  # 20 min
    local poll_interval_s=2
    local elapsed=0
    local last_health=""
    local last_message_at=0
    local banner=""

    echo ""
    echo -e "  ${GRAY}Waiting for opennvr-core to be healthy before showing the${NC}"
    echo -e "  ${GRAY}first-time setup token. Init containers can take 10-15 min${NC}"
    echo -e "  ${GRAY}on a Pi 5 the first time (YOLOv8 .pt → ONNX export).${NC}"

    while [ "$elapsed" -lt "$max_wait_s" ]; do
        # docker inspect returns "" if the container hasn't been
        # created yet (e.g. yolov8-weights-init is still running and
        # opennvr-core hasn't been scheduled). Treat that as "waiting".
        local health
        health=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
                 "$container" 2>/dev/null || echo "absent")

        case "$health" in
            healthy)
                echo -e "  ${GREEN}✓ opennvr-core is healthy${NC}"
                break
                ;;
            unhealthy)
                echo ""
                echo -e "  ${YELLOW}opennvr-core reported unhealthy. Inspect:${NC}"
                echo -e "  ${GRAY}    docker compose $compose_args logs --tail 100 opennvr-core${NC}"
                echo ""
                return 1
                ;;
            none)
                # The container is running but defines no healthcheck
                # (e.g. someone built a custom image that stripped it).
                # There's no signal to wait on — fall through to the
                # banner extraction immediately. The token banner is
                # printed early in lifespan, so if the container has
                # gotten this far it's almost certainly in the logs.
                echo -e "  ${GRAY}  opennvr-core has no healthcheck; checking logs now${NC}"
                break
                ;;
            absent|starting|"")
                # Progress message every ~15 seconds so the operator
                # knows we're still alive (init containers can run
                # silently for several minutes).
                if [ "$health" != "$last_health" ] || \
                   [ $((elapsed - last_message_at)) -ge 15 ]; then
                    case "$health" in
                        absent)
                            echo -e "  ${GRAY}  [${elapsed}s] opennvr-core not yet created (init containers running)...${NC}"
                            ;;
                        starting|"")
                            echo -e "  ${GRAY}  [${elapsed}s] opennvr-core booting...${NC}"
                            ;;
                    esac
                    last_message_at=$elapsed
                fi
                ;;
        esac
        last_health="$health"
        sleep "$poll_interval_s"
        elapsed=$((elapsed + poll_interval_s))
    done

    if [ "$elapsed" -ge "$max_wait_s" ]; then
        echo ""
        echo -e "  ${YELLOW}Timed out after ${max_wait_s}s waiting for opennvr-core${NC}"
        echo -e "  ${YELLOW}to become healthy. Check init container progress:${NC}"
        echo -e "  ${GRAY}    docker compose $compose_args ps${NC}"
        echo -e "  ${GRAY}    docker compose $compose_args logs --tail 100 opennvr-core${NC}"
        echo -e "  ${GRAY}Once healthy, retrieve the token manually:${NC}"
        echo -e "  ${GRAY}    docker compose $compose_args logs opennvr-core | grep -A 6 'first-time setup token'${NC}"
        echo ""
        return 1
    fi

    # Server is healthy — the lifespan hook prints the banner *very*
    # early in boot (right after admin user creation) so it's
    # definitely in the logs by now. Use --tail 5000 to scoop the
    # early-boot region without a brittle --since time window.
    # -A 6 matches the operator-facing guidance in the React form,
    # README, and the fallback message below — keep aligned so
    # operators see the same command surface everywhere.
    #
    # ``tail -7`` keeps only the LAST banner. If opennvr-core
    # crash-looped during boot, ``maybe_arm`` runs once per restart
    # and prints a fresh banner with a new token each time; the
    # earlier banners are stale (their in-memory tokens died with the
    # container) and would mislead the operator into copy-pasting an
    # invalidated value. Banners are exactly 7 lines (match line + 6
    # via ``-A 6``).
    banner=$(docker compose $compose_args logs \
            --no-color --no-log-prefix --tail 5000 opennvr-core 2>/dev/null \
        | grep -A 6 "first-time setup token" \
        | tail -7 \
        || true)

    if [ -n "$banner" ]; then
        echo ""
        echo -e "  ${YELLOW}🔑 First-time setup token (one-time use — copy into the UI):${NC}"
        echo ""
        echo "$banner" | sed 's/^/  /'
        echo ""
    else
        # Container healthy AND no banner = admin already activated
        # on a previous boot. Unambiguous now — give the operator the
        # right next step.
        local admin_user
        admin_user=$(get_env_var "DEFAULT_ADMIN_USERNAME" 2>/dev/null || echo "admin")
        admin_user=${admin_user:-admin}
        echo ""
        echo -e "  ${GREEN}First-time setup is already complete.${NC}"
        echo -e "  ${GRAY}Log in at ${CYAN}http://localhost:8000${GRAY} as ${WHITE}${admin_user}${GRAY}.${NC}"
        echo -e "  ${GRAY}(To re-arm the setup token, wipe the database volume and restart.)${NC}"
        echo ""
    fi
}

# ── Raw start / build (no front-door prompt) ───────────────
# These assume .env already exists — the smart `start` front door and the
# installer guarantee that before calling them. Kept as their own commands so
# the installer can `exec start.sh up` without re-triggering the front door
# (which would loop), and so power users can bypass the prompt.
INSTALLER="$(dirname "$0")/scripts/install.sh"

run_up() {
    if [ ! -f ".env" ]; then
        echo -e "${RED}  No .env found. Run ./start.sh (no arguments) to set up.${NC}"
        exit 1
    fi
    print_banner
    run_validate || exit 1
    ARGS=$(compose_args)
    configure_nginx_bind_host || exit 1
    configure_webrtc_ice_port || exit 1
    preflight_published_ports "$ARGS" || exit 1
    export_camera_lan_ips
    write_net_hints
    echo -e "  ${GREEN}Starting all services ...${NC}"
    docker compose $ARGS up -d --remove-orphans
    echo ""
    ADMIN_USER=$(get_env_var "DEFAULT_ADMIN_USERNAME")
    ADMIN_USER=${ADMIN_USER:-admin}
    print_access_urls "$ADMIN_USER"
    print_security_posture
    print_first_time_setup_token "$ARGS"
}

run_build() {
    if [ ! -f ".env" ]; then
        echo -e "${RED}  No .env found. Run ./start.sh (no arguments) to set up.${NC}"
        exit 1
    fi
    print_banner
    run_validate || exit 1
    ARGS=$(compose_args)
    configure_nginx_bind_host || exit 1
    configure_webrtc_ice_port || exit 1
    preflight_published_ports "$ARGS" || exit 1
    export_camera_lan_ips
    write_net_hints
    echo -e "  ${GREEN}Building images and starting all services ...${NC}"
    docker compose $ARGS build
    docker compose $ARGS up -d --remove-orphans
    echo ""
    ADMIN_USER=$(get_env_var "DEFAULT_ADMIN_USERNAME")
    ADMIN_USER=${ADMIN_USER:-admin}
    print_access_urls "$ADMIN_USER"
    print_security_posture
    print_first_time_setup_token "$ARGS"
}

# ── Smart front door (bare `./start.sh`) ───────────────────
# One command for everything:
#   * No .env yet            → run the interactive installer, which creates
#                              .env, configures it, builds, and starts.
#   * .env exists + a TTY    → ask whether to start as-is or reconfigure.
#   * .env exists, no TTY    → just start (CI / piped: never block on a prompt).
run_start() {
    if [ ! -f ".env" ]; then
        echo -e "  ${GREEN}First run — launching the OpenNVR installer ...${NC}"
        exec bash "$INSTALLER"
    fi

    if [ -t 0 ] && [ -t 1 ]; then
        echo ""
        echo -e "  ${WHITE}An existing OpenNVR configuration (.env) was found.${NC}"
        echo -e "    ${WHITE}1)${NC} ${GRAY}Start with the current configuration${NC}"
        echo -e "    ${WHITE}2)${NC} ${GRAY}Reconfigure (change settings / example), then start${NC}"
        echo -e "    ${WHITE}3)${NC} ${GRAY}Quit${NC}"
        echo ""
        local choice
        read -rp "  Your choice [1]: " choice
        choice="${choice:-1}"
        case "$choice" in
            1) run_up ;;
            2) exec bash "$INSTALLER" reconfigure ;;
            3) echo -e "  ${GRAY}Nothing started.${NC}"; exit 0 ;;
            *) echo -e "  ${RED}Invalid choice: $choice${NC}"; exit 1 ;;
        esac
    else
        run_up
    fi
}

# ── Run command ────────────────────────────────────────────
case "$COMMAND" in

  start)
    run_start
    ;;

  install|reconfigure)
    # Force the interactive installer in reconfigure mode (edit existing values).
    exec bash "$INSTALLER" reconfigure
    ;;

  up)
    run_up
    ;;

  build)
    run_build
    ;;

  restart)
    # Deliberately not `docker compose restart` / `docker restart`: those
    # replay the port bindings frozen into the container at creation time
    # and never re-run the ICE probe or the port pre-flight. That matters
    # most after a reboot, which is exactly when WinNAT re-rolls its
    # reserved ranges — the one moment the chosen port may need to change
    # is the one moment those commands cannot change it.
    #
    # Stopping first also frees the ports, so the probe can return to the
    # preferred ICE port when a previous run had to fall back.
    ARGS=$(compose_args) || exit 1
    echo -e "  ${YELLOW}Stopping all services ...${NC}"
    docker compose $ARGS stop
    run_up
    ;;

  down)
    print_banner
    ARGS=$(compose_args 2>/dev/null || echo "-f $COMPOSE_FILE")
    echo -e "  ${YELLOW}Stopping all services ...${NC}"
    docker compose $ARGS down
    echo -e "  ${GREEN}✓ All services stopped.${NC}"
    ;;

  logs)
    print_banner
    ARGS=$(compose_args 2>/dev/null || echo "-f $COMPOSE_FILE")
    echo -e "  ${GREEN}Tailing logs (Ctrl+C to exit) ...${NC}"
    docker compose $ARGS logs -f
    ;;

  status)
    ARGS=$(compose_args 2>/dev/null || echo "-f $COMPOSE_FILE")
    docker compose $ARGS ps
    ;;

  validate)
    print_banner
    run_validate
    ;;

  token)
    # Re-surface the first-time setup token on demand (e.g. if it scrolled off
    # or you started the stack outside this launcher). Mints nothing — it just
    # reads what opennvr-core already printed. If setup is already complete,
    # it says so and reminds you how to re-arm.
    ARGS=$(compose_args 2>/dev/null || echo "-f $COMPOSE_FILE")
    print_first_time_setup_token "$ARGS"
    ;;

  refresh-net)
    # Rewrite ./data/net-hints/host-ips from the host's CURRENT networks —
    # and nothing else: no validation, no compose, no container churn. The
    # file is bind-mounted read-only into opennvr-core and re-read on every
    # discovery request, so camera discovery follows a host that moved to a
    # new network the moment this finishes. The camera dialog's network
    # dropdown points operators here. An operator-set OPENNVR_HOST_IP /
    # OPENNVR_LAN_IPS (.env or environment) still wins over detection.
    if [ -z "${OPENNVR_HOST_IP:-}" ] && [ -z "$(get_env_var OPENNVR_HOST_IP 2>/dev/null || true)" ]; then
        detected_ip=$(detect_lan_ip)
        [ -n "$detected_ip" ] && export OPENNVR_HOST_IP="$detected_ip"
    fi
    export_camera_lan_ips
    write_net_hints
    if [ -f ./data/net-hints/host-ips ]; then
        echo -e "  ${GREEN}✓ Network hints refreshed:${NC} $(cat ./data/net-hints/host-ips)"
        echo -e "  ${GRAY}Camera discovery picks this up immediately — no restart needed.${NC}"
    else
        echo -e "  ${YELLOW}⚠ No LAN address detected; hint file cleared.${NC}"
    fi
    ;;

  refresh-certs)
    # ISSUE-6 v9: regenerate the TLS certs on demand. Used when:
    #   - The host's LAN IP changed (DHCP renewal, moved to a new
    #     network) and the cert SAN no longer matches.
    #   - The operator just set OPENNVR_HOST_IP in .env and wants
    #     the cert to pick it up without waiting for a full restart.
    #   - The existing certs expired (3650-day lifetime, unlikely
    #     but possible on long-running deployments).
    # Strategy: stop the services that hold the cert volumes open,
    # delete the cert directories, and re-up. The init containers
    # are idempotent (skip if cert exists), so deleting forces
    # regeneration on the next boot.
    print_banner
    ARGS=$(compose_args 2>/dev/null || echo "-f $COMPOSE_FILE")
    echo -e "  ${YELLOW}This will:${NC}"
    echo -e "  ${GRAY}    1. Stop nginx and mediamtx${NC}"
    echo -e "  ${GRAY}    2. Delete ./nginx-certs/ and ./mediamtx-certs/${NC}"
    echo -e "  ${GRAY}    3. Restart the stack so the init containers regenerate${NC}"
    echo -e "  ${GRAY}       fresh certs with the current OPENNVR_HOST_IP value.${NC}"
    echo ""
    if [ -t 0 ]; then
        read -rp "  Continue? [y/N]: " confirm
        if ! [[ "$confirm" =~ ^[Yy]$ ]]; then
            echo -e "  ${GRAY}Aborted. No changes made.${NC}"
            exit 0
        fi
    fi
    echo -e "  ${YELLOW}Stopping nginx and mediamtx ...${NC}"
    docker compose $ARGS stop nginx mediamtx 2>/dev/null || true
    echo -e "  ${YELLOW}Removing old certs ...${NC}"
    rm -rf ./nginx-certs ./mediamtx-certs
    echo -e "  ${GREEN}✓ Old certs removed.${NC}"
    echo -e "  ${YELLOW}Restarting stack to regenerate certs ...${NC}"
    configure_nginx_bind_host || exit 1
    configure_webrtc_ice_port || exit 1
    preflight_published_ports "$ARGS" || exit 1
    export_camera_lan_ips
    write_net_hints
    docker compose $ARGS up -d --remove-orphans
    echo ""
    echo -e "  ${GREEN}✓ Fresh certs will be generated by the init containers.${NC}"
    echo -e "  ${GRAY}You'll need to accept the new cert in your browser on next visit.${NC}"
    ;;

  *)
    echo -e "${RED}Unknown command: $COMMAND${NC}"
    echo "Usage: ./start.sh [start|restart|up|build|down|logs|status|validate|token|refresh-net|install|reconfigure|refresh-certs]"
    exit 1
    ;;
esac
