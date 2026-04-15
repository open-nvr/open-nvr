#!/usr/bin/env bash
# ============================================================
# OpenNVR - Smart Start Script (Linux / macOS)
# ============================================================
# Automatically detects your OS and picks the correct
# Docker Compose file:
#   - Linux  → docker-compose.linux.yml (host network mode)
#   - macOS  → docker-compose.yml       (bridge network mode)
#
# Usage:
#   chmod +x start.sh
#   ./start.sh              # start
#   ./start.sh build        # rebuild images and start
#   ./start.sh down         # stop all services
#   ./start.sh logs         # tail logs
#   ./start.sh status       # show container status
# ============================================================

set -e

# ── Colours ────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

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

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════╗"
echo "║           OpenNVR - Smart Launcher           ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  OS detected   : ${GREEN}${OS_LABEL}${NC}"
echo -e "  Compose file  : ${GREEN}${COMPOSE_FILE}${NC}"
echo -e "  Command       : ${GREEN}${COMMAND}${NC}"
echo ""

# ── Pre-flight checks ──────────────────────────────────────
# 1. Docker running?
if ! docker info > /dev/null 2>&1; then
  echo -e "${RED} Docker is not running. Please start Docker and try again.${NC}"
  exit 1
fi

# 2. Compose file exists?
if [ ! -f "$COMPOSE_FILE" ]; then
  echo -e "${RED} Compose file not found: $COMPOSE_FILE${NC}"
  exit 1
fi

# 3. .env file exists?
if [ ! -f ".env" ]; then
  echo -e "${YELLOW} No .env file found. Copying from .env.docker ...${NC}"
  cp .env.docker .env
  echo -e "${GREEN}✓ .env created. Edit it to customise settings.${NC}"
fi

# 4. AI model_weights directory — must be writable by aiuser (uid 1000) inside the container.
#    On a fresh git clone the directory is owned by root, so we always fix it here.
MODEL_WEIGHTS_DIR="../ai-adapter/model_weights"
mkdir -p "$MODEL_WEIGHTS_DIR"
if [ "$(stat -c '%u' "$MODEL_WEIGHTS_DIR")" != "1000" ]; then
  echo -e "${YELLOW} Fixing model_weights ownership for container user (uid 1000) ...${NC}"
  chown -R 1000:1000 "$MODEL_WEIGHTS_DIR"
  echo -e "${GREEN}✓ model_weights ownership fixed.${NC}"
fi

# ── Run command ────────────────────────────────────────────
case "$COMMAND" in
  up)
    echo -e "${GREEN} Starting all services ...${NC}"
    docker compose -f "$COMPOSE_FILE" up -d
    echo ""
    echo -e "${GREEN}✓ OpenNVR is running!${NC}"
    echo -e "  Web UI   → ${CYAN}http://localhost:8000${NC}  (admin / SecurePass123!)"
    echo -e "  API Docs → ${CYAN}http://localhost:8000/docs${NC}"
    ;;

  build)
    echo -e "${GREEN} Building images and starting all services ...${NC}"
    docker compose -f "$COMPOSE_FILE" build
    docker compose -f "$COMPOSE_FILE" up -d
    echo ""
    echo -e "${GREEN}✓ OpenNVR is running!${NC}"
    echo -e "  Web UI   → ${CYAN}http://localhost:8000${NC}  (admin / SecurePass123!)"
    echo -e "  API Docs → ${CYAN}http://localhost:8000/docs${NC}"
    ;;

  down)
    echo -e "${YELLOW} Stopping all services ...${NC}"
    docker compose -f "$COMPOSE_FILE" down
    echo -e "${GREEN}✓ All services stopped.${NC}"
    ;;

  logs)
    echo -e "${GREEN} Tailing logs (Ctrl+C to exit) ...${NC}"
    docker compose -f "$COMPOSE_FILE" logs -f
    ;;

  status)
    docker compose -f "$COMPOSE_FILE" ps
    ;;

  *)
    echo -e "${RED}Unknown command: $COMMAND${NC}"
    echo "Usage: ./start.sh [up|build|down|logs|status]"
    exit 1
    ;;
esac

