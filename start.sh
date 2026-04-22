#!/usr/bin/env bash
# ============================================================
# OpenNVR - Smart Launcher (Linux / macOS)
# ============================================================
# First run → launches the interactive installer automatically.
# Subsequent runs → validates and starts services.
#
# Usage:
#   ./start.sh              # start (or install on first run)
#   ./start.sh build        # rebuild images and start
#   ./start.sh install      # re-run the interactive installer
#   ./start.sh down         # stop all services
#   ./start.sh logs         # tail logs
#   ./start.sh status       # show container status
#   ./start.sh validate     # run pre-flight checks only
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
OS="$(uname -s)"
case "$OS" in
  Linux*)
    COMPOSE_FILE="docker-compose.linux.yml"
    OS_LABEL="Linux (host network mode)"
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

COMMAND="${1:-up}"

# ── Helper: read a value from .env ────────────────────────
get_env_var() {
    local key="$1"
    grep -E "^${key}=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'"
}

# ── Build Docker Compose profile args ─────────────────────
compose_args() {
    local ai_enabled
    ai_enabled=$(get_env_var "AI_ENABLED")
    local args="-f $COMPOSE_FILE"
    [[ "$ai_enabled" == "true" ]] && args="$args --profile ai"
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

# ── Run command ────────────────────────────────────────────
case "$COMMAND" in

  install)
    # Force re-run installer
    bash "$(dirname "$0")/scripts/install.sh"
    ;;

  up)
    # First run check
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}  No .env found — launching installer...${NC}"
        echo ""
        bash "$(dirname "$0")/scripts/install.sh"
        exit $?
    fi
    print_banner
    run_validate || exit 1
    ARGS=$(compose_args)
    echo -e "  ${GREEN}Starting all services ...${NC}"
    docker compose $ARGS up -d
    echo ""
    ADMIN_USER=$(get_env_var "DEFAULT_ADMIN_USERNAME")
    echo -e "  ${GREEN}✓ OpenNVR is running!${NC}"
    echo -e "  Web UI   → ${CYAN}http://localhost:8000${NC}  ${GRAY}(login: ${ADMIN_USER})${NC}"
    echo -e "  API Docs → ${CYAN}http://localhost:8000/docs${NC}"
    echo -e "  ${GRAY}First-time setup page opens automatically on first visit.${NC}"
    ;;

  build)
    # First run check
    if [ ! -f ".env" ]; then
        echo -e "${YELLOW}  No .env found — launching installer...${NC}"
        echo ""
        bash "$(dirname "$0")/scripts/install.sh"
        exit $?
    fi
    print_banner
    run_validate || exit 1
    ARGS=$(compose_args)
    echo -e "  ${GREEN}Building images and starting all services ...${NC}"
    docker compose $ARGS build
    docker compose $ARGS up -d
    echo ""
    ADMIN_USER=$(get_env_var "DEFAULT_ADMIN_USERNAME")
    echo -e "  ${GREEN}✓ OpenNVR is running!${NC}"
    echo -e "  Web UI   → ${CYAN}http://localhost:8000${NC}  ${GRAY}(login: ${ADMIN_USER})${NC}"
    echo -e "  API Docs → ${CYAN}http://localhost:8000/docs${NC}"
    echo -e "  ${GRAY}First-time setup page opens automatically on first visit.${NC}"
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

  *)
    echo -e "${RED}Unknown command: $COMMAND${NC}"
    echo "Usage: ./start.sh [up|build|down|logs|status|validate|install]"
    exit 1
    ;;
esac
