#!/usr/bin/env bash
# ============================================================
# OpenNVR - Secrets Generator (Linux / macOS)
# ============================================================
# Generates cryptographically secure secrets and writes them
# directly into the .env file (in the project root).
#
# Usage:
#   ./scripts/generate-secrets.sh          # dry-run: print only
#   ./scripts/generate-secrets.sh --write  # generate and write to .env
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
GRAY='\033[0;37m'
NC='\033[0m'

WRITE_MODE=false
if [ "${1}" = "--write" ]; then
    WRITE_MODE=true
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       OpenNVR - Secrets Generator            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Dependency checks ──────────────────────────────────────
if ! command -v openssl &>/dev/null; then
    echo -e "${RED}ERROR: openssl not found.${NC}"
    echo "  Ubuntu/Debian: sudo apt-get install openssl"
    echo "  macOS:         brew install openssl"
    exit 1
fi

PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo -e "${YELLOW}WARNING: Python not found — cannot generate CREDENTIAL_ENCRYPTION_KEY.${NC}"
    echo "  Install Python 3 and run: pip install cryptography"
fi

# ── Generate secrets ───────────────────────────────────────
echo -e "${GREEN}Generating secrets...${NC}"
echo ""

POSTGRES_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)
SECRET_KEY=$(openssl rand -hex 32)
INTERNAL_API_KEY=$(openssl rand -base64 32 | tr -d '\n')
MEDIAMTX_SECRET=$(openssl rand -hex 32)

CREDENTIAL_ENCRYPTION_KEY=""
if [ -n "$PYTHON_CMD" ]; then
    CREDENTIAL_ENCRYPTION_KEY=$($PYTHON_CMD -c \
        "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || true)
fi

if [ -z "$CREDENTIAL_ENCRYPTION_KEY" ]; then
    # A Fernet key is 32 random bytes, urlsafe-base64 encoded (44 chars,
    # padding included): openssl makes one without the cryptography
    # package. The example file's key is public, and the core refuses it,
    # so the key must be generated here, not skipped.
    CREDENTIAL_ENCRYPTION_KEY=$(openssl rand 32 | openssl base64 -A | tr '+/' '-_')
fi

# ── Display generated values ───────────────────────────────
echo -e "${GRAY}  POSTGRES_PASSWORD        = ${POSTGRES_PASSWORD}${NC}"
echo -e "${GRAY}  SECRET_KEY                = ${SECRET_KEY}${NC}"
echo -e "${GRAY}  CREDENTIAL_ENCRYPTION_KEY = ${CREDENTIAL_ENCRYPTION_KEY:-<skipped>}${NC}"
echo -e "${GRAY}  INTERNAL_API_KEY          = ${INTERNAL_API_KEY}${NC}"
echo -e "${GRAY}  MEDIAMTX_SECRET           = ${MEDIAMTX_SECRET}${NC}"
echo ""

# ── Write to .env ──────────────────────────────────────────
if [ "$WRITE_MODE" = false ]; then
    echo -e "${YELLOW}Dry-run mode — nothing written.${NC}"
    echo -e "Run with ${CYAN}--write${NC} to apply: ./scripts/generate-secrets.sh --write"
    echo ""
    exit 0
fi

# Ensure .env exists
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}.env not found — copying from .env.example ...${NC}"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo -e "${GREEN}✓ .env created from template.${NC}"
fi

# Helper: replace a key=value line in .env. Pure shell, line by line: GNU
# and BSD sed disagree on `-i` (macOS reads the expression as the backup
# suffix and writes nothing), and a generated value may hold any character.
replace_env_var() {
    local key="$1"
    local value="$2"
    local file="$3"
    local tmp found=false line
    tmp=$(mktemp "${file}.tmp.XXXXXX")
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*)
                if [ "$found" = false ]; then
                    printf '%s=%s\n' "$key" "$value" >> "$tmp"
                    found=true
                fi
                ;;
            *) printf '%s\n' "$line" >> "$tmp" ;;
        esac
    done < "$file"
    [ "$found" = true ] || printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$file"
}

echo -e "${GREEN}Writing secrets to .env ...${NC}"

replace_env_var "POSTGRES_PASSWORD"  "$POSTGRES_PASSWORD"  "$ENV_FILE"
replace_env_var "SECRET_KEY"         "$SECRET_KEY"         "$ENV_FILE"
replace_env_var "INTERNAL_API_KEY"   "$INTERNAL_API_KEY"   "$ENV_FILE"
replace_env_var "MEDIAMTX_SECRET"    "$MEDIAMTX_SECRET"    "$ENV_FILE"

if [ -n "$CREDENTIAL_ENCRYPTION_KEY" ]; then
    replace_env_var "CREDENTIAL_ENCRYPTION_KEY" "$CREDENTIAL_ENCRYPTION_KEY" "$ENV_FILE"
fi

echo ""
echo -e "${GREEN}✓ Secrets written to .env${NC}"
echo ""
echo -e "${YELLOW}IMPORTANT:${NC}"
echo "  1. Never commit .env to version control"
echo "  2. Store a secure backup of these secrets"
echo "  3. CREDENTIAL_ENCRYPTION_KEY cannot be changed after first run"
echo ""
echo -e "${GREEN}Ready to start:${NC}  ./start.sh build"
echo ""
