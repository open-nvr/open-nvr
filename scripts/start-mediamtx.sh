#!/usr/bin/env bash
# Start MediaMTX with environment variables from server/.env
# This script automatically loads MEDIAMTX_SECRET from server/.env
# and sets the necessary environment variables for local development.

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;37m'
NC='\033[0m' # No Color

# Show help
if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    cat << EOF
Start MediaMTX with environment variables

USAGE:
    ./scripts/start-mediamtx.sh

DESCRIPTION:
    This script loads MEDIAMTX_SECRET from server/.env and starts MediaMTX
    with the correct environment variables for local development.

ENVIRONMENT VARIABLES SET:
    - MEDIAMTX_SECRET: Loaded from server/.env
    - BACKEND_HOST: 127.0.0.1 (localhost)
    - BACKEND_PORT: 8000

REQUIREMENTS:
    - server/.env must exist with MEDIAMTX_SECRET defined
    - MediaMTX binary must be in mediamtx/ directory

EOF
    exit 0
fi

# Change to project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  OpenNVR - MediaMTX Startup Script${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if server/.env exists
ENV_FILE="$PROJECT_ROOT/server/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}❌ ERROR: server/.env file not found!${NC}"
    echo ""
    echo -e "${YELLOW}Please create server/.env from server/env.example and configure MEDIAMTX_SECRET${NC}"
    echo -e "${YELLOW}You can generate a secret with: openssl rand -hex 32${NC}"
    echo ""
    exit 1
fi

# Load MEDIAMTX_SECRET from server/.env
echo -e "${GREEN}📄 Loading configuration from server/.env...${NC}"
MEDIAMTX_SECRET=""

while IFS='=' read -r key value; do
    # Skip comments and empty lines
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    
    # Trim whitespace
    key=$(echo "$key" | xargs)
    value=$(echo "$value" | xargs)
    
    # Remove surrounding quotes from value
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    
    # Parse MEDIAMTX_SECRET
    if [ "$key" = "MEDIAMTX_SECRET" ]; then
        MEDIAMTX_SECRET="$value"
    fi
done < "$ENV_FILE"

# Validate MEDIAMTX_SECRET was found
if [ -z "$MEDIAMTX_SECRET" ]; then
    echo -e "${RED}❌ ERROR: MEDIAMTX_SECRET not found in server/.env!${NC}"
    echo ""
    echo -e "${YELLOW}Please add MEDIAMTX_SECRET to server/.env${NC}"
    echo -e "${YELLOW}Generate with: openssl rand -hex 32${NC}"
    echo ""
    exit 1
fi

# Set environment variables for MediaMTX
echo -e "${GREEN}🔐 Setting environment variables...${NC}"
export MEDIAMTX_SECRET="$MEDIAMTX_SECRET"
export BACKEND_HOST="127.0.0.1"
export BACKEND_PORT="8000"

echo -e "${GRAY}   ✓ MEDIAMTX_SECRET: ****${NC}"
echo -e "${GRAY}   ✓ BACKEND_HOST: $BACKEND_HOST${NC}"
echo -e "${GRAY}   ✓ BACKEND_PORT: $BACKEND_PORT${NC}"
echo ""

# Check if MediaMTX binary exists
MEDIAMTX_DIR="$PROJECT_ROOT/mediamtx"
MEDIAMTX_BIN="$MEDIAMTX_DIR/mediamtx"

if [ ! -f "$MEDIAMTX_BIN" ]; then
    echo -e "${RED}❌ ERROR: MediaMTX binary not found!${NC}"
    echo ""
    echo -e "${YELLOW}Expected location: mediamtx/mediamtx${NC}"
    echo -e "${YELLOW}Please download from: https://github.com/bluenviron/mediamtx/releases${NC}"
    echo ""
    exit 1
fi

# Make sure binary is executable
chmod +x "$MEDIAMTX_BIN"

# Check if mediamtx.yml exists
MEDIAMTX_CONFIG="$MEDIAMTX_DIR/mediamtx.yml"
if [ ! -f "$MEDIAMTX_CONFIG" ]; then
    echo -e "${RED}❌ ERROR: mediamtx.yml not found in mediamtx/ directory!${NC}"
    echo ""
    echo -e "${YELLOW}Please ensure mediamtx.yml is copied to mediamtx/ directory${NC}"
    echo ""
    exit 1
fi

# Display startup info
echo -e "${GREEN}🚀 Starting MediaMTX...${NC}"
echo -e "${GRAY}   Binary: $MEDIAMTX_BIN${NC}"
echo -e "${GRAY}   Config: $MEDIAMTX_CONFIG${NC}"
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Start MediaMTX
cd "$MEDIAMTX_DIR"
exec "$MEDIAMTX_BIN" "$MEDIAMTX_CONFIG"
